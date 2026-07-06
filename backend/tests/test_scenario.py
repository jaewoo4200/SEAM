"""Tests for actor compilation, the scenario service, and live state-sync.

Covers (BE-ACTORS):
- rf_compiler emits each actor as its own shape + a manifest "actors" list;
- run_scenario (mock): frame count, actors move along waypoints, per-pair
  LinkMetrics with SINR filled, include_paths toggles the heavy paths field;
- POST /live/state: apply + persist roundtrip, unknown ids reported,
  resimulate returns links;
- a Sionna-guarded test that a compiled actor scene loads and one frame solves.
"""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.actors import (
    ActorState,
    LiveStateUpdate,
    ScenarioSimulateRequest,
)
from app.schemas.devices import Device
from app.schemas.materials import RFMaterialLibrary
from app.schemas.scene import (
    Actor,
    ActorTrajectory,
    MeshRef,
    Prim,
    RFBinding,
    Scene,
)
from app.schemas.simulation import SimulationConfig
from app.services import project_store
from app.services.availability import sionna_available
from app.services.rf_compiler import compile_project
from app.services.scenario import actor_position_at, run_scenario
from app.services.simulation_backends.mock_backend import MockBackend
from app.services.simulation_backends.sionna_backend import SionnaBackend

SIONNA_INSTALLED = sionna_available()

WALL_ID = "/buildings/b01/wall"


def _build_glb(project_dir: Path) -> None:
    wall = trimesh.creation.box(extents=[4.0, 0.2, 3.0])
    wall.apply_translation([0.0, 0.0, 1.5])
    ground = trimesh.creation.box(extents=[60.0, 60.0, 0.2])
    ground.apply_translation([0.0, 0.0, -0.1])
    tm_scene = trimesh.Scene()
    tm_scene.add_geometry(wall, geom_name="wall_box", node_name="wall_box")
    tm_scene.add_geometry(ground, geom_name="ground_box", node_name="ground_box")
    visual = project_dir / "visual"
    visual.mkdir(parents=True, exist_ok=True)
    tm_scene.export(visual / "scene.glb")


def _car_actor() -> Actor:
    return Actor(
        id="car_001",
        kind="car",
        position=[-30.0, 0.0, 0.0],
        trajectory=ActorTrajectory(
            waypoints=[
                [-30.0, 0.0, 0.0],
                [-10.0, 0.0, 0.0],
                [10.0, 0.0, 0.0],
                [30.0, 0.0, 0.0],
            ],
            dt_s=0.5,
            loop=False,
        ),
    )


def _scene_with_actor() -> Scene:
    return Scene(
        scene_id="scenario_test",
        name="Scenario Test",
        prims=[
            Prim(
                id="/ground",
                name="ground",
                semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground_box"),
                rf=RFBinding(
                    material_id="ground_28ghz",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
            Prim(
                id=WALL_ID,
                name="wall",
                semantic_tags=["building", "wall"],
                mesh_ref=MeshRef(mesh_name="wall_box"),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=[-40.0, 0.0, 8.0], power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=[40.0, 0.0, 1.5]),
        ],
        actors=[_car_actor()],
    )


@pytest.fixture()
def library() -> RFMaterialLibrary:
    return project_store.load_default_library()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "scenario_test.sionnatwin"
    _build_glb(project_dir)
    return project_dir


# --------------------------------------------------------- compiler: actors


def test_compile_emits_actor_shapes(project: Path, library: RFMaterialLibrary) -> None:
    result = compile_project(project, _scene_with_actor(), library)
    assert result.ok is True

    # An actor mesh is exported per actor, base-center baked (car h=1.5 ->
    # centroid z = 0.75, size 4.5 x 1.8 x 1.5).
    car_ply = project / "rf" / "meshes" / "actor_car_001.ply"
    assert car_ply.is_file()
    mesh = trimesh.load_mesh(car_ply)
    centroid = mesh.bounds.mean(axis=0)
    assert centroid[2] == pytest.approx(0.75, abs=1e-4)
    assert list(mesh.extents) == pytest.approx([4.5, 1.8, 1.5], abs=1e-4)

    # Actor shape is its OWN shape id, separate from the material groups.
    root = ET.parse(project / "rf" / "generated_scene.xml").getroot()
    shape_ids = [s.attrib["id"] for s in root.findall("shape")]
    assert "shape-actor-car_001" in shape_ids
    # Existing group behavior is untouched.
    assert "shape-itu_concrete" in shape_ids
    actor_shape = next(
        s for s in root.findall("shape") if s.attrib["id"] == "shape-actor-car_001"
    )
    assert actor_shape.find("string").attrib["value"] == "meshes/actor_car_001.ply"
    # The actor gets its OWN unique bsdf (never shared with static geometry -
    # Sionna merges shapes sharing a bsdf, which would make the actor
    # immovable). ITU-backed materials use the itu-radio-material plugin so
    # the frequency-dependent ITU tables still apply.
    assert actor_shape.find("ref").attrib["id"] == "mat-actor-car_001"
    actor_bsdf = next(
        b for b in root.findall("bsdf") if b.attrib["id"] == "mat-actor-car_001"
    )
    assert actor_bsdf.attrib["type"] == "itu-radio-material"
    assert actor_bsdf.find("string[@name='type']").attrib["value"] == "metal"


def test_manifest_lists_actors(project: Path, library: RFMaterialLibrary) -> None:
    compile_project(project, _scene_with_actor(), library)
    manifest = json.loads(
        (project / "rf" / "compile_manifest.json").read_text(encoding="utf-8")
    )
    assert "actors" in manifest
    assert len(manifest["actors"]) == 1
    entry = manifest["actors"][0]
    assert entry["actor_id"] == "car_001"
    assert entry["mesh_file"] == "rf/meshes/actor_car_001.ply"
    assert entry["rf_material_id"] == "metal"
    assert entry["itu_name"] == "itu_metal"


def test_actors_are_deterministic(project: Path, library: RFMaterialLibrary) -> None:
    compile_project(project, _scene_with_actor(), library)
    xml_first = (project / "rf" / "generated_scene.xml").read_bytes()
    compile_project(project, _scene_with_actor(), library)
    assert (project / "rf" / "generated_scene.xml").read_bytes() == xml_first


# ------------------------------------------------------ scenario (mock run)


def test_actor_position_at_waypoints() -> None:
    actor = _car_actor()  # dt 0.5, 4 waypoints, no loop
    assert actor_position_at(actor, 0.0) == [-30.0, 0.0, 0.0]
    assert actor_position_at(actor, 0.5) == [-10.0, 0.0, 0.0]
    assert actor_position_at(actor, 1.0) == [10.0, 0.0, 0.0]
    # LINEAR INTERPOLATION between waypoints (smooth motion).
    assert actor_position_at(actor, 0.25) == pytest.approx([-20.0, 0.0, 0.0])
    # Clamp past the end (mode "once").
    assert actor_position_at(actor, 5.0) == [30.0, 0.0, 0.0]


def test_actor_position_loop_and_pingpong() -> None:
    actor = _car_actor()  # 4 waypoints span=3, dt 0.5
    actor.trajectory.loop = True  # legacy flag -> mode "loop"
    # s = t/dt wraps over the span (3): t=2.0 -> s=4 -> s%3=1 -> waypoint 1.
    assert actor_position_at(actor, 2.0) == pytest.approx([-10.0, 0.0, 0.0])

    actor.trajectory.mode = "pingpong"
    # s=4 on a 0..3 triangle (period 6) -> reflected to 2 -> waypoint 2.
    assert actor_position_at(actor, 2.0) == pytest.approx([10.0, 0.0, 0.0])
    # s=5 -> reflected to 1.
    assert actor_position_at(actor, 2.5) == pytest.approx([-10.0, 0.0, 0.0])


def test_scenario_mock_frames_and_movement(project, library) -> None:
    backend = MockBackend()
    config = SimulationConfig(backend="mock")
    request = ScenarioSimulateRequest(num_frames=4, dt_s=0.5, include_paths=True)

    result = run_scenario(backend, project, _scene_with_actor(), library, config, request)

    assert result.backend == "mock"
    assert len(result.frames) == 4
    assert [f.time_s for f in result.frames] == [0.0, 0.5, 1.0, 1.5]

    # The actor moves along its waypoints: its per-frame position changes.
    car_positions = [
        next(s.position for s in f.actor_states if s.id == "car_001")
        for f in result.frames
    ]
    assert car_positions[0] != car_positions[1]
    assert car_positions == [
        [-30.0, 0.0, 0.0],
        [-10.0, 0.0, 0.0],
        [10.0, 0.0, 0.0],
        [30.0, 0.0, 0.0],
    ]


def test_scenario_mock_links_have_sinr(project, library) -> None:
    backend = MockBackend()
    config = SimulationConfig(backend="mock")
    request = ScenarioSimulateRequest(num_frames=2, dt_s=0.5, include_paths=True)
    result = run_scenario(backend, project, _scene_with_actor(), library, config, request)

    for frame in result.frames:
        # One LinkMetrics per tx x rx pair (1 x 1 here).
        assert len(frame.links) == 1
        link = frame.links[0]
        assert link.tx_id == "tx_001"
        assert link.rx_id == "rx_001"
        assert link.path_count > 0
        assert link.rss_dbm is not None
        assert link.path_gain_db is not None
        assert link.sinr_db is not None
        # Single TX: no interferer, SINR degenerates to SNR semantics.
        assert link.interference_dbm is None


def test_scenario_two_tx_per_link_interference(project, library) -> None:
    """With two TXs each link's SINR is S/(I+N): the other TX's power at the
    same RX counts as co-channel interference, and the two links swap the
    signal/interferer roles symmetrically."""
    import math

    from app.schemas.devices import Device

    backend = MockBackend()
    scene = _scene_with_actor()
    scene.devices.append(
        Device(id="tx_002", kind="tx", position=[50.0, -20.0, 10.0])
    )
    config = SimulationConfig(backend="mock")
    request = ScenarioSimulateRequest(num_frames=1, dt_s=0.5)
    result = run_scenario(backend, project, scene, library, config, request)

    links = {l.tx_id: l for l in result.frames[0].links}
    assert set(links) == {"tx_001", "tx_002"}
    lin = lambda d: 10.0 ** (d / 10.0)  # noqa: E731
    noise = -87.0  # 100 MHz + NF 7 dB thermal floor (see noise_floor_dbm)
    for tx_id, other in (("tx_001", "tx_002"), ("tx_002", "tx_001")):
        link = links[tx_id]
        assert link.interference_dbm is not None
        # The interferer's received power IS the other link's RSS.
        assert link.interference_dbm == pytest.approx(links[other].rss_dbm, abs=1e-9)
        expect = link.rss_dbm - 10.0 * math.log10(lin(noise) + lin(link.interference_dbm))
        assert link.sinr_db == pytest.approx(expect, abs=0.2)
        # Interference dominates this geometry: SINR well below SNR.
        assert link.sinr_db < (link.rss_dbm - noise) - 1.0


def test_scenario_include_paths_toggle(project, library) -> None:
    backend = MockBackend()
    config = SimulationConfig(backend="mock")

    with_paths = run_scenario(
        backend, project, _scene_with_actor(), library, config,
        ScenarioSimulateRequest(num_frames=1, include_paths=True),
    )
    assert with_paths.frames[0].paths is not None
    assert len(with_paths.frames[0].paths) > 0

    without = run_scenario(
        backend, project, _scene_with_actor(), library, config,
        ScenarioSimulateRequest(num_frames=1, include_paths=False),
    )
    assert without.frames[0].paths is None


# --------------------------------------------------------------- live/state


def _test_app() -> FastAPI:
    """Minimal app wiring the simulate + scenario routers (main.py wiring is
    the lead's job; this keeps the API tests self-contained)."""
    from app.api import scenario as scenario_api
    from app.api import simulate as simulate_api

    app = FastAPI()
    app.include_router(simulate_api.router, prefix="/api")
    app.include_router(scenario_api.router, prefix="/api")
    return app


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    from app.api.deps import get_store
    from app.core.config import get_settings

    get_settings.cache_clear()
    get_store.cache_clear()

    store = get_store()
    store.create_project("Scenario Test", project_id="scenario_test")
    # Build the GLB inside the created project so the compile has geometry.
    project_dir = store.resolve("scenario_test")
    _build_glb(project_dir)
    store.save_scene("scenario_test", _scene_with_actor())

    client = TestClient(_test_app())
    try:
        yield client, tmp_path
    finally:
        get_settings.cache_clear()
        get_store.cache_clear()


MOCK_CFG = {"config": {"backend": "mock"}}


def test_live_state_apply_and_persist(api_client) -> None:
    client, _root = api_client
    resp = client.post(
        "/api/projects/scenario_test/live/state",
        json={
            "actors": [{"id": "car_001", "position": [5.0, 1.0, 0.0]}],
            "devices": [{"id": "rx_001", "position": [12.0, 0.0, 1.5]}],
            "persist": True,
            "resimulate": False,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied_actors"] == ["car_001"]
    assert body["applied_devices"] == ["rx_001"]
    assert body["unknown_ids"] == []

    # Persisted: the stored scene now reflects the pushed positions.
    from app.api.deps import get_store

    scene = get_store().load_scene("scenario_test")
    car = next(a for a in scene.actors if a.id == "car_001")
    assert car.position == [5.0, 1.0, 0.0]
    rx = next(d for d in scene.devices if d.id == "rx_001")
    assert rx.position == [12.0, 0.0, 1.5]


def test_live_state_reports_unknown_ids(api_client) -> None:
    client, _root = api_client
    resp = client.post(
        "/api/projects/scenario_test/live/state",
        json={
            "actors": [{"id": "ghost_actor", "position": [0.0, 0.0, 0.0]}],
            "devices": [{"id": "ghost_device", "position": [0.0, 0.0, 0.0]}],
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["unknown_ids"]) == {"ghost_actor", "ghost_device"}
    assert body["applied_actors"] == []
    assert body["applied_devices"] == []


def test_live_state_resimulate_returns_links(api_client) -> None:
    client, _root = api_client
    resp = client.post(
        "/api/projects/scenario_test/live/state",
        json={
            "actors": [{"id": "car_001", "position": [0.0, 0.0, 0.0]}],
            "resimulate": True,
            "persist": False,
        },
    )
    # LiveStateUpdate carries no config; resimulate resolves the scene's default
    # config and runs whatever backend "auto" picks (sionna if installed, else
    # mock). Either way we get a well-formed link row per tx->rx pair.
    assert resp.status_code == 200
    body = resp.json()
    assert body["applied_actors"] == ["car_001"]
    assert len(body["links"]) == 1
    assert body["links"][0]["tx_id"] == "tx_001"
    assert body["links"][0]["rx_id"] == "rx_001"


def test_scenario_api_roundtrip(api_client) -> None:
    client, _root = api_client
    resp = client.post(
        "/api/projects/scenario_test/simulate/scenario",
        json={"num_frames": 3, "dt_s": 0.5, "include_paths": False, **MOCK_CFG},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["kind"] == "scenario"
    assert body["backend"] == "mock"
    assert body["result_id"] == "mock_scenario_001"
    assert len(body["frames"]) == 3

    fetched = client.get("/api/projects/scenario_test/results/scenario")
    assert fetched.status_code == 200
    assert fetched.json()["result_id"] == "mock_scenario_001"


# ------------------------------------------------------- sionna-guarded


@pytest.mark.skipif(not SIONNA_INSTALLED, reason="requires sionna-rt installed")
def test_sionna_scenario_frame_solves(project, library) -> None:
    backend = SionnaBackend()
    # 3.5 GHz keeps ITU ground/metal in-band; small sample budget for speed.
    config = SimulationConfig(
        backend="sionna", frequency_hz=3.5e9, max_depth=2, num_samples=200_000
    )
    request = ScenarioSimulateRequest(num_frames=2, dt_s=0.5, include_paths=True)

    result = run_scenario(backend, project, _scene_with_actor(), library, config, request)

    assert result.backend == "sionna"
    assert len(result.frames) == 2
    # The compiled actor scene loaded and at least one frame produced a link
    # with paths (the tx->rx LoS is clear, so a path is expected).
    total_paths = sum(len(f.paths or []) for f in result.frames)
    assert total_paths > 0, f"expected some paths; warnings={result.warnings}"
    # Actor moved between frames.
    car0 = next(s.position for s in result.frames[0].actor_states if s.id == "car_001")
    car1 = next(s.position for s in result.frames[1].actor_states if s.id == "car_001")
    assert car0 != car1
