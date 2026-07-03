"""Automated model-validation checklist (docs/model_validation.md section 5).

Pure-python, no GPU, no ray tracer. Each test pins a piece of
``app.services.channel_analysis`` to an *independent* closed-form computation
of the 3GPP TR 38.901 / Friis / thermal-noise reference, so a silent drift in
the implementation constants would fail here.

Mapping to the doc's section-5 items:
  * item A  -> FSPL exactness vs the Friis closed form (3 freqs x distances)
  * item B  -> 38.901 UMa/UMi/InH LOS spot values at the reference geometries,
               plus breakpoint continuity (PL1(dBP)==PL2(dBP)) and NLOS>=LOS
  * item D  -> CFR<->CIR consistency (compute_cfr vs a manual tap sum) and the
               thermal noise-floor value

Breakpoint continuity note: for the 38.901 dual-slope LOS models the transition
IS analytically continuous. At d_2D = d'_BP we have d_3D^2 = d'_BP^2 + (h_BS -
h_UT)^2, so log10(d'_BP^2 + dh^2) = 2*log10(d_3D); the second-slope correction
-9*(2 log10 d3D) turns 40 log10 d3D into 22 log10 d3D (UMa), matching the first
slope exactly (and 40 - 2*9.5 = 21 for UMi). So we assert equality within
0.1 dB rather than only monotonicity.
"""

import math

import pytest

from app.schemas.simulation import SimulationConfig
from app.services import channel_analysis as ca
from app.services.simulation_backends.sionna_backend import noise_floor_dbm

C = 299_792_458.0  # speed of light [m/s], matches channel_analysis.SPEED_OF_LIGHT


# ============================================================ (1) FSPL exactness


@pytest.mark.parametrize(
    "freq_hz, dist_m",
    [
        (900e6, 10.0),
        (2.4e9, 100.0),
        (28e9, 250.0),
    ],
)
def test_fspl_matches_friis_closed_form(freq_hz: float, dist_m: float):
    # Independent Friis free-space path loss: 20 log10(4*pi*d*f/c).
    expected = 20.0 * math.log10(4.0 * math.pi * dist_m * freq_hz / C)
    got = ca.fspl_db(freq_hz, dist_m)
    assert got == pytest.approx(expected, abs=1e-9)


def test_fspl_matches_constant_form_92_45():
    # With f in GHz and d in km the additive constant is 92.45 dB.
    freq_hz, dist_m = 3.5e9, 1000.0
    expected = 92.45 + 20.0 * math.log10(freq_hz / 1e9) + 20.0 * math.log10(dist_m / 1e3)
    assert ca.fspl_db(freq_hz, dist_m) == pytest.approx(expected, abs=0.01)


# ================================================ (2) 38.901 LOS spot values


def _d3d(d_2d: float, h_bs: float, h_ut: float) -> float:
    return math.sqrt(d_2d * d_2d + (h_bs - h_ut) ** 2)


def test_uma_los_reference_point():
    # 38.901 reference geometry: UMa hBS=25 m, hUT=1.5 m, fc=2 GHz, d_2D=100 m
    # (< breakpoint, so first slope). PL = 28 + 22 log10(d3D) + 20 log10(fc).
    freq_hz, fc, h_bs, h_ut, d_2d = 2e9, 2.0, 25.0, 1.5, 100.0
    d3d = _d3d(d_2d, h_bs, h_ut)
    expected = 28.0 + 22.0 * math.log10(d3d) + 20.0 * math.log10(fc)
    got = ca._uma_los(freq_hz, d_2d, d3d, h_bs, h_ut)
    assert got == pytest.approx(expected, abs=0.01)


def test_umi_los_reference_point():
    # UMi hBS=10 m, hUT=1.5 m, fc=28 GHz, d_2D=50 m (< breakpoint, first slope).
    # PL = 32.4 + 21 log10(d3D) + 20 log10(fc).
    freq_hz, fc, h_bs, h_ut, d_2d = 28e9, 28.0, 10.0, 1.5, 50.0
    d3d = _d3d(d_2d, h_bs, h_ut)
    expected = 32.4 + 21.0 * math.log10(d3d) + 20.0 * math.log10(fc)
    got = ca._umi_los(freq_hz, d_2d, d3d, h_bs, h_ut)
    assert got == pytest.approx(expected, abs=0.01)


def test_inh_los_reference_point():
    # Indoor-office, fc=28 GHz, d_3D=20 m. PL = 32.4 + 17.3 log10(d3D) + 20 log10(fc).
    freq_hz, fc, d3d = 28e9, 28.0, 20.0
    expected = 32.4 + 17.3 * math.log10(d3d) + 20.0 * math.log10(fc)
    got = ca._inh_los(freq_hz, d3d)
    assert got == pytest.approx(expected, abs=0.01)


def test_38901_nlos_never_below_los():
    # 38.901 NLOS = max(LOS, NLOS'); NLOS path loss must be >= LOS everywhere.
    freq_hz = 28e9
    for d_2d in (20.0, 100.0, 500.0, 2000.0):
        for h_bs, h_ut in ((25.0, 1.5), (10.0, 1.5)):
            d3d = _d3d(d_2d, h_bs, h_ut)
            uma_los = ca._uma(freq_hz, d_2d, d3d, h_bs, h_ut, los=True)
            uma_nlos = ca._uma(freq_hz, d_2d, d3d, h_bs, h_ut, los=False)
            assert uma_nlos >= uma_los - 1e-9
            umi_los = ca._umi(freq_hz, d_2d, d3d, h_bs, h_ut, los=True)
            umi_nlos = ca._umi(freq_hz, d_2d, d3d, h_bs, h_ut, los=False)
            assert umi_nlos >= umi_los - 1e-9
    for d3d in (5.0, 50.0, 140.0):
        inh_los = ca._inh(freq_hz, d3d, los=True)
        inh_nlos = ca._inh(freq_hz, d3d, los=False)
        assert inh_nlos >= inh_los - 1e-9


# ==================================================== (3) breakpoint continuity


@pytest.mark.parametrize(
    "los_fn, h_bs, h_ut, freq_hz",
    [
        (ca._uma_los, 25.0, 1.5, 28e9),
        (ca._uma_los, 25.0, 1.5, 2e9),
        (ca._umi_los, 10.0, 1.5, 28e9),
        (ca._umi_los, 10.0, 1.5, 3.5e9),
    ],
)
def test_breakpoint_is_continuous(los_fn, h_bs: float, h_ut: float, freq_hz: float):
    # Evaluate the dual-slope LOS model on both sides of, and at, the 2D
    # breakpoint. The two slopes must agree at d_BP within 0.1 dB (they are
    # analytically equal -- see module docstring).
    d_bp = ca._breakpoint_distance(h_bs, h_ut, freq_hz)
    assert d_bp > 0.0
    eps = 1e-4  # meters; straddle the breakpoint
    d3d_at = _d3d(d_bp, h_bs, h_ut)
    d3d_lo = _d3d(d_bp - eps, h_bs, h_ut)
    d3d_hi = _d3d(d_bp + eps, h_bs, h_ut)
    pl_lo = los_fn(freq_hz, d_bp - eps, d3d_lo, h_bs, h_ut)  # first slope branch
    pl_hi = los_fn(freq_hz, d_bp + eps, d3d_hi, h_bs, h_ut)  # second slope branch
    pl_at = los_fn(freq_hz, d_bp, d3d_at, h_bs, h_ut)
    # Continuity: the jump across the breakpoint is far under 0.1 dB.
    assert abs(pl_hi - pl_lo) < 0.1
    assert abs(pl_at - pl_lo) < 0.1


# ==================================================== (4) CFR <-> CIR consistency


def _mk_paths():
    from app.schemas.results import RayPath

    verts = [[0.0, 0.0, 10.0], [20.0, 0.0, 1.5]]
    return [
        RayPath(
            path_id="p0", tx_id="tx", rx_id="rx", path_type="los",
            vertices=verts, delay_ns=50.0, power_dbm=-70.0, phase_rad=0.3,
        ),
        RayPath(
            path_id="p1", tx_id="tx", rx_id="rx", path_type="reflection",
            vertices=verts, delay_ns=83.0, power_dbm=-88.5, phase_rad=1.7,
        ),
        RayPath(
            path_id="p2", tx_id="tx", rx_id="rx", path_type="reflection",
            vertices=verts, delay_ns=120.0, power_dbm=-95.0, phase_rad=-2.1,
        ),
    ]


def _manual_cfr_mag_db(paths, freq_offset_hz: float) -> float:
    # Independent re-derivation of H(f) = sum_l a_l exp(-j 2 pi f tau_l),
    # |a_l| = sqrt(10^(P_dbm/10)) linear voltage amplitude.
    acc = 0j
    for p in paths:
        amp = math.sqrt(10.0 ** (p.power_dbm / 10.0))
        a = amp * complex(math.cos(p.phase_rad), math.sin(p.phase_rad))
        angle = -2.0 * math.pi * freq_offset_hz * (p.delay_ns * 1e-9)
        acc += a * complex(math.cos(angle), math.sin(angle))
    mag = abs(acc)
    return 20.0 * math.log10(mag) if mag > 0.0 else -300.0


def test_cfr_matches_manual_tap_sum():
    paths = _mk_paths()
    bandwidth_hz = 100e6
    num_points = 9
    offsets, mags_db = ca.compute_cfr(paths, bandwidth_hz, num_points)
    assert len(offsets) == num_points == len(mags_db)
    for f, mag_db in zip(offsets, mags_db):
        assert mag_db == pytest.approx(_manual_cfr_mag_db(paths, f), abs=1e-9)


def test_cfr_dc_bin_is_coherent_tap_sum():
    # The zero-offset (DC) bin equals the coherent sum of the tap voltages
    # (magnitude), the classic CFR<->CIR consistency check (item D).
    paths = _mk_paths()
    offsets, mags_db = ca.compute_cfr(paths, 100e6, 1)  # single point -> f=0
    assert offsets == [0.0]
    coherent = 0j
    for p in paths:
        amp = math.sqrt(10.0 ** (p.power_dbm / 10.0))
        coherent += amp * complex(math.cos(p.phase_rad), math.sin(p.phase_rad))
    expected_db = 20.0 * math.log10(abs(coherent))
    assert mags_db[0] == pytest.approx(expected_db, abs=1e-9)


# ==================================================== (5) noise floor value


def test_noise_floor_100mhz_nf7():
    # kTB (-174 dBm/Hz) + 10 log10(100e6) + NF 7 dB.
    # 10 log10(1e8) = 80 exactly, so the exact value is -174 + 80 + 7 = -87.00 dBm.
    # (docs/model_validation.md item 5 quotes "-86.99" as a rounded figure; the
    # mathematically exact value from the implemented formula is -87.00.)
    cfg = SimulationConfig(bandwidth_hz=100e6, noise_figure_db=7.0)
    expected = -174.0 + 10.0 * math.log10(100e6) + 7.0
    assert expected == pytest.approx(-87.0, abs=1e-12)
    assert noise_floor_dbm(cfg) == pytest.approx(expected, abs=0.01)
    assert noise_floor_dbm(cfg) == pytest.approx(-87.0, abs=0.01)
