"""Signal-level sensing maths (Track B, #22 / #24): the slow-time channel of
RayPath rows on an OFDM grid, its thermal noise, the delay-Doppler map, 2-D
CA/OS-CFAR, threshold calibration and Clopper-Pearson intervals. Pure numpy
(float64 / complex128): no solve, no Sionna, no Paths.cfr.

Amplitude convention (one for all Track B code). A path's dimensionless gain
is ``a = 10^(path_gain_db / 20) e^{j phase_rad}``; ``phase_rad`` already
holds the carrier term -2 pi f_c tau, so the channel adds only the BASEBAND
subcarrier term and the slow-time Doppler rotation:

    H[n, k] = sum_p a_p e^{-j 2 pi f_k tau_p} e^{+j 2 pi (f_D,p + cfo) t_n}

with f_k the baseband subcarrier frequency and t_n the symbol time (no delay
normalization). Per resource element Es = P_tx / n_active_sc and N0 = kT0 F
scs, so the channel-domain noise variance is sigma_h2 = N0 / Es =
N_floor(B_occ) / P_tx and a path's per-RE SNR is |a|^2 / sigma_h2 =
P_echo - N_floor(B_occ) [dB].
"""

import hashlib
import math
from dataclasses import dataclass
from typing import Any, Optional, Sequence

import numpy as np

from seam_studio.schemas.results import RayPath
from seam_studio.schemas.sensing import CfarOptions, DetectionMapOptions
from seam_studio.services.sensing import SensingRequestError

SPEED_OF_LIGHT = 299_792_458.0
# In-run CFAR and channel-export noise.
NOISE_STREAM = 0x5EA024
# H0 calibration noise (ensemble and scenario H0 sources): a stream of its
# own, so a calibration never reuses the noise a run tests on.
H0_NOISE_STREAM = 0x5EA031
# Rows whose noise variance mean_subtract lowers by more than this fraction
# do not train the CFAR.
MEAN_SUBTRACT_TRAINING_TOL = 0.01
# An H0 calibration needs at least this many exceedances (floor(n * pfa)).
MIN_EMPIRICAL_EXCEEDANCES = 20
# Cells per chunk of the streaming threshold calibration.
CALIBRATION_CHUNK = 2**24
# complex128 elements per synthesis chunk (paths x max(N, K)).
_SYNTH_CHUNK_ELEMENTS = 2**22
# float64 elements of one OS-CFAR gather (training cells x rows x K').
_OS_CHUNK_ELEMENTS = 2**24

AMPLITUDE_CONVENTION = (
    "a = 10^(path_gain_db/20) * exp(j*phase_rad), dimensionless; phase_rad already "
    "holds -2*pi*f_c*tau. path_gain_db None: power_dbm - tx_power_dbm. Es per RE = "
    "P_tx / n_active_sc, N0 per RE = -174 dBm/Hz + 10log10(scs_hz) + NF, sigma_h2 = "
    "N0 / Es (channel-domain noise variance); per-RE SNR = |a|^2 / sigma_h2. CP excluded."
)
SYNTHESIS_FORMULA = (
    "H[n, k] = sum_p a_p * exp(-j*2*pi*f_k*tau_p) * exp(+j*2*pi*(f_D,p + cfo_hz)*t_n); "
    "f_k baseband subcarrier frequency (f_hz), t_n symbol time (t_s), tau_p = delay_ns*1e-9, "
    "f_D,p = doppler_hz (positive = closing; None -> 0); no extra carrier term, no delay "
    "normalization"
)
NOISE_KEY = (
    "numpy SeedSequence(entropy=[seed, 0x5EA024], spawn_key=(realization, frame_index, "
    "id32(tx_id), id32(rx_id))), PCG64; re = standard_normal(shape), im = "
    "standard_normal(shape); w = (re + j*im) * sqrt(sigma2 / 2); id32(s) = first 4 bytes "
    "of sha256(utf-8 s), little-endian; H0 calibration noise uses 0x5EA031 instead of "
    "0x5EA024"
)


class CalibrationError(SensingRequestError):
    """An H0 threshold calibration cannot run as requested (API -> 400)."""


# ------------------------------------------------------------------ synthesis


def path_gain(p: RayPath, tx_power_dbm: float) -> complex:
    """Dimensionless complex gain of ``p`` (carrier phase included)."""
    gain_db = p.path_gain_db if p.path_gain_db is not None else p.power_dbm - tx_power_dbm
    return 10.0 ** (gain_db / 20.0) * complex(math.cos(p.phase_rad), math.sin(p.phase_rad))


def path_arrays(
    paths: Sequence[RayPath], tx_power_dbm: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(a complex128, tau_s float64, doppler_hz float64) per path."""
    gain_db = np.array(
        [
            p.path_gain_db if p.path_gain_db is not None else p.power_dbm - tx_power_dbm
            for p in paths
        ],
        dtype=np.float64,
    )
    phase = np.array([p.phase_rad for p in paths], dtype=np.float64)
    a = 10.0 ** (gain_db / 20.0) * np.exp(1j * phase)
    tau = np.array([p.delay_ns for p in paths], dtype=np.float64) * 1e-9
    fd = np.array(
        [p.doppler_hz if p.doppler_hz is not None else 0.0 for p in paths], dtype=np.float64
    )
    return a, tau, fd


def synthesize_arrays(
    a: np.ndarray,
    tau_s: np.ndarray,
    doppler_hz: np.ndarray,
    f_hz: np.ndarray,
    t_s: np.ndarray,
    *,
    cfo_hz: float = 0.0,
) -> np.ndarray:
    """complex128 H[len(t), len(f)] of path arrays (module formula). The sum
    over paths is a float64 matrix product T @ F, T[n, p] = a_p e^{j 2 pi f_D,p
    t_n}, F[p, k] = e^{-j 2 pi f_k tau_p}, in chunks of paths."""
    f = np.asarray(f_hz, dtype=np.float64)
    t = np.asarray(t_s, dtype=np.float64)
    a = np.asarray(a, dtype=np.complex128).ravel()
    tau = np.asarray(tau_s, dtype=np.float64).ravel()
    fd = np.asarray(doppler_hz, dtype=np.float64).ravel() + float(cfo_hz)
    H = np.zeros((t.size, f.size), dtype=np.complex128)
    if a.size == 0:
        return H
    step = max(1, _SYNTH_CHUNK_ELEMENTS // max(1, t.size, f.size))
    for s in range(0, a.size, step):
        e = min(a.size, s + step)
        T = a[None, s:e] * np.exp(2j * np.pi * np.multiply.outer(t, fd[s:e]))
        F = np.exp(-2j * np.pi * np.multiply.outer(tau[s:e], f))
        H += T @ F
    return H


def synthesize_cfr(
    paths: Sequence[RayPath],
    f_hz: np.ndarray,
    t_s: np.ndarray,
    tx_power_dbm: float,
    *,
    cfo_hz: float = 0.0,
) -> np.ndarray:
    """complex128 H[len(t_s), len(f_hz)] of ``paths`` (module formula); an
    empty list gives zeros."""
    if not paths:
        return np.zeros((len(t_s), len(f_hz)), dtype=np.complex128)
    a, tau, fd = path_arrays(paths, tx_power_dbm)
    return synthesize_arrays(a, tau, fd, f_hz, t_s, cfo_hz=cfo_hz)


def matched_filter_snr_db(
    H: np.ndarray,
    f_hz: np.ndarray,
    t_s: np.ndarray,
    tau_s: float,
    doppler_hz: float,
    sigma_h2: float,
) -> float:
    """10log10(|sum H conj(e^{-j2pi f tau} e^{j2pi f_D t})|^2 / (sigma_h2 K N))."""
    ref = np.exp(-2j * np.pi * np.asarray(f_hz)[None, :] * tau_s) * np.exp(
        2j * np.pi * doppler_hz * np.asarray(t_s)[:, None]
    )
    z = np.sum(H * np.conj(ref))
    return 10.0 * math.log10(abs(z) ** 2 / (sigma_h2 * H.size))


# ------------------------------------------------------------------ noise


def id32(s: str) -> int:
    return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "little")


def noise_seed_sequence(
    seed: int,
    frame_index: int,
    tx_id: str,
    rx_id: str,
    realization: int = 0,
    stream: int = NOISE_STREAM,
) -> np.random.SeedSequence:
    return np.random.SeedSequence(
        entropy=[int(seed), int(stream)],
        spawn_key=(int(realization), int(frame_index), id32(tx_id), id32(rx_id)),
    )


def noise_key(seed: Any = "seed", realization: Any = 0, stream: int = NOISE_STREAM) -> str:
    """Human-readable noise key of one (realization, frame, link)."""
    return (
        f"SeedSequence(entropy=[{seed}, 0x{int(stream):X}], spawn_key=({realization}, frame, "
        "id32(tx), id32(rx)))"
    )


def noise_frame(
    seed: int,
    frame_index: int,
    tx_id: str,
    rx_id: str,
    shape: tuple[int, ...],
    sigma2: float,
    realization: int = 0,
    stream: int = NOISE_STREAM,
) -> np.ndarray:
    """complex128 circular Gaussian noise with E|w|^2 = sigma2."""
    rng = np.random.Generator(
        np.random.PCG64(
            noise_seed_sequence(seed, frame_index, tx_id, rx_id, realization, stream)
        )
    )
    re = rng.standard_normal(shape)
    im = rng.standard_normal(shape)
    return (re + 1j * im) * math.sqrt(sigma2 / 2.0)


# ------------------------------------------------------------------ map


def hann(length: int) -> np.ndarray:
    """Periodic Hann window w[i] = 0.5 - 0.5 cos(2 pi i / L)."""
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(length) / length)


def window_loss_db(w: np.ndarray) -> float:
    """Coherent gain loss 10log10((sum w)^2 / (L sum w^2)) (Hann: -1.7609 dB)."""
    return 10.0 * math.log10(float(np.sum(w)) ** 2 / (w.size * float(np.sum(w * w))))


@dataclass(frozen=True)
class DDMap:
    """Delay-Doppler map of one pilot channel: row r <-> signed Doppler cell
    v = r - N'//2, column u = delay cell (0 .. K'-1)."""

    Z: np.ndarray  # complex128 [N', K']
    P: np.ndarray  # float64 [N', K'], noise cells mean 1
    notch: np.ndarray  # bool [N', K'], never a cell under test
    w_sum_sq: float  # sum(w_f^2) * sum(w_t^2)
    k_pad: int  # K'
    n_pad: int  # N'
    num: Any  # Numerology (pilot_spacing_hz, pilot_period_s)
    train_rows: Optional[np.ndarray] = None  # bool [N']: rows that train the CFAR

    def delay_s(self, u: float) -> float:
        return (u % self.k_pad) / (self.k_pad * self.num.pilot_spacing_hz)

    def doppler_hz(self, v: float) -> float:
        return v / (self.n_pad * self.num.pilot_period_s)

    def cell_of(self, delay_s: float, doppler_hz: float) -> tuple[float, float]:
        """Fractional (u*, v*) of a path at (delay, Doppler)."""
        return (
            delay_s * self.k_pad * self.num.pilot_spacing_hz,
            doppler_hz * self.n_pad * self.num.pilot_period_s,
        )


def notch_rows(n_pad: int, zero_pad: int, notch_bins: Optional[int]) -> np.ndarray:
    """bool [N']: row r is notched iff |r - N'//2| < (k + 0.5) * zero_pad."""
    if notch_bins is None:
        return np.zeros(n_pad, dtype=bool)
    v = np.arange(n_pad) - n_pad // 2
    return np.abs(v) < (notch_bins + 0.5) * zero_pad


def effective_notch_bins(map_opts: DetectionMapOptions) -> Optional[int]:
    """zero_doppler_notch_bins; 0 when unset with mean_subtract (its 0 Hz row
    holds no noise left to test)."""
    if map_opts.zero_doppler_notch_bins is None and map_opts.clutter_removal == "mean_subtract":
        return 0
    return map_opts.zero_doppler_notch_bins


def mean_subtract_variance(n_rows: int, map_opts: DetectionMapOptions) -> np.ndarray:
    """float [N']: noise variance of each map row after removing the
    slow-time mean, relative to without: 1 - |W(v)|^2 / (N sum w_t^2), W the
    N'-point DFT of the slow-time window (map row order)."""
    w = hann(n_rows) if map_opts.window == "hann" else np.ones(n_rows)
    W = np.fft.fftshift(np.fft.fft(w, n=map_opts.zero_pad * n_rows))
    return 1.0 - (W.real**2 + W.imag**2) / (n_rows * float(np.sum(w * w)))


def training_rows(n_rows: int, map_opts: DetectionMapOptions) -> np.ndarray:
    """bool [N']: map rows whose cells may train the CFAR: not notched and,
    with mean_subtract, noise variance lowered by at most
    MEAN_SUBTRACT_TRAINING_TOL (CA / OS assume unit-mean training noise)."""
    zp = map_opts.zero_pad
    train = ~notch_rows(zp * n_rows, zp, effective_notch_bins(map_opts))
    if map_opts.clutter_removal == "mean_subtract":
        train &= mean_subtract_variance(n_rows, map_opts) >= 1.0 - MEAN_SUBTRACT_TRAINING_TOL
    return train


def dd_map(
    Y: np.ndarray, num: Any, map_opts: DetectionMapOptions, sigma_h2: float
) -> DDMap:
    """Y[n, k] (slow time x subcarrier) -> DDMap. Delay: K'-point IFFT x K'
    (e^{+j2pi k u/K'}); Doppler: N'-point FFT (e^{-j2pi n v/N'}), shifted so
    0 Hz is row N'//2. P = |Z|^2 / (sigma_h2 sum w_f^2 sum w_t^2)."""
    Y = np.asarray(Y, dtype=np.complex128)
    n_rows, n_cols = Y.shape
    if map_opts.clutter_removal == "mean_subtract":
        Y = Y - Y.mean(axis=0, keepdims=True)
    if map_opts.window == "hann":
        w_t, w_f = hann(n_rows), hann(n_cols)
    else:
        w_t, w_f = np.ones(n_rows), np.ones(n_cols)
    w_sum_sq = float(np.sum(w_f * w_f)) * float(np.sum(w_t * w_t))
    if not w_sum_sq > 0.0:  # a one-sample Hann axis is all zero
        raise SensingRequestError(
            f"a {n_cols} x {n_rows} pilot grid is too small for the Hann window (it needs "
            "at least 2 pilot subcarriers and 2 pilot symbols; use window 'rect')"
        )
    X = Y * w_t[:, None] * w_f[None, :]
    k_pad = map_opts.zero_pad * n_cols
    n_pad = map_opts.zero_pad * n_rows
    Z = np.fft.ifft(X, n=k_pad, axis=1) * k_pad
    Z = np.fft.fft(Z, n=n_pad, axis=0)
    Z = np.fft.fftshift(Z, axes=0)
    P = (Z.real * Z.real + Z.imag * Z.imag) / (sigma_h2 * w_sum_sq)
    rows = notch_rows(n_pad, map_opts.zero_pad, effective_notch_bins(map_opts))
    notch = np.repeat(rows[:, None], k_pad, axis=1)
    return DDMap(
        Z=Z, P=P, notch=notch, w_sum_sq=w_sum_sq, k_pad=k_pad, n_pad=n_pad, num=num,
        train_rows=training_rows(n_rows, map_opts),
    )


# ------------------------------------------------------------------ CFAR


def ca_alpha(n_train: int, pfa: float) -> float:
    """CA-CFAR scale: P_FA = (1 + alpha / N_t)^(-N_t)."""
    return n_train * (pfa ** (-1.0 / n_train) - 1.0)


def os_pfa(n_train: int, k: int, alpha: float) -> float:
    """OS-CFAR (k-th smallest) P_FA = prod_{i<k} (N_t - i) / (N_t - i + alpha)."""
    i = np.arange(k, dtype=np.float64)
    return float(np.exp(np.sum(np.log((n_train - i) / (n_train - i + alpha)))))


def os_alpha(n_train: int, k: int, pfa: float) -> float:
    """OS-CFAR scale solving os_pfa(N_t, k, alpha) = pfa (monotone, brentq)."""
    from scipy.optimize import brentq

    if not 1 <= k <= n_train:
        raise SensingRequestError(f"OS-CFAR rank {k} outside 1..{n_train}")
    target = math.log(pfa)
    i = np.arange(k, dtype=np.float64)

    def f(alpha: float) -> float:
        return float(np.sum(np.log((n_train - i) / (n_train - i + alpha)))) - target

    hi = 1.0
    while f(hi) > 0.0:
        hi *= 2.0
    return float(brentq(f, 0.0, hi, xtol=1e-300, rtol=4.0 * np.finfo(float).eps, maxiter=500))


def os_rank(opts: CfarOptions) -> int:
    return opts.os_rank if opts.os_rank is not None else math.ceil(0.75 * opts.n_train())


def analytic_alpha(opts: CfarOptions) -> float:
    if opts.type == "ca":
        return ca_alpha(opts.n_train(), opts.pfa_cell)
    return os_alpha(opts.n_train(), os_rank(opts), opts.pfa_cell)


def check_cfar_fits(opts: CfarOptions, n_pad: int, k_pad: int) -> None:
    span_tau = 2 * (opts.guard[0] + opts.train[0]) + 1
    span_nu = 2 * (opts.guard[1] + opts.train[1]) + 1
    if span_tau > k_pad or span_nu > n_pad:
        raise SensingRequestError(
            f"the CFAR window ({span_tau} delay x {span_nu} Doppler cells: 2 (guard + "
            f"train) + 1) does not fit the {k_pad} x {n_pad} delay-Doppler map; lower "
            "detection.cfar.guard / train or raise detection.map.zero_pad"
        )


def _add_shifted(out: np.ndarray, src: np.ndarray, d: int, axis: int) -> None:
    """out[i] += src[(i + d) % n] along ``axis`` (a circular shift, no copy)."""
    n = src.shape[axis]
    d %= n
    if axis == 0:
        out[: n - d] += src[d:]
        if d:
            out[n - d:] += src[:d]
    else:
        out[:, : n - d] += src[:, d:]
        if d:
            out[:, n - d:] += src[:, :d]


def _shift_sum(src: np.ndarray, offsets, axis: int) -> np.ndarray:
    out = np.zeros_like(src)
    for d in offsets:
        _add_shifted(out, src, d, axis)
    return out


def training_offsets(opts: CfarOptions) -> list[tuple[int, int]]:
    """(dv, du) of every training cell, Doppler-major."""
    g0, g1 = opts.guard
    a0, a1 = g0 + opts.train[0], g1 + opts.train[1]
    return [
        (dv, du)
        for dv in range(-a1, a1 + 1)
        for du in range(-a0, a0 + 1)
        if not (abs(du) <= g0 and abs(dv) <= g1)
    ]


def _training_sum(P: np.ndarray, opts: CfarOptions) -> np.ndarray:
    # Doppler rows outside the guard band x the full delay span, plus the
    # guard band's rows x the delay span outside the guard: disjoint sums of
    # non-negative cells, so a strong cell cannot cancel its neighbours'.
    g0, g1 = opts.guard
    a0, a1 = g0 + opts.train[0], g1 + opts.train[1]
    outer_rows = _shift_sum(P, [*range(-a1, -g1), *range(g1 + 1, a1 + 1)], axis=0)
    guard_rows = _shift_sum(P, range(-g1, g1 + 1), axis=0)
    return _shift_sum(outer_rows, range(-a0, a0 + 1), axis=1) + _shift_sum(
        guard_rows, [*range(-a0, -g0), *range(g0 + 1, a0 + 1)], axis=1
    )


def _kth_smallest_training(
    P: np.ndarray, opts: CfarOptions, k: int, rows: Optional[np.ndarray] = None
) -> np.ndarray:
    """k-th smallest training cell of every cell of ``rows`` (default: all
    rows) -> float64 [len(rows), K']."""
    n_pad, k_pad = P.shape
    offsets = training_offsets(opts)
    cols = {du: np.roll(P, -du, axis=1) for du in sorted({du for _, du in offsets})}
    rows_all = np.arange(n_pad) if rows is None else np.asarray(rows, dtype=np.int64)
    out = np.empty((rows_all.size, k_pad), dtype=np.float64)
    chunk = max(1, _OS_CHUNK_ELEMENTS // (len(offsets) * k_pad))
    for s in range(0, rows_all.size, chunk):
        sel = rows_all[s:s + chunk]
        stack = np.empty((len(offsets), sel.size, k_pad))
        for i, (dv, du) in enumerate(offsets):
            stack[i] = cols[du][(sel + dv) % n_pad]
        flat = np.ascontiguousarray(stack.reshape(len(offsets), -1).T)
        out[s:s + sel.size] = np.partition(flat, k - 1, axis=1)[:, k - 1].reshape(
            sel.size, k_pad
        )
    return out


def training_counts(train_rows: np.ndarray, opts: CfarOptions) -> np.ndarray:
    """int [N']: valid training cells of a CUT in each row (the window's
    cells on rows of ``train_rows``)."""
    m = np.asarray(train_rows, dtype=np.float64)[:, None]
    return np.rint(_training_sum(m, opts)[:, 0]).astype(np.int64)


def os_rank_for(k: int, n_train: int, n_valid: int) -> int:
    """OS rank scaled to n_valid valid training cells (k at n_valid = N_t)."""
    return max(1, min(n_valid, math.ceil(k * n_valid / n_train)))


@dataclass(frozen=True)
class CfarResult:
    stat: np.ndarray  # float64 [N', K']: P / CFAR noise estimate
    noise_mean: np.ndarray  # float64 [N', K']: unbiased noise mean
    detected: np.ndarray  # bool [N', K']


def _harmonic(n: int, k: int) -> float:
    return sum(1.0 / i for i in range(n - k + 1, n + 1))


def cfar_statistic(
    P: np.ndarray, opts: CfarOptions, train_rows: Optional[np.ndarray] = None
) -> tuple[np.ndarray, np.ndarray]:
    """(statistic, unbiased noise mean) of every cell. CA: P / mean(train);
    OS: P / x_(k), noise mean x_(k) / sum_{i=N_t-k+1}^{N_t} 1/i.

    ``train_rows`` (bool [N']): only cells on those rows train. A CUT left
    with n_v < N_t valid cells uses their mean (CA) or their k_v-th smallest,
    k_v = ceil(k n_v / N_t) (OS), and its statistic s is mapped onto the
    N_t-cell scale so one threshold keeps P_FA per cell: CA s' = N_t ((1 +
    s / n_v)^(n_v / N_t) - 1) (the N_t-cell distribution exactly), OS s' = s
    alpha(N_t, k) / alpha(n_v, k_v) at pfa_cell. A row with no valid cell
    gets statistic 0 (only notched rows can)."""
    P = np.asarray(P, dtype=np.float64)
    check_cfar_fits(opts, P.shape[0], P.shape[1])
    n_train = opts.n_train()
    if train_rows is not None and not np.all(train_rows):
        return _masked_statistic(P, opts, np.asarray(train_rows, dtype=bool))
    if opts.type == "ca":
        est = _training_sum(P, opts) / n_train
        noise_mean = est
    else:
        k = os_rank(opts)
        est = _kth_smallest_training(P, opts, k)
        noise_mean = est / _harmonic(n_train, k)
    with np.errstate(divide="ignore", invalid="ignore"):
        stat = P / est
    empty = ~(est > 0.0)
    if empty.any():  # noiseless maps: a zero training estimate
        stat[empty] = np.where(P[empty] > 0.0, np.inf, 0.0)
    return stat, noise_mean


def _masked_statistic(
    P: np.ndarray, opts: CfarOptions, train_rows: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    n_train = opts.n_train()
    counts = training_counts(train_rows, opts)
    est = np.zeros_like(P)
    noise_mean = np.zeros_like(P)
    scale = np.ones(P.shape[0])  # statistic multiplier per row (OS)
    if opts.type == "ca":
        sums = _training_sum(P * train_rows[:, None], opts)
        ok = counts > 0
        est[ok] = sums[ok] / counts[ok, None]
        noise_mean = est
    else:
        k = os_rank(opts)
        alpha_full = os_alpha(n_train, k, opts.pfa_cell)
        masked = np.where(train_rows[:, None], P, np.inf)
        ranks = np.array([os_rank_for(k, n_train, int(c)) if c > 0 else 0 for c in counts])
        for n_v, k_v in sorted({(int(c), int(r)) for c, r in zip(counts, ranks) if c > 0}):
            sel = np.flatnonzero((counts == n_v) & (ranks == k_v))
            est[sel] = _kth_smallest_training(masked, opts, k_v, sel)
            noise_mean[sel] = est[sel] / _harmonic(n_v, k_v)
            if n_v < n_train:
                scale[sel] = alpha_full / os_alpha(n_v, k_v, opts.pfa_cell)
    with np.errstate(divide="ignore", invalid="ignore"):
        stat = P / est
    empty = ~(est > 0.0)
    if empty.any():  # noiseless maps: a zero training estimate
        stat[empty] = np.where(P[empty] > 0.0, np.inf, 0.0)
    partial = (counts > 0) & (counts < n_train)
    if opts.type == "ca":
        for n_v in np.unique(counts[partial]):
            rows = counts == n_v
            stat[rows] = n_train * ((1.0 + stat[rows] / n_v) ** (n_v / n_train) - 1.0)
    else:
        stat[partial] *= scale[partial, None]
    stat[counts == 0] = 0.0
    return stat, noise_mean


def cfar(
    P: np.ndarray,
    notch: Optional[np.ndarray],
    opts: CfarOptions,
    threshold: float,
    train_rows: Optional[np.ndarray] = None,
) -> CfarResult:
    """Detect iff statistic > threshold (strict) and the cell is not notched;
    ``train_rows``: see cfar_statistic."""
    stat, noise_mean = cfar_statistic(P, opts, train_rows)
    detected = stat > threshold
    if notch is not None:
        detected &= ~notch
    return CfarResult(stat=stat, noise_mean=noise_mean, detected=detected)


def _db(x: float) -> float:
    return 10.0 * math.log10(x) if x > 0.0 else -math.inf


def interpolate_peak(P: np.ndarray, r: int, u: int) -> tuple[float, float]:
    """(delta_u, delta_v) of a parabola through the circular neighbours of
    cell (r, u) on L = 10log10(P), per axis: d = L[-1] - 2 L[0] + L[+1],
    delta = (L[-1] - L[+1]) / (2 d) when d < 0 else 0, clipped to +-0.5."""
    n_pad, k_pad = P.shape

    def delta(lm: float, l0: float, lp: float) -> float:
        d = lm - 2.0 * l0 + lp
        if not (d < 0.0 and math.isfinite(d)):
            return 0.0
        return min(0.5, max(-0.5, 0.5 * (lm - lp) / d))

    l0 = _db(float(P[r, u]))
    du = delta(_db(float(P[r, (u - 1) % k_pad])), l0, _db(float(P[r, (u + 1) % k_pad])))
    dv = delta(_db(float(P[(r - 1) % n_pad, u])), l0, _db(float(P[(r + 1) % n_pad, u])))
    return du, dv


def snr_est_db(power: float, noise_mean: float) -> Optional[float]:
    if not (noise_mean > 0.0 and math.isfinite(noise_mean)):
        return None
    x = power / noise_mean - 1.0
    return 10.0 * math.log10(x) if x > 0.0 else None


# ------------------------------------------------------------------ calibration


def calibrate_threshold(values: np.ndarray, pfa: float) -> float:
    """sorted_ascending(values)[n - floor(n * pfa)] (the floor(n pfa)-th
    largest value; 400 when floor(n * pfa) < MIN_EMPIRICAL_EXCEEDANCES)."""
    v = np.asarray(values, dtype=np.float64).ravel()
    n = v.size
    m = _exceedances(n, pfa)
    return float(np.partition(v, n - m)[n - m])


def _exceedances(n: int, pfa: float) -> int:
    m = math.floor(n * pfa)
    if m < MIN_EMPIRICAL_EXCEEDANCES:
        raise CalibrationError(
            f"H0 calibration: {n} values x P_FA {pfa:g} = {n * pfa:.3g} expected "
            f"exceedances; at least {MIN_EMPIRICAL_EXCEEDANCES} are needed (more H0 "
            "frames, or a larger P_FA)"
        )
    return m


class ThresholdAccumulator:
    """Streaming calibrate_threshold over ``n_total`` values fed in chunks:
    keeps only the floor(n_total * pfa) largest, so the result equals the
    one-shot sort exactly."""

    def __init__(self, n_total: int, pfa: float):
        self.n_total = int(n_total)
        self.pfa = float(pfa)
        self.m = _exceedances(self.n_total, self.pfa)
        self.n = 0
        self._top = np.empty(0, dtype=np.float64)
        self._pending: list[np.ndarray] = []
        self._pending_n = 0

    def add(self, values: np.ndarray) -> None:
        v = np.asarray(values, dtype=np.float64).ravel()
        self.n += v.size
        self._pending.append(v)
        self._pending_n += v.size
        if self._pending_n >= max(CALIBRATION_CHUNK, 4 * self.m):
            self._reduce()

    def _reduce(self) -> None:
        if not self._pending:
            return
        allv = np.concatenate([self._top, *self._pending])
        self._pending, self._pending_n = [], 0
        if allv.size > self.m:
            # A copy: a slice would keep the whole partitioned buffer alive.
            allv = np.partition(allv, allv.size - self.m)[allv.size - self.m:].copy()
        self._top = allv

    def threshold(self) -> float:
        self._reduce()
        if self.n != self.n_total:
            raise CalibrationError(
                f"H0 calibration saw {self.n} values, expected {self.n_total}"
            )
        return float(self._top.min())


def clopper_pearson(k: int, n: int, alpha: float = 0.05) -> tuple[float, float]:
    """Exact binomial interval: lo = Beta(a/2; k, n-k+1) (0 if k = 0), hi =
    Beta(1-a/2; k+1, n-k) (1 if k = n)."""
    from scipy.stats import beta

    if n <= 0:
        return 0.0, 1.0
    lo = 0.0 if k <= 0 else float(beta.ppf(alpha / 2.0, k, n - k + 1))
    hi = 1.0 if k >= n else float(beta.ppf(1.0 - alpha / 2.0, k + 1, n - k))
    return lo, hi


def dispersion(counts: Sequence[int], trials: Sequence[int]) -> float:
    """Pearson dispersion phi = max(1, chi^2 / (M - 1)) of per-unit counts
    k_i of n_i trials against one binomial rate (1 for < 2 units or no
    count)."""
    k = np.asarray(counts, dtype=np.float64)
    n = np.asarray(trials, dtype=np.float64)
    use = n > 0
    k, n = k[use], n[use]
    if k.size < 2 or k.sum() <= 0.0 or k.sum() >= n.sum():
        return 1.0
    p = k.sum() / n.sum()
    chi2 = float(np.sum((k - n * p) ** 2 / (n * p * (1.0 - p))))
    return max(1.0, chi2 / (k.size - 1))


def rate_block(false_alarms: int, trials: int, phi: Optional[float] = None) -> dict:
    """False-alarm count, trials, rate and Clopper-Pearson 95 % interval;
    with ``phi`` (overdispersion of correlated cells) the interval is the
    one of the effective sample k / phi of n / phi."""
    if phi is None:
        lo, hi = clopper_pearson(false_alarms, trials)
    else:
        lo, hi = clopper_pearson_effective(false_alarms / phi, trials / phi)
    block = {
        "false_alarms": int(false_alarms),
        "trials": int(trials),
        "pfa": (false_alarms / trials) if trials else None,
        "ci95": [lo, hi],
    }
    if phi is not None:
        block["dispersion"] = float(phi)
    return block


def clopper_pearson_effective(k: float, n: float, alpha: float = 0.05) -> tuple[float, float]:
    """clopper_pearson with real-valued (effective) k and n."""
    from scipy.stats import beta

    if n <= 0.0:
        return 0.0, 1.0
    lo = 0.0 if k <= 0.0 else float(beta.ppf(alpha / 2.0, k, n - k + 1.0))
    hi = 1.0 if k >= n else float(beta.ppf(1.0 - alpha / 2.0, k + 1.0, n - k))
    return lo, hi
