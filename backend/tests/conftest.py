"""Shared test fixtures: tmp_path-backed ProjectStore and a demo Scene factory.

Kept generic on purpose - multiple test modules (schema, assignment, compiler,
backends, AI) build on these. Nothing here touches the real example projects.
"""

from pathlib import Path
from typing import Callable

import pytest

from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.projects import ProjectInfo
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene, VisualBinding
from seam_studio.services.project_store import ProjectStore, load_default_library

DEMO_PROJECT_ID = "demo_scene"


def make_demo_scene(scene_id: str = DEMO_PROJECT_ID, *, with_devices: bool = True) -> Scene:
    """Small in-memory demo scene.

    One building group with a confirmed concrete wall and an unassigned
    glass-looking window, one road surface, and a tx/rx pair.
    """
    prims = [
        Prim(id="/buildings/b01", name="b01", type="group"),
        Prim(
            id="/buildings/b01/wall_01",
            name="wall_01",
            parent_id="/buildings/b01",
            semantic_tags=["building", "wall"],
            mesh_ref=MeshRef(mesh_name="building_01", face_group="wall_01"),
            visual=VisualBinding(
                material_id="concrete_wall_pbr",
                material_name="concrete_wall",
                base_color_texture="visual/textures/concrete.jpg",
            ),
            rf=RFBinding(
                material_id="itu_concrete",
                assignment_status="user_confirmed",
                assignment_sources=["user"],
                confidence=0.95,
            ),
        ),
        Prim(
            id="/buildings/b01/window_12",
            name="window_12",
            parent_id="/buildings/b01",
            semantic_tags=["building", "window"],
            mesh_ref=MeshRef(mesh_name="building_01", face_group="window_12"),
            visual=VisualBinding(
                material_id="blue_glass_pbr", material_name="blue_glass_pbr"
            ),
        ),
        Prim(
            id="/roads/r01/surface",
            name="surface",
            semantic_tags=["road"],
            mesh_ref=MeshRef(mesh_name="road_01"),
            visual=VisualBinding(material_name="asphalt_pbr"),
        ),
    ]
    devices = (
        [
            Device(id="tx_001", kind="tx", position=[0.0, 0.0, 10.0]),
            Device(id="rx_001", kind="rx", position=[25.0, 5.0, 1.5]),
        ]
        if with_devices
        else []
    )
    return Scene(scene_id=scene_id, name="Demo Scene", prims=prims, devices=devices)


@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "projects"
    root.mkdir()
    return root


@pytest.fixture()
def store(project_root: Path) -> ProjectStore:
    return ProjectStore(roots=[project_root])


@pytest.fixture()
def library() -> RFMaterialLibrary:
    return load_default_library()


@pytest.fixture()
def demo_scene() -> Scene:
    return make_demo_scene()


@pytest.fixture()
def scene_factory() -> Callable[..., Scene]:
    return make_demo_scene


@pytest.fixture()
def demo_project(store: ProjectStore, demo_scene: Scene) -> ProjectInfo:
    """A created project whose scene file is the demo scene."""
    info = store.create_project(name="Demo Scene", project_id=DEMO_PROJECT_ID)
    store.save_scene(DEMO_PROJECT_ID, demo_scene)
    return store.info(Path(info.path))


@pytest.fixture()
def api_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient over a fresh app whose project root lives under tmp_path.

    deps.get_store and config.get_settings are lru_cached, so the caches are
    cleared after pointing SIONNATWIN_PROJECT_ROOTS at the tmp root (and again
    on teardown) - tests never leak state into the real project roots.
    """
    from fastapi.testclient import TestClient

    from seam_studio.api import deps
    from seam_studio.core import config

    root = tmp_path / "api_projects"
    root.mkdir()
    monkeypatch.setenv("SEAM_PROJECT_ROOTS", str(root))
    config.get_settings.cache_clear()
    deps.get_store.cache_clear()

    from seam_studio.main import create_app

    with TestClient(create_app()) as client:
        yield client

    config.get_settings.cache_clear()
    deps.get_store.cache_clear()
