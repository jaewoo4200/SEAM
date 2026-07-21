"""Project deletion + results-prune API tests (mock backend only).

Covers the new management contracts:
  DELETE /api/projects/{id}                -> {"deleted": true, "project_id": id}
  POST   /api/projects/{id}/results/prune  -> {"removed": [...], "kept": [...]}

Everything runs against a tmp_path-backed project root with the mock backend
forced, so nothing depends on Sionna RT being installed.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from .test_mock_backend import make_scene

# Force the deterministic mock backend for every solve here.
MOCK_REQ = {"config": {"backend": "mock"}}


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient over a temp project root holding one ready-to-simulate project."""
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))

    from seam_studio.api.deps import get_store
    from seam_studio.core.config import get_settings

    get_settings.cache_clear()
    get_store.cache_clear()

    from seam_studio.main import app

    store = get_store()
    store.create_project("Sim Test", project_id="sim_test")
    store.save_scene("sim_test", make_scene())

    client = TestClient(app)
    try:
        yield client, tmp_path
    finally:
        get_settings.cache_clear()
        get_store.cache_clear()


def _project_dir(client) -> Path:
    """Resolve the on-disk project folder via the API.

    The folder suffix is intentionally not hard-coded here: the store writes
    ``.seam`` but still reads legacy ``.sionnatwin`` projects, so tests read
    the real path from GET /projects/{id} instead of assuming one.
    """
    resp = client.get("/api/projects/sim_test")
    assert resp.status_code == 200
    return Path(resp.json()["path"])


def _results_dir(client) -> Path:
    return _project_dir(client) / "results"


def _seed_results(client) -> None:
    """Two paths results + one radio_map result, in that chronological order."""
    for _ in range(2):
        r = client.post("/api/projects/sim_test/simulate/paths", json=MOCK_REQ)
        assert r.status_code == 200
    r = client.post("/api/projects/sim_test/simulate/radio-map", json=MOCK_REQ)
    assert r.status_code == 200


def _scene_file(project_dir: Path) -> Path:
    """The canonical scene file, whichever suffix this project uses."""
    for name in ("scene.seam.json", "scene.sionnatwin.json"):
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    raise AssertionError(f"no scene file found in {project_dir}")


def _scene_refs(project_dir: Path):
    scene = json.loads(_scene_file(project_dir).read_text("utf-8"))
    return scene["result_sets"]


def _provenance_events(project_dir: Path):
    prov = json.loads((project_dir / "provenance.json").read_text("utf-8"))
    return prov.get("events", [])


# ------------------------------------------------------------------- prune


def test_prune_keep_latest_one_keeps_newest_paths_and_radio_map(api_client):
    client, _root = api_client
    _seed_results(client)
    project_dir = _project_dir(client)
    results_dir = project_dir / "results"

    # Sanity: 3 refs, 3 files before pruning.
    assert len(_scene_refs(project_dir)) == 3
    assert sorted(p.name for p in results_dir.glob("*.json")) == [
        "mock_paths_001.json",
        "mock_paths_002.json",
        "mock_radio_map_001.json",
    ]

    resp = client.post(
        "/api/projects/sim_test/results/prune", json={"keep_latest": 1}
    )
    assert resp.status_code == 200
    body = resp.json()

    # Oldest paths ref removed; newest paths + the single radio_map survive.
    assert body["removed"] == ["mock_paths_001"]
    assert set(body["kept"]) == {"mock_paths_002", "mock_radio_map_001"}

    # Files on disk match the surviving refs exactly.
    remaining = sorted(p.name for p in results_dir.glob("*.json"))
    assert remaining == ["mock_paths_002.json", "mock_radio_map_001.json"]

    # Scene refs consistent with the response (order preserved).
    ref_ids = [r["result_id"] for r in _scene_refs(project_dir)]
    assert ref_ids == ["mock_paths_002", "mock_radio_map_001"]

    # Exactly one results_pruned provenance event with the removed count.
    pruned = [
        e for e in _provenance_events(project_dir) if e["type"] == "results_pruned"
    ]
    assert len(pruned) == 1
    assert pruned[0]["removed_count"] == 1


def test_prune_kinds_filter_only_touches_named_kind(api_client):
    client, _root = api_client
    _seed_results(client)
    project_dir = _project_dir(client)
    results_dir = project_dir / "results"

    resp = client.post(
        "/api/projects/sim_test/results/prune",
        json={"keep_latest": 0, "kinds": ["paths"]},
    )
    assert resp.status_code == 200
    body = resp.json()

    # Both paths refs dropped; radio_map untouched despite keep_latest=0.
    assert set(body["removed"]) == {"mock_paths_001", "mock_paths_002"}
    assert body["kept"] == ["mock_radio_map_001"]

    remaining = sorted(p.name for p in results_dir.glob("*.json"))
    assert remaining == ["mock_radio_map_001.json"]
    assert [r["result_id"] for r in _scene_refs(project_dir)] == ["mock_radio_map_001"]


def test_prune_default_keep_latest_zero_sweeps_all_kinds(api_client):
    client, _root = api_client
    _seed_results(client)
    project_dir = _project_dir(client)
    results_dir = project_dir / "results"

    # No body -> keep_latest defaults to 0, kinds defaults to all.
    resp = client.post("/api/projects/sim_test/results/prune")
    assert resp.status_code == 200
    body = resp.json()

    assert set(body["removed"]) == {
        "mock_paths_001",
        "mock_paths_002",
        "mock_radio_map_001",
    }
    assert body["kept"] == []
    assert list(results_dir.glob("*.json")) == []
    assert _scene_refs(project_dir) == []


def test_prune_missing_file_still_drops_ref(api_client):
    client, _root = api_client
    _seed_results(client)
    project_dir = _project_dir(client)

    # Delete one result file out from under the store; the ref must still drop.
    (project_dir / "results" / "mock_paths_001.json").unlink()

    resp = client.post(
        "/api/projects/sim_test/results/prune", json={"keep_latest": 1}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["removed"] == ["mock_paths_001"]
    assert "mock_paths_001" not in [r["result_id"] for r in _scene_refs(project_dir)]


def test_prune_unknown_project_404(api_client):
    client, _root = api_client
    resp = client.post("/api/projects/nope/results/prune", json={"keep_latest": 1})
    assert resp.status_code == 404


def test_prune_negative_keep_latest_422(api_client):
    client, _root = api_client
    resp = client.post(
        "/api/projects/sim_test/results/prune", json={"keep_latest": -1}
    )
    assert resp.status_code == 422


# ------------------------------------------------------------------ delete


def test_delete_project_removes_folder_and_makes_get_404(api_client):
    client, _root = api_client
    project_dir = _project_dir(client)  # capture path before deletion
    assert project_dir.is_dir()

    resp = client.delete("/api/projects/sim_test")
    assert resp.status_code == 200
    assert resp.json() == {"deleted": True, "project_id": "sim_test"}

    # Folder gone from disk.
    assert not project_dir.exists()
    # And the project is no longer resolvable.
    assert client.get("/api/projects/sim_test").status_code == 404
    assert "sim_test" not in [p["project_id"] for p in client.get("/api/projects").json()]


def test_delete_unknown_project_404(api_client):
    client, _root = api_client
    resp = client.delete("/api/projects/nope")
    assert resp.status_code == 404
