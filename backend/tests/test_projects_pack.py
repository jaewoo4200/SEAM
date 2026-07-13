"""Project duplicate + rename API tests.

Covers the new management contracts:
  POST  /api/projects/{id}/duplicate  -> 201 ProjectInfo (a true fork: whole
                                         folder copied, scene_id rewritten)
  PATCH /api/projects/{id}            -> 200 ProjectInfo (scene name updated)

Everything runs against the shared tmp_path-backed ``api_client`` fixture
(conftest.py), so nothing touches the real project roots.
"""

import json
from pathlib import Path

import pytest

from app.services.project_store import LEGACY_SCENE_FILENAME

from .conftest import make_demo_scene

SRC_ID = "pack_src"


def _project_dir(client, project_id: str) -> Path:
    """Resolve a project's on-disk folder via the API (suffix not hard-coded)."""
    resp = client.get(f"/api/projects/{project_id}")
    assert resp.status_code == 200
    return Path(resp.json()["path"])


def _get_scene(client, project_id: str) -> dict:
    resp = client.get(f"/api/projects/{project_id}/scene")
    assert resp.status_code == 200
    return resp.json()


@pytest.fixture()
def client(api_client):
    """The shared tmp-rooted TestClient with one seeded source project.

    The source scene is the demo scene (devices included) and the folder gets
    results/ + ai/ artifacts so tests can prove duplication forks everything.
    """
    resp = api_client.post(
        "/api/projects", json={"name": "Pack Source", "project_id": SRC_ID}
    )
    assert resp.status_code == 201

    scene = make_demo_scene(scene_id=SRC_ID)
    put = api_client.put(
        f"/api/projects/{SRC_ID}/scene", json=scene.model_dump(mode="json")
    )
    assert put.status_code == 200

    project_dir = _project_dir(api_client, SRC_ID)
    (project_dir / "results" / "fork_evidence.json").write_text(
        json.dumps({"result_id": "fork_evidence"}), encoding="utf-8"
    )
    (project_dir / "ai" / "suggestions.jsonl").write_text(
        json.dumps({"event": "suggested"}) + "\n", encoding="utf-8"
    )
    return api_client


# --------------------------------------------------------------- duplicate


def test_duplicate_default_id_creates_openable_fork(client):
    resp = client.post(f"/api/projects/{SRC_ID}/duplicate")
    assert resp.status_code == 201
    info = resp.json()
    assert info["project_id"] == f"{SRC_ID}_copy"
    assert info["scene_id"] == f"{SRC_ID}_copy"
    # Name untouched when not provided (demo scene name).
    assert info["name"] == "Demo Scene"

    # The copy is a fully openable project with a rewritten scene_id.
    copy_scene = _get_scene(client, f"{SRC_ID}_copy")
    assert copy_scene["scene_id"] == f"{SRC_ID}_copy"

    # Devices (and prims) are identical to the source - a true fork.
    src_scene = _get_scene(client, SRC_ID)
    assert copy_scene["devices"] == src_scene["devices"]
    assert copy_scene["devices"]  # non-empty: the demo scene has a tx/rx pair
    assert copy_scene["prims"] == src_scene["prims"]

    # results/ and ai/ artifacts came along.
    copy_dir = _project_dir(client, f"{SRC_ID}_copy")
    assert (copy_dir / "results" / "fork_evidence.json").is_file()
    assert (copy_dir / "ai" / "suggestions.jsonl").is_file()
    # Modern folder suffix, sibling of the source.
    assert copy_dir.name == f"{SRC_ID}_copy.seam"
    assert copy_dir.parent == _project_dir(client, SRC_ID).parent

    # Source untouched.
    assert src_scene["scene_id"] == SRC_ID

    # Both listed side by side.
    ids = [p["project_id"] for p in client.get("/api/projects").json()]
    assert SRC_ID in ids and f"{SRC_ID}_copy" in ids


def test_duplicate_default_id_increments_on_collision(client):
    first = client.post(f"/api/projects/{SRC_ID}/duplicate")
    second = client.post(f"/api/projects/{SRC_ID}/duplicate")
    third = client.post(f"/api/projects/{SRC_ID}/duplicate")
    assert first.json()["project_id"] == f"{SRC_ID}_copy"
    assert second.json()["project_id"] == f"{SRC_ID}_copy2"
    assert third.json()["project_id"] == f"{SRC_ID}_copy3"
    # Every copy is independently openable with its own scene_id.
    for pid in (f"{SRC_ID}_copy", f"{SRC_ID}_copy2", f"{SRC_ID}_copy3"):
        assert _get_scene(client, pid)["scene_id"] == pid


def test_duplicate_explicit_id_and_name(client):
    resp = client.post(
        f"/api/projects/{SRC_ID}/duplicate",
        json={"new_id": "pack_fork", "name": "Forked Scene"},
    )
    assert resp.status_code == 201
    info = resp.json()
    assert info["project_id"] == "pack_fork"
    assert info["scene_id"] == "pack_fork"
    assert info["name"] == "Forked Scene"

    scene = _get_scene(client, "pack_fork")
    assert scene["scene_id"] == "pack_fork"
    assert scene["name"] == "Forked Scene"
    # Source keeps its own name.
    assert _get_scene(client, SRC_ID)["name"] == "Demo Scene"


def test_duplicate_explicit_id_collision_409(client):
    # Colliding with the source itself.
    resp = client.post(
        f"/api/projects/{SRC_ID}/duplicate", json={"new_id": SRC_ID}
    )
    assert resp.status_code == 409
    # Colliding with a previously made duplicate.
    assert (
        client.post(
            f"/api/projects/{SRC_ID}/duplicate", json={"new_id": "pack_fork"}
        ).status_code
        == 201
    )
    resp = client.post(
        f"/api/projects/{SRC_ID}/duplicate", json={"new_id": "pack_fork"}
    )
    assert resp.status_code == 409


def test_duplicate_unknown_source_404(client):
    resp = client.post("/api/projects/nope/duplicate")
    assert resp.status_code == 404


def test_duplicate_invalid_new_id_422(client):
    resp = client.post(
        f"/api/projects/{SRC_ID}/duplicate", json={"new_id": "Bad Id!"}
    )
    assert resp.status_code == 422


def test_duplicate_blank_name_422(client):
    resp = client.post(
        f"/api/projects/{SRC_ID}/duplicate", json={"name": "   "}
    )
    assert resp.status_code == 422


def test_duplicate_legacy_source_gets_modern_suffix(client):
    # Hand-build a legacy .sionnatwin project next to the seeded one.
    root = _project_dir(client, SRC_ID).parent
    legacy_dir = root / "old_proj.sionnatwin"
    legacy_dir.mkdir()
    (legacy_dir / LEGACY_SCENE_FILENAME).write_text(
        make_demo_scene(scene_id="old_proj").model_dump_json(indent=2),
        encoding="utf-8",
    )

    resp = client.post("/api/projects/old_proj/duplicate")
    assert resp.status_code == 201
    info = resp.json()
    assert info["project_id"] == "old_proj_copy"
    # The fork uses the modern folder suffix even though the source is legacy.
    assert Path(info["path"]).name == "old_proj_copy.seam"
    assert _get_scene(client, "old_proj_copy")["scene_id"] == "old_proj_copy"
    # The original legacy project is untouched.
    assert _get_scene(client, "old_proj")["scene_id"] == "old_proj"


# ------------------------------------------------------------------ rename


def test_rename_persists_and_reflects_in_list(client):
    resp = client.patch(
        f"/api/projects/{SRC_ID}", json={"name": "Renamed Pack"}
    )
    assert resp.status_code == 200
    info = resp.json()
    assert info["project_id"] == SRC_ID
    assert info["name"] == "Renamed Pack"

    # Persisted in the scene file (read back through the API).
    assert _get_scene(client, SRC_ID)["name"] == "Renamed Pack"

    # Reflected in the project list.
    listed = {p["project_id"]: p["name"] for p in client.get("/api/projects").json()}
    assert listed[SRC_ID] == "Renamed Pack"


def test_rename_strips_whitespace(client):
    resp = client.patch(
        f"/api/projects/{SRC_ID}", json={"name": "  Padded Name  "}
    )
    assert resp.status_code == 200
    assert resp.json()["name"] == "Padded Name"


def test_rename_blank_name_422(client):
    assert (
        client.patch(f"/api/projects/{SRC_ID}", json={"name": ""}).status_code == 422
    )
    assert (
        client.patch(f"/api/projects/{SRC_ID}", json={"name": "   "}).status_code
        == 422
    )
    assert client.patch(f"/api/projects/{SRC_ID}", json={}).status_code == 422


def test_rename_unknown_project_404(client):
    resp = client.patch("/api/projects/nope", json={"name": "X"})
    assert resp.status_code == 404
