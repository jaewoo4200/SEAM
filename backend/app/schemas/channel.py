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
    # Dispersion / fading metrics.
    num_paths: int = 0
    k_factor_db: Optional[float] = None  # LoS power / sum(NLoS); None if no LoS
    mean_delay_ns: Optional[float] = None
    rms_delay_spread_ns: Optional[float] = None
    coherence_bandwidth_mhz: Optional[float] = None  # ~1/(2*pi*rms_ds)
    # Channel responses.
    cir: list[CirTap] = Field(default_factory=list)
    cfr_freq_offset_hz: list[float] = Field(default_factory=list)
    cfr_mag_db: list[float] = Field(default_factory=list)
    # Empirical model comparison.
    pl_models: list[PathLossModelResult] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)
