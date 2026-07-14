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
    # Optional stable id carried from the imported CSV (measurement_id column);
    # lets the UI/exports round-trip a caller's own row identity.
    measurement_id: Optional[str] = None
    # Optional capture time in seconds (drive/flight log); when present the
    # import and the trajectory validation keep samples time-ordered.
    time_s: Optional[float] = None
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


class DisambiguationRequest(StrictModel):
    """Which candidate RF material best explains the measurements?

    The RF-sensing disambiguation step from Dai et al. (JSTEAP 2025):
    visually identical materials (e.g. glass types spanning 2.5-23.6 dB
    penetration loss) are separated by re-simulating the measured links with
    each candidate bound to the target prims and ranking the level-aligned
    RMSE - the same error metric as parameter calibration.
    """

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    # Prims whose RF binding is being decided (usually one suggestion's prim).
    prim_ids: list[str] = Field(min_length=1)
    # Candidate library material ids to try (the suggestion + alternatives).
    candidate_material_ids: list[str] = Field(min_length=2)
    measurements: list[MeasurementSample] = Field(min_length=1)


class DisambiguationCandidate(StrictModel):
    material_id: str
    rmse_db: Optional[float] = None
    mean_abs_error_db: Optional[float] = None
    level_offset_db: Optional[float] = None
    n_links: int = 0


class DisambiguationReport(StrictModel):
    prim_ids: list[str]
    candidates: list[DisambiguationCandidate] = Field(default_factory=list)
    # Lowest-RMSE candidate; None when nothing produced comparable links.
    best_material_id: Optional[str] = None
    backend: str
    warnings: list[str] = Field(default_factory=list)


class TrajectoryValidationRequest(StrictModel):
    """Body for POST /calibrate/validate-trajectory.

    Replay a measurement log's RX positions through the trajectory solver and
    score measured vs predicted path gain per point with the same level-offset
    alignment the material calibration uses (measured-vs-predicted along the
    flight/drive log — the measurement round-trip check).
    """

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    tx_id: Optional[str] = None  # None = first tx
    # Inline samples; None = the project's stored (imported) measurements.
    measurements: Optional[list[MeasurementSample]] = Field(
        default=None, min_length=1
    )
    # Solve budget: logs longer than this are subsampled evenly (first and
    # last point kept) before the per-point trajectory solve.
    max_points: int = Field(default=200, ge=1)


class TrajectoryValidationPoint(StrictModel):
    # Index into the time-ordered (and subsampled) measurement sequence, so
    # excluded zero-path points leave visible gaps instead of shifting rows.
    index: int
    time_s: Optional[float] = None
    position: Vec3
    measured_db: float
    predicted_db: float
    aligned_predicted_db: float  # predicted + level_offset_db
    error_db: float  # aligned predicted - measured (same sign as LinkError)


class TrajectoryValidationStats(StrictModel):
    level_offset_db: float
    rmse_db: float
    mean_abs_error_db: float
    n: int  # points compared (zero-path points excluded)


class TrajectoryValidationReport(StrictModel):
    tx_id: str
    points: list[TrajectoryValidationPoint] = Field(default_factory=list)
    stats: TrajectoryValidationStats
    backend: str = ""
    warnings: list[str] = Field(default_factory=list)
