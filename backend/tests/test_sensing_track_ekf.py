"""EKF tracking over scenario frames (sensing.tracking): the filter pieces on
synthetic constant-velocity targets (exact measurements, so every number is
analytic), and the tracker's status machine on mock scenarios."""

import math

import numpy as np
import pytest

from seam_studio.schemas.results import SensingLinkReport, TargetEstimate
from seam_studio.schemas.scene import Actor, ActorTrajectory
from seam_studio.schemas.sensing import SensingTrackOptions
from seam_studio.services import sensing_track
from seam_studio.services.sensing_track import (
    TRACKING_MODEL,
    chi2_3dof_gate,
    cv_predict,
    detection_constants,
    doppler_model,
    ekf_update,
    init_track,
    measurement_sigmas,
    range_model,
    track_measurements,
)

from .test_sensing_track import C, F1, TRPS, _config, _scenario, _trp_scene

LAM = C / F1
NODES = {k: (list(p), [0.0, 0.0, 0.0]) for k, p in TRPS.items()}
NODES.update({f"{k}_rx": (list(p), [0.0, 0.0, 0.0]) for k, p in TRPS.items()})
P0 = np.array([30.0, 30.0, 60.0])
V0 = np.array([8.0, -3.0, 0.5])
DT = 0.5
TRACK_FIELDS = {
    "track_status", "track_position", "track_velocity", "track_position_error_m",
    "track_velocity_error_m_s", "track_position_std_m", "track_updates", "track_gated",
}


def _consts():
    return detection_constants(_config(), SensingTrackOptions())


def _h_doppler(p, v, tx, rx):
    k_ts = (p - tx[0]) / np.linalg.norm(p - tx[0])
    k_sr = (rx[0] - p) / np.linalg.norm(rx[0] - p)
    return float(
        np.asarray(tx[1]) @ k_ts - np.asarray(rx[1]) @ k_sr + v @ (k_sr - k_ts)
    ) / LAM


def _reports(p, v, links, snr_db=20.0, range_offset=None):
    """Exact detected direct reports of a target at (p, v) on ``links``."""
    out = []
    for tx_id, rx_id in links:
        tx, rx = NODES[tx_id], NODES[rx_id]
        r = float(np.linalg.norm(p - tx[0]) + np.linalg.norm(p - rx[0]))
        if range_offset is not None and (tx_id, rx_id) == range_offset[0]:
            r += range_offset[1]
        out.append(SensingLinkReport(
            tx_id=tx_id, rx_id=rx_id, target_id="uav_01", detected=True, reason="detected",
            snr_db=snr_db, measured_range_m=r,
            measured_doppler_hz=_h_doppler(p, v, (np.asarray(tx[0]), tx[1]),
                                           (np.asarray(rx[0]), rx[1])),
        ))
    return out


def _estimate(p, v, *, gdop=2.0, links=("trp1>trp1_rx",)):
    return TargetEstimate(
        target_id="uav_01", status="ok", n_links_used=len(links), links_used=list(links),
        position_true=list(p), velocity_true=list(V0), position_est=list(p),
        velocity_est=None if v is None else list(v), gdop=gdop,
    )


MONO4 = [(k, f"{k}_rx") for k in TRPS]


def _run_filter(x, P, frames, links_of, *, update=True, sigma_a=2.0):
    """x, P after ``frames`` CV steps of a target starting at (P0, V0)."""
    consts = _consts()
    for k in range(1, frames + 1):
        x, P = cv_predict(x, P, DT, sigma_a)
        if update:
            p_true = P0 + V0 * DT * k
            meas = track_measurements(_reports(p_true, V0, links_of(k)), "uav_01", NODES, consts)
            x, P, _, _ = ekf_update(x, P, meas, LAM, 16.0)
    return x, P


# ------------------------------------------------------------- the filter


def test_cv_predict_dwna():
    x = np.array([1.0, 2.0, 3.0, 4.0, -5.0, 6.0])
    x1, P1 = cv_predict(x, np.zeros((6, 6)), 0.5, 2.0)
    assert x1 == pytest.approx([3.0, -0.5, 6.0, 4.0, -5.0, 6.0])
    # Q = sigma_a^2 [[dt^4/4, dt^3/2], [dt^3/2, dt^2]] per axis.
    assert P1[0, 0] == pytest.approx(4.0 * 0.5**4 / 4.0)
    assert P1[0, 3] == pytest.approx(4.0 * 0.5**3 / 2.0)
    assert P1[3, 3] == pytest.approx(4.0 * 0.25)
    assert P1[0, 1] == 0.0 and np.allclose(P1, P1.T)


def test_jacobians_match_finite_differences():
    rng = np.random.default_rng(3)
    eps = 1e-6
    worst = 0.0
    for _ in range(100):
        x = np.concatenate([rng.normal(0, 100, 3), rng.normal(0, 10, 3)])
        tx = (rng.normal(0, 100, 3), rng.normal(0, 5, 3))  # moving nodes
        rx = (rng.normal(0, 100, 3), rng.normal(0, 5, 3))
        for f in (
            lambda y: range_model(y, tx[0], rx[0]),
            lambda y: doppler_model(y, tx, rx, LAM),
        ):
            h, H = f(x)
            num = np.array([(f(x + eps * e)[0] - f(x - eps * e)[0]) / (2 * eps) for e in np.eye(6)])
            worst = max(worst, float(np.max(np.abs(num - H)) / max(1.0, float(np.max(np.abs(H))))))
        # The Doppler model is solve_doppler_velocity's row model.
        assert doppler_model(x, tx, rx, LAM)[0] == pytest.approx(
            _h_doppler(x[:3], x[3:], tx, rx), rel=1e-12
        )
    assert worst < 1e-6


def test_measurements_order_and_sigmas():
    consts = _consts()
    reports = _reports(P0, V0, MONO4[:2])
    reports[1] = reports[1].model_copy(update={"snr_db": 30.0})
    reports.append(reports[0].model_copy(update={"tx_id": "trp6", "rx_id": "trp6_rx",
                                                  "multipath": True}))
    reports.append(reports[0].model_copy(update={"tx_id": "trp4", "rx_id": "trp4_rx",
                                                  "detected": False}))
    meas = track_measurements(reports, "uav_01", NODES, consts)
    # Highest SNR first, range then Doppler per link; multipath / undetected skipped.
    assert [(m.link, m.kind) for m in meas] == [
        ("trp3>trp3_rx", "range"), ("trp3>trp3_rx", "doppler"),
        ("trp1>trp1_rx", "range"), ("trp1>trp1_rx", "doppler"),
    ]
    sigma_r, sigma_f = measurement_sigmas(20.0, consts)
    assert sigma_r == pytest.approx((C / 2e7) / math.sqrt(200.0))
    assert sigma_f == pytest.approx(100.0 / math.sqrt(200.0))
    assert measurement_sigmas(400.0, consts) == (1e-3, 1e-3)  # floored


def test_init_covariance():
    consts = _consts()
    reports = _reports(P0, V0, MONO4[:3])
    est = _estimate(P0, V0, gdop=3.0, links=[f"{t}>{r}" for t, r in MONO4[:3]])
    track = init_track(est, reports, consts, 1.5)
    sigma_r = measurement_sigmas(20.0, consts)[0]
    assert track.x == pytest.approx([*P0, *V0])
    assert np.diag(track.P) == pytest.approx([9.0 * sigma_r**2 / 3.0] * 3 + [25.0] * 3)
    assert track.time_s == 1.5 and track.coast == 0
    tight = init_track(_estimate(P0, None, gdop=0.1), reports, consts, 0.0)
    assert np.diag(tight.P) == pytest.approx([0.25] * 3 + [400.0] * 3)
    assert tight.x[3:] == pytest.approx([0.0, 0.0, 0.0])
    no_gdop = init_track(_estimate(P0, V0, gdop=None), reports, consts, 0.0)
    assert np.diag(no_gdop.P)[:3] == pytest.approx([100.0] * 3)


def test_ekf_converges_on_exact_cv_measurements():
    x = np.concatenate([P0 + [3.0, -2.0, 1.5], [0.0, 0.0, 0.0]])
    P = np.diag([9.0] * 3 + [400.0] * 3)
    x, P = _run_filter(x, P, 40, lambda k: MONO4)
    p_true, v_true = P0 + V0 * DT * 40, V0
    assert np.linalg.norm(x[:3] - p_true) < 0.1
    assert np.linalg.norm(x[3:] - v_true) < 0.1
    # Joseph form keeps P symmetric positive definite.
    assert np.allclose(P, P.T) and np.all(np.linalg.eigvalsh(P) > 0.0)


@pytest.mark.parametrize("n_links", [1, 2])
def test_one_or_two_links_beat_pure_coasting(n_links):
    # A track with a 1 m/s velocity error: coasting drifts 0.5 m per frame,
    # while even one or two links (range + Doppler) keep pulling it back.
    x0 = np.concatenate([P0, V0 + [1.0, 0.0, 0.0]])
    P = np.diag([0.25] * 3 + [25.0] * 3)
    frames = 20
    p_true = P0 + V0 * DT * frames
    upd, _ = _run_filter(x0, P, frames, lambda k: MONO4[:n_links])
    coast, _ = _run_filter(x0, P, frames, lambda k: MONO4[:n_links], update=False)
    err_upd = np.linalg.norm(upd[:3] - p_true)
    err_coast = np.linalg.norm(coast[:3] - p_true)
    assert err_coast == pytest.approx(10.0, abs=1e-9)
    assert err_upd < 0.5 * err_coast, (err_upd, err_coast)


def test_gate_rejects_a_50_m_range_outlier():
    consts = _consts()
    x = np.concatenate([P0, V0])
    P = np.diag([0.25] * 3 + [1.0] * 3)
    bad = _reports(P0, V0, MONO4[:1], range_offset=(MONO4[0], 50.0))
    meas = track_measurements(bad, "uav_01", NODES, consts)
    x1, P1, accepted, gated = ekf_update(x, P, meas, LAM, 16.0)
    assert (accepted, gated) == (1, 1)  # the range is gated, the Doppler accepted
    doppler_only, P_d, _, _ = ekf_update(x, P, meas[1:], LAM, 16.0)
    assert np.array_equal(x1, doppler_only) and np.array_equal(P1, P_d)
    # Alone, the outlier leaves the state exactly as it was.
    x2, P2, accepted, gated = ekf_update(x, P, meas[:1], LAM, 16.0)
    assert (accepted, gated) == (0, 1)
    assert np.array_equal(x2, x) and np.array_equal(P2, P)
    # A huge gate takes it.
    assert ekf_update(x, P, meas[:1], LAM, 1e6)[2] == 1


def test_chi2_3dof_gate_matches_the_1dof_tail():
    for g in (1.0, 9.0, 16.0, 100.0):
        x = chi2_3dof_gate(g)
        z, zx = math.sqrt(g / 2.0), math.sqrt(x / 2.0)
        tail3 = math.erfc(zx) + math.sqrt(2.0 * x / math.pi) * math.exp(-x / 2.0)
        assert tail3 == pytest.approx(math.erfc(z), rel=1e-9)
    assert chi2_3dof_gate(16.0) == pytest.approx(22.0613, abs=1e-4)
    assert math.isfinite(chi2_3dof_gate(1e6)) and chi2_3dof_gate(1e6) > 1e6


# ------------------------------------------------------------- scenarios


def _strip(est) -> dict:
    return est.model_dump(exclude=TRACK_FIELDS)


def test_scenario_tracking_statuses_and_summary():
    result = _scenario(
        _trp_scene(), _config(), num_frames=8, cpi_pulses=4096, measurement_noise=True,
        tracking={"enabled": True},
    )
    ests = [f.sensing.estimates[0] for f in result.frames]
    assert [e.track_status for e in ests] == ["init"] + ["tracking"] * 7
    assert (ests[0].track_updates, ests[0].track_gated) == (0, 0)
    assert ests[0].track_position == pytest.approx(ests[0].position_est)
    for e in ests[1:]:
        assert e.track_updates == 2 * e.n_links_detected and e.track_gated == 0
        assert e.track_position_error_m == pytest.approx(
            math.dist(e.track_position, e.position_true)
        )
        assert e.track_position_std_m < 2.0 and e.track_position_error_m < 2.0
    summary = result.metadata["sensing"]
    stats = summary["targets"]["uav_01"]
    assert stats["tracked_frames"] == 8
    assert stats["coasting_frames"] == stats["lost_frames"] == 0
    assert stats["frames_track_without_fusion"] == 0
    errs = [e.track_position_error_m for e in ests]
    assert stats["median_track_position_error_m"] == pytest.approx(float(np.median(errs)))
    assert stats["p90_track_position_error_m"] == pytest.approx(float(np.percentile(errs, 90)))
    assert stats["frames_improved_over_fusion"] == sum(
        e.track_position_error_m < e.position_error_m for e in ests
    )
    assert summary["median_track_position_error_m"] == stats["median_track_position_error_m"]
    assert summary["tracking_model"] == TRACKING_MODEL
    assert summary["options"]["tracking"]["enabled"] is True
    assert "detector" not in summary["options"] and "pfa" not in summary["options"]


def test_coast_then_lost_then_reinit(monkeypatch):
    real = sensing_track.link_report
    blackout = set(range(2, 8))

    def blind(*args, **kwargs):
        report = real(*args, **kwargs)
        if args[6] in blackout:  # frame_index
            return report.model_copy(update={"detected": False, "reason": "below_threshold"})
        return report

    monkeypatch.setattr(sensing_track, "link_report", blind)
    result = _scenario(
        _trp_scene(), _config(), num_frames=10, cpi_pulses=4096,
        tracking={"enabled": True, "coast_max_frames": 3},
    )
    ests = [f.sensing.estimates[0] for f in result.frames]
    assert [e.track_status for e in ests] == [
        "init", "tracking", "coasting", "coasting", "coasting", "lost", "lost", "lost",
        "init", "tracking",
    ]
    # Coasting frames carry the prediction; the drop frame still reports counts.
    for e in ests[2:5]:
        assert (e.track_updates, e.track_gated) == (0, 0)
        assert e.track_position is not None and e.track_position_std_m is not None
    stds = [e.track_position_std_m for e in ests[1:5]]
    assert stds == sorted(stds)  # uncertainty grows while coasting
    assert ests[5].track_position is None and (ests[5].track_updates, ests[5].track_gated) == (0, 0)
    assert ests[6].track_updates is None and ests[6].track_position is None
    assert ests[8].track_position == pytest.approx(ests[8].position_est)
    stats = result.metadata["sensing"]["targets"]["uav_01"]
    assert (stats["tracked_frames"], stats["coasting_frames"], stats["lost_frames"]) == (7, 3, 3)
    assert stats["frames_track_without_fusion"] == 3


def test_no_track_before_the_first_ok_fusion(monkeypatch):
    real = sensing_track.link_report

    def late(*args, **kwargs):
        report = real(*args, **kwargs)
        if args[6] < 2:
            return report.model_copy(update={"detected": False, "reason": "below_threshold"})
        return report

    monkeypatch.setattr(sensing_track, "link_report", late)
    result = _scenario(_trp_scene(), _config(), num_frames=3, cpi_pulses=4096,
                       tracking={"enabled": True})
    ests = [f.sensing.estimates[0] for f in result.frames]
    assert [e.track_status for e in ests] == ["none", "none", "init"]
    assert all(e.track_position is None and e.track_updates is None for e in ests[:2])
    assert result.metadata["sensing"]["targets"]["uav_01"]["lost_frames"] == 0


def test_track_restarts_after_a_corner():
    # The trajectory turns 90 degrees at t = 12 s (frame 24). Within frame 25
    # the track's prediction falls outside the fusion's gate: it restarts from
    # the fusion instead of drifting on a velocity the gated Doppler never fixes.
    result = _scenario(
        _trp_scene(), _config(), num_frames=30, cpi_pulses=4096, measurement_noise=True,
        noise_seed=3, tracking={"enabled": True},
    )
    ests = [f.sensing.estimates[0] for f in result.frames]
    statuses = [e.track_status for e in ests]
    assert statuses[0] == "init" and statuses.count("init") == 2
    assert 24 <= statuses.index("init", 1) <= 26
    assert all(e.track_position_error_m < 2.0 for e in ests[26:])


@pytest.mark.parametrize("seed,outlier", [(0, 2), (2, 7), (5, 3)])
def test_no_restart_on_a_straight_path(seed, outlier):
    # Each seed draws one fusion outlier (3-7 m off) outside the 3-D restart
    # gate while the gated update accepts every raw scalar of that frame: no
    # manoeuvre, so the track must update through it, not restart from it.
    drone = Actor(
        id="uav_01", kind="uav", position=[-60.0, -20.0, 60.0],
        trajectory=ActorTrajectory(
            waypoints=[[-60.0, -20.0, 60.0], [140.0, 80.0, 60.0]], speed_m_s=8.0
        ),
        sensing={"model": "tr38901", "object_type": "uav-small-size"},
    )
    result = _scenario(
        _trp_scene(actors=[drone]), _config(), num_frames=10, cpi_pulses=4096,
        threshold_db=5.0, measurement_noise=True, noise_seed=seed,
        tracking={"enabled": True},
    )
    ests = [f.sensing.estimates[0] for f in result.frames]
    assert [e.track_status for e in ests] == ["init"] + ["tracking"] * 9
    assert all(e.track_gated == 0 for e in ests)
    bad = ests[outlier]
    assert bad.status == "ok" and bad.position_error_m > 3.0
    assert bad.track_position_error_m < 1.5


def test_track_dropped_once_its_sigma_exceeds_the_cap(monkeypatch):
    # Frames 2-11 keep one detected link: every scalar is accepted (no coast),
    # but the unobserved directions grow P until sqrt(trace(P_pos)) passes
    # max_position_std_m. The track is then lost and restarts at frame 12,
    # the next frame with >= 3 links (an ok fusion).
    real = sensing_track.link_report
    single = set(range(2, 12))

    def one_link(*args, **kwargs):
        report = real(*args, **kwargs)
        if args[6] in single and (args[0], args[1]) != ("trp1", "trp1_rx"):
            return report.model_copy(update={"detected": False, "reason": "below_threshold"})
        return report

    monkeypatch.setattr(sensing_track, "link_report", one_link)
    cap = 1.5
    result = _scenario(
        _trp_scene(), _config(), num_frames=14, cpi_pulses=4096,
        tracking={"enabled": True, "max_position_std_m": cap},
    )
    ests = [f.sensing.estimates[0] for f in result.frames]
    statuses = [e.track_status for e in ests]
    drop = statuses.index("lost")
    assert 2 <= drop < 12
    assert statuses[:drop] == ["init"] + ["tracking"] * (drop - 1)
    assert statuses[drop:12] == ["lost"] * (12 - drop)
    assert statuses[12:] == ["init", "tracking"]
    # Within the cap before the drop; the drop frame still reports its counts.
    assert all(e.track_position_std_m <= cap for e in ests[:drop])
    assert ests[drop - 1].track_position_std_m > ests[1].track_position_std_m
    assert ests[drop].track_position is None and ests[drop].track_updates == 2
    stats = result.metadata["sensing"]["targets"]["uav_01"]
    assert stats["lost_by_std_frames"] == 1 and stats["lost_frames"] == 12 - drop
    assert result.metadata["sensing"]["options"]["tracking"]["max_position_std_m"] == cap

    # The default cap (25 m) never fires on this short stretch: same run
    # without the drop, every frame tracked.
    wide = _scenario(_trp_scene(), _config(), num_frames=14, cpi_pulses=4096,
                     tracking={"enabled": True})
    assert [f.sensing.estimates[0].track_status for f in wide.frames] == (
        ["init"] + ["tracking"] * 13
    )
    assert wide.metadata["sensing"]["targets"]["uav_01"]["lost_by_std_frames"] == 0


def test_use_as_prior_only_changes_the_fusion_start(monkeypatch):
    knobs = dict(num_frames=6, cpi_pulses=4096, measurement_noise=True, noise_seed=3)
    off = _scenario(_trp_scene(), _config(), **knobs)
    no_prior = _scenario(_trp_scene(), _config(), **knobs,
                         tracking={"enabled": True, "use_as_prior": False})
    for f_off, f_np in zip(off.frames, no_prior.frames):
        assert f_np.sensing.links == f_off.sensing.links
        assert [_strip(e) for e in f_np.sensing.estimates] == [
            _strip(e) for e in f_off.sensing.estimates
        ]

    real = sensing_track.solve_bistatic_position
    priors: list = []

    def spy(*args, **kwargs):
        priors.append(kwargs.get("prior"))
        return real(*args, **kwargs)

    monkeypatch.setattr(sensing_track, "solve_bistatic_position", spy)
    seeded = _scenario(_trp_scene(), _config(), **knobs, tracking={"enabled": True})
    ests = [f.sensing.estimates[0] for f in seeded.frames]
    assert priors[0] is None
    for k in range(1, len(ests)):
        # The CV prediction of the previous posterior, not the last fusion.
        prev = ests[k - 1]
        assert priors[k] == pytest.approx(
            [p + 0.5 * v for p, v in zip(prev.track_position, prev.track_velocity)], abs=1e-9
        )
    assert all(e.status == "ok" for e in ests)
