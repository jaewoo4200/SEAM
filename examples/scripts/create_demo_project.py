"""Generate the kaist_demo example project.

Builds a small campus-corner scene (ground, road, two buildings with windows,
one tree), exports it as a GLB with exact per-object mesh names, and writes a
complete SionnaTwin project folder around it.

Run from the repo root:

    backend\\.venv\\Scripts\\python.exe examples/scripts/create_demo_project.py [--out DIR]

Pinned conventions honored here:
- all coordinates are Z-up ENU meters and every world transform is baked into
  the GLB vertex data (prim transforms stay identity);
- prim ids are absolute path-like, device ids are short;
- visual/PBR material info is recorded as suggestion evidence only, never as
  RF truth.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import numpy as np  # noqa: E402
import trimesh  # noqa: E402

from app.schemas import (  # noqa: E402
    Device,
    MeshRef,
    Prim,
    RFBinding,
    Scene,
    SceneAssets,
    SimulationConfig,
    VisualBinding,
)
from app.services.project_store import (  # noqa: E402
    ProjectStore,
    load_default_library,
)

PROJECT_ID = "kaist_demo"
SCENE_NAME = "KAIST Demo"
GLB_URI = "visual/scene.glb"
DEFAULT_OUT = REPO_ROOT / "examples" / "demo_project"

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


def _box(bounds_min: tuple[float, float, float], bounds_max: tuple[float, float, float]) -> trimesh.Trimesh:
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


def build_scene() -> Scene:
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
                material_id="ground",
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

    devices = [
        Device(
            id="tx_001",
            name="Rooftop TX",
            kind="tx",
            position=[-9.0, 7.0, 10.5],
            power_dbm=30.0,
            color="#ff4136",
        ),
        Device(
            id="rx_001",
            name="Street RX",
            kind="rx",
            position=[10.0, 0.0, 1.5],
            color="#2ecc40",
        ),
    ]

    return Scene(
        scene_id=PROJECT_ID,
        name=SCENE_NAME,
        assets=SceneAssets(visual_scene_uri=GLB_URI),
        prims=prims,
        devices=devices,
        simulation_configs=[
            SimulationConfig(
                id="default",
                name="Default 3.5 GHz",
                backend="auto",
                frequency_hz=3.5e9,
                max_depth=3,
            )
        ],
    )


def write_project(out_root: Path) -> Path:
    project_dir = out_root / f"{PROJECT_ID}.sionnatwin"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    for sub in ("visual", "rf/meshes", "mapping", "ai", "results"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    meshes = build_meshes()
    export_glb(meshes, project_dir / "visual" / "scene.glb")

    scene = build_scene()
    (project_dir / "scene.sionnatwin.json").write_text(
        scene.model_dump_json(indent=2), encoding="utf-8"
    )

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
                "created_by": "create_demo_project.py",
                "events": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return project_dir


def verify(out_root: Path, project_dir: Path) -> None:
    store = ProjectStore(roots=[out_root])
    scene = store.load_scene(PROJECT_ID)
    n_prims, n_devices = len(scene.prims), len(scene.devices)
    if n_prims != 13 or n_devices != 2:
        raise RuntimeError(
            f"expected 13 prims and 2 devices, got {n_prims} prims / {n_devices} devices"
        )
    unassigned = [
        p.id for p in scene.prims
        if p.type == "mesh_primitive" and p.rf.material_id is None
    ]
    print(f"project: {project_dir}")
    print(f"prims: {n_prims} (8 mesh + 5 group), devices: {n_devices}")
    print(f"rf unassigned mesh prims: {len(unassigned)}")
    print("GLB mesh names verified:", ", ".join(GEOMETRY_NAMES))


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the kaist_demo example project.")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output root directory (default: {DEFAULT_OUT})",
    )
    args = parser.parse_args()
    out_root = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)
    project_dir = write_project(out_root)
    verify(out_root, project_dir)


if __name__ == "__main__":
    main()
