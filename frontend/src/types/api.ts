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
}

export interface Device {
  id: string;
  name: string;
  kind: "tx" | "rx";
  position: Vec3;
  orientation_deg: Vec3;
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
  frequency_hz: number;
  max_depth: number;
  tx_ids: string[] | null;
  rx_ids: string[] | null;
  los: boolean;
  reflection: boolean;
  diffraction: boolean;
  scattering: boolean;
  num_samples: number;
  radio_map: RadioMapGridConfig;
}

export interface ResultSetRef {
  result_id: string;
  kind: "paths" | "radio_map" | "trajectory";
  backend: string;
  simulation_config_id: string;
  uri: string;
  created_at: string | null;
}

export interface Scene {
  schema_version: string;
  scene_id: string;
  name: string;
  coordinate_system: CoordinateSystem;
  assets: SceneAssets;
  prims: Prim[];
  devices: Device[];
  simulation_configs: SimulationConfig[];
  result_sets: ResultSetRef[];
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
  rms_delay_spread_ns: number | null;
  path_count: number;
  strongest_delay_ns: number | null;
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
  waypoints?: number[][] | null;
  start_m?: number[] | null;
  end_m?: number[] | null;
  num_points?: number;
  dt_s?: number;
}

export interface RFDataExportSummary {
  export_dir: string;
  files: string[];
  has_paths: boolean;
  has_radio_map: boolean;
  has_trajectory: boolean;
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
