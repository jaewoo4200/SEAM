"""Single-link channel analysis: ray-traced CIR/CFR + dispersion metrics, side
by side with empirical path-loss models (the Wireless-InSite / operator
workflow — a ray-traced prediction compared against 3GPP TR 38.901, close-in
(CI) and free-space (FSPL) reference models).

The empirical models are pure, unit-testable functions with explicit validity
checks (out-of-range frequency/distance -> ``valid=False`` plus a note). The
ray-traced side reuses ``resolve_backend(config).simulate_paths`` with tx/rx
narrowed to the single link so the CIR taps come straight from RayPath entries.

On top of the single-link analysis: ``sweep_channel`` re-runs it with one
scalar knob patched per point, and ``analyze_spectrogram`` STFTs the coherent
complex h(t) into a Doppler-time grid (ISAC sensing readout).
"""

import math
from pathlib import Path
from typing import Optional

from app.schemas.channel import (
    ChannelAnalysisRequest,
    ChannelAnalysisResult,
    ChannelSweepPoint,
    ChannelSweepRequest,
    ChannelSweepResult,
    CirTap,
    PathLossModelName,
    PathLossModelResult,
    SpectrogramRequest,
    SpectrogramResult,
)
from app.schemas.devices import Device
from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import RayPath
from app.schemas.scene import Scene
from app.schemas.simulation import SimulationConfig
from app.services.simulation_backends import resolve_backend
from app.services.simulation_backends.sionna_backend import noise_floor_dbm

SPEED_OF_LIGHT = 299_792_458.0


# ============================================================ empirical models
#
# All models return a (path_loss_db, valid, notes) triple via _ModelOutput so
# the caller can build a PathLossModelResult uniformly. Frequency inputs are in
# Hz, distances in meters (3D). Validity notes never suppress a value: the
# model is always evaluated (extrapolated) and the note flags that the point is
# outside the published range so a UI can grey it out.


class _ModelOutput:
    __slots__ = ("path_loss_db", "valid", "notes")

    def __init__(self, path_loss_db: float, valid: bool, notes: str) -> None:
        self.path_loss_db = path_loss_db
        self.valid = valid
        self.notes = notes


def _freq_note(freq_hz: float, lo_ghz: float, hi_ghz: float) -> Optional[str]:
    f_ghz = freq_hz / 1e9
    if f_ghz < lo_ghz or f_ghz > hi_ghz:
        return f"frequency {f_ghz:.2f} GHz outside {lo_ghz}-{hi_ghz} GHz validity"
    return None


def fspl_db(freq_hz: float, dist_m: float) -> float:
    """Exact free-space path loss: 20log10(4*pi*d*f/c).

    Equivalent to 32.45 + 20log10(f_GHz) + 20log10(d_km); we use the closed
    form directly so it is exact at any unit. Distance floored at 1 m so a
    co-located pair does not blow up to -inf.
    """
    d = max(dist_m, 1.0)
    return 20.0 * math.log10(4.0 * math.pi * d * freq_hz / SPEED_OF_LIGHT)


def _fspl(freq_hz: float, dist_m: float) -> _ModelOutput:
    # FSPL is frequency-unbounded but assumes far-field, isotropic antennas -
    # worth stating at mmWave where near-field/pattern effects are common.
    return _ModelOutput(
        fspl_db(freq_hz, dist_m), True, "far-field, isotropic antennas assumed"
    )


def _ci_model(freq_hz: float, dist_m: float, n: float) -> _ModelOutput:
    """Close-in (CI) reference-distance model, d0 = 1 m:
    PL = FSPL(1 m) + 10*n*log10(d).  n=2 recovers the free-space slope.
    """
    pl0 = fspl_db(freq_hz, 1.0)  # FSPL at the 1 m reference distance
    d = max(dist_m, 1.0)
    pl = pl0 + 10.0 * n * math.log10(d)
    notes = f"close-in model, d0=1 m, PLE n={n:g}; far-field, isotropic antennas assumed"
    return _ModelOutput(pl, True, notes)


# ---- 3GPP TR 38.901 (v16) large-scale path loss (median, shadowing dropped).
#
# h_BS = TX height (z), h_UT = UT/RX height (z). d_2D is the horizontal
# separation, d_3D the slant. Breakpoint distance uses the effective antenna
# heights (h - 1.0 m environment height) with the standard c = 3e8 approx.


def _breakpoint_distance(h_bs: float, h_ut: float, freq_hz: float) -> float:
    # 2D breakpoint d'_BP = 4 * h'_BS * h'_UT * f_c / c, with effective heights
    # h' = h - h_E and h_E = 1.0 m (TR 38.901 note 1 for the UMa/UMi cases).
    h_bs_eff = max(h_bs - 1.0, 0.1)
    h_ut_eff = max(h_ut - 1.0, 0.1)
    return 4.0 * h_bs_eff * h_ut_eff * freq_hz / SPEED_OF_LIGHT


def _geometry(h_bs: float, h_ut: float, dist_3d_m: float) -> tuple[float, float]:
    """(d_2D, d_3D) from BS/UT heights and a 3D separation. When the requested
    3D distance is shorter than the height difference we clamp d_2D to a small
    positive value rather than take a sqrt of a negative number."""
    d_3d = max(dist_3d_m, 1.0)
    dh = abs(h_bs - h_ut)
    d_2d_sq = d_3d * d_3d - dh * dh
    d_2d = math.sqrt(d_2d_sq) if d_2d_sq > 0.0 else 1.0
    return d_2d, d_3d


def _uma_los(freq_hz: float, d_2d: float, d_3d: float, h_bs: float, h_ut: float) -> float:
    fc = freq_hz / 1e9
    d_bp = _breakpoint_distance(h_bs, h_ut, freq_hz)
    if d_2d <= d_bp:
        return 28.0 + 22.0 * math.log10(d_3d) + 20.0 * math.log10(fc)
    return (
        28.0
        + 40.0 * math.log10(d_3d)
        + 20.0 * math.log10(fc)
        - 9.0 * math.log10(d_bp * d_bp + (h_bs - h_ut) ** 2)
    )


def _uma(freq_hz: float, d_2d: float, d_3d: float, h_bs: float, h_ut: float, los: bool) -> float:
    pl_los = _uma_los(freq_hz, d_2d, d_3d, h_bs, h_ut)
    if los:
        return pl_los
    fc = freq_hz / 1e9
    pl_nlos_prime = (
        13.54
        + 39.08 * math.log10(d_3d)
        + 20.0 * math.log10(fc)
        - 0.6 * (h_ut - 1.5)
    )
    return max(pl_los, pl_nlos_prime)  # TR 38.901: NLOS = max(LOS, NLOS')


def _umi_los(freq_hz: float, d_2d: float, d_3d: float, h_bs: float, h_ut: float) -> float:
    fc = freq_hz / 1e9
    d_bp = _breakpoint_distance(h_bs, h_ut, freq_hz)
    if d_2d <= d_bp:
        return 32.4 + 21.0 * math.log10(d_3d) + 20.0 * math.log10(fc)
    return (
        32.4
        + 40.0 * math.log10(d_3d)
        + 20.0 * math.log10(fc)
        - 9.5 * math.log10(d_bp * d_bp + (h_bs - h_ut) ** 2)
    )


def _umi(freq_hz: float, d_2d: float, d_3d: float, h_bs: float, h_ut: float, los: bool) -> float:
    pl_los = _umi_los(freq_hz, d_2d, d_3d, h_bs, h_ut)
    if los:
        return pl_los
    fc = freq_hz / 1e9
    pl_nlos_prime = (
        35.3 * math.log10(d_3d)
        + 22.4
        + 21.3 * math.log10(fc)
        - 0.3 * (h_ut - 1.5)
    )
    return max(pl_los, pl_nlos_prime)


def _inh_los(freq_hz: float, d_3d: float) -> float:
    fc = freq_hz / 1e9
    return 32.4 + 17.3 * math.log10(d_3d) + 20.0 * math.log10(fc)


def _inh(freq_hz: float, d_3d: float, los: bool) -> float:
    pl_los = _inh_los(freq_hz, d_3d)
    if los:
        return pl_los
    fc = freq_hz / 1e9
    pl_nlos_prime = 38.3 * math.log10(d_3d) + 17.30 + 24.9 * math.log10(fc)
    return max(pl_los, pl_nlos_prime)


def _tr38901(
    kind: str, freq_hz: float, d_2d: float, d_3d: float, h_bs: float, h_ut: float
) -> _ModelOutput:
    """Dispatch to a named TR 38.901 scenario and attach validity notes.

    Frequency validity is 0.5-100 GHz across the 38.901 scenarios; distance
    ranges are scenario-specific (UMa/UMi 10 m-5 km 2D; InH 1-150 m 3D). We
    always return the (extrapolated if needed) median PL and flag ranges.
    """
    notes: list[str] = []
    fnote = _freq_note(freq_hz, 0.5, 100.0)
    valid = fnote is None
    if fnote:
        notes.append(fnote)

    if kind == "uma_los":
        pl = _uma(freq_hz, d_2d, d_3d, h_bs, h_ut, los=True)
        rng = (10.0, 5000.0)
    elif kind == "uma_nlos":
        pl = _uma(freq_hz, d_2d, d_3d, h_bs, h_ut, los=False)
        rng = (10.0, 5000.0)
    elif kind == "umi_los":
        pl = _umi(freq_hz, d_2d, d_3d, h_bs, h_ut, los=True)
        rng = (10.0, 5000.0)
    elif kind == "umi_nlos":
        pl = _umi(freq_hz, d_2d, d_3d, h_bs, h_ut, los=False)
        rng = (10.0, 5000.0)
    elif kind == "inh_los":
        pl = _inh(freq_hz, d_3d, los=True)
        rng = (1.0, 150.0)
    elif kind == "inh_nlos":
        pl = _inh(freq_hz, d_3d, los=False)
        rng = (1.0, 150.0)
    else:  # pragma: no cover - dispatch table is closed
        raise ValueError(f"unknown TR 38.901 scenario: {kind!r}")

    ref = d_3d if kind.startswith("inh") else d_2d
    ref_name = "d_3D" if kind.startswith("inh") else "d_2D"
    if ref < rng[0] or ref > rng[1]:
        valid = False
        notes.append(f"{ref_name}={ref:.1f} m outside {rng[0]:g}-{rng[1]:g} m validity")
    return _ModelOutput(pl, valid, "; ".join(notes))


# ---- 3GPP TR 36.777 (study on enhanced LTE support for aerial vehicles),
# Annex B.1.2 UMa-AV air-to-ground path loss (median, shadowing dropped).
#
# The UT is the aerial vehicle: h_UT is the UAV altitude. The closed forms
# apply above the urban clutter, 22.5-300 m (below 22.5 m the spec hands back
# to the terrestrial TR 38.901 UMa models, which are already in the table), so
# altitude out of range is flagged via valid=False like the 38.901 distance
# gating, never an error.

_UMA_AV_ALT_RANGE_M = (22.5, 300.0)


def _uma_av_altitude_note(h_ut: float) -> Optional[str]:
    lo, hi = _UMA_AV_ALT_RANGE_M
    if h_ut < lo or h_ut > hi:
        return f"h_UT={h_ut:.1f} m outside {lo:g}-{hi:g} m UMa-AV validity"
    return None


def _uma_av_los(freq_hz: float, d_3d: float) -> float:
    # TR 36.777 Table B-1 (UMa-AV LOS, 22.5 m < h_UT <= 300 m):
    # PL = 28.0 + 22 log10(d_3D) + 20 log10(f_c[GHz]) - free-space-like slope,
    # the UAV sits above the clutter so the link is effectively unobstructed.
    fc = freq_hz / 1e9
    return 28.0 + 22.0 * math.log10(d_3d) + 20.0 * math.log10(fc)


def _uma_av_nlos(freq_hz: float, d_3d: float, h_ut: float) -> float:
    # TR 36.777 Table B-2 (UMa-AV NLOS):
    # PL = -17.5 + (46 - 7 log10(h_UT)) log10(d_3D) + 20 log10(40 pi f_c / 3);
    # the distance exponent relaxes with altitude (higher UAV -> fewer
    # obstructions). h_UT floored at 1 m so extrapolating a ground-level UT
    # stays finite; the altitude note flags it invalid anyway.
    fc = freq_hz / 1e9
    h = max(h_ut, 1.0)
    return (
        -17.5
        + (46.0 - 7.0 * math.log10(h)) * math.log10(d_3d)
        + 20.0 * math.log10(40.0 * math.pi * fc / 3.0)
    )


def _tr36777(kind: str, freq_hz: float, d_3d: float, h_ut: float) -> _ModelOutput:
    if kind == "uma_av_los":
        pl = _uma_av_los(freq_hz, d_3d)
    elif kind == "uma_av_nlos":
        pl = _uma_av_nlos(freq_hz, d_3d, h_ut)
    else:  # pragma: no cover - dispatch table is closed
        raise ValueError(f"unknown TR 36.777 scenario: {kind!r}")
    note = _uma_av_altitude_note(h_ut)
    return _ModelOutput(pl, note is None, note or "")


def evaluate_path_loss_models(
    freq_hz: float,
    dist_3d_m: float,
    h_bs: float,
    h_ut: float,
    rt_path_loss_db: Optional[float] = None,
    *,
    include_aerial: bool = False,
) -> list[PathLossModelResult]:
    """Evaluate every empirical model at one geometry and (when a ray-traced
    path loss is available) fill ``delta_vs_rt_db = model - RT``.

    ``include_aerial`` adds the TR 36.777 UMa-AV (UAV) rows. Opt-in because
    the default nine-model list is a pinned contract for pre-aerial callers;
    the /analyze/channel endpoint always opts in.
    """
    d_2d, d_3d = _geometry(h_bs, h_ut, dist_3d_m)
    outputs: list[tuple[PathLossModelName, _ModelOutput]] = [
        ("fspl", _fspl(freq_hz, d_3d)),
        ("tr38901_uma_los", _tr38901("uma_los", freq_hz, d_2d, d_3d, h_bs, h_ut)),
        ("tr38901_uma_nlos", _tr38901("uma_nlos", freq_hz, d_2d, d_3d, h_bs, h_ut)),
        ("tr38901_umi_los", _tr38901("umi_los", freq_hz, d_2d, d_3d, h_bs, h_ut)),
        ("tr38901_umi_nlos", _tr38901("umi_nlos", freq_hz, d_2d, d_3d, h_bs, h_ut)),
        ("tr38901_inh_los", _tr38901("inh_los", freq_hz, d_2d, d_3d, h_bs, h_ut)),
        ("tr38901_inh_nlos", _tr38901("inh_nlos", freq_hz, d_2d, d_3d, h_bs, h_ut)),
    ]
    if include_aerial:
        outputs += [
            ("tr36777_uma_av_los", _tr36777("uma_av_los", freq_hz, d_3d, h_ut)),
            ("tr36777_uma_av_nlos", _tr36777("uma_av_nlos", freq_hz, d_3d, h_ut)),
        ]
    outputs += [
        ("ci_n2", _ci_model(freq_hz, d_3d, 2.0)),
        ("ci_n3", _ci_model(freq_hz, d_3d, 3.0)),
    ]
    results: list[PathLossModelResult] = []
    for name, out in outputs:
        delta = (
            round(out.path_loss_db - rt_path_loss_db, 4)
            if rt_path_loss_db is not None
            else None
        )
        results.append(
            PathLossModelResult(
                model=name,
                path_loss_db=round(out.path_loss_db, 4),
                delta_vs_rt_db=delta,
                valid=out.valid,
                notes=out.notes,
            )
        )
    return results


# ============================================================ CIR / dispersion


def _lin_from_dbm(power_dbm: float) -> float:
    return 10.0 ** (power_dbm / 10.0)


def _path_amplitudes(paths: list[RayPath]) -> list[complex]:
    """Complex voltage amplitude per path: |a| = sqrt(linear power) (power is
    |a|^2) at the path's phase. The building block every coherent-sum quantity
    (CFR, Doppler envelope, spectrogram h(t)) shares."""
    return [
        math.sqrt(_lin_from_dbm(p.power_dbm))
        * complex(math.cos(p.phase_rad), math.sin(p.phase_rad))
        for p in paths
    ]


def build_cir(
    paths: list[RayPath], doppler_by_path_id: Optional[dict[str, float]] = None
) -> list[CirTap]:
    """One CIR tap per ray path, sorted by delay. ``doppler_by_path_id`` (when
    given) fills each tap's ``doppler_hz`` by the path's id."""
    dop = doppler_by_path_id or {}
    taps = [
        CirTap(
            delay_ns=p.delay_ns,
            power_dbm=p.power_dbm,
            phase_rad=p.phase_rad,
            path_type=p.path_type,
            doppler_hz=dop.get(p.path_id),
        )
        for p in paths
    ]
    taps.sort(key=lambda t: t.delay_ns)
    return taps


def k_factor_db(paths: list[RayPath]) -> Optional[float]:
    """Rician K in dB = P_LoS / sum(P_NLoS). None when there is no LoS path or
    no NLoS path (K is undefined / infinite there)."""
    los = [p for p in paths if p.path_type == "los"]
    nlos = [p for p in paths if p.path_type != "los"]
    if not los or not nlos:
        return None
    p_los = sum(_lin_from_dbm(p.power_dbm) for p in los)
    p_nlos = sum(_lin_from_dbm(p.power_dbm) for p in nlos)
    if p_los <= 0.0 or p_nlos <= 0.0:
        return None
    return 10.0 * math.log10(p_los / p_nlos)


def delay_metrics(paths: list[RayPath]) -> tuple[Optional[float], Optional[float]]:
    """(power-weighted mean delay ns, RMS delay spread ns). None when no paths
    or zero total power."""
    if not paths:
        return None, None
    weights = [_lin_from_dbm(p.power_dbm) for p in paths]
    delays = [p.delay_ns for p in paths]
    total = sum(weights)
    if total <= 0.0:
        return None, None
    mean_tau = sum(w * t for w, t in zip(weights, delays)) / total
    var = sum(w * (t - mean_tau) ** 2 for w, t in zip(weights, delays)) / total
    rms = math.sqrt(max(var, 0.0))
    return mean_tau, rms


def coherence_bandwidth_mhz(rms_delay_spread_ns: Optional[float]) -> Optional[float]:
    """B_c ~= 1/(2*pi*sigma_tau), in MHz. None when the RMS delay spread is
    None or zero (a single tap has no dispersion, so B_c is unbounded)."""
    if rms_delay_spread_ns is None or rms_delay_spread_ns <= 0.0:
        return None
    rms_s = rms_delay_spread_ns * 1e-9
    return 1.0 / (2.0 * math.pi * rms_s) / 1e6


def compute_cfr(
    paths: list[RayPath], bandwidth_hz: float, num_points: int
) -> tuple[list[float], list[float]]:
    """Channel frequency response H(f_k) = sum_l a_l * exp(-j 2 pi f_k tau_l),
    sampled at ``num_points`` frequency offsets across [-B/2, +B/2].

    |a_l| is the linear voltage amplitude from the path power (power is |a|^2),
    phase is the path phase. Returns (offsets_hz, magnitude_dB) where the
    magnitude is absolute 20log10|H| (not normalized). NaN-free: an empty H
    yields a floor of -300 dB rather than log10(0).
    """
    if num_points < 1:
        return [], []
    offsets = [
        -bandwidth_hz / 2.0 + bandwidth_hz * k / (num_points - 1)
        for k in range(num_points)
    ] if num_points > 1 else [0.0]

    amps: list[complex] = []
    taus_s: list[float] = []
    for p in paths:
        # power_dbm -> linear power (mW) -> voltage amplitude sqrt(power).
        amp_mag = math.sqrt(_lin_from_dbm(p.power_dbm))
        amps.append(amp_mag * complex(math.cos(p.phase_rad), math.sin(p.phase_rad)))
        taus_s.append(p.delay_ns * 1e-9)

    mags_db: list[float] = []
    for f in offsets:
        acc = 0j
        two_pi_f = 2.0 * math.pi * f
        for a, tau in zip(amps, taus_s):
            angle = -two_pi_f * tau
            acc += a * complex(math.cos(angle), math.sin(angle))
        mag = abs(acc)
        mags_db.append(20.0 * math.log10(mag) if mag > 0.0 else -300.0)
    return offsets, mags_db


# ================================================================== Doppler
#
# Doppler shift per path (f_d = v.k/lambda summed over interactions) comes from
# the backend (sionna surfaces it in PathResultSet.metadata["doppler_hz"],
# aligned 1:1 with paths). From that plus per-path power we derive the classic
# Doppler-spectrum scalars: power-weighted mean/spread and the coherence time.


def doppler_metrics(
    paths: list[RayPath], doppler_hz: Optional[list[float]]
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    """(mean, spread, max_abs, coherence_time_ms) from per-path Doppler.

    - mean:   power-weighted mean shift (Hz);
    - spread: power-weighted std of the per-path shifts (Hz) - the RMS Doppler
              spread that broadens the Doppler power spectrum;
    - max_abs: max |per-path Doppler| (Hz);
    - coherence_time_ms: T_c ~= 0.42 / max|Doppler| (classic Clarke/Jakes rule),
                         None when nothing moves (max Doppler 0).

    Returns all-None when there is no Doppler information or zero total power.
    """
    if not paths or not doppler_hz or len(doppler_hz) != len(paths):
        return None, None, None, None
    weights = [_lin_from_dbm(p.power_dbm) for p in paths]
    total = sum(weights)
    if total <= 0.0:
        return None, None, None, None
    mean = sum(w * d for w, d in zip(weights, doppler_hz)) / total
    var = sum(w * (d - mean) ** 2 for w, d in zip(weights, doppler_hz)) / total
    spread = math.sqrt(max(var, 0.0))
    max_abs = max(abs(d) for d in doppler_hz)
    coherence_time_ms = (0.42 / max_abs * 1e3) if max_abs > 0.0 else None
    return mean, spread, max_abs, coherence_time_ms


def doppler_time_envelope(
    paths: list[RayPath],
    doppler_hz: Optional[list[float]],
    num_time_steps: int,
    sampling_frequency_hz: Optional[float],
) -> tuple[list[float], list[float]]:
    """Time-varying channel envelope |h(t)| in dB over ``num_time_steps``.

    h(t) = sum_i a_i e^{j phase_i} e^{j 2 pi f_d,i t}, sampled at t = n/fs. This
    is the coherent superposition the Doppler shifts produce: its ripple is the
    fast fading. Mirrors ``paths.cir(num_time_steps=N)`` in sionna (same
    a_i e^{j2 pi f_d t} model, paths.py:405) but is backend-agnostic - computed
    from the per-path (power, phase, doppler) the result already carries.

    Returns ([] , []) when num_time_steps <= 1 or no Doppler is available. When
    ``sampling_frequency_hz`` is None it defaults to Nyquist (2x max|Doppler|),
    falling back to 1 kHz so a window still exists when nothing moves.
    """
    if num_time_steps <= 1 or not paths or not doppler_hz:
        return [], []
    if len(doppler_hz) != len(paths):
        return [], []
    fs = sampling_frequency_hz
    if fs is None:
        max_abs = max((abs(d) for d in doppler_hz), default=0.0)
        fs = 2.0 * max_abs if max_abs > 0.0 else 1000.0
    amps = _path_amplitudes(paths)
    times = [n / fs for n in range(num_time_steps)]
    env_db: list[float] = []
    for t in times:
        acc = 0j
        for a, fd in zip(amps, doppler_hz):
            angle = 2.0 * math.pi * fd * t
            acc += a * complex(math.cos(angle), math.sin(angle))
        mag = abs(acc)
        env_db.append(20.0 * math.log10(mag) if mag > 0.0 else -300.0)
    return times, env_db


def _unit_vector(a: list[float], b: list[float]) -> Optional[list[float]]:
    d = [bi - ai for ai, bi in zip(a, b)]
    n = math.sqrt(sum(x * x for x in d))
    return [x / n for x in d] if n > 1e-12 else None


def geometric_doppler_hz(
    paths: list[RayPath], tx: Device, rx: Device, freq_hz: float
) -> list[float]:
    """Per-path Doppler from the endpoint devices' velocities and the path
    polyline's departure/arrival directions:

        f_d = (f/c) * (v_tx . k_dep - v_rx . k_arr)

    with k_dep the unit vector tx -> first vertex after tx and k_arr the unit
    vector last vertex before rx -> rx, i.e. f_d = -(f/c) d(path length)/dt.
    A closing link yields a positive shift. Scatterer (actor) motion is
    invisible here - backends that model it surface the true per-path Doppler
    in PathResultSet.metadata["doppler_hz"] instead; this is the fallback for
    backends (e.g. mock) that do not.
    """
    v_tx = tx.velocity_m_s
    v_rx = rx.velocity_m_s
    out: list[float] = []
    for p in paths:
        rate = 0.0  # -(d path length / dt), m/s
        if len(p.vertices) >= 2:
            if v_tx is not None:
                k = _unit_vector(p.vertices[0], p.vertices[1])
                if k is not None:
                    rate += sum(v * kk for v, kk in zip(v_tx, k))
            if v_rx is not None:
                k = _unit_vector(p.vertices[-2], p.vertices[-1])
                if k is not None:
                    rate -= sum(v * kk for v, kk in zip(v_rx, k))
        out.append(rate * freq_hz / SPEED_OF_LIGHT)
    return out


# ============================================================ orchestration


def _resolve_config(scene: Scene, request: ChannelAnalysisRequest) -> SimulationConfig:
    if request.config is not None:
        return request.config
    if request.config_id is not None:
        for cfg in scene.simulation_configs:
            if cfg.id == request.config_id:
                return cfg
        raise ValueError(f"simulation config not found: {request.config_id}")
    if scene.simulation_configs:
        return scene.simulation_configs[0]
    return SimulationConfig()


def _pick_device(devices: list[Device], wanted_id: Optional[str], kind: str) -> Optional[Device]:
    if wanted_id is not None:
        return next((d for d in devices if d.id == wanted_id), None)
    return devices[0] if devices else None


def _pick_link(
    scene: Scene, tx_id: Optional[str], rx_id: Optional[str]
) -> tuple[Device, Device]:
    """Resolve the (tx, rx) pair for a single-link analysis; None = first of
    kind. Raises ValueError (-> 4xx at the API) when either end is missing."""
    txs = [d for d in scene.devices if d.kind == "tx"]
    rxs = [d for d in scene.devices if d.kind == "rx"]
    tx = _pick_device(txs, tx_id, "tx")
    rx = _pick_device(rxs, rx_id, "rx")
    if tx is None:
        raise ValueError(
            f"tx device not found: {tx_id!r}" if tx_id else "scene has no transmitter"
        )
    if rx is None:
        raise ValueError(
            f"rx device not found: {rx_id!r}" if rx_id else "scene has no receiver"
        )
    return tx, rx


def analyze_channel(
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    request: ChannelAnalysisRequest,
) -> ChannelAnalysisResult:
    """Solve one TX->RX link and derive the full channel-analysis payload.

    Raises ValueError for an unknown config id / missing device (the API maps
    it to a 4xx). Backend-solve warnings are surfaced in ``warnings``.
    """
    config = _resolve_config(scene, request)
    tx, rx = _pick_link(scene, request.tx_id, request.rx_id)
    txs = [d for d in scene.devices if d.kind == "tx"]

    warnings: list[str] = []
    backend = resolve_backend(config)
    # Narrow the solve to exactly this link so every RayPath belongs to it.
    link_cfg = config.model_copy(update={"tx_ids": [tx.id], "rx_ids": [rx.id]})
    result = backend.simulate_paths(project_dir, scene, library, link_cfg)
    warnings.extend(result.warnings)
    # Per-path Doppler rides in metadata aligned 1:1 with result.paths (RayPath
    # has no doppler field). Map it by path id so it survives the link filter
    # and the delay sort below.
    raw_doppler = result.metadata.get("doppler_hz")
    doppler_by_path_id: dict[str, float] = {}
    if isinstance(raw_doppler, list) and len(raw_doppler) == len(result.paths):
        doppler_by_path_id = {
            p.path_id: float(d) for p, d in zip(result.paths, raw_doppler)
        }
    # Defensive: the backend may (in multi-tx scenes) return extra links.
    paths = [p for p in result.paths if p.tx_id == tx.id and p.rx_id == rx.id]
    # Per-path Doppler for exactly this link, aligned to ``paths`` order.
    link_doppler: Optional[list[float]] = (
        [doppler_by_path_id[p.path_id] for p in paths]
        if doppler_by_path_id and all(p.path_id in doppler_by_path_id for p in paths)
        else None
    )

    dist_3d = math.dist(list(tx.position), list(rx.position))
    h_bs = float(tx.position[2])
    h_ut = float(rx.position[2])

    # ---- Ray-traced link budget.
    lin_total = sum(_lin_from_dbm(p.power_dbm) for p in paths)
    rss_dbm: Optional[float] = 10.0 * math.log10(lin_total) if lin_total > 0.0 else None
    rt_path_loss_db: Optional[float] = (
        tx.power_dbm - rss_dbm if rss_dbm is not None else None
    )
    noise_floor = noise_floor_dbm(config)
    snr_db: Optional[float] = (rss_dbm - noise_floor) if rss_dbm is not None else None

    # ---- Co-channel interference: every OTHER TX's ray-traced power at this
    # RX (full-buffer: all TXs transmit simultaneously on the same resources).
    # One extra solve covers all interferers; the scene cache makes it cheap.
    interference_dbm: Optional[float] = None
    other_tx_ids = [d.id for d in txs if d.id != tx.id]
    if other_tx_ids:
        intf_cfg = config.model_copy(
            update={"tx_ids": other_tx_ids, "rx_ids": [rx.id]}
        )
        intf_result = backend.simulate_paths(project_dir, scene, library, intf_cfg)
        intf_lin = sum(
            _lin_from_dbm(p.power_dbm)
            for p in intf_result.paths
            if p.rx_id == rx.id and p.tx_id != tx.id
        )
        if intf_lin > 0.0:
            interference_dbm = 10.0 * math.log10(intf_lin)

    # SINR over interference + noise; equals SNR when nothing interferes.
    intf_plus_noise = _lin_from_dbm(noise_floor) + (
        _lin_from_dbm(interference_dbm) if interference_dbm is not None else 0.0
    )
    sinr_db: Optional[float] = (
        rss_dbm - 10.0 * math.log10(intf_plus_noise) if rss_dbm is not None else None
    )

    # Capacity uses the SINR (interference-aware Shannon bound).
    shannon_capacity_mbps: Optional[float] = None
    if sinr_db is not None:
        sinr_lin = 10.0 ** (sinr_db / 10.0)
        shannon_capacity_mbps = config.bandwidth_hz * math.log2(1.0 + sinr_lin) / 1e6

    # ---- 3GPP measurement quantities (TS 38.215-style) over an OFDM grid at
    # the requested subcarrier spacing. RSRP is the per-resource-element power
    # (wideband RSS spread evenly across occupied subcarriers), RSSI includes
    # co-channel interference + thermal noise, RSRQ = N_RB * RSRP / RSSI.
    scs_khz = request.subcarrier_spacing_khz
    n_rb = max(1, int(config.bandwidth_hz / (12.0 * scs_khz * 1e3)))
    n_sc = n_rb * 12
    rsrp_dbm: Optional[float] = None
    rssi_dbm: Optional[float] = None
    rsrq_db: Optional[float] = None
    if rss_dbm is not None:
        rsrp_dbm = rss_dbm - 10.0 * math.log10(n_sc)
        rssi_lin = _lin_from_dbm(rss_dbm) + intf_plus_noise
        rssi_dbm = 10.0 * math.log10(rssi_lin)
        rsrq_db = 10.0 * math.log10(n_rb * _lin_from_dbm(rsrp_dbm) / rssi_lin)

    # ---- Dispersion / fading metrics.
    cir = build_cir(paths, doppler_by_path_id)
    kf = k_factor_db(paths)
    mean_delay, rms_ds = delay_metrics(paths)
    coh_bw = coherence_bandwidth_mhz(rms_ds)

    # ---- Doppler / time-variability metrics (moving tx/rx/actors).
    mean_dop, dop_spread, max_dop, coh_time_ms = doppler_metrics(paths, link_doppler)
    cir_time_s, cir_time_env = doppler_time_envelope(
        paths, link_doppler, request.num_time_steps, request.sampling_frequency_hz
    )

    # ---- Channel responses.
    cfr_offsets, cfr_mag = compute_cfr(paths, config.bandwidth_hz, request.num_cfr_points)

    # ---- Empirical model comparison (aerial rows included: the UMa-AV
    # validity flag greys them out on terrestrial links rather than hiding).
    pl_models = evaluate_path_loss_models(
        config.frequency_hz, dist_3d, h_bs, h_ut, rt_path_loss_db,
        include_aerial=True,
    )

    # Plugin-registered models (docs/extending.md) run with the real device
    # endpoints; a plugin failure degrades to an invalid row, never a 500.
    from app.services.plugins import plugin_path_loss_models

    for name, fn in plugin_path_loss_models().items():
        try:
            r = fn(config.frequency_hz, tx, rx, config)
            pl = float(r["path_loss_db"])
            pl_models.append(PathLossModelResult(
                model=name, path_loss_db=pl,
                delta_vs_rt_db=(pl - rt_path_loss_db) if rt_path_loss_db is not None else None,
                valid=bool(r.get("valid", True)), notes=str(r.get("notes", "")),
            ))
        except Exception as exc:  # noqa: BLE001 - plugin isolation contract
            pl_models.append(PathLossModelResult(
                model=name, path_loss_db=None, valid=False,
                notes=f"plugin error: {exc}",
            ))

    return ChannelAnalysisResult(
        tx_id=tx.id,
        rx_id=rx.id,
        backend=backend.name,
        frequency_hz=config.frequency_hz,
        bandwidth_hz=config.bandwidth_hz,
        distance_3d_m=dist_3d,
        rss_dbm=rss_dbm,
        rt_path_loss_db=rt_path_loss_db,
        snr_db=snr_db,
        interference_dbm=interference_dbm,
        num_interferers=len(other_tx_ids),
        sinr_db=sinr_db,
        shannon_capacity_mbps=shannon_capacity_mbps,
        rsrp_dbm=rsrp_dbm,
        rssi_dbm=rssi_dbm,
        rsrq_db=rsrq_db,
        num_resource_blocks=n_rb,
        subcarrier_spacing_khz=scs_khz,
        num_paths=len(paths),
        k_factor_db=kf,
        mean_delay_ns=mean_delay,
        rms_delay_spread_ns=rms_ds,
        coherence_bandwidth_mhz=coh_bw,
        doppler_spread_hz=dop_spread,
        mean_doppler_hz=mean_dop,
        max_doppler_hz=max_dop,
        coherence_time_ms=coh_time_ms,
        cir=cir,
        cfr_freq_offset_hz=cfr_offsets,
        cfr_mag_db=cfr_mag,
        cir_time_s=cir_time_s,
        cir_time_envelope_db=cir_time_env,
        pl_models=pl_models,
        warnings=warnings,
        metadata={
            "frequency_hz": config.frequency_hz,
            "h_bs_m": h_bs,
            "h_ut_m": h_ut,
            "noise_floor_dbm": noise_floor,
            "tx_power_dbm": tx.power_dbm,
            # Device poses AT COMPUTE TIME: the UI shows these so a static
            # link result stays attributable after devices move (provenance).
            "tx_position_m": list(tx.position),
            "rx_position_m": list(rx.position),
            "engine": result.metadata.get("engine"),
        },
    )


# ============================================================ parameter sweep


def sweep_channel(
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    request: ChannelSweepRequest,
) -> ChannelSweepResult:
    """Run analyze_channel once per sweep value with the swept field patched.

    tx_power_dbm lives on the Device, the other sweep fields on the
    SimulationConfig; either way each point is an independent in-memory copy -
    the stored scene/config never change and nothing is persisted. Config
    patches round-trip through validation so an out-of-range value (e.g. a
    negative frequency) surfaces as ValueError/400, not a math domain error.
    """
    config = _resolve_config(scene, request)
    tx, _ = _pick_link(scene, request.tx_id, request.rx_id)
    # The per-point request is the sweep request minus the sweep fields; the
    # (patched) config is always passed inline so config_id resolution happens
    # exactly once, against the original scene.
    base = ChannelAnalysisRequest(
        **request.model_dump(exclude={"sweep_field", "sweep_values", "config", "config_id"})
    )

    rows: list[ChannelSweepPoint] = []
    warnings: list[str] = []
    tx_id = rx_id = backend_name = ""
    for value in request.sweep_values:
        point_scene = scene
        point_config = config
        if request.sweep_field == "tx_power_dbm":
            point_scene = scene.model_copy(update={"devices": [
                d.model_copy(update={"power_dbm": float(value)}) if d.id == tx.id else d
                for d in scene.devices
            ]})
        else:
            point_config = SimulationConfig.model_validate(
                {**config.model_dump(), request.sweep_field: float(value)}
            )
        point_req = base.model_copy(update={"config": point_config})
        res = analyze_channel(project_dir, point_scene, library, point_req)
        tx_id, rx_id, backend_name = res.tx_id, res.rx_id, res.backend
        for w in res.warnings:  # dedupe: points mostly repeat the same warning
            if w not in warnings:
                warnings.append(w)
        rows.append(ChannelSweepPoint(
            value=float(value),
            path_loss_db=res.rt_path_loss_db,
            rss_dbm=res.rss_dbm,
            snr_db=res.snr_db,
            sinr_db=res.sinr_db,
            rms_delay_spread_ns=res.rms_delay_spread_ns,
            k_factor_db=res.k_factor_db,
        ))
    return ChannelSweepResult(
        tx_id=tx_id,
        rx_id=rx_id,
        backend=backend_name,
        sweep_field=request.sweep_field,
        rows=rows,
        warnings=warnings,
    )


# ============================================================== spectrogram
#
# Doppler-time spectrogram (ISAC sensing readout): STFT of the coherent
# complex channel h(t) = sum_i a_i e^{j 2 pi f_d,i t} - the same per-path
# superposition doppler_time_envelope draws, kept complex so the FFT resolves
# WHICH Doppler frequencies are present (signed), not just the fading ripple.

# JSON-payload guardrails: the sample series and the emitted grid stay small
# enough for an interactive response.
MAX_SPECTROGRAM_SAMPLES = 8192
MAX_SPECTROGRAM_CELLS = 131_072


def analyze_spectrogram(
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    request: SpectrogramRequest,
) -> SpectrogramResult:
    """Solve the single link (same backend-resolution path as analyze_channel)
    and STFT the synthesized complex h(t) into a Doppler-time grid.

    Raises ValueError (-> 400) for an unknown config id / missing device and
    for window/hop/duration combinations that cannot produce a frame or would
    blow the grid cap.
    """
    import numpy as np

    config = _resolve_config(scene, request)
    tx, rx = _pick_link(scene, request.tx_id, request.rx_id)

    backend = resolve_backend(config)
    link_cfg = config.model_copy(update={"tx_ids": [tx.id], "rx_ids": [rx.id]})
    result = backend.simulate_paths(project_dir, scene, library, link_cfg)
    warnings = list(result.warnings)
    paths = [p for p in result.paths if p.tx_id == tx.id and p.rx_id == rx.id]

    # Per-path Doppler: backend metadata when present (real solver; includes
    # moving actors), else the geometric estimate from the endpoint device
    # velocities so Doppler-free backends (mock) still produce a physically
    # sensible spectrogram.
    raw_doppler = result.metadata.get("doppler_hz")
    if isinstance(raw_doppler, list) and len(raw_doppler) == len(result.paths):
        by_id = {p.path_id: float(d) for p, d in zip(result.paths, raw_doppler)}
        doppler = [by_id.get(p.path_id, 0.0) for p in paths]
        doppler_source = "backend"
    else:
        doppler = geometric_doppler_hz(paths, tx, rx, config.frequency_hz)
        doppler_source = "geometric"

    fs = request.sampling_frequency_hz
    window = request.window
    hop = request.hop if request.hop is not None else window // 2
    n = int(round(request.duration_s * fs))
    if n > MAX_SPECTROGRAM_SAMPLES:
        warnings.append(
            f"duration clipped to {MAX_SPECTROGRAM_SAMPLES / fs:.3f} s: "
            f"{n} samples exceed the {MAX_SPECTROGRAM_SAMPLES}-sample cap at "
            f"fs={fs:g} Hz"
        )
        n = MAX_SPECTROGRAM_SAMPLES
    if n < window:
        raise ValueError(
            f"duration_s * sampling_frequency_hz = {n} samples is shorter than "
            f"one STFT window ({window}); lengthen duration_s or shrink window"
        )
    num_frames = 1 + (n - window) // hop
    if num_frames * window > MAX_SPECTROGRAM_CELLS:
        raise ValueError(
            f"spectrogram grid {num_frames}x{window} exceeds "
            f"{MAX_SPECTROGRAM_CELLS} cells; increase hop or reduce duration_s"
        )

    max_abs_dop = max((abs(d) for d in doppler), default=0.0)
    if max_abs_dop > fs / 2.0:
        warnings.append(
            f"max |Doppler| {max_abs_dop:.1f} Hz exceeds Nyquist "
            f"{fs / 2.0:.1f} Hz; shifts will alias - raise sampling_frequency_hz"
        )

    t = np.arange(n) / fs
    if paths:
        amps = np.asarray(_path_amplitudes(paths), dtype=complex)
        fds = np.asarray(doppler, dtype=float)
        h = (amps[:, None] * np.exp(2j * np.pi * fds[:, None] * t[None, :])).sum(axis=0)
    else:
        warnings.append("no ray paths for this link; spectrogram is the -300 dB floor")
        h = np.zeros(n, dtype=complex)

    # Hann-windowed STFT, fftshifted so the Doppler axis ascends through 0 Hz
    # at index window//2. Magnitude floored at -300 dB (compute_cfr convention)
    # via the 1e-15 amplitude floor.
    win = np.hanning(window)
    freqs = np.fft.fftshift(np.fft.fftfreq(window, d=1.0 / fs))
    times: list[float] = []
    mags: list[list[float]] = []
    for k in range(num_frames):
        start = k * hop
        spec = np.fft.fftshift(np.fft.fft(h[start:start + window] * win))
        db = 20.0 * np.log10(np.maximum(np.abs(spec), 1e-15))
        times.append(round((start + window / 2.0) / fs, 6))
        mags.append([round(float(v), 3) for v in db])

    return SpectrogramResult(
        tx_id=tx.id,
        rx_id=rx.id,
        backend=backend.name,
        frequency_hz=config.frequency_hz,
        sampling_frequency_hz=fs,
        window=window,
        hop=hop,
        num_paths=len(paths),
        times_s=times,
        doppler_hz=[round(float(f), 6) for f in freqs],
        magnitude_db=mags,
        warnings=warnings,
        metadata={
            "doppler_source": doppler_source,
            "num_samples": n,
            "duration_s": round(n / fs, 6),
            "max_abs_doppler_hz": max_abs_dop,
            "engine": result.metadata.get("engine"),
        },
    )
