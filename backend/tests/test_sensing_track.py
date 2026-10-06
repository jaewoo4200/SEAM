"""Sensing over time (ISAC): per-frame sensing solves inside a scenario, link
detection (SNR, MTI), multistatic fusion, and the scenario API with
``sensing`` on.

Mock backend and pure math throughout, so every number is analytic; the
Sionna frame-mode solve is exercised by test_sensing_track_sionna.py.
"""

import itertools
import math
import random
import statistics
from pathlib import Path

import numpy as np
import pytest

from seam_studio.schemas.actors import (
    ActorState,
    ScenarioFrame,
    ScenarioResultSet,
    ScenarioSimulateRequest,
)
from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import (
    PathInteraction,
    RayPath,
    SensingFrame,
    SensingLinkReport,
    TargetEstimate,
)
from seam_studio.schemas.scene import Actor, ActorTrajectory, Scene
from seam_studio.schemas.sensing import SensingSimulateRequest, SensingTrackOptions
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import sensing_track
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import run_scenario
from seam_studio.services.sensing import resolve_target_state
from seam_studio.services.sensing_track import (
    SPEED_OF_LIGHT,
    FusionResult,
    SensingTracker,
    detection_constants,
    estimate_target,
    link_report,
    solve_bistatic_position,
    solve_doppler_velocity,
)
from seam_studio.services.simulation_backends.mock_backend import MockBackend

C = SPEED_OF_LIGHT
F1 = 3.5e9
TRPS = {
    "trp1": (82.75, -57.74, 20.84),
    "trp3": (78.64, 87.18, 19.51),
    "trp4": (-50.5, 84.57, 18.62),
    "trp6": (-4.17, -25.43, 24.37),
}
P_TRUE = (30.0, 30.0, 60.125)
MOCK_CFG = {
    "id": "default", "backend": "mock", "frequency_hz": F1,
    "bandwidth_hz": 2e7, "noise_figure_db": 7.0,
}
PID = "isac_api"


def _config(**overrides) -> SimulationConfig:
    return SimulationConfig(**{**MOCK_CFG, **overrides})


def _uav(**sensing) -> Actor:
    return Actor(
        id="uav_01",
        kind="uav",
        position=[-30.0, 0.0, 60.0],
        trajectory=ActorTrajectory(
            waypoints=[[-30.0, 0.0, 60.0], [90.0, 0.0, 60.0], [90.0, 60.0, 60.0]],
            speed_m_s=10.0,
            mode="once",
        ),
        sensing={"model": "tr38901", "object_type": "uav-small-size", **sensing},
    )


def _trp_scene(actors=None, scene_id: str = "isac", with_rx: bool = True) -> Scene:
    devices = [
        Device(id=k, kind="tx", position=list(p), power_dbm=43.0) for k, p in TRPS.items()
    ]
    if with_rx:
        devices += [Device(id=f"{k}_rx", kind="rx", position=list(p)) for k, p in TRPS.items()]
    return Scene(
        scene_id=scene_id, devices=devices, actors=[_uav()] if actors is None else actors
    )


def _direct(power_dbm: float, doppler_hz: float, *, path_id="sensing_0001", tx="a", rx="b",
            target="uav_01", delay_ns: float = 1000.0) -> RayPath:
    point = [0.0, 0.0, 50.0]
    return RayPath(
        path_id=path_id, tx_id=tx, rx_id=rx, path_type="sensing",
        vertices=[[0.0, 0.0, 0.0], point, [1.0, 0.0, 0.0]],
        power_dbm=power_dbm, delay_ns=delay_ns,
        interactions=[PathInteraction(type="sensing", prim_id=target, point=point)],
        doppler_hz=doppler_hz, target_id=target,
    )


def _via_wall(power_dbm: float, doppler_hz: float, **kw) -> RayPath:
    path = _direct(power_dbm, doppler_hz, **kw)
    wall = PathInteraction(type="reflection", prim_id="/wall", point=[5.0, 0.0, 10.0])
    return path.model_copy(update={"interactions": [wall, *path.interactions]})


def _ranges(p, links) -> list[float]:
    return [math.dist(p, t) + math.dist(p, s) for t, s in links]


def _scenario(scene: Scene, config: SimulationConfig, num_frames: int = 5, **sensing):
    request = ScenarioSimulateRequest(
        num_frames=num_frames, dt_s=0.5, include_paths=False,
        sensing={"enabled": True, **sensing},
    )
    return run_scenario(MockBackend(), Path("."), scene, load_default_library(), config, request)


# ------------------------------------------------------------ detection


def test_noise_floor_and_snr_known_numbers():
    consts = detection_constants(_config(), SensingTrackOptions(cpi_pulses=100))
    assert consts["noise_floor_dbm"] == pytest.approx(-93.98970004336019, abs=1e-12)
    assert consts["integration_gain_db"] == pytest.approx(20.0, abs=1e-12)
    echo = [_direct(-100.0, 500.0)]

    passed = link_report("a", "b", "uav_01", echo, consts, SensingTrackOptions(cpi_pulses=100), 0)
    assert passed.snr_db == pytest.approx(13.98970004336019, abs=1e-9)
    assert passed.detected and passed.reason == "detected"
    strict = SensingTrackOptions(cpi_pulses=100, threshold_db=14.0)
    failed = link_report("a", "b", "uav_01", echo, detection_constants(_config(), strict), strict, 0)
    assert not failed.detected and failed.reason == "below_threshold"

    big = detection_constants(_config(), SensingTrackOptions(cpi_pulses=4096))
    assert big["integration_gain_db"] == pytest.approx(36.12359947967774, abs=1e-12)
    assert big["bistatic_range_resolution_m"] == pytest.approx(C / 2e7)
    assert big["range_resolution_m"] == pytest.approx(C / 4e7)
    assert big["velocity_resolution_m_s"] == pytest.approx((C / F1) / 0.02)
    # Range-sum cell and Doppler cell of the reported echo.
    assert passed.bistatic_range_m == pytest.approx(C * 1e-6)
    assert passed.range_bin == math.floor(C * 1e-6 / (C / 2e7)) == 20
    assert passed.doppler_bin == 5


def test_mti_rejects_zero_doppler_echo():
    opts = SensingTrackOptions(cpi_pulses=1000)
    consts = detection_constants(_config(), opts)
    static = [_direct(-40.0, 0.0)]
    assert link_report("a", "b", "uav_01", static, consts, opts, 0).reason == "mti_rejected"

    off = SensingTrackOptions(cpi_pulses=1000, mti_min_doppler_hz=0.0)
    report = link_report("a", "b", "uav_01", static, detection_constants(_config(), off), off, 0)
    assert report.detected and report.reason == "detected"

    slow = SensingTrackOptions(cpi_s=0.02)  # default notch 1 / cpi_s = 50 Hz
    consts = detection_constants(_config(), slow)
    assert consts["mti_min_doppler_hz"] == pytest.approx(50.0)
    assert consts["mti_blind_speed_m_s"] == pytest.approx((C / F1) * 50.0 / 2.0)
    below = link_report("a", "b", "uav_01", [_direct(-40.0, 49.9)], consts, slow, 0)
    above = link_report("a", "b", "uav_01", [_direct(-40.0, -50.1)], consts, slow, 0)
    assert below.reason == "mti_rejected" and above.reason == "detected"


def test_echo_selection_direct_vs_multipath():
    opts = SensingTrackOptions(cpi_pulses=1000)
    consts = detection_constants(_config(), opts)
    weak_direct = _direct(-80.0, 300.0, path_id="sensing_0001")
    strong_bounce = _via_wall(-60.0, 250.0, path_id="sensing_0002", delay_ns=1500.0)
    other_target = _direct(-20.0, 300.0, path_id="sensing_0003", target="car_01")
    report = link_report(
        "a", "b", "uav_01", [weak_direct, strong_bounce, other_target], consts, opts, 0
    )
    assert report.path_id == "sensing_0001"
    assert report.multipath is False and report.num_echoes == 2
    assert report.echo_power_dbm == -80.0

    only_bounce = link_report("a", "b", "uav_01", [strong_bounce], consts, opts, 0)
    assert only_bounce.multipath is True and only_bounce.detected
    assert only_bounce.path_id == "sensing_0002"

    none = link_report("a", "b", "uav_01", [other_target], consts, opts, 0)
    assert none.reason == "no_echo" and not none.detected
    assert none.path_id is None and none.snr_db is None and none.range_bin is None

    # A detected multipath report never enters the fusion.
    nodes = {k: (list(p), [0.0, 0.0, 0.0]) for k, p in TRPS.items()}
    reports = [
        SensingLinkReport(
            tx_id=k, rx_id=k, target_id="uav_01", detected=True, reason="detected",
            multipath=True, snr_db=30.0, measured_range_m=2 * math.dist(p, P_TRUE),
            measured_doppler_hz=100.0,
        )
        for k, p in TRPS.items()
    ]
    est = estimate_target(_target_at(P_TRUE), reports, nodes, consts, None, 0.0)
    assert est.status == "insufficient_links"
    assert est.n_links_detected == 4 and est.n_links_used == 0


# --------------------------------------------------------------- fusion


def _target_at(center, velocity=(0.0, 0.0, 0.0)):
    actor = _uav()
    base = [center[0], center[1], center[2] - 0.125]  # uav box 0.25 m tall
    return resolve_target_state(
        actor, ActorState(id="uav_01", position=base), list(velocity)
    )


def test_fusion_exact_three_links():
    t = [TRPS["trp1"], TRPS["trp3"], TRPS["trp4"]]
    fusion = solve_bistatic_position(t, t, _ranges(P_TRUE, zip(t, t)))
    assert fusion.status == "ok"
    assert math.dist(fusion.position, P_TRUE) < 1e-3
    assert fusion.gdop is not None and fusion.rms_residual_m < 1e-6


def test_fusion_exact_four_links():
    links = [
        (TRPS["trp1"], TRPS["trp3"]),
        (TRPS["trp3"], TRPS["trp4"]),
        (TRPS["trp4"], TRPS["trp6"]),
        (TRPS["trp6"], TRPS["trp6"]),
    ]
    tx, rx = zip(*links)
    fusion = solve_bistatic_position(tx, rx, _ranges(P_TRUE, links))
    assert fusion.status == "ok"
    assert math.dist(fusion.position, P_TRUE) < 1e-3
    assert fusion.iterations > 0


def test_fusion_noisy_ranges_bounded():
    links = list(itertools.combinations_with_replacement(TRPS.values(), 2))
    assert len(links) == 10
    tx, rx = zip(*links)
    exact = _ranges(P_TRUE, links)
    errors = []
    for k in range(50):
        rng = random.Random(f"t:{k}")
        noisy = [r + rng.gauss(0.0, 0.5) for r in exact]
        fusion = solve_bistatic_position(tx, rx, noisy, max_rms_residual_m=C / 2e7)
        assert fusion.status == "ok", k
        errors.append(math.dist(fusion.position, P_TRUE))
    assert max(errors) < 3.0
    assert statistics.median(errors) < 1.0


def test_fusion_insufficient_links():
    consts = detection_constants(_config(), SensingTrackOptions())
    nodes = {k: (list(p), [0.0, 0.0, 0.0]) for k, p in TRPS.items()}
    nodes.update({f"{k}_rx": (list(p), [0.0, 0.0, 0.0]) for k, p in TRPS.items()})

    def report(tx, rx, snr):
        r = math.dist(P_TRUE, TRPS[tx]) + math.dist(P_TRUE, TRPS[rx])
        return SensingLinkReport(
            tx_id=tx, rx_id=f"{rx}_rx", target_id="uav_01", detected=True,
            reason="detected", snr_db=snr, measured_range_m=r, measured_doppler_hz=150.0,
        )

    # trp1->trp3 and trp3->trp1 are the same ellipsoid (reciprocal pair).
    reports = [report("trp1", "trp3", 20.0), report("trp3", "trp1", 25.0), report("trp4", "trp4", 18.0)]
    est = estimate_target(_target_at(P_TRUE), reports, nodes, consts, None, 0.0)
    assert est.status == "insufficient_links"
    assert est.n_links_detected == 3 and est.n_links_used == 2
    assert est.links_used == ["trp3>trp1_rx", "trp4>trp4_rx"]  # highest SNR first
    assert est.position_est is None and est.position_error_m is None
    assert est.position_true == pytest.approx(list(P_TRUE))
    assert solve_bistatic_position([TRPS["trp1"]] * 2, [TRPS["trp3"]] * 2, [200.0, 200.0]).status == (
        "insufficient_links"
    )


def test_fusion_no_converged_candidate_diverges():
    # Range sums far below the foci spacing: no ellipsoid exists, so no
    # Gauss-Newton start converges and there is no position at all.
    tx = [(0.0, 0.0, 10.0), (100.0, 0.0, 12.0), (0.0, 100.0, 14.0)]
    rx = [(100.0, 0.0, 12.0), (0.0, 100.0, 14.0), (0.0, 0.0, 10.0)]
    fusion = solve_bistatic_position(tx, rx, [1.0, 1.0, 1.0], max_rms_residual_m=15.0)
    assert fusion.status == "diverged" and fusion.position is None


def test_fusion_rms_residual_gate():
    # Converges, but one range sum is 100 m off: the rms residual exceeds one
    # range-sum cell (the gate estimate_target uses), so the fit is diverged.
    links = list(itertools.combinations_with_replacement(TRPS.values(), 2))
    tx, rx = zip(*links)
    ranges = _ranges(P_TRUE, links)
    ranges[0] += 100.0
    gated = solve_bistatic_position(tx, rx, ranges, max_rms_residual_m=15.0)
    free = solve_bistatic_position(tx, rx, ranges, max_rms_residual_m=math.inf)
    assert gated.status == "diverged" and gated.position is not None
    assert gated.rms_residual_m > 15.0
    assert free.status == "ok" and free.position == pytest.approx(gated.position)


def test_fusion_three_links_ambiguous_without_prior():
    # Three range sums: every root fits exactly, so the cost cannot choose.
    # Here a second root above ground (~50 m off) is also the highest, so
    # without a prior the fit is ambiguous (diverged); a prior picks exactly.
    p_true = (73.0, 19.0, 78.0)
    links = [
        (TRPS["trp1"], TRPS["trp4"]),
        (TRPS["trp3"], TRPS["trp6"]),
        (TRPS["trp3"], TRPS["trp3"]),
    ]
    tx, rx = zip(*links)
    ranges = _ranges(p_true, links)
    blind = solve_bistatic_position(tx, rx, ranges, max_rms_residual_m=15.0)
    assert blind.status == "diverged" and blind.rms_residual_m < 1e-6
    assert math.dist(blind.position, p_true) > 10.0
    seeded = solve_bistatic_position(
        tx, rx, ranges, max_rms_residual_m=15.0, prior=[75.0, 19.0, 78.0]
    )
    assert seeded.status == "ok" and math.dist(seeded.position, p_true) < 1e-6

    # Two starts used to miss this one's true root (122 m off, reported ok);
    # the link-midpoint starts find it and the other roots are underground.
    p_true = (-7.3, 48.9, 77.0)
    links = [
        (TRPS["trp1"], TRPS["trp3"]),
        (TRPS["trp1"], TRPS["trp4"]),
        (TRPS["trp4"], TRPS["trp6"]),
    ]
    tx, rx = zip(*links)
    found = solve_bistatic_position(tx, rx, _ranges(p_true, links), max_rms_residual_m=15.0)
    assert found.status == "ok" and math.dist(found.position, p_true) < 1e-6


def test_fusion_merges_sites_within_one_metre():
    # A sensing RX 0.6 m from its TX is the same site (the ISAC co-location
    # rule, shared with the coverage map), so trp1 -> trp3_rx and trp3 ->
    # trp1_rx are one ellipsoid. The v0.1.12 rule (0.5 m, pairwise) kept both.
    consts = detection_constants(_config(), SensingTrackOptions())
    off = np.array([0.6, 0.0, 0.0])
    nodes = {k: (list(p), [0.0, 0.0, 0.0]) for k, p in TRPS.items()}
    nodes.update({f"{k}_rx": (list(np.array(p) + off), [0.0, 0.0, 0.0]) for k, p in TRPS.items()})

    def report(tx, rx, snr):
        r = math.dist(P_TRUE, nodes[tx][0]) + math.dist(P_TRUE, nodes[f"{rx}_rx"][0])
        return SensingLinkReport(
            tx_id=tx, rx_id=f"{rx}_rx", target_id="uav_01", detected=True,
            reason="detected", snr_db=snr, measured_range_m=r, measured_doppler_hz=150.0,
        )

    reports = [report("trp1", "trp3", 20.0), report("trp3", "trp1", 25.0),
               report("trp4", "trp4", 18.0), report("trp6", "trp6", 17.0)]
    est = estimate_target(_target_at(P_TRUE), reports, nodes, consts, None, 0.0)
    assert est.n_links_detected == 4 and est.n_links_used == 3
    assert est.links_used == ["trp3>trp1_rx", "trp4>trp4_rx", "trp6>trp6_rx"]
    assert est.status == "ok" and est.position_error_m < 0.05


def test_fusion_two_nodes_is_degenerate():
    # Three links over only two nodes: every ellipsoid is symmetric about the
    # node axis, so the solutions form a circle and the Jacobian is rank 2.
    a, b = TRPS["trp4"], TRPS["trp6"]
    links = [(a, a), (a, b), (b, b)]
    tx, rx = zip(*links)
    fusion = solve_bistatic_position(tx, rx, _ranges(P_TRUE, links), max_rms_residual_m=15.0)
    assert fusion.status == "diverged"


def test_fusion_noisy_mirror_within_noise_prefers_above_plane():
    # Noisy 28 GHz ranges (demo frame, SNR ~13-22 dB) of a drone at (0, 0, 60.125)
    # seen by four links into trp6: the mirror below the near-coplanar TRPs fits
    # better by chance (rms 0.28 m vs 1.40 m), yet both are well inside the
    # range noise, so the above-plane solution must win.
    p_true = (0.0, 0.0, 60.125)
    links = [("trp6", 87.06, 22.15), ("trp4", 147.52, 14.46),
             ("trp1", 149.12, 14.35), ("trp3", 167.73, 13.15)]
    tx = [TRPS[k] for k, _, _ in links]
    rx = [TRPS["trp6"]] * len(links)
    ranges = [r for _, r, _ in links]

    exact_tie_only = solve_bistatic_position(tx, rx, ranges, max_rms_residual_m=15.0)
    assert exact_tie_only.position[2] < 0.0  # the mirror: why the noise tie exists
    mirror = exact_tie_only.position

    fusion = solve_bistatic_position(tx, rx, ranges, max_rms_residual_m=15.0, range_sigma_m=2.0)
    assert fusion.status == "ok" and fusion.position[2] > 40.0
    assert math.dist(fusion.position, p_true) < 3.0

    # A prior picks inside the tied set: near the truth it keeps the true
    # side; a prior on the mirror locks onto the mirror (documented limit).
    near = [p_true[0] + 1.0, p_true[1], p_true[2]]
    seeded = solve_bistatic_position(
        tx, rx, ranges, max_rms_residual_m=15.0, range_sigma_m=2.0, prior=near
    )
    assert seeded.position[2] > 40.0 and math.dist(seeded.position, p_true) < 3.0
    locked = solve_bistatic_position(
        tx, rx, ranges, max_rms_residual_m=15.0, range_sigma_m=2.0, prior=mirror
    )
    assert math.dist(locked.position, mirror) < 3.0

    consts = detection_constants(_config(frequency_hz=28e9), SensingTrackOptions(cpi_pulses=4096))
    nodes = {k: (list(p), [0.0, 0.0, 0.0]) for k, p in TRPS.items()}
    nodes["trp6_rx"] = (list(TRPS["trp6"]), [0.0, 0.0, 0.0])
    reports = [
        SensingLinkReport(
            tx_id=k, rx_id="trp6_rx", target_id="uav_01", detected=True, reason="detected",
            snr_db=snr, measured_range_m=r, measured_doppler_hz=300.0,
        )
        for k, r, snr in links
    ]
    est = estimate_target(
        _target_at(p_true), reports, nodes, consts, None, 0.0, measurement_noise=True
    )
    assert est.status == "ok" and est.n_links_used == 4
    assert est.position_est[2] > 40.0 and est.position_error_m < 3.0
    # Without measurement noise the ranges count as exact: no noise tie.
    exact = estimate_target(_target_at(p_true), reports, nodes, consts, None, 0.0)
    assert exact.position_est == pytest.approx(mirror, abs=1e-6)


def test_doppler_velocity_sign_matches_sionna_convention():
    v_true = (10.0, -3.0, 1.0)
    scene = _trp_scene(actors=[Actor(
        id="uav_01", kind="uav", position=[29.0, 31.0, 59.0],
        sensing={"model": "tr38901", "object_type": "uav-small-size"},
    )])
    target = _target_at(P_TRUE, v_true)
    states = [ActorState(id="uav_01", position=[P_TRUE[0], P_TRUE[1], P_TRUE[2] - 0.125])]
    config = _config()
    result = MockBackend().simulate_sensing(
        Path("."), scene, load_default_library(), config,
        SensingSimulateRequest(), [target],
        actor_states=states, actor_velocities={"uav_01": list(v_true)},
    )
    assert len(result.paths) == 16

    opts = SensingTrackOptions(cpi_pulses=100_000_000, threshold_db=-30.0, mti_min_doppler_hz=0.0)
    consts = detection_constants(config, opts)
    reports = [
        link_report(p.tx_id, p.rx_id, "uav_01", result.paths, consts, opts, 0)
        for p in result.paths
    ]
    nodes = {d.id: (list(d.position), [0.0, 0.0, 0.0]) for d in scene.devices}
    est = estimate_target(target, reports, nodes, consts, None, 0.0)
    assert est.status == "ok" and est.n_links_used == 10
    assert est.position_error_m < 1e-6
    assert est.velocity_error_m_s < 1e-6
    assert est.velocity_est == pytest.approx(list(v_true), abs=1e-6)

    # Monostatic, target receding along k_ts: negative Doppler -2 v / lambda.
    radar = (0.0, 0.0, 10.0)
    p = np.array([30.0, 0.0, 10.0])
    lam = C / F1
    single = Scene(
        scene_id="mono",
        devices=[
            Device(id="tx", kind="tx", position=list(radar), power_dbm=30.0),
            Device(id="rx", kind="rx", position=list(radar)),
        ],
        actors=[Actor(id="uav_01", kind="uav", position=[30.0, 0.0, 9.875], sensing={})],
    )
    receding = MockBackend().simulate_sensing(
        Path("."), single, load_default_library(), config, SensingSimulateRequest(),
        [_target_at(tuple(p), (10.0, 0.0, 0.0))],
        actor_states=[ActorState(id="uav_01", position=[30.0, 0.0, 9.875])],
        actor_velocities={"uav_01": [10.0, 0.0, 0.0]},
    )
    assert receding.paths[0].doppler_hz == pytest.approx(-2.0 * 10.0 / lam, abs=1e-9)

    # Static bistatic geometry: rows k_sr - k_ts recover the velocity exactly.
    tx_pos = [TRPS["trp1"], TRPS["trp3"], TRPS["trp4"], TRPS["trp6"]]
    rx_pos = [TRPS["trp3"], TRPS["trp4"], TRPS["trp6"], TRPS["trp1"]]
    v = np.array(v_true)
    doppler = []
    for t, s in zip(tx_pos, rx_pos):
        k_ts = (np.array(P_TRUE) - t) / np.linalg.norm(np.array(P_TRUE) - t)
        k_sr = (np.array(s) - P_TRUE) / np.linalg.norm(np.array(s) - P_TRUE)
        doppler.append(float(v @ (k_sr - k_ts)) / lam)
    fused = solve_doppler_velocity(P_TRUE, tx_pos, rx_pos, doppler, lam)
    assert fused == pytest.approx(list(v_true), abs=1e-9)


def test_mock_frame_mode_takes_the_callers_kinematics():
    # A radar riding a moving ego actor: the t = 0 snapshot gives it the ego
    # velocity; in frame mode the caller owns velocities (none here: at rest).
    scene = Scene(
        scene_id="ego",
        devices=[
            Device(id="tx", kind="tx", position=[0.0, 0.0, 10.0], power_dbm=30.0),
            Device(id="rx", kind="rx", position=[0.0, 0.0, 10.0]),
        ],
        actors=[
            Actor(id="uav_01", kind="uav", position=[30.0, 0.0, 9.875], sensing={}),
            Actor(
                id="ego_01", kind="car", position=[0.0, 0.0, 0.0],
                trajectory=ActorTrajectory(waypoints=[[0, 0, 0], [10, 0, 0]], dt_s=1.0),
                attached_device_ids=["tx", "rx"],
            ),
        ],
    )
    target = _target_at((30.0, 0.0, 10.0))
    args = (Path("."), scene, load_default_library(), _config(), SensingSimulateRequest(), [target])
    snapshot = MockBackend().simulate_sensing(*args)
    frame = MockBackend().simulate_sensing(*args, actor_states=[], actor_velocities={})
    assert snapshot.paths[0].doppler_hz > 100.0
    assert frame.paths[0].doppler_hz == pytest.approx(0.0, abs=1e-12)


# ------------------------------------------------------------- scenario


def test_scenario_sensing_mock_end_to_end():
    scene = _trp_scene()
    config = _config()
    result = _scenario(scene, config, cpi_pulses=4096)
    assert len(result.frames) == 5
    order = [
        (tx, f"{rx}_rx", "uav_01") for tx in TRPS for rx in TRPS
    ]
    for i, frame in enumerate(result.frames):
        assert frame.sensing is not None
        assert [(r.tx_id, r.rx_id, r.target_id) for r in frame.sensing.links] == order
        assert len(frame.sensing.echoes) == 16
        assert frame.sensing.echoes[0].path_id == "sensing_0001"
        est = frame.sensing.estimates[0]
        assert est.status == "ok", (i, est)
        assert est.position_error_m < 1e-3 and est.velocity_error_m_s < 1e-3
        # Truth follows the frame: base + h/2, trajectory velocity.
        base = frame.actor_states[0].position
        assert est.position_true == pytest.approx([base[0], base[1], base[2] + 0.125])
        assert est.velocity_true == pytest.approx([10.0, 0.0, 0.0])
        assert est.n_links_used >= 3 and len(est.links_used) == est.n_links_used

    summary = result.metadata["sensing"]
    assert summary["wavelength_m"] == pytest.approx(C / F1)
    assert summary["num_links"] == 16 and summary["target_ids"] == ["uav_01"]
    assert summary["options"]["cpi_pulses"] == 4096
    stats = summary["targets"]["uav_01"]
    assert set(stats) == {
        "frames", "detected_frames", "detection_rate", "link_detection_rate",
        "frames_ge3_links", "frames_ge3_links_rate", "ok_frames",
        "median_position_error_m", "p90_position_error_m",
        "median_velocity_error_m_s", "median_gdop",
    }
    assert stats["frames"] == 5 and stats["ok_frames"] == 5
    assert stats["detection_rate"] == 1.0
    assert 0.0 < stats["link_detection_rate"] <= 1.0
    assert stats["median_position_error_m"] < 1e-3
    for key in ("detection_rate", "frames_ge3_links_rate", "median_position_error_m",
                "median_velocity_error_m_s"):
        assert key in summary
    assert any(w.startswith("sensing: mock sensing") for w in result.warnings)
    assert not any(w.startswith("sensing frame") for w in result.warnings)

    again = _scenario(scene, config, cpi_pulses=4096)
    assert again.model_dump() == result.model_dump()


def test_scenario_sensing_off_is_unchanged():
    scene = _trp_scene()
    base = dict(num_frames=3, dt_s=0.5, include_paths=False)
    runs = [
        run_scenario(
            MockBackend(), Path("."), scene, load_default_library(), _config(),
            ScenarioSimulateRequest(**base, sensing=sensing),
        )
        for sensing in (None, SensingTrackOptions(enabled=False))
    ]
    assert runs[0].model_dump() == runs[1].model_dump()
    assert all(f.sensing is None for f in runs[0].frames)
    assert "sensing" not in runs[0].metadata


def test_frequency_agnostic_3p5_and_28_ghz():
    scene = _trp_scene()
    knobs = dict(cpi_pulses=100_000_000, threshold_db=-30.0, mti_min_doppler_hz=0.0)
    low = _scenario(scene, _config(frequency_hz=3.5e9), num_frames=3, **knobs)
    high = _scenario(scene, _config(frequency_hz=28e9), num_frames=3, **knobs)
    for f_lo, f_hi in zip(low.frames, high.frames):
        assert all(r.detected for r in f_lo.sensing.links + f_hi.sensing.links)
        e_lo, e_hi = f_lo.sensing.estimates[0], f_hi.sensing.estimates[0]
        assert e_lo.status == e_hi.status == "ok"
        assert e_hi.position_est == pytest.approx(e_lo.position_est, abs=1e-9)
        assert e_hi.velocity_est == pytest.approx(e_lo.velocity_est, abs=1e-9)
        for r_lo, r_hi in zip(f_lo.sensing.links, f_hi.sensing.links):
            assert r_lo.snr_db - r_hi.snr_db == pytest.approx(20.0 * math.log10(8.0), abs=1e-9)
            assert r_hi.doppler_hz * (C / 28e9) == pytest.approx(
                r_lo.doppler_hz * (C / 3.5e9), abs=1e-9
            )
    assert low.metadata["sensing"]["wavelength_m"] == pytest.approx(C / 3.5e9)
    assert high.metadata["sensing"]["wavelength_m"] == pytest.approx(C / 28e9)
    blind = [
        detection_constants(_config(frequency_hz=f), SensingTrackOptions())["mti_blind_speed_m_s"]
        for f in (3.5e9, 28e9)
    ]
    assert blind[0] / blind[1] == pytest.approx(8.0)


def test_resolve_target_state_follows_frame():
    actor = _uav()
    state = ActorState(id="uav_01", position=[1.0, 2.0, 3.0], orientation_deg=[45.0, 1.0, 2.0])
    t = resolve_target_state(actor, state, [4.0, 5.0, 6.0])
    assert t.center == pytest.approx((1.0, 2.0, 3.125))
    assert t.orientation_deg == pytest.approx((45.0, 1.0, 2.0))
    assert t.velocity_m_s == pytest.approx((4.0, 5.0, 6.0))
    assert t.object_type == "uav-small-size" and t.rcs_dbsm == -12.81

    pinned = _uav(velocity_m_s=[-1.0, 0.0, 0.0])
    t = resolve_target_state(pinned, state, [4.0, 5.0, 6.0])
    assert t.velocity_m_s == pytest.approx((-1.0, 0.0, 0.0))
    assert t.center == pytest.approx((1.0, 2.0, 3.125))  # pose still follows the frame


def test_measurement_noise_deterministic():
    scene = _trp_scene()

    def measured(**knobs):
        res = _scenario(scene, _config(), num_frames=2, cpi_pulses=4096, **knobs)
        return [
            (r.measured_range_m, r.measured_doppler_hz)
            for f in res.frames for r in f.sensing.links
        ], res

    first, res = measured(measurement_noise=True, noise_seed=7)
    second, _ = measured(measurement_noise=True, noise_seed=7)
    other, _ = measured(measurement_noise=True, noise_seed=8)
    exact, res_exact = measured()
    assert first == second
    assert first != other
    assert first != exact
    assert exact == [
        (r.bistatic_range_m, r.doppler_hz) for f in res_exact.frames for r in f.sensing.links
    ]
    # Noise never changes a decision.
    assert [r.reason for f in res.frames for r in f.sensing.links] == [
        r.reason for f in res_exact.frames for r in f.sensing.links
    ]
    # sigma = cell / sqrt(2 SNR), cell = c/B for range and 1/CPI for Doppler,
    # seeded per frame, link and target; the range draw comes first.
    for i, frame in enumerate(res.frames):
        for r in frame.sensing.links:
            rng = random.Random(f"7:{i}:{r.tx_id}:{r.rx_id}:{r.target_id}")
            root = math.sqrt(2.0 * 10.0 ** (r.snr_db / 10.0))
            assert r.measured_range_m == pytest.approx(
                r.bistatic_range_m + rng.gauss(0.0, (C / 2e7) / root), abs=1e-9
            )
            assert r.measured_doppler_hz == pytest.approx(
                r.doppler_hz + rng.gauss(0.0, (1.0 / 0.01) / root), abs=1e-9
            )


def test_summary_statistics_from_frames():
    scene = _trp_scene()
    tracker = SensingTracker(
        MockBackend(), Path("."), scene, load_default_library(), _config(),
        SensingTrackOptions(enabled=True),
        [d for d in scene.devices if d.kind == "tx"],
        [d for d in scene.devices if d.kind == "rx"],
        ["uav_01"],
    )

    def frame(status, used, detected, err=None):
        hit = SensingLinkReport(
            tx_id="trp1", rx_id="trp1_rx", target_id="uav_01", detected=True, reason="detected"
        )
        est = TargetEstimate(
            target_id="uav_01", status=status, n_links_detected=detected, n_links_used=used,
            position_true=[0.0, 0.0, 0.0], velocity_true=[0.0, 0.0, 0.0],
            position_error_m=err, velocity_error_m_s=None if err is None else err / 10.0,
            gdop=None if err is None else 1.0,
        )
        return ScenarioFrame(
            time_s=0.0, sensing=SensingFrame(links=[hit] * detected, estimates=[est])
        )

    errs = [0.1, 0.2, 0.3, 0.4, 5.0]
    frames = [frame("ok", 4, 5, e) for e in errs]
    # Three detected reciprocal/repeated links can fuse into fewer than three.
    frames += [frame("insufficient_links", 2, 3), frame("diverged", 3, 3)]
    stats = tracker.summary(frames)["targets"]["uav_01"]
    assert stats["frames"] == 7 and stats["ok_frames"] == 5
    assert stats["frames_ge3_links"] == 6  # n_links_used >= 3, not n_links_detected
    assert stats["frames_ge3_links_rate"] == pytest.approx(6 / 7)
    assert stats["median_position_error_m"] == pytest.approx(0.3)
    assert stats["p90_position_error_m"] == pytest.approx(float(np.percentile(errs, 90)))
    assert stats["p90_position_error_m"] == pytest.approx(3.16)
    assert stats["median_velocity_error_m_s"] == pytest.approx(0.03)
    assert stats["detection_rate"] == 1.0
    assert stats["link_detection_rate"] == pytest.approx((5 * 5 + 3 + 3) / (7 * 16))


def _car_scene() -> Scene:
    car = Actor(
        id="car_01", kind="car", position=[-20.0, 30.0, 0.0],
        trajectory=ActorTrajectory(
            waypoints=[[-20.0, 30.0, 0.0], [60.0, 30.0, 0.0]], speed_m_s=10.0
        ),
        sensing={"model": "constant", "rcs_dbsm": -10.0},
    )
    return _trp_scene(actors=[car])


def test_scenario_ground_target_under_rooftop_trps():
    # A car (center 0.75 m up) under TRPs at ~20 m: the ellipsoids also meet
    # near its mirror ~40 m up, which fits within the range noise at 15-28 dB.
    knobs = dict(cpi_pulses=512, cpi_s=0.05)
    exact = _scenario(_car_scene(), _config(), num_frames=8, **knobs)
    for i, frame in enumerate(exact.frames):
        est = frame.sensing.estimates[0]
        assert est.status == "ok" and est.n_links_used >= 4, (i, est)
        assert est.position_error_m < 1e-3, (i, est)  # noise off: exact ranges, exact root
    for seed in range(3):
        noisy = _scenario(
            _car_scene(), _config(), num_frames=8, measurement_noise=True, noise_seed=seed,
            **knobs,
        )
        for i, frame in enumerate(noisy.frames):
            est = frame.sensing.estimates[0]
            assert est.status == "ok", (seed, i, est)
            assert est.position_est[2] < 5.0 and est.position_error_m < 3.0, (seed, i, est)


def _ego_radar_scene(drone: Actor, devices=()) -> Scene:
    ego = Actor(
        id="ego_01", kind="car", position=[0.0, 0.0, 0.0],
        trajectory=ActorTrajectory(waypoints=[[0.0, 0.0, 0.0], [300.0, 0.0, 0.0]], speed_m_s=15.0),
        attached_device_ids=["ego_tx", "ego_rx"],
    )
    radar = [
        Device(id="ego_tx", kind="tx", position=[0.0, 0.0, 2.0], power_dbm=40.0),
        Device(id="ego_rx", kind="rx", position=[0.0, 0.0, 2.0]),
    ]
    return Scene(scene_id="ego", devices=[*devices, *radar], actors=[ego, drone])


def test_mti_notch_follows_a_moving_radar():
    # Monostatic radar on a car at 15 m/s: a hovering drone sits on the static
    # scene's (ego-motion) Doppler and is clutter; a drone flying alongside
    # has f_D ~ 0 yet moves over the ground, so MTI must pass it.
    def drone(trajectory):
        return Actor(
            id="uav_01", kind="uav", position=[60.0, 0.0, 20.0], trajectory=trajectory,
            sensing={"model": "tr38901", "object_type": "uav-small-size"},
        )

    hover = _scenario(_ego_radar_scene(drone(None)), _config(), num_frames=3, cpi_pulses=4096)
    alongside = ActorTrajectory(waypoints=[[60.0, 0.0, 20.0], [360.0, 0.0, 20.0]], speed_m_s=15.0)
    flying = _scenario(_ego_radar_scene(drone(alongside)), _config(), num_frames=3, cpi_pulses=4096)
    for f_hover, f_flying in zip(hover.frames, flying.frames):
        still, moving = f_hover.sensing.links[0], f_flying.sensing.links[0]
        assert abs(still.doppler_hz) > 300.0 and still.reason == "mti_rejected"
        assert abs(moving.doppler_hz) < 1e-6 and moving.reason == "detected"


def test_scenario_sensing_moving_rider():
    # The ego radar's own motion enters the Doppler fit as v_tx . k_ts and
    # v_rx . k_sr: with either sign wrong the velocity would not fit exactly.
    scene = _ego_radar_scene(_uav(), devices=_trp_scene().devices)
    knobs = dict(cpi_pulses=100_000_000, threshold_db=-30.0, mti_min_doppler_hz=0.0)
    result = _scenario(scene, _config(), num_frames=3, **knobs)
    lam = C / F1
    for i, frame in enumerate(result.frames):
        est = frame.sensing.estimates[0]
        assert est.status == "ok" and "ego_tx>ego_rx" in est.links_used, (i, est)
        assert est.position_error_m < 1e-6 and est.velocity_error_m_s < 1e-6, (i, est)
        radar = next(d.position for d in frame.device_states if d.id == "ego_tx")
        k = np.array(est.position_true) - np.array(radar)
        rider_hz = 2.0 * 15.0 * k[0] / np.linalg.norm(k) / lam  # ego drives along +x
        assert abs(rider_hz) > 10.0  # the rider term is not negligible


def test_scenario_frame_on_a_loop_wrap():
    # 180 m at 10 m/s loops every 18 s; a frame exactly on the wrap is back at
    # the first waypoint and moving along the first leg (not the jump back).
    loop = _uav()
    loop.trajectory.mode = "loop"
    request = ScenarioSimulateRequest(
        num_frames=4, dt_s=6.0, include_paths=False,
        sensing={"enabled": True, "cpi_pulses": 4096},
    )
    result = run_scenario(
        MockBackend(), Path("."), _trp_scene(actors=[loop]), load_default_library(), _config(),
        request,
    )
    wrap = result.frames[3]
    est = wrap.sensing.estimates[0]
    assert wrap.time_s == 18.0
    assert est.position_true == pytest.approx([-30.0, 0.0, 60.125])
    assert est.velocity_true == pytest.approx([10.0, 0.0, 0.0])
    assert max(abs(r.doppler_hz) for r in wrap.sensing.links) < 2.0 * 10.0 / (C / F1)
    assert est.status == "ok" and est.velocity_error_m_s < 1e-6


def test_include_comm_paths_appends_but_never_detects():
    scene = _trp_scene()
    plain = _scenario(scene, _config(), num_frames=3, cpi_pulses=4096)
    comm = _scenario(scene, _config(), num_frames=3, cpi_pulses=4096, include_comm_paths=True)
    for f_plain, f_comm in zip(plain.frames, comm.frames):
        n = len(f_plain.sensing.echoes)
        assert f_comm.sensing.echoes[:n] == f_plain.sensing.echoes
        extra = f_comm.sensing.echoes[n:]
        assert extra and all(p.target_id is None and p.path_type != "sensing" for p in extra)
        # Comm paths never become detections or fused links.
        assert f_comm.sensing.links == f_plain.sensing.links
        assert f_comm.sensing.estimates == f_plain.sensing.estimates


def test_tracker_seeds_the_next_frame_from_the_last_ok_estimate(monkeypatch):
    real = sensing_track.solve_bistatic_position
    priors: list = []

    def spy(*args, **kwargs):
        priors.append(kwargs.get("prior"))
        fused = real(*args, **kwargs)
        if len(priors) == 2:  # frame 1 fails: it must not become the prior
            return FusionResult(fused.position, "diverged", fused.iterations, None, None)
        return fused

    monkeypatch.setattr(sensing_track, "solve_bistatic_position", spy)
    result = _scenario(_trp_scene(), _config(), num_frames=3, cpi_pulses=4096)
    first, failed, last = (f.sensing.estimates[0] for f in result.frames)
    assert (first.status, failed.status, last.status) == ("ok", "diverged", "ok")
    assert priors[0] is None
    # Advanced by the last ok velocity over the time since that frame.
    for prior, dt in ((priors[1], 0.5), (priors[2], 1.0)):
        assert prior == pytest.approx(
            [p + v * dt for p, v in zip(first.position_est, first.velocity_est)]
        )


# ------------------------------------------------------------------ API


def _api_scene(**kw) -> dict:
    return _trp_scene(scene_id=PID, **kw).model_dump(mode="json")


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "ISAC", "project_id": PID})
    assert resp.status_code == 201, resp.text
    put = api_client.put(f"/api/projects/{PID}/scene", json=_api_scene())
    assert put.status_code == 200, put.text
    return api_client


def _post(client, sensing=None, config=None, **body):
    payload = {
        "config": config or MOCK_CFG, "num_frames": 3, "dt_s": 0.5, "include_paths": False,
        **body,
    }
    if sensing is not None:
        payload["sensing"] = sensing
    return client.post(f"/api/projects/{PID}/simulate/scenario", json=payload)


SENSING = {"enabled": True, "cpi_pulses": 4096, "measurement_noise": True}


def test_api_scenario_sensing_200_and_reload(client):
    resp = _post(client, SENSING)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result_id"] == "mock_scenario_001" and body["kind"] == "scenario"
    frame = body["frames"][0]
    # nodes: v0.1.13 per-frame TX/RX kinematics.
    assert set(frame["sensing"]) == {"echoes", "links", "estimates", "nodes"}
    assert len(frame["sensing"]["nodes"]) == 8
    assert len(frame["sensing"]["links"]) == 16
    assert frame["sensing"]["estimates"][0]["status"] == "ok"
    summary = body["metadata"]["sensing"]
    assert summary["options"]["measurement_noise"] is True
    assert summary["noise_floor_dbm"] == pytest.approx(-93.98970004336019)
    assert summary["targets"]["uav_01"]["frames"] == 3

    stored = client.get(f"/api/projects/{PID}/results/scenario")
    assert stored.status_code == 200 and stored.json() == body


def test_api_scenario_sensing_400_no_targets(client):
    unbound = _trp_scene(
        actors=[Actor(id="uav_01", kind="uav", position=[0.0, 0.0, 60.0])], scene_id=PID
    )
    client.put(f"/api/projects/{PID}/scene", json=unbound.model_dump(mode="json"))
    resp = _post(client, SENSING)
    assert resp.status_code == 400 and "no sensing targets" in resp.json()["detail"]

    resp = _post(client, {**SENSING, "target_actor_ids": ["ghost"]})
    assert resp.status_code == 400 and "actor not found: ghost" in resp.json()["detail"]
    # Sensing off: the same scene still runs.
    assert _post(client).status_code == 200


def test_api_scenario_sensing_400_no_rx(client):
    client.put(f"/api/projects/{PID}/scene", json=_api_scene(with_rx=False))
    resp = _post(client, SENSING)
    assert resp.status_code == 400
    assert "at least one tx and one rx" in resp.json()["detail"]


def test_api_scenario_sensing_409_backend_without_sensing(client, monkeypatch):
    from seam_studio.services import availability
    from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend

    monkeypatch.setattr(SionnaBackend, "is_available", lambda self: True)
    monkeypatch.setattr(availability, "sionna_rcs_available", lambda: False)
    for backend in ("sionna", "auto"):
        resp = _post(client, SENSING, config={**MOCK_CFG, "backend": backend})
        assert resp.status_code == 409, (backend, resp.text)
        assert "sensing over time needs a backend with sensing" in resp.json()["detail"]
        assert "the sionna backend has none" in resp.json()["detail"]
    assert client.get(f"/api/projects/{PID}/scene").json()["result_sets"] == []


def test_api_scenario_without_sensing_still_200(client):
    resp = client.post(
        f"/api/projects/{PID}/simulate/scenario",
        json={"num_frames": 2, "dt_s": 0.5, "include_paths": False, "config": MOCK_CFG},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert all(f["sensing"] is None for f in body["frames"])
    assert "sensing" not in body["metadata"]
    assert _post(client, {"enabled": True, "bogus": 1}).status_code == 422
    assert _post(client, {"enabled": True, "threshold_db": -31}).status_code == 422


def test_openapi_pins_scenario_sensing():
    from seam_studio.main import app

    schema = app.openapi()
    components = schema["components"]["schemas"]
    assert set(components["SensingTrackOptions"]["properties"]) == {
        "enabled", "threshold_db", "cpi_s", "cpi_pulses", "mti_min_doppler_hz",
        "include_comm_paths", "target_actor_ids", "samples_per_sp", "max_depth",
        "measurement_noise", "noise_seed",
        # v0.1.13 (Phase C): EKF tracking, detector model, link-report Pfa.
        "tracking", "detector", "pfa",
    }
    for name in ("SensingLinkReport", "TargetEstimate", "SensingFrame"):
        assert name in components, name
    assert "sensing" in components["ScenarioSimulateRequest"]["properties"]
    assert "sensing" in components["ScenarioFrame"]["properties"]
    assert "post" in schema["paths"]["/api/projects/{project_id}/simulate/scenario"]


def test_stored_v0110_scenario_json_still_validates():
    stored = {
        "result_id": "mock_scenario_001",
        "kind": "scenario",
        "backend": "mock",
        "simulation_config_id": "default",
        "frames": [
            {
                "time_s": 0.0,
                "actor_states": [{"id": "uav_01", "position": [0.0, 0.0, 60.0]}],
                "device_states": [],
                "links": [{"tx_id": "trp1", "rx_id": "trp1_rx", "path_count": 0}],
                "paths": None,
            }
        ],
        "warnings": [],
        "metadata": {"num_frames": 1},
    }
    result = ScenarioResultSet.model_validate(stored)
    assert result.frames[0].sensing is None


# ------------------------------------------------- v0.1.13 (ISAC Phase C)

# A v0.1.12 mock run (4 frames, noisy, seed 3), computed with the v0.1.12
# code: position_est, velocity_est, position_error_m, sum of measured ranges.
V012_PIN = [
    ([-29.25558004895766, 0.23782117573314238, 60.68760275237849],
     [9.895317434747442, -0.42526884163979306, 0.021988827599850505],
     0.9629339708000871, 3393.8926978176046),
    ([-25.209616016162013, 0.5701230183386533, 59.76184430159438],
     [10.35642686945149, -0.02671136408171387, 0.34669808331351587],
     0.7077154735878561, 3327.7230682254244),
    ([-19.375155399021637, 0.24468985900319842, 60.555508816126206],
     [10.102018691017447, -0.14863226491682013, 0.03235215740257456],
     0.7972714363585359, 3255.592999368356),
    ([-13.572380190522797, 0.5164424899156839, 61.135705928887006],
     [9.752759646015669, -0.24009211469624467, -0.4815245531514199],
     1.8238250027043121, 3197.994206380758),
]
V012_OPTIONS = {
    "cpi_pulses": 4096, "cpi_s": 0.01, "enabled": True, "include_comm_paths": False,
    "max_depth": None, "measurement_noise": True, "mti_min_doppler_hz": None, "noise_seed": 3,
    "samples_per_sp": 1000000, "target_actor_ids": None, "threshold_db": 13.0,
}
TRACK_FIELDS = (
    "track_status", "track_position", "track_velocity", "track_position_error_m",
    "track_velocity_error_m_s", "track_position_std_m", "track_updates", "track_gated",
)


def test_phase_c_defaults_reproduce_v0112():
    knobs = dict(num_frames=4, cpi_pulses=4096, measurement_noise=True, noise_seed=3)
    off = _scenario(_trp_scene(), _config(), **knobs)
    disabled = _scenario(_trp_scene(), _config(), **knobs, tracking={"enabled": False})
    assert disabled.model_dump() == off.model_dump()

    for frame, (pos, vel, err, ranges) in zip(off.frames, V012_PIN):
        est = frame.sensing.estimates[0]
        assert est.position_est == pytest.approx(pos, abs=1e-9)
        assert est.velocity_est == pytest.approx(vel, abs=1e-9)
        assert est.position_error_m == pytest.approx(err, abs=1e-9)
        assert sum(r.measured_range_m for r in frame.sensing.links) == pytest.approx(
            ranges, abs=1e-6
        )
        assert all(getattr(est, f) is None for f in TRACK_FIELDS)
        assert all(r.pd is None and r.pd_mc is None for r in frame.sensing.links)
    summary = off.metadata["sensing"]
    assert summary["options"] == V012_OPTIONS  # no Phase C keys
    assert summary["median_position_error_m"] == pytest.approx(0.8801027035793115, abs=1e-9)
    assert summary["median_velocity_error_m_s"] == pytest.approx(0.4682320566044862, abs=1e-9)
    assert "tracking_model" not in summary and "median_track_position_error_m" not in summary
    assert not any("track" in k for k in summary["targets"]["uav_01"])


def test_frame_nodes_are_the_trackers_kinematics():
    scene = _ego_radar_scene(_uav(), devices=_trp_scene().devices)
    result = _scenario(scene, _config(), num_frames=3, cpi_pulses=4096)
    for frame in result.frames:
        nodes = frame.sensing.nodes
        tx_ids = [d.id for d in scene.devices if d.kind == "tx"]
        rx_ids = [d.id for d in scene.devices if d.kind == "rx"]
        assert [n.id for n in nodes] == tx_ids + rx_ids  # every TX, then every RX
        by_id = {n.id: n for n in nodes}
        moved = {d.id: d.position for d in frame.device_states}
        for dev in scene.devices:
            assert by_id[dev.id].position == pytest.approx(moved.get(dev.id, dev.position))
        assert by_id["ego_tx"].velocity == pytest.approx([15.0, 0.0, 0.0])
        assert by_id["ego_rx"].velocity == pytest.approx([15.0, 0.0, 0.0])
        assert by_id["trp1"].velocity == [0.0, 0.0, 0.0]


def test_link_pd_follows_the_detector_model():
    from seam_studio.services.detector import pd_swerling, wilson_ci
    from seam_studio.services.sensing_track import swerling1_pd

    scene = _trp_scene()
    plain = _scenario(scene, _config(), num_frames=2, cpi_pulses=4096)
    assert all(r.pd is None for f in plain.frames for r in f.sensing.links)

    sw1 = _scenario(scene, _config(), num_frames=2, cpi_pulses=4096, pfa=1e-6)
    sw3 = _scenario(scene, _config(), num_frames=2, cpi_pulses=4096, pfa=1e-6,
                    detector={"model": "swerling3"})
    for f1, f3, f0 in zip(sw1.frames, sw3.frames, plain.frames):
        for r1, r3, r0 in zip(f1.sensing.links, f3.sensing.links, f0.sensing.links):
            assert r1.pd == swerling1_pd(r1.snr_db, 1e-6)  # bit-identical
            assert r3.pd == pd_swerling(r3.snr_db, 1e-6, "swerling3")
            assert r1.pd_mc is None
            # Pd never changes a detection or a measurement.
            assert r1.model_copy(update={"pd": None}) == r0
        assert f1.sensing.estimates == f0.sensing.estimates

    trials = 20_000
    mc = _scenario(scene, _config(), num_frames=2, cpi_pulses=4096, pfa=1e-3,
                   detector={"monte_carlo_trials": trials, "seed": 5})
    again = _scenario(scene, _config(), num_frames=2, cpi_pulses=4096, pfa=1e-3,
                      detector={"monte_carlo_trials": trials, "seed": 5})
    assert mc.model_dump() == again.model_dump()
    for frame in mc.frames:
        for r in frame.sensing.links:
            assert r.snr_db is not None and r.pd_mc is not None
            lo, hi = wilson_ci(round(r.pd_mc * trials), trials, z=4.0)
            assert lo <= r.pd <= hi, r
    assert mc.metadata["sensing"]["options"]["detector"]["monte_carlo_trials"] == trials

    # No echo: Pd = Pfa, no Monte Carlo.
    opts = SensingTrackOptions(pfa=1e-3, detector={"monte_carlo_trials": 100})
    none = sensing_track.with_link_pd(
        SensingLinkReport(tx_id="a", rx_id="b", target_id="uav_01"), opts, 0, 0
    )
    assert none.pd == 1e-3 and none.pd_mc is None


def test_link_monte_carlo_costs_one_sample_per_trial():
    # 976 trials x 4096 pulses fit the old explicit-pulse budget (4e6), so
    # every link drew all 4e6 pulses (~1000x the cost of 977 trials) while
    # the 400 budget counted 976. Now a link's draw ignores cpi_pulses: it is
    # the cpi_pulses = 1 stream exactly.
    from seam_studio.services.detector import monte_carlo_pd

    trials = 976
    res = _scenario(_trp_scene(), _config(), num_frames=1, cpi_pulses=4096, pfa=1e-3,
                    detector={"monte_carlo_trials": trials, "seed": 2})
    links = res.frames[0].sensing.links
    assert all(r.snr_db is not None for r in links)
    for k, r in enumerate(links):
        mc = monte_carlo_pd(r.snr_db, 1e-3, trials, "swerling1", seed=[2, 0, k],
                            cpi_pulses=1, measure_pfa=False)
        assert r.pd_mc == mc.pd


def test_link_pd_with_an_empirical_threshold():
    # Each link draws its own noise-only run for the threshold (no measured
    # Pfa is stored per link); pd_mc still estimates the analytic pd.
    from seam_studio.services.detector import wilson_ci

    trials = 20_000
    knobs = dict(num_frames=2, cpi_pulses=4096, pfa=1e-3,
                 detector={"monte_carlo_trials": trials, "empirical_threshold": True})
    res = _scenario(_trp_scene(), _config(), **knobs)
    assert res.model_dump() == _scenario(_trp_scene(), _config(), **knobs).model_dump()
    for frame in res.frames:
        for r in frame.sensing.links:
            lo, hi = wilson_ci(round(r.pd_mc * trials), trials, z=4.0)
            assert lo <= r.pd <= hi, r


def test_api_scenario_mc_budget_400(client):
    # 10 frames x 4 tx x 4 rx x 1 target = 160 link estimates x 2e6 > 2e8.
    sensing = {**SENSING, "pfa": 1e-6, "detector": {"monte_carlo_trials": 2_000_000}}
    resp = _post(client, sensing, num_frames=10)
    assert resp.status_code == 400
    assert "Monte Carlo trials" in resp.json()["detail"]
    assert client.get(f"/api/projects/{PID}/scene").json()["result_sets"] == []
    assert _post(client, {**SENSING, "detector": {"monte_carlo_trials": 10}}).status_code == 422
    ok = _post(client, {**sensing, "detector": {"monte_carlo_trials": 1000}}, num_frames=2)
    assert ok.status_code == 200, ok.text
    assert ok.json()["frames"][0]["sensing"]["links"][0]["pd_mc"] is not None
    # An empirical threshold doubles each link's draws (its noise run):
    # 1e6 x 160 = 1.6e8 fits, 2 x 1.6e8 does not.
    empirical = {**SENSING, "pfa": 1e-3,
                 "detector": {"monte_carlo_trials": 1_000_000, "empirical_threshold": True}}
    resp = _post(client, empirical, num_frames=10)
    assert resp.status_code == 400 and "noise run" in resp.json()["detail"]


def test_openapi_pins_tracking_and_sensing_dataset():
    from seam_studio.main import app

    schema = app.openapi()
    components = schema["components"]["schemas"]
    assert set(components["TrackingOptions"]["properties"]) == {
        "enabled", "process_accel_sigma_m_s2", "gate_chi2", "init_from",
        "coast_max_frames", "max_position_std_m", "use_as_prior",
    }
    assert {"track_status", "track_position", "track_updates"} <= set(
        components["TargetEstimate"]["properties"]
    )
    route = schema["paths"]["/api/projects/{project_id}/export/sensing-dataset"]["post"]
    assert route["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith(
        "SensingDatasetExportResult"
    )
