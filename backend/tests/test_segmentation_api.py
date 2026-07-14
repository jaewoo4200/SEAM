"""API-level material-segmentation tests (TestClient via the conftest fixtures).

The conftest ``api_client`` fixture points SIONNATWIN_PROJECT_ROOTS at a tmp
root and mounts the full app (segmentation router + the /assets route), so
``deps.get_store()`` inside a test drives the same store the endpoints use.

Each test fabricates a project with ONE textured, UV-carrying prim: a box mesh
with per-vertex UVs and a PBR baseColorTexture, exported to visual/scene.glb,
with the atlas also written to visual/textures/t.png and a Scene prim whose
mesh_ref/visual point at both. The atlas is split top/bottom into two heuristic
classes so the color heuristic yields a real multi-material split.
"""

import io
import json
from pathlib import Path

import numpy as np
import pytest
import trimesh
from PIL import Image

from seam_studio.api import deps
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene, VisualBinding

MESH_NAME = "building_box"
PRIM_ID = "/buildings/b01/box"
TEX_REL = "visual/textures/t.png"


def _two_class_atlas(size: int = 64) -> Image.Image:
    """A 64x64 atlas: top half bright near-neutral grey (heuristic -> metal),
    bottom half mid grey (heuristic -> concrete default). Sampling faces across
    it yields >=2 material classes for a real split."""
    arr = np.empty((size, size, 3), dtype=np.uint8)
    arr[: size // 2, :, :] = 200  # bright grey -> metal (id 3)
    arr[size // 2 :, :, :] = 128  # mid grey    -> concrete default (id 1)
    return Image.fromarray(arr, mode="RGB")


def _make_textured_project(
    project_id: str, *, with_texture: bool = True, atlas: Image.Image | None = None
) -> tuple[Path, Scene]:
    """Create a project with one textured UV box prim; return (dir, scene).

    The box carries deterministic per-vertex UVs (fixed seed) spanning the
    atlas, so with the two-class atlas its 12 faces split across both classes.
    When ``with_texture`` is False the prim's visual.base_color_texture is left
    None (and no PNG is written) to exercise the no-texture 400 path.
    """
    store = deps.get_store()
    store.create_project(project_id, project_id=project_id)
    project_dir = store.resolve(project_id)

    if atlas is None:
        atlas = _two_class_atlas()

    box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    rng = np.random.default_rng(1234)
    uv = rng.random((len(box.vertices), 2))
    material = trimesh.visual.material.PBRMaterial(baseColorTexture=atlas)
    box.visual = trimesh.visual.texture.TextureVisuals(uv=uv, material=material)

    tm = trimesh.Scene()
    tm.add_geometry(box, geom_name=MESH_NAME, node_name=MESH_NAME)
    (project_dir / "visual").mkdir(parents=True, exist_ok=True)
    (project_dir / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))

    if with_texture:
        (project_dir / "visual" / "textures").mkdir(parents=True, exist_ok=True)
        atlas.save(project_dir / TEX_REL)

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
                visual=VisualBinding(
                    material_name="atlas",
                    base_color_texture=(TEX_REL if with_texture else None),
                ),
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


def _geom_names(glb_path: Path) -> set[str]:
    tm = trimesh.load(glb_path, force="scene")
    return set(tm.geometry.keys())


# --------------------------------------------------------------------- preview


def test_preview_color_heuristic_200(api_client):
    _dir, _scene = _make_textured_project("segp")
    resp = api_client.post(
        "/api/projects/segp/segmentation/preview",
        json={"prim_id": PRIM_ID, "mask_source": "color_heuristic"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["batch_id"]
    assert body["manifest"], "expected at least one material region"
    # face_materials length == the box's face count (12 for a trimesh box).
    assert body["total_faces"] == 12
    assert len(body["face_materials"]) == 12
    # The two-class atlas produced a genuine multi-material preview.
    assert len({r["material_id"] for r in body["manifest"]}) >= 2

    # The overlay asset is servable via the /assets route.
    overlay = body["overlay_asset_path"]
    asset = api_client.get(f"/api/projects/segp/assets/{overlay}")
    assert asset.status_code == 200, asset.text


def test_preview_without_texture_400(api_client):
    _make_textured_project("segnotex", with_texture=False)
    resp = api_client.post(
        "/api/projects/segnotex/segmentation/preview",
        json={"prim_id": PRIM_ID, "mask_source": "color_heuristic"},
    )
    assert resp.status_code == 400, resp.text
    assert "base_color_texture" in resp.json()["detail"]


# ----------------------------------------------------------------------- apply


def test_apply_splits_prim_and_rewrites_glb(api_client):
    project_dir, _scene = _make_textured_project("sega")
    preview = api_client.post(
        "/api/projects/sega/segmentation/preview",
        json={"prim_id": PRIM_ID, "mask_source": "color_heuristic"},
    ).json()
    mask_ref = preview["mask_ref"]

    resp = api_client.post(
        "/api/projects/sega/segmentation/apply",
        json={"prim_id": PRIM_ID, "mask_ref": mask_ref},
    )
    assert resp.status_code == 200, resp.text
    info = resp.json()
    assert len(info["added_prim_ids"]) >= 2
    assert info["removed_prim_id"] == PRIM_ID

    # Scene now carries the new per-material prims and not the old one.
    scene = api_client.get("/api/projects/sega/scene").json()
    prim_ids = {p["id"] for p in scene["prims"]}
    assert PRIM_ID not in prim_ids
    for new_id in info["added_prim_ids"]:
        assert new_id in prim_ids

    # Backup GLB exists under visual/.
    backup = project_dir / info["backup_glb"]
    assert backup.is_file(), info["backup_glb"]

    # The rewritten GLB reloads with the new per-material mesh names and no
    # longer the original.
    names = _geom_names(project_dir / "visual" / "scene.glb")
    assert MESH_NAME not in names
    assert any(n.startswith(f"{MESH_NAME}__") for n in names)


def test_apply_single_class_mask_400(api_client):
    # Upload a user mask that assigns EVERY pixel to one class -> the assign
    # yields a single material -> apply returns 400 "nothing to split".
    _dir, _scene = _make_textured_project("sega1")
    atlas = _two_class_atlas()
    # A same-size L-mode mask of all-glass (id 2).
    mono = np.full((atlas.height, atlas.width), 2, dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(mono, mode="L").save(buf, format="PNG")
    up = api_client.post(
        "/api/projects/sega1/segmentation/upload-mask",
        files=[("file", ("mono.png", buf.getvalue(), "image/png"))],
    )
    assert up.status_code == 200, up.text
    mask_asset_path = up.json()["mask_asset_path"]

    # Preview with the mono user mask persists a single-class mask...
    preview = api_client.post(
        "/api/projects/sega1/segmentation/preview",
        json={
            "prim_id": PRIM_ID,
            "mask_source": "user_png",
            "mask_asset_path": mask_asset_path,
        },
    )
    assert preview.status_code == 200, preview.text
    # ...and applying it must be rejected (only one material -> nothing to split).
    resp = api_client.post(
        "/api/projects/sega1/segmentation/apply",
        json={"prim_id": PRIM_ID, "mask_ref": preview.json()["mask_ref"]},
    )
    assert resp.status_code == 400, resp.text
    assert "nothing to split" in resp.json()["detail"]


# ------------------------------------------------------------------------ undo


def test_undo_restores_original_prim(api_client):
    _dir, _scene = _make_textured_project("segu")
    preview = api_client.post(
        "/api/projects/segu/segmentation/preview",
        json={"prim_id": PRIM_ID, "mask_source": "color_heuristic"},
    ).json()
    apply = api_client.post(
        "/api/projects/segu/segmentation/apply",
        json={"prim_id": PRIM_ID, "mask_ref": preview["mask_ref"]},
    ).json()
    batch_id = apply["batch_id"]
    added = apply["added_prim_ids"]

    resp = api_client.post(
        "/api/projects/segu/segmentation/undo",
        json={"batch_id": batch_id},
    )
    assert resp.status_code == 200, resp.text
    info = resp.json()
    assert info["restored_prim_id"] == PRIM_ID
    assert set(info["removed_prim_ids"]) == set(added)

    # Original prim back, the split prims gone.
    scene = api_client.get("/api/projects/segu/scene").json()
    prim_ids = {p["id"] for p in scene["prims"]}
    assert PRIM_ID in prim_ids
    for new_id in added:
        assert new_id not in prim_ids


# ---------------------------------------------------------------- upload-mask


def test_upload_mask_returns_uploads_path(api_client):
    _make_textured_project("segum")
    mask = np.zeros((16, 16), dtype=np.uint8)
    buf = io.BytesIO()
    Image.fromarray(mask, mode="L").save(buf, format="PNG")
    resp = api_client.post(
        "/api/projects/segum/segmentation/upload-mask",
        files=[("file", ("m.png", buf.getvalue(), "image/png"))],
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["mask_asset_path"].startswith("ai/segmentation/uploads/")
    assert body["width"] == 16 and body["height"] == 16


def test_preview_user_png_wrong_size_400(api_client):
    # A mask whose size differs from the atlas (64x64) is rejected at preview.
    _make_textured_project("segws")
    wrong = np.zeros((16, 16), dtype=np.uint8)  # atlas is 64x64
    buf = io.BytesIO()
    Image.fromarray(wrong, mode="L").save(buf, format="PNG")
    up = api_client.post(
        "/api/projects/segws/segmentation/upload-mask",
        files=[("file", ("wrong.png", buf.getvalue(), "image/png"))],
    ).json()
    resp = api_client.post(
        "/api/projects/segws/segmentation/preview",
        json={
            "prim_id": PRIM_ID,
            "mask_source": "user_png",
            "mask_asset_path": up["mask_asset_path"],
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "16x16" in detail and "64x64" in detail
