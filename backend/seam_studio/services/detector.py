"""Square-law detector models and Monte Carlo Pd / Pfa (ISAC Phase C).

Every SNR here is the POST-integration SNR, noise-normalized: the coherently
integrated noise sample is z ~ CN(0, 1), so noise alone gives y = |z|^2 ~
Exp(1) and the threshold T = -ln(Pfa) gives P(y > T) = Pfa exactly.
S = 10^(snr_db / 10) is the mean |A|^2 of the target amplitude A, which is
constant over the CPI (scan-to-scan fluctuation).

- swerling0 (steady): Pd = Q_1(sqrt(2 S), sqrt(2 T)) (2y is noncentral
  chi-square, 2 dof, lambda = 2 S).
- swerling1 (Rayleigh, |A|^2 ~ Exp(S)): Pd = Pfa^(1 / (1 + S)).
- swerling3 (one dominant scatterer, |A|^2 ~ Gamma(2, S / 2), chi-square
  4 dof): Pd = exp(-T / (1 + S/2)) (1 + T (S/2) / (1 + S/2)^2).

One identity gives all three. For a fixed |A|^2 = s, P(y > T) = P(X <= K)
with K ~ Pois(s) and X ~ Pois(T) independent (the Poisson mixture of the
noncentral chi-square). Swerling 0 sums that series. Mixing s over
Gamma(2, theta) makes K negative binomial, P(K = k) = (k + 1) p^2 q^k with
p = 1 / (1 + theta), q = theta / (1 + theta);
sum_{k >= x} (k + 1) p^2 q^k = q^x ((x + 1) p + q), and averaging over
X ~ Pois(T) (E q^X = e^{-T p}, E X q^X = T q e^{-T p}) gives
e^{-T p} (1 + T p q): Swerling 3 with theta = S / 2. Gamma(1, S) makes K
geometric and the same steps give e^{-T / (1 + S)}: Swerling 1.
"""

import math
from dataclasses import dataclass
from typing import Optional, Sequence

import numpy as np

from seam_studio.schemas.results import PdCurveModel, PdCurveResult
from seam_studio.schemas.sensing import (
    DETECTOR_MODELS,
    MAX_MC_TRIALS,
    DetectorModel,
    PdCurveRequest,
    check_empirical_trials,
)

# Poisson window of the Swerling-0 series: S +- (40 sqrt(S) + 40) holds all
# but < 1e-300 of the mass.
_SERIES_SIGMAS = 40.0
_SERIES_PAD = 40.0
# sqrt(2S) - sqrt(2T) above this: Pd = 1 to double precision (keeps the
# series window bounded).
_SURE_DETECTION_GAP = 12.0
# Rows of S per series chunk (sorted by S, so a chunk's k window is narrow).
_SERIES_ROWS = 1024
# explicit_pulses draws every pulse only while trials * cpi_pulses is at or
# below this (else the sufficient statistic).
_EXPLICIT_PULSE_BUDGET = 4_000_000
# Complex samples per Monte Carlo chunk (memory bound).
_MC_CHUNK = 1_000_000
_SQRT_HALF = math.sqrt(0.5)
# The shared noise-only pair of a request (empirical threshold, measured
# Pfa) uses this stream key next to the seed (see noise_seed).
NOISE_STREAM = 999
PD_MODEL = "square-law, coherent integration, scan-to-scan fluctuation"


class DetectorError(ValueError):
    """Bad detector input (API -> 400)."""


def _check_model(model: str) -> None:
    if model not in DETECTOR_MODELS:
        raise DetectorError(f"unknown detector model {model!r}; one of {list(DETECTOR_MODELS)}")


def _check_pfa(pfa: float) -> None:
    if not 0.0 < pfa < 1.0:
        raise DetectorError(f"pfa must be in (0, 1), got {pfa}")


# ------------------------------------------------------------- analytic


def _log_factorials(n: int) -> np.ndarray:
    return np.array([math.lgamma(k + 1.0) for k in range(n + 1)])


def _poisson_mixture(s: np.ndarray, t: float) -> np.ndarray:
    """P(Pois(t) <= Pois(s)) per element of s (t > 0): Q_1(sqrt(2s), sqrt(2t))."""
    s = np.asarray(s, dtype=float)
    flat_s = s.ravel()
    flat = np.full(flat_s.size, np.nan)
    finite = ~np.isnan(flat_s)
    sure = finite & (np.sqrt(np.maximum(2.0 * flat_s, 0.0)) - math.sqrt(2.0 * t) > _SURE_DETECTION_GAP)
    zero = finite & ~sure & (flat_s <= 0.0)
    flat[sure] = 1.0
    flat[zero] = math.exp(-t)
    work = np.flatnonzero(finite & ~sure & ~zero)
    if work.size:
        order = work[np.argsort(flat_s[work], kind="stable")]
        sv = flat_s[order]
        half = _SERIES_SIGMAS * np.sqrt(sv) + _SERIES_PAD
        k_lo = np.maximum(0, np.floor(sv - half)).astype(np.int64)
        k_hi = np.ceil(sv + half).astype(np.int64)
        k_max = int(k_hi.max())
        j_max = max(k_max, math.ceil(t + _SERIES_SIGMAS * math.sqrt(t) + _SERIES_PAD)) + 1
        lgk = _log_factorials(j_max)
        ks = np.arange(lgk.size, dtype=float)
        pmf_t = np.exp(-t + ks * math.log(t) - lgk)
        # P(Pois(t) <= k) and P(Pois(t) > k), k = 0..k_max, as running sums
        # of positive terms: each keeps its relative accuracy where it is
        # tiny, so Pd is summed directly below 0.5 and as 1 - P(miss) above
        # (no 1e-14 ripple next to Pd = 1).
        cdf_t = np.minimum(np.cumsum(pmf_t), 1.0)[: k_max + 1]
        sf_t = np.cumsum(pmf_t[::-1])[::-1][1 : k_max + 2]
        res = np.empty(sv.size)
        for start in range(0, sv.size, _SERIES_ROWS):
            stop = min(start + _SERIES_ROWS, sv.size)
            lo, hi = int(k_lo[start:stop].min()), int(k_hi[start:stop].max())
            k = ks[lo : hi + 1]
            chunk = sv[start:stop, None]
            weights = np.exp(-chunk + k[None, :] * np.log(chunk) - lgk[None, lo : hi + 1])
            hit = weights @ cdf_t[lo : hi + 1]
            miss = weights @ sf_t[lo : hi + 1]
            res[start:stop] = np.where(hit < 0.5, hit, 1.0 - miss)
        flat[order] = np.clip(res, 0.0, 1.0)
    return flat.reshape(s.shape)


def marcum_q1(a, b):
    """Marcum Q_1(a, b) for a, b >= 0 (floats or arrays), via the Poisson
    mixture P(Pois(b^2/2) <= Pois(a^2/2))."""
    a_arr, b_arr = np.broadcast_arrays(np.asarray(a, dtype=float), np.asarray(b, dtype=float))
    s = (a_arr**2 / 2.0).ravel()
    t = (b_arr**2 / 2.0).ravel()
    out = np.empty(s.size)
    for tv in np.unique(t):
        mask = t == tv
        out[mask] = 1.0 if tv <= 0.0 else _poisson_mixture(s[mask], float(tv))
    if not isinstance(a, np.ndarray) and not isinstance(b, np.ndarray):
        return float(out[0])
    return out.reshape(a_arr.shape)


def pd_swerling(snr_db, pfa: float, model: DetectorModel = "swerling1"):
    """Pd of a square-law detector at ``pfa`` for post-integration ``snr_db``
    (float, None = no echo -> pfa, or an array -> array)."""
    _check_model(model)
    _check_pfa(pfa)
    if snr_db is None:
        return pfa
    if model == "swerling1":
        # The literal v0.1.12 expression (sensing_track.swerling1_pd), so the
        # default numbers stay bit-identical for floats and arrays.
        return pfa ** (1.0 / (1.0 + 10.0 ** (snr_db / 10.0)))
    t = -math.log(pfa)
    if model == "swerling3":
        if isinstance(snr_db, np.ndarray):
            h = 10.0 ** (snr_db / 10.0) / 2.0
            return np.exp(-t / (1.0 + h)) * (1.0 + t * h / (1.0 + h) ** 2)
        h = 10.0 ** (float(snr_db) / 10.0) / 2.0
        return math.exp(-t / (1.0 + h)) * (1.0 + t * h / (1.0 + h) ** 2)
    if isinstance(snr_db, np.ndarray):
        return _poisson_mixture(10.0 ** (snr_db / 10.0), t)
    return float(_poisson_mixture(np.array([10.0 ** (float(snr_db) / 10.0)]), t)[0])


def snr_for_pd(pd: float, pfa: float, model: DetectorModel = "swerling1") -> Optional[float]:
    """Post-integration SNR [dB] that reaches ``pd`` at ``pfa`` (None: pd <=
    pfa, any SNR does). swerling1 is the closed form; swerling0/3 bisect on
    dB to 1e-9 (Pd is monotone in S)."""
    _check_model(model)
    _check_pfa(pfa)
    if not 0.0 < pd < 1.0:
        raise DetectorError(f"pd must be in (0, 1), got {pd}")
    if model == "swerling1":
        lin = math.log(pfa) / math.log(pd) - 1.0
        return 10.0 * math.log10(lin) if lin > 0.0 else None
    if pd <= pfa:
        return None
    lo, hi = -50.0, 100.0
    if pd_swerling(hi, pfa, model) < pd:
        return None
    while pd_swerling(lo, pfa, model) >= pd and lo > -400.0:
        lo -= 50.0
    while hi - lo > 1e-9:
        mid = 0.5 * (lo + hi)
        if pd_swerling(mid, pfa, model) >= pd:
            hi = mid
        else:
            lo = mid
    return 0.5 * (lo + hi)


def wilson_ci(successes: int, trials: int, z: float = 1.959963984540054) -> tuple[float, float]:
    """Wilson score interval of a binomial proportion (95 % by default)."""
    if trials <= 0:
        return 0.0, 1.0
    n = float(trials)
    p = successes / n
    z2 = z * z
    den = 1.0 + z2 / n
    center = (p + z2 / (2.0 * n)) / den
    half = z / den * math.sqrt(p * (1.0 - p) / n + z2 / (4.0 * n * n))
    # All-or-none samples: the exact bound is 0 / 1, which center -/+ half
    # can miss by one ulp (an analytic Pd of exactly 1.0 then reads as
    # outside the interval).
    low = 0.0 if successes <= 0 else max(0.0, center - half)
    high = 1.0 if successes >= trials else min(1.0, center + half)
    return low, high


# ---------------------------------------------------------- Monte Carlo


@dataclass(frozen=True)
class MonteCarloPd:
    pd: float
    ci_low: float
    ci_high: float
    # NaN when measure_pfa=False.
    pfa_measured: float
    pfa_ci_low: float
    pfa_ci_high: float
    # The threshold actually used (analytic, empirical or the caller's).
    threshold: float
    trials: int
    empirical_threshold: bool


@dataclass(frozen=True)
class NoiseReference:
    """One noise-only pair shared by every estimate of a request."""

    threshold: float
    # (1 - pfa) quantile of noise run 1 (empirical_threshold only).
    empirical_threshold: Optional[float]
    pfa_measured: float
    pfa_ci_low: float
    pfa_ci_high: float
    trials: int
    explicit_pulses: bool


def _cn(rng: np.random.Generator, shape) -> np.ndarray:
    return (rng.standard_normal(shape) + 1j * rng.standard_normal(shape)) * _SQRT_HALF


def _amplitudes(rng: np.random.Generator, model: str, s: float, n: int) -> np.ndarray:
    if model == "swerling1":
        return math.sqrt(s) * _cn(rng, n)
    phase = np.exp(1j * rng.uniform(0.0, 2.0 * math.pi, n))
    if model == "swerling0":
        return math.sqrt(s) * phase
    return np.sqrt(rng.gamma(2.0, s / 2.0, n)) * phase


def _draw_power(
    rng: np.random.Generator, model: str, s: float, n: int, pulses: int, explicit: bool
) -> np.ndarray:
    """y = |z|^2 of n trials; s <= 0 is noise only (no amplitude draw).

    Explicit: pulse i = A / sqrt(N) + n_i, z = sum_i pulse_i / sqrt(N).
    Otherwise z = A + CN(0, 1) directly: the same law, since the sum of N iid
    CN(0, 1) over sqrt(N) is CN(0, 1)."""
    amp = _amplitudes(rng, model, s, n) if s > 0.0 else None
    if not explicit:
        z = _cn(rng, n)
        if amp is not None:
            z += amp
        return z.real**2 + z.imag**2
    root = math.sqrt(pulses)
    z = _cn(rng, (n, pulses))
    if amp is not None:
        z += (amp / root)[:, None]
    z = z.sum(axis=1) / root
    return z.real**2 + z.imag**2


def _chunks(trials: int, pulses: int, explicit: bool):
    rows = max(1, _MC_CHUNK // pulses) if explicit else _MC_CHUNK
    for start in range(0, trials, rows):
        yield min(rows, trials - start)


def _count_above(
    rng: np.random.Generator, model: str, s: float, trials: int, pulses: int,
    explicit: bool, threshold: float,
) -> int:
    return sum(
        int(np.count_nonzero(_draw_power(rng, model, s, n, pulses, explicit) > threshold))
        for n in _chunks(trials, pulses, explicit)
    )


def _noise_quantile(
    rng: np.random.Generator, trials: int, pulses: int, explicit: bool, pfa: float
) -> float:
    y = np.concatenate(
        [_draw_power(rng, "swerling1", 0.0, n, pulses, explicit)
         for n in _chunks(trials, pulses, explicit)]
    )
    return float(np.quantile(y, 1.0 - pfa))


def _check_trials(trials: int, cpi_pulses: int) -> None:
    if not 1 <= trials <= MAX_MC_TRIALS:
        raise DetectorError(f"monte_carlo_trials must be in [1, {MAX_MC_TRIALS}], got {trials}")
    if cpi_pulses < 1:
        raise DetectorError(f"cpi_pulses must be >= 1, got {cpi_pulses}")


def _check_empirical(trials: int, pfa: float) -> None:
    try:
        check_empirical_trials(trials, pfa)
    except ValueError as exc:
        raise DetectorError(str(exc)) from exc


def noise_seed(seed: int) -> list[int]:
    """Stream key of a request's shared noise pair. SeedSequence pads a key
    shorter than 4 words with zeros, so [seed, c] and [seed, c, 0] are one
    stream; the nonzero 4th word keeps this key apart from every 2- or
    3-element Pd key ([seed, cell], [seed, tx, beam], ...)."""
    return [seed, NOISE_STREAM, 0, 1]


def noise_reference(
    pfa: float,
    trials: int,
    seed: int | Sequence[int] = 0,
    cpi_pulses: int = 1,
    empirical_threshold: bool = False,
    *,
    explicit_pulses: bool = False,
) -> NoiseReference:
    """Threshold in use and measured Pfa from one noise-only pair: run 1
    (empirical_threshold only) gives the (1 - pfa) quantile, run 2 (always,
    never reusing run 1) the false alarms against the threshold in use.
    Callers pass seed=noise_seed(request seed). ``explicit_pulses`` as in
    monte_carlo_pd."""
    _check_pfa(pfa)
    _check_trials(trials, cpi_pulses)
    if empirical_threshold:
        _check_empirical(trials, pfa)
    rng = np.random.default_rng(seed)
    explicit = explicit_pulses and trials * cpi_pulses <= _EXPLICIT_PULSE_BUDGET
    emp = _noise_quantile(rng, trials, cpi_pulses, explicit, pfa) if empirical_threshold else None
    threshold = emp if emp is not None else -math.log(pfa)
    fa = _count_above(rng, "swerling1", 0.0, trials, cpi_pulses, explicit, threshold)
    lo, hi = wilson_ci(fa, trials)
    return NoiseReference(
        threshold=threshold, empirical_threshold=emp, pfa_measured=fa / trials,
        pfa_ci_low=lo, pfa_ci_high=hi, trials=trials, explicit_pulses=explicit,
    )


def monte_carlo_pd(
    snr_db: Optional[float],
    pfa: float,
    trials: int,
    model: DetectorModel = "swerling1",
    seed: int | Sequence[int] = 0,
    cpi_pulses: int = 1,
    empirical_threshold: bool = False,
    *,
    threshold: Optional[float] = None,
    measure_pfa: bool = True,
    explicit_pulses: bool = False,
) -> MonteCarloPd:
    """Monte Carlo Pd of ``snr_db`` (None = noise only) over ``trials``
    target draws, with ``np.random.default_rng(seed)``.

    Each trial draws the integrated sample z = A + CN(0, 1) directly: the
    law of the ``cpi_pulses``-pulse coherent sum, at a cost of O(trials)
    whatever cpi_pulses is (so the trial budgets bound the run time).
    ``explicit_pulses`` draws every pulse instead (cpi_pulses times the cost,
    only while trials * cpi_pulses <= 4e6), for the test that checks the two
    agree.

    The threshold is -ln(pfa), or with ``empirical_threshold`` the (1 - pfa)
    quantile of a noise-only run drawn first (needs trials >= 20 / pfa,
    else DetectorError). ``threshold`` overrides both (e.g. a request's
    shared ``noise_reference``). ``pfa_measured`` comes from an independent
    noise-only run after the signal run (skipped with measure_pfa=False:
    NaN)."""
    _check_model(model)
    _check_pfa(pfa)
    _check_trials(trials, cpi_pulses)
    if empirical_threshold and threshold is None:
        _check_empirical(trials, pfa)
    rng = np.random.default_rng(seed)
    explicit = explicit_pulses and trials * cpi_pulses <= _EXPLICIT_PULSE_BUDGET
    if threshold is not None:
        thr = float(threshold)
    elif empirical_threshold:
        thr = _noise_quantile(rng, trials, cpi_pulses, explicit, pfa)
    else:
        thr = -math.log(pfa)
    s = 0.0 if snr_db is None else 10.0 ** (float(snr_db) / 10.0)
    hits = _count_above(rng, model, s, trials, cpi_pulses, explicit, thr)
    lo, hi = wilson_ci(hits, trials)
    pfa_m = pfa_lo = pfa_hi = math.nan
    if measure_pfa:
        fa = _count_above(rng, model, 0.0, trials, cpi_pulses, explicit, thr)
        pfa_m = fa / trials
        pfa_lo, pfa_hi = wilson_ci(fa, trials)
    return MonteCarloPd(
        pd=hits / trials, ci_low=lo, ci_high=hi,
        pfa_measured=pfa_m, pfa_ci_low=pfa_lo, pfa_ci_high=pfa_hi,
        threshold=thr, trials=trials, empirical_threshold=bool(empirical_threshold),
    )


def mc_method(explicit: bool, cpi_pulses: int) -> str:
    if explicit:
        return f"explicit {cpi_pulses}-pulse coherent sum per trial"
    return (
        "sufficient statistic z = A + CN(0, 1) per trial (the same law as the "
        f"{cpi_pulses}-pulse coherent sum)"
    )


def detector_metadata(
    model: str, pfa: float, trials: int, ref: Optional[NoiseReference]
) -> dict:
    """metadata["detector"] of an ISAC / coverage run with a non-default detector."""
    out: dict = {
        "model": model,
        "pd_model": PD_MODEL,
        "analytic_threshold": -math.log(pfa),
        "monte_carlo_trials": trials,
    }
    if ref is not None:
        out.update(
            threshold=ref.threshold,
            empirical_threshold=ref.empirical_threshold,
            pfa_measured=ref.pfa_measured,
            pfa_measured_ci=[ref.pfa_ci_low, ref.pfa_ci_high],
            noise_stream=NOISE_STREAM,
        )
    return out


def pd_curve(request: PdCurveRequest) -> PdCurveResult:
    """Analytic Pd vs SNR per model, with a Monte Carlo per point when
    monte_carlo_trials > 0 (streams [seed, model index, snr index]; the
    threshold and measured Pfa from one noise pair noise_seed(seed))."""
    pfa = request.pfa
    trials = request.monte_carlo_trials
    snr = request.snr_axis_db()
    axis = np.asarray(snr, dtype=float)
    ref: Optional[NoiseReference] = None
    if trials > 0:
        ref = noise_reference(
            pfa, trials, noise_seed(request.seed), request.cpi_pulses,
            request.empirical_threshold,
        )
    models: list[PdCurveModel] = []
    for name in request.models:
        analytic = [float(v) for v in pd_swerling(axis, pfa, name)]
        entry = PdCurveModel(
            model=name, pd=analytic, snr_for_pd_target_db=snr_for_pd(request.pd_target, pfa, name)
        )
        if ref is not None:
            index = DETECTOR_MODELS.index(name)
            runs = [
                monte_carlo_pd(
                    db, pfa, trials, name, seed=[request.seed, index, i],
                    cpi_pulses=request.cpi_pulses,
                    empirical_threshold=request.empirical_threshold,
                    threshold=ref.threshold, measure_pfa=False,
                )
                for i, db in enumerate(snr)
            ]
            entry.pd_mc = [r.pd for r in runs]
            entry.pd_mc_ci_low = [r.ci_low for r in runs]
            entry.pd_mc_ci_high = [r.ci_high for r in runs]
            entry.mc_max_abs_deviation = max(abs(r.pd - p) for r, p in zip(runs, analytic))
            entry.mc_within_ci_fraction = sum(
                r.ci_low <= p <= r.ci_high for r, p in zip(runs, analytic)
            ) / len(runs)
        models.append(entry)
    warnings: list[str] = []
    if ref is not None and request.empirical_threshold:
        warnings.append(
            "empirical threshold: the Monte Carlo points use the measured "
            f"threshold {ref.threshold:.4f} (analytic {-math.log(pfa):.4f}), so "
            "their spread around the analytic curve includes its estimation error"
        )
    return PdCurveResult(
        pfa=pfa,
        pd_target=request.pd_target,
        cpi_pulses=request.cpi_pulses,
        monte_carlo_trials=trials,
        seed=request.seed,
        snr_db=snr,
        threshold=-math.log(pfa),
        empirical_threshold=ref.empirical_threshold if ref is not None else None,
        pfa_measured=ref.pfa_measured if ref is not None else None,
        pfa_measured_ci=[ref.pfa_ci_low, ref.pfa_ci_high] if ref is not None else None,
        models=models,
        warnings=warnings,
        metadata={
            "mc_method": (
                mc_method(ref.explicit_pulses, request.cpi_pulses) if ref is not None
                else "none (analytic only)"
            ),
            "pd_model": PD_MODEL,
        },
    )
