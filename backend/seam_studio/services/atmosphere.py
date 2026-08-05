"""Atmospheric gas attenuation (ITU-R P.676) as a per-path post-process.

Sionna RT models no atmospheric absorption at all (verified: package-wide
grep, and a DeepVerse DT31 cross-check where Wireless InSite GT carried an
exactly linear 14.17 dB/km oxygen term at 60 GHz that SEAM lacked to three
decimals). Because the effect is a pure per-path exponential, it can be
applied outside the solver: gain_db -= alpha(f) * path_length_km, with
path_length = delay * c. This applies to path-based outputs (paths, channel
analysis, trajectory, beamforming); grid/mesh radio maps are computed inside
the solver and are NOT corrected.

The built-in curve below is a COARSE reading of the ITU-R P.676 sea-level
standard-atmosphere specific-attenuation curve (1013 hPa, 15 C, 7.5 g/m3
water vapour): anchors are order-of-magnitude faithful (the 60 GHz oxygen
band peak ~15 dB/km, the 22.2 GHz water line ~0.2 dB/km) but between anchors
the error can reach a factor of ~2. Quantitative comparisons should set
``SimulationConfig.absorption_db_per_km`` explicitly — the solve emits a
warning whenever the approximate curve is used.
"""

import bisect
import math
from typing import Optional

SPEED_OF_LIGHT = 299_792_458.0

# (frequency_GHz, specific attenuation dB/km) — coarse ITU-R P.676 anchors,
# sea level standard atmosphere. Log-log interpolated between anchors.
_P676_APPROX: list[tuple[float, float]] = [
    (1.0, 0.006),
    (6.0, 0.009),
    (10.0, 0.02),
    (15.0, 0.05),
    (22.2, 0.20),   # water-vapour line
    (26.0, 0.12),
    (32.0, 0.10),
    (38.0, 0.13),
    (45.0, 0.35),
    (50.0, 1.2),
    (54.0, 6.0),
    (57.0, 12.0),
    (60.0, 15.0),   # oxygen band peak
    (63.0, 12.0),
    (66.0, 5.0),
    (70.0, 1.5),
    (75.0, 0.6),
    (85.0, 0.45),
    (100.0, 0.7),
]


def _approx_db_per_km(frequency_hz: float) -> float:
    f = frequency_hz / 1e9
    lo_f, lo_a = _P676_APPROX[0]
    hi_f, hi_a = _P676_APPROX[-1]
    if f <= lo_f:
        return lo_a
    if f >= hi_f:
        return hi_a
    idx = bisect.bisect_left([p[0] for p in _P676_APPROX], f)
    f0, a0 = _P676_APPROX[idx - 1]
    f1, a1 = _P676_APPROX[idx]
    # Log-log interpolation: the curve spans 3+ decades and is closer to
    # piecewise power-law than linear.
    t = (math.log(f) - math.log(f0)) / (math.log(f1) - math.log(f0))
    return math.exp(math.log(a0) + t * (math.log(a1) - math.log(a0)))


def absorption_db_per_km(config) -> tuple[float, Optional[str]]:
    """(alpha_db_per_km, warning) for a SimulationConfig-like object.

    0.0 with no warning when atmospheric_absorption is off; the explicit
    override verbatim when set; else the approximate built-in curve WITH a
    warning so approximate numbers are never mistaken for P.676-exact ones.
    """
    if not getattr(config, "atmospheric_absorption", False):
        return 0.0, None
    override = getattr(config, "absorption_db_per_km", None)
    if override is not None:
        return float(override), None
    alpha = _approx_db_per_km(config.frequency_hz)
    return alpha, (
        f"atmospheric absorption uses the built-in APPROXIMATE P.676 curve "
        f"({alpha:.2f} dB/km at {config.frequency_hz / 1e9:.1f} GHz; coarse "
        "anchors, up to ~2x off between them) — set absorption_db_per_km "
        "explicitly for quantitative comparisons"
    )


def path_attenuation_db(alpha_db_per_km: float, delay_s: float) -> float:
    """Attenuation over one path: alpha * (delay * c) in km."""
    if alpha_db_per_km <= 0.0 or delay_s <= 0.0:
        return 0.0
    return alpha_db_per_km * (delay_s * SPEED_OF_LIGHT / 1000.0)
