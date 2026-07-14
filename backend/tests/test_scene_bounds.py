"""Scene-bounds AABB, the /scene/bounds route, and terrain z-snapping.

The bounds/route tests are deterministic and need no ray engine. The terrain
test raycasts onto a mesh; trimesh's pure-python intersector needs rtree's
native spatial index, so it is skipped when that isn't functional rather than
reddening the suite on a bare install.
"""

from pathlib import Path

import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seam_studio.api import deps
from seam_studio.api import scene as scene_api
from seam_studio.core.config import get_settings
from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Scene
from seam_studio.services.scene_bounds import compute_scene_bounds
from seam_studio.services.terrain import snap_to_terrain


def _box_glb(path: Path, extents=(10.0, 10.0, 4.0)) -> None:
    """Write a single-box GLB (centered at origin) to ``path``.

    A box with extents (l, w, h) spans [-l/2, l/2] x [-w/2, w/2] x [-h/2, h/2]
    in world coordinates, so its top face sits at z = h/2.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tm = trimesh.Scene()
    box = trimesh.creation.box(extents=extents)
    tm.add_geometry(box, geom_name="box", node_name="box")
    path.write_bytes(tm.export(file_type="glb"))


def _terrain_ray_available() -> bool:
    """True when trimesh can actually raycast (rtree's native index loads)."""
    try:
        import numpy as np

        box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
        box.ray.intersects_location(
            ray_origins=np.array([[0.0, 0.0, 5.0]]),
            ray_directions=np.array([[0.0, 0.0, -1.0]]),
        )
        return True
    except Exception:  # noqa: BLE001 - any import/native failure => skip
        return False


# ------------------------------------------------------ compute_scene_bounds


def test_bounds_merge_mesh_and_device(tmp_path: Path):
    # 10x10x4 box (x,y in [-5,5], z in [-2,2]) plus a device well outside it;
    # the merged AABB must cover both.
    _box_glb(tmp_path / "visual" / "scene.glb")
    scene = Scene(
        scene_id="b", name="b",
        devices=[Device(id="tx_001", kind="tx", position=[100.0, -50.0, 20.0])],
    )
    bounds = compute_scene_bounds(tmp_path, scene)
    assert bounds is not None
    assert bounds.min == pytest.approx([-5.0, -50.0, -2.0])
    assert bounds.max == pytest.approx([100.0, 5.0, 20.0])


def test_bounds_none_without_mesh_or_devices(tmp_path: Path):
    # No visual asset and no devices/actors -> nothing to bound.
    scene = Scene(scene_id="e", name="e")
    assert compute_scene_bounds(tmp_path, scene) is None


def test_bounds_cache_hit_returns_equal(tmp_path: Path):
    # Second call (same path+mtime) hits the module cache and is deep-equal.
    _box_glb(tmp_path / "visual" / "scene.glb")
    scene = Scene(scene_id="c", name="c")
    first = compute_scene_bounds(tmp_path, scene)
    second = compute_scene_bounds(tmp_path, scene)
    assert first is not None and second is not None
    assert first.model_dump() == second.model_dump()


def test_bounds_devices_only_when_no_mesh(tmp_path: Path):
    # Mock-only project (no GLB): bounds fall back to device positions.
    scene = Scene(
        scene_id="d", name="d",
        devices=[
            Device(id="tx_001", kind="tx", position=[0.0, 0.0, 10.0]),
            Device(id="rx_001", kind="rx", position=[20.0, -4.0, 1.5]),
        ],
    )
    bounds = compute_scene_bounds(tmp_path, scene)
    assert bounds is not None
    assert bounds.min == pytest.approx([0.0, -4.0, 1.5])
    assert bounds.max == pytest.approx([20.0, 0.0, 10.0])


# -------------------------------------------------------------- route (API)


@pytest.fixture()
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient over the scene router with a fresh tmp project root."""
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    app = FastAPI()
    app.include_router(scene_api.router, prefix="/api")
    client = TestClient(app)
    try:
        yield client
    finally:
        get_settings.cache_clear()
        deps.get_store.cache_clear()


def test_route_bounds_200_with_min_max(api_client):
    store = deps.get_store()
    store.create_project("Bounds", project_id="bnd")
    _box_glb(store.resolve("bnd") / "visual" / "scene.glb")
    scene = Scene(
        scene_id="bnd", name="Bounds",
        devices=[Device(id="tx_001", kind="tx", position=[100.0, 0.0, 20.0])],
    )
    store.save_scene("bnd", scene)

    resp = api_client.get("/api/projects/bnd/scene/bounds")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["min"] == pytest.approx([-5.0, -5.0, -2.0])
    assert body["max"] == pytest.approx([100.0, 5.0, 20.0])


def test_route_bounds_404_without_mesh_or_devices(api_client):
    store = deps.get_store()
    store.create_project("Empty", project_id="empt")
    # create_project writes an empty scene (no mesh file, no devices).
    resp = api_client.get("/api/projects/empt/scene/bounds")
    assert resp.status_code == 404


def test_route_bounds_404_unknown_project(api_client):
    resp = api_client.get("/api/projects/nope/scene/bounds")
    assert resp.status_code == 404


# -------------------------------------------------------------- terrain snap


@pytest.mark.skipif(
    not _terrain_ray_available(),
    reason="trimesh raycast unavailable (rtree native index not loadable)",
)
def test_snap_to_terrain_hit_and_miss(tmp_path: Path):
    # Box top face at z = 4/2 = 2. Snapping a point far above it with a 1.5 m
    # height offset lands at 2 + 1.5 = 3.5.
    _box_glb(tmp_path / "visual" / "scene.glb", extents=(20.0, 20.0, 4.0))
    scene = Scene(scene_id="t", name="t")
    warnings: list[str] = []

    inside = [0.0, 0.0, 99.0]
    outside = [100.0, 100.0, 7.0]  # beyond the 20x20 footprint
    out = snap_to_terrain(tmp_path, scene, [inside, outside], 1.5, warnings)

    assert out[0] == pytest.approx([0.0, 0.0, 3.5])
    # A point with no surface underneath keeps its z and triggers one warning.
    assert out[1] == pytest.approx([100.0, 100.0, 7.0])
    assert len(warnings) == 1
    assert "no surface underneath" in warnings[0]


def test_snap_to_terrain_no_mesh_keeps_z_and_warns(tmp_path: Path):
    # No visual mesh -> points are returned unchanged with a single warning
    # (this path returns before any raycast, so it needs no ray engine).
    scene = Scene(scene_id="nm", name="nm")
    warnings: list[str] = []
    out = snap_to_terrain(tmp_path, scene, [[1.0, 2.0, 9.0]], 1.5, warnings)
    assert out == [[1.0, 2.0, 9.0]]
    assert len(warnings) == 1
    assert "no visual mesh" in warnings[0]
