"""Simulation configuration stored in the canonical scene."""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel


class RadioMapGridConfig(StrictModel):
    cell_size_m: float = Field(default=2.0, gt=0.0)
    # Height above ground for the planar measurement grid.
    height_m: float = 1.5
    # Default matches Sionna RT's preview/render default (path gain in dB).
    # sinr_db needs >=1 TX; with a single TX it degenerates to SNR.
    metric: Literal["path_gain_db", "rss_dbm", "sinr_db"] = "path_gain_db"
    # Optional explicit extent override ([x, y] center / size in meters).
    # None = auto-fit to the scene geometry. Lets a caller refine a selected
    # region at a finer cell size instead of re-solving the whole map.
    center_xy: Optional[list[float]] = Field(default=None, min_length=2, max_length=2)
    size_xy: Optional[list[float]] = Field(default=None, min_length=2, max_length=2)


class SimulationConfig(StrictModel):
    """Full Sionna RT solver parameter surface (mirrors PathSolver/
    RadioMapSolver options so every knob is user-controllable in the UI)."""

    id: str = "default"
    name: str = "Default"
    # "auto" resolves to the sionna backend when installed, else mock.
    backend: Literal["auto", "mock", "sionna"] = "auto"
    # Compute-engine id (see GET /api/engines and docs/sionna_versions.md).
    # None/"builtin" = in-process sionna-rt; other ids run the paths solve in
    # that engine's own venv via a subprocess worker. Currently applies to
    # paths solves; other analyses always use the builtin engine.
    engine: Optional[str] = None
    # Default 28 GHz to match the FTC/lab-room mmWave ISAC digital twin.
    frequency_hz: float = Field(default=28e9, gt=0.0)
    max_depth: int = Field(default=3, ge=0, le=12)
    # None means all devices of that kind in the scene.
    tx_ids: Optional[list[str]] = None
    rx_ids: Optional[list[str]] = None
    # Interaction mechanisms (PathSolver/RadioMapSolver flags).
    los: bool = True
    reflection: bool = True  # specular_reflection
    scattering: bool = False  # diffuse_reflection
    refraction: bool = False  # transmission through slabs
    diffraction: bool = False
    edge_diffraction: bool = False
    # sionna-rt >= 1.2: also generate diffracted paths inside the lit region
    # (not only the shadow zone). Ignored by engines that predate the flag.
    diffraction_lit_region: bool = False
    # Solver mechanics.
    synthetic_array: bool = True
    seed: int = Field(default=42, ge=0)
    # Ray-launching sample budget (consumer-level default, refinable later).
    num_samples: int = Field(default=1_000_000, ge=1)
    # Link-budget context for SNR/SINR readouts (no interference model yet, so
    # SINR == SNR = RSS - (-174 dBm/Hz + 10log10(B) + NF)).
    bandwidth_hz: float = Field(default=100e6, gt=0.0)
    noise_figure_db: float = Field(default=7.0, ge=0.0)
    radio_map: RadioMapGridConfig = Field(default_factory=RadioMapGridConfig)


class SimulateRequest(StrictModel):
    """Body for POST /simulate/paths and /simulate/radio-map."""

    # Use a config stored in the scene by id...
    config_id: Optional[str] = None
    # ...or supply an inline config (wins over config_id).
    config: Optional[SimulationConfig] = None


class MeshRadioMapRequest(StrictModel):
    """Body for POST /simulate/mesh-radio-map: per-triangle coverage on the
    selected prims' surfaces (facades, roads, floors) instead of a horizontal
    plane. Probe receivers are parked at triangle centers, offset along the
    face normal, and solved in chunks with the active backend."""

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    prim_ids: list[str] = Field(min_length=1)
    # Serving TX; None = first tx in the scene.
    tx_id: Optional[str] = None
    metric: Literal["path_gain_db", "rss_dbm"] = "rss_dbm"
    # Sampling budget across ALL requested surfaces; meshes above it are
    # sampled every k-th triangle (stride recorded per surface).
    max_triangles: int = Field(default=2000, ge=1, le=20000)
    # Probe offset along the face normal so receivers sit just off the surface.
    offset_m: float = Field(default=0.05, gt=0.0)


class BeamformingRequest(StrictModel):
    """Body for POST /simulate/beamforming.

    Modes (explicitly defined):
    - codebook_sweep: hardware-style beam training - a DFT codebook of azimuth
      beams is scanned on BOTH ends (default -60..60 deg, 5 deg step =>
      25x25 = 625 beam pairs, the ICC'26 paper setup) and the strongest pair
      is selected. This is what real mmWave systems do.
    - tx_mrt: transmit maximum-ratio combining toward the first RX antenna
      (full CSI at TX only).
    - svd: both-ends SVD precoding (full-CSI upper bound, not implementable
      by beam-sweep hardware).
    """

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    tx_id: Optional[str] = None  # None = first tx
    rx_id: Optional[str] = None  # None = first rx
    tx_rows: int = Field(default=4, ge=1, le=16)
    tx_cols: int = Field(default=4, ge=1, le=16)
    rx_rows: int = Field(default=4, ge=1, le=16)
    rx_cols: int = Field(default=4, ge=1, le=16)
    mode: Literal["codebook_sweep", "tx_mrt", "svd"] = "codebook_sweep"
    sweep_start_deg: float = -60.0
    sweep_stop_deg: float = 60.0
    sweep_step_deg: float = Field(default=5.0, gt=0.0)


class UERoute(StrictModel):
    """One routed UE for a multi-UE trajectory: an rx device id and the
    waypoint polyline it walks. The polyline is resampled to the request's
    num_points steps by arc length at solve time."""

    ue_id: str
    waypoints: list[list[float]] = Field(min_length=2)


class TrajectorySimulateRequest(StrictModel):
    """Body for POST /simulate/trajectory: move one RX along waypoints.

    Multi-UE: when ``routes`` is set, the legacy single-UE fields
    (ue_id/waypoints/start_m/end_m) are ignored; every route is resampled to
    ``num_points`` steps along its polyline by arc length, all routed UEs move
    together per step, and one solve per step yields the per-UE metrics.
    ``dt_s`` and ``follow_terrain`` apply to all routes.
    """

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    # RX device to move; None = the first rx in the scene.
    ue_id: Optional[str] = None
    # Serving TX for the per-waypoint link budget; None = the first tx. Other
    # TXs count as co-channel interference in the per-sample SINR.
    serving_tx_id: Optional[str] = None
    # Explicit waypoints (meters, Z-up)...
    waypoints: Optional[list[list[float]]] = None
    # ...or a straight line: start -> end sampled at num_points.
    start_m: Optional[list[float]] = None
    end_m: Optional[list[float]] = None
    # Multi-UE routes. When set, the legacy single-UE fields above
    # (ue_id/waypoints/start_m/end_m) are ignored; every route is resampled to
    # num_points steps along its polyline by arc length, all routed UEs move
    # together per step, and dt_s/follow_terrain apply to all.
    routes: Optional[list[UERoute]] = None
    # Multi-UE only: also solve every un-routed RX at its fixed position each
    # step — fixed and moving UEs share one link table/interference context.
    include_static_rx: bool = False
    num_points: int = Field(default=8, ge=2, le=200)
    dt_s: float = Field(default=0.1, gt=0.0)
    # Include the full ray paths per waypoint so playback redraws rays live.
    include_paths: bool = False
    # Snap each waypoint's z to the scene surface underneath it plus
    # follow_height_m (raycast down onto the visual mesh). For outdoor
    # sloped terrain; indoor scenes should leave this off.
    follow_terrain: bool = False
    follow_height_m: float = Field(default=1.5, gt=0.0)
    # Interior footprint holes get their surface z interpolated between the
    # nearest draped neighbors (False keeps the raw chord z across a hole).
    drape_fill_gaps: bool = True
