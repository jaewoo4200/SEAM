"""Sensing solves on the real sionna-rt RCSSolver (sionna-rt >= 2.2).

Skipped without sionna-rt 2.2. When installed, these pin the parts the mock
cannot: the constant-RCS echo matches the bistatic radar equation (and so the
mock) in gain, Doppler and phase; a TR 38.901 vehicle yields its 5 scattering
points; the target replaces its actor mesh in the echo solve and, as an
absorber, in the comm paths (include_comm_paths, channel npz); actor
velocities never leak from one solve into the next; and the cached Sionna
scene is left exactly as a plain load (targets, their absorber materials, and
the hidden actor mesh all restored).
"""

import math
from pathlib import Path

import pytest
import trimesh

from seam_studio.schemas.actors import ActorState
from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Actor, ActorTrajectory, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.sensing import SensingSimulateRequest
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.availability import sionna_available, sionna_rcs_available
from seam_studio.services.project_store import load_default_library
from seam_studio.services.sensing import run_sensing, select_targets
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.simulation_backends.sionna_backend import (
    SionnaBackend,
    _load_scene_cached,
)

pytestmark = pytest.mark.skipif(
    not (sionna_available() and sionna_rcs_available()),
    reason="sionna-rt >= 2.2 (sionna.rt.rcs) not installed",
)

# 3.5 GHz keeps the ITU ground (valid 1-10 GHz) in band.
FREQ = 3.5e9
SAMPLES = 100_000


def _write_ground(project_dir: Path) -> None:
    (project_dir / "visual").mkdir(parents=True, exist_ok=True)
    (project_dir / "rf").mkdir(exist_ok=True)
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(120.0, 120.0, 0.2))
    ground.apply_translation((0.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    (project_dir / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "sensing_it.seam"
    _write_ground(proj)
    return proj


def _scene(sensing: dict, rx=(0.0, 0.0, 10.0), scene_id: str = "sensing_it") -> Scene:
    """Car on +x of a co-located (monostatic) radar 10 m above the ground."""
    return Scene(
        scene_id=scene_id,
        prims=[
            Prim(
                id="/ground",
                name="ground",
                semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            )
        ],
        devices=[
            Device(id="tx_001", kind="tx", position=[0.0, 0.0, 10.0], power_dbm=30.0),
            Device(id="rx_001", kind="rx", position=list(rx)),
        ],
        actors=[Actor(id="car_01", kind="car", position=[30.0, 0.0, 0.0], sensing=sensing)],
    )


def _config(**overrides) -> SimulationConfig:
    base = dict(
        backend="sionna", frequency_hz=FREQ, max_depth=1, reflection=False,
        num_samples=SAMPLES,
    )
    return SimulationConfig(**{**base, **overrides})


def _run(backend, project: Path, scene: Scene, config: SimulationConfig, **request):
    req = SensingSimulateRequest(samples_per_sp=SAMPLES, **request)
    return run_sensing(
        backend, project, scene, load_default_library(), config, req,
        select_targets(scene, None),
    )


def test_sionna_constant_target_matches_radar_equation(project: Path):
    # +x velocity on a target at +x of the radar: receding, negative Doppler.
    # 20 dBsm (100 m^2), not 10: 10 dBsm == 10 m^2 would hide a missing
    # dBsm -> m^2 conversion.
    scene = _scene({"model": "constant", "rcs_dbsm": 20.0, "velocity_m_s": [10.0, 0.0, 0.0]})
    config = _config()

    result = _run(SionnaBackend(), project, scene, config)
    mock = _run(MockBackend(), project, scene, config)

    assert result.backend == "sionna" and result.metadata["solver"] == "RCSSolver"
    sensing = [p for p in result.paths if p.target_id is not None]
    assert len(sensing) == 1, (result.paths, result.warnings)
    path, expected = sensing[0], mock.paths[0]
    assert path.path_type == "sensing"
    assert path.interactions[0].type == "sensing"
    assert path.interactions[0].prim_id == "car_01"
    # Monostatic radar equation, iso antennas: lambda^2 sigma / ((4 pi)^3 d^4).
    lam = 299_792_458.0 / FREQ
    d = math.dist([0.0, 0.0, 10.0], [30.0, 0.0, 0.75])
    closed_form = 10.0 * math.log10(lam**2 * 100.0 / ((4 * math.pi) ** 3 * d**4))
    assert abs(path.path_gain_db - closed_form) < 0.5
    assert abs(path.path_gain_db - expected.path_gain_db) < 0.5
    assert abs(path.doppler_hz - expected.doppler_hz) < 1.0
    assert path.doppler_hz < 0.0
    assert path.delay_ns == pytest.approx(expected.delay_ns, abs=1e-3)
    # angle(a) of a constant-RCS echo is 0, so the carrier-phase convention
    # makes the total phase the pure propagation term, as in the mock.
    assert abs(math.remainder(path.phase_rad - expected.phase_rad, 2 * math.pi)) < 1e-2
    assert result.targets[0].num_scattering_points == 1
    assert result.targets[0].path_count == 1


def test_sionna_tr38901_vehicle_multi_sp(project: Path):
    scene = _scene({"model": "tr38901", "velocity_m_s": [-5.0, 0.0, 0.0]})
    result = _run(SionnaBackend(), project, scene, _config())

    summary = result.targets[0]
    assert summary.object_type == "vehicle-multi-sp" and summary.model_type == 2
    assert summary.num_scattering_points == 5
    assert len(summary.scattering_points) == 5
    sensing = [p for p in result.paths if p.target_id is not None]
    assert sensing, result.warnings
    assert summary.path_count == len(sensing)
    for p in sensing:
        assert p.target_id == "car_01"
        assert p.doppler_hz is not None and p.doppler_hz > 0.0  # approaching
        point = p.interactions[0].point
        # Every echo leaves from one of the summary's world scattering points.
        assert min(math.dist(point, sp) for sp in summary.scattering_points) < 1e-3


def test_sionna_cache_clean_after_sensing(project: Path):
    # Bistatic RX placed so the comm solve's specular bounce lands on the car
    # roof (the car also blocks the ground bounce under it).
    scene = _scene(
        {"model": "constant", "rcs_dbsm": 5.0}, rx=(60.0, 0.0, 10.0)
    )
    comm_config = _config(max_depth=2, reflection=True)
    backend = SionnaBackend()

    before = backend.simulate_paths(project, scene, load_default_library(), comm_config)
    assert before.paths, before.warnings
    car_hits_before = sum(
        1 for p in before.paths for i in p.interactions if i.rf_material_id == "actor-car_01"
    )
    assert car_hits_before > 0, "the comm solve must interact with the car box"

    sensing = _run(backend, project, scene, _config())
    assert sensing.paths, sensing.warnings

    rt_scene = _load_scene_cached(project / "rf" / "generated_scene.xml", [])
    assert rt_scene.sensing_targets == {}
    assert not [m for m in rt_scene.radio_materials if m.startswith("seam-st-")]
    assert "shape-actor-car_01" in rt_scene.objects or "actor-car_01" in rt_scene.objects

    after = backend.simulate_paths(project, scene, load_default_library(), comm_config)
    assert len(after.paths) == len(before.paths)
    car_hits_after = sum(
        1 for p in after.paths for i in p.interactions if i.rf_material_id == "actor-car_01"
    )
    assert car_hits_after == car_hits_before

    # A second sensing solve reuses the same target names on the cached scene.
    again = _run(backend, project, scene, _config())
    assert len(again.paths) == len(sensing.paths)
    assert not any("failed" in w for w in again.warnings)


def _hits(paths, material_id: str) -> int:
    return sum(1 for p in paths for i in p.interactions if i.rf_material_id == material_id)


def test_sionna_hides_the_target_actor_mesh(project: Path):
    # Contract A5. A target cuboid smaller than the car box puts its
    # scattering point inside the box: only removing the actor mesh lets the
    # echo out, and no leg may reflect off the box the RCS model replaces.
    scene = _scene({"model": "constant", "rcs_dbsm": 10.0, "size_m": [1.0, 1.0, 0.5]})
    result = _run(SionnaBackend(), project, scene, _config(max_depth=2, reflection=True))
    assert not [w for w in result.warnings if "not individually addressable" in w]
    direct = [p for p in result.paths if [i.type for i in p.interactions] == ["sensing"]]
    assert len(direct) == 1, (result.paths, result.warnings)
    assert _hits(result.paths, "actor-car_01") == 0


def test_sionna_include_comm_paths_targets_absorb(project: Path):
    # Bistatic RX placed so a plain paths solve bounces off the car roof. In
    # the comm half of a sensing result the target stands in for the car as
    # an absorber (Sionna's PathSolver semantics): the echo alone describes it.
    v_tx = [5.0, 0.0, 0.0]
    scene = _scene({"model": "constant", "rcs_dbsm": 5.0}, rx=(60.0, 0.0, 10.0))
    scene.devices[0].velocity_m_s = v_tx
    config = _config(max_depth=2, reflection=True)
    plain = SionnaBackend().simulate_paths(project, scene, load_default_library(), config)
    assert _hits(plain.paths, "actor-car_01") > 0

    result = _run(SionnaBackend(), project, scene, config, include_comm_paths=True)
    sensing = [p for p in result.paths if p.target_id is not None]
    comm = [p for p in result.paths if p.target_id is None]
    assert sensing and comm, result.warnings
    assert result.metadata["comm_path_count"] == len(comm)
    assert all(p.path_id.startswith("path_") for p in comm)
    assert all(p.path_type != "sensing" for p in comm)
    assert _hits(comm, "actor-car_01") == 0
    # The moving TX Doppler-shifts every comm path by v_tx . k_departure / lambda.
    lam = 299_792_458.0 / FREQ
    for p in comm:
        leg = [b - a for a, b in zip(p.vertices[0], p.vertices[1])]
        norm = math.sqrt(sum(c * c for c in leg))
        expected = sum(v * c / norm for v, c in zip(v_tx, leg)) / lam
        assert p.doppler_hz == pytest.approx(expected, abs=1.0), p


def test_sionna_comm_paths_run_on_the_builtin_engine(project: Path):
    # The echo solve always uses the builtin engine; so must the comm half,
    # or one result would mix two engines (here: a missing one).
    scene = _scene({"model": "constant", "rcs_dbsm": 5.0}, rx=(60.0, 0.0, 10.0))
    config = _config(max_depth=2, reflection=True, engine="no-such-engine")
    result = _run(SionnaBackend(), project, scene, config, include_comm_paths=True)
    assert [p for p in result.paths if p.target_id is None], result.warnings
    assert any("config.engine ignored" in w for w in result.warnings)
    assert not any("no-such-engine" in w and "unavailable" in w for w in result.warnings)


def test_sionna_sensing_ignores_stale_actor_velocities(project: Path):
    # Monostatic radar 1 m up, constant target at (20, 0) and a static truck
    # (car_02) whose side face mirrors legs TX -> truck -> scattering point.
    scene = _scene({"model": "constant", "rcs_dbsm": 10.0}, rx=(0.0, 0.0, 1.0))
    scene.devices[0].position = [0.0, 0.0, 1.0]
    scene.actors[0].position = [20.0, 0.0, 0.0]
    scene.actors.append(Actor(id="car_02", kind="car", position=[10.0, 8.0, 0.0]))
    config = _config(max_depth=2, reflection=True)
    backend = SionnaBackend()
    library = load_default_library()

    # A scenario frame leaves the truck moving on the cached scene.
    states = [
        ActorState(id=a.id, position=list(a.position), orientation_deg=list(a.orientation_deg))
        for a in scene.actors
    ]
    backend.simulate_paths(
        project, scene, library, config,
        actor_states=states, actor_velocities={"car_02": [0.0, 10.0, 0.0]},
    )
    result = _run(backend, project, scene, config)
    assert _hits(result.paths, "actor-car_02") > 0, result.warnings
    assert all(abs(p.doppler_hz) < 1e-3 for p in result.paths)

    # A truck that really moves (trajectory tangent at t = 0) shifts its legs.
    scene.actors[1].trajectory = ActorTrajectory(
        waypoints=[[10.0, 8.0, 0.0], [10.0, 18.0, 0.0]], dt_s=1.0
    )
    moving = _run(backend, project, scene, config)
    off_truck = [
        p for p in moving.paths
        if any(i.rf_material_id == "actor-car_02" for i in p.interactions)
    ]
    assert off_truck and all(abs(p.doppler_hz) > 1.0 for p in off_truck)
    rest = [p for p in moving.paths if p not in off_truck]
    assert rest and all(abs(p.doppler_hz) < 1e-3 for p in rest)


def test_sionna_radar_riding_a_moving_actor_moves_with_it(project: Path):
    # Monostatic radar mounted on an ego car driving +x at 10 m/s toward a
    # static target: the echo carries the ego motion (closing, ~ +223 Hz at
    # 3.5 GHz), on Sionna and the mock alike, though no device sets a velocity.
    scene = _scene({"model": "constant", "rcs_dbsm": 20.0})
    scene.actors.append(
        Actor(
            id="ego_01",
            kind="car",
            position=[0.0, 0.0, 0.0],
            trajectory=ActorTrajectory(waypoints=[[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]], dt_s=1.0),
            attached_device_ids=["tx_001", "rx_001"],
        )
    )
    config = _config()

    result = _run(SionnaBackend(), project, scene, config)
    mock = _run(MockBackend(), project, scene, config)

    sensing = [p for p in result.paths if p.target_id is not None]
    assert len(sensing) == 1, (result.paths, result.warnings)
    lam = 299_792_458.0 / FREQ
    leg = [30.0, 0.0, 0.75 - 10.0]
    expected = 2.0 * 10.0 * leg[0] / math.sqrt(sum(c * c for c in leg)) / lam
    assert sensing[0].doppler_hz == pytest.approx(expected, abs=1.0)
    assert mock.paths[0].doppler_hz == pytest.approx(expected, abs=1e-6)
    assert all(d.velocity_m_s is None for d in scene.devices)


def test_sionna_channel_npz_sensing_targets_absorb(project: Path):
    # With include_sensing the per-UE comm solve gets the stored targets as
    # absorbers, exactly like the sensing result's own comm paths.
    from seam_studio.schemas.results import ChannelNpzExportRequest
    from seam_studio.services.channel_npz_export import export_channel_npz

    scene = _scene({"model": "constant", "rcs_dbsm": 5.0}, rx=(60.0, 0.0, 10.0))
    config = _config(max_depth=2, reflection=True)
    library = load_default_library()
    ue = [[60.0, 0.0, 10.0]]
    request = ChannelNpzExportRequest(ue_source="explicit", ue_positions=ue, max_paths=16)
    targets = select_targets(scene, None)
    backend = SionnaBackend()

    plain = export_channel_npz(backend, project, scene, library, config, request, ue)
    absorbed = export_channel_npz(
        backend, project, scene, library, config, request, ue,
        sensing_paths=[], sensing_targets=targets,
    )
    comm = backend.simulate_paths_with_targets(project, scene, library, config, targets)
    assert absorbed["path_count"] == len(comm.paths) < plain["path_count"]


def test_sionna_sensing_api_200(api_client):
    pid = "sensing_live"
    assert api_client.post(
        "/api/projects", json={"name": "Sensing live", "project_id": pid}
    ).status_code == 201
    from seam_studio.api import deps

    _write_ground(deps.get_store().resolve(pid))
    scene = _scene(
        {"model": "tr38901", "velocity_m_s": [-10.0, 0.0, 0.0]}, scene_id=pid
    )
    put = api_client.put(f"/api/projects/{pid}/scene", json=scene.model_dump(mode="json"))
    assert put.status_code == 200, put.text

    config = _config().model_dump(mode="json")
    resp = api_client.post(
        f"/api/projects/{pid}/simulate/sensing",
        json={"config": config, "samples_per_sp": SAMPLES},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result_id"] == "sionna_sensing_001"
    assert body["targets"][0]["num_scattering_points"] == 5
    assert body["metadata"]["sensing_path_count"] == len(body["paths"]) > 0
    stored = api_client.get(f"/api/projects/{pid}/results/sensing")
    assert stored.status_code == 200 and stored.json() == body
