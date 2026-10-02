"""Actor trajectory velocity on plain paths solves (real sionna-rt).

A paths solve without per-frame actor states is the t = 0 snapshot a sensing
solve is: every actor with a trajectory moves at its t = 0 tangent velocity,
so a comm path reflecting off a moving car carries that car's Doppler even
with a static TX and RX, and a device riding the car moves with it (as in
scenario frame 0). Static actors leave the solve Doppler-free, an explicit
actor_states caller (a scenario frame) keeps owning the velocities, and a
dataset sampling an actor's path keeps that actor's parked mesh at rest.
"""

import math
from pathlib import Path

import pytest
import trimesh

from seam_studio.schemas.actors import ActorState
from seam_studio.schemas.datasets import DatasetGenerateRequest, DatasetSampling
from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Actor, ActorTrajectory, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import SimulationConfig, TrajectorySimulateRequest
from seam_studio.services.availability import sionna_available
from seam_studio.services.dataset import generate_dataset
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import (
    _actor_states_at,
    _device_states_at,
    _solve_frame_paths,
    _velocities_at,
)
from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend
from seam_studio.services.trajectory import run_trajectory

pytestmark = pytest.mark.skipif(not sionna_available(), reason="sionna-rt not installed")

FREQ = 3.5e9  # keeps the ITU ground (valid 1-10 GHz) in band
LAM = 299_792_458.0 / FREQ
CAR = "actor-car_01"
V_CAR = [10.0, 0.0, 0.0]  # receding from the TX/RX pair at x = 0
DRIVE = ActorTrajectory(waypoints=[[20.0, 0.0, 0.0], [30.0, 0.0, 0.0]], dt_s=1.0)


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "actor_doppler.seam"
    (proj / "visual").mkdir(parents=True)
    (proj / "rf").mkdir()
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(120.0, 120.0, 0.2))
    ground.apply_translation((0.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    (proj / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))
    return proj


def _scene(trajectory=None) -> Scene:
    """Static TX/RX 8 m apart, 1 m up; the car's -x face (x = 17.75) mirrors
    TX -> car -> RX, so its velocity along +x shows up in that leg."""
    return Scene(
        scene_id="actor_doppler",
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
            Device(id="tx_001", kind="tx", position=[0.0, -4.0, 1.0], power_dbm=30.0),
            Device(id="rx_001", kind="rx", position=[0.0, 4.0, 1.0]),
        ],
        actors=[
            Actor(
                id="car_01",
                kind="car",
                position=[20.0, 0.0, 0.0],
                trajectory=trajectory.model_copy(deep=True) if trajectory else None,
            )
        ],
    )


def _config() -> SimulationConfig:
    return SimulationConfig(
        backend="sionna", frequency_hz=FREQ, max_depth=1, reflection=True,
        scattering=False, diffraction=False, num_samples=200_000,
    )


def _off_car(path) -> bool:
    return any(i.rf_material_id == CAR for i in path.interactions)


def _unit(a, b) -> list[float]:
    d = [bb - aa for aa, bb in zip(a, b)]
    n = math.sqrt(sum(c * c for c in d))
    return [c / n for c in d]


def _car_doppler_hz(path, v) -> float:
    """Sionna's per-bounce term v . (k_out - k_in) / lambda summed over the
    car interactions (positive when the path is closing)."""
    total = 0.0
    for i, inter in enumerate(path.interactions):
        if inter.rf_material_id != CAR:
            continue
        k_in = _unit(path.vertices[i], path.vertices[i + 1])
        k_out = _unit(path.vertices[i + 1], path.vertices[i + 2])
        total += sum(vc * (o - n) for vc, o, n in zip(v, k_out, k_in))
    return total / LAM


def test_sionna_plain_paths_carry_actor_trajectory_doppler(project: Path):
    result = SionnaBackend().simulate_paths(
        project, _scene(DRIVE), load_default_library(), _config()
    )

    doppler = result.metadata.get("doppler_hz")
    assert isinstance(doppler, list) and len(doppler) == len(result.paths), result.warnings
    off_car = [i for i, p in enumerate(result.paths) if _off_car(p)]
    assert off_car, (result.paths, result.warnings)
    for i in off_car:
        expected = _car_doppler_hz(result.paths[i], V_CAR)
        assert expected < -200.0  # receding face reflection, ~ -228 Hz
        assert doppler[i] == pytest.approx(expected, abs=1.0), result.paths[i]
    rest = [d for i, d in enumerate(doppler) if i not in off_car]
    assert rest and all(abs(d) < 1e-3 for d in rest)  # LoS and ground bounce


def test_sionna_static_actor_keeps_plain_solve_doppler_free(project: Path):
    backend = SionnaBackend()
    library = load_default_library()
    moving = backend.simulate_paths(project, _scene(DRIVE), library, _config())
    assert "doppler_hz" in moving.metadata

    # Same cached scene, car at rest: no Doppler surfaced, same geometry.
    static = backend.simulate_paths(project, _scene(), library, _config())
    assert any(_off_car(p) for p in static.paths), static.warnings
    assert "doppler_hz" not in static.metadata
    # Velocity never moves geometry (Sionna's path order may differ).
    assert sorted(p.power_dbm for p in static.paths) == pytest.approx(
        sorted(p.power_dbm for p in moving.paths), abs=1e-6
    )

    # A zero-velocity TX still asks for Doppler: the car legs read 0, so the
    # previous solve's car velocity did not stay on the cached scene.
    probe_scene = _scene()
    probe_scene.devices[0].velocity_m_s = [0.0, 0.0, 0.0]
    probe = backend.simulate_paths(project, probe_scene, library, _config())
    assert any(_off_car(p) for p in probe.paths)
    assert all(abs(d) < 1e-3 for d in probe.metadata["doppler_hz"])


def test_sionna_explicit_actor_states_own_actor_velocity(project: Path):
    # A scenario frame (or live pose) passes actor_states; absent velocities
    # then mean "at rest", never the trajectory's t = 0 tangent.
    scene = _scene(DRIVE)
    states = [
        ActorState(id=a.id, position=list(a.position), orientation_deg=list(a.orientation_deg))
        for a in scene.actors
    ]
    result = SionnaBackend().simulate_paths(
        project, scene, load_default_library(), _config(), actor_states=states
    )
    assert any(_off_car(p) for p in result.paths), result.warnings
    assert "doppler_hz" not in result.metadata


def test_sionna_trajectory_samples_see_actor_doppler(project: Path):
    # The moving-UE trajectory loop does not move actors per sample: they keep
    # their pose and move at the t = 0 velocity. A parked UE isolates the car.
    request = TrajectorySimulateRequest(
        ue_id="rx_001", waypoints=[[0.0, 4.0, 1.0], [0.0, 4.0, 1.0]], dt_s=0.1
    )
    library = load_default_library()
    backend = SionnaBackend()

    moving = run_trajectory(backend, project, _scene(DRIVE), library, _config(), request)
    spreads = moving.metadata.get("doppler_spread_hz")
    assert spreads and all(s is not None and s > 1.0 for s in spreads), moving.warnings

    static = run_trajectory(backend, project, _scene(), library, _config(), request)
    assert all(s == pytest.approx(0.0, abs=1e-3) for s in static.metadata["doppler_spread_hz"])


def test_sionna_any_moving_actor_surfaces_doppler(project: Path):
    # LoS only, so no path touches the car: the solve still reports Doppler
    # (all zero), because an actor in the scene moves at t = 0.
    result = SionnaBackend().simulate_paths(
        project, _scene(DRIVE), load_default_library(),
        _config().model_copy(update={"max_depth": 0}),
    )
    assert [p.path_type for p in result.paths] == ["los"], result.warnings
    assert result.metadata.get("doppler_hz") == [pytest.approx(0.0, abs=1e-3)]


def test_sionna_device_riding_a_moving_actor_matches_scenario_frame0(project: Path):
    # rx_001 rides the car: the plain t = 0 solve must move it with the car,
    # exactly like scenario frame 0, not leave it at rest.
    scene = _scene(DRIVE)
    scene.devices[0].position = [0.0, 0.0, 10.0]
    scene.devices[1].position = [20.0, 0.0, 2.5]
    scene.actors[0].attached_device_ids = ["rx_001"]
    library = load_default_library()
    backend = SionnaBackend()

    plain = backend.simulate_paths(project, scene, library, _config())

    states = _actor_states_at(scene, 0.0)
    _, device_positions = _device_states_at(scene, states)
    actor_v, device_v = _velocities_at(scene, 0.0)
    frame0 = _solve_frame_paths(
        backend, project, scene, library, _config(),
        states, device_positions, actor_v, device_v,
    )

    def los_doppler(result) -> float:
        dop = result.metadata["doppler_hz"]
        (i,) = [k for k, p in enumerate(result.paths) if p.path_type == "los"]
        return dop[i]

    k = _unit([0.0, 0.0, 10.0], [20.0, 0.0, 2.5])
    expected = -sum(v * c for v, c in zip(V_CAR, k)) / LAM  # receding RX, ~ -109 Hz
    assert los_doppler(frame0) == pytest.approx(expected, abs=1.0), frame0.warnings
    assert los_doppler(plain) == pytest.approx(expected, abs=1.0), plain.warnings
    assert sorted(plain.metadata["doppler_hz"]) == pytest.approx(
        sorted(frame0.metadata["doppler_hz"]), abs=1.0
    )
    # The scene the caller passed keeps its authored (unset) velocity.
    assert scene.devices[1].velocity_m_s is None


class _RecordingSionna(SionnaBackend):
    def __init__(self) -> None:
        super().__init__()
        self.results = []

    def simulate_paths(self, *args, **kwargs):
        result = super().simulate_paths(*args, **kwargs)
        self.results.append(result)
        return result


def test_sionna_dataset_parks_the_sampled_actor_at_rest(project: Path):
    # Actor-path sampling moves only the UE (rx_001's mount, x = 0 -> 10);
    # the car's mesh stays parked at x = 20 and must not Doppler-shift the
    # legs it reflects: those carry the UE's own Doppler only.
    scene = _scene(DRIVE)
    scene.actors[0].attached_device_ids = ["rx_001"]
    request = DatasetGenerateRequest(
        name="actor_path",
        sampling=DatasetSampling(mode="trajectory", num_samples=3, actor_id="car_01"),
        num_cfr_points=8,
    )
    backend = _RecordingSionna()
    generate_dataset(project, scene, load_default_library(), _config(), request, backend)

    assert len(backend.results) == 3
    checked = 0
    for result in backend.results:
        doppler = result.metadata["doppler_hz"]
        off_car = [i for i, p in enumerate(result.paths) if _off_car(p)]
        checked += len(off_car)
        for i in off_car:
            path = result.paths[i]
            ue_only = -sum(
                v * c for v, c in zip(V_CAR, _unit(path.vertices[-2], path.vertices[-1]))
            ) / LAM
            assert abs(_car_doppler_hz(path, V_CAR)) > 100.0  # what a moving car would add
            assert doppler[i] == pytest.approx(ue_only, abs=1.0), path
    # The face mirrors TX -> car -> UE for the first two samples (x = 0, 5).
    assert checked >= 2, [r.warnings for r in backend.results]
