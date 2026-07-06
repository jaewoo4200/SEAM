"""Backend-neutral result schemas.

All backends (mock, sionna, future AODT import / remote solvers) normalize
into these models. Interactions reference canonical prim ids so results can
always be mapped back onto the unified scene. MVP persists JSON; field layout
is chosen so the same rows can move to Parquet (paths, radio maps) and Zarr
(CIR tensors) without renaming.
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel, Vec3

PathType = Literal["los", "reflection", "diffraction", "scattering", "transmission", "mixed"]
InteractionType = Literal["reflection", "diffraction", "scattering", "transmission"]


class PathInteraction(StrictModel):
    type: InteractionType
    # Canonical prim id of the surface hit; None if the backend could not map it.
    prim_id: Optional[str] = None
    rf_material_id: Optional[str] = None
    point: Vec3


class RayPath(StrictModel):
    path_id: str
    tx_id: str
    rx_id: str
    path_type: PathType
    # Polyline from tx to rx, including interaction points.
    vertices: list[Vec3] = Field(min_length=2)
    power_dbm: float
    delay_ns: float = Field(ge=0.0)
    phase_rad: float = 0.0
    # Azimuth/zenith of departure and arrival in degrees (future AoA/AoD plots).
    aod_deg: Optional[list[float]] = None
    aoa_deg: Optional[list[float]] = None
    interactions: list[PathInteraction] = Field(default_factory=list)


class PathResultSet(StrictModel):
    result_id: str
    kind: Literal["paths"] = "paths"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    paths: list[RayPath] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    # Free-form backend metadata (frequency, sample count, timing, ...).
    metadata: dict = Field(default_factory=dict)


class BeamformingResult(StrictModel):
    """MIMO beamforming gain summary for one TX->RX link.

    Gains are relative to a single antenna element (dB):
    - tx_mrt_gain_db: transmit maximum-ratio combining toward the RX;
    - svd_gain_db: both-ends SVD precoding (full-CSI upper bound).
    """

    backend: str
    simulation_config_id: str
    tx_id: str
    rx_id: str
    frequency_hz: float
    tx_array: list[int] = Field(min_length=2, max_length=2)  # [rows, cols]
    rx_array: list[int] = Field(min_length=2, max_length=2)
    num_paths: int = 0
    single_element_dbm: Optional[float] = None
    tx_mrt_gain_db: Optional[float] = None
    svd_gain_db: Optional[float] = None
    # Codebook beam-sweep results (mode="codebook_sweep"): azimuth DFT beams
    # on both ends; sweep_gain_db is [rx_beam][tx_beam] gain over a single
    # element in dB; best_* give the selected beam pair.
    mode: str = "svd"
    codebook_gain_db: Optional[float] = None
    best_tx_angle_deg: Optional[float] = None
    best_rx_angle_deg: Optional[float] = None
    sweep_angles_deg: list[float] = Field(default_factory=list)
    sweep_gain_db: Optional[list[list[Optional[float]]]] = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class RadioMapGrid(StrictModel):
    # World position of cell (0, 0)'s corner.
    origin: Vec3
    cell_size_m: float = Field(gt=0.0)
    nx: int = Field(ge=1)
    ny: int = Field(ge=1)
    height_m: float = 1.5


class TrajectorySample(StrictModel):
    """Per-waypoint RF metrics along a moving-RX (UE) trajectory."""

    time_s: float
    ue_id: str
    position: Vec3
    rss_dbm: Optional[float] = None
    path_gain_db: Optional[float] = None
    # Co-channel interference (sum of every non-serving TX's power at the UE);
    # None when the scene has a single TX or nothing interferes here.
    interference_dbm: Optional[float] = None
    # True SINR = S / (I + N); equals the SNR when interference_dbm is None.
    sinr_db: Optional[float] = None
    rms_delay_spread_ns: Optional[float] = None
    path_count: int = 0
    strongest_delay_ns: Optional[float] = None
    # Full ray paths at this waypoint (heavy; filled when the request sets
    # include_paths so playback can redraw rays live as the UE moves).
    paths: Optional[list["RayPath"]] = None


class TrajectoryResultSet(StrictModel):
    result_id: str
    kind: Literal["trajectory"] = "trajectory"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    ue_id: str
    samples: list[TrajectorySample] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class RadioMapResultSet(StrictModel):
    result_id: str
    kind: Literal["radio_map"] = "radio_map"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    tx_id: str
    metric: Literal["path_gain_db", "rss_dbm"] = "rss_dbm"
    grid: RadioMapGrid
    # Row-major [ny][nx]; None marks cells that were not computed (progressive
    # refinement leaves holes rather than fabricating values).
    values: list[list[Optional[float]]]
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class RFDataExportSummary(StrictModel):
    """POST /export/rfdata response - enforced contract for the FE's
    RFDataExportSummary type (audit polish: was a bare dict, silent drift)."""

    export_dir: str
    files: list[str] = Field(default_factory=list)
    has_paths: bool = False
    has_radio_map: bool = False
    has_trajectory: bool = False
