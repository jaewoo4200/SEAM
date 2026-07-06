"""Channel analysis contracts: CIR/CFR, dispersion metrics, and empirical
path-loss model comparison (the operator/Wireless-InSite style workflow:
ray-traced prediction side by side with 3GPP TR 38.901 / CI / FSPL models)."""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel
from .simulation import SimulationConfig

PathLossModelName = Literal[
    "fspl",
    "tr38901_uma_los",
    "tr38901_uma_nlos",
    "tr38901_umi_los",
    "tr38901_umi_nlos",
    "tr38901_inh_los",
    "tr38901_inh_nlos",
    "ci_n2",
    "ci_n3",
]


class CirTap(StrictModel):
    delay_ns: float
    power_dbm: float
    phase_rad: float
    path_type: str = "reflection"
    # Per-path Doppler shift [Hz] (v.k/lambda summed over interactions), when
    # the backend supplies it (moving tx/rx or actors). None on backends that
    # do not model Doppler.
    doppler_hz: Optional[float] = None


class PathLossModelResult(StrictModel):
    # Builtin names are PathLossModelName values; plugin-registered models
    # (docs/extending.md) contribute their own names, so the field is open.
    model: str
    path_loss_db: Optional[float] = None
    # Difference vs the ray-traced path loss (model - RT), when both exist.
    delta_vs_rt_db: Optional[float] = None
    valid: bool = True
    notes: str = ""


class ChannelAnalysisRequest(StrictModel):
    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    tx_id: Optional[str] = None  # None = first tx
    rx_id: Optional[str] = None  # None = first rx
    num_cfr_points: int = Field(default=128, ge=8, le=4096)
    # Time-varying CIR (Doppler) evolution. num_time_steps=1 (default) is a
    # static snapshot: no per-tap time series is emitted, but per-path Doppler
    # and the Doppler-spread scalar are always computed from device velocities.
    # >1 synthesizes the coherent CIR magnitude over time via paths.cir().
    # Capped at 64 to keep the response bounded.
    num_time_steps: int = Field(default=1, ge=1, le=64)
    # CIR resampling rate [Hz] for the time evolution window (length =
    # num_time_steps / sampling_frequency_hz seconds). None => 2x the max
    # per-path |Doppler| (Nyquist) so the fastest tap is resolved, falling back
    # to 1 kHz when no path has any Doppler.
    sampling_frequency_hz: Optional[float] = Field(default=None, gt=0.0)
    # OFDM subcarrier spacing [kHz] for the 3GPP measurement quantities
    # (RSRP/RSSI/RSRQ resource grid). 30 kHz = 5G NR FR1 default; 15 = LTE.
    subcarrier_spacing_khz: float = Field(default=30.0, gt=0.0)


class ChannelAnalysisResult(StrictModel):
    tx_id: str
    rx_id: str
    backend: str
    frequency_hz: float
    bandwidth_hz: float
    distance_3d_m: float
    # Ray-traced link budget.
    rss_dbm: Optional[float] = None
    rt_path_loss_db: Optional[float] = None
    snr_db: Optional[float] = None
    shannon_capacity_mbps: Optional[float] = None
    # 3GPP measurement quantities (TS 38.215-style, derived from the
    # ray-traced wideband RSS over an OFDM grid at subcarrier_spacing_khz):
    # RSRP = per-resource-element power = RSS - 10log10(num_subcarriers);
    # RSSI = wideband signal + noise power; RSRQ = N_RB * RSRP / RSSI.
    rsrp_dbm: Optional[float] = None
    rssi_dbm: Optional[float] = None
    rsrq_db: Optional[float] = None
    num_resource_blocks: Optional[int] = None
    subcarrier_spacing_khz: float = 30.0
    # Dispersion / fading metrics.
    num_paths: int = 0
    k_factor_db: Optional[float] = None  # LoS power / sum(NLoS); None if no LoS
    mean_delay_ns: Optional[float] = None
    rms_delay_spread_ns: Optional[float] = None
    coherence_bandwidth_mhz: Optional[float] = None  # ~1/(2*pi*rms_ds)
    # Doppler / time-variability metrics (moving tx/rx/actors). All None on
    # backends that do not model Doppler, or when nothing in the link moves.
    doppler_spread_hz: Optional[float] = None  # power-weighted std of per-path Doppler
    mean_doppler_hz: Optional[float] = None  # power-weighted mean shift
    max_doppler_hz: Optional[float] = None  # max |per-path Doppler|
    coherence_time_ms: Optional[float] = None  # ~0.42 / max|Doppler|
    # Channel responses.
    cir: list[CirTap] = Field(default_factory=list)
    cfr_freq_offset_hz: list[float] = Field(default_factory=list)
    cfr_mag_db: list[float] = Field(default_factory=list)
    # Time-varying channel envelope (only when request.num_time_steps > 1),
    # i.e. the Doppler fading curve |h(t)| = |sum_i a_i e^{j2 pi f_d,i t}| in dB
    # sampled at cir_time_s seconds. Per-tap magnitude is time-invariant under
    # pure Doppler (only phase rotates), so the useful time series is the
    # coherent sum: its ripple is the fading the Doppler spread produces.
    cir_time_s: list[float] = Field(default_factory=list)
    cir_time_envelope_db: list[float] = Field(default_factory=list)
    # Empirical model comparison.
    pl_models: list[PathLossModelResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
