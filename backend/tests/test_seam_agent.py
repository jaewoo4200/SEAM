"""SEAM-Agent tests (retrieval-augmented RF material authoring).

Everything here runs OFFLINE: the ``_offline`` autouse fixture replaces the
agent's three network tools (_tool_web_search, _tool_image_search, _vlm_chat)
with functions that raise loudly, so any test that reaches the network without
patching a fixed VLM answer fails instead of silently hitting the wire.

The pipeline runs in a background thread (seam_agent.start_job); tests drive it
on a tiny synthetic box project (one mesh prim + GLB, no texture required - the
agent resolves geometry via seg._resolve_prim_geometry) and poll get_job until
the job reaches a terminal state.
"""

import base64
import io
import time
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from seam_studio.api import deps
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene, VisualBinding
from seam_studio.services import material_segmentation as seg
from seam_studio.services import seam_agent

MESH_NAME = "agent_box"
PRIM_ID = "/buildings/b01/box"


# --------------------------------------------------------------- offline guard


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """Fail loudly if any test hits the network without patching a tool.

    Individual tests that need a VLM answer monkeypatch _vlm_chat AFTER this
    fixture runs (later setattr wins), so this only bites unpatched paths.
    """

    def _no_web(*a, **k):
        raise AssertionError("_tool_web_search hit the network in an offline test")

    def _no_img(*a, **k):
        raise AssertionError("_tool_image_search hit the network in an offline test")

    def _no_vlm(*a, **k):
        raise AssertionError("_vlm_chat hit the network in an offline test")

    monkeypatch.setattr(seam_agent, "_tool_web_search", _no_web)
    monkeypatch.setattr(seam_agent, "_tool_image_search", _no_img)
    monkeypatch.setattr(seam_agent, "_vlm_chat", _no_vlm)


# ------------------------------------------------------------------ fixtures


def _make_box_project(project_id: str) -> tuple[Path, Scene]:
    """One untextured box prim exported to visual/scene.glb; return (dir, scene).

    A trimesh box has 12 faces; the agent resolves it purely by mesh name via
    seg._resolve_prim_geometry (no texture / UVs needed for the agent path).
    """
    store = deps.get_store()
    store.create_project(project_id, project_id=project_id)
    project_dir = store.resolve(project_id)

    box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    tm = trimesh.Scene()
    tm.add_geometry(box, geom_name=MESH_NAME, node_name=MESH_NAME)
    (project_dir / "visual").mkdir(parents=True, exist_ok=True)
    (project_dir / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))

    scene = Scene(
        scene_id=project_id,
        name=project_id,
        prims=[
            Prim(
                id=PRIM_ID,
                name="box",
                type="mesh_primitive",
                semantic_tags=["building"],
                mesh_ref=MeshRef(asset_uri="visual/scene.glb", mesh_name=MESH_NAME),
                visual=VisualBinding(material_name="plain"),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="rule_assigned",
                    assignment_sources=["rule_based"],
                    confidence=0.6,
                ),
            )
        ],
    )
    store.save_scene(project_id, scene)
    return project_dir, scene


def _rgb_data_url(w: int = 8, h: int = 8) -> str:
    """A tiny solid JPEG data URL (the RGB the VLM would 'see')."""
    img = Image.new("RGB", (w, h), (120, 120, 120))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def _tri_id_png(id_grid: np.ndarray) -> str:
    """PNG data URL encoding a face-id grid as uint24 (r<<16|g<<8|b).

    id_grid values are face indices; use 0xFFFFFF for background. Mirrors the FE
    triangle-id buffer the decoder reverses.
    """
    ids = id_grid.astype(np.uint32)
    r = ((ids >> 16) & 0xFF).astype(np.uint8)
    g = ((ids >> 8) & 0xFF).astype(np.uint8)
    b = (ids & 0xFF).astype(np.uint8)
    arr = np.stack([r, g, b], axis=-1)
    img = Image.fromarray(arr, mode="RGB")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buf.getvalue()).decode()


def _view(view_id: str, id_grid: np.ndarray) -> dict:
    h, w = id_grid.shape
    return {
        "view_id": view_id,
        "rgb_data_url": _rgb_data_url(w, h),
        "tri_id_png_data_url": _tri_id_png(id_grid),
        "width": w,
        "height": h,
    }


def _wait_for_terminal(job_id: str, timeout: float = 10.0):
    """Poll until the background job reaches a terminal state, then return it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = seam_agent.get_job(job_id)
        if job is not None and job.status in ("needs_review", "done", "error"):
            return job
        time.sleep(0.02)
    raise AssertionError(f"job {job_id} did not finish within {timeout}s")


# ------------------------------------------------------------- _extract_json


def test_extract_json_valid_object():
    assert seam_agent._extract_json('{"a": 1, "b": [2, 3]}') == {"a": 1, "b": [2, 3]}


def test_extract_json_embedded_in_prose():
    text = 'Sure! Here is the labeling:\n{"regions": [{"label": "roof"}]}\nDone.'
    assert seam_agent._extract_json(text) == {"regions": [{"label": "roof"}]}


def test_extract_json_malformed_returns_none():
    assert seam_agent._extract_json("no json here at all") is None
    assert seam_agent._extract_json('{"unbalanced": [1, 2') is None


# ---------------------------------------------------- decode out-of-range guard


def test_decode_drops_out_of_range_ids(api_client, monkeypatch):
    """An 8x8 tri-id buffer carrying a face id past the mesh's face count is
    dropped during decode (no IndexError past the vote table)."""
    project_dir, scene = _make_box_project("agent_decode")

    # The box has 12 faces (ids 0..11). Fill the grid with a valid id, then poke
    # one pixel with an out-of-range id and one background pixel.
    grid = np.zeros((8, 8), dtype=np.uint32)  # face 0 everywhere
    grid[0, 0] = 999           # out of range -> must be dropped, not indexed
    grid[7, 7] = 0xFFFFFF      # background sentinel

    def fake_vlm(prompt, images, model, max_tokens=1500):
        # One region covering the whole view, labeled a valid class.
        return '{"regions": [{"label": "exterior_wall", "bbox": [0,0,1,1], "confidence": 0.9}]}'

    monkeypatch.setattr(seam_agent, "_vlm_chat", fake_vlm)

    job_id = seam_agent.start_job(
        project_dir, scene, PRIM_ID, [_view("v0", grid)],
        user_hint=None, allow_web=False, model=None, budget=seam_agent.AgentBudget(),
    )
    job = _wait_for_terminal(job_id)
    # The pipeline completed (segments proposed) instead of crashing on the
    # bad id, and a trace step records the dropped pixel.
    assert job.status == "needs_review", job.detail
    assert job.segments is not None and len(job.segments) >= 1
    assert any("out of range" in s["summary"] for s in job.steps)
    # Every proposed segment references only in-range faces.
    n_faces = 12
    for faces in job.face_groups.values():
        assert all(0 <= f < n_faces for f in faces)


# ------------------------------------------------------------ worker exception


def test_worker_exception_sets_error_status(api_client, monkeypatch):
    """A VLM that always throws leaves the job status 'error' with a detail;
    start_job / the worker never propagate the exception."""
    project_dir, scene = _make_box_project("agent_boom")

    def boom_vlm(*a, **k):
        raise RuntimeError("VLM exploded")

    monkeypatch.setattr(seam_agent, "_vlm_chat", boom_vlm)

    # start_job itself must not raise even though every view will fail.
    job_id = seam_agent.start_job(
        project_dir, scene, PRIM_ID, [_view("v0", np.zeros((8, 8), dtype=np.uint32))],
        user_hint=None, allow_web=False, model=None, budget=seam_agent.AgentBudget(),
    )
    job = _wait_for_terminal(job_id)
    assert job.status == "error"
    assert job.detail  # non-empty detail
    # No view analyzed -> the pipeline raised, caught by the worker guard.
    assert "no view" in job.detail.lower() or "vlm" in job.detail.lower()


# -------------------------------------------------------------- apply_segments


def _job_with_face_groups(prim_id: str, groups: dict, segments: list) -> seam_agent.AgentJob:
    job = seam_agent.AgentJob(job_id="j", prim_id=prim_id)
    job.segments = segments
    job.face_groups = groups
    job.status = "needs_review"
    return job


def test_apply_segments_strict_partition(api_client):
    """Accepted segments plus the unassigned remainder cover ALL faces exactly
    once; the rewritten scene carries a prim per accepted class + remainder."""
    project_dir, scene = _make_box_project("agent_apply")
    # Box: 12 faces. Split 0..5 -> wall, leave 6..11 for the remainder.
    groups = {"box_exterior_wall": list(range(0, 6))}
    segments = [
        {
            "segment_id": "box_exterior_wall",
            "semantic_label": "exterior_wall",
            "face_count": 6,
            "rf_material_id": "itu_concrete",
            "confidence": 0.8,
            "alternatives": [],
            "evidence_ids": [],
        }
    ]
    job = _job_with_face_groups(PRIM_ID, groups, segments)
    new_scene, info = seam_agent.apply_segments(
        project_dir, new_scene_fixup(scene), job, ["box_exterior_wall"]
    )
    # Two new prims: the accepted wall + the unassigned remainder.
    assert len(info["added_prim_ids"]) == 2
    assert info["removed_prim_id"] == PRIM_ID
    prim_ids = {p.id for p in new_scene.prims}
    assert PRIM_ID not in prim_ids
    for pid in info["added_prim_ids"]:
        assert pid in prim_ids
    # The split reloads with the same total face count (strict partition).
    tm = trimesh.load(project_dir / "visual" / "scene.glb", force="scene")
    total = sum(len(g.faces) for g in tm.geometry.values())
    assert total == 12


def test_apply_segments_dedups_faces_across_segments(api_client):
    """A face listed in two chosen segments is assigned to the first only
    (deduped), so the partition never double-counts."""
    project_dir, scene = _make_box_project("agent_dedup")
    # Faces 3,4,5 appear in BOTH segments; the second must not re-take them.
    groups = {
        "box_exterior_wall": [0, 1, 2, 3, 4, 5],
        "box_glass_window": [3, 4, 5, 6, 7, 8],
    }
    segments = [
        {"segment_id": "box_exterior_wall", "semantic_label": "exterior_wall",
         "face_count": 6, "rf_material_id": "itu_concrete", "confidence": 0.8,
         "alternatives": [], "evidence_ids": []},
        {"segment_id": "box_glass_window", "semantic_label": "glass_window",
         "face_count": 6, "rf_material_id": "itu_glass", "confidence": 0.7,
         "alternatives": [], "evidence_ids": []},
    ]
    job = _job_with_face_groups(PRIM_ID, groups, segments)
    new_scene, info = seam_agent.apply_segments(
        project_dir, new_scene_fixup(scene), job,
        ["box_exterior_wall", "box_glass_window"],
    )
    tm = trimesh.load(project_dir / "visual" / "scene.glb", force="scene")
    # Faces are partitioned, never duplicated: total across submeshes == 12.
    total = sum(len(g.faces) for g in tm.geometry.values())
    assert total == 12
    faces_by_class = {
        node.rsplit("__", 1)[-1]: len(g.faces)
        for node, g in tm.geometry.items()
        if "__" in node
    }
    # wall claimed 0..5 first; glass's 3,4,5 were deduped, leaving only 6,7,8.
    assert faces_by_class["exterior_wall"] == 6
    assert faces_by_class["glass_window"] == 3
    assert faces_by_class["unassigned"] == 3  # 9,10,11
    assert len(info["added_prim_ids"]) == 3


def test_apply_segments_unknown_ids_raises(api_client):
    project_dir, scene = _make_box_project("agent_unknown")
    job = _job_with_face_groups(
        PRIM_ID,
        {"box_exterior_wall": [0, 1, 2]},
        [{"segment_id": "box_exterior_wall", "semantic_label": "exterior_wall",
          "face_count": 3, "rf_material_id": "itu_concrete", "confidence": 0.8,
          "alternatives": [], "evidence_ids": []}],
    )
    with pytest.raises(seg.SegmentationError):
        seam_agent.apply_segments(
            project_dir, new_scene_fixup(scene), job, ["does_not_exist"]
        )


def new_scene_fixup(scene: Scene) -> Scene:
    """apply_segments mutates/returns a scene; re-load a clean copy per call so
    tests that share a project don't stack splits."""
    return scene.model_copy(deep=True)


# ----------------------------------------------------------- api 404 paths


def test_trace_unknown_job_404(api_client):
    api_client.post("/api/projects", json={"name": "Ag", "project_id": "ag_proj"})
    resp = api_client.get(
        "/api/projects/ag_proj/agent/material-assignment/nope/trace"
    )
    assert resp.status_code == 404, resp.text
    assert "unknown job" in resp.json()["detail"]


def test_apply_unknown_job_404(api_client):
    api_client.post("/api/projects", json={"name": "Ag2", "project_id": "ag_proj2"})
    resp = api_client.post(
        "/api/projects/ag_proj2/agent/material-assignment/nope/apply",
        json={"segment_ids": ["x"]},
    )
    assert resp.status_code == 404, resp.text
    assert "unknown job" in resp.json()["detail"]
