"""Doppler / time-varying CIR tests (docs/dynamic_scattering.md).

Two layers, mirroring the rest of the suite:

- Backend-independent unit tests (always run): the velocity schema field, the
  Doppler-spread and time-envelope math, and that the trajectory/scenario
  services plumb a per-waypoint / per-actor velocity into the solve. The solver
  is monkeypatched with a capturing fake so these need no Sionna and no GPU.
- Sionna-guarded integration test (skipped when sionna-rt is absent): a real
  solve on a tiny LoS scene with a moving RX yields paths.doppler ~= f*v/c and
  the value is surfaced through PathResultSet.metadata["doppler_hz"].
"""

import math
from pathlib import Path

import pytest
import trimesh

from seam_studio.schemas.channel import ChannelAnalysisRequest
from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import PathResultSet, RayPath
from seam_studio.schemas.scene import (
    Actor,
    ActorTrajectory,
    MeshRef,
    Prim,
    RFBinding,
    Scene,
)
from seam_studio.schemas.simulation import (
    SimulationConfig,
    TrajectorySimulateRequest,
)
from seam_studio.schemas.actors import ScenarioSimulateRequest
from seam_studio.services import channel_analysis as ca
from seam_studio.services import scenario as scen
from seam_studio.services import trajectory as traj
from seam_studio.services.availability import sionna_available
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.base import UNSAVED_RESULT_ID, RayTracingBackend
from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend

C = 299_792_458.0
SIONNA_INSTALLED = sionna_available()


# ===================================================== schema: velocity field


def test_device_velocity_defaults_none():
    d = Device(id="rx_001", kind="rx", position=[0.0, 0.0, 1.5])
    assert d.velocity_m_s is None


def test_device_velocity_roundtrips():
    d = Device(id="rx_001", kind="rx", position=[0.0, 0.0, 1.5], velocity_m_s=[30.0, 0.0, 0.0])
    assert d.velocity_m_s == [30.0, 0.0, 0.0]
    # Survives a serialize/parse cycle (what the API persists).
    assert Device.model_validate(d.model_dump()).velocity_m_s == [30.0, 0.0, 0.0]


def test_device_velocity_rejects_wrong_length():
    with pytest.raises(Exception):
        Device(id="rx_001", kind="rx", position=[0.0, 0.0, 1.5], velocity_m_s=[1.0, 2.0])


# ===================================================== Doppler-spectrum math


def _paths_with(powers_dbm):
    return [
        RayPath(
            path_id=f"p{i}", tx_id="tx", rx_id="rx", path_type="los",
            vertices=[[0, 0, 0], [1, 0, 0]], power_dbm=pw, delay_ns=10.0 * (i + 1),
        )
        for i, pw in enumerate(powers_dbm)
    ]


def test_doppler_metrics_hand_computed():
    # Equal-power paths at +100 and -100 Hz: mean 0, spread 100, max 100.
    paths = _paths_with([0.0, 0.0])
    mean, spread, max_abs, coh_ms = ca.doppler_metrics(paths, [100.0, -100.0])
    assert mean == pytest.approx(0.0, abs=1e-9)
    assert spread == pytest.approx(100.0, abs=1e-9)
    assert max_abs == pytest.approx(100.0, abs=1e-9)
    # Coherence time ~= 0.42 / f_d,max -> in ms.
    assert coh_ms == pytest.approx(0.42 / 100.0 * 1e3, abs=1e-9)


def test_doppler_metrics_power_weighted_mean():
    # Path A (0 dBm = 1 mW) at 200 Hz, path B (-10 dBm = 0.1 mW) at 0 Hz.
    paths = _paths_with([0.0, -10.0])
    mean, spread, max_abs, _ = ca.doppler_metrics(paths, [200.0, 0.0])
    assert mean == pytest.approx(200.0 * 1.0 / 1.1, abs=1e-6)  # 181.8 Hz
    assert max_abs == pytest.approx(200.0)
    assert spread > 0.0


def test_doppler_metrics_none_without_doppler():
    paths = _paths_with([0.0, -10.0])
    assert ca.doppler_metrics(paths, None) == (None, None, None, None)
    # Misaligned length -> all None (never a partial answer).
    assert ca.doppler_metrics(paths, [1.0]) == (None, None, None, None)


def test_doppler_metrics_zero_when_static():
    # All paths at 0 Hz: mean/spread/max 0, coherence time undefined (None).
    paths = _paths_with([0.0, 0.0])
    mean, spread, max_abs, coh_ms = ca.doppler_metrics(paths, [0.0, 0.0])
    assert (mean, spread, max_abs) == (0.0, 0.0, 0.0)
    assert coh_ms is None


# ===================================================== time-varying envelope


def test_time_envelope_length_and_default_fs():
    # One path at 100 Hz Doppler over 8 steps; default fs = 2*max|f_d| = 200 Hz
    # so the window is 8/200 = 40 ms and spans well over one Doppler period.
    paths = _paths_with([0.0])
    times, env = ca.doppler_time_envelope(paths, [100.0], num_time_steps=8, sampling_frequency_hz=None)
    assert len(times) == 8
    assert len(env) == 8
    assert times[0] == 0.0
    assert times[1] == pytest.approx(1.0 / (2.0 * 100.0))
    # Single path: |a e^{jwt}| is constant, so the envelope is flat.
    assert all(e == pytest.approx(env[0], abs=1e-9) for e in env)


def test_time_envelope_two_path_fading_ripples():
    # Two equal paths with opposite Doppler beat against each other: the
    # coherent sum envelope must vary over the window (fast fading), not be flat.
    paths = _paths_with([0.0, 0.0])
    times, env = ca.doppler_time_envelope(
        paths, [50.0, -50.0], num_time_steps=16, sampling_frequency_hz=400.0
    )
    assert len(env) == 16
    assert max(env) - min(env) > 1.0  # a real ripple, not numerical noise


def test_time_envelope_empty_when_static_or_single_step():
    paths = _paths_with([0.0, 0.0])
    assert ca.doppler_time_envelope(paths, [10.0, -10.0], 1, None) == ([], [])
    assert ca.doppler_time_envelope(paths, None, 8, None) == ([], [])


def test_build_cir_fills_doppler_by_path_id():
    paths = _paths_with([0.0, -10.0])  # ids p0 (10 ns), p1 (20 ns)
    cir = ca.build_cir(paths, {"p0": 123.0, "p1": -45.0})
    # sorted by delay -> p0 then p1.
    assert cir[0].doppler_hz == pytest.approx(123.0)
    assert cir[1].doppler_hz == pytest.approx(-45.0)
    # Without the map, doppler stays None (backends that don't model it).
    assert all(t.doppler_hz is None for t in ca.build_cir(paths))


# ============================ service plumbing (capturing fake backend) ======
#
# These prove velocity reaches the solver without needing Sionna: a fake
# backend records the Device.velocity_m_s / actor_velocities it is handed and
# returns a fixed one-path result carrying a doppler_hz in metadata.


class _CaptureBackend(RayTracingBackend):
    name = "sionna"  # scenario routes sionna through the actor_velocities path

    def __init__(self):
        self.captured_device_velocities = []
        self.captured_actor_velocities = []

    def is_available(self) -> bool:
        return True

    def compile(self, project_dir, scene, library):  # type: ignore[override]
        from seam_studio.schemas.compile import CompileResult
        return CompileResult(ok=True)

    def simulate_paths(self, project_dir, scene, library, config, actor_states=None, actor_velocities=None):  # type: ignore[override]
        self.captured_actor_velocities.append(actor_velocities)
        self.captured_device_velocities.append(
            {d.id: d.velocity_m_s for d in scene.devices if d.velocity_m_s is not None}
        )
        rx = next((d for d in scene.devices if d.kind == "rx"), None)
        tx = next((d for d in scene.devices if d.kind == "tx"), None)
        paths = []
        metadata = {"frequency_hz": config.frequency_hz, "engine": "sionna"}
        if rx and tx:
            paths = [
                RayPath(
                    path_id="path_0001", tx_id=tx.id, rx_id=rx.id, path_type="los",
                    vertices=[list(tx.position), list(rx.position)],
                    power_dbm=-60.0, delay_ns=50.0,
                )
            ]
            metadata["doppler_hz"] = [321.0]
        return PathResultSet(
            result_id=UNSAVED_RESULT_ID, backend=self.name,
            simulation_config_id=config.id, paths=paths, warnings=[], metadata=metadata,
        )

    def simulate_radio_map(self, project_dir, scene, library, config):  # type: ignore[override]
        raise NotImplementedError


def _traj_scene() -> Scene:
    return Scene(
        scene_id="doppler_traj",
        name="Doppler Traj",
        prims=[
            Prim(
                id="/ground", name="ground", semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(material_id="ground", assignment_status="user_confirmed",
                             assignment_sources=["user"]),
            ),
        ],
        devices=[
            Device(id="tx_001", kind="tx", position=[0.0, 0.0, 10.0], power_dbm=30.0),
            Device(id="rx_001", kind="rx", position=[0.0, 0.0, 1.5]),
        ],
    )


def test_trajectory_plumbs_ue_velocity(tmp_path):
    backend = _CaptureBackend()
    scene = _traj_scene()
    config = SimulationConfig(id="default")
    # Straight line +x, 10 m per 0.5 s step -> 20 m/s along +x.
    request = TrajectorySimulateRequest(
        start_m=[0.0, 0.0, 1.5], end_m=[30.0, 0.0, 1.5], num_points=4, dt_s=0.5,
    )
    result = traj.run_trajectory(backend, tmp_path, scene, load_default_library(), config, request)

    # Every waypoint solve saw the moving RX with a +x velocity of 20 m/s
    # (except the final backward-difference point, still +20 m/s here).
    seen = [c.get("rx_001") for c in backend.captured_device_velocities]
    assert all(v is not None for v in seen), seen
    assert seen[0] == pytest.approx([20.0, 0.0, 0.0])
    assert seen[-1] == pytest.approx([20.0, 0.0, 0.0])
    # Doppler spread surfaced per waypoint in metadata (single path -> spread 0).
    spreads = result.metadata.get("doppler_spread_hz")
    assert spreads is not None and len(spreads) == len(result.samples)
    assert all(s == pytest.approx(0.0) for s in spreads)


def test_waypoint_velocity_finite_difference():
    wps = [[0.0, 0.0, 0.0], [5.0, 0.0, 0.0], [5.0, 10.0, 0.0]]
    # forward diff at 0: (5-0)/0.5 = 10 m/s +x.
    assert traj._waypoint_velocity(wps, 0, 0.5) == pytest.approx([10.0, 0.0, 0.0])
    # forward diff at 1: (0,10,0)/0.5 = 20 m/s +y.
    assert traj._waypoint_velocity(wps, 1, 0.5) == pytest.approx([0.0, 20.0, 0.0])
    # backward diff at the last point reuses the previous segment.
    assert traj._waypoint_velocity(wps, 2, 0.5) == pytest.approx([0.0, 20.0, 0.0])
    # single waypoint / degenerate dt -> zero.
    assert traj._waypoint_velocity([[1.0, 1.0, 1.0]], 0, 0.5) == [0.0, 0.0, 0.0]


def _scenario_scene() -> Scene:
    car = Actor(
        id="car_001", kind="car", position=[-30.0, 0.0, 0.0],
        trajectory=ActorTrajectory(
            waypoints=[[-30.0, 0.0, 0.0], [-10.0, 0.0, 0.0], [10.0, 0.0, 0.0], [30.0, 0.0, 0.0]],
            dt_s=0.5, loop=False,
        ),
    )
    return Scene(
        scene_id="doppler_scen", name="Doppler Scen",
        prims=[
            Prim(
                id="/ground", name="ground", semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(material_id="ground", assignment_status="user_confirmed",
                             assignment_sources=["user"]),
            ),
        ],
        devices=[
            Device(id="tx_001", kind="tx", position=[-40.0, 0.0, 8.0], power_dbm=30.0),
            Device(id="rx_001", kind="rx", position=[40.0, 0.0, 1.5]),
        ],
        actors=[car],
    )


def test_actor_velocity_at_tangent():
    scene = _scenario_scene()
    car = scene.actors[0]
    # Car covers 20 m per 0.5 s waypoint step -> 40 m/s along +x while moving.
    v = scen.actor_velocity_at(car, 0.25)
    assert v[0] == pytest.approx(40.0, abs=1e-3)
    assert v[1] == pytest.approx(0.0, abs=1e-6)


def test_scenario_plumbs_actor_velocity(tmp_path):
    backend = _CaptureBackend()
    scene = _scenario_scene()
    config = SimulationConfig(id="default")
    request = ScenarioSimulateRequest(num_frames=3, dt_s=0.5, include_paths=False)
    result = scen.run_scenario(backend, tmp_path, scene, load_default_library(), config, request)

    # The moving car's velocity reached the solve on the frames where it moves.
    moving = [av for av in backend.captured_actor_velocities if av and "car_001" in av]
    assert moving, backend.captured_actor_velocities
    vx = moving[0]["car_001"][0]
    assert vx == pytest.approx(40.0, abs=1.0)
    # Per-frame Doppler spread surfaced in metadata (single path -> 0).
    spreads = result.metadata.get("doppler_spread_hz")
    assert spreads is not None and len(spreads) == 3


# ============================================================ sionna-guarded


def _los_scene() -> Scene:
    """Tiny lab-room-like scene: a ground plane with a clear tx->rx LoS. The RX
    carries a velocity so a real solve yields a non-zero Doppler."""
    return Scene(
        scene_id="doppler_it", name="Doppler IT",
        prims=[
            Prim(
                id="/ground", name="ground", semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(material_id="ground", assignment_status="user_confirmed",
                             assignment_sources=["user"]),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=[-15.0, 0.0, 1.5], power_dbm=30.0),
            # RX approaches the TX along -x at 30 m/s (positive Doppler).
            Device(id="rx_001", name="RX", kind="rx", position=[15.0, 0.0, 1.5],
                   velocity_m_s=[-30.0, 0.0, 0.0]),
        ],
    )


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "doppler_it.sionnatwin"
    (proj / "visual").mkdir(parents=True)
    (proj / "rf").mkdir()
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(80.0, 80.0, 0.2))
    ground.apply_translation((0.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    (proj / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))
    return proj


@pytest.mark.skipif(not SIONNA_INSTALLED, reason="sionna-rt not installed (optional backend)")
def test_sionna_moving_rx_doppler_matches_v_over_lambda(project: Path):
    scene = _los_scene()
    library = load_default_library()
    freq = 3.5e9
    cfg = SimulationConfig(
        id="default", frequency_hz=freq, max_depth=0, num_samples=200_000,
        reflection=False, scattering=False, diffraction=False,
    )

    result = SionnaBackend().simulate_paths(project, scene, library, cfg)

    los = [p for p in result.paths if p.path_type == "los"]
    assert los, f"expected a LoS path; warnings={result.warnings}"
    doppler = result.metadata.get("doppler_hz")
    assert isinstance(doppler, list) and len(doppler) == len(result.paths), result.metadata
    # LoS Doppler for an RX closing at 30 m/s is f*v/c = v/lambda, sign positive.
    lam = C / freq
    expected = 30.0 / lam  # ~350.2 Hz at 3.5 GHz
    los_idx = result.paths.index(los[0])
    assert doppler[los_idx] == pytest.approx(expected, rel=0.02), doppler


@pytest.mark.skipif(not SIONNA_INSTALLED, reason="sionna-rt not installed (optional backend)")
def test_sionna_static_link_has_no_doppler_metadata(project: Path):
    scene = _los_scene()
    for d in scene.devices:
        d.velocity_m_s = None  # nothing moves
    cfg = SimulationConfig(
        id="default", frequency_hz=3.5e9, max_depth=0, num_samples=200_000,
        reflection=False, scattering=False, diffraction=False,
    )
    result = SionnaBackend().simulate_paths(project, scene, library=load_default_library(), config=cfg)
    # A fully static solve does not surface doppler_hz (byte-identical to before
    # this feature); channel analysis therefore reports Doppler metrics as None.
    assert "doppler_hz" not in result.metadata


@pytest.mark.skipif(not SIONNA_INSTALLED, reason="sionna-rt not installed (optional backend)")
def test_sionna_channel_analysis_reports_doppler(project: Path, tmp_path: Path):
    scene = _los_scene()
    library = load_default_library()
    request = ChannelAnalysisRequest(
        config=SimulationConfig(
            id="default", backend="sionna", frequency_hz=3.5e9, max_depth=0,
            num_samples=200_000, reflection=False, scattering=False, diffraction=False,
        ),
        num_time_steps=16,
    )
    res = ca.analyze_channel(project, scene, library, request)
    assert res.num_paths >= 1
    # Doppler metrics filled from the moving RX.
    assert res.max_doppler_hz is not None and res.max_doppler_hz > 0.0
    assert res.doppler_spread_hz is not None
    assert res.coherence_time_ms is not None and res.coherence_time_ms > 0.0
    # The strongest (LoS) tap carries a Doppler shift.
    assert any(t.doppler_hz is not None for t in res.cir)
    # Time-varying envelope emitted with 16 samples.
    assert len(res.cir_time_s) == 16
    assert len(res.cir_time_envelope_db) == 16
