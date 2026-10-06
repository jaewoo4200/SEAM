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
