"""Compute-engine registry + subprocess job runner.

The builtin engine is the sionna-rt import in this process' venv. Additional
engines are separate venvs holding other sionna-rt versions, declared in
engines.json at the repo root:

    {"engines": [{"id": "sionna-rt-1.2.2",
                   "label": "Sionna RT 1.2.2",
                   "python": "backend/.venv-sionna-rt-122/Scripts/python.exe",
                   "adapter": "sionna_rt"}]}

Relative python paths resolve against the repo root, so the manifest is
portable across checkouts. Availability is probed by importing sionna.rt in
the target venv (cached per process; refresh=True re-probes). Jobs run through
engine_workers/sionna_rt_worker.py with a file-based JSON protocol - see the
worker docstring. Rationale for version switching: docs/sionna_versions.md.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path
from typing import Optional

from ..core.paths import REPO_ROOT, SEAM_HOME
from ..schemas.engines import EngineInfo

# Registry + worker locations: repo root in a source checkout, the user's
# SEAM home (~/.seam) for a pip-installed run. Both absent-safe — no
# engines.json simply means "builtin engine only".
_ANCHOR = REPO_ROOT if REPO_ROOT is not None else SEAM_HOME
ENGINES_FILE = _ANCHOR / "engines.json"
WORKERS_DIR = (
    REPO_ROOT / "backend" / "engine_workers"
    if REPO_ROOT is not None
    else SEAM_HOME / "engine_workers"
)

# Importing sionna.rt loads mitsuba/drjit; cold imports take tens of seconds.
PROBE_TIMEOUT_S = 120
JOB_TIMEOUT_S = 600

_probe_cache: dict[str, tuple[bool, Optional[str], str]] = {}


class EngineError(RuntimeError):
    """Engine job failed; message carries worker stderr/error context."""


def _builtin_info() -> EngineInfo:
    try:
        import sionna.rt as rt  # type: ignore[import-not-found]

        version = getattr(rt, "__version__", "unknown")
        return EngineInfo(
            id="builtin", label=f"Sionna RT {version} (builtin)", kind="builtin",
            adapter="builtin", available=True, version=version,
            detail="in-process engine of the backend venv",
        )
    except Exception as exc:  # noqa: BLE001 - report, never raise
        return EngineInfo(
            id="builtin", label="Sionna RT (builtin)", kind="builtin",
            adapter="builtin", available=False, detail=f"import failed: {exc}",
        )


def _resolve_python(entry_python: str) -> Path:
    # Relative entries resolve against wherever engines.json lives (repo root
    # in a checkout, SEAM home when installed).
    p = Path(entry_python)
    return p if p.is_absolute() else (_ANCHOR / p)


def _probe(python: Path, refresh: bool) -> tuple[bool, Optional[str], str]:
    key = str(python)
    if not refresh and key in _probe_cache:
        return _probe_cache[key]
    if not python.is_file():
        result = (False, None, f"interpreter not found: {python}")
    else:
        try:
            # The interpreter version rides along: sionna-rt <= 1.1 pins
            # mitsuba/drjit wheels that stop at cp313, so `pip install`
            # SUCCEEDS on Python 3.14 but the import breaks — the version in
            # the failure detail is what makes that self-diagnosing.
            proc = subprocess.run(
                [str(python), "-c",
                 "import sys; import sionna.rt as rt; "
                 "print(getattr(rt, '__version__', 'unknown')); "
                 "print('py%d.%d' % sys.version_info[:2])"],
                capture_output=True, text=True, timeout=PROBE_TIMEOUT_S,
            )
            lines = proc.stdout.strip().splitlines()
            if proc.returncode == 0 and lines:
                result = (True, lines[0], "")
            else:
                pyv = ""
                try:
                    vp = subprocess.run(
                        [str(python), "-c", "import sys; print('%d.%d' % sys.version_info[:2])"],
                        capture_output=True, text=True, timeout=10,
                    )
                    pyv = f" (Python {vp.stdout.strip()})" if vp.returncode == 0 else ""
                except (OSError, subprocess.TimeoutExpired):
                    pass
                tail = (proc.stderr or "").strip().splitlines()[-3:]
                result = (
                    False,
                    None,
                    "probe failed" + pyv + ": " + " | ".join(tail)
                    + " — fix the venv, then GET /api/engines?refresh=true",
                )
        except (OSError, subprocess.TimeoutExpired) as exc:
            result = (False, None, f"probe error: {exc}")
    # Only successes are cached: a failed probe would otherwise stick for the
    # process lifetime even after the user rebuilds the venv.
    if result[0]:
        _probe_cache[key] = result
    return result


def list_engines(refresh: bool = False) -> list[EngineInfo]:
    engines = [_builtin_info()]
    if not ENGINES_FILE.is_file():
        return engines
    try:
        manifest = json.loads(ENGINES_FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        engines[0].detail += f" | engines.json unreadable: {exc}"
        return engines
    for entry in manifest.get("engines", []):
        try:
            python = _resolve_python(entry["python"])
            available, version, detail = _probe(python, refresh)
            engines.append(EngineInfo(
                id=entry["id"],
                label=entry.get("label", entry["id"]),
                kind="subprocess",
                adapter=entry.get("adapter", "sionna_rt"),
                python=str(python),
                available=available,
                version=version,
                detail=detail,
            ))
        except (KeyError, TypeError) as exc:
            # A malformed entry must not hide the others.
            engines.append(EngineInfo(
                id=str(entry.get("id", "invalid")), label="invalid engines.json entry",
                available=False, detail=f"bad entry: {exc}",
            ))
    return engines


def get_engine(engine_id: str, refresh: bool = False) -> Optional[EngineInfo]:
    for engine in list_engines(refresh=refresh):
        if engine.id == engine_id:
            return engine
    return None


def run_paths_job(engine: EngineInfo, job: dict, timeout_s: int = JOB_TIMEOUT_S) -> dict:
    """Run a paths job in the engine venv; returns the worker's result dict."""
    if engine.kind != "subprocess" or not engine.python:
        raise EngineError(f"engine '{engine.id}' is not a subprocess engine")
    worker = WORKERS_DIR / f"{engine.adapter}_worker.py"
    if not worker.is_file():
        raise EngineError(f"no worker for adapter '{engine.adapter}' ({worker})")

    with tempfile.TemporaryDirectory(prefix="stw_engine_") as tmp:
        job_path = Path(tmp) / "job.json"
        out_path = Path(tmp) / "out.json"
        job_path.write_text(json.dumps(job), encoding="utf-8")
        try:
            proc = subprocess.run(
                [engine.python, str(worker), str(job_path), str(out_path)],
                capture_output=True, text=True, timeout=timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise EngineError(f"engine '{engine.id}' timed out after {timeout_s}s") from exc
        if not out_path.is_file():
            tail = (proc.stderr or "").strip().splitlines()[-5:]
            raise EngineError(
                f"engine '{engine.id}' produced no result (exit {proc.returncode}): "
                + " | ".join(tail)
            )
        result = json.loads(out_path.read_text(encoding="utf-8"))
    if not result.get("ok"):
        raise EngineError(
            f"engine '{engine.id}' failed: {(result.get('error') or 'unknown').strip()}"
        )
    return result
