"""In-memory registry for background scene-import jobs.

``POST /projects/import/start`` streams the upload to disk, wraps the actual
import in a worker closure and hands it to :func:`start_import_job`, which
runs it on a daemon thread while recording phase/progress here for
``GET /projects/import/jobs/{job_id}`` to poll.

Deliberately process-local (no broker, no persistence): imports are
user-initiated, minutes-long at worst, and the registry only needs to outlive
the polling UI. State is lost on restart, which matches the UI's "retry the
import" recovery path.
"""

from __future__ import annotations

import threading
import uuid
from datetime import datetime, timezone
from typing import Callable, Optional

from app.schemas.projects import ProjectInfo

# Progress callback handed to a worker: ``cb(phase, done=0, total=0)``.
# See app.api.import_scene._run_import for the phase sequence it emits
# (extracting -> parsing -> writing -> done).
ProgressCb = Callable[..., None]

# Completed (done/error) jobs kept around for late pollers; older finished
# jobs are pruned when a new job is registered. Running jobs are never pruned.
_MAX_FINISHED_JOBS = 20

_lock = threading.Lock()
_jobs: dict[str, dict] = {}
# project_id -> job_id for jobs still running. A second import for the same id
# must 409 instead of racing the first thread into store.create_project.
_running_project_ids: dict[str, str] = {}


class ImportInProgressError(RuntimeError):
    """A running import job already claims this project id."""


def is_import_running(project_id: str) -> bool:
    """True while a registered job for this project id is still running."""
    with _lock:
        return project_id in _running_project_ids


def start_import_job(
    worker: Callable[[ProgressCb], tuple[ProjectInfo, list[str]]],
    *,
    project_id: Optional[str] = None,
) -> str:
    """Register a job and run ``worker(progress_cb)`` on a daemon thread.

    On return the job is ``status="done"`` with the project info + warnings;
    on exception ``status="error"`` with the exception's user-facing message
    (an ``HTTPException``'s ``.detail`` when present, else ``str(exc)``).

    ``project_id`` (when given) is reserved for the lifetime of the job;
    raises :class:`ImportInProgressError` if another running job already holds
    it. The reservation is released in the thread's ``finally`` even when the
    worker blows up.
    """
    job_id = uuid.uuid4().hex[:12]
    job = {
        "job_id": job_id,
        "status": "running",
        "phase": "queued",
        "done": 0,
        "total": 0,
        "project": None,
        "warnings": [],
        "error": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    with _lock:
        if project_id is not None:
            if project_id in _running_project_ids:
                raise ImportInProgressError(
                    f"import already in progress for project id: {project_id}"
                )
            _running_project_ids[project_id] = job_id
        _prune_finished_locked()
        _jobs[job_id] = job

    def _progress(phase: str, done: int = 0, total: int = 0) -> None:
        with _lock:
            j = _jobs.get(job_id)
            if j is not None and j["status"] == "running":
                j["phase"] = phase
                j["done"] = done
                j["total"] = total

    def _run() -> None:
        try:
            info, warnings = worker(_progress)
        except Exception as exc:  # noqa: BLE001 - job must record ANY failure
            # HTTPException carries the user-facing message in .detail; keep
            # the job error identical to what the sync endpoint would return.
            detail = getattr(exc, "detail", None)
            message = str(detail) if detail else (str(exc) or exc.__class__.__name__)
            with _lock:
                j = _jobs.get(job_id)
                if j is not None:
                    j["status"] = "error"
                    j["error"] = message
        else:
            with _lock:
                j = _jobs.get(job_id)
                if j is not None:
                    j["status"] = "done"
                    j["project"] = info.model_dump()
                    j["warnings"] = list(warnings)
        finally:
            if project_id is not None:
                with _lock:
                    if _running_project_ids.get(project_id) == job_id:
                        del _running_project_ids[project_id]

    threading.Thread(target=_run, name=f"import-job-{job_id}", daemon=True).start()
    return job_id


def get_import_job(job_id: str) -> Optional[dict]:
    """Snapshot copy of a job's state, or None for unknown/pruned ids."""
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            return None
        snap = dict(job)
        snap["warnings"] = list(job["warnings"])
        snap["project"] = dict(job["project"]) if job["project"] else None
        return snap


def _prune_finished_locked() -> None:
    """Drop the oldest finished jobs beyond the cap. Caller holds ``_lock``.

    Insertion order == creation order for ``_jobs``, so the front of the
    filtered list is the oldest.
    """
    finished = [jid for jid, j in _jobs.items() if j["status"] != "running"]
    excess = len(finished) - _MAX_FINISHED_JOBS
    if excess > 0:
        for jid in finished[:excess]:
            del _jobs[jid]
