"""Backend-neutral result schemas.

All backends (mock, sionna, future AODT import / remote solvers) normalize
into these models. Interactions reference canonical prim ids so results can
always be mapped back onto the unified scene. MVP persists JSON; field layout
is chosen so the same rows can move to Parquet (paths, radio maps) and Zarr
(CIR tensors) without renaming.
"""

from typing import Literal, Optional

from pydantic import Field, model_validator

from .common import StrictModel, Vec3
from .sensing import SensingCoverageMetric
from .simulation import SimulationConfig

PathType = Literal[
    "los", "reflection", "diffraction", "scattering", "transmission", "mixed", "sensing"
]
InteractionType = Literal[
    "reflection", "diffraction", "scattering", "transmission", "sensing"
]


class PathInteraction(StrictModel):
    type: InteractionType
    # Canonical prim id of the surface hit; None if the backend could not map it.
    # For type "sensing" it carries the ACTOR id of the target instead
    # (rf_material_id None).
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
    # Per-path channel gain (power_dbm minus the TX power): backend-agnostic
    # comparison metric independent of the configured transmit power.
    path_gain_db: Optional[float] = None
    delay_ns: float = Field(ge=0.0)
    # TOTAL passband phase at the carrier, wrapped to (-pi, pi]: the solver's
    # interaction phase PLUS the carrier propagation term -2*pi*f_c*tau
    # (e^{-j*omega*tau} convention). Coherent consumers (CFR/CIR, coherent
    # RSS) use this directly; compute_cfr adds only the BASEBAND offset term
    # e^{-j*2*pi*f_k*tau} on top. For a pure LoS path this equals
    # -2*pi*f_c*d/c mod 2*pi (regression-tested).
    phase_rad: float = 0.0
    # [azimuth_deg, elevation_deg] of departure (at TX) and arrival (at RX).
    # Azimuth is atan2(y, x) about +Z; elevation is up from the XY plane.
    # Arrival points FROM the RX TOWARD the incoming ray (where it came from).
    aod_deg: Optional[list[float]] = None
    aoa_deg: Optional[list[float]] = None
    interactions: list[PathInteraction] = Field(default_factory=list)
    # Per-path Doppler shift [Hz], positive when the path is closing (target
    # approaching). Filled on sensing results; plain paths solves keep
    # reporting Doppler in PathResultSet.metadata["doppler_hz"].
    doppler_hz: Optional[float] = None
    # Actor id of the sensing target this path scatters off (sensing paths only).
    target_id: Optional[str] = None


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


class SensingTargetSummary(StrictModel):
    actor_id: str
    model: Literal["tr38901", "constant"]
    object_type: Optional[str] = None
    model_type: Optional[int] = None
    num_scattering_points: int = Field(default=0, ge=0)
    # World xyz of every scattering point (markers in the viewer).
    scattering_points: list[Vec3] = Field(default_factory=list)
    # World center of the target cuboid (actor base + height/2).
    position: Vec3
    # Cuboid [yaw, pitch, roll] deg (yaw follows the trajectory at t=0 when
    # the velocity does).
    orientation_deg: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    velocity_m_s: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    size_m: Vec3
    # constant: the bound value; tr38901: the spec's mean monostatic sigma_M.
    rcs_dbsm: Optional[float] = None
    path_count: int = Field(default=0, ge=0)


class SensingResultSet(StrictModel):
    result_id: str
    kind: Literal["sensing"] = "sensing"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    # Sensing paths first (path_type "sensing", ids "sensing_0001"...), then the
    # comm paths when the request set include_comm_paths (ids "path_0001"...).
    paths: list[RayPath] = Field(default_factory=list)
    targets: list[SensingTargetSummary] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


DetectionReason = Literal["detected", "no_echo", "below_threshold", "mti_rejected"]
TargetEstimateStatus = Literal["ok", "insufficient_links", "diverged"]


class SensingLinkReport(StrictModel):
    """Detection of one target on one TX->RX link in one scenario frame."""

    tx_id: str
    rx_id: str
    target_id: str
    # Echo this report is built from: the strongest DIRECT echo (interactions
    # == [sensing]); without one, the strongest echo of any kind (multipath).
    path_id: Optional[str] = None
    num_echoes: int = Field(default=0, ge=0)
    multipath: bool = False
    # c * tau of the reported echo: |TX - p| + |p - RX| for a direct echo [m].
    bistatic_range_m: Optional[float] = None
    doppler_hz: Optional[float] = None
    echo_power_dbm: Optional[float] = None
    # echo_power - noise_floor + integration_gain [dB].
    snr_db: Optional[float] = None
    # What fusion consumed: the values above, plus noise when the request set
    # measurement_noise.
    measured_range_m: Optional[float] = None
    measured_doppler_hz: Optional[float] = None
    # Range-sum cell floor(R / (c / B)) and Doppler cell round(f_D * cpi_s).
    range_bin: Optional[int] = None
    doppler_bin: Optional[int] = None
    detected: bool = False
    reason: DetectionReason = "no_echo"


class TargetEstimate(StrictModel):
    """Multistatic position/velocity estimate of one target in one frame."""

    target_id: str
    status: TargetEstimateStatus
    # Detected links of this target (direct or multipath echo).
    n_links_detected: int = Field(default=0, ge=0)
    # Geometrically distinct detected DIRECT echoes that entered the fusion.
    n_links_used: int = Field(default=0, ge=0)
    # "tx_id>rx_id" of each fused link, highest SNR first.
    links_used: list[str] = Field(default_factory=list)
    # Ground truth: the target cuboid center and velocity the solve used.
    position_true: Vec3
    velocity_true: Vec3
    position_est: Optional[Vec3] = None
    velocity_est: Optional[Vec3] = None
    position_error_m: Optional[float] = None
    velocity_error_m_s: Optional[float] = None
    # sqrt(trace((J^T J)^-1)) at the estimate: position error per metre of
    # range error.
    gdop: Optional[float] = None
    rms_residual_m: Optional[float] = None
    iterations: int = Field(default=0, ge=0)


class SensingFrame(StrictModel):
    # Echo paths of this frame (path_type "sensing", doppler_hz, target_id),
    # then the comm paths when include_comm_paths (target_id None).
    echoes: list[RayPath] = Field(default_factory=list)
    links: list[SensingLinkReport] = Field(default_factory=list)
    estimates: list[TargetEstimate] = Field(default_factory=list)


class ISACBeam(StrictModel):
    """One codebook beam of one tx, used full time for comm OR sensing (rho = 1)."""

    angle_deg: float
    # Per UE of this tx: SINR (= SNR, no inter-tx interference) [dB]; None = no path.
    ue_sinr_db: dict[str, Optional[float]] = Field(default_factory=dict)
    # Sum over this tx's UEs of log2(1 + SINR) with this beam [bit/s/Hz].
    sum_rate_bps_hz: float = 0.0
    # Per target: echo SNR with this TX beam and the best RX beam, full CPI.
    target_snr_db: dict[str, Optional[float]] = Field(default_factory=dict)
    target_best_rx_angle_deg: dict[str, Optional[float]] = Field(default_factory=dict)
    # Weakest target (min over targets; None if any target has no echo).
    sensing_snr_db: Optional[float] = None
    # Swerling-1 Pd of sensing_snr_db (pfa when None).
    pd: float
    # sensing_snr_db >= threshold_db.
    detected: bool = False


class ISACPoint(StrictModel):
    # Fraction of slots / pulses spent sensing.
    rho: float
    # Sensing-slot beam; None for rho == 0 (comm only).
    beam_idx: Optional[int] = None
    sum_rate_bps_hz: float
    ue_rates_bps_hz: dict[str, float] = Field(default_factory=dict)
    # Weakest target, rho * cpi_pulses integrated.
    sensing_snr_db: Optional[float] = None
    pd: float
    pareto: bool = False


class ISACParetoSummary(StrictModel):
    # rho = 0: the comm beam full time.
    comm_only_rate_bps_hz: float
    max_pd: float
    pd_target: float
    # Max rate over points with pd >= pd_target (None: no point reaches it).
    rate_at_pd_target_bps_hz: Optional[float] = None
    rho_at_pd_target: Optional[float] = None
    beam_idx_at_pd_target: Optional[int] = None
    # comm_only - rate_at_pd_target.
    rate_loss_at_pd_target_bps_hz: Optional[float] = None
    # Max pd over points with rate >= 0.95 * comm_only.
    pd_at_95pct_rate: float
    num_pareto_points: int = 0


class ISACTxResult(StrictModel):
    tx_id: str
    sensing_rx_id: str
    # 0 ~ monostatic.
    sensing_baseline_m: float
    # UEs this tx serves.
    ue_ids: list[str] = Field(default_factory=list)
    target_ids: list[str] = Field(default_factory=list)
    tx_array: list[int] = Field(min_length=2, max_length=2)  # [rows, cols]
    rx_array: list[int] = Field(min_length=2, max_length=2)
    # Codebook, local azimuth of the panel.
    angles_deg: list[float] = Field(default_factory=list)
    beams: list[ISACBeam] = Field(default_factory=list)
    comm_beam_idx: Optional[int] = None
    sensing_beam_idx: Optional[int] = None
    comm_beam_angle_deg: Optional[float] = None
    sensing_beam_angle_deg: Optional[float] = None
    angle_gap_deg: Optional[float] = None
    # Hand-check anchors: 1x1 both ends.
    ue_single_element_rss_dbm: dict[str, Optional[float]] = Field(default_factory=dict)
    target_single_element_snr_db: dict[str, Optional[float]] = Field(default_factory=dict)
    points: list[ISACPoint] = Field(default_factory=list)
    pareto: ISACParetoSummary
    warnings: list[str] = Field(default_factory=list)


class ISACResultSet(StrictModel):
    result_id: str
    kind: Literal["isac"] = "isac"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    frequency_hz: float
    sharing_mode: Literal["time_sharing", "dual_function"]
    ue_association: Literal["serving", "all"]
    # Sorted, deduped.
    slot_ratios: list[float] = Field(default_factory=list)
    noise_floor_dbm: float
    # UE id -> serving tx id (None = no path to any selected tx).
    ue_serving_tx: dict[str, Optional[str]] = Field(default_factory=dict)
    txs: list[ISACTxResult] = Field(default_factory=list)
    # include_paths: the echo paths, then the comm paths.
    paths: Optional[list[RayPath]] = None
    warnings: list[str] = Field(default_factory=list)
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


class SensingCoverageLink(StrictModel):
    tx_id: str
    rx_id: str
    # Links with the same foci (either order) share a group.
    geometry_group: int
    baseline_m: float
    # Fraction of cells with both legs LOS.
    los_fraction: float
    # Fraction of cells with SNR >= threshold.
    detected_fraction: float


class SensingCoverageSummary(StrictModel):
    num_cells: int
    num_links: int
    num_geometries: int
    # 0..100: >= 1 link with both legs LOS.
    pct_cells_los: float
    # 0..100: >= 1 detected link.
    pct_cells_detected: float
    pct_cells_fusion_feasible: float
    # Over cells with an echo.
    median_best_snr_db: Optional[float] = None


class SensingCoverageResultSet(StrictModel):
    result_id: str
    kind: Literal["sensing_coverage"] = "sensing_coverage"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    frequency_hz: float
    rcs_dbsm: float
    # 0 (none) or 10log10(Ntx) + 10log10(Nrx) (steered).
    array_gain_db: float
    tx_ids: list[str] = Field(default_factory=list)
    sensing_rx_ids: list[str] = Field(default_factory=list)
    grid: RadioMapGrid
    # Row-major [ny][nx] like RadioMapResultSet.values, one array per metric:
    # best_snr_db / pd_best: None where no link has an echo;
    # n_links_detected: 0..num_links; fusion_feasible: 0.0 / 1.0 (never None).
    values: dict[SensingCoverageMetric, list[list[Optional[float]]]]
    links: list[SensingCoverageLink] = Field(default_factory=list)
    summary: SensingCoverageSummary
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class TrajectorySample(StrictModel):
    """Per-waypoint RF metrics along a moving-RX (UE) trajectory."""

    time_s: float
    ue_id: str
    position: Vec3
    rss_dbm: Optional[float] = None
    path_gain_db: Optional[float] = None
    # Narrowband COHERENT received power |sum a_l e^{j phi_l}|^2 — what a
    # CW/narrowband receiver actually measures, fading nulls included
    # (DeepVerse DT31 GT link loss is this quantity; comparing it against the
    # noncoherent rss_dbm mixes definitions and reads as +10..20 dB outliers).
    # Requires the carrier-phase convention on RayPath.phase_rad.
    rss_coherent_dbm: Optional[float] = None
    # Co-channel interference (sum of every non-serving TX's power at the UE);
    # None when the scene has a single TX or nothing interferes here.
    interference_dbm: Optional[float] = None
    # True SINR = S / (I + N); equals the SNR when interference_dbm is None.
    sinr_db: Optional[float] = None
    rms_delay_spread_ns: Optional[float] = None
    path_count: int = 0
    strongest_delay_ns: Optional[float] = None
    # Cell this sample's link budget used. Fixed-serving runs repeat the one
    # requested TX; handover runs show the per-step A3 re-selection.
    serving_tx_id: Optional[str] = None
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


class PlaybackFrame(StrictModel):
    """One frame of a GT-vs-DT playback pack: the SEAM side of the sensor
    manifest's frame at the same ``index``. GT scalars/curves live in the
    manifest (inline gt + beam_profile channel files) — the pack stores only
    what the solver produced, so GT data is never duplicated."""

    index: int
    time_s: Optional[float] = None
    rx_position: Vec3
    # Codebook sweep reprojected onto WORLD azimuth (deg, atan2(y,x) about
    # +Z): axis_deg + sweep angle, where axis_deg is the TX device yaw when
    # the sweep ran with use_device_orientation, else the TX->RX link azimuth
    # (mock backend: its sweep axis is already world-anchored — axis 0).
    beam_azimuth_deg: list[float] = Field(default_factory=list)
    # Absolute beam power (dBm): single_element_dbm + sweep gain at the best
    # RX beam. None marks sweep cells the backend could not evaluate.
    beam_power_dbm: list[Optional[float]] = Field(default_factory=list)
    best_beam_deg: Optional[float] = None  # world azimuth of the peak
    rss_dbm: Optional[float] = None
    rss_coherent_dbm: Optional[float] = None
    tau_rms_ns: Optional[float] = None
    n_paths: int = 0
    # Strongest-K ray polylines for the viewport (capped by the request).
    paths: list[RayPath] = Field(default_factory=list)


class PlaybackResultSet(StrictModel):
    """Frame-synced GT-vs-DT playback pack (issue #3).

    Built by POST /simulate/playback: one solve pair (paths + codebook
    beamforming) per sensor-manifest frame, with the RX moved to the frame
    pose. Frames align 1:1 with manifest frames by ``index``."""

    result_id: str
    kind: Literal["playback"] = "playback"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    tx_id: str
    rx_id: str
    frames: list[PlaybackFrame] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class RadioMapResultSet(StrictModel):
    result_id: str
    kind: Literal["radio_map"] = "radio_map"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    tx_id: str
    metric: Literal["path_gain_db", "rss_dbm", "sinr_db"] = "rss_dbm"
    grid: RadioMapGrid
    # Row-major [ny][nx]; None marks cells that were not computed (progressive
    # refinement leaves holes rather than fabricating values).
    values: list[list[Optional[float]]]
    # Every TX that contributed, in solver order. serving_tx holds per-cell
    # indices into this list (the strongest TX at that cell); only filled for
    # multi-TX scenes so single-TX payloads stay small.
    tx_ids: list[str] = Field(default_factory=list)
    serving_tx: Optional[list[list[Optional[int]]]] = None
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class MeshRadioMapSurface(StrictModel):
    """Per-triangle coverage sampled on one prim's mesh surface.

    Probe receivers sit at each triangle center, offset along the face normal,
    so the viewer can paint facades/roads/floors instead of a horizontal
    plane. Centers/normals are included so rendering never depends on the
    viewer reproducing the backend's triangle ordering."""

    prim_id: str
    mesh_ref: Optional[str] = None
    triangle_count: int = Field(ge=0)
    # Aligned lists, one entry per sampled triangle.
    centers: list[Vec3] = Field(default_factory=list)
    normals: list[Vec3] = Field(default_factory=list)
    values: list[Optional[float]] = Field(default_factory=list)
    # >1 when the mesh had more triangles than the sampling budget and every
    # k-th triangle was sampled instead.
    sample_stride: int = 1


class MeshRadioMapResultSet(StrictModel):
    result_id: str
    kind: Literal["mesh_radio_map"] = "mesh_radio_map"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    tx_id: str
    metric: Literal["path_gain_db", "rss_dbm"] = "rss_dbm"
    surfaces: list[MeshRadioMapSurface] = Field(default_factory=list)
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
    has_sensing: bool = False


class AodtExportRequest(StrictModel):
    """Body for POST /export/aodt (NVIDIA AODT results-schema parquet).

    ``source`` picks what becomes the time axis: "paths" writes ONE snapshot
    (time_idx 0) from a stored path result, "playback" writes one time index
    per frame of a stored playback pack, "sensing" writes ONE snapshot from a
    stored sensing result (all its paths, comm paths included when the solve
    requested them). ``result_id`` selects a specific stored result of that
    kind; None takes the latest.
    """

    config_id: Optional[str] = None
    source: Literal["paths", "playback", "sensing"] = "paths"
    result_id: Optional[str] = None
    # Baseband tone count of the cfrs rows (FFT bin grid across bandwidth_hz).
    fft_size: int = Field(default=64, ge=8, le=4096)
    # Reported in rus/dus; SEAM has no numerology, so it is a declared value.
    subcarrier_spacing_hz: float = Field(default=30e3, gt=0.0)


class AodtExportSummary(StrictModel):
    """POST /export/aodt response: what was written, and how many rows per
    AODT table (telemetry/ran_config are never written - see aodt_export)."""

    export_dir: str
    files: list[str] = Field(default_factory=list)
    tables: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


# Hard ceiling on how many UE positions one channel-dataset export may solve.
# The export is one paths solve per UE, so this bounds a single request's
# wall-clock the way max_frames bounds a playback build.
MAX_CHANNEL_NPZ_UES = 5000


class ChannelNpzExportRequest(StrictModel):
    """Body for POST /export/channel-npz (AODT/HYRAY-layout channel dataset).

    ``ue_source`` picks where the UE grid comes from:

    - ``"explicit"`` (default) - the ``ue_positions`` list, [x, y, z] meters in
      scene coordinates. This is the baseline and always available.
    - ``"devices"`` - every ``kind="rx"`` device in the scene, ordered by id.
      This is what ``POST /import/devices`` persists, so an imported UE list is
      exportable without restating it.
    - ``"trajectory"`` - the per-sample positions of a stored ``trajectory``
      result set (``ue_result_id``, else the latest one).

    ``ue_ids`` / ``time_idx`` are optional passthrough columns (defaults:
    ``arange(U)`` and ``zeros(U)``); when given they must be exactly U long.
    """

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    # Transmitters that become the TRP axis; None = every tx device, in scene
    # order. An explicit list also fixes the TRP ordering of the arrays.
    tx_ids: Optional[list[str]] = None
    ue_source: Literal["explicit", "devices", "trajectory"] = "explicit"
    ue_positions: Optional[list[Vec3]] = Field(
        default=None, max_length=MAX_CHANNEL_NPZ_UES
    )
    # ue_source="trajectory": which stored trajectory result to read; None =
    # the latest.
    ue_result_id: Optional[str] = None
    # Path axis length P: links are sorted strongest-first and zero-padded to
    # it (500 is the reference layout's width).
    max_paths: int = Field(default=500, ge=1, le=2000)
    # dB offset folded into every amplitude (|a| scales by 10**(x/20)). 0 keeps
    # SEAM's own physical channel gain; -5.06 reproduces the AODT convention.
    normalization_db: float = 0.0
    # Written verbatim into the npz's scalar ``batch`` key.
    batch: int = Field(default=0, ge=0)
    ue_ids: Optional[list[int]] = Field(default=None, max_length=MAX_CHANNEL_NPZ_UES)
    time_idx: Optional[list[int]] = Field(default=None, max_length=MAX_CHANNEL_NPZ_UES)
    # Append the sensing paths (target_id set) of a stored sensing result to each
    # UE x TX link before strongest-first sorting. A sensing path joins link
    # (u, t) when its tx_id is TX t and its last vertex lies within 1 mm of UE u.
    include_sensing: bool = False
    # Which sensing result; None = the latest. Ignored unless include_sensing.
    sensing_result_id: Optional[str] = None

    @model_validator(mode="after")
    def _explicit_needs_positions(self) -> "ChannelNpzExportRequest":
        if self.ue_source == "explicit" and not self.ue_positions:
            raise ValueError(
                'ue_source="explicit" requires a non-empty ue_positions list'
            )
        return self


class ChannelNpzExportSummary(StrictModel):
    """POST /export/channel-npz response: what was written and how big it is."""

    export_dir: str
    files: list[str] = Field(default_factory=list)
    # Array name -> shape, so a client can show the layout without loading the
    # npz (e.g. sorted_dataset_ampl: [U, T, P]).
    shapes: dict[str, list[int]] = Field(default_factory=dict)
    num_tx: int = 0
    num_ue: int = 0
    link_count: int = 0
    # Paths actually written across every link (after strongest-first capping).
    path_count: int = 0
    # Sensing paths matched into links (include_sensing), before capping.
    sensing_path_count: int = 0
    max_paths: int = 0
    size_bytes: int = 0
    elapsed_s: float = 0.0
    warnings: list[str] = Field(default_factory=list)
