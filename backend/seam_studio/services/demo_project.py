"""Programmatic "Sample Demo" project generator.

Builds the small urban toy scene (ground, road, two buildings with windows,
one tree, TX/RX pair, car + pedestrian actors, since v0.1.14 a drone sensing
target plus a sensing RX co-located with the TX, and since v0.1.15 two more
TRPs (TX + co-located sensing RX) and the 3.5 GHz ``sensing_fr1`` config)
entirely from code — meshes via trimesh, exported as a GLB with exact
per-object mesh names — and writes a complete project folder around it.

This is how a pip-installed run gets its first project without shipping any
binary assets in the wheel: the CLI (and POST /projects with
``template="demo"``) call :func:`create_demo_project` on demand. The
``examples/scripts/create_demo_project.py`` repo script delegates here too.
The committed example predates the v0.1.14/v0.1.15 additions; seeding a source
checkout's ``projects/`` (:func:`seed_checkout_projects`) adds them to the
copy, never to ``examples/``.

Pinned conventions honored (HANDOFF):
- all coordinates are Z-up ENU meters and every world transform is baked into
  the GLB vertex data (prim transforms stay identity);
- prim ids are absolute path-like, device ids are short;
- visual/PBR material info is recorded as suggestion evidence only, never as
  RF truth.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
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

logger = logging.getLogger(__name__)

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


def demo_sensing_rx() -> Device:
    """Monostatic radar receiver at the rooftop TX (same pose, iso 1x1), so
    sensing, scenario sensing, ISAC and coverage work out of the box."""
    return Device(
        id="tx_001_rx",
        name="TX 1 sensing RX",
        kind="rx",
        position=[-9.0, 7.0, 10.5],
        color="#2e9bff",
    )


# (tx id, name, position): street masts east of the scene that form, with the
# rooftop TX, a triangle covering the drone's second leg (the first leg starts
# outside it, so the fusion GDOP is worst at t=0; see the sensing guide's §6).
# Every monostatic and bistatic echo of the flight has line of
# sight (checked on Sionna), and the three sites give the multistatic fusion
# (and so the EKF, which starts from it) its >= 3 distinct links.
DEMO_TRP_SITES: tuple[tuple[str, str, tuple[float, float, float]], ...] = (
    ("tx_002", "TX 2", (30.0, -30.0, 10.0)),
    ("tx_003", "TX 3", (35.0, 35.0, 10.0)),
)


def demo_trp_devices() -> list[Device]:
    """TX 2 and TX 3, each with a co-located sensing RX (same style as tx_001
    and tx_001_rx: iso 1x1, 30 dBm)."""
    devices: list[Device] = []
    for tx_id, name, position in DEMO_TRP_SITES:
        devices.append(
            Device(id=tx_id, name=name, kind="tx", position=list(position),
                   power_dbm=30.0, color="#ff0000")
        )
        devices.append(
            Device(id=f"{tx_id}_rx", name=f"{name} sensing RX", kind="rx",
                   position=list(position), color="#2e9bff")
        )
    return devices


def demo_sensing_config() -> SimulationConfig:
    """FR1 config for the sensing guide. At 28 GHz / 100 MHz the drone echo
    of one 30 dBm iso element peaks at 7.7 dB with 4096 pulses (+36 dB),
    under the 13 dB threshold; 3.5 GHz (+18 dB from lambda^2) and 20 MHz
    (7 dB less noise) put every link of the flight at 15-33 dB."""
    return SimulationConfig(
        id="sensing_fr1",
        name="Sensing demo (3.5 GHz)",
        backend="auto",
        frequency_hz=3.5e9,
        bandwidth_hz=20e6,
        noise_figure_db=7.0,
        max_depth=2,
        diffraction=False,
    )


def demo_uav_actor() -> Actor:
    """A TR 38.901 small-UAV sensing target flying 90 m (9 s at 10 m/s) at
    40 m, well above both buildings."""
    return Actor(
        id="uav_001",
        name="Drone",
        kind="uav",
        position=[-25.0, -20.0, 40.0],
        trajectory=ActorTrajectory(
            waypoints=[[-25.0, -20.0, 40.0], [25.0, -20.0, 40.0], [25.0, 20.0, 40.0]],
            speed_m_s=10.0,
            mode="once",
        ),
        sensing={"model": "tr38901", "object_type": "uav-small-size"},
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
        demo_sensing_rx(),
        *demo_trp_devices(),
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
        demo_uav_actor(),
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
            ),
            demo_sensing_config(),
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


# ------------------------------------------------- source-checkout seeding

# Explicit allowlist: the gitignored *_xeng.seam paper copies are never seeded.
EXAMPLE_PROJECT_IDS = ("ftc_outdoor", "lab_room", "sample_demo")
# The v0.1.14+ demo additions change the scene, so the copy recompiles on its
# first solve instead of reusing the committed projection.
_SAMPLE_DEMO_SKIP_RF = frozenset({"generated_scene.xml", "compile_manifest.json", "meshes"})


def _existing_project(root: Path, project_id: str) -> bool:
    from seam_studio.services.project_store import PROJECT_SUFFIXES

    return any((root / f"{project_id}{suffix}").exists() for suffix in PROJECT_SUFFIXES)


def _staging_dir(root: Path, project_id: str) -> Path:
    # Not "<id>.seam", so _existing_project never counts a half-built copy.
    return root / f".{project_id}.seam.seeding"


def _add_demo_additions(project_dir: Path) -> None:
    from seam_studio.services.project_store import project_id_from_dir

    store = ProjectStore(roots=[project_dir])  # a root may itself be a project folder
    pid = project_id_from_dir(project_dir)
    scene = store.load_scene(pid)
    changed = False
    if scene.device_by_id("tx_001_rx") is None:
        scene.devices.append(demo_sensing_rx())
        changed = True
    for device in demo_trp_devices():
        if scene.device_by_id(device.id) is None:
            scene.devices.append(device)
            changed = True
    if not any(a.id == "uav_001" for a in scene.actors):
        scene.actors.append(demo_uav_actor())
        changed = True
    sensing_config = demo_sensing_config()
    if not any(c.id == sensing_config.id for c in scene.simulation_configs):
        scene.simulation_configs.append(sensing_config)
        changed = True
    if changed:
        store.save_scene(pid, scene, clear_live_overlay=False, record_history=False)


def _move_into_place(staging: Path, dst: Path, attempts: int = 3) -> None:
    # A virus scanner or sync client may still hold a just-copied file open.
    for attempt in range(attempts):
        try:
            os.replace(staging, dst)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.5)


def seed_checkout_projects(root: Path, examples_dir: Path) -> list[str]:
    """Copy each committed example project missing from ``root`` into it;
    returns the seeded ids. An existing project (``.seam`` or legacy
    ``.sionnatwin``) is never overwritten or modified. The whole folder is
    copied, local results and history included, so an earlier session that
    ran on the example carries over. The Sample Demo copy also gains the
    v0.1.14 drone target and co-located sensing RX, and the v0.1.15 TRPs
    TX 2 / TX 3 and the ``sensing_fr1`` config. Each copy is built in a
    hidden staging folder and renamed into place only when complete, so an
    interrupted copy (lock, full disk, Ctrl-C) leaves nothing behind and the
    next start retries it. Never raises (except KeyboardInterrupt): a failing
    project is logged and skipped."""
    seeded: list[str] = []
    for project_id in EXAMPLE_PROJECT_IDS:
        src = examples_dir / f"{project_id}.seam"
        dst = root / f"{project_id}.seam"
        staging = _staging_dir(root, project_id)
        try:
            if staging.exists():  # left by a hard kill during an earlier start
                shutil.rmtree(staging)
            if _existing_project(root, project_id) or not src.is_dir():
                continue
            ignore = None
            if project_id == PROJECT_ID:
                rf_dir = os.path.normcase(str((src / "rf").resolve()))

                def ignore(directory, names, rf_dir=rf_dir):
                    if os.path.normcase(str(Path(directory).resolve())) != rf_dir:
                        return set()
                    return {n for n in names if n in _SAMPLE_DEMO_SKIP_RF}

            shutil.copytree(src, staging, ignore=ignore)
            if project_id == PROJECT_ID:
                _add_demo_additions(staging)
            _move_into_place(staging, dst)
            seeded.append(project_id)
        except Exception:  # noqa: BLE001 - seeding must never block startup
            logger.warning("could not seed example project %s into %s", project_id, root,
                           exc_info=True)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
    return seeded


def seed_default_checkout_projects() -> list[str]:
    """Seed ``<repo>/projects`` from ``examples/demo_project`` on start.

    A no-op unless this is a source checkout running on its default project
    root (no SEAM_PROJECT_ROOTS / SIONNATWIN_PROJECT_ROOTS override). Never
    raises."""
    try:
        from seam_studio.core.config import get_settings
        from seam_studio.core.paths import (
            EXAMPLE_PROJECTS_DIR,
            IS_SOURCE_CHECKOUT,
            REPO_ROOT,
        )

        if not IS_SOURCE_CHECKOUT or REPO_ROOT is None or EXAMPLE_PROJECTS_DIR is None:
            return []
        if os.environ.get("SEAM_PROJECT_ROOTS") or os.environ.get("SIONNATWIN_PROJECT_ROOTS"):
            return []
        roots = get_settings().project_roots
        root = REPO_ROOT / "projects"
        if not roots or Path(roots[0]) != root:
            return []
        root.mkdir(parents=True, exist_ok=True)
        seeded = seed_checkout_projects(root, EXAMPLE_PROJECTS_DIR)
        if seeded:
            logger.info("seeded example projects into %s: %s", root, ", ".join(seeded))
        return seeded
    except Exception:  # noqa: BLE001 - never block startup
        logger.warning("example project seeding failed", exc_info=True)
        return []
