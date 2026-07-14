"""Programmatic "Sample Demo" project generator.

Builds the small urban toy scene (ground, road, two buildings with windows,
one tree, TX/RX pair, car + pedestrian actors) entirely from code — meshes
via trimesh, exported as a GLB with exact per-object mesh names — and writes
a complete project folder around it.

This is how a pip-installed run gets its first project without shipping any
binary assets in the wheel: the CLI (and POST /projects with
``template="demo"``) call :func:`create_demo_project` on demand. The
``examples/scripts/create_demo_project.py`` repo script delegates here too,
so the committed example and the generated first-run project stay identical.

Pinned conventions honored (HANDOFF):
- all coordinates are Z-up ENU meters and every world transform is baked into
  the GLB vertex data (prim transforms stay identity);
- prim ids are absolute path-like, device ids are short;
- visual/PBR material info is recorded as suggestion evidence only, never as
  RF truth.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import trimesh

from seam_studio.core.config import APP_VERSION
from seam_studio.schemas import (
    Device,
    MeshRef,
    Prim,
    RFBinding,
    Scene,
    SceneAssets,
    SimulationConfig,
    VisualBinding,
)
from seam_studio.schemas.scene import Actor, ActorTrajectory
from seam_studio.services.project_store import ProjectStore, load_default_library

PROJECT_ID = "sample_demo"
SCENE_NAME = "Sample Demo"
GLB_URI = "visual/scene.glb"

# name -> (baseColorFactor RGBA in 0..1, alphaMode BLEND)
PBR_MATERIALS: dict[str, tuple[tuple[float, float, float, float], bool]] = {
    "grass_pbr": ((0.36, 0.48, 0.28, 1.0), False),
    "asphalt_pbr": ((0.16, 0.17, 0.18, 1.0), False),
    "concrete_panel_pbr": ((0.72, 0.70, 0.66, 1.0), False),
    "blue_glass_pbr": ((0.35, 0.62, 0.80, 0.55), True),
    "red_brick_pbr": ((0.62, 0.32, 0.18, 1.0), False),
    "bark_pbr": ((0.35, 0.25, 0.16, 1.0), False),
    "leaf_pbr": ((0.22, 0.42, 0.20, 1.0), False),
}

GEOMETRY_NAMES = (
    "ground",
    "road_surface",
    "building_01_walls",
    "building_01_window_01",
    "building_01_window_02",
    "building_02_walls",
    "tree_01_trunk",
    "tree_01_canopy",
)


def _box(
    bounds_min: tuple[float, float, float], bounds_max: tuple[float, float, float]
) -> trimesh.Trimesh:
    """Axis-aligned box given world-space min/max corners (transform baked)."""
    lo = np.asarray(bounds_min, dtype=np.float64)
    hi = np.asarray(bounds_max, dtype=np.float64)
    mesh = trimesh.creation.box(extents=hi - lo)
    mesh.apply_translation((lo + hi) / 2.0)
    return mesh


def _apply_pbr(mesh: trimesh.Trimesh, material_name: str) -> None:
    rgba, blend = PBR_MATERIALS[material_name]
    material = trimesh.visual.material.PBRMaterial(
        name=material_name,
        baseColorFactor=list(rgba),
        alphaMode="BLEND" if blend else None,
        metallicFactor=0.0,
        roughnessFactor=0.9,
    )
    mesh.visual = trimesh.visual.texture.TextureVisuals(material=material)


def build_meshes() -> dict[str, trimesh.Trimesh]:
    """All geometry in world coordinates (Z-up ENU meters), transforms baked."""
    trunk = trimesh.creation.cylinder(radius=0.3, height=3.0, sections=24)
    trunk.apply_translation((0.0, -8.0, 1.5))  # cylinder is origin-centered
    canopy = trimesh.creation.icosphere(subdivisions=2, radius=2.0)
    canopy.apply_translation((0.0, -8.0, 4.0))

    meshes: dict[str, trimesh.Trimesh] = {
        "ground": _box((-40, -40, -0.2), (40, 40, 0.0)),
        "road_surface": _box((-40, -3.5, 0.0), (40, 3.5, 0.08)),
        "building_01_walls": _box((-20, 6, 0), (-8, 16, 10)),
        "building_01_window_01": _box((-17, 5.95, 3), (-15, 6.06, 5)),
        "building_01_window_02": _box((-13, 5.95, 3), (-11, 6.06, 5)),
        "building_02_walls": _box((6, 8, 0), (14, 16, 14)),
        "tree_01_trunk": trunk,
        "tree_01_canopy": canopy,
    }

    material_by_mesh = {
        "ground": "grass_pbr",
        "road_surface": "asphalt_pbr",
        "building_01_walls": "concrete_panel_pbr",
        "building_01_window_01": "blue_glass_pbr",
        "building_01_window_02": "blue_glass_pbr",
        "building_02_walls": "red_brick_pbr",
        "tree_01_trunk": "bark_pbr",
        "tree_01_canopy": "leaf_pbr",
    }
    for mesh_name, mat_name in material_by_mesh.items():
        _apply_pbr(meshes[mesh_name], mat_name)
    return meshes


def export_glb(meshes: dict[str, trimesh.Trimesh], glb_path: Path) -> None:
    scene = trimesh.Scene()
    for name, mesh in meshes.items():
        scene.add_geometry(mesh, geom_name=name, node_name=name)
    glb_path.parent.mkdir(parents=True, exist_ok=True)
    glb_path.write_bytes(scene.export(file_type="glb"))

    # Named-mesh correctness is the contract the rest of the app relies on:
    # every MeshRef.mesh_name must resolve after a trimesh round-trip.
    reloaded = trimesh.load(glb_path, file_type="glb")
    got = set(reloaded.geometry.keys())
    expected = set(GEOMETRY_NAMES)
    if got != expected:
        raise RuntimeError(
            f"GLB mesh names did not round-trip: missing={sorted(expected - got)} "
            f"unexpected={sorted(got - expected)}"
        )


def _visual(material_name: str) -> VisualBinding:
    rgba, _ = PBR_MATERIALS[material_name]
    return VisualBinding(
        material_name=material_name,
        base_color_rgba=list(rgba),
        base_color_texture=None,
    )


def _leaf(
    prim_id: str,
    parent_id: str,
    mesh_name: str,
    tags: list[str],
    material_name: str,
    rf: RFBinding | None = None,
) -> Prim:
    return Prim(
        id=prim_id,
        name=prim_id.rsplit("/", 1)[-1],
        type="mesh_primitive",
        parent_id=parent_id,
        semantic_tags=tags,
        mesh_ref=MeshRef(asset_uri=GLB_URI, mesh_name=mesh_name, face_group=None),
        visual=_visual(material_name),
        rf=rf if rf is not None else RFBinding(),
    )


def _group(prim_id: str, tags: list[str] | None = None) -> Prim:
    return Prim(
        id=prim_id,
        name=prim_id.rsplit("/", 1)[-1],
        type="group",
        parent_id=None,
        semantic_tags=tags or [],
    )


def build_scene(scene_id: str = PROJECT_ID, name: str = SCENE_NAME) -> Scene:
    prims: list[Prim] = [
        _group("/terrain", ["terrain"]),
        _group("/roads/r01", ["road"]),
        _group("/buildings/b01", ["building"]),
        _group("/buildings/b02", ["building"]),
        _group("/vegetation/tree_01", ["vegetation", "tree"]),
        # Pre-assigned, user-confirmed binding (demo of a trusted assignment).
        _leaf(
            "/terrain/ground",
            "/terrain",
            "ground",
            ["terrain", "ground"],
            "grass_pbr",
            rf=RFBinding(
                # 28 GHz-safe constant ground (ITU ground models are invalid at
                # mmWave; the demo runs at 28 GHz).
                material_id="ground_28ghz",
                assignment_status="user_confirmed",
                assignment_sources=["user"],
                confidence=1.0,
            ),
        ),
        # Rule-suggested but not confirmed (demo of the suggestion lifecycle).
        _leaf(
            "/roads/r01/surface",
            "/roads/r01",
            "road_surface",
            ["road"],
            "asphalt_pbr",
            rf=RFBinding(
                material_id="asphalt_custom",
                assignment_status="rule_suggested",
                assignment_sources=["rule_based"],
                confidence=0.85,
            ),
        ),
        _leaf(
            "/buildings/b01/walls",
            "/buildings/b01",
            "building_01_walls",
            ["building", "wall"],
            "concrete_panel_pbr",
        ),
        _leaf(
            "/buildings/b01/window_01",
            "/buildings/b01",
            "building_01_window_01",
            ["building", "window"],
            "blue_glass_pbr",
        ),
        _leaf(
            "/buildings/b01/window_02",
            "/buildings/b01",
            "building_01_window_02",
            ["building", "window"],
            "blue_glass_pbr",
        ),
        _leaf(
            "/buildings/b02/walls",
            "/buildings/b02",
            "building_02_walls",
            ["building", "wall"],
            "red_brick_pbr",
        ),
        _leaf(
            "/vegetation/tree_01/trunk",
            "/vegetation/tree_01",
            "tree_01_trunk",
            ["vegetation", "tree"],
            "bark_pbr",
        ),
        _leaf(
            "/vegetation/tree_01/canopy",
            "/vegetation/tree_01",
            "tree_01_canopy",
            ["vegetation", "tree"],
            "leaf_pbr",
        ),
    ]

    # Device colors follow the AODT-like viewer legend: red transmitters,
    # blue UE/receivers.
    devices = [
        Device(
            id="tx_001",
            name="Rooftop TX",
            kind="tx",
            position=[-9.0, 7.0, 10.5],
            power_dbm=30.0,
            color="#ff0000",
        ),
        Device(
            id="rx_001",
            name="Street RX",
            kind="rx",
            position=[10.0, 0.0, 1.5],
            color="#2e9bff",
        ),
    ]

    # Movable actors (compiled as their own RF shapes; moved per frame by the
    # scenario/live-sync backends). The car drives down the road (y=0); the
    # pedestrian takes a short walk near building b01's entrance (y~6).
    actors = [
        Actor(
            id="car_001",
            name="Sedan",
            kind="car",  # metal box, 4.5 x 1.8 x 1.5 m
            position=[-30.0, 0.0, 0.0],
            orientation_deg=[0.0, 0.0, 0.0],
            trajectory=ActorTrajectory(
                waypoints=[
                    [-30.0, 0.0, 0.0],
                    [-15.0, 0.0, 0.0],
                    [0.0, 0.0, 0.0],
                    [15.0, 0.0, 0.0],
                    [30.0, 0.0, 0.0],
                ],
                dt_s=0.5,
                loop=True,
            ),
        ),
        Actor(
            id="human_001",
            name="Pedestrian",
            kind="human",  # human_body box, 0.5 x 0.35 x 1.7 m
            position=[-14.0, 4.0, 0.0],
            # orientation_deg is [yaw, pitch, roll]; yaw 90 = facing +y.
            orientation_deg=[90.0, 0.0, 0.0],
            trajectory=ActorTrajectory(
                waypoints=[
                    [-14.0, 4.0, 0.0],
                    [-12.0, 4.5, 0.0],
                    [-10.0, 5.0, 0.0],
                ],
                dt_s=0.5,
                loop=False,
            ),
        ),
    ]

    return Scene(
        scene_id=scene_id,
        name=name,
        assets=SceneAssets(visual_scene_uri=GLB_URI),
        prims=prims,
        devices=devices,
        actors=actors,
        simulation_configs=[
            SimulationConfig(
                id="default",
                name="Default 28 GHz",
                backend="auto",
                frequency_hz=28e9,
                max_depth=3,
            )
        ],
    )


def create_demo_project(
    store: ProjectStore,
    project_id: str = PROJECT_ID,
    name: str = SCENE_NAME,
) -> Path:
    """Materialize the demo project through the store's own conventions.

    ``store.create_project`` scaffolds the canonical folder (suffix, default
    library, provenance); this fills it with the generated GLB, scene, and
    mapping. Raises ValueError if ``project_id`` already exists.
    """
    info = store.create_project(name=name, project_id=project_id)
    project_dir = Path(info.path)
    for sub in ("visual", "rf/meshes", "mapping", "ai", "results"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    export_glb(build_meshes(), project_dir / GLB_URI)

    scene = build_scene(scene_id=project_id, name=name)
    store.save_scene(project_id, scene)
    ProjectStore.save_materials_to_dir(project_dir, load_default_library())

    # Same schema the RF compiler writes (it re-emits this file on compile,
    # filling group_mesh_file for compiled prims): one stable shape per file.
    object_map = {
        prim.id: {
            "mesh_name": prim.mesh_ref.mesh_name,
            "rf_material_id": prim.rf.material_id,
            "group_mesh_file": None,
        }
        for prim in scene.prims
        if prim.mesh_ref is not None
    }
    (project_dir / "mapping" / "object_map.json").write_text(
        json.dumps(object_map, indent=2), encoding="utf-8"
    )

    (project_dir / "provenance.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": f"seam-studio/{APP_VERSION} (demo template)",
                "events": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return project_dir
