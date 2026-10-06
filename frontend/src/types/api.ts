/**
 * TypeScript mirror of the backend Pydantic schemas (backend/app/schemas/).
 *
 * Convention decision (HANDOFF.md section 17): JSON keys stay snake_case
 * end-to-end. These types intentionally match the wire format exactly so
 * there is no conversion boundary to drift.
 *
 * This file is the frontend's contract with the backend - edit it only
 * together with the corresponding Pydantic model.
 */

export type Vec3 = [number, number, number];
export type Vec4 = [number, number, number, number];

export type AssignmentStatus =
  | "unassigned"
  | "rule_suggested"
  | "rule_assigned"
  | "ai_suggested"
  | "user_confirmed"
  | "measurement_calibrated"
  | "rejected";

// ----------------------------------------------------------------- scene

export interface Transform {
  translation: Vec3;
  rotation_quat_xyzw: Vec4;
  scale: Vec3;
}

export interface CoordinateSystem {
  type: "local_enu";
  origin_lat_lon_alt: Vec3 | null;
  units: "meters";
}

export interface SceneAssets {
  visual_scene_uri: string | null;
  visual_overlay_uri: string | null;
  tileset_uri: string | null;
}

export interface MeshRef {
  asset_uri: string;
  mesh_name: string;
  primitive_index: number;
  face_group: string | null;
}

export interface VisualBinding {
  material_id: string | null;
  material_name: string | null;
  base_color_texture: string | null;
  base_color_rgba: Vec4 | null;
}

export interface RFBinding {
  material_id: string | null;
  thickness_m: number | null;
  scattering_coefficient: number | null;
  xpd_coefficient: number | null;
  assignment_status: AssignmentStatus;
  assignment_sources: string[];
  confidence: number | null;
}

export interface Prim {
  id: string;
  name: string;
  type: "mesh_primitive" | "group";
  parent_id: string | null;
  semantic_tags: string[];
  mesh_ref: MeshRef | null;
  transform: Transform;
  visual: VisualBinding | null;
  rf: RFBinding;
}

export interface Antenna {
  pattern: string;
  polarization: "V" | "H" | "VH" | "cross";
  num_rows: number;
  num_cols: number;
  // Element spacing in wavelengths (0.5 = half-wavelength); optional for
  // scenes written before the field existed (backend defaults to 0.5).
  vertical_spacing?: number;
  horizontal_spacing?: number;
}

export interface Device {
  id: string;
  name: string;
  kind: "tx" | "rx";
  position: Vec3;
  orientation_deg: Vec3;
  // [vx,vy,vz] m/s (Z-up world). Set -> solved paths carry per-path Doppler.
  velocity_m_s?: Vec3 | null;
  power_dbm: number;
  antenna: Antenna;
  color: string;
}

export interface RadioMapGridConfig {
  cell_size_m: number;
  height_m: number;
  metric: "path_gain_db" | "rss_dbm" | "sinr_db";
  // Region refinement: recenter/resize the sampled patch (world XY meters).
  // null = derive from the scene bounds like before.
  center_xy?: [number, number] | null;
  size_xy?: [number, number] | null;
}

export interface SimulationConfig {
  id: string;
  name: string;
  backend: "auto" | "mock" | "sionna";
  // Compute-engine id from GET /api/engines; null/"builtin" = in-process
  // sionna-rt. Alternate engines currently apply to paths solves.
  engine?: string | null;
  frequency_hz: number;
  max_depth: number;
  tx_ids: string[] | null;
  rx_ids: string[] | null;
  los: boolean;
  reflection: boolean;
  scattering: boolean;
  refraction: boolean;
  diffraction: boolean;
  edge_diffraction: boolean;
  // sionna-rt >= 1.2: diffracted paths in the lit region too (not only shadow).
  diffraction_lit_region: boolean;
  synthetic_array: boolean;
  seed: number;
  num_samples: number;
  /** PathSolver max_num_paths_per_src: hard cap on paths kept per source.
   *  Radio-map solves ignore it (RadioMapSolver has no such kwarg). */
  max_num_paths_per_src: number;
  bandwidth_hz: number;
  noise_figure_db: number;
  /** ITU-R P.676 atmospheric gas attenuation as a per-path post-process
   *  (paths/channel/trajectory/beamforming; radio maps unaffected). */
  atmospheric_absorption: boolean;
  /** Explicit dB/km override; null = built-in coarse P.676 curve (warns). */
  absorption_db_per_km: number | null;
  radio_map: RadioMapGridConfig;
}

export interface ResultSetRef {
  result_id: string;
  kind:
    | "paths"
    | "radio_map"
    | "mesh_radio_map"
    | "trajectory"
    | "scenario"
    | "channel"
    | "playback"
    | "sensing"
    | "isac"
    | "sensing_coverage";
  backend: string;
  simulation_config_id: string;
  uri: string;
  created_at: string | null;
  /** User-facing run name; labeled runs are spared by results pruning. */
  label?: string | null;
  /** On-disk size of the persisted result file, stamped at save time. */
  size_bytes?: number | null;
}

// Compute engine registry entry (GET /api/engines).
export interface EngineInfo {
  id: string;
  label: string;
  kind: "builtin" | "subprocess";
  adapter: "builtin" | "sionna_rt";
  python: string | null;
  available: boolean;
  version: string | null;
  detail: string;
}

export interface EngineListResponse {
  engines: EngineInfo[];
}

export type ActorKind = "car" | "human" | "custom" | "uav";

export interface ActorShape {
  type: "box" | "mesh";
  size_m: Vec3;
  mesh_ref: MeshRef | null;
}

export interface ActorTrajectory {
  waypoints: Vec3[];
  /** Seconds per waypoint step; ignored while speed_m_s is set. */
  dt_s: number;
  /** Constant travel speed [m/s]: each segment takes length / speed. */
  speed_m_s: number | null;
  loop: boolean;
  mode: "once" | "loop" | "pingpong" | null;
}

/** TR 38.901 §7.9 sensing-target object types (the only strings sionna-rt 2.2
 *  accepts; "vehicle"/"agv" alone are rejected). */
export type TR38901ObjectType =
  | "uav-small-size"
  | "uav-large-size"
  | "human"
  | "vehicle-single-sp"
  | "vehicle-multi-sp"
  | "agv-single-sp"
  | "agv-multi-sp";

/** Binds an actor as a radar sensing target (POST /simulate/sensing). Fields
 *  of the other model must stay null/false — the backend rejects them. */
export interface SensingTargetSpec {
  model: "tr38901" | "constant";
  /** tr38901: null = derived from the actor kind (custom: required). */
  object_type: TR38901ObjectType | null;
  /** tr38901: null = the highest model type the object type defines. */
  model_type: 1 | 2 | null;
  /** constant: null = 0 dBsm (1 m²). */
  rcs_dbsm: number | null;
  /** constant: null = the scattering point does not depolarize. */
  xpr_db: number | null;
  /** tr38901: draw σ_S, XPR and initial phases per direction pair. */
  random_components: boolean;
  /** Target cuboid (length, width, height) m; null = actor.shape.size_m. */
  size_m: Vec3 | null;
  /** World-frame m/s; null = the trajectory tangent at t=0 (static: 0). */
  velocity_m_s: Vec3 | null;
  enabled: boolean;
}

export interface Actor {
  id: string;
  name: string;
  kind: ActorKind;
  shape: ActorShape;
  rf_material_id: string | null;
  position: Vec3;
  orientation_deg: Vec3;
  trajectory: ActorTrajectory | null;
  attached_device_ids: string[];
  color: string | null;
  /** Radar sensing-target binding; null/absent = not a target. */
  sensing?: SensingTargetSpec | null;
}

export type Environment = "auto" | "indoor" | "outdoor";

export interface Scene {
  schema_version: string;
  scene_id: string;
  name: string;
  environment: Environment;
  coordinate_system: CoordinateSystem;
  assets: SceneAssets;
  prims: Prim[];
  devices: Device[];
  actors: Actor[];
  simulation_configs: SimulationConfig[];
  result_sets: ResultSetRef[];
  /** Optimistic-concurrency counter bumped on every save. Sent back on PUT so
   *  a stale write (another tab saved meanwhile) gets a 409 instead of
   *  clobbering. null on fresh/pre-revision scenes = "skip the check". */
  revision?: number | null;
}

/** World-space AABB of the visual scene (GET /scene/bounds, Z-up meters).
 *  Seeds sampling regions, trajectory endpoints, and placement defaults. */
export interface SceneBounds {
  min: Vec3;
  max: Vec3;
}

// ------------------------------------------------------------- materials

export interface RFMaterial {
  id: string;
  display_name: string;
  category: string;
  model: "itu_frequency_dependent" | "constant";
  itu_name: string | null;
  relative_permittivity: number | null;
  conductivity_s_per_m: number | null;
  thickness_m: number | null;
  scattering_coefficient: number;
  xpd_coefficient: number;
  transmissive: boolean;
  preview_color: string;
  notes: string;
  builtin: boolean;
}

export interface RFMaterialLibrary {
  materials: RFMaterial[];
}

export interface RFOverrides {
  thickness_m?: number | null;
  scattering_coefficient?: number | null;
  xpd_coefficient?: number | null;
}

export interface AssignRequest {
  prim_ids: string[];
  rf_material_id: string;
  assignment_status?: AssignmentStatus;
  sources?: string[];
  confidence?: number | null;
  overrides?: RFOverrides | null;
}

export interface BatchAssignRequest {
  assignments: AssignRequest[];
}

// POST /projects/{pid}/rf/unassign — clear the RF binding on these prims.
export interface UnassignRequest {
  prim_ids: string[];
}

export interface AssignResponse {
  updated_prim_ids: string[];
  skipped_prim_ids: string[];
  warnings: string[];
}

// ------------------------------------------------------------ validation

export type Severity = "error" | "warning" | "info";

export interface ValidationIssue {
  severity: Severity;
  code: string;
  message: string;
  prim_id: string | null;
  device_id: string | null;
  // Machine-actionable remediation hints (rendered by the validation panel,
  // owned by a sibling; typed here so the shape is shared).
  suggested_actions: string[];
}

export interface ValidationReport {
  ok: boolean;
  issues: ValidationIssue[];
  error_count: number;
  warning_count: number;
  info_count: number;
}

// --------------------------------------------------------------- compile

export interface MaterialGroup {
  rf_material_id: string;
  prim_ids: string[];
  mesh_file: string | null;
  face_count: number | null;
}

export interface CompileResult {
  ok: boolean;
  backend_format: string;
  scene_xml: string | null;
  manifest: string | null;
  mesh_dir: string | null;
  material_groups: MaterialGroup[];
  generated_files: string[];
  skipped_prim_ids: string[];
  validation: ValidationReport | null;
  warnings: string[];
  errors: string[];
}

// --------------------------------------------------------------- results

export type PathType =
  | "los"
  | "reflection"
  | "diffraction"
  | "scattering"
  | "transmission"
  | "mixed"
  | "sensing";

export interface PathInteraction {
  type: "reflection" | "diffraction" | "scattering" | "transmission" | "sensing";
  /** For type "sensing": the ACTOR id of the target (rf_material_id null). */
  prim_id: string | null;
  rf_material_id: string | null;
  point: Vec3;
}

export interface RayPath {
  path_id: string;
  tx_id: string;
  rx_id: string;
  path_type: PathType;
  vertices: Vec3[];
  power_dbm: number;
  // Free-space-independent channel gain of this path (dB). Null when the
  // backend cannot report it separately from power.
  path_gain_db?: number | null;
  delay_ns: number;
  phase_rad: number;
  // [azimuth_deg, elevation_deg] | null. AoD points FROM the TX toward the
  // departing ray; AoA points FROM the RX toward the incoming ray. Elevation
  // is measured up from the XY plane.
  aod_deg: [number, number] | null;
  aoa_deg: [number, number] | null;
  interactions: PathInteraction[];
  /** Per-path Doppler [Hz], positive when closing (target approaching).
   *  Filled on sensing results; paths solves keep metadata.doppler_hz. */
  doppler_hz?: number | null;
  /** Actor id of the sensing target this path scatters off (sensing only). */
  target_id?: string | null;
}

export interface PathResultSet {
  result_id: string;
  kind: "paths";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  paths: RayPath[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

export interface RadioMapGrid {
  origin: Vec3;
  cell_size_m: number;
  nx: number;
  ny: number;
  height_m: number;
}

export interface RadioMapResultSet {
  result_id: string;
  kind: "radio_map";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  tx_id: string;
  // Every TX contributing to the map (single-element for the legacy 1-TX case).
  tx_ids: string[];
  metric: "path_gain_db" | "rss_dbm" | "sinr_db";
  grid: RadioMapGrid;
  values: (number | null)[][];
  // Per-cell serving-TX association: index into tx_ids (null = no serving TX).
  // Only populated for multi-TX maps; null otherwise.
  serving_tx?: (number | null)[][] | null;
  warnings: string[];
  metadata: Record<string, unknown>;
}

export interface SimulateRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
}

// ------------------------------------------------------- mesh radio map
// Radio-map samples projected ONTO the triangles of specific surfaces
// (a "material response" heatmap draped on the geometry), rather than on a
// flat sampling plane.

export interface MeshRadioMapRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  // The prims whose surfaces to sample (at least one).
  prim_ids: string[];
  tx_id?: string | null;
  metric?: "path_gain_db" | "rss_dbm";
  // Cap the sampled triangles per surface (uniform stride to stay under it).
  max_triangles?: number;
  // Lift sample points off the surface along the normal (meters) so the source
  // ray does not start embedded in the wall.
  offset_m?: number;
}

export interface MeshRadioMapSurface {
  prim_id: string;
  mesh_ref?: string | null;
  triangle_count: number;
  // One entry per sampled triangle: face centroid, outward normal, metric value.
  centers: Vec3[];
  normals: Vec3[];
  values: (number | null)[];
  // Stride used to subsample triangles (1 = every triangle).
  sample_stride: number;
}

export interface MeshRadioMapResultSet {
  result_id: string;
  kind: "mesh_radio_map";
  backend: string;
  simulation_config_id: string;
  created_at?: string | null;
  tx_id: string;
  metric: "path_gain_db" | "rss_dbm";
  surfaces: MeshRadioMapSurface[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

// GET /api/backends entry: which solver backends exist, whether they can run,
// and a free-form capability bag (has_radio_map, mesh_radio_map, sinr, ...).
export interface BackendCapabilities {
  name: string;
  available: boolean;
  detail: string;
  capabilities: Record<string, unknown>;
}

export interface TrajectorySample {
  time_s: number;
  ue_id: string;
  position: Vec3;
  rss_dbm: number | null;
  path_gain_db: number | null;
  /** Narrowband coherent received power (fading nulls included) — what a CW
   *  receiver measures, vs the wideband-average rss_dbm. */
  rss_coherent_dbm?: number | null;
  sinr_db: number | null;
  interference_dbm?: number | null;
  rms_delay_spread_ns: number | null;
  path_count: number;
  strongest_delay_ns: number | null;
  paths: RayPath[] | null;
  /** Serving cell used for this sample's link budget. In handover mode it is
   *  the per-step A3 re-selection; otherwise the one requested serving TX. */
  serving_tx_id?: string | null;
}

export interface TrajectoryResultSet {
  result_id: string;
  kind: "trajectory";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  ue_id: string;
  samples: TrajectorySample[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

/** POST /projects/import-osm — build a project from OpenStreetMap building
 *  footprints in a rectangle around a coordinate (needs internet). */
export interface OsmImportRequest {
  project_id?: string | null;
  name: string;
  lat: number;
  lon: number;
  width_m?: number;
  height_m?: number;
  default_building_material?: string;
  ground_material?: string;
  default_building_height_m?: number;
}

export interface OsmImportResponse {
  project_id: string;
  num_buildings: number;
  num_skipped: number;
  warnings: string[];
}

/** One routed UE for a multi-UE trajectory: resampled to num_points steps
 *  along its polyline by arc length at solve time. */
export interface UERoute {
  ue_id: string;
  waypoints: number[][];
  /** Optional per-waypoint [yaw, pitch, roll] degrees (parallel to waypoints;
   *  entries may be null). Aims the moving UE's antenna per step. */
  orientations_deg?: (number[] | null)[] | null;
}

export interface TrajectorySimulateRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  ue_id?: string | null;
  serving_tx_id?: string | null;
  waypoints?: number[][] | null;
  start_m?: number[] | null;
  end_m?: number[] | null;
  // Multi-UE: when set, the legacy single-UE fields above are ignored; all
  // routed UEs move together per step (one solve per step, per-UE metrics).
  routes?: UERoute[] | null;
  num_points?: number;
  dt_s?: number;
  include_paths?: boolean;
  follow_terrain?: boolean;
  follow_height_m?: number;
  /** Multi-UE only: also solve every un-routed RX at its fixed position each
   *  step (fixed + moving UEs share one per-frame link table). */
  include_static_rx?: boolean;
  /** When set, the serving TX is re-selected per step (3GPP A3 with
   *  hysteresis + time-to-trigger); serving_tx_id above only picks the
   *  INITIAL cell. Metadata gains a per-UE handover event log + per-TX RSS. */
  handover?: HandoverConfig | null;
}

/** Per-step serving-TX re-selection along a trajectory (3GPP A3-style). */
export interface HandoverConfig {
  hysteresis_db?: number;
  time_to_trigger_steps?: number;
}

/** One A3 handover event on a trajectory (from the result metadata). */
export interface HandoverEvent {
  step: number;
  time_s: number;
  from_tx: string;
  to_tx: string;
}

/** Per-UE handover summary in TrajectoryResultSet.metadata.handover[ue_id]. */
export interface HandoverSummary {
  events: HandoverEvent[];
  count: number;
  ping_pongs: number;
}

export interface RFDataExportSummary {
  export_dir: string;
  files: string[];
  has_paths: boolean;
  has_radio_map: boolean;
  has_trajectory: boolean;
  /** sensing.json written (a stored sensing result with paths existed). */
  has_sensing?: boolean;
}

/** Where POST /export/channel-npz takes its UE grid from. */
export type ChannelNpzUeSource = "explicit" | "devices" | "trajectory";

/** Body for POST /export/channel-npz (AODT/HYRAY-layout channel dataset). */
export interface ChannelNpzExportRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  /** TRP axis; null = every tx device in scene order. */
  tx_ids?: string[] | null;
  ue_source?: ChannelNpzUeSource;
  /** Required when ue_source is "explicit". */
  ue_positions?: [number, number, number][] | null;
  /** ue_source "trajectory": which stored result; null = latest. */
  ue_result_id?: string | null;
  max_paths?: number;
  /** dB folded into every amplitude; -5.06 reproduces the AODT convention. */
  normalization_db?: number;
  batch?: number;
  ue_ids?: number[] | null;
  time_idx?: number[] | null;
  /** Append a stored sensing result's echo paths to the UE x TX link whose
   *  UE sits at the echo's RX (1 mm match). */
  include_sensing?: boolean;
  /** Which sensing result; null = latest. Ignored unless include_sensing. */
  sensing_result_id?: string | null;
}

export interface ChannelNpzExportSummary {
  export_dir: string;
  files: string[];
  /** Array name -> shape, e.g. sorted_dataset_ampl: [U, T, P]. */
  shapes: Record<string, number[]>;
  num_tx: number;
  num_ue: number;
  link_count: number;
  path_count: number;
  max_paths: number;
  size_bytes: number;
  elapsed_s: number;
  warnings: string[];
  /** Sensing echo paths matched into links (include_sensing). */
  sensing_path_count?: number;
}

// ------------------------------------------------------- scenario / live

export interface ActorState {
  id: string;
  position: Vec3;
  orientation_deg: Vec3;
}

export interface DeviceState {
  id: string;
  position: Vec3;
}

export interface LinkMetrics {
  tx_id: string;
  rx_id: string;
  rss_dbm: number | null;
  path_gain_db: number | null;
  interference_dbm?: number | null;
  sinr_db: number | null;
  rms_delay_spread_ns: number | null;
  path_count: number;
}

export interface ScenarioFrame {
  time_s: number;
  actor_states: ActorState[];
  device_states: DeviceState[];
  links: LinkMetrics[];
  paths: RayPath[] | null;
  /** Per-frame sensing (request.sensing.enabled); null/absent otherwise. */
  sensing?: SensingFrame | null;
}

export interface ScenarioResultSet {
  result_id: string;
  kind: "scenario";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  frames: ScenarioFrame[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

export interface ScenarioSimulateRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  num_frames?: number;
  dt_s?: number;
  include_paths?: boolean;
  /** null / absent / enabled=false: no sensing (frames as in v0.1.10). */
  sensing?: SensingTrackOptions | null;
}

export interface LiveStateUpdate {
  timestamp?: string | null;
  devices?: DeviceState[];
  actors?: ActorState[];
  resimulate?: boolean;
  persist?: boolean;
}

export interface LiveStateResponse {
  applied_devices: string[];
  applied_actors: string[];
  unknown_ids: string[];
  links: LinkMetrics[];
  warnings: string[];
}

// --------------------------------------------------------------- channel

export type PathLossModelName =
  | "fspl"
  | "tr38901_uma_los"
  | "tr38901_uma_nlos"
  | "tr38901_umi_los"
  | "tr38901_umi_nlos"
  | "tr38901_inh_los"
  | "tr38901_inh_nlos"
  | "ci_n2"
  | "ci_n3";

export interface CirTap {
  doppler_hz?: number | null;
  delay_ns: number;
  power_dbm: number;
  phase_rad: number;
  path_type: string;
}

export interface PathLossModelResult {
  model: PathLossModelName;
  path_loss_db: number | null;
  delta_vs_rt_db: number | null;
  valid: boolean;
  notes: string;
}

export interface ChannelAnalysisRequest {
  num_time_steps?: number;
  sampling_frequency_hz?: number | null;
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  rx_id?: string | null;
  num_cfr_points?: number;
  // OFDM subcarrier spacing for RSRP/RSSI/RSRQ (kHz; 30 = 5G FR1, 15 = LTE).
  subcarrier_spacing_khz?: number;
  /** Persist as a stored run (kind "channel") so the Metrics dashboard
   *  survives reload. Off for auto-reruns to avoid spamming results/. */
  persist?: boolean;
}

export interface ChannelAnalysisResult {
  /** "unsaved" for interactive analyses; a real id when persisted. */
  result_id?: string;
  created_at?: string | null;
  doppler_spread_hz?: number | null;
  mean_doppler_hz?: number | null;
  max_doppler_hz?: number | null;
  coherence_time_ms?: number | null;
  cir_time_s?: number[];
  cir_time_envelope_db?: number[];
  tx_id: string;
  rx_id: string;
  backend: string;
  frequency_hz: number;
  bandwidth_hz: number;
  distance_3d_m: number;
  rss_dbm: number | null;
  rt_path_loss_db: number | null;
  snr_db: number | null;
  // Co-channel interference: summed ray-traced power of every OTHER TX at the
  // RX (null when single TX / nothing else reaches). sinr_db == snr_db then.
  interference_dbm?: number | null;
  num_interferers?: number;
  sinr_db?: number | null;
  shannon_capacity_mbps: number | null;
  // 3GPP measurement quantities (TS 38.215-style) over an OFDM grid.
  rsrp_dbm?: number | null;
  rssi_dbm?: number | null;
  rsrq_db?: number | null;
  num_resource_blocks?: number | null;
  subcarrier_spacing_khz?: number;
  num_paths: number;
  k_factor_db: number | null;
  mean_delay_ns: number | null;
  rms_delay_spread_ns: number | null;
  coherence_bandwidth_mhz: number | null;
  cir: CirTap[];
  cfr_freq_offset_hz: number[];
  cfr_mag_db: number[];
  pl_models: PathLossModelResult[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

export type BeamformingMode = "codebook_sweep" | "tx_mrt" | "svd";

export interface BeamformingRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  rx_id?: string | null;
  tx_rows?: number;
  tx_cols?: number;
  rx_rows?: number;
  rx_cols?: number;
  mode?: BeamformingMode;
  /** True: honor Device.orientation_deg (broadside-relative codebook angles,
   *  comparable to fixed-bearing BS datasets). False/omitted: panels auto-aim
   *  at each other (look_at) — the lab-preset behavior. */
  use_device_orientation?: boolean;
  sweep_start_deg?: number;
  sweep_stop_deg?: number;
  sweep_step_deg?: number;
}

export interface BeamformingResult {
  backend: string;
  simulation_config_id: string;
  tx_id: string;
  rx_id: string;
  frequency_hz: number;
  tx_array: [number, number];
  rx_array: [number, number];
  num_paths: number;
  single_element_dbm: number | null;
  tx_mrt_gain_db: number | null;
  svd_gain_db: number | null;
  mode: string;
  codebook_gain_db: number | null;
  best_tx_angle_deg: number | null;
  best_rx_angle_deg: number | null;
  sweep_angles_deg: number[];
  sweep_gain_db: (number | null)[][] | null;
  warnings: string[];
  metadata: Record<string, unknown>;
}

// -------------------------------------------------------------- datasets

export interface DatasetSampling {
  mode: "random" | "grid" | "trajectory";
  region_min?: Vec3 | null;
  region_max?: Vec3 | null;
  height_m: number;
  num_samples: number;
  grid_spacing_m: number;
  start_m?: Vec3 | null;
  end_m?: Vec3 | null;
  seed: number;
  // Snap sampled z to the scene surface underneath + height_m (outdoor terrain).
  follow_terrain?: boolean;
  /** Explicit flight-path polyline (>=2 pts), arc-length resampled to
   *  num_samples. Precedence: waypoints > actor_id > start_m/end_m. */
  waypoints?: Vec3[] | null;
  /** Sample along a scene actor's authored trajectory (its own dt/speed). */
  actor_id?: string | null;
  /** Finite-difference time step behind the ue_velocity labels. */
  dt_s?: number;
}

export interface DatasetGenerateRequest {
  name: string;
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  rx_id?: string | null;
  sampling: DatasetSampling;
  num_cfr_points: number;
  include_paths: boolean;
}

export interface DatasetInfo {
  dataset_id: string;
  name: string;
  num_samples: number;
  num_cfr_points: number;
  created_at?: string | null;
  files: string[];
  size_bytes: number;
  warnings: string[];
  metadata: Record<string, unknown>;
}

export interface DatasetListResponse {
  datasets: DatasetInfo[];
}

// DELETE /projects/{pid}/datasets/{dataset_id} — remove a dataset's files.
export interface DatasetDeleteResponse {
  deleted: boolean;
  dataset_id: string;
}

// -------------------------------------------------------------------- ai

/** One selectable model for an AI provider (GET /ai/models). */
export interface AIModelInfo {
  id: string;
  label: string;
  is_default: boolean;
  // False when the configured default was injected into the list but a
  // reachable server's discovery did not include it (picking it will likely
  // make the server substitute a loaded model).
  is_available?: boolean;
}

/** Models available for a single provider ("local_openai", "ollama_text"),
 *  plus whether the provider is reachable right now. `default_model` is the id
 *  the backend uses when the request omits `model` (null = provider decides). */
export interface ProviderModels {
  provider: string;
  available: boolean;
  models: AIModelInfo[];
  default_model: string | null;
  detail: string;
}

export interface AIModelsResponse {
  providers: ProviderModels[];
}

/** GET/PUT /api/settings/ai — global local-AI endpoints + default models.
 *  Values are the EFFECTIVE ones (overlay > env > built-in default).
 *  `overlay_fields` names the fields coming from ~/.seam/settings.json;
 *  `env_fields` names the fields that also have a SEAM_* env var set (the
 *  overlay wins over it — the dialog shows a hint). */
export interface AISettings {
  ollama_url: string;
  text_model: string;
  openai_url: string;
  openai_model: string;
  overlay_fields: string[];
  env_fields: string[];
  overlay_path: string;
  warnings: string[];
}

/** PUT body: a FULL replacement of the AI overlay. An empty string clears
 *  that field back to the env value / built-in default. */
export interface AISettingsUpdate {
  ollama_url: string;
  text_model: string;
  openai_url: string;
  openai_model: string;
}

export interface MaterialAlternative {
  rf_material_id: string;
  confidence: number;
}

export interface MaterialSuggestion {
  prim_id: string;
  recommended_rf_material_id: string;
  confidence: number;
  evidence: string[];
  alternatives: MaterialAlternative[];
  needs_user_confirmation: boolean;
}

/** One VLM evidence crop: the texture region the model actually saw for a
 *  prim, as a project-relative asset path (servable via GET .../assets/...). */
export interface EvidenceImage {
  prim_id: string;
  asset_path: string;
}

export interface MaterialSuggestionResponse {
  suggestions: MaterialSuggestion[];
  provider: string;
  model: string | null;
  prompt_version: string | null;
  warnings: string[];
  evidence_images?: EvidenceImage[] | null;
}

export interface SuggestMaterialsRequest {
  prim_ids?: string[] | null;
  provider?: string | null;
  // Explicit model id within the chosen provider; null = the provider default.
  model?: string | null;
  screenshot_data_url?: string | null;
  // Multi-view capture (up to 6): the old single-image field stays for
  // back-compat and is treated by the backend as a one-item list.
  screenshot_data_urls?: string[] | null;
  attach_texture_crops?: boolean;
}

export interface SuggestionDecision {
  prim_id: string;
  action: "approve" | "reject" | "edit";
  rf_material_id?: string | null;
}

/** POST /ai/generate-rules — natural-language → assignment rules. `provider`
 *  and `model` are both optional (null = server picks the best provider / the
 *  provider default model). */
export interface GenerateRulesRequest {
  instruction: string;
  provider?: string | null;
  model?: string | null;
}

/** One name-match rule: any prim whose name contains one of
 *  `match_name_contains` gets `rf_material_id`. */
export interface AssignmentRule {
  id: string;
  match_name_contains: string[];
  rf_material_id: string;
  note?: string | null;
}

export interface GenerateRulesResponse {
  rules: AssignmentRule[];
  provider: string;
  model?: string | null;
  warnings: string[];
}

/** POST /ai/apply-rules — evaluate edited rules into reviewable suggestions
 *  (nothing touches the scene until the user applies decisions). */
export interface ApplyRulesRequest {
  rules: AssignmentRule[];
}

/** POST /ai/explain-validation — plain-language walk-through of the current
 *  validation report (read-only). */
export interface ExplainValidationResponse {
  explanation: string;
  provider: string;
  model?: string | null;
  warnings: string[];
}

// -------------------------------------------------- calibration (RF disambig)

/** One measured link: RX position + measured path gain (dB). Also the row
 *  shape for flight-log validation (optional capture time orders the replay). */
export interface MeasurementSample {
  measurement_id?: string | null;
  time_s?: number | null;
  rx_position: Vec3;
  tx_id?: string | null;
  measured_path_gain_db: number;
  measured_rms_delay_spread_ns?: number | null;
}

/** RF-sensing disambiguation (Dai et al., JSTEAP 2025): which candidate
 *  material best explains the measurements for these prims? */
export interface DisambiguationRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  prim_ids: string[];
  candidate_material_ids: string[];
  measurements: MeasurementSample[];
}

export interface DisambiguationCandidate {
  material_id: string;
  rmse_db?: number | null;
  mean_abs_error_db?: number | null;
  level_offset_db?: number | null;
  n_links: number;
}

export interface DisambiguationReport {
  prim_ids: string[];
  candidates: DisambiguationCandidate[];
  best_material_id?: string | null;
  backend: string;
  warnings: string[];
}

// ------------------------------------------------ material-impact analysis

/** Material-aware vs single-material-baseline channel impact (Lee et al.,
 *  KICS 2026): NMSE / cosine / dRSS / capacity along a set of positions. */
export interface MaterialImpactRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  rx_id?: string | null;
  waypoints?: Vec3[] | null;
  baseline_material_id?: string;
  num_cfr_points?: number;
  sensitive_nmse_db?: number;
}

export interface PositionImpact {
  position: Vec3;
  nmse_db?: number | null;
  cosine_similarity?: number | null;
  delta_rss_db?: number | null;
  rss_material_dbm?: number | null;
  rss_baseline_dbm?: number | null;
  material_sensitive: boolean;
}

export interface MaterialImpactReport {
  baseline_material_id: string;
  tx_id: string;
  rx_id: string;
  global_nmse_db?: number | null;
  mean_cosine_similarity?: number | null;
  mean_delta_rss_db?: number | null;
  mean_capacity_material_mbps?: number | null;
  mean_capacity_baseline_mbps?: number | null;
  material_sensitive_count: number;
  positions: PositionImpact[];
  backend: string;
  warnings: string[];
}

export interface ApplySuggestionsRequest {
  decisions: SuggestionDecision[];
  suggestions: MaterialSuggestion[];
  provider: string;
  model?: string | null;
}

// ------------------------------------------------ material segmentation
// Multi-material building split: a mask + per-face material assignment is
// previewed (reviewable overlay), then physically baked into the visual GLB
// as per-material sub-prims (apply), with a GLB backup for undo.

export type MaskSource = "color_heuristic" | "vlm_tile_vote" | "user_png";

export interface SegmentationPreviewRequest {
  prim_id: string;
  mask_source: MaskSource;
  // Mask images are top-left-origin, GLB UVs bottom-left; leave true unless
  // the atlas was authored with an already-flipped V.
  flip_v?: boolean;
  // vlm_tile_vote tuning (ignored for the other sources).
  tile_px?: number;
  max_tiles?: number;
  // null = the provider default model (same convention as suggest-materials).
  model?: string | null;
  // user_png: project-relative path returned by upload-mask.
  mask_asset_path?: string | null;
}

/** One material region in the split (material_id is the mask id, not an RF id). */
export interface SegmentationRegion {
  material_id: number;
  name: string;
  rf_material_id: string;
  face_count: number;
}

export interface SegmentationPreviewResponse {
  batch_id: string;
  // Project-relative refs servable via GET /projects/{id}/assets/{path}.
  mask_ref: string;
  overlay_asset_path: string;
  manifest: SegmentationRegion[];
  // Per-face material id in mesh face order (can be ~700k entries).
  face_materials: number[];
  total_faces: number;
}

/** vlm_tile_vote kicks off an async job instead of answering inline. */
export interface SegmentationJobStart {
  job_id: string;
}

export interface SegmentationJobStatus {
  status: "running" | "done" | "error";
  progress: number;
  total: number;
  detail: string;
  result?: SegmentationPreviewResponse | null;
}

export interface SegmentationApplyRequest {
  prim_id: string;
  // The preview's mask_ref (material_mask_ids.png under the batch dir).
  mask_ref: string;
  flip_v?: boolean;
}

export interface SegmentationApplyResponse {
  added_prim_ids: string[];
  removed_prim_id: string;
  backup_glb: string;
  batch_id: string;
}

export interface SplitPartsRequest {
  prim_id: string;
  // Components below min_faces (and beyond the max_parts largest) pool into
  // one "rest" sub-mesh; new prims inherit the source RF binding verbatim.
  min_faces?: number;
  max_parts?: number;
}

export interface SplitPartsResponse {
  added_prim_ids: string[];
  removed_prim_id: string;
  backup_glb: string;
  batch_id: string;
  part_face_counts: Record<string, number>;
}

export interface SegmentationUndoRequest {
  batch_id: string;
}

export interface SegmentationUndoResponse {
  restored_prim_id: string;
  removed_prim_ids: string[];
}

export interface MaskUploadResponse {
  // Project-relative path to pass back as mask_asset_path.
  mask_asset_path: string;
  width: number;
  height: number;
}

/** Material classes for the segmentation overlay tint (id → name → color).
 *  Matches the backend heuristic classes; unknown ids fall back to class 0. */
export const SEGMENTATION_CLASSES: { id: number; name: string; color: string }[] = [
  { id: 0, name: "unknown", color: "#282828" },
  { id: 1, name: "concrete", color: "#69a98e" },
  { id: 2, name: "glass", color: "#4a7cb0" },
  { id: 3, name: "metal", color: "#d97706" },
  { id: 4, name: "ground", color: "#b0833e" },
];

export function segmentationClassColor(materialId: number): string {
  return (
    SEGMENTATION_CLASSES.find((c) => c.id === materialId)?.color ??
    SEGMENTATION_CLASSES[0].color
  );
}

// ------------------------------------------------ SEAM-Agent (AI material authoring)
// An agentic material-authoring job: the frontend captures multi-view RGB +
// triangle-id buffers of one prim's mesh and POSTs them to the backend agent,
// which segments the surface, gathers web/image evidence, and proposes an
// RF-material assignment per segment. The trace endpoint streams a live
// activity log (steps + evidence + segments) polled until the job settles;
// apply bakes the accepted segments into the visual GLB (reusing the same
// per-material sub-prim machinery + undo as the material segmentation split).

/** One captured view of the target prim's mesh. `tri_id_png_data_url` encodes
 *  faceIndex as a uint24 RGB (r<<16|g<<8|b), background 0xFFFFFF, so the agent
 *  can map any pixel it reasons about back to a triangle. */
export interface AgentView {
  view_id: string;
  /** JPEG data URL, ~768px longest side. */
  rgb_data_url: string;
  /** PNG data URL: RGB = faceIndex uint24, background = white (0xFFFFFF). */
  tri_id_png_data_url: string;
  width: number;
  height: number;
}

/** Optional caps on the agent's tool usage / runtime (backend defaults apply
 *  to any omitted field). */
export interface AgentBudget {
  max_web_searches?: number;
  max_image_searches?: number;
  max_vlm_calls?: number;
  /** Zoomed re-queries for low-confidence regions (0 disables refinement). */
  max_refine_calls?: number;
  max_runtime_sec?: number;
}

export interface AgentStartRequest {
  prim_id: string;
  user_hint?: string | null;
  allow_web: boolean;
  /** null = provider default (same convention as suggest-materials). */
  model?: string | null;
  views: AgentView[];
  budget?: AgentBudget;
}

export interface AgentStartResponse {
  job_id: string;
}

export type AgentStatus = "running" | "needs_review" | "error" | "done" | "cancelled";

/** One step in the agent's activity trace (the "thinking" visibility). */
export interface AgentStep {
  step_id: string;
  status: "running" | "done" | "error";
  summary: string;
  /** Search/tool queries issued in this step (rendered as chips). */
  queries?: string[];
}

/** One piece of gathered evidence (a web result, image match, datasheet…). */
export interface AgentEvidence {
  evidence_id: string;
  type: string;
  claim: string;
  source_url?: string;
  page_url?: string;
  /** Project-relative asset path (serve via api.assetUrl). */
  thumb_asset_path?: string;
  query?: string;
}

/** A proposed material assignment for one surface segment. */
export interface AgentSegment {
  segment_id: string;
  semantic_label: string;
  face_count: number;
  rf_material_id: string;
  confidence: number;
  alternatives?: { rf_material_id: string; confidence: number }[];
  evidence_ids: string[];
  /** Tinted render crop showing exactly which faces this segment covers. */
  preview_asset_path?: string | null;
}

/** Live pipeline progress (stage + counters + measured ETA). */
export interface AgentProgress {
  stage: string;
  stage_index?: number;
  total_stages?: number;
  views_done?: number;
  views_total?: number;
  vlm_calls?: number;
  elapsed_sec?: number;
  eta_sec?: number | null;
}

/** A per-view render the agent saved for review (what the VLM saw). */
export interface AgentViewAsset {
  view_id: string;
  asset_path: string;
}

export interface AgentTrace {
  status: AgentStatus;
  detail?: string;
  steps: AgentStep[];
  evidence: AgentEvidence[];
  segments?: AgentSegment[];
  progress?: AgentProgress | null;
  views?: AgentViewAsset[];
}

export interface AgentCancelResponse {
  status: "accepted" | "not_running";
}

// ------------------------------------------- device / trajectory JSON import
//
// Request bodies are user-authored JSON (cartesian [x,y,z] / {x,y,z} or
// geographic {lat,lon, alt_m|agl_m}, auto-detected per point) — the FE passes
// the parsed file through untouched; the backend validates and normalizes.

export interface DeviceImportResponse {
  added_ids: string[];
  updated_ids: string[];
  warnings: string[];
}

export interface TrajectoryImportResponse {
  ue_id: string | null;
  /** Fully-cartesian Z-up waypoints, ready for the trajectory routes UI. */
  waypoints: Vec3[];
  /** Per-waypoint [yaw, pitch, roll] degrees (parallel to waypoints; null
   *  where a point gave none). Null when no waypoint carried an orientation. */
  orientations_deg?: (Vec3 | null)[] | null;
  warnings: string[];
}

export interface AgentApplyRequest {
  segment_ids: string[];
}

export interface AgentApplyResponse {
  added_prim_ids: string[];
  removed_prim_id: string;
  backup_glb: string;
  batch_id: string;
}

// -------------------------------------------------------------- projects

export interface ProjectInfo {
  project_id: string;
  name: string;
  path: string;
  scene_id: string | null;
  created_at: string | null;
  modified_at: string | null;
}

/** POST /projects/import — the created project plus non-fatal import warnings
 *  (skipped meshes, out-of-band material remaps) so they can be surfaced
 *  instead of buried in provenance.json. */
export interface SceneImportResult extends ProjectInfo {
  warnings: string[];
}

/** GET /projects/import/jobs/{job_id} — background scene-import job status.
 *  POST /projects/import/start returns { job_id }; the UI polls this until
 *  status leaves "running" (phases: extracting → parsing → converting →
 *  writing). `project` and `warnings` are set on "done"; `error` on "error". */
export interface ImportJobStatus {
  job_id: string;
  status: "running" | "done" | "error";
  phase: string;
  done: number;
  total: number;
  project: ProjectInfo | null;
  warnings: string[];
  error: string | null;
}

/** GET /projects/{id}/scene/positions — positions-only live feed (the 2s
 *  Live-sync poll uses this instead of downloading the whole scene). */
export interface ScenePositions {
  devices: { id: string; position: Vec3; orientation_deg: Vec3 }[];
  actors: { id: string; position: Vec3; orientation_deg: Vec3 }[];
}

export interface ProjectCreateRequest {
  name: string;
  project_id?: string | null;
  template?: "empty" | "demo";
}

// DELETE /projects/{pid} — permanently removes the project folder.
export interface ProjectDeleteResponse {
  deleted: boolean;
  project_id: string;
}

// POST /projects/{pid}/results/prune — remove result files + their
// ResultSetRef entries, keeping the newest `keep_latest` per kind. `kinds`
// null = every kind.
export interface ResultsPruneRequest {
  keep_latest?: number;
  kinds?: ResultSetRef["kind"][] | null;
}

export interface ResultsPruneResponse {
  removed: string[];
  kept: string[];
}

export interface HealthBackendStatus {
  name: string;
  available: boolean;
  detail: string;
}

export interface AIProviderStatus {
  name: string;
  available: boolean;
  model: string | null;
  detail: string;
}

export interface HealthResponse {
  status: "ok";
  app: string;
  version: string;
  schema_version: string;
  sionna_available: boolean;
  backends: HealthBackendStatus[];
  ai_providers: AIProviderStatus[];
  project_roots: string[];
}

// ---------------------------------------------------- session-wave additions

/** PATCH /projects/{id}/results/{result_id}/label — name/clear a stored run. */
export interface ResultLabelRequest {
  label?: string | null;
}

/** POST /projects/{id}/duplicate — deep-copy a project folder. */
export interface ProjectDuplicateRequest {
  new_id?: string | null;
  name?: string | null;
}

/** PATCH /projects/{id} — rename the project (display name only). */
export interface ProjectRenameRequest {
  name: string;
}

/** POST /rf/materials/import — merge a material library into the project. */
export interface MaterialImportRequest {
  materials: RFMaterial[];
}

export interface MaterialImportResponse {
  imported: string[];
  /** Map of requested id -> id it was renamed to on collision. */
  renamed: Record<string, string>;
  skipped: string[];
}

/** POST /simulate/radio-map-sweep — one planar solve per altitude. */
export interface RadioMapSweepRequest {
  heights_m: number[];
  threshold_db?: number | null;
  config_id?: string | null;
  config?: SimulationConfig | null;
}

export interface RadioMapSweepRun {
  height_m: number;
  result_id: string;
}

export interface RadioMapSweepCoveragePoint {
  height_m: number;
  /** Fraction of solved cells >= threshold_db; null when no threshold given. */
  coverage: number | null;
}

export interface RadioMapSweepResult {
  runs: RadioMapSweepRun[];
  coverage: RadioMapSweepCoveragePoint[];
}

/** POST /analyze/channel-sweep — link metrics vs one swept config field. */
export interface ChannelSweepRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  rx_id?: string | null;
  num_cfr_points?: number;
  num_time_steps?: number;
  sampling_frequency_hz?: number | null;
  subcarrier_spacing_khz?: number;
  sweep_field: "frequency_hz" | "tx_power_dbm" | "bandwidth_hz" | "noise_figure_db";
  sweep_values: number[];
}

export interface ChannelSweepPoint {
  value: number;
  path_loss_db: number | null;
  rss_dbm: number | null;
  snr_db: number | null;
  sinr_db: number | null;
  rms_delay_spread_ns: number | null;
  k_factor_db: number | null;
}

export interface ChannelSweepResult {
  tx_id: string;
  rx_id: string;
  backend: string;
  sweep_field: string;
  rows: ChannelSweepPoint[];
  warnings: string[];
}

/** POST /analyze/spectrogram — Doppler-time STFT of the channel h(t). */
export interface SpectrogramRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  rx_id?: string | null;
  duration_s?: number;
  sampling_frequency_hz?: number;
  window?: number;
  hop?: number | null;
}

export interface SpectrogramResult {
  tx_id: string;
  rx_id: string;
  backend: string;
  frequency_hz: number;
  sampling_frequency_hz: number;
  window: number;
  hop: number;
  num_paths: number;
  times_s: number[];
  doppler_hz: number[];
  /** magnitude_db[frame][bin], fftshifted so 0 Hz sits at window/2. */
  magnitude_db: number[][];
  warnings: string[];
  metadata: Record<string, unknown>;
}

// MeasurementSample (shared with calibration, declared above) is the row
// shape for the flight-log validation request below.

/** POST /calibrate/validate-trajectory — replay measured RX positions and
 *  compare measured vs predicted path gain along the flight. */
export interface TrajectoryValidationRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  measurements?: MeasurementSample[] | null;
  max_points?: number;
  /** "noncoherent" (power sum — RSRP-like wideband logs, default) or
   *  "coherent" (CW/narrowband drive tests, fading nulls included). */
  metric?: "noncoherent" | "coherent";
}

export interface TrajectoryValidationPoint {
  index: number;
  time_s: number | null;
  position: number[];
  measured_db: number;
  // Null when the point produced zero ray paths (excluded from the stats but
  // now INCLUDED in points[] so failures are visible).
  predicted_db: number | null;
  aligned_predicted_db: number | null;
  error_db: number | null;
  path_count: number;
}

export interface TrajectoryValidationStats {
  level_offset_db: number;
  rmse_db: number;
  mean_abs_error_db: number;
  n: number;
}

export interface TrajectoryValidationReport {
  tx_id: string;
  points: TrajectoryValidationPoint[];
  stats: TrajectoryValidationStats;
  backend: string;
  warnings: string[];
}

/* ------------------------------------------------------------------ *
 * Multimodal sensor data + GT-vs-DT playback (issue #3)
 * ------------------------------------------------------------------ */

export interface SensorChannel {
  key: string;
  kind: "image" | "pointcloud" | "beam_profile" | "json";
  label: string;
}

export interface SensorFramePose {
  position: Vec3;
  orientation_deg: Vec3;
}

export interface SensorGtMetrics {
  rss_dbm: number | null;
  rss_coherent_dbm: number | null;
  tau_rms_ns: number | null;
  n_paths: number | null;
  best_beam_deg: number | null;
}

export interface SensorFrame {
  index: number;
  time_s: number | null;
  // channel key -> project-relative file path (serve via api.assetUrl).
  files: Record<string, string>;
  pose: SensorFramePose | null;
  gt: SensorGtMetrics | null;
}

export interface SensorManifest {
  version: number;
  entity_id: string | null;
  channels: SensorChannel[];
  frames: SensorFrame[];
}

export interface SensorSegment {
  label: string;
  // Inclusive positions into the sorted frames array (not frame indices).
  start: number;
  end: number;
}

export interface SensorManifestResponse {
  manifest: SensorManifest;
  segments: SensorSegment[];
  warnings: string[];
}

// GT beam_profile channel file payload ({azimuth_deg, power_dbm}).
export interface BeamProfile {
  azimuth_deg: number[];
  power_dbm: (number | null)[];
}

export interface PlaybackFrame {
  index: number;
  time_s: number | null;
  rx_position: Vec3;
  // World azimuth (deg) per sweep sample; power is absolute dBm.
  beam_azimuth_deg: number[];
  beam_power_dbm: (number | null)[];
  best_beam_deg: number | null;
  rss_dbm: number | null;
  rss_coherent_dbm: number | null;
  tau_rms_ns: number | null;
  n_paths: number;
  paths: RayPath[];
}

export interface PlaybackResultSet {
  result_id: string;
  kind: "playback";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  tx_id: string;
  rx_id: string;
  frames: PlaybackFrame[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

export interface PlaybackBuildRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_id?: string | null;
  rx_id?: string | null;
  frame_stride?: number;
  max_frames?: number;
  tx_rows?: number;
  tx_cols?: number;
  rx_rows?: number;
  rx_cols?: number;
  use_device_orientation?: boolean;
  sweep_start_deg?: number;
  sweep_stop_deg?: number;
  sweep_step_deg?: number;
  max_paths_per_frame?: number;
}

// --------------------------------------------------------------- sensing

export interface SensingTargetSummary {
  actor_id: string;
  model: "tr38901" | "constant";
  object_type: string | null;
  model_type: number | null;
  num_scattering_points: number;
  /** World xyz of every scattering point (markers in the viewer). */
  scattering_points: Vec3[];
  /** World center of the target cuboid (actor base + height/2). */
  position: Vec3;
  /** Cuboid [yaw, pitch, roll] deg (yaw from the trajectory at t=0 when the velocity is). */
  orientation_deg?: Vec3;
  velocity_m_s: Vec3;
  size_m: Vec3;
  /** constant: the bound value; tr38901: the spec's mean monostatic σ_M. */
  rcs_dbsm: number | null;
  path_count: number;
}

export interface SensingResultSet {
  result_id: string;
  kind: "sensing";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  /** Sensing paths first (path_type "sensing", target_id set), then the comm
   *  paths when the request set include_comm_paths. */
  paths: RayPath[];
  targets: SensingTargetSummary[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

/** Body for POST /projects/{pid}/simulate/sensing. */
export interface SensingSimulateRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_ids?: string[] | null;
  rx_ids?: string[] | null;
  /** null = every actor whose sensing binding is enabled. */
  target_actor_ids?: string[] | null;
  /** Also run the normal paths solve and append its RayPaths. */
  include_comm_paths?: boolean;
  samples_per_sp?: number;
  /** RCSSolver depth (counts the scattering event); null = max(1, config). */
  max_depth?: number | null;
}

// ------------------------------------------- detector models (Phase C)

/** Target fluctuation of the square-law detector: Swerling 0 = steady,
 *  1 = Rayleigh scan-to-scan (the v0.1.12 numbers), 3 = one dominant scatterer. */
export type DetectorModel = "swerling0" | "swerling1" | "swerling3";
export const DETECTOR_MODELS: DetectorModel[] = ["swerling0", "swerling1", "swerling3"];
/** schemas/sensing.py: Monte Carlo trials per Pd estimate / per request. */
export const MAX_MC_TRIALS = 2_000_000;
export const MAX_MC_TOTAL_TRIALS = 200_000_000;

export interface DetectorOptions {
  model?: DetectorModel;
  /** 0 = analytic Pd only. */
  monte_carlo_trials?: number;
  /** Threshold from a noise-only run instead of -ln(Pfa); needs trials >= 20 / pfa. */
  empirical_threshold?: boolean;
  seed?: number;
}

/** Constant-velocity EKF over the scenario frames. */
export interface TrackingOptions {
  enabled?: boolean;
  /** White (piecewise-constant) acceleration std of the process noise. */
  process_accel_sigma_m_s2?: number;
  /** Mahalanobis gate per scalar measurement (chi-square, 1 dof). */
  gate_chi2?: number;
  init_from?: "fusion";
  /** Consecutive frames without an accepted update before the track is dropped. */
  coast_max_frames?: number;
  /** The track is also dropped once its posterior sqrt(trace(P_pos)) exceeds this (m). */
  max_position_std_m?: number;
  /** Seed the next frame's Gauss-Newton fusion with the predicted position. */
  use_as_prior?: boolean;
}

// ------------------------------------------- sensing over time (scenario)

/** ScenarioSimulateRequest.sensing: a sensing solve in every scenario frame,
 *  turned into per-link detections and a fused target estimate. */
export interface SensingTrackOptions {
  enabled?: boolean;
  /** Detection threshold on the post-integration SNR [dB]. */
  threshold_db?: number;
  /** Coherent processing interval [s]; Doppler resolution = 1 / cpi_s. */
  cpi_s?: number;
  /** Coherently integrated pulses / OFDM samples: gain 10*log10(cpi_pulses). */
  cpi_pulses?: number;
  /** MTI notch: |f_D| below this is rejected. null = 1 / cpi_s; 0 = MTI off. */
  mti_min_doppler_hz?: number | null;
  /** Also solve the comm paths (targets as absorbers) into echoes (target_id null). */
  include_comm_paths?: boolean;
  /** null = every actor whose sensing binding is enabled. */
  target_actor_ids?: string[] | null;
  samples_per_sp?: number;
  max_depth?: number | null;
  /** Gaussian noise (sigma = cell / sqrt(2 SNR)) on the fused range / Doppler. */
  measurement_noise?: boolean;
  noise_seed?: number;
  /** null or enabled=false: no tracking (v0.1.12 output). */
  tracking?: TrackingOptions | null;
  /** Pd model of the link reports (needs pfa); detection stays snr_db >= threshold_db. */
  detector?: DetectorOptions;
  /** null: link reports carry no Pd. */
  pfa?: number | null;
}

export type SensingDetectionReason = "detected" | "no_echo" | "below_threshold" | "mti_rejected";

/** Detection of one target on one TX->RX link in one scenario frame. */
export interface SensingLinkReport {
  tx_id: string;
  rx_id: string;
  target_id: string;
  /** Strongest DIRECT echo; without one, the strongest echo of any kind. */
  path_id: string | null;
  num_echoes: number;
  /** The reported echo is not a direct one (excluded from fusion). */
  multipath: boolean;
  /** c * tau of the reported echo (|TX - p| + |p - RX| when direct) [m]. */
  bistatic_range_m: number | null;
  doppler_hz: number | null;
  echo_power_dbm: number | null;
  /** echo_power - noise_floor + integration_gain [dB]. */
  snr_db: number | null;
  /** What fusion consumed (exact values plus noise when measurement_noise). */
  measured_range_m: number | null;
  measured_doppler_hz: number | null;
  range_bin: number | null;
  doppler_bin: number | null;
  detected: boolean;
  reason: SensingDetectionReason;
  /** Pd of snr_db under sensing.detector.model at sensing.pfa; null when the
   *  run set no pfa. pd_mc: its Monte Carlo (trials > 0 and an echo). */
  pd?: number | null;
  pd_mc?: number | null;
}

export type TargetEstimateStatus = "ok" | "insufficient_links" | "diverged";

/** EKF track state of one target in one frame (tracking enabled). */
export type TrackStatus = "none" | "init" | "tracking" | "coasting" | "lost";

/** Multistatic position/velocity estimate of one target in one frame. */
export interface TargetEstimate {
  target_id: string;
  status: TargetEstimateStatus;
  n_links_detected: number;
  /** Geometrically distinct detected direct echoes that entered the fusion. */
  n_links_used: number;
  /** "tx_id>rx_id" of each fused link, highest SNR first. */
  links_used: string[];
  /** Ground truth: target cuboid center + velocity the solve used. */
  position_true: Vec3;
  velocity_true: Vec3;
  position_est: Vec3 | null;
  velocity_est: Vec3 | null;
  position_error_m: number | null;
  velocity_error_m_s: number | null;
  /** Position error per metre of range error at the estimate. */
  gdop: number | null;
  rms_residual_m: number | null;
  iterations: number;
  /** EKF track (sensing.tracking enabled); every field null otherwise. */
  track_status?: TrackStatus | null;
  /** Posterior state at this frame (null for none / lost). */
  track_position?: Vec3 | null;
  track_velocity?: Vec3 | null;
  track_position_error_m?: number | null;
  track_velocity_error_m_s?: number | null;
  /** sqrt(trace(P_pos)): RMS of the 3-D position error the filter expects. */
  track_position_std_m?: number | null;
  /** Scalar measurements (range, Doppler) accepted / gated out this frame. */
  track_updates?: number | null;
  track_gated?: number | null;
}

/** A TX/RX device as one sensing frame used it. */
export interface SensingNodeState {
  id: string;
  position: Vec3;
  velocity: Vec3;
}

export interface SensingFrame {
  /** Echo paths (path_type "sensing", target_id set), then comm paths when
   *  include_comm_paths (target_id null). */
  echoes: RayPath[];
  links: SensingLinkReport[];
  estimates: TargetEstimate[];
  /** Every selected TX and RX at this frame (v0.1.13+; null in older results). */
  nodes?: SensingNodeState[] | null;
}

/** Per-target run summary in ScenarioResultSet.metadata.sensing.targets. */
export interface ScenarioSensingTargetSummary {
  frames: number;
  detected_frames: number;
  detection_rate: number;
  link_detection_rate: number;
  frames_ge3_links: number;
  frames_ge3_links_rate: number;
  ok_frames: number;
  median_position_error_m: number | null;
  p90_position_error_m: number | null;
  median_velocity_error_m_s: number | null;
  median_gdop: number | null;
  // Tracking enabled only (absent otherwise).
  /** Frames with status init / tracking / coasting. */
  tracked_frames?: number;
  coasting_frames?: number;
  lost_frames?: number;
  /** Frames whose track the max_position_std_m cap dropped. */
  lost_by_std_frames?: number;
  median_track_position_error_m?: number | null;
  p90_track_position_error_m?: number | null;
  median_track_velocity_error_m_s?: number | null;
  /** Fusion ok and the track error below the fusion error. */
  frames_improved_over_fusion?: number;
  /** Track state present while the fusion was not ok. */
  frames_track_without_fusion?: number;
}

/** ScenarioResultSet.metadata.sensing (present only when sensing ran). */
export interface ScenarioSensingSummary {
  options: SensingTrackOptions;
  frequency_hz: number;
  wavelength_m: number;
  bandwidth_hz: number;
  noise_figure_db: number;
  noise_floor_dbm: number;
  integration_gain_db: number;
  threshold_db: number;
  cpi_s: number;
  mti_min_doppler_hz: number;
  /** c / 2B (monostatic range cell). */
  range_resolution_m: number;
  /** c / B (bistatic range-sum cell). */
  bistatic_range_resolution_m: number;
  doppler_resolution_hz: number;
  velocity_resolution_m_s: number;
  /** Monostatic radial speed at the MTI notch edge: lambda * f_min / 2. */
  mti_blind_speed_m_s: number;
  num_links: number;
  target_ids: string[];
  targets: Record<string, ScenarioSensingTargetSummary>;
  detection_rate: number;
  frames_ge3_links_rate: number;
  median_position_error_m: number | null;
  median_velocity_error_m_s: number | null;
  /** Tracking enabled only. */
  median_track_position_error_m?: number | null;
  tracking_model?: string;
}

// ------------------------------------------------ ISAC beam trade-off (B1)

export type SharingMode = "time_sharing" | "dual_function";
export type UeAssociation = "serving" | "all";

/** Body for POST /projects/{pid}/simulate/isac. */
export interface ISACRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  /** null = every tx device. */
  tx_ids?: string[] | null;
  /** Both null: an rx within 1 m of a selected tx is its sensing receiver,
   *  every other rx is a UE. */
  ue_rx_ids?: string[] | null;
  sensing_rx_ids?: string[] | null;
  /** null = every actor whose sensing binding is enabled. */
  target_actor_ids?: string[] | null;
  /** serving: each UE belongs to its strongest tx; all: every tx serves every UE. */
  ue_association?: UeAssociation;
  tx_rows?: number;
  tx_cols?: number;
  /** Sensing-RX array; null = the tx array. UEs are always single element. */
  rx_rows?: number | null;
  rx_cols?: number | null;
  /** false is rejected (400): one look_at panel cannot aim at UEs and targets. */
  use_device_orientation?: boolean;
  sweep_start_deg?: number;
  sweep_stop_deg?: number;
  sweep_step_deg?: number;
  cpi_pulses?: number;
  /** Metadata only (resolutions). */
  cpi_s?: number;
  /** Per-beam "detected" flag. */
  threshold_db?: number;
  pfa?: number;
  /** Pareto summary operating point. */
  pd_target?: number;
  /** Fractions of slots/pulses spent sensing; the backend sorts and dedupes. */
  slot_ratios?: number[];
  sharing_mode?: SharingMode;
  samples_per_sp?: number;
  max_depth?: number | null;
  /** Store the echo + comm paths of the solve in the result (echoes first). */
  include_paths?: boolean;
  /** Elevation sweep of a 2-D codebook (local elevation of the panel). All
   *  three null: azimuth-only codebook. Beams: k = i_el * n_az + i_az. */
  elevation_start_deg?: number | null;
  elevation_stop_deg?: number | null;
  elevation_step_deg?: number | null;
  /** Inter-TX interference in the UE SINR (ue_association "serving" only). */
  interference?: boolean;
  detector?: DetectorOptions;
}

/** One codebook beam of one tx, used full time for comm OR sensing (rho = 1). */
export interface ISACBeam {
  angle_deg: number;
  /** Local elevation of a 2-D codebook beam; null = azimuth-only codebook. */
  elevation_deg?: number | null;
  /** Per UE of this tx: SINR dB; null = no path. Equals the SNR without
   *  interference; with it the other txs radiate their comm beams. */
  ue_sinr_db: Record<string, number | null>;
  /** request.interference only: interference-free SNR and the comm-slot
   *  interference power (null: no interferer reaches the UE). */
  ue_snr_db?: Record<string, number | null> | null;
  ue_interference_dbm?: Record<string, number | null> | null;
  /** Sum over this tx's UEs of log2(1 + SINR) [bit/s/Hz]. */
  sum_rate_bps_hz: number;
  /** Per target: echo SNR with this TX beam and the best RX beam, full CPI. */
  target_snr_db: Record<string, number | null>;
  target_best_rx_angle_deg: Record<string, number | null>;
  /** 2-D codebook only: elevation of that best RX beam. */
  target_best_rx_elevation_deg?: Record<string, number | null> | null;
  /** Weakest target (null if any target has no echo). */
  sensing_snr_db: number | null;
  /** Pd of sensing_snr_db under request.detector.model (pfa when null). */
  pd: number;
  /** Monte Carlo of pd (detector.monte_carlo_trials > 0 and an echo). */
  pd_mc?: number | null;
  detected: boolean;
}

export interface ISACPoint {
  /** Fraction of slots/pulses spent sensing. */
  rho: number;
  /** Sensing-slot beam; null for rho == 0 (comm only). */
  beam_idx: number | null;
  sum_rate_bps_hz: number;
  ue_rates_bps_hz: Record<string, number>;
  /** Weakest target, rho * cpi_pulses integrated. */
  sensing_snr_db: number | null;
  pd: number;
  /** Monte Carlo of pd: Pareto points only (detector.monte_carlo_trials > 0). */
  pd_mc?: number | null;
  pareto: boolean;
}

export interface ISACParetoSummary {
  /** rho = 0: comm beam full time. */
  comm_only_rate_bps_hz: number;
  max_pd: number;
  pd_target: number;
  /** Max rate over points with pd >= pd_target. */
  rate_at_pd_target_bps_hz: number | null;
  rho_at_pd_target: number | null;
  beam_idx_at_pd_target: number | null;
  /** comm_only - rate_at_pd_target. */
  rate_loss_at_pd_target_bps_hz: number | null;
  /** Max pd over points with rate >= 0.95 * comm_only. */
  pd_at_95pct_rate: number;
  num_pareto_points: number;
}

export interface ISACTxResult {
  tx_id: string;
  sensing_rx_id: string;
  /** 0 ~ monostatic. */
  sensing_baseline_m: number;
  /** UEs this tx serves. */
  ue_ids: string[];
  target_ids: string[];
  /** [rows, cols] */
  tx_array: [number, number];
  rx_array: [number, number];
  /** Codebook, local azimuth, aligned with beams. */
  angles_deg: number[];
  /** 2-D codebook only: local elevation per beam, aligned with beams. */
  elevations_deg?: number[] | null;
  beams: ISACBeam[];
  comm_beam_idx: number | null;
  sensing_beam_idx: number | null;
  comm_beam_angle_deg: number | null;
  sensing_beam_angle_deg: number | null;
  /** Azimuth gap. */
  angle_gap_deg: number | null;
  comm_beam_elevation_deg?: number | null;
  sensing_beam_elevation_deg?: number | null;
  elevation_gap_deg?: number | null;
  /** request.interference only: per served UE, the interference power while
   *  every other tx radiates its sensing beam (sensing slots). */
  ue_interference_sensing_dbm?: Record<string, number | null> | null;
  /** Hand-check anchors: 1x1 both ends. */
  ue_single_element_rss_dbm: Record<string, number | null>;
  target_single_element_snr_db: Record<string, number | null>;
  points: ISACPoint[];
  pareto: ISACParetoSummary;
  warnings: string[];
}

export interface ISACResultSet {
  result_id: string;
  kind: "isac";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  frequency_hz: number;
  sharing_mode: SharingMode;
  ue_association: UeAssociation;
  /** Sorted, deduped. */
  slot_ratios: number[];
  noise_floor_dbm: number;
  /** UE id -> serving tx id (null = no path to any selected tx). */
  ue_serving_tx: Record<string, string | null>;
  txs: ISACTxResult[];
  /** include_paths: echoes, then comm. */
  paths?: RayPath[] | null;
  warnings: string[];
  metadata: Record<string, unknown>;
}

// --------------------------------------------- sensing coverage map (B2)

export type SensingCoverageMetric =
  | "best_snr_db"
  | "n_links_detected"
  | "pd_best"
  | "fusion_feasible";

/** Body for POST /projects/{pid}/simulate/sensing-coverage. */
export interface SensingCoverageRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  tx_ids?: string[] | null;
  /** null = rx devices co-located (<= 1 m) with a selected tx. */
  sensing_rx_ids?: string[] | null;
  /** Point target RCS; both null = TR 38.901 uav-small-size nominal (-12.81 dBsm). */
  rcs_dbsm?: number | null;
  object_type?: TR38901ObjectType | null;
  height_m?: number;
  cell_size_m?: number;
  /** Explicit extent (RadioMapGridConfig semantics); null = scene bounds. */
  center_xy?: [number, number] | null;
  size_xy?: [number, number] | null;
  threshold_db?: number;
  cpi_pulses?: number;
  pfa?: number;
  array_gain?: "none" | "steered";
  tx_rows?: number;
  tx_cols?: number;
  /** null = tx */
  rx_rows?: number | null;
  rx_cols?: number | null;
  min_links_for_fusion?: number;
  /** pd_best uses detector.model; Monte Carlo only spot-checks 5 cells. */
  detector?: DetectorOptions;
}

export interface SensingCoverageLink {
  tx_id: string;
  rx_id: string;
  /** Links with the same foci (either order) share a group. */
  geometry_group: number;
  baseline_m: number;
  /** Cells with both legs LOS. */
  los_fraction: number;
  /** Cells with SNR >= threshold. */
  detected_fraction: number;
}

export interface SensingCoverageSummary {
  num_cells: number;
  num_links: number;
  num_geometries: number;
  /** 0..100: >= 1 link with both legs LOS. */
  pct_cells_los: number;
  /** >= 1 detected link. */
  pct_cells_detected: number;
  pct_cells_fusion_feasible: number;
  /** Over cells with an echo. */
  median_best_snr_db: number | null;
  /** detector.monte_carlo_trials > 0: 5 cells at the 0/25/50/75/100 %
   *  quantiles of best_snr_db over the cells with an echo. */
  mc_spot_check?: PdMcSpotCheck[] | null;
}

/** One coverage cell's analytic pd_best against a Monte Carlo of it. */
export interface PdMcSpotCheck {
  /** [ix, iy] (row-major index iy * nx + ix). */
  cell: [number, number];
  snr_db: number;
  pd: number;
  pd_mc: number;
  ci_low: number;
  ci_high: number;
}

export interface SensingCoverageResultSet {
  result_id: string;
  kind: "sensing_coverage";
  backend: string;
  simulation_config_id: string;
  created_at: string | null;
  frequency_hz: number;
  rcs_dbsm: number;
  /** 0 (none) or 10log10(Ntx)+10log10(Nrx). */
  array_gain_db: number;
  tx_ids: string[];
  sensing_rx_ids: string[];
  grid: RadioMapGrid;
  /** Row-major [ny][nx] like RadioMapResultSet.values, one array per metric:
   *  best_snr_db / pd_best null where no link has an echo; n_links_detected
   *  0..num_links; fusion_feasible 0 / 1 (never null). */
  values: Record<SensingCoverageMetric, (number | null)[][]>;
  links: SensingCoverageLink[];
  summary: SensingCoverageSummary;
  warnings: string[];
  metadata: Record<string, unknown>;
}

// ------------------------------------------- Pd curve (Phase C, not persisted)

/** Body for POST /projects/{pid}/analysis/pd-curve. snr_db is the
 *  post-integration SNR; cpi_pulses only changes how the Monte Carlo builds it. */
export interface PdCurveRequest {
  pfa?: number;
  snr_min_db?: number;
  snr_max_db?: number;
  step_db?: number;
  /** Deduplicated, order kept. */
  models?: DetectorModel[];
  monte_carlo_trials?: number;
  empirical_threshold?: boolean;
  seed?: number;
  cpi_pulses?: number;
  pd_target?: number;
}

/** schemas/sensing.py MAX_PD_CURVE_POINTS. */
export const MAX_PD_CURVE_POINTS = 1001;

export interface PdCurveModel {
  model: DetectorModel;
  /** Analytic, aligned with PdCurveResult.snr_db. */
  pd: number[];
  /** Monte Carlo (trials > 0): estimate and Wilson 95 % interval per point. */
  pd_mc?: number[] | null;
  pd_mc_ci_low?: number[] | null;
  pd_mc_ci_high?: number[] | null;
  /** Analytic SNR reaching pd_target (null: pd_target <= pfa). */
  snr_for_pd_target_db?: number | null;
  /** max |pd_mc - pd| and the fraction of points whose interval holds pd. */
  mc_max_abs_deviation?: number | null;
  mc_within_ci_fraction?: number | null;
}

/** POST /analysis/pd-curve response. */
export interface PdCurveResult {
  pfa: number;
  pd_target: number;
  cpi_pulses: number;
  monte_carlo_trials: number;
  seed: number;
  snr_db: number[];
  /** Analytic threshold -ln(pfa) on the noise-normalized integrated power. */
  threshold: number;
  /** empirical_threshold: the (1 - pfa) quantile of a noise-only run. */
  empirical_threshold?: number | null;
  /** trials > 0: false alarms of an independent noise-only run against the
   *  threshold in use, with its Wilson 95 % interval. */
  pfa_measured?: number | null;
  pfa_measured_ci?: [number, number] | null;
  models: PdCurveModel[];
  warnings: string[];
  metadata: Record<string, unknown>;
}

// ----------------------------------------- sensing dataset export (Phase C)

export type SensingDatasetFormat = "npz" | "csv" | "parquet";

/** Frame-level train/val/test split (fractions sum to 1). */
export interface SensingDatasetSplit {
  train?: number;
  val?: number;
  test?: number;
  seed?: number;
}

/** Body for POST /projects/{pid}/export/sensing-dataset. */
export interface SensingDatasetExportRequest {
  /** null = every stored scenario result that has sensing frames. */
  result_ids?: string[] | null;
  /** With result_ids: skip a listed result without sensing frames (named in warnings) instead of 400. */
  skip_without_sensing?: boolean;
  /** Also write one row per echo path (echoes.* tables). */
  include_echo_paths?: boolean;
  /** parquet needs pyarrow on the backend (400 otherwise). */
  formats?: SensingDatasetFormat[];
  /** null = one split "all". */
  split?: SensingDatasetSplit | null;
}

export interface SensingDatasetExportResult {
  /** Project-relative directory of the zip ("export/sensing_dataset"). */
  export_dir: string;
  zip_name: string;
  download_url: string;
  /** Entries inside the zip. */
  files: string[];
  result_ids: string[];
  num_rows: number;
  rows_per_result: Record<string, number>;
  rows_per_split: Record<string, number>;
  num_echo_rows: number;
  /** Fraction of link rows with detected = true (null: no rows). */
  detected_fraction: number | null;
  size_bytes: number;
  elapsed_s: number;
  warnings: string[];
}
