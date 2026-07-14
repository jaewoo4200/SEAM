"""Tests for the background import job flow.

POST /projects/import/start (202 + job_id) + GET /projects/import/jobs/{id}
polling, the running-project-id guard, and the job registry itself. Reuses the
self-contained tiny-scene fixture approach from test_import_api.py: a minimal
Mitsuba XML plus the .ply meshes it references, all built at runtime.
"""

import threading
import time

import trimesh

from seam_studio.schemas.projects import ProjectInfo
from seam_studio.services import import_jobs

# Minimal Sionna/Mitsuba XML: two ITU materials + two ply shapes (same shape
# as test_import_api.SCENE_XML).
SCENE_XML = """<?xml version='1.0' encoding='utf-8'?>
<scene version="3.0.0">
  <bsdf type="twosided" id="mat-itu_concrete">
    <bsdf type="diffuse"><rgb name="reflectance" value="0.52 0.52 0.50"/></bsdf>
  </bsdf>
  <bsdf type="twosided" id="mat-itu_glass">
    <bsdf type="diffuse"><rgb name="reflectance" value="0.30 0.55 0.75"/></bsdf>
  </bsdf>
  <shape type="ply" id="mesh-wall">
    <string name="filename" value="meshes/wall.ply"/>
    <ref id="mat-itu_concrete"/>
  </shape>
  <shape type="ply" id="mesh-window">
    <string name="filename" value="meshes/window.ply"/>
    <ref id="mat-itu_glass"/>
  </shape>
</scene>
"""

_JOB_TIMEOUT_S = 30.0


def _ply_bytes() -> bytes:
    return trimesh.creation.box(extents=[1.0, 1.0, 1.0]).export(file_type="ply")


def _import_files():
    """Multipart file tuples for a valid two-shape upload."""
    ply = _ply_bytes()
    return [
        ("file", ("scene.xml", SCENE_XML.encode("utf-8"), "application/xml")),
        ("meshes", ("wall.ply", ply, "application/octet-stream")),
        ("meshes", ("window.ply", ply, "application/octet-stream")),
    ]


def _poll_until_finished(api_client, job_id: str):
    """Poll the job endpoint until status != running; return (job, phases)."""
    deadline = time.monotonic() + _JOB_TIMEOUT_S
    phases: list[str] = []
    while time.monotonic() < deadline:
        resp = api_client.get(f"/api/projects/import/jobs/{job_id}")
        assert resp.status_code == 200, resp.text
        job = resp.json()
        if job["phase"] and (not phases or phases[-1] != job["phase"]):
            phases.append(job["phase"])
        if job["status"] != "running":
            return job, phases
        time.sleep(0.05)
    raise AssertionError(f"import job {job_id} still running after {_JOB_TIMEOUT_S}s")


class TestImportJobFlow:
    def test_job_import_creates_loadable_project(self, api_client):
        resp = api_client.post(
            "/api/projects/import/start",
            files=_import_files(),
            data={"project_id": "job_lab", "name": "Job Lab", "environment": "indoor"},
        )
        assert resp.status_code == 202, resp.text
        job_id = resp.json()["job_id"]

        job, phases = _poll_until_finished(api_client, job_id)
        assert job["status"] == "done", job
        assert job["error"] is None
        assert job["project"]["project_id"] == "job_lab"
        assert job["project"]["name"] == "Job Lab"
        assert isinstance(job["warnings"], list)
        # Progress ended at the terminal phase with a full bar.
        assert job["phase"] == "done"
        assert (job["done"], job["total"]) == (4, 4)
        # Polling can miss intermediate phases on a fast import, but whatever
        # was observed must be the documented sequence ending at "done".
        assert phases[-1] == "done"
        assert [p for p in phases if p != "queued"] == [
            p for p in ("extracting", "parsing", "writing", "done") if p in phases
        ]

        # The project actually opens: scene loads with the imported prims.
        scene = api_client.get("/api/projects/job_lab/scene")
        assert scene.status_code == 200, scene.text
        body = scene.json()
        assert body["environment"] == "indoor"
        assert len(body["prims"]) == 2

    def test_duplicate_project_id_start_conflicts(self, api_client):
        created = api_client.post("/api/projects", json={"name": "Taken", "project_id": "taken_id"})
        assert created.status_code == 201, created.text
        resp = api_client.post(
            "/api/projects/import/start",
            files=_import_files(),
            data={"project_id": "taken_id", "name": "Dup", "environment": "auto"},
        )
        assert resp.status_code == 409, resp.text
        assert "already exists" in resp.json()["detail"]

    def test_unknown_job_id_404(self, api_client):
        resp = api_client.get("/api/projects/import/jobs/no_such_job")
        assert resp.status_code == 404
        assert "unknown import job" in resp.json()["detail"]

    def test_second_start_while_running_conflicts(self, api_client):
        # Deterministic stand-in for a slow import: a registered job that
        # blocks on an event while holding the project id reservation.
        release = threading.Event()

        def blocked_worker(progress):
            progress("parsing", 2, 4)
            release.wait(timeout=_JOB_TIMEOUT_S)
            return ProjectInfo(project_id="held_id", name="Held", path="unused"), []

        job_id = import_jobs.start_import_job(blocked_worker, project_id="held_id")
        try:
            assert import_jobs.is_import_running("held_id")
            resp = api_client.post(
                "/api/projects/import/start",
                files=_import_files(),
                data={"project_id": "held_id", "name": "Held 2", "environment": "auto"},
            )
            assert resp.status_code == 409, resp.text
            assert "already in progress" in resp.json()["detail"]
        finally:
            release.set()
        # Wait for the fake job to release the reservation so it cannot leak
        # into other tests in this process.
        deadline = time.monotonic() + _JOB_TIMEOUT_S
        while import_jobs.is_import_running("held_id"):
            assert time.monotonic() < deadline, "blocked job never released its id"
            time.sleep(0.02)
        assert import_jobs.get_import_job(job_id)["status"] == "done"

    def test_failed_import_surfaces_error(self, api_client):
        # XML without its meshes: every shape is skipped, the worker raises the
        # same 400 the sync endpoint would return, and the job records .detail.
        resp = api_client.post(
            "/api/projects/import/start",
            files=[("file", ("scene.xml", SCENE_XML.encode("utf-8"), "application/xml"))],
            data={"project_id": "job_no_meshes", "name": "No Meshes", "environment": "auto"},
        )
        assert resp.status_code == 202, resp.text
        job, _phases = _poll_until_finished(api_client, resp.json()["job_id"])
        assert job["status"] == "error", job
        assert job["project"] is None
        assert "were not found" in job["error"]
        # No half-imported project was left behind.
        listed = {p["project_id"] for p in api_client.get("/api/projects").json()}
        assert "job_no_meshes" not in listed

    def test_sync_endpoint_still_works(self, api_client):
        # The refactor extracted the sync body into _run_import; prove the
        # original blocking endpoint is intact end to end.
        resp = api_client.post(
            "/api/projects/import",
            files=_import_files(),
            data={"project_id": "sync_lab", "name": "Sync Lab", "environment": "indoor"},
        )
        assert resp.status_code == 201, resp.text
        info = resp.json()
        assert info["project_id"] == "sync_lab"
        assert isinstance(info["warnings"], list)
        assert api_client.get("/api/projects/sync_lab/scene").status_code == 200


class TestJobRegistry:
    def _instant_job(self, i: int) -> str:
        def worker(progress):
            progress("done", 4, 4)
            return ProjectInfo(project_id=f"reg_{i}", name=f"Reg {i}", path="unused"), []

        return import_jobs.start_import_job(worker)

    def _wait_finished(self, job_id: str) -> None:
        deadline = time.monotonic() + _JOB_TIMEOUT_S
        while import_jobs.get_import_job(job_id)["status"] == "running":
            assert time.monotonic() < deadline
            time.sleep(0.01)

    def test_finished_jobs_are_pruned_to_cap(self):
        ids = []
        for i in range(import_jobs._MAX_FINISHED_JOBS + 5):
            job_id = self._instant_job(i)
            self._wait_finished(job_id)
            ids.append(job_id)
        # One more insert prunes the oldest finished jobs down to the cap.
        final = self._instant_job(999)
        self._wait_finished(final)
        assert import_jobs.get_import_job(ids[0]) is None
        assert import_jobs.get_import_job(final)["status"] == "done"

    def test_snapshot_is_a_copy(self):
        job_id = self._instant_job(0)
        self._wait_finished(job_id)
        snap = import_jobs.get_import_job(job_id)
        snap["warnings"].append("mutated")
        snap["status"] = "error"
        fresh = import_jobs.get_import_job(job_id)
        assert fresh["warnings"] == []
        assert fresh["status"] == "done"
