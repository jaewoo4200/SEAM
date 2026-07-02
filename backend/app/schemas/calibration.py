"""Measurement-based calibration contracts (HANDOFF Milestone 11).

Import measured per-link path gain, compare against the ray-traced prediction,
and fit one RF material parameter to reduce the RT-vs-measurement error. A
level offset absorbs the (usually uncalibrated) absolute measurement power, so
the reported RMSE reflects the *shape* error the material fit can actually fix.
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel, Vec3
from .simulation import SimulationConfig

CalibParam = Literal[
    "scattering_coefficient", "relative_permittivity", "conductivity_s_per_m"
]


class MeasurementSample(StrictModel):
    # RX (receiver) position in meters, Z-up.
    rx_position: Vec3
    tx_id: Optional[str] = None  # None = first tx
    measured_path_gain_db: float
    measured_rms_delay_spread_ns: Optional[float] = None


class CalibrationRequest(StrictModel):
    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    measurements: list[MeasurementSample] = Field(min_length=1)
    target_material_id: str
    param: CalibParam = "scattering_coefficient"
    # Explicit grid of candidate values; None = a sensible default per param.
    grid: Optional[list[float]] = None
    # When true, write the fitted value into the project material library and
    # mark prims using it as measurement_calibrated. Default: report only.
    apply: bool = False


class LinkError(StrictModel):
    rx_position: Vec3
    measured_path_gain_db: float
    simulated_path_gain_db: float
    error_db: float  # simulated (level-aligned) - measured


class CalibrationStats(StrictModel):
    n_links: int
    level_offset_db: float
    rmse_db: float
    mean_abs_error_db: float


class CalibrationReport(StrictModel):
    target_material_id: str
    param: CalibParam
    baseline_value: Optional[float] = None
    fitted_value: Optional[float] = None
    before: CalibrationStats
    after: CalibrationStats
    grid_values: list[float] = Field(default_factory=list)
    # None marks grid values whose compile failed (skipped, not zero-error).
    grid_rmse_db: list[Optional[float]] = Field(default_factory=list)
    per_link_after: list[LinkError] = Field(default_factory=list)
    applied: bool = False
    backend: str = ""
    warnings: list[str] = Field(default_factory=list)
