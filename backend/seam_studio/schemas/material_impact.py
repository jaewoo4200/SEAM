"""Material-aware vs baseline channel-impact contracts.

The CFR-based evaluation framework from Lee et al. (KICS 2026): compare the
scene's assigned materials against a single-material baseline along a set of
positions - NMSE, cosine similarity, signed dRSS, and a Shannon throughput
proxy quantify how much the material mapping changes the channel.
"""

from typing import Optional

from pydantic import Field

from .common import StrictModel, Vec3
from .simulation import SimulationConfig


class MaterialImpactRequest(StrictModel):
    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    tx_id: Optional[str] = None  # None = first tx
    rx_id: Optional[str] = None  # None = first rx (moved along waypoints)
    # Positions to evaluate; None = the current RX position only.
    waypoints: Optional[list[Vec3]] = None
    # Every prim is rebound to this material for the baseline run.
    baseline_material_id: str = "itu_concrete"
    num_cfr_points: int = Field(default=128, ge=8, le=2048)
    # Per-position NMSE above this marks the position "material-sensitive"
    # (KICS uses -60 dB).
    sensitive_nmse_db: float = -60.0


class PositionImpact(StrictModel):
    position: Vec3
    nmse_db: Optional[float] = None
    cosine_similarity: Optional[float] = None
    delta_rss_db: Optional[float] = None  # material-aware minus baseline
    rss_material_dbm: Optional[float] = None
    rss_baseline_dbm: Optional[float] = None
    material_sensitive: bool = False


class MaterialImpactReport(StrictModel):
    baseline_material_id: str
    tx_id: str
    rx_id: str
    global_nmse_db: Optional[float] = None
    mean_cosine_similarity: Optional[float] = None
    mean_delta_rss_db: Optional[float] = None
    # Shannon throughput proxy (Mbps) means over positions with a channel.
    mean_capacity_material_mbps: Optional[float] = None
    mean_capacity_baseline_mbps: Optional[float] = None
    material_sensitive_count: int = 0
    positions: list[PositionImpact] = Field(default_factory=list)
    backend: str
    warnings: list[str] = Field(default_factory=list)
