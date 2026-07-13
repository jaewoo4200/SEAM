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
    # 3GPP TR 36.777 UMa-AV air-to-ground (UAV) models, Annex B.1.2.
    "tr36777_uma_av_los",
    "tr36777_uma_av_nlos",
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
    # Co-channel interference from every OTHER TX in the scene (full-buffer
    # assumption: all transmit simultaneously on the same resources). None
    # when the scene has one TX or no interferer path reaches this RX.
    interference_dbm: Optional[float] = None
    num_interferers: int = 0
    # SINR = S / (I + N). Equals snr_db when there is no interference.
    sinr_db: Optional[float] = None
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


# Sweepable link knobs: SimulationConfig scalars plus the serving TX power.
# Each is a scalar the single-link analysis consumes directly, so a sweep is
# exactly "the same analysis with one field patched per point".
ChannelSweepField = Literal[
    "frequency_hz",
    "tx_power_dbm",
    "bandwidth_hz",
    "noise_figure_db",
]


class ChannelSweepRequest(ChannelAnalysisRequest):
    """Body for POST /analyze/channel-sweep: the normal single-link selection
    plus the field to sweep and the values to evaluate it at. Nothing is
    persisted; each point is an independent on-demand analysis."""

    sweep_field: ChannelSweepField
    # 2..50 points: one point is not a sweep, and 50 solver runs is the ceiling
    # for an interactive readout.
    sweep_values: list[float] = Field(min_length=2, max_length=50)


class ChannelSweepPoint(StrictModel):
    """One sweep row: the swept value plus the scalar KPIs of that analysis.
    All KPIs inherit the null-ness of ChannelAnalysisResult (e.g. no LoS path
    -> k_factor_db is None)."""

    value: float
    path_loss_db: Optional[float] = None  # ray-traced (tx power - RSS)
    rss_dbm: Optional[float] = None
    snr_db: Optional[float] = None
    sinr_db: Optional[float] = None
    rms_delay_spread_ns: Optional[float] = None
    k_factor_db: Optional[float] = None


class ChannelSweepResult(StrictModel):
    tx_id: str
    rx_id: str
    backend: str
    sweep_field: ChannelSweepField
    rows: list[ChannelSweepPoint] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class SpectrogramRequest(StrictModel):
    """Body for POST /analyze/spectrogram: Doppler-time spectrogram of the
    coherent channel h(t) (ISAC sensing readout). Deliberately its own schema:
    the spectrogram needs thousands of time samples, far beyond the 64-step cap
    the normal analysis keeps for its inline envelope."""

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    tx_id: Optional[str] = None  # None = first tx
    rx_id: Optional[str] = None  # None = first rx
    # Observation window [s]; long captures resolve slow Doppler, capped so
    # duration*fs stays a JSON-sized series (see the 8192-sample service cap).
    duration_s: float = Field(default=1.0, gt=0.0, le=10.0)
    # h(t) sampling rate [Hz]; must exceed 2x the largest expected |Doppler|
    # (Nyquist) or shifts alias into the wrong bin.
    sampling_frequency_hz: float = Field(default=500.0, gt=0.0, le=4000.0)
    # STFT window length in samples (Hann). Powers of two keep the FFT cheap;
    # the default 128 gives fs/128 Hz Doppler resolution.
    window: int = Field(default=128, ge=8, le=512)
    # Frame advance in samples; None = window // 2 (50% overlap).
    hop: Optional[int] = Field(default=None, ge=1, le=512)


class SpectrogramResult(StrictModel):
    tx_id: str
    rx_id: str
    backend: str
    frequency_hz: float
    sampling_frequency_hz: float
    window: int
    hop: int
    num_paths: int = 0
    # magnitude_db[i][j] is frame times_s[i] at Doppler bin doppler_hz[j];
    # bins are fftshifted so 0 Hz sits at index window//2 (ascending axis).
    times_s: list[float] = Field(default_factory=list)
    doppler_hz: list[float] = Field(default_factory=list)
    magnitude_db: list[list[float]] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
