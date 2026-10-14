"""Track B #24: 2-D CA/OS-CFAR, threshold calibration and the Grid S golden
links (services/channel_synthesis.py, services/sensing_detection.py)
against independent numpy references written here (the reference side never
imports the module under test).

Golden: backend/tests/data/trackb_gridS_golden.json = 5 links of
R_a1_h15_full_cpi10.json (frames 42 / 50) with every echo row and the
PLAIN_SIONNA_PLAN 2.5 table A (direct echo) / B (all echoes) values."""

import hashlib
import json
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.optimize import brentq
from scipy.stats import betabinom, binom

from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import PathInteraction, RayPath, SensingLinkReport
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.sensing import (
    CfarOptions,
    DetectionMapOptions,
    SensingTrackOptions,
    WaveformOptions,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import channel_synthesis as cs
from seam_studio.services import sensing_detection
from seam_studio.services.numerology import GRID_S, NR_30K_51PRB, resolve_numerology
from seam_studio.services.sensing import SensingRequestError
from seam_studio.services.sensing_detection import H0Group, H0Link, H0Source, SignalDetector
from seam_studio.services.sensing_track import detection_constants

GOLDEN = Path(__file__).parent / "data" / "trackb_gridS_golden.json"
P_TX = 43.0


# ------------------------------------------------------------ references


def ref_ca_alpha(n: int, pfa: float) -> float:
    return n * (pfa ** (-1.0 / n) - 1.0)


def ref_os_pfa(n: int, k: int, alpha: float) -> float:
    out = 1.0
    for i in range(k):
        out *= (n - i) / (n - i + alpha)
    return out


def ref_os_alpha(n: int, k: int, pfa: float) -> float:
    def f(a):
        return sum(math.log((n - i) / (n - i + a)) for i in range(k)) - math.log(pfa)

    return brentq(f, 0.0, 1e6, xtol=1e-14, rtol=1e-15)


def ref_cfar_stat(P, guard, train, kind, k=None):
    """Brute force: gather every training cell (circular), CA mean / OS
    k-th smallest, statistic = CUT / estimate."""
    n, m = P.shape
    g0, g1 = guard
    a0, a1 = g0 + train[0], g1 + train[1]
    rows = np.arange(n)[:, None]
    cols = np.arange(m)[None, :]
    stack = np.stack([
        P[(rows + dv) % n, (cols + du) % m]
        for dv in range(-a1, a1 + 1)
        for du in range(-a0, a0 + 1)
        if not (abs(du) <= g0 and abs(dv) <= g1)
    ])
    est = stack.mean(axis=0) if kind == "ca" else np.sort(stack, axis=0)[k - 1]
    return P / est


def ref_cfar_stat_masked(P, guard, train, kind, train_rows, k=None, pfa=None):
    """Brute force with only the cells of ``train_rows`` training: CA mean
    of the n_v valid cells mapped to N_t (1 + s/n_v)^(n_v/N_t) - N_t; OS
    k_v = ceil(k n_v / N_t)-th smallest scaled by alpha(N_t,k)/alpha(n_v,k_v)."""
    n, m = P.shape
    g0, g1 = guard
    a0, a1 = g0 + train[0], g1 + train[1]
    offsets = [(dv, du) for dv in range(-a1, a1 + 1) for du in range(-a0, a0 + 1)
               if not (abs(du) <= g0 and abs(dv) <= g1)]
    n_t = len(offsets)
    stat = np.zeros_like(P)
    for r in range(n):
        valid = [(dv, du) for dv, du in offsets if train_rows[(r + dv) % n]]
        n_v = len(valid)
        if n_v == 0:
            continue
        cells = np.stack([P[(r + dv) % n, (np.arange(m) + du) % m] for dv, du in valid])
        if kind == "ca":
            s = P[r] / cells.mean(axis=0)
            stat[r] = s if n_v == n_t else n_t * ((1 + s / n_v) ** (n_v / n_t) - 1)
        else:
            k_v = max(1, min(n_v, math.ceil(k * n_v / n_t)))
            s = P[r] / np.sort(cells, axis=0)[k_v - 1]
            stat[r] = s if n_v == n_t else s * ref_os_alpha(n_t, k, pfa) / ref_os_alpha(
                n_v, k_v, pfa)
    return stat


def ref_hann(n):
    return np.array([0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)])


def ref_map(Y, window, pad, sigma2, mean_subtract=False):
    Y = np.array(Y, dtype=complex)
    n, k = Y.shape
    if mean_subtract:
        Y = Y - Y.mean(axis=0)
    wt = ref_hann(n) if window == "hann" else np.ones(n)
    wf = ref_hann(k) if window == "hann" else np.ones(k)
    X = np.zeros((pad * n, pad * k), dtype=complex)
    X[:n, :k] = Y * np.outer(wt, wf)
    Z = np.fft.fft(np.conj(np.fft.fft(np.conj(X), axis=1)), axis=0)
    Z = np.roll(Z, pad * n // 2, axis=0)
    return np.abs(Z) ** 2 / (sigma2 * np.sum(wt**2) * np.sum(wf**2))


H0_STREAM = 0x5EA031  # H0 calibration noise; in-run noise is 0x5EA024


def ref_noise(seed, frame, tx, rx, shape, sigma2, realization=0, stream=0x5EA024):
    def id32(s):
        return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "little")

    rng = np.random.Generator(np.random.PCG64(np.random.SeedSequence(
        entropy=[seed, stream], spawn_key=(realization, frame, id32(tx), id32(rx))
    )))
    re = rng.standard_normal(shape)
    im = rng.standard_normal(shape)
    return (re + 1j * im) * math.sqrt(sigma2 / 2)


def ref_synth(rows, f, t):
    H = np.zeros((len(t), len(f)), dtype=complex)
    for gain_db, phase, delay_ns, fd in rows:
        H += (10 ** (gain_db / 20) * np.exp(1j * phase)
              * np.exp(-2j * np.pi * f[None, :] * delay_ns * 1e-9)
              * np.exp(2j * np.pi * fd * t[:, None]))
    return H


def ref_quantile(values, p):
    v = np.sort(np.asarray(values, dtype=float).ravel())
    return float(v[v.size - math.floor(v.size * p)])


class Grid:
    """Minimal numerology stand-in for noise-only maps."""

    def __init__(self, k, n):
        self.grid_k, self.grid_n = k, n
        self.pilot_spacing_hz, self.pilot_period_s = 1.0, 1.0


def _noise_maps(n_maps, shape, seed):
    rng = np.random.default_rng(seed)
    grid = Grid(shape[1], shape[0])
    opts = DetectionMapOptions(window="rect")
    for _ in range(n_maps):
        Y = (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) / math.sqrt(2)
        yield cs.dd_map(Y, grid, opts, 1.0).P


# ------------------------------------------------------------ (i) P_FA


@pytest.mark.parametrize("kind", ["ca", "os"])
def test_achieved_cell_pfa_on_a_million_noise_cells(kind):
    """250 maps of 16 x 256 iid CN(0, 1) cells (rect, no padding), guard
    [1, 1], train [2, 2] (N_t 40), pfa_cell 1e-3: the false alarms lie in
    the binomial 95 % interval; alpha reproduces the analytic P_FA."""
    opts = CfarOptions(type=kind, guard=[1, 1], train=[2, 2], pfa_cell=1e-3,
                       os_rank=30 if kind == "os" else None)
    n_t = opts.n_train()
    assert n_t == 40
    if kind == "ca":
        alpha = cs.ca_alpha(n_t, 1e-3)
        assert alpha == pytest.approx(ref_ca_alpha(40, 1e-3), rel=1e-13)
        assert (1 + alpha / n_t) ** (-n_t) == pytest.approx(1e-3, rel=1e-12)
    else:
        alpha = cs.os_alpha(n_t, 30, 1e-3)
        assert ref_os_pfa(40, 30, alpha) == pytest.approx(1e-3, rel=1e-9)
        assert alpha == pytest.approx(ref_os_alpha(40, 30, 1e-3), rel=1e-9)
    assert cs.analytic_alpha(opts) == alpha
    cells = fa = 0
    for P in _noise_maps(250, (16, 256), seed=2026):
        res = cs.cfar(P, None, opts, alpha)
        cells += P.size
        fa += int(res.detected.sum())
    assert cells == 1_024_000
    lo, hi = binom.ppf(0.025, cells, 1e-3), binom.ppf(0.975, cells, 1e-3)
    assert lo <= fa <= hi, (fa, lo, hi)


# ------------------------------------------------------------ (ii) cell sets


def _target_maps(window, pad, notch_bins):
    num = resolve_numerology(GRID_S, 0.01)
    f, t = num.axes("pilot")
    rng = np.random.default_rng(5)
    rows = [(-120.0, 0.3, 700.0, 180.0), (-127.0, -1.0, 1400.0, -250.0), (-110.0, 2.0, 300.0, 0.0)]
    sigma2 = 10 ** (-136.99 / 10)
    Y = ref_synth(rows, f, t) + (
        rng.standard_normal((16, 256)) + 1j * rng.standard_normal((16, 256))
    ) * math.sqrt(sigma2 / 2)
    opts = DetectionMapOptions(window=window, zero_pad=pad, zero_doppler_notch_bins=notch_bins)
    return cs.dd_map(Y, num, opts, sigma2)


@pytest.mark.parametrize("kind", ["ca", "os"])
@pytest.mark.parametrize("window,pad,notch_bins", [
    ("hann", 2, None), ("hann", 2, 1), ("rect", 1, 0), ("hann", 1, None),
])
def test_detection_cells_equal_brute_force(kind, window, pad, notch_bins):
    ddm = _target_maps(window, pad, notch_bins)
    opts = CfarOptions(type=kind, guard=[2, 1], train=[6, 3], pfa_cell=1e-3)
    k = math.ceil(0.75 * opts.n_train()) if kind == "os" else None
    alpha = ref_ca_alpha(opts.n_train(), 1e-3) if kind == "ca" else ref_os_alpha(
        opts.n_train(), k, 1e-3
    )
    res = cs.cfar(ddm.P, ddm.notch, opts, alpha)
    ref_stat = ref_cfar_stat(ddm.P, [2, 1], [6, 3], kind, k)
    assert np.allclose(res.stat, ref_stat, rtol=1e-11, atol=0.0)
    v = np.arange(ddm.n_pad) - ddm.n_pad // 2
    notched = (
        np.zeros(ddm.n_pad, bool) if notch_bins is None else np.abs(v) < (notch_bins + 0.5) * pad
    )
    ref_det = (ref_stat > alpha) & ~notched[:, None]
    assert set(zip(*np.nonzero(res.detected))) == set(zip(*np.nonzero(ref_det)))
    assert ref_det.sum() > 3  # the targets are there
    if kind == "os":
        harmonic = sum(1.0 / i for i in range(opts.n_train() - k + 1, opts.n_train() + 1))
        assert np.allclose(res.noise_mean, ddm.P / ref_stat / harmonic, rtol=1e-11)


@pytest.mark.parametrize("kind", ["ca", "os"])
@pytest.mark.parametrize("window,pad,notch_bins,clutter", [
    ("hann", 2, 1, "none"), ("rect", 1, None, "mean_subtract"),
    ("hann", 1, None, "mean_subtract"), ("rect", 2, 0, "mean_subtract"),
])
def test_excluded_rows_do_not_train(kind, window, pad, notch_bins, clutter):
    """Notched rows and the rows mean_subtract empties never train; the
    statistic of a CUT with fewer training cells is mapped onto N_t."""
    num = resolve_numerology(GRID_S, 0.01)
    rng = np.random.default_rng(31)
    Y = (rng.standard_normal((16, 256)) + 1j * rng.standard_normal((16, 256))) / math.sqrt(2)
    Y = Y + 3.0  # static clutter at 0 Hz
    map_opts = DetectionMapOptions(window=window, zero_pad=pad, zero_doppler_notch_bins=notch_bins,
                                   clutter_removal=clutter)
    ddm = cs.dd_map(Y, num, map_opts, 1.0)
    opts = CfarOptions(type=kind, guard=[2, 1], train=[6, 3], pfa_cell=1e-3)
    k = math.ceil(0.75 * opts.n_train()) if kind == "os" else None
    assert not ddm.train_rows.all()
    res = cs.cfar(ddm.P, ddm.notch, opts, 7.0, ddm.train_rows)
    ref = ref_cfar_stat_masked(ddm.P, [2, 1], [6, 3], kind, ddm.train_rows, k, 1e-3)
    assert np.allclose(res.stat, ref, rtol=1e-10, atol=0.0)
    assert not (res.detected & ddm.notch).any()


def test_mean_subtract_keeps_pfa_next_to_zero_doppler():
    """Noise only, rect, mean_subtract (notch 0 implied), CA N_t 200, pfa
    1e-3: the Doppler rows |v| = 1..5 (their training window holds the
    emptied 0 Hz row) false-alarm at the design rate (they ran ~1.9x before
    the 0 Hz row stopped training)."""
    map_opts = DetectionMapOptions(window="rect", clutter_removal="mean_subtract")
    opts = CfarOptions(pfa_cell=1e-3)
    alpha = cs.analytic_alpha(opts)
    grid = Grid(256, 80)
    rng = np.random.default_rng(7)
    v = np.arange(80) - 40
    near = (np.abs(v) >= 1) & (np.abs(v) <= 5)
    fa = trials = 0
    for _ in range(300):
        Y = (rng.standard_normal((80, 256)) + 1j * rng.standard_normal((80, 256))) / math.sqrt(2)
        ddm = cs.dd_map(Y, grid, map_opts, 1.0)
        assert ddm.notch[40].all() and not ddm.train_rows[40]
        res = cs.cfar(ddm.P, ddm.notch, opts, alpha, ddm.train_rows)
        fa += int(res.detected[near].sum())
        trials += int(near.sum()) * 256
    lo, hi = binom.ppf(0.0005, trials, 1e-3), binom.ppf(0.9995, trials, 1e-3)
    assert lo <= fa <= hi, (fa, lo, hi)


def test_cfar_window_must_fit_the_map():
    P = np.ones((16, 32))
    with pytest.raises(SensingRequestError, match="does not fit"):
        cs.cfar(P, None, CfarOptions(guard=[1, 1], train=[15, 2]), 10.0)
    with pytest.raises(SensingRequestError, match="does not fit"):
        cs.cfar(P, None, CfarOptions(guard=[1, 4], train=[2, 4]), 10.0)


def test_interpolation_recovers_an_off_grid_target():
    num = resolve_numerology(GRID_S, 0.05)
    f, t = num.axes("pilot")
    tau, fd = 1234.5e-9, 87.3
    H = ref_synth([(-100.0, 0.0, tau * 1e9, fd)], f, t)
    ddm = cs.dd_map(H, num, DetectionMapOptions(window="hann", zero_pad=4), 1e-15)
    r, u = np.unravel_index(np.argmax(ddm.P), ddm.P.shape)
    du, dv = cs.interpolate_peak(ddm.P, int(r), int(u))
    assert abs(du) <= 0.5 and abs(dv) <= 0.5
    assert ddm.delay_s(u + du) == pytest.approx(tau, abs=0.02 * num.delay_bin_s)
    assert ddm.doppler_hz(r - ddm.n_pad // 2 + dv) == pytest.approx(
        fd, abs=0.02 * num.doppler_resolution_hz
    )


# ------------------------------------------------------------ (iii) calibration


def test_calibrate_threshold_equals_numpy_quantile_index():
    rng = np.random.default_rng(9)
    values = rng.exponential(size=123_457) * rng.uniform(0.5, 2.0, size=123_457)
    for p in (1e-2, 1e-3, 2e-4):
        thr = cs.calibrate_threshold(values, p)
        assert thr == pytest.approx(ref_quantile(values, p), rel=1e-6)
        assert thr == ref_quantile(values, p)
        acc = cs.ThresholdAccumulator(values.size, p)
        for chunk in np.array_split(values, 37):
            acc.add(chunk)
        assert acc.threshold() == thr
        assert int(np.sum(values > thr)) == math.floor(values.size * p) - 1
    with pytest.raises(SensingRequestError, match="at least 20"):
        cs.calibrate_threshold(values[:1000], 1e-2)


def _small_detector(monkeypatch, *, system_pfa=None, pfa_cell=1e-3, frames=400, h0_txs=None):
    """SignalDetector with an h0_calibrated threshold from a synthetic H0
    source (one static clutter path per link) - no background solver."""
    waveform = WaveformOptions(scs_hz=30e3, n_fft=64, n_active_sc=64, symbols_per_cpi=8)
    options = SensingTrackOptions(
        enabled=True, waveform=waveform,
        detection={
            "mode": "cfar", "map": {"window": "hann"},
            "cfar": {"type": "ca", "guard": [1, 1], "train": [4, 2], "pfa_cell": pfa_cell},
            "threshold": "h0_calibrated", "h0_result_id": "h0_synthetic",
            "system_pfa": system_pfa,
        },
    )
    config = SimulationConfig(id="c", backend="mock", frequency_hz=3.5e9, bandwidth_hz=2e7,
                              noise_figure_db=7.0)
    txs = [Device(id="t1", kind="tx", position=[0, 0, 10], power_dbm=40.0),
           Device(id="t2", kind="tx", position=[50, 0, 10], power_dbm=46.0)]
    rxs = [Device(id="r1", kind="rx", position=[100, 0, 10])]
    clutter = {
        "t1": (-95.0, 0.4, 420.0, 0.0),
        "t2": (-101.0, -2.2, 260.0, 0.0),
    }

    def bg_path(tx):
        g, ph, d, fd = clutter[tx]
        return RayPath(path_id=f"bg_{tx}", tx_id=tx, rx_id="r1", path_type="reflection",
                       vertices=[[0, 0, 0], [1, 1, 0], [2, 0, 0]], power_dbm=g + 43.0,
                       path_gain_db=g, phase_rad=ph, delay_ns=d, doppler_hz=fd,
                       interactions=[PathInteraction(type="reflection", prim_id="/w",
                                                     point=[1, 1, 0])])

    h0_txs = h0_txs or ("t1", "t2")
    source = H0Source(result_id="h0_synthetic", kind="sensing_background_ensemble", groups=[
        H0Group(seed=77, realization=r, frame_indices=range(frames // 2), links=[
            *([H0Link("t1", "r1", [bg_path("t1")])] if "t1" in h0_txs else []),
            *([H0Link("t2", "r1", [bg_path("t2")], cfo_hz=15.0 * r)] if "t2" in h0_txs else []),
            H0Link("t9", "r1", [bg_path("t1")]),  # not a link of this run: ignored
        ])
        for r in range(2)
    ])
    monkeypatch.setattr(sensing_detection, "load_h0_source", lambda *a: source)
    num = resolve_numerology(waveform, options.cpi_s)
    det = SignalDetector(
        options=options, consts=detection_constants(config, options), numerology=num,
        config=config, txs=txs, rxs=rxs, project_dir=Path("."), scene=Scene(scene_id="x"),
    )
    return det, num, txs, clutter, source


@pytest.mark.parametrize("system_pfa", [None, 0.1])
def test_h0_calibrated_threshold_equals_reference(monkeypatch, system_pfa):
    det, num, txs, clutter, source = _small_detector(monkeypatch, system_pfa=system_pfa)
    # Reference: the same H0 maps rebuilt independently.
    scs, n_act, nf = 30e3, 64, 7.0
    f = (np.arange(64) - 32) * scs
    t = np.arange(8) * (0.5e-3 / 14)
    power = {d.id: d.power_dbm for d in txs}
    cell_values, frame_max = [], []
    for g in source.groups:
        for m in g.frame_indices:
            fmax = -np.inf
            for lk in g.links[:2]:
                sigma2 = 10 ** ((-174 + 10 * math.log10(scs) + nf
                                 - (power[lk.tx_id] - 10 * math.log10(n_act))) / 10)
                gain, ph, d, fd = clutter[lk.tx_id]
                Y = ref_synth([(gain, ph, d, fd + lk.cfo_hz)], f, t) + ref_noise(
                    g.seed, m, lk.tx_id, lk.rx_id, (8, 64), sigma2, realization=g.realization,
                    stream=H0_STREAM,
                )
                stat = ref_cfar_stat(ref_map(Y, "hann", 1, sigma2), [1, 1], [4, 2], "ca")
                cell_values.append(stat.ravel())
                fmax = max(fmax, float(stat.max()))
            frame_max.append(fmax)
    if system_pfa is None:
        expected = ref_quantile(np.concatenate(cell_values), 1e-3)
        n_values = 400 * 2 * 512
    else:
        expected = ref_quantile(frame_max, system_pfa)
        n_values = 400
    assert det.threshold == pytest.approx(expected, rel=1e-6)
    summary = det.summary()
    src = summary["threshold"]["source"]
    assert summary["threshold"]["kind"] == "h0_calibrated" and summary["cfar"]["alpha"] is None
    assert src["n_values"] == n_values and src["m"] == math.floor(n_values * (system_pfa or 1e-3))
    assert src["quantile_rule"] == "sorted[n - floor(n*p)]"
    assert src["links"] == ["t1>r1", "t2>r1"] and src["frames"] == 400 and src["maps"] == 800


def test_h0_calibration_rejects_too_few_exceedances(monkeypatch):
    with pytest.raises(SensingRequestError, match="at least 20"):
        _small_detector(monkeypatch, system_pfa=0.01, frames=400)


def test_h0_source_must_cover_every_link_for_system_pfa(monkeypatch):
    """The system statistic is the max over every link of a frame: an H0
    source without t2 > r1 would set the threshold too low (400); a cell
    threshold only warns."""
    with pytest.raises(SensingRequestError, match="no H0 maps for this run's links t2>r1"):
        _small_detector(monkeypatch, system_pfa=0.1, h0_txs=("t1",))
    det, *_ = _small_detector(monkeypatch, h0_txs=("t1",))
    assert any("no H0 maps for the links t2>r1" in w for w in det.warnings)
    src = det.summary()["threshold"]["source"]
    assert src["links"] == ["t1>r1"] and src["uncalibrated_links"] == ["t2>r1"]


def test_calibration_noise_never_repeats_the_run_noise(monkeypatch):
    """Seed 77 for the H0 source and the run: the calibration maps draw from
    0x5EA031, the run's maps from 0x5EA024."""
    det, *_ = _small_detector(monkeypatch)
    in_run = cs.noise_frame(77, 3, "t1", "r1", (8, 64), 1.0)
    calib = cs.noise_frame(77, 3, "t1", "r1", (8, 64), 1.0, stream=cs.H0_NOISE_STREAM)
    assert not np.allclose(in_run, calib)
    assert np.array_equal(in_run, ref_noise(77, 3, "t1", "r1", (8, 64), 1.0))
    assert np.array_equal(calib, ref_noise(77, 3, "t1", "r1", (8, 64), 1.0, stream=H0_STREAM))
    assert "0x5EA031" in det.summary()["threshold"]["source"]["noise_key"]


def test_system_threshold_held_out_pfa():
    """20,000 H0 frames (64 SC x 8 symbols, one link, rect, CA guard [1, 1]
    train [2, 1]): the system threshold from frames 0..9,999 lets through a
    held-out count inside the beta-binomial 95 % interval [74, 129]."""
    waveform = WaveformOptions(scs_hz=30e3, n_fft=64, n_active_sc=64, symbols_per_cpi=8)
    num = resolve_numerology(waveform, 0.01)
    opts = CfarOptions(type="ca", guard=[1, 1], train=[2, 1])
    map_opts = DetectionMapOptions(window="rect")
    sigma2 = num.sigma_h2(43.0, 7.0)
    stats = np.empty(20_000)
    for m in range(20_000):
        Y = cs.noise_frame(4242, m, "tx", "rx", (8, 64), sigma2)
        stat, _ = cs.cfar_statistic(cs.dd_map(Y, num, map_opts, sigma2).P, opts)
        stats[m] = stat.max()
    thr = cs.calibrate_threshold(stats[:10_000], 1e-2)
    assert thr == ref_quantile(stats[:10_000], 1e-2)
    held_out = int(np.sum(stats[10_000:] > thr))
    lo, hi = betabinom(10_000, 100, 10_000 - 100 + 1).ppf([0.025, 0.975])
    assert (lo, hi) == (74, 129)
    assert lo <= held_out <= hi, held_out
    cp_lo, cp_hi = cs.clopper_pearson(held_out, 10_000)
    assert cp_lo < held_out / 10_000 < cp_hi


def test_clopper_pearson_edges():
    from scipy.stats import beta

    assert cs.clopper_pearson(0, 100) == (0.0, pytest.approx(beta.ppf(0.975, 1, 100)))
    assert cs.clopper_pearson(100, 100) == (pytest.approx(beta.ppf(0.025, 100, 1)), 1.0)
    lo, hi = cs.clopper_pearson(5, 1000)
    assert lo == pytest.approx(beta.ppf(0.025, 5, 996)) and hi == pytest.approx(
        beta.ppf(0.975, 6, 995)
    )
    block = cs.rate_block(3, 300)
    assert block["pfa"] == 0.01 and block["ci95"][0] < 0.01 < block["ci95"][1]
    assert "dispersion" not in block


def test_cell_interval_widens_with_overdispersion():
    """Hann x2 maps correlate neighbouring cells: false alarms cluster, the
    per-map counts are overdispersed and the cell interval is the
    Clopper-Pearson one of the effective sample n / phi."""
    assert cs.dispersion([5, 5, 5, 5], [1000] * 4) == 1.0
    assert cs.dispersion([0, 0], [10, 10]) == 1.0
    assert cs.dispersion([3], [100]) == 1.0
    grid = Grid(256, 16)
    map_opts = DetectionMapOptions(window="hann", zero_pad=2)
    opts = CfarOptions(guard=[1, 1], train=[4, 2], pfa_cell=1e-3)
    alpha = cs.analytic_alpha(opts)
    rng = np.random.default_rng(3)
    counts, trials = [], []
    for _ in range(200):
        Y = (rng.standard_normal((16, 256)) + 1j * rng.standard_normal((16, 256))) / math.sqrt(2)
        res = cs.cfar(cs.dd_map(Y, grid, map_opts, 1.0).P, None, opts, alpha)
        counts.append(int(res.detected.sum()))
        trials.append(res.detected.size)
    k, n = sum(counts), sum(trials)
    p = k / n
    chi2 = sum((c - t * p) ** 2 / (t * p * (1 - p)) for c, t in zip(counts, trials))
    phi = cs.dispersion(counts, trials)
    assert phi == pytest.approx(chi2 / 199) and phi > 1.5
    plain, wide = cs.rate_block(k, n), cs.rate_block(k, n, phi)
    assert wide["dispersion"] == phi and wide["pfa"] == plain["pfa"]
    assert wide["ci95"][0] < plain["ci95"][0] < p < plain["ci95"][1] < wide["ci95"][1]
    from scipy.stats import beta
    assert wide["ci95"][1] == pytest.approx(beta.ppf(0.975, k / phi + 1, (n - k) / phi))


# ------------------------------------------------------------ detector checks


def _analytic_detector(waveform=None, cpi_s=0.01, **detection):
    waveform = waveform or WaveformOptions(
        scs_hz=30e3, n_fft=64, n_active_sc=64, symbols_per_cpi=8
    )
    cfar_opts = {"guard": [1, 1], "train": [4, 2], **detection.pop("cfar", {})}
    options = SensingTrackOptions(
        enabled=True, waveform=waveform, cpi_s=cpi_s,
        detection={"mode": "cfar", "cfar": cfar_opts, **detection},
    )
    config = SimulationConfig(id="c", backend="mock", frequency_hz=3.5e9, bandwidth_hz=2e7,
                              noise_figure_db=7.0)
    txs = [Device(id="t1", kind="tx", position=[0, 0, 10], power_dbm=40.0)]
    rxs = [Device(id="r1", kind="rx", position=[100, 0, 10])]
    num = resolve_numerology(waveform, cpi_s)
    det = SignalDetector(
        options=options, consts=detection_constants(config, options), numerology=num,
        config=config, txs=txs, rxs=rxs, project_dir=Path("."), scene=Scene(scene_id="x"),
    )
    return det, num


def test_oracle_gate_is_measured_from_the_nearest_cell():
    det, _ = _analytic_detector(oracle_gate_cells=1)
    axis = np.arange(64, dtype=float)
    for centre in (40.0, 40.3, 40.5, 40.7, 63.6):
        assert int(det._circ_mask(64, centre, axis).sum()) == 3
    assert set(np.flatnonzero(det._circ_mask(64, 63.6, axis))) == {63, 0, 1}
    det0, num = _analytic_detector(oracle_gate_cells=0, cfar={"pfa_cell": 1e-6})
    assert set(np.flatnonzero(det0._circ_mask(64, 40.3, axis))) == {40}
    # An off-grid echo is detected with gate 0 (its own cell), not a miss.
    tau = 3.4 * num.delay_bin_s
    fd = 1.3 * num.doppler_resolution_hz
    echo = RayPath(path_id="sensing_0001", tx_id="t1", rx_id="r1", path_type="sensing",
                   vertices=[[0, 0, 10], [50, 50, 60], [100, 0, 10]], power_dbm=-40.0,
                   path_gain_db=-80.0, phase_rad=0.3, delay_ns=tau * 1e9, doppler_hz=fd,
                   target_id="uav")
    report = SensingLinkReport(tx_id="t1", rx_id="r1", target_id="uav", path_id="sensing_0001",
                               num_echoes=1, bistatic_range_m=299_792_458.0 * tau,
                               doppler_hz=fd, echo_power_dbm=-40.0, snr_db=60.0,
                               detected=True, reason="detected")
    detections, (out,) = det0.frame(0, [echo], None, [report])
    assert out.detected and out.reason == "detected", out
    hit = detections[out.detection_index]
    assert (hit.cell_tau, hit.cell_nu) == (3, 1)


@pytest.mark.parametrize("waveform,detection,message", [
    # 8 symbols, a pilot every 8th: one pilot symbol.
    (WaveformOptions(scs_hz=30e3, n_fft=64, n_active_sc=64, symbols_per_cpi=8,
                     pilot={"symbol_step": 8}),
     {"map": {"window": "hann", "zero_pad": 4}, "cfar": {"guard": [1, 0], "train": [4, 1]}},
     "too small for the Hann window"),
    (None, {"map": {"window": "rect", "zero_doppler_notch_bins": 4}}, "notches every one"),
    (WaveformOptions(scs_hz=30e3, n_fft=64, n_active_sc=64, symbols_per_cpi=8,
                     pilot={"symbol_step": 8}),
     {"map": {"window": "rect", "zero_pad": 4, "clutter_removal": "mean_subtract"},
      "cfar": {"guard": [1, 0], "train": [4, 1]}},
     "no CFAR training cell"),
    (NR_30K_51PRB, {"map": {"zero_pad": 8}}, "at most 16777216"),
])
def test_degenerate_or_oversized_maps_answer_400(waveform, detection, message):
    with pytest.raises(SensingRequestError, match=message):
        _analytic_detector(waveform, cpi_s=0.05 if waveform is NR_30K_51PRB else 0.01,
                           **detection)


# ------------------------------------------------------------ Grid S golden


def _golden_links():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))["links"]


def _golden_rows(echoes):
    return [(e["path_gain_db"], e["phase_rad"], e["delay_ns"], e["doppler_hz"]) for e in echoes]


def _golden_paths(echoes):
    return [
        RayPath(path_id=e["path_id"], tx_id=e["tx_id"], rx_id=e["rx_id"], path_type="sensing",
                vertices=[[0, 0, 0], [1, 1, 1]], power_dbm=e["power_dbm"],
                path_gain_db=e["path_gain_db"], phase_rad=e["phase_rad"],
                delay_ns=e["delay_ns"], doppler_hz=e["doppler_hz"], target_id=e["target_id"])
        for e in echoes
    ]


@pytest.mark.parametrize("link", _golden_links(), ids=lambda lk: f"{lk['frame_index']}_{lk['rx_id']}")
def test_grid_s_golden_matched_filter_and_grid_peaks(link):
    """PLAIN_SIONNA_PLAN 2.5 tables A / B: matched-filter SNR and the grid
    peak (rect / Hann x 1 / x 4) within +-0.05 dB; MF(direct) = SEAM snr_db."""
    gold = json.loads(GOLDEN.read_text(encoding="utf-8"))
    num = resolve_numerology(GRID_S, gold["cpi_s"])
    f, t = num.axes("pilot")
    dfp, tp = num.pilot_spacing_hz, num.pilot_period_s
    sigma2 = num.sigma_h2(P_TX, 7.0)
    assert 10 * math.log10(sigma2) == pytest.approx(gold["noise_floor_dbm"] - P_TX, abs=1e-9)
    direct = [e for e in link["echoes"] if e["path_id"] == link["reported_path_id"]]
    tau, fd = direct[0]["delay_ns"] * 1e-9, direct[0]["doppler_hz"]
    for table, echoes in (("A_direct_only", direct), ("B_all_echoes", link["echoes"])):
        H = cs.synthesize_cfr(_golden_paths(echoes), f, t, P_TX)
        assert np.linalg.norm(H - ref_synth(_golden_rows(echoes), f, t)) <= 1e-12 * np.linalg.norm(H)
        got = [cs.matched_filter_snr_db(H, f, t, tau, fd, sigma2)]
        for window in ("rect", "hann"):
            for pad in (1, 4):
                ddm = cs.dd_map(H, num, DetectionMapOptions(window=window, zero_pad=pad), sigma2)
                u0 = tau * ddm.k_pad * dfp
                v0 = fd * ddm.n_pad * tp + ddm.n_pad // 2
                uc, vc = round(u0), round(v0)
                got.append(10 * math.log10(max(
                    ddm.P[(vc + dv) % ddm.n_pad, (uc + du) % ddm.k_pad]
                    for du in range(-pad, pad + 1)
                    for dv in range(-pad, pad + 1)
                )))
        for value, expected in zip(got, link["expected_db"][table]):
            assert value == pytest.approx(expected, abs=0.05), (table, got)
        if table == "A_direct_only":
            assert got[0] == pytest.approx(link["snr_db"], abs=0.05)
