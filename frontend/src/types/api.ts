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
  | "ai_suggested"
  | "user_confirmed"
  | "measurement_calibrated";

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
  metric: "path_gain_db" | "rss_dbm";
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
  bandwidth_hz: number;
  noise_figure_db: number;
  radio_map: RadioMapGridConfig;
}

export interface ResultSetRef {
  result_id: string;
  kind: "paths" | "radio_map" | "trajectory" | "scenario";
  backend: string;
  simulation_config_id: string;
  uri: string;
  created_at: string | null;
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

export type ActorKind = "car" | "human" | "custom";

export interface ActorShape {
  type: "box" | "mesh";
  size_m: Vec3;
  mesh_ref: MeshRef | null;
}

export interface ActorTrajectory {
  waypoints: Vec3[];
  dt_s: number;
  loop: boolean;
  mode: "once" | "loop" | "pingpong" | null;
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
  | "mixed";

export interface PathInteraction {
  type: "reflection" | "diffraction" | "scattering" | "transmission";
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
  delay_ns: number;
  phase_rad: number;
  aod_deg: number[] | null;
  aoa_deg: number[] | null;
  interactions: PathInteraction[];
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
  metric: "path_gain_db" | "rss_dbm";
  grid: RadioMapGrid;
  values: (number | null)[][];
  warnings: string[];
  metadata: Record<string, unknown>;
}

export interface SimulateRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
}

export interface TrajectorySample {
  time_s: number;
  ue_id: string;
  position: Vec3;
  rss_dbm: number | null;
  path_gain_db: number | null;
  sinr_db: number | null;
  interference_dbm?: number | null;
  rms_delay_spread_ns: number | null;
  path_count: number;
  strongest_delay_ns: number | null;
  paths: RayPath[] | null;
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

export interface TrajectorySimulateRequest {
  config_id?: string | null;
  config?: SimulationConfig | null;
  ue_id?: string | null;
  serving_tx_id?: string | null;
  waypoints?: number[][] | null;
  start_m?: number[] | null;
  end_m?: number[] | null;
  num_points?: number;
  dt_s?: number;
  include_paths?: boolean;
  follow_terrain?: boolean;
  follow_height_m?: number;
}

export interface RFDataExportSummary {
  export_dir: string;
  files: string[];
  has_paths: boolean;
  has_radio_map: boolean;
  has_trajectory: boolean;
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
}

export interface ChannelAnalysisResult {
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

// -------------------------------------------------------------------- ai

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

export interface MaterialSuggestionResponse {
  suggestions: MaterialSuggestion[];
  provider: string;
  model: string | null;
  prompt_version: string | null;
  warnings: string[];
}

export interface SuggestMaterialsRequest {
  prim_ids?: string[] | null;
  provider?: string | null;
  screenshot_data_url?: string | null;
}

export interface SuggestionDecision {
  prim_id: string;
  action: "approve" | "reject" | "edit";
  rf_material_id?: string | null;
}

export interface ApplySuggestionsRequest {
  decisions: SuggestionDecision[];
  suggestions: MaterialSuggestion[];
  provider: string;
  model?: string | null;
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

export interface ProjectCreateRequest {
  name: string;
  project_id?: string | null;
  template?: "empty" | "demo";
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
