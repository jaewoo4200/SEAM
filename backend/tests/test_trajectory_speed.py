"""ActorTrajectory.speed_m_s: constant-speed pacing next to the legacy dt_s.

With a speed, each segment takes length / speed, so the velocity no longer
scales with how far apart the waypoints were placed (a drone with waypoints
50 m apart and the default dt flew at ~200 m/s). Without one, every consumer
(scenario poses/velocities, sensing, datasets, AODT export, scene_hash) must
behave exactly as before the field existed.
"""

import json
import math
import re
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from seam_studio.api.simulate import _provenance_hashes, _sha256
from seam_studio.schemas.scene import Actor, ActorKind, ActorTrajectory, Scene
from seam_studio.services.aodt_export import _scatterer_rows
from seam_studio.services.scenario import (
    _actor_states_at,
    _velocities_at,
    actor_heading_at,
    actor_position_at,
    actor_velocity_at,
)
from seam_studio.services.sensing import resolve_target

# Unequal legs: 10 m along +x, then 30 m along +y (40 m in total).
L_PATH = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 30.0, 0.0]]


def _actor(traj: ActorTrajectory, kind: str = "uav", **kw) -> Actor:
    return Actor(id="a1", kind=kind, position=list(traj.waypoints[0]), trajectory=traj, **kw)


def _norm(v) -> float:
    return math.sqrt(sum(c * c for c in v))


# --------------------------------------------------------------------- schema


def test_schema_roundtrip_with_and_without_speed():
    paced = ActorTrajectory(waypoints=L_PATH, dt_s=0.251, speed_m_s=12.5)
    again = ActorTrajectory.model_validate(json.loads(paced.model_dump_json()))
    assert again == paced
    assert again.speed_m_s == 12.5 and again.dt_s == 0.251

    legacy = ActorTrajectory.model_validate({"waypoints": L_PATH, "dt_s": 0.5})
    assert legacy.speed_m_s is None
    assert ActorTrajectory.model_validate(legacy.model_dump()) == legacy


@pytest.mark.parametrize("bad", [0.0, -3.0])
def test_speed_must_be_positive(bad):
    with pytest.raises(ValidationError):
        ActorTrajectory(waypoints=L_PATH, speed_m_s=bad)


def test_duration_follows_the_pacing():
    assert ActorTrajectory(waypoints=L_PATH, dt_s=0.5).duration_s() == 1.0
    assert ActorTrajectory(waypoints=L_PATH, speed_m_s=8.0).duration_s() == 5.0
    assert ActorTrajectory(waypoints=L_PATH).segment_lengths_m() == [10.0, 30.0]


# ------------------------------------------------------------ velocity / poses


def test_velocity_is_speed_along_the_segment():
    # The reported trap: 50 m between waypoints at the editor's 0.251 s dt.
    traj = ActorTrajectory(waypoints=[[0, 0, 40], [30, 40, 40]], dt_s=0.251)
    drone = _actor(traj)
    assert _norm(actor_velocity_at(drone, 0.0)) == pytest.approx(50.0 / 0.251)

    drone.trajectory.speed_m_s = 10.0
    for t in (0.0, 1.0, 2.5, 4.9):
        v = actor_velocity_at(drone, t)
        assert v == pytest.approx([6.0, 8.0, 0.0])  # unit (0.6, 0.8, 0) x 10
        assert _norm(v) == pytest.approx(10.0)
    assert actor_position_at(drone, 5.0) == pytest.approx([30.0, 40.0, 40.0])


def test_unequal_segments_get_per_segment_time():
    actor = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0))
    # 1 s on the 10 m leg, 3 s on the 30 m leg.
    assert actor_position_at(actor, 0.5) == pytest.approx([5.0, 0.0, 0.0])
    assert actor_position_at(actor, 1.0) == pytest.approx([10.0, 0.0, 0.0])
    assert actor_position_at(actor, 2.5) == pytest.approx([10.0, 15.0, 0.0])
    assert actor_position_at(actor, 4.0) == pytest.approx([10.0, 30.0, 0.0])
    assert actor_position_at(actor, 9.0) == pytest.approx([10.0, 30.0, 0.0])  # once
    assert actor_velocity_at(actor, 0.5) == pytest.approx([10.0, 0.0, 0.0])
    assert actor_velocity_at(actor, 2.5) == pytest.approx([0.0, 10.0, 0.0])
    assert actor_heading_at(actor, 2.5) == pytest.approx(90.0)
    assert actor_velocity_at(actor, 9.0) == pytest.approx([0.0, 0.0, 0.0])

    # dt pacing on the same path: 1 s per leg however long -> 30 m/s on leg 2.
    actor.trajectory.speed_m_s = None
    actor.trajectory.dt_s = 1.0
    assert actor_position_at(actor, 1.5) == pytest.approx([10.0, 15.0, 0.0])
    assert actor_velocity_at(actor, 1.5) == pytest.approx([0.0, 30.0, 0.0])


def test_loop_and_pingpong_fold_over_the_path_length():
    actor = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0, mode="loop"))
    # 45 m on a 40 m path wraps to 5 m: back on the first leg.
    assert actor_position_at(actor, 4.5) == pytest.approx([5.0, 0.0, 0.0])

    actor.trajectory.mode = "pingpong"
    # 50 m on a 40 m path reflects to 30 m: on the way back along leg 2.
    assert actor_position_at(actor, 5.0) == pytest.approx([10.0, 20.0, 0.0])
    assert actor_velocity_at(actor, 5.0) == pytest.approx([0.0, -10.0, 0.0])


@pytest.mark.parametrize("pacing", [{"speed_m_s": 10.0}, {"dt_s": 1.0}])
def test_loop_wrap_velocity_is_one_sided(pacing):
    # A loop jumps from the last waypoint back to the first: a central
    # difference across that jump would be a ~10 km/s spike.
    actor = _actor(ActorTrajectory(waypoints=L_PATH, mode="loop", **pacing))
    period = 4.0 if "speed_m_s" in pacing else 2.0
    leg1 = [10.0, 0.0, 0.0]  # 10 m in 1 s either way
    leg2 = [0.0, 10.0, 0.0] if "speed_m_s" in pacing else [0.0, 30.0, 0.0]
    assert actor_position_at(actor, period) == pytest.approx([0.0, 0.0, 0.0])
    assert actor_velocity_at(actor, period) == pytest.approx(leg1)  # on the wrap: first leg
    assert actor_velocity_at(actor, 2 * period) == pytest.approx(leg1)
    assert actor_velocity_at(actor, period - 5e-4) == pytest.approx(leg2)  # just before it
    assert actor_velocity_at(actor, period + 5e-4) == pytest.approx(leg1)  # just after it


def test_degenerate_segments():
    # A repeated waypoint is crossed instantly.
    dup = _actor(ActorTrajectory(waypoints=[[0, 0, 0], [0, 0, 0], [10, 0, 0]], speed_m_s=5.0))
    assert actor_position_at(dup, 1.0) == pytest.approx([5.0, 0.0, 0.0])
    assert actor_velocity_at(dup, 1.0) == pytest.approx([5.0, 0.0, 0.0])
    # All waypoints coincident: the actor stays put and is at rest.
    still = _actor(ActorTrajectory(waypoints=[[3, 4, 5], [3, 4, 5]], speed_m_s=5.0))
    assert actor_position_at(still, 2.0) == [3.0, 4.0, 5.0]
    assert actor_velocity_at(still, 2.0) == [0.0, 0.0, 0.0]


def _legacy_param(traj: ActorTrajectory, time_s: float) -> float:
    """The waypoint parameter as computed before speed_m_s existed."""
    n = len(traj.waypoints)
    if n <= 1:
        return 0.0
    s = time_s / traj.dt_s
    mode = traj.resolved_mode()
    span = float(n - 1)
    if mode == "once":
        return max(0.0, min(s, span))
    if mode == "loop":
        return s % span
    period = 2.0 * span
    phase = s % period
    return phase if phase <= span else period - phase


@pytest.mark.parametrize("mode", [None, "once", "loop", "pingpong"])
def test_without_speed_positions_are_bit_identical_to_dt_pacing(mode):
    traj = ActorTrajectory(
        waypoints=[[0, 0, 0], [3, 1, 0], [3, 9, 2], [-4, 9, 2]], dt_s=0.37, mode=mode,
    )
    actor = _actor(traj, kind="car")
    for k in range(80):
        t = k * 0.0731
        s = _legacy_param(traj, t)
        i = max(0, min(int(math.floor(s)), len(traj.waypoints) - 2))
        a, b = traj.waypoints[i], traj.waypoints[i + 1]
        expected = [float(a[c]) + (float(b[c]) - float(a[c])) * (s - i) for c in range(3)]
        assert actor_position_at(actor, t) == expected  # exact, not approx


# ------------------------------------------------- scenario / sensing / export


def test_scenario_frames_and_velocities_honor_speed():
    actor = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0), kind="car")
    scene = Scene(scene_id="speed_scene", actors=[actor])
    states = _actor_states_at(scene, 2.5)
    assert states[0].position == pytest.approx([10.0, 15.0, 0.0])
    assert states[0].orientation_deg[0] == pytest.approx(90.0)
    actor_v, _ = _velocities_at(scene, 2.5)
    assert actor_v["a1"] == pytest.approx([0.0, 10.0, 0.0])


def test_sensing_target_velocity_uses_speed():
    traj = ActorTrajectory(waypoints=[[0, 0, 40], [30, 40, 40]], dt_s=0.251, speed_m_s=10.0)
    drone = _actor(traj, sensing={"model": "tr38901"})
    assert resolve_target(drone).velocity_m_s == pytest.approx((6.0, 8.0, 0.0))


def test_aodt_scatterer_route_times_follow_speed():
    actor = _actor(ActorTrajectory(waypoints=L_PATH, dt_s=0.5, speed_m_s=10.0), kind="car")
    row = _scatterer_rows(Scene(scene_id="s", actors=[actor]), {"a1": 0})[0]
    assert row["route_times"][0] == pytest.approx([0.0, 1.0, 4.0])
    assert row["route_speeds"][0] == pytest.approx([10.0, 10.0, 10.0])

    actor.trajectory.speed_m_s = None  # legacy: dt per step, leg length / dt
    row = _scatterer_rows(Scene(scene_id="s", actors=[actor]), {"a1": 0})[0]
    assert row["route_times"][0] == pytest.approx([0.0, 0.5, 1.0])
    assert row["route_speeds"][0] == pytest.approx([20.0, 60.0, 60.0])


@pytest.mark.parametrize(
    "waypoints",
    [[[3, 4, 5]], [[3, 4, 5], [3, 4, 5]]],
    ids=["single_waypoint", "coincident_pair"],
)
def test_aodt_speed_paced_actor_that_never_moves_is_at_rest(waypoints):
    actor = _actor(ActorTrajectory(waypoints=waypoints, speed_m_s=10.0), kind="car")
    row = _scatterer_rows(Scene(scene_id="s", actors=[actor]), {"a1": 0})[0]
    assert row["route_positions"][0] == [[3.0, 4.0, 5.0]]
    assert row["route_times"][0] == [0.0]
    assert row["route_speeds"][0] == [0.0]
    assert actor_velocity_at(actor, 0.0) == [0.0, 0.0, 0.0]


def test_aodt_speed_route_drops_repeated_waypoints():
    wps = [[0, 0, 0], [0, 0, 0], [10, 0, 0], [10, 0, 0], [10, 30, 0]]
    actor = _actor(ActorTrajectory(waypoints=wps, speed_m_s=10.0), kind="car")
    row = _scatterer_rows(Scene(scene_id="s", actors=[actor]), {"a1": 0})[0]
    assert row["route_positions"][0] == [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 30.0, 0.0]]
    assert row["route_times"][0] == pytest.approx([0.0, 1.0, 4.0])
    assert row["route_speeds"][0] == pytest.approx([10.0, 10.0, 10.0])
    assert len(row["route_orientations"][0]) == 3


# ----------------------------------------------------------------- scene hash


def test_dt_paced_trajectory_keeps_its_scene_hash():
    actor = _actor(ActorTrajectory(waypoints=L_PATH, dt_s=0.5), kind="car")
    scene = Scene(scene_id="hash_scene", actors=[actor])
    payload = scene.model_dump(mode="json")
    for key in ("result_sets", "revision"):
        payload.pop(key)
    for a in payload["actors"]:
        del a["sensing"]
        del a["trajectory"]["speed_m_s"]  # the pre-speed trajectory layout
    assert _provenance_hashes(scene, None)["scene_hash"] == _sha256(payload)

    paced = scene.model_copy(deep=True)
    paced.actors[0].trajectory.speed_m_s = 10.0
    assert _provenance_hashes(paced, None)["scene_hash"] != _sha256(payload)


# ------------------------------------------------- frontend placeholder speeds


def test_frontend_speed_placeholders_cover_every_actor_kind():
    src_file = Path(__file__).resolve().parents[2] / "frontend" / "src" / "actorDefaults.ts"
    if not src_file.is_file():
        pytest.skip("frontend sources not present")
    src = src_file.read_text(encoding="utf-8")
    block = re.search(r"DEFAULT_SPEED_M_S_BY_KIND[^=]*=\s*\{(.*?)\};", src, re.S)
    assert block, "DEFAULT_SPEED_M_S_BY_KIND not found in actorDefaults.ts"
    speeds = {k: float(v) for k, v in re.findall(r"(\w+):\s*([0-9.]+)", block.group(1))}
    assert set(speeds) == set(get_args(ActorKind))
    assert speeds == {"car": 10.0, "human": 1.4, "uav": 10.0, "custom": 5.0}


# ------------------------------------- velocity at leg boundaries (v0.1.14)


def test_velocity_at_waypoint_is_outgoing_leg():
    # At an exact waypoint time the actor is on the next leg: its velocity,
    # not the chord average of both (7.07 m/s at a 90 deg turn before v0.1.14).
    actor = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0))
    assert actor_velocity_at(actor, 1.0) == pytest.approx([0.0, 10.0, 0.0])
    actor.trajectory.speed_m_s = None
    actor.trajectory.dt_s = 1.0
    assert actor_velocity_at(actor, 1.0) == pytest.approx([0.0, 30.0, 0.0])


def test_velocity_at_once_end_is_zero():
    # A once trajectory stops at its last waypoint: at rest there, not half speed.
    actor = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0), kind="car")
    assert actor_velocity_at(actor, 4.0) == [0.0, 0.0, 0.0]
    actor_v, _ = _velocities_at(Scene(scene_id="s", actors=[actor]), 4.0)
    assert actor_v == {}


def test_pingpong_turnaround_takes_reversed_leg():
    # At the turnaround the actor heads back along the last leg.
    actor = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0, mode="pingpong"))
    assert actor_velocity_at(actor, 4.0) == pytest.approx([0.0, -10.0, 0.0])
    assert actor_velocity_at(actor, 7.0) == pytest.approx([-10.0, 0.0, 0.0])  # past the corner again


def test_velocity_just_before_knot_is_incoming():
    # Within eps before a waypoint the backward difference stays on the incoming leg.
    actor = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0))
    assert actor_velocity_at(actor, 1.0 - 5e-4) == pytest.approx([10.0, 0.0, 0.0])
    assert actor_velocity_at(actor, 1.0 + 5e-4) == pytest.approx([0.0, 10.0, 0.0])


def test_scenario_waypoint_frames_report_leg_velocity_and_doppler():
    # The sensing demo's UAV at dt 0.5 s hits the corner at frame 24 (t = 12 s)
    # and stops at frame 36 (t = 18 s): velocity_true and the solver's Doppler
    # follow the outgoing leg and the stop.
    from seam_studio.services.simulation_backends.mock_backend import SPEED_OF_LIGHT
    from .test_sensing_track import _config, _scenario, _trp_scene

    result = _scenario(_trp_scene(), _config(), num_frames=37, cpi_pulses=4096)
    corner, end = result.frames[24], result.frames[36]
    assert corner.time_s == 12.0 and end.time_s == 18.0
    assert corner.sensing.estimates[0].velocity_true == pytest.approx([0.0, 10.0, 0.0])
    assert end.sensing.estimates[0].velocity_true == [0.0, 0.0, 0.0]

    lam = SPEED_OF_LIGHT / _config().frequency_hz
    p = corner.sensing.estimates[0].position_true
    nodes = {n.id: n.position for n in corner.sensing.nodes}
    link = next(r for r in corner.sensing.links if r.doppler_hz is not None)
    k_ts = [(p[i] - nodes[link.tx_id][i]) / math.dist(p, nodes[link.tx_id]) for i in range(3)]
    k_sr = [(nodes[link.rx_id][i] - p[i]) / math.dist(p, nodes[link.rx_id]) for i in range(3)]
    expected = 10.0 * (k_sr[1] - k_ts[1]) / lam
    assert link.doppler_hz == pytest.approx(expected, abs=1e-6)


@pytest.mark.parametrize("mode", ["loop", "pingpong"])
def test_velocity_on_a_near_zero_loop_is_bounded(mode):
    # Two waypoints a float-noise apart (a GUI drag): the +-eps window spans
    # ~1e7 periods, which the boundary search used to enumerate one by one.
    import time

    tiny = [[0.0, 0.0, 0.0], [1e-9, 0.0, 0.0]]
    actor = _actor(ActorTrajectory(waypoints=tiny, speed_m_s=10.0, mode=mode))
    started = time.perf_counter()
    v = actor_velocity_at(actor, 7.3)
    assert time.perf_counter() - started < 0.5
    assert all(math.isfinite(c) for c in v) and _norm(v) < 1e-3
    # Normal paths keep the one-sided boundary rule.
    normal = _actor(ActorTrajectory(waypoints=L_PATH, speed_m_s=10.0, mode=mode))
    assert actor_velocity_at(normal, 1.0) == pytest.approx([0.0, 10.0, 0.0])
