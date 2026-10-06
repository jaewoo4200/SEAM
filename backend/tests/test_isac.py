"""ISAC beam trade-off: the pinned beam math (normalization, frame, conj
convention), Swerling-1 Pd, Pareto flags and slot-sharing identities, the
role split, run_isac on the mock backend, and POST /simulate/isac.

The live Sionna cross-checks (synthesis == /simulate/beamforming) are in
test_isac_sionna.py.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import ISACPoint, RayPath
from seam_studio.schemas.scene import Actor, Scene
from seam_studio.schemas.sensing import ISACRequest, SensingSimulateRequest
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.isac import (
    ISACRequestError,
    array_response,
    codebook_angles,
    codebook_weights,
    comm_beam_channel,
    pareto_flags,
    pareto_summary,
    plan_isac_roles,
    planar_positions,
    run_isac,
    snr_for_pd_db,
    tradeoff_points,
)
from seam_studio.services.project_store import load_default_library
from seam_studio.services.sensing import select_targets
from seam_studio.services.sensing_track import swerling1_pd
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm

PID = "isac_api"
F = 3.5e9
MOCK_CFG = {"id": "default", "backend": "mock", "frequency_hz": F, "reflection": False}


def _gain_db(pos, weights, orientation, aod) -> np.ndarray:
    """Beam gain over a single element for one unit path."""
    a = array_response(pos, orientation, aod)
    return 20.0 * np.log10(np.abs(np.conj(weights) @ a))


# ----------------------------------------------------------- beam math


def test_codebook_normalization():
    angles = codebook_angles(-60.0, 60.0, 5.0)
    assert len(angles) == 25 and angles[0] == -60.0 and angles[-1] == 60.0
    one = planar_positions(1, 1, 0.5, 0.5)
    w1 = codebook_weights(one, angles)
    assert np.allclose(_gain_db(one, w1, [0, 0, 0], [33.0, 12.0]), 0.0, atol=1e-12)

    pos = planar_positions(4, 4, 0.5, 0.5)
    w = codebook_weights(pos, angles)
    assert np.allclose(np.linalg.norm(w, axis=1), 1.0, atol=1e-12)
    gains = _gain_db(pos, w, [0, 0, 0], [0.0, 0.0])
    broadside = angles.index(0.0)
    assert gains[broadside] == pytest.approx(10.0 * math.log10(16), abs=1e-9)
    assert int(np.argmax(gains)) == broadside


def test_planar_positions_are_panel_centered():
    pos = planar_positions(4, 2, 0.5, 0.7)
    assert pos.shape == (3, 8)
    assert np.allclose(pos[0], 0.0)
    assert np.allclose(pos.mean(axis=1), 0.0)
    assert sorted(set(np.round(pos[1], 9))) == [-0.35, 0.35]
    assert sorted(set(np.round(pos[2], 9))) == [-0.75, -0.25, 0.25, 0.75]


def test_frame_pin_device_yaw():
    pos = planar_positions(4, 4, 0.5, 0.5)
    angles = codebook_angles(-60.0, 60.0, 5.0)
    w = codebook_weights(pos, angles)
    # Yaw 90 turns the panel broadside to world +y: a path leaving along +y
    # is local azimuth 0.
    assert np.allclose(array_response(pos, [90.0, 0.0, 0.0], [90.0, 0.0]), 1.0)
    gains = _gain_db(pos, w, [90.0, 0.0, 0.0], [90.0, 0.0])
    zero = angles.index(0.0)
    assert gains[zero] == pytest.approx(10.0 * math.log10(16), abs=1e-9)
    assert all(g < gains[zero] for i, g in enumerate(gains) if i != zero)
    # Unrotated, the same path is local azimuth +90: the sweep edge on the
    # +60 side wins, never broadside.
    flat = _gain_db(pos, w, [0.0, 0.0, 0.0], [90.0, 0.0])
    assert int(np.argmax(flat)) == len(angles) - 1
    # Asymmetric yaw tells R^T k from R k: yaw 30 + path 60 is local +30
    # (the transposed rotation would put it at local +90, the +60 edge).
    yawed = _gain_db(pos, w, [30.0, 0.0, 0.0], [60.0, 0.0])
    assert angles[int(np.argmax(yawed))] == 30.0


def test_frame_pin_device_pitch():
    # Sionna pitch sign: pitch -15 tilts the panel 15 deg UP, so a path
    # leaving at elevation +15 is on boresight (every element in phase).
    pos = planar_positions(4, 4, 0.5, 0.5)
    assert np.allclose(array_response(pos, [0.0, -15.0, 0.0], [0.0, 15.0]), 1.0)
    assert not np.allclose(array_response(pos, [0.0, 15.0, 0.0], [0.0, 15.0]), 1.0)


def test_conj_convention_no_mirror():
    pos = planar_positions(1, 4, 0.5, 0.5)
    angles = codebook_angles(-60.0, 60.0, 5.0)
    w = codebook_weights(pos, angles)
    gains = _gain_db(pos, w, [0.0, 0.0, 0.0], [30.0, 0.0])
    assert angles[int(np.argmax(gains))] == 30.0
    assert gains[angles.index(30.0)] > gains[angles.index(-30.0)] + 10.0


def test_comm_beam_channel_sums_paths_coherently():
    tx = Device(id="t", kind="tx", position=[0, 0, 0], power_dbm=30.0)
    pos = planar_positions(1, 1, 0.5, 0.5)
    w = codebook_weights(pos, [0.0])

    def path(gain_db, phase):
        return RayPath(
            path_id="p", tx_id="t", rx_id="u", path_type="los",
            vertices=[[0, 0, 0], [1, 0, 0]], power_dbm=30.0 + gain_db,
            path_gain_db=gain_db, delay_ns=1.0, phase_rad=phase, aod_deg=[0.0, 0.0],
        )

    h = comm_beam_channel([path(-60.0, 0.0), path(-60.0, math.pi)], tx, pos, w)
    assert abs(h[0]) < 1e-12  # opposite phases cancel
    h = comm_beam_channel([path(-60.0, 0.3), path(-60.0, 0.3)], tx, pos, w)
    assert 20 * math.log10(abs(h[0])) == pytest.approx(-60.0 + 20 * math.log10(2), abs=1e-9)


# ------------------------------------------------------------------ Pd


def test_swerling1_pd():
    pfa = 1e-6
    values = [swerling1_pd(s, pfa) for s in np.arange(-20.0, 40.0, 0.5)]
    assert all(b > a for a, b in zip(values, values[1:]))
    # 1 - Pd ~ -ln(Pfa) / SNR at high SNR: 1.4e-5 at 60 dB, 1.4e-7 at 80 dB.
    assert 1.0 - swerling1_pd(60.0, pfa) == pytest.approx(-math.log(pfa) / 1e6, rel=1e-4)
    assert swerling1_pd(80.0, pfa) > 1.0 - 1e-6
    assert swerling1_pd(None, pfa) == pfa
    snr = 10.0 * math.log10(math.log(pfa) / math.log(0.9) - 1.0)
    assert swerling1_pd(snr, pfa) == pytest.approx(0.9, abs=1e-9)
    assert snr_for_pd_db(0.9, pfa) == pytest.approx(snr, abs=1e-12)
    # Reference numbers of the guide.
    assert snr == pytest.approx(21.14, abs=0.01)
    assert swerling1_pd(13.0, pfa) == pytest.approx(0.517, abs=1e-3)


# ---------------------------------------------------------- trade-off


def test_pareto_flags():
    assert pareto_flags([]) == []
    assert pareto_flags([(1.0, 0.5)]) == [True]
    # (1, .5) dominated by (2, .5); (3, .1) and (2, .5) and (0, .9) trade off.
    pts = [(1.0, 0.5), (2.0, 0.5), (3.0, 0.1), (0.0, 0.9), (0.0, 0.8)]
    assert pareto_flags(pts) == [False, True, True, True, False]
    # Exact duplicates keep only the first in emission order.
    assert pareto_flags([(1.0, 0.5), (1.0, 0.5), (1.0 + 1e-15, 0.5)]) == [True, False, False]


def _dense_pareto_flags(pairs, tol=1e-12):
    """The pairwise O(n^2) definition pareto_flags must reproduce."""
    r = np.array([p[0] for p in pairs], dtype=float)
    d = np.array([p[1] for p in pairs], dtype=float)
    at_least = (r[None, :] >= r[:, None] - tol) & (d[None, :] >= d[:, None] - tol)
    better = (r[None, :] > r[:, None] + tol) | (d[None, :] > d[:, None] + tol)
    dominated = (at_least & better).any(axis=1)
    same = (np.abs(r[None, :] - r[:, None]) <= tol) & (np.abs(d[None, :] - d[:, None]) <= tol)
    earlier_duplicate = np.tril(same, k=-1).any(axis=1)
    return [bool(x) for x in ~dominated & ~earlier_duplicate]


def test_pareto_flags_match_the_pairwise_definition():
    rng = np.random.default_rng(7)
    cases = [
        # Continuous: few ties.
        list(zip(rng.uniform(0, 20, 400), rng.uniform(0, 1, 400))),
        # Coarse grid: many exact ties on one or both axes.
        list(zip(rng.integers(0, 6, 400) * 1.0, rng.integers(0, 6, 400) / 5.0)),
        # Float-noise ties (1 ulp apart) and the sensing-only shape (rate 0).
        [(5.0 + k * 4e-15, 0.9 + (k % 3) * 1e-13) for k in range(-5, 6)] + [(0.0, 1e-6)] * 4,
        [(0.0, p) for p in rng.uniform(0, 1, 50)] + [(0.0, 1e-6)] * 10,
    ]
    # ISAC-shaped: comm-beam rows (1 - rho) R + rho R at every rho.
    pts = tradeoff_points(
        ["u"], [{"u": 3.3}, {"u": 1.7}, {"u": 0.2}], 0, [25.0, 30.0, None],
        [0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0], "dual_function", 1e-6,
    )
    cases.append([(p.sum_rate_bps_hz, p.pd) for p in pts])
    for pairs in cases:
        pairs = [(float(a), float(b)) for a, b in pairs]
        assert pareto_flags(pairs) == _dense_pareto_flags(pairs)


def test_tradeoff_points_identities():
    ues = ["u1", "u2"]
    rates = [{"u1": 1.0, "u2": 2.0}, {"u1": 3.0, "u2": 0.5}, {"u1": 0.0, "u2": 0.0}]
    snr = [5.0, None, 25.0]
    ratios = [0.0, 0.25, 1.0]
    pfa = 1e-6
    for mode in ("dual_function", "time_sharing"):
        points = tradeoff_points(ues, rates, 1, snr, ratios, mode, pfa)
        assert len(points) == 1 + 2 * len(snr)
        first = points[0]
        assert (first.rho, first.beam_idx, first.sensing_snr_db) == (0.0, None, None)
        assert first.sum_rate_bps_hz == pytest.approx(3.5)
        assert first.pd == pfa
        assert [(p.rho, p.beam_idx) for p in points[1:]] == [
            (0.25, 0), (0.25, 1), (0.25, 2), (1.0, 0), (1.0, 1), (1.0, 2)
        ]
        quarter = points[1:4]
        assert quarter[0].sensing_snr_db == pytest.approx(5.0 + 10 * math.log10(0.25))
        assert quarter[1].sensing_snr_db is None and quarter[1].pd == pfa
        full = points[4:]
        for k, p in enumerate(full):
            assert p.pd == swerling1_pd(snr[k], pfa)
            if mode == "dual_function":
                # One beam serves both: the rho = 1 rows are the beams themselves.
                assert p.sum_rate_bps_hz == sum(rates[k].values())
                assert p.ue_rates_bps_hz == rates[k]
            else:
                assert p.sum_rate_bps_hz == 0.0
        if mode == "time_sharing":
            assert len({p.sum_rate_bps_hz for p in quarter}) == 1
            assert quarter[0].sum_rate_bps_hz == pytest.approx(0.75 * 3.5)
        else:
            assert quarter[2].ue_rates_bps_hz["u1"] == pytest.approx(0.75 * 3.0)
    # No UE (sensing-only tx): every rate is 0.
    solo = tradeoff_points([], [{}, {}], None, [10.0, 20.0], [0.0, 0.5], "dual_function", pfa)
    assert all(p.sum_rate_bps_hz == 0.0 for p in solo)


def test_pareto_summary():
    pfa = 1e-6
    ues = ["u"]
    rates = [{"u": 4.0}, {"u": 1.0}]
    points = tradeoff_points(ues, rates, 0, [0.0, 30.0], [0.0, 0.1, 1.0], "dual_function", pfa)
    s = pareto_summary(points, 4.0, 0.9, pfa)
    assert s.comm_only_rate_bps_hz == 4.0
    assert s.max_pd == max(p.pd for p in points)
    target = [p for p in points if p.pd >= 0.9]
    best = max(target, key=lambda p: p.sum_rate_bps_hz)
    assert s.rate_at_pd_target_bps_hz == best.sum_rate_bps_hz
    assert (s.rho_at_pd_target, s.beam_idx_at_pd_target) == (best.rho, best.beam_idx)
    assert s.rate_loss_at_pd_target_bps_hz == pytest.approx(4.0 - best.sum_rate_bps_hz)
    assert s.pd_at_95pct_rate == max(
        p.pd for p in points if p.sum_rate_bps_hz >= 0.95 * 4.0 - 1e-12
    )
    assert s.num_pareto_points == sum(p.pareto for p in points)
    unreachable = pareto_summary(points, 4.0, 0.999999999, pfa)
    assert unreachable.rate_at_pd_target_bps_hz is None
    assert unreachable.rate_loss_at_pd_target_bps_hz is None


def test_pareto_summary_rate_ties_go_to_higher_pd():
    # The trp1 case: in dual_function the comm beam has rate R at every rho,
    # so rho 0.5, 0.7 and 1.0 on beam 0 all reach Pd 0.9 at the max rate.
    # The summary names rho 1 (highest Pd, on the front), not the dominated
    # first qualifier, whatever the 1-ulp noise of (1 - rho) R + rho R.
    pfa = 1e-6
    points = tradeoff_points(
        ["u"], [{"u": 5.0}, {"u": 1.0}], 0, [25.0, 30.0],
        [0.0, 0.3, 0.5, 0.7, 1.0], "dual_function", pfa,
    )
    tied = [p for p in points if p.beam_idx == 0 and p.pd >= 0.9]
    assert [p.rho for p in tied] == [0.5, 0.7, 1.0]
    s = pareto_summary(points, 5.0, 0.9, pfa)
    assert (s.rho_at_pd_target, s.beam_idx_at_pd_target) == (1.0, 0)
    best = next(p for p in points if p.rho == 1.0 and p.beam_idx == 0)
    assert best.pareto and s.rate_at_pd_target_bps_hz == best.sum_rate_bps_hz
    assert s.rate_loss_at_pd_target_bps_hz == pytest.approx(0.0, abs=1e-12)

    def point(rate, pd, rho):
        return ISACPoint(rho=rho, beam_idx=0, sum_rate_bps_hz=rate, pd=pd)

    # Rates within 1e-12 tie: equal Pd keeps the first, higher Pd wins.
    first = pareto_summary([point(5.0, 0.95, 0.5), point(5.0 + 4e-15, 0.95, 0.7)], 5.0, 0.9, pfa)
    assert first.rho_at_pd_target == 0.5
    higher = pareto_summary(
        [point(5.0, 0.95, 0.5), point(5.0 - 4e-15, 0.97, 0.7), point(5.0 + 4e-15, 0.96, 1.0)],
        5.0, 0.9, pfa,
    )
    assert higher.rho_at_pd_target == 0.7
    # A real rate step still beats Pd.
    step = pareto_summary([point(5.0, 0.99, 0.5), point(5.1, 0.91, 0.7)], 5.1, 0.9, pfa)
    assert step.rho_at_pd_target == 0.7
    # Sensing-only tx (all rates 0): the highest-Pd point, rho 1 on the best beam.
    solo = tradeoff_points([], [{}, {}], None, [25.0, 30.0], [0.0, 0.05, 1.0], "dual_function", pfa)
    s = pareto_summary(solo, 0.0, 0.9, pfa)
    assert (s.rho_at_pd_target, s.beam_idx_at_pd_target) == (1.0, 1)


# ---------------------------------------------------------------- roles


def _dev(device_id, kind, position, **kw):
    return Device(id=device_id, kind=kind, position=list(position), **kw)


def _roles_scene(*extra):
    return Scene(
        scene_id="roles",
        devices=[
            _dev("t1", "tx", (0, 0, 20)),
            _dev("t1_rx", "rx", (0.5, 0, 20)),
            _dev("t2", "tx", (100, 0, 20)),
            _dev("t2_rx", "rx", (100, 0, 20)),
            _dev("ue1", "rx", (50, 10, 1.5)),
            _dev("near", "rx", (1.5, 0, 20)),
            *extra,
        ],
    )


def test_plan_isac_roles_auto_split():
    plan = plan_isac_roles(_roles_scene(), None, None, None)
    assert [t.id for t in plan.txs] == ["t1", "t2"]
    assert {k: v.id for k, v in plan.sensing_rx.items()} == {"t1": "t1_rx", "t2": "t2_rx"}
    # 1.5 m away is not co-located: a UE.
    assert [u.id for u in plan.ues] == ["ue1", "near"]


def test_plan_isac_roles_tx_subset_keeps_other_radars_out_of_the_ues():
    # The GUI's TX checkboxes send tx_ids: the co-located sensing rx of an
    # unselected TRP is still a radar receiver, never a served UE.
    plan = plan_isac_roles(_roles_scene(), ["t1"], None, None)
    assert [t.id for t in plan.txs] == ["t1"]
    assert {k: v.id for k, v in plan.sensing_rx.items()} == {"t1": "t1_rx"}
    assert [u.id for u in plan.ues] == ["ue1", "near"]
    scene = _mock_scene()
    for subset in (["t1"], ["t1", "t3"]):
        assert [u.id for u in plan_isac_roles(scene, subset, None, None).ues] == ["ue1", "ue2"]
    # Explicit UEs still override.
    plan = plan_isac_roles(scene, ["t1"], ["t2_rx", "ue1"], None)
    assert [u.id for u in plan.ues] == ["t2_rx", "ue1"]
    # The comm beam and rate of t1 do not change when t2 / t3 are left out.
    full = {t.tx_id: t for t in _run_mock(scene, ue_association="all").txs}["t1"]
    alone = _run_mock(scene, tx_ids=["t1"], ue_association="all").txs[0]
    assert alone.ue_ids == ["ue1", "ue2"]
    assert alone.comm_beam_idx == full.comm_beam_idx
    assert alone.pareto.comm_only_rate_bps_hz == pytest.approx(full.pareto.comm_only_rate_bps_hz)


def test_plan_isac_roles_explicit_and_errors():
    scene = _roles_scene()
    plan = plan_isac_roles(scene, ["t2"], ["ue1"], None)
    assert [t.id for t in plan.txs] == ["t2"]
    assert plan.sensing_rx["t2"].id == "t2_rx"
    assert [u.id for u in plan.ues] == ["ue1"]
    # Explicit bistatic: every tx takes the nearest of the given receivers.
    # t1_rx sits 0.5 m from t1: co-located with a tx, so not a UE either.
    plan = plan_isac_roles(scene, None, None, ["near", "t2_rx"])
    assert plan.sensing_rx["t1"].id == "near" and plan.sensing_rx["t2"].id == "t2_rx"
    assert [u.id for u in plan.ues] == ["ue1"]
    shared = plan_isac_roles(scene, None, None, ["ue1"])
    assert {v.id for v in shared.sensing_rx.values()} == {"ue1"}
    assert [u.id for u in shared.ues] == ["near"]

    cases = [
        ((["ghost"], None, None), "tx device not found: ghost"),
        ((["ue1"], None, None), "tx device not found: ue1"),
        ((None, ["t1"], None), "rx device not found: t1"),
        ((None, None, ["ghost"]), "rx device not found: ghost"),
        ((None, ["ue1"], ["ue1"]), "rx ue1 cannot be both a UE and a sensing receiver"),
    ]
    for args, message in cases:
        with pytest.raises(ISACRequestError, match=message):
            plan_isac_roles(scene, *args)
    no_ue = Scene(scene_id="x", devices=[d for d in scene.devices if d.id in ("t1", "t1_rx")])
    with pytest.raises(ISACRequestError, match="at least one UE"):
        plan_isac_roles(no_ue, None, None, None)
    assert plan_isac_roles(no_ue, None, None, None, require_ues=False).ues == []
    lonely = Scene(scene_id="x", devices=[d for d in scene.devices if d.id in ("t1", "ue1")])
    with pytest.raises(ISACRequestError, match="tx t1 has no sensing receiver"):
        plan_isac_roles(lonely, None, None, None)
    with pytest.raises(ISACRequestError, match="at least one tx"):
        plan_isac_roles(Scene(scene_id="x", devices=[]), None, None, None)


# ----------------------------------------------------------- mock solve


def _mock_scene(scene_id: str = "isac") -> Scene:
    """Three panels around a drone hovering at their height (local elevation
    0, so the azimuth-only codebook peaks at the true bearing) and two UEs
    on upper floors (shallow elevation: the broadside rows keep their gain)."""
    target_z = 20.0 - 0.5  # cuboid 1 m tall: center at z = 20
    return Scene(
        scene_id=scene_id,
        devices=[
            _dev("t1", "tx", (0, 0, 20), power_dbm=43.0, orientation_deg=[0, 0, 0]),
            _dev("t1_rx", "rx", (0, 0, 20), orientation_deg=[0, 0, 0]),
            _dev("t2", "tx", (120, 0, 20), power_dbm=43.0, orientation_deg=[180, 0, 0]),
            _dev("t2_rx", "rx", (120, 0.3, 20), orientation_deg=[180, 0, 0]),
            _dev("t3", "tx", (60, 100, 20), power_dbm=40.0, orientation_deg=[-90, 0, 0]),
            _dev("t3_rx", "rx", (60, 100, 20), orientation_deg=[-90, 0, 0]),
            _dev("ue1", "rx", (40, -20, 15)),
            _dev("ue2", "rx", (95, 10, 15)),
        ],
        actors=[
            Actor(
                id="uav_01",
                kind="uav",
                position=[55.0, 35.0, target_z],
                sensing={"model": "constant", "rcs_dbsm": -10.0, "size_m": [1.0, 1.0, 1.0]},
            )
        ],
    )


def _run_mock(scene: Scene, **request):
    req = ISACRequest(**request)
    plan = plan_isac_roles(scene, req.tx_ids, req.ue_rx_ids, req.sensing_rx_ids)
    return run_isac(
        MockBackend(), Path("."), scene, load_default_library(),
        SimulationConfig(**MOCK_CFG), req, plan, select_targets(scene, req.target_actor_ids),
    )


def test_run_isac_mock():
    scene = _mock_scene()
    result = _run_mock(scene)
    config = SimulationConfig(**MOCK_CFG)
    n0 = noise_floor_dbm(config)
    assert result.kind == "isac" and result.backend == "mock"
    assert result.noise_floor_dbm == pytest.approx(n0)
    assert result.ue_serving_tx == {"ue1": "t1", "ue2": "t2"}
    assert [t.tx_id for t in result.txs] == ["t1", "t2", "t3"]
    angles = codebook_angles(-60.0, 60.0, 5.0)
    assert result.metadata["codebook_size"] == 25
    assert result.metadata["model"].startswith("path-synthesized")
    assert "mti_min_doppler_hz" not in result.metadata
    assert result.paths is None

    # Plain mock solves on the same scene: the 1x1 anchors are their powers.
    echo = MockBackend().simulate_sensing(
        Path("."), scene, load_default_library(), config,
        SensingSimulateRequest(), select_targets(scene, None),
    )
    comm = MockBackend().simulate_paths(Path("."), scene, load_default_library(), config)
    ig = 10.0 * math.log10(4096)
    by_tx = {t.tx_id: t for t in result.txs}
    for tx in result.txs:
        dev = next(d for d in scene.devices if d.id == tx.tx_id)
        assert tx.angles_deg == angles and len(tx.beams) == 25
        assert len(tx.points) == 1 + 7 * 25
        assert tx.tx_array == [4, 4] and tx.rx_array == [4, 4]
        assert tx.target_ids == ["uav_01"]
        # Target at the panel height: the sensing beam is within one step of
        # its local azimuth.
        target = [55.0, 35.0, 20.0]
        world_az = math.degrees(math.atan2(target[1] - dev.position[1], target[0] - dev.position[0]))
        local_az = (world_az - dev.orientation_deg[0] + 180.0) % 360.0 - 180.0
        assert abs(tx.sensing_beam_angle_deg - local_az) <= 5.0, (tx.tx_id, local_az)
        rx_angle = tx.beams[tx.sensing_beam_idx].target_best_rx_angle_deg["uav_01"]
        assert abs(rx_angle - local_az) <= 5.0
        mine = next(p for p in echo.paths if p.tx_id == tx.tx_id and p.rx_id == tx.sensing_rx_id)
        assert tx.target_single_element_snr_db["uav_01"] == pytest.approx(
            mine.power_dbm - n0 + ig, abs=1e-9
        )
        for u in tx.ue_ids:
            los = next(p for p in comm.paths if p.tx_id == tx.tx_id and p.rx_id == u)
            assert tx.ue_single_element_rss_dbm[u] == pytest.approx(los.power_dbm, abs=1e-9)
        # Points: rho = 0 first, then rho ascending x beam ascending.
        assert tx.points[0].beam_idx is None and tx.points[0].pd == pytest.approx(1e-6)
        assert tx.pareto.num_pareto_points == sum(p.pareto for p in tx.points) >= 1
        assert tx.pareto.comm_only_rate_bps_hz == tx.points[0].sum_rate_bps_hz
        for k, beam in enumerate(tx.beams):
            full = tx.points[1 + 6 * 25 + k]
            assert (full.rho, full.beam_idx) == (1.0, k)
            assert full.sum_rate_bps_hz == beam.sum_rate_bps_hz
            assert full.pd == beam.pd
            assert beam.detected == (beam.sensing_snr_db >= 13.0)
    assert by_tx["t1"].ue_ids == ["ue1"] and by_tx["t2"].ue_ids == ["ue2"]
    assert by_tx["t3"].ue_ids == [] and by_tx["t3"].comm_beam_idx is None
    assert by_tx["t3"].warnings == ["tx t3 serves no UE: sensing-only trade-off"]
    assert all(p.sum_rate_bps_hz == 0.0 for p in by_tx["t3"].points)
    t1 = by_tx["t1"]
    assert t1.comm_beam_idx is not None
    assert t1.angle_gap_deg == abs(t1.comm_beam_angle_deg - t1.sensing_beam_angle_deg)
    assert t1.sensing_baseline_m == 0.0
    assert by_tx["t2"].sensing_baseline_m == pytest.approx(0.3)


def test_run_isac_mock_ue_association_all_and_paths():
    result = _run_mock(_mock_scene(), ue_association="all", include_paths=True)
    assert all(t.ue_ids == ["ue1", "ue2"] for t in result.txs)
    assert result.ue_serving_tx == {"ue1": "t1", "ue2": "t2"}
    echoes = [p for p in result.paths if p.target_id is not None]
    comm = [p for p in result.paths if p.target_id is None]
    assert echoes and comm and result.paths[: len(echoes)] == echoes
    assert result.metadata["echo_path_count"] == len(echoes)
    assert result.metadata["comm_path_count"] == len(comm)
    # 3 tx x 3 sensing rx x 1 target, then 3 tx x 2 UE LoS paths.
    assert len(echoes) == 9 and len(comm) == 6


def test_run_isac_mock_sensing_rx_frame():
    # A sensing panel turned away from its TX (yaw 60 vs 0) with its own 1x8
    # array: the best RX beam is the target's azimuth in the RX's frame, from
    # the arrival angle, not a copy of the TX side.
    scene = _mock_scene()
    scene.devices = [
        d.model_copy(update={"orientation_deg": [60.0, 0.0, 0.0]}) if d.id == "t1_rx" else d
        for d in scene.devices
    ]
    result = _run_mock(scene, tx_ids=["t1"], rx_rows=1, rx_cols=8)
    tx = result.txs[0]
    assert tx.sensing_rx_id == "t1_rx"
    assert tx.tx_array == [4, 4] and tx.rx_array == [1, 8]
    world_az = math.degrees(math.atan2(35.0, 55.0))
    assert abs(tx.sensing_beam_angle_deg - world_az) <= 5.0
    rx_angle = tx.beams[tx.sensing_beam_idx].target_best_rx_angle_deg["uav_01"]
    assert abs(rx_angle - (world_az - 60.0)) <= 5.0, rx_angle


def test_run_isac_slot_ratios_sorted_and_deduped():
    result = _run_mock(_mock_scene(), slot_ratios=[0.5, 0.1, 0.5, 1.0], sharing_mode="time_sharing")
    assert result.slot_ratios == [0.1, 0.5, 1.0]
    tx = result.txs[0]
    assert len(tx.points) == 3 * 25
    assert all(p.sum_rate_bps_hz == 0.0 for p in tx.points if p.rho == 1.0)


def test_run_isac_degraded_solve():
    class _Empty(MockBackend):
        def simulate_sensing(self, *args, **kwargs):
            result = super().simulate_sensing(*args, **kwargs)
            result.paths = []
            result.warnings.append("solver exploded")
            return result

        def simulate_paths(self, *args, **kwargs):
            result = super().simulate_paths(*args, **kwargs)
            result.paths = []
            return result

    scene = _mock_scene()
    req = ISACRequest()
    result = run_isac(
        _Empty(), Path("."), scene, load_default_library(), SimulationConfig(**MOCK_CFG),
        req, plan_isac_roles(scene, None, None, None), select_targets(scene, None),
    )
    assert "solver exploded" in result.warnings
    assert result.ue_serving_tx == {"ue1": None, "ue2": None}
    assert any("UE ue1 has no path to any selected tx; left unserved" == w for w in result.warnings)
    for tx in result.txs:
        assert tx.sensing_beam_idx is None and tx.comm_beam_idx is None
        assert all(b.sensing_snr_db is None and b.pd == req.pfa for b in tx.beams)
        assert all(p.sum_rate_bps_hz == 0.0 for p in tx.points)


def test_isac_request_validation():
    with pytest.raises(ValidationError, match="sweep_start_deg must be <= sweep_stop_deg"):
        ISACRequest(sweep_start_deg=10, sweep_stop_deg=-10)
    with pytest.raises(ValidationError, match="at most 361"):
        ISACRequest(sweep_start_deg=-90, sweep_stop_deg=90, sweep_step_deg=0.1)
    # A subnormal step overflows the beam count: still a validation error.
    with pytest.raises(ValidationError, match="too many beams; at most 361"):
        ISACRequest(sweep_start_deg=-90, sweep_stop_deg=90, sweep_step_deg=1e-310)
    with pytest.raises(ValidationError):
        ISACRequest(sweep_step_deg=math.inf)
    # Beams x nonzero slot ratios is capped (361 x 32 would be 11.5k points).
    many = [i / 31 for i in range(32)]
    with pytest.raises(ValidationError, match="361 beams x 31 nonzero slot ratios = 11191"):
        ISACRequest(sweep_start_deg=-90, sweep_stop_deg=90, sweep_step_deg=0.5, slot_ratios=many)
    assert len(ISACRequest(sweep_start_deg=-90, sweep_stop_deg=90, sweep_step_deg=0.5,
                           slot_ratios=many[:12]).slot_ratios) == 12
    with pytest.raises(ValidationError):
        ISACRequest(slot_ratios=[0.5, 1.5])
    with pytest.raises(ValidationError):
        ISACRequest(slot_ratios=[])
    with pytest.raises(ValidationError):
        ISACRequest(bogus=1)


# ------------------------------------------------------------------- API


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "ISAC", "project_id": PID})
    assert resp.status_code == 201, resp.text
    _put(api_client, _mock_scene(PID))
    return api_client


def _put(client, scene: Scene) -> None:
    put = client.put(f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json"))
    assert put.status_code == 200, put.text


def _post(client, body=None, project_id: str = PID):
    return client.post(
        f"/api/projects/{project_id}/simulate/isac",
        json={"config": MOCK_CFG} if body is None else body,
    )


def _project_dir():
    from seam_studio.api import deps

    return deps.get_store().resolve(PID)


def test_api_isac_persists_and_reloads(client):
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "isac" and body["result_id"] == "mock_isac_001"
    assert body["metadata"]["scene_hash"].startswith("sha256:")
    assert body["metadata"]["request_hash"].startswith("sha256:")
    assert len(body["txs"]) == 3
    refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    assert [(r["kind"], r["result_id"]) for r in refs] == [("isac", "mock_isac_001")]
    assert client.get(f"/api/projects/{PID}/results/isac").json() == body
    by_id = client.get(f"/api/projects/{PID}/results/isac", params={"result_id": "mock_isac_001"})
    assert by_id.status_code == 200 and by_id.json() == body
    missing = client.get(f"/api/projects/{PID}/results/isac", params={"result_id": "nope"})
    assert missing.status_code == 404


def test_api_isac_404(client):
    assert _post(client, project_id="ghost").status_code == 404
    assert _post(client, {"config_id": "nope"}).status_code == 404
    assert client.get(f"/api/projects/{PID}/results/isac").status_code == 404


def test_api_isac_400(client):
    cases = [
        ({"tx_ids": ["ghost"]}, "tx device not found: ghost"),
        ({"use_device_orientation": False}, "fixed-bearing arrays"),
        ({"target_actor_ids": ["ghost"]}, "actor not found: ghost"),
        ({"ue_rx_ids": ["t1_rx"], "sensing_rx_ids": ["t1_rx"]}, "cannot be both"),
    ]
    for extra, message in cases:
        resp = _post(client, {"config": MOCK_CFG, **extra})
        assert resp.status_code == 400 and message in resp.json()["detail"], (extra, resp.text)
    scene = _mock_scene(PID)
    scene.devices = [d for d in scene.devices if not d.id.startswith("ue")]
    _put(client, scene)
    resp = _post(client)
    assert resp.status_code == 400 and "at least one UE" in resp.json()["detail"]
    scene = _mock_scene(PID)
    scene.devices = [d for d in scene.devices if d.id != "t3_rx"]
    _put(client, scene)
    resp = _post(client)
    assert resp.status_code == 400 and "tx t3 has no sensing receiver" in resp.json()["detail"]
    # Nothing was solved or announced.
    assert client.get(f"/api/projects/{PID}/scene").json()["result_sets"] == []


def test_api_isac_409_sionna_without_rcs(client, monkeypatch):
    from seam_studio.services import availability
    from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend

    monkeypatch.setattr(SionnaBackend, "is_available", lambda self: True)
    monkeypatch.setattr(availability, "sionna_rcs_available", lambda: False)
    resp = _post(client, {"config": {**MOCK_CFG, "backend": "sionna"}})
    assert resp.status_code == 409 and "sionna-rt>=2.2" in resp.json()["detail"]
    auto = _post(client, {"config": {**MOCK_CFG, "backend": "auto"}})
    assert auto.status_code == 200, auto.text
    assert auto.json()["backend"] == "mock"
    assert any("needs >=2.2" in w for w in auto.json()["warnings"])


def test_api_isac_422(client):
    assert _post(client, {"sweep_start_deg": 10, "sweep_stop_deg": -10}).status_code == 422
    assert _post(client, {"slot_ratios": [0.2, 1.5]}).status_code == 422
    assert _post(client, {"tx_rows": 0}).status_code == 422
    assert _post(client, {"bogus": 1}).status_code == 422
    assert _post(client, {"sweep_step_deg": 1e-310}).status_code == 422
    # Python's json accepts the Infinity literal: rejected, not a 1-beam run.
    resp = client.post(
        f"/api/projects/{PID}/simulate/isac",
        content=json.dumps({"config": MOCK_CFG, "sweep_step_deg": math.inf}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text


def test_api_isac_include_paths(client):
    body = _post(client, {"config": MOCK_CFG, "include_paths": True}).json()
    paths = body["paths"]
    echoes = [p for p in paths if p["target_id"] is not None]
    assert echoes and paths[: len(echoes)] == echoes
    assert all(p["path_type"] == "sensing" for p in echoes)
    assert all(p["target_id"] is None for p in paths[len(echoes):])


def test_api_isac_request_hash_pins_slot_ratios(client):
    first = _post(client, {"config": MOCK_CFG, "slot_ratios": [0.0, 0.5]}).json()["metadata"]
    second = _post(client, {"config": MOCK_CFG, "slot_ratios": [0.0, 0.25]}).json()["metadata"]
    for key in ("scene_hash", "rf_assignment_hash", "sim_config_hash"):
        assert first[key] == second[key], key
    assert first["request_hash"] != second["request_hash"]
    from seam_studio.api.simulate import _sha256

    assert second["request_hash"] == _sha256(second["request"])


def test_api_isac_prune(client):
    assert _post(client).status_code == 200
    target = _project_dir() / "results" / "mock_isac_001.json"
    assert target.is_file()
    resp = client.post(
        f"/api/projects/{PID}/results/prune", json={"kinds": ["isac"], "keep_latest": 0}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == ["mock_isac_001"]
    assert not target.exists()
