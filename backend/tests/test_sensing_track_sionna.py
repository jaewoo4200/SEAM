"""Sensing over time on the real sionna-rt RCSSolver (sionna-rt >= 2.2).

Skipped without sionna-rt 2.2. When installed, a two-frame scenario with
sensing on pins what the mock cannot: every link of a moving constant-RCS
target returns a direct echo in frame mode (target at the frame pose with the
frame velocity), the fusion recovers the true track from Sionna's delays and
Doppler, the Doppler agrees with the mock frame by frame, the comm paths
(include_comm_paths) carry Sionna's Doppler, and the cached Sionna scene is
left clean after the run, the actors the frames moved going back on the next
snapshot solve.
"""

import math
from pathlib import Path

import numpy as np
import pytest

from seam_studio.schemas.actors import ActorState, ScenarioSimulateRequest
from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Actor, ActorTrajectory, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.sensing import SensingSimulateRequest
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.availability import sionna_available, sionna_rcs_available
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import run_scenario
from seam_studio.services.sensing import select_targets
from seam_studio.services.simulation_backends import sionna_backend
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.simulation_backends.sionna_backend import (
    SionnaBackend,
    _actor_object_key,
    _load_scene_cached,
)

from .test_sensing_sionna import _config as sensing_config
from .test_sensing_sionna import _hits, _write_ground
from .test_sensing_sionna import _scene as sensing_scene

pytestmark = pytest.mark.skipif(
    not (sionna_available() and sionna_rcs_available()),
    reason="sionna-rt >= 2.2 (sionna.rt.rcs) not installed",
)

FREQ = 3.5e9  # keeps the ITU ground (1-10 GHz) in band
SAMPLES = 100_000
TRPS = {
    "trp_a": (-40.0, -30.0, 15.0),
    "trp_b": (40.0, -25.0, 20.0),
    "trp_c": (0.0, 45.0, 25.0),
}
SENSING = {
    "enabled": True,
    "threshold_db": -30.0,
    "cpi_pulses": 100_000_000,
    "mti_min_doppler_hz": 0.0,
    "samples_per_sp": SAMPLES,
    "include_comm_paths": True,
}


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "isac_it.seam"
    _write_ground(proj)
    return proj


def _scene() -> Scene:
    devices = [
        Device(id=k, kind="tx", position=list(p), power_dbm=43.0) for k, p in TRPS.items()
    ]
    devices += [Device(id=f"{k}_rx", kind="rx", position=list(p)) for k, p in TRPS.items()]
    return Scene(
        scene_id="isac_it",
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
        devices=devices,
        actors=[
            Actor(
                id="tgt_01",
                kind="custom",
                position=[-5.0, 2.0, 30.0],
                trajectory=ActorTrajectory(
                    waypoints=[[-5.0, 2.0, 30.0], [25.0, 2.0, 30.0]], speed_m_s=10.0
                ),
                sensing={"model": "constant", "rcs_dbsm": 20.0},
            )
        ],
    )


def _config(backend: str) -> SimulationConfig:
    return SimulationConfig(
        backend=backend, frequency_hz=FREQ, bandwidth_hz=2e7, max_depth=2,
        reflection=True, num_samples=SAMPLES,
    )


def _run(backend, project: Path, config: SimulationConfig):
    request = ScenarioSimulateRequest(
        num_frames=2, dt_s=0.5, include_paths=False, sensing=SENSING
    )
    return run_scenario(backend, project, _scene(), load_default_library(), config, request)


def test_sionna_scenario_sensing_fuses_the_true_track(project: Path, monkeypatch):
    # The periodic full flush would drop the cached scene this test inspects.
    monkeypatch.setattr(sionna_backend, "_FULL_FLUSH_EVERY", 10**9)
    result = _run(SionnaBackend(), project, _config("sionna"))
    mock = _run(MockBackend(), project, _config("mock"))
    assert not [w for w in result.warnings if "failed" in w], result.warnings

    for i, (frame, mock_frame) in enumerate(zip(result.frames, mock.frames)):
        sensing = frame.sensing
        assert sensing is not None
        assert len(sensing.links) == 9
        for report, mock_report in zip(sensing.links, mock_frame.sensing.links):
            assert report.num_echoes >= 1 and not report.multipath, (i, report)
            assert report.detected, (i, report)
            assert report.doppler_hz == pytest.approx(mock_report.doppler_hz, abs=1.0)
            assert report.bistatic_range_m == pytest.approx(
                mock_report.bistatic_range_m, abs=1e-2
            )
        est = sensing.estimates[0]
        assert est.status == "ok", (i, est)
        assert est.n_links_used == 6  # 9 links, 3 reciprocal pairs
        assert est.position_error_m < 0.05, (i, est)
        assert est.velocity_error_m_s < 0.1, (i, est)
        # Truth is the frame pose: the 1 m custom box center, 10 m/s along +x.
        x = -5.0 + 10.0 * frame.time_s
        assert est.position_true == pytest.approx([x, 2.0, 30.5])
        assert est.velocity_true == pytest.approx([10.0, 0.0, 0.0])
        # Moving target, static nodes: the target leg carries all Doppler.
        assert any(abs(r.doppler_hz) > 10.0 for r in sensing.links)
        # The comm paths follow the echoes with Sionna's per-path Doppler.
        comm = [p for p in sensing.echoes if p.target_id is None]
        assert comm and all(p.doppler_hz is not None for p in comm), i
        # v0.1.13: the frame stores the TX/RX kinematics its fusion used.
        assert [n.id for n in sensing.nodes] == [*TRPS, *(f"{k}_rx" for k in TRPS)]
        assert all(n.position == pytest.approx(list(TRPS[n.id.removesuffix("_rx")]))
                   and n.velocity == [0.0, 0.0, 0.0] for n in sensing.nodes)

    # The frame-mode solves leave the cached scene as a plain load.
    rt_scene = _load_scene_cached(project / "rf" / "generated_scene.xml", [])
    assert rt_scene.sensing_targets == {}
    assert not [m for m in rt_scene.radio_materials if m.startswith("seam-st-")]
    assert not getattr(rt_scene, "_seam_hidden_actor_objects", [])
    assert any("tgt_01" in name for name in rt_scene.objects)
    summary = result.metadata["sensing"]["targets"]["tgt_01"]
    assert summary["ok_frames"] == 2 and summary["detection_rate"] == 1.0
    assert math.isclose(result.metadata["sensing"]["wavelength_m"], 299_792_458.0 / FREQ)

    # The frames leave tgt_01 at their last pose (x = 0) on the cached scene,
    # though its authored pose equals the baked one: a later snapshot solve
    # must put it back instead of tracing it where the run ended.
    def centroid():
        obj = rt_scene.objects[_actor_object_key(rt_scene, "tgt_01")]
        return [float(v) for v in np.array(obj.position).reshape(-1)[:3]]

    baked = rt_scene._actor_authored_pose["tgt_01"][0]
    assert centroid()[0] == pytest.approx(baked[0] + 5.0, abs=1e-3)
    SionnaBackend().simulate_paths(project, _scene(), load_default_library(), _config("sionna"))
    assert _load_scene_cached(project / "rf" / "generated_scene.xml", []) is rt_scene
    assert centroid() == pytest.approx(baked, abs=1e-3)


def test_sionna_frame_mode_moves_non_target_actors(project: Path):
    # Monostatic radar 1 m up, constant target at (20, 0) and a truck (car_02)
    # whose side face mirrors legs TX -> truck -> scattering point. The truck's
    # trajectory gives it a t = 0 velocity; in frame mode the caller's pose
    # and velocities win instead.
    scene = sensing_scene({"model": "constant", "rcs_dbsm": 10.0}, rx=(0.0, 0.0, 1.0))
    scene.devices[0].position = [0.0, 0.0, 1.0]
    scene.actors[0].position = [20.0, 0.0, 0.0]
    scene.actors.append(
        Actor(
            id="car_02", kind="car", position=[10.0, 8.0, 0.0],
            trajectory=ActorTrajectory(waypoints=[[10.0, 8.0, 0.0], [10.0, 18.0, 0.0]], dt_s=1.0),
        )
    )
    config = sensing_config(max_depth=2, reflection=True)
    backend = SionnaBackend()
    library = load_default_library()
    targets = select_targets(scene, None)
    request = SensingSimulateRequest(samples_per_sp=SAMPLES)

    def solve(truck_position, velocities):
        states = [
            ActorState(id="car_01", position=[20.0, 0.0, 0.0]),
            ActorState(id="car_02", position=truck_position),
        ]
        return backend.simulate_sensing(
            project, scene, library, config, request, targets,
            actor_states=states, actor_velocities=velocities,
        )

    snapshot = backend.simulate_sensing(project, scene, library, config, request, targets)
    assert any(abs(p.doppler_hz) > 1.0 for p in snapshot.paths if _hits([p], "actor-car_02"))

    at_rest = solve([10.0, 8.0, 0.0], {})
    assert _hits(at_rest.paths, "actor-car_02") > 0, at_rest.warnings
    assert all(abs(p.doppler_hz) < 1e-3 for p in at_rest.paths)

    moving = solve([10.0, 8.0, 0.0], {"car_02": [0.0, -10.0, 0.0]})
    off_truck = [p for p in moving.paths if _hits([p], "actor-car_02")]
    assert off_truck and all(abs(p.doppler_hz) > 1.0 for p in off_truck)

    # Off the x = 10 m line no truck face mirrors TX onto the target.
    gone = solve([60.0, 40.0, 0.0], {})
    assert _hits(gone.paths, "actor-car_02") == 0
    assert [p for p in gone.paths if p.target_id == "car_01"], gone.warnings
