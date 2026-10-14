"""Track B #22 / #23.6: slow-time channel synthesis, noise keys and the
delay-Doppler map (services/channel_synthesis.py) against independent numpy
references written here (the reference side never imports the module under
test). Golden T1 numbers: paper/blindspot_atb/trackB/g1_results.json."""

import hashlib
import math

import numpy as np
import pytest

from seam_studio.schemas.results import PathInteraction, RayPath
from seam_studio.schemas.sensing import DetectionMapOptions
from seam_studio.services import channel_synthesis as cs
from seam_studio.services.numerology import GRID_S, NR_30K_51PRB, resolve_numerology

C = 299_792_458.0
F_C = 3.5e9
P_TX = 43.0
NF = 7.0

# T1 free-space golden (G1): TX (0,0,10), bistatic RX (300,0,10), target
# (112.5, 157.619, 60) moving (10,0,0) m/s.
T1_BISTATIC_GAIN_DB = -161.11064283583022
T1_BISTATIC_TAU_S = 1.5010384283916843e-06
T1_BISTATIC_DOPPLER_HZ = 21.890143747378726


# ------------------------------------------------------------ references


def ref_grid_s(cpi_s: float = 0.01):
    """(f_k, t_n, pilot spacing, pilot period): 1280 SC x 15.625 kHz centred,
    comb-5 pilots on every 9th 1/14400 s symbol."""
    t_sym = 1.0 / 14_400
    n_sym = math.floor(cpi_s / t_sym + 1e-9)
    k = math.ceil(1280 / 5)
    n = math.ceil(n_sym / 9)
    f = (np.arange(k) * 5 - 640) * 15_625.0
    t = np.arange(n) * 9 * t_sym
    return f, t, 5 * 15_625.0, 9 * t_sym


def ref_grid_nr(cpi_s: float = 0.01):
    """NR 30 kHz, 51 PRB (612 SC), every RE a pilot, T_sym = 0.5 ms / 14."""
    t_sym = 0.5e-3 / 14
    n = math.floor(cpi_s / t_sym + 1e-9)
    f = (np.arange(612) - 306) * 30e3
    t = np.arange(n) * t_sym
    return f, t, 30e3, t_sym


def ref_synth(rows, f, t) -> np.ndarray:
    """rows: (gain_db, phase_rad, delay_ns, doppler_hz); float64 loop."""
    H = np.zeros((len(t), len(f)), dtype=complex)
    for gain_db, phase, delay_ns, fd in rows:
        a = 10.0 ** (gain_db / 20.0) * np.exp(1j * phase)
        H += (
            a
            * np.exp(-2j * np.pi * f[None, :] * (delay_ns * 1e-9))
            * np.exp(2j * np.pi * fd * t[:, None])
        )
    return H


def ref_hann(n: int) -> np.ndarray:
    return np.array([0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)])


def ref_map(Y, window: str, pad: int, sigma2: float, mean_subtract: bool = False):
    Y = np.array(Y, dtype=complex)
    n, k = Y.shape
    if mean_subtract:
        Y = Y - Y.mean(axis=0)
    wt = ref_hann(n) if window == "hann" else np.ones(n)
    wf = ref_hann(k) if window == "hann" else np.ones(k)
    X = np.zeros((pad * n, pad * k), dtype=complex)
    X[:n, :k] = Y * np.outer(wt, wf)
    # Z[v, u] = sum_n sum_k X e^{+j2pi k u/K'} e^{-j2pi n v/N'}
    Z = np.fft.fft(np.conj(np.fft.fft(np.conj(X), axis=1)), axis=0)
    Z = np.roll(Z, pad * n // 2, axis=0)
    return np.abs(Z) ** 2 / (sigma2 * np.sum(wt**2) * np.sum(wf**2))


def ref_sigma2(p_tx_dbm: float, nf_db: float, scs: float, n_active: int) -> float:
    n0 = -174.0 + 10 * math.log10(scs) + nf_db
    es = p_tx_dbm - 10 * math.log10(n_active)
    return 10 ** ((n0 - es) / 10)


def path(gain_db, phase, delay_ns, fd, *, pid="p", gain_known=True, power_offset=P_TX):
    return RayPath(
        path_id=pid, tx_id="a", rx_id="b", path_type="sensing",
        vertices=[[0.0, 0.0, 0.0], [1.0, 0.0, 0.0]],
        power_dbm=gain_db + power_offset,
        path_gain_db=gain_db if gain_known else None,
        phase_rad=phase, delay_ns=delay_ns, doppler_hz=fd, target_id="uav",
        interactions=[PathInteraction(type="sensing", prim_id="uav", point=[0.5, 0.0, 0.0])],
    )


def _wrap(x):
    return (np.asarray(x) + np.pi) % (2 * np.pi) - np.pi


# ------------------------------------------------------------ synthesis


@pytest.mark.parametrize("grid", ["grid_s", "nr"])
def test_synthesis_matches_independent_numpy(grid):
    f, t, _, _ = ref_grid_s() if grid == "grid_s" else ref_grid_nr()
    rng = np.random.default_rng(3)
    rows = [
        (
            float(rng.uniform(-170, -90)),
            float(rng.uniform(-np.pi, np.pi)),
            float(rng.uniform(200, 3000)),
            float(rng.uniform(-400, 400)),
        )
        for _ in range(40)
    ]
    # Half of the rows without path_gain_db: power_dbm - tx power.
    paths = [path(*r, pid=f"p{i}", gain_known=i % 2 == 0) for i, r in enumerate(rows)]
    H = cs.synthesize_cfr(paths, f, t, P_TX)
    ref = ref_synth(rows, f, t)
    assert H.shape == (len(t), len(f)) and H.dtype == np.complex128
    assert np.linalg.norm(H - ref) / np.linalg.norm(ref) < 1e-12
    assert cs.path_gain(paths[1], P_TX) == pytest.approx(
        10 ** (rows[1][0] / 20) * np.exp(1j * rows[1][1]), rel=1e-13
    )
    assert not cs.synthesize_cfr([], f, t, P_TX).any()


def test_numerology_axes_match_reference_grids():
    for preset, ref in ((GRID_S, ref_grid_s), (NR_30K_51PRB, ref_grid_nr)):
        num = resolve_numerology(preset, 0.01)
        f, t = num.axes("pilot")
        rf, rt, dfp, tp = ref()
        assert np.array_equal(f, rf) and np.allclose(t, rt, rtol=1e-15, atol=0.0)
        assert num.pilot_spacing_hz == dfp and num.pilot_period_s == pytest.approx(tp, rel=1e-15)


def test_single_los_phase_carries_the_carrier_once():
    f, t, _, _ = ref_grid_s()
    tau = 300.0 / C
    phase = math.remainder(-2 * math.pi * F_C * tau, 2 * math.pi)
    H = cs.synthesize_cfr([path(-80.0, phase, tau * 1e9, 0.0)], f, t, P_TX)
    expected = _wrap(-2 * np.pi * (F_C + f) * tau)
    assert np.max(np.abs(_wrap(np.angle(H[0]) - expected))) < 1e-9
    assert np.allclose(np.abs(H), 10 ** (-80 / 20), rtol=1e-12)


def test_doppler_sign_positive_is_closing():
    num = resolve_numerology(GRID_S, 0.05)
    f, t = num.axes("pilot")
    fd = 160.0  # 8 Doppler bins of 20 Hz exactly
    H = cs.synthesize_cfr([path(-100.0, 0.3, 1000.0, fd)], f, t, P_TX)
    step = np.angle(H[1] / H[0])
    assert np.allclose(step, math.remainder(2 * math.pi * fd * num.pilot_period_s, 2 * math.pi))
    ddm = cs.dd_map(H, num, DetectionMapOptions(window="rect"), 1.0)
    r, u = np.unravel_index(np.argmax(ddm.P), ddm.P.shape)
    v = r - ddm.n_pad // 2
    assert v > 0 and v == round(fd * num.grid_n * num.pilot_period_s)
    assert ddm.doppler_hz(v) == pytest.approx(fd, abs=num.doppler_resolution_hz / 2)


# ------------------------------------------------------------ noise


def _ref_noise(seed, frame, tx, rx, shape, sigma2, realization=0):
    def id32(s):
        return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "little")

    ss = np.random.SeedSequence(
        entropy=[seed, 0x5EA024], spawn_key=(realization, frame, id32(tx), id32(rx))
    )
    rng = np.random.Generator(np.random.PCG64(ss))
    re = rng.standard_normal(shape)
    im = rng.standard_normal(shape)
    return (re + 1j * im) * math.sqrt(sigma2 / 2)


def test_noise_frames_variance_keys_and_reproducibility():
    sigma2 = ref_sigma2(P_TX, NF, 15_625.0, 1280)
    frames = [cs.noise_frame(7, i, "ru1", "rx1", (16, 256), sigma2) for i in range(25)]
    w = np.stack(frames)
    assert w.size >= 100_000
    assert 0.98 <= float(np.mean(np.abs(w) ** 2)) / sigma2 <= 1.02
    assert abs(float(np.mean(w.real * w.imag))) / sigma2 < 0.01  # circular
    for i in (0, 13):
        assert np.array_equal(frames[i], _ref_noise(7, i, "ru1", "rx1", (16, 256), sigma2))
    assert np.array_equal(
        cs.noise_frame(7, 2, "ru1", "rx1", (16, 256), sigma2, realization=4),
        _ref_noise(7, 2, "ru1", "rx1", (16, 256), sigma2, realization=4),
    )
    again = cs.noise_frame(7, 0, "ru1", "rx1", (16, 256), sigma2)
    assert np.array_equal(again, frames[0])
    for other in (
        cs.noise_frame(8, 0, "ru1", "rx1", (16, 256), sigma2),
        cs.noise_frame(7, 0, "ru1", "rx2", (16, 256), sigma2),
        cs.noise_frame(7, 0, "ru2", "rx1", (16, 256), sigma2),
        cs.noise_frame(7, 0, "ru1", "rx1", (16, 256), sigma2, realization=1),
    ):
        assert not np.array_equal(other, frames[0])
    # Adjacent frames are uncorrelated (pooled).
    rho = abs(np.sum(w[:-1] * np.conj(w[1:]))) / math.sqrt(
        np.sum(np.abs(w[:-1]) ** 2) * np.sum(np.abs(w[1:]) ** 2)
    )
    assert rho < 0.01


# ------------------------------------------------------------ map


@pytest.mark.parametrize("window,pad,mean_subtract", [
    ("rect", 1, False), ("hann", 1, False), ("hann", 2, True), ("rect", 4, True),
])
def test_dd_map_matches_reference(window, pad, mean_subtract):
    num = resolve_numerology(GRID_S, 0.01)
    rng = np.random.default_rng(11)
    Y = rng.standard_normal((16, 256)) + 1j * rng.standard_normal((16, 256))
    opts = DetectionMapOptions(
        window=window, zero_pad=pad,
        clutter_removal="mean_subtract" if mean_subtract else "none",
    )
    ddm = cs.dd_map(Y, num, opts, 2.0)
    ref = ref_map(Y, window, pad, 2.0, mean_subtract)
    assert ddm.P.shape == (16 * pad, 256 * pad) == (ddm.n_pad, ddm.k_pad)
    assert np.allclose(ddm.P, ref, rtol=1e-10, atol=1e-12 * ref.max())
    _, _, dfp, tp = ref_grid_s()
    assert ddm.delay_s(7) == pytest.approx(7 / (256 * pad * dfp), rel=1e-15)
    assert ddm.doppler_hz(-3) == pytest.approx(-3 / (16 * pad * tp), rel=1e-15)
    v = np.arange(16 * pad) - 8 * pad
    # mean_subtract notches its 0 Hz row (notch 0) unless a notch is set.
    notched = np.abs(v) < 0.5 * pad if mean_subtract else np.zeros(v.size, bool)
    assert np.array_equal(ddm.notch.all(axis=1), notched)
    assert np.array_equal(ddm.notch.any(axis=1), notched)
    assert np.array_equal(ddm.train_rows, ref_training_rows(window, pad, 16, mean_subtract, None))


def ref_training_rows(window, pad, n, mean_subtract, notch_bins):
    """Rows that train: not notched and, with mean_subtract, a noise
    variance ||P x_v||^2 / ||x_v||^2 >= 0.99 (P = I - 11^T / N the mean
    removal, x_v[n] = w[n] e^{-j 2 pi n v / N'})."""
    n_pad = n * pad
    v = np.arange(n_pad) - n_pad // 2
    if notch_bins is None and mean_subtract:
        notch_bins = 0
    train = np.ones(n_pad, bool) if notch_bins is None else ~(np.abs(v) < (notch_bins + 0.5) * pad)
    if mean_subtract:
        w = np.array([0.5 - 0.5 * math.cos(2 * math.pi * i / n) for i in range(n)]) if (
            window == "hann") else np.ones(n)
        proj = np.eye(n) - np.full((n, n), 1.0 / n)
        for r, vv in enumerate(v):
            x = w * np.exp(-2j * np.pi * np.arange(n) * vv / n_pad)
            g = np.linalg.norm(proj @ x) ** 2 / np.linalg.norm(x) ** 2
            train[r] &= g >= 0.99
    return train


@pytest.mark.parametrize("window,pad,expected", [
    ("rect", 1, {0}), ("hann", 1, {-1, 0, 1}),
])
def test_mean_subtract_rows_do_not_train(window, pad, expected):
    """Rect: only 0 Hz loses its noise; Hann also lowers |v| = 1 to 5/6."""
    opts = DetectionMapOptions(window=window, zero_pad=pad, clutter_removal="mean_subtract")
    train = cs.training_rows(16, opts)
    v = np.arange(16 * pad) - 8 * pad
    assert set(v[~train].tolist()) == expected
    var = cs.mean_subtract_variance(16, opts)
    if window == "hann":
        assert var[8] == pytest.approx(1 / 3) and var[7] == var[9] == pytest.approx(5 / 6)
    assert np.array_equal(train, ref_training_rows(window, pad, 16, True, None))
    for pad2, notch in ((2, None), (4, 1), (2, 2)):
        o = DetectionMapOptions(window=window, zero_pad=pad2, clutter_removal="mean_subtract",
                                zero_doppler_notch_bins=notch)
        assert np.array_equal(cs.training_rows(16, o), ref_training_rows(window, pad2, 16, True, notch))


def test_hann_needs_two_samples_per_axis():
    num = resolve_numerology(GRID_S, 0.01)
    with pytest.raises(cs.SensingRequestError, match="too small for the Hann window"):
        cs.dd_map(np.ones((1, 256), complex), num, DetectionMapOptions(window="hann"), 1.0)
    ddm = cs.dd_map(np.ones((1, 256), complex), num, DetectionMapOptions(window="rect"), 1.0)
    assert np.isfinite(ddm.P).all()


def test_zero_doppler_notch_rows():
    num = resolve_numerology(GRID_S, 0.01)
    Y = np.ones((16, 256), dtype=complex)
    for k, pad, expected in ((0, 1, {0}), (1, 1, {-1, 0, 1}), (1, 2, {-2, -1, 0, 1, 2})):
        ddm = cs.dd_map(Y, num, DetectionMapOptions(zero_pad=pad, zero_doppler_notch_bins=k), 1.0)
        rows = {r - ddm.n_pad // 2 for r in range(ddm.n_pad) if ddm.notch[r].all()}
        assert rows == expected
        assert not ddm.notch[~ddm.notch.any(axis=1)].any()


@pytest.mark.parametrize("preset,ref", [(GRID_S, ref_grid_s), (NR_30K_51PRB, ref_grid_nr)])
@pytest.mark.parametrize("window", ["rect", "hann"])
def test_noiseless_peak_snr_at_bin_centre(preset, ref, window):
    """#23 acceptance: peak = per-RE SNR + 10log10(K N) + L_t + L_f (Hann
    -1.7609 dB per dimension), within 0.2 dB (bin-centred: to rounding)."""
    num = resolve_numerology(preset, 0.01)
    f, t, dfp, tp = ref()
    K, N = len(f), len(t)
    u0, v0 = 37, 3
    tau, fd = u0 / (K * dfp), v0 / (N * tp)
    gain_db, sigma2 = -120.0, 1e-11
    snr_re_db = gain_db - 10 * math.log10(sigma2)
    H = cs.synthesize_cfr([path(gain_db, 0.7, tau * 1e9, fd)], f, t, P_TX)
    ddm = cs.dd_map(H, num, DetectionMapOptions(window=window), sigma2)
    loss = 10 * math.log10(2 / 3) if window == "hann" else 0.0
    expected = snr_re_db + 10 * math.log10(K * N) + 2 * loss
    peak = 10 * math.log10(ddm.P[v0 + ddm.n_pad // 2, u0])
    assert abs(peak - expected) < 0.2
    assert abs(peak - expected) < 1e-6
    assert np.unravel_index(np.argmax(ddm.P), ddm.P.shape) == (v0 + ddm.n_pad // 2, u0)
    assert cs.window_loss_db(cs.hann(N)) == pytest.approx(10 * math.log10(2 / 3), abs=1e-12)


def test_t1_golden_echo_grid_s_hann_x8():
    num = resolve_numerology(GRID_S, 0.01)
    f, t, _, _ = ref_grid_s()
    sigma2 = ref_sigma2(P_TX, NF, 15_625.0, 1280)
    n_floor = -174 + 10 * math.log10(20e6) + NF
    assert 10 * math.log10(sigma2) == pytest.approx(n_floor - P_TX, abs=1e-9)
    phase = math.remainder(-2 * math.pi * F_C * T1_BISTATIC_TAU_S, 2 * math.pi)
    H = cs.synthesize_cfr(
        [path(T1_BISTATIC_GAIN_DB, phase, T1_BISTATIC_TAU_S * 1e9, T1_BISTATIC_DOPPLER_HZ)],
        f, t, P_TX,
    )
    ddm = cs.dd_map(H, num, DetectionMapOptions(window="hann", zero_pad=8), sigma2)
    p_echo = T1_BISTATIC_GAIN_DB + P_TX
    expected = p_echo - n_floor + 36.1236 - 3.5218
    got = 10 * math.log10(float(ddm.P.max()))
    assert -0.2 <= got - expected <= 0.05
    r, u = np.unravel_index(np.argmax(ddm.P), ddm.P.shape)
    du, dv = cs.interpolate_peak(ddm.P, int(r), int(u))
    assert ddm.delay_s(u + du) == pytest.approx(T1_BISTATIC_TAU_S, abs=0.05 * num.delay_bin_s)
    assert ddm.doppler_hz(r - ddm.n_pad // 2 + dv) == pytest.approx(
        T1_BISTATIC_DOPPLER_HZ, abs=0.05 * num.doppler_resolution_hz
    )


def test_matched_filter_snr_is_per_re_snr_plus_gain():
    f, t, _, _ = ref_grid_nr()
    tau, fd, gain_db, sigma2 = 1.234e-6, 37.5, -130.0, 3e-13
    H = cs.synthesize_cfr([path(gain_db, -1.1, tau * 1e9, fd)], f, t, P_TX)
    mf = cs.matched_filter_snr_db(H, f, t, tau, fd, sigma2)
    expected = gain_db - 10 * math.log10(sigma2) + 10 * math.log10(len(f) * len(t))
    assert mf == pytest.approx(expected, abs=1e-9)
