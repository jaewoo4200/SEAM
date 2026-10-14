"""OFDM numerology of the sensing waveform (#23).

One ``WaveformOptions`` resolves, at a CPI length ``cpi_s``, into the pilot
lattice the delay-Doppler map and the channel tensors use, and into the
numbers every sensing service shares:

    T_sym      = (1 ms / 2^mu) / symbols per slot      (NR numerology mu)
               = (1 / scs) (1 + CP ratio)               (any other scs)
    symbols    = symbols_per_cpi or floor(cpi_s / T_sym)
    K x N      = ceil(n_active_sc / subcarrier_step) x ceil(symbols / symbol_step)
    gain       = 10 log10(K N)                          (= cpi_pulses)
    B_occ      = n_active_sc * scs
    noise      = -174 dBm/Hz + 10 log10(B_occ) + NF

Amplitude convention (one for all new code): H is the dimensionless path
gain; every active subcarrier carries Es = P_tx / n_active_sc per resource
element (CP excluded) and the noise per RE is N0 = kT0 F scs, so the
per-RE SNR of a path, |a|^2 Es / N0 = P_echo - N_floor(B_occ), is the
SNR the threshold detector integrates over K N pilot REs.
"""

import math
from dataclasses import asdict, dataclass
from typing import Optional

import numpy as np

from seam_studio.schemas.sensing import PilotOptions, WaveformOptions
from seam_studio.services.sensing import SensingRequestError

SPEED_OF_LIGHT = 299_792_458.0
# Kept for the default: the CPI the scenario, ISAC and coverage requests ship with.
DEFAULT_CPI_S = 0.01
# Pilot REs per CPI (cpi_pulses); the request schemas cap cpi_pulses alike.
MAX_RE_PER_CPI = 100_000_000
CP_RATIO = {"normal": 144 / 2048, "extended": 512 / 2048}
NR_SYMBOLS_PER_SLOT = {"normal": 14, "extended": 12}
THERMAL_NOISE_DBM_PER_HZ = -174.0

# Grid S of PLAIN_SIONNA_PLAN 2.4: 1280 SC x 15.625 kHz (20 MHz), comb-5
# pilots on every 9th symbol of 1/14400 s -> K 256 x N 16 = 4096 per 10 ms.
GRID_S = WaveformOptions(
    scs_hz=15_625.0,
    n_fft=2048,
    n_active_sc=1280,
    cp="normal",
    symbol_duration_s=1.0 / 14_400,
    pilot=PilotOptions(pattern="comb", subcarrier_step=5, symbol_step=9),
)
# 5G NR FR1 20 MHz carrier at 30 kHz: 51 PRB = 612 SC, every RE a pilot.
NR_30K_51PRB = WaveformOptions(scs_hz=30e3, n_fft=1024, n_active_sc=612, cp="normal")


class NumerologyError(SensingRequestError):
    """The waveform does not fit the CPI (API -> 400)."""


def symbol_duration_s(w: WaveformOptions) -> float:
    """Average OFDM symbol duration incl. CP [s]."""
    if w.symbol_duration_s is not None:
        return float(w.symbol_duration_s)
    mu = math.log2(w.scs_hz / 15e3)
    if abs(mu - round(mu)) < 1e-9 and 0 <= round(mu) <= 6:
        return (1e-3 / 2 ** round(mu)) / NR_SYMBOLS_PER_SLOT[w.cp]
    return (1.0 / w.scs_hz) * (1.0 + CP_RATIO[w.cp])


@dataclass(frozen=True)
class Numerology:
    scs_hz: float
    n_fft: int
    n_active_sc: int
    cp: str
    symbol_duration_s: float
    symbols_per_cpi: int
    cpi_s: float
    cpi_eff_s: float
    subcarrier_step: int
    symbol_step: int
    grid_k: int
    grid_n: int
    n_re_per_cpi: int
    pilot_fraction: float
    b_occ_hz: float
    pilot_spacing_hz: float
    pilot_period_s: float
    delay_bin_s: float
    unambiguous_delay_s: float
    doppler_resolution_hz: float
    unambiguous_doppler_hz: float
    range_resolution_m: float
    bistatic_range_resolution_m: float
    integration_gain_db: float
    pilot_pattern: str = "comb"

    def noise_floor_dbm(self, nf_db: float) -> float:
        return THERMAL_NOISE_DBM_PER_HZ + 10.0 * math.log10(self.b_occ_hz) + float(nf_db)

    def n0_dbm_per_re(self, nf_db: float) -> float:
        return THERMAL_NOISE_DBM_PER_HZ + 10.0 * math.log10(self.scs_hz) + float(nf_db)

    def es_dbm_per_re(self, p_tx_dbm: float) -> float:
        return float(p_tx_dbm) - 10.0 * math.log10(self.n_active_sc)

    def sigma_h2(
        self, p_tx_dbm: float, nf_db: float, n0_dbm_per_re: Optional[float] = None
    ) -> float:
        """Noise variance of the LS channel estimate per RE: N0 / Es
        (= N_floor(B_occ) / P_tx)."""
        n0 = self.n0_dbm_per_re(nf_db) if n0_dbm_per_re is None else float(n0_dbm_per_re)
        return 10.0 ** ((n0 - self.es_dbm_per_re(p_tx_dbm)) / 10.0)

    def axes(self, subcarriers: str = "pilot") -> tuple[np.ndarray, np.ndarray]:
        """(f_hz, t_s) float64: baseband subcarrier frequencies (centred like
        sionna.rt.subcarrier_frequencies) and symbol times of the pilot
        lattice ("pilot") or of every active RE ("active")."""
        half = self.n_active_sc // 2
        if subcarriers == "pilot":
            k = np.arange(self.grid_k, dtype=np.float64)
            f = (k * self.subcarrier_step - half) * self.scs_hz
            t = np.arange(self.grid_n, dtype=np.float64) * self.pilot_period_s
            return f, t
        if subcarriers == "active":
            m = np.arange(self.n_active_sc, dtype=np.float64)
            f = (m - half) * self.scs_hz
            t = np.arange(self.symbols_per_cpi, dtype=np.float64) * self.symbol_duration_s
            return f, t
        raise ValueError(f"subcarriers must be 'pilot' or 'active', got {subcarriers!r}")

    def as_dict(self) -> dict:
        return asdict(self)


def resolve_numerology(w: WaveformOptions, cpi_s: float) -> Numerology:
    t_sym = symbol_duration_s(w)
    if w.symbols_per_cpi is not None:
        symbols = int(w.symbols_per_cpi)
        if symbols * t_sym > cpi_s * (1.0 + 1e-9):
            raise NumerologyError(
                f"waveform.symbols_per_cpi {symbols} x {t_sym:.6g} s = "
                f"{symbols * t_sym:.6g} s do not fit the CPI (cpi_s {cpi_s:g} s)"
            )
    else:
        symbols = int(math.floor(cpi_s / t_sym + 1e-9))
        if symbols < 1:
            raise NumerologyError(
                f"cpi_s {cpi_s:g} s is shorter than one waveform symbol ({t_sym:.6g} s)"
            )
    sc_step = int(w.pilot.subcarrier_step)
    sym_step = int(w.pilot.symbol_step)
    grid_k = -(-int(w.n_active_sc) // sc_step)
    grid_n = -(-symbols // sym_step)
    n_re = grid_k * grid_n
    if n_re > MAX_RE_PER_CPI:
        raise NumerologyError(
            f"the waveform has {grid_k} x {grid_n} = {n_re} pilot REs per CPI (cpi_s "
            f"{cpi_s:g} s); at most {MAX_RE_PER_CPI} (shorten cpi_s or thin the pilot lattice)"
        )
    b_occ = float(w.n_active_sc) * float(w.scs_hz)
    pilot_spacing = sc_step * float(w.scs_hz)
    pilot_period = sym_step * t_sym
    return Numerology(
        scs_hz=float(w.scs_hz),
        n_fft=int(w.n_fft),
        n_active_sc=int(w.n_active_sc),
        cp=w.cp,
        symbol_duration_s=t_sym,
        symbols_per_cpi=symbols,
        cpi_s=float(cpi_s),
        cpi_eff_s=symbols * t_sym,
        subcarrier_step=sc_step,
        symbol_step=sym_step,
        grid_k=grid_k,
        grid_n=grid_n,
        n_re_per_cpi=n_re,
        pilot_fraction=1.0 / (sc_step * sym_step),
        b_occ_hz=b_occ,
        pilot_spacing_hz=pilot_spacing,
        pilot_period_s=pilot_period,
        delay_bin_s=1.0 / (grid_k * pilot_spacing),
        unambiguous_delay_s=1.0 / pilot_spacing,
        doppler_resolution_hz=1.0 / (grid_n * pilot_period),
        unambiguous_doppler_hz=1.0 / pilot_period,
        range_resolution_m=SPEED_OF_LIGHT / (2.0 * b_occ),
        bistatic_range_resolution_m=SPEED_OF_LIGHT / b_occ,
        integration_gain_db=10.0 * math.log10(n_re),
        pilot_pattern=w.pilot.pattern,
    )


def effective_cpi_pulses(
    waveform: Optional[WaveformOptions],
    cpi_s: float,
    cpi_pulses: int,
    explicit: bool,
    where: str,
) -> tuple[int, list[str], Optional[Numerology]]:
    """(cpi_pulses to compute with, warnings, numerology). Without a waveform
    the request's cpi_pulses stands (v0.1.15); with one it is the pilot RE
    count K N per CPI. ``explicit``: cpi_pulses was set in the request."""
    warnings: list[str] = []
    if waveform is None:
        if explicit and abs(cpi_s - DEFAULT_CPI_S) > 1e-12:
            warnings.append(
                f"{where}: integration gain does not follow cpi_s without sensing.waveform "
                f"(cpi_s {cpi_s:g} s, cpi_pulses {cpi_pulses} -> "
                f"{10.0 * math.log10(cpi_pulses):.2f} dB)"
            )
        return int(cpi_pulses), warnings, None
    num = resolve_numerology(waveform, cpi_s)
    if explicit and int(cpi_pulses) != num.n_re_per_cpi:
        warnings.append(
            f"{where}: cpi_pulses {cpi_pulses} overridden by the waveform numerology: "
            f"{num.n_re_per_cpi} pilot REs per CPI ({num.grid_k} x {num.grid_n})"
        )
    return num.n_re_per_cpi, warnings, num
