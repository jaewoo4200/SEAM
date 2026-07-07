/**
 * Thin typed fetch wrappers for every backend endpoint.
 *
 * All URLs are relative "/api/..." so the Vite dev proxy (and any production
 * reverse proxy) can route them to the FastAPI backend without CORS.
 * Non-2xx responses throw ApiError carrying the backend's `detail` message.
 */

import type {
  AgentApplyRequest,
  AgentApplyResponse,
  AgentStartRequest,
  AgentStartResponse,
  AgentTrace,
  AIModelsResponse,
  AIProviderStatus,
  ApplySuggestionsRequest,
  AssignRequest,
  AssignResponse,
  BackendCapabilities,
  EngineListResponse,
  BatchAssignRequest,
  BeamformingRequest,
  BeamformingResult,
  ChannelAnalysisRequest,
  ChannelAnalysisResult,
  CompileResult,
  DatasetGenerateRequest,
  DatasetInfo,
  DatasetListResponse,
  DisambiguationReport,
  DisambiguationRequest,
  HealthResponse,
  MaterialImpactReport,
  MaterialImpactRequest,
  MaskUploadResponse,
  MaterialSuggestionResponse,
  MeshRadioMapRequest,
  MeshRadioMapResultSet,
  OsmImportRequest,
  OsmImportResponse,
  PathResultSet,
  ProjectCreateRequest,
  ProjectDeleteResponse,
  ProjectInfo,
  RadioMapResultSet,
  ResultsPruneRequest,
  ResultsPruneResponse,
  RFDataExportSummary,
  RFMaterial,
  RFMaterialLibrary,
  ScenarioResultSet,
  ScenarioSimulateRequest,
  Scene,
  SceneBounds,
  SegmentationApplyRequest,
  SegmentationApplyResponse,
  SegmentationJobStart,
  SegmentationJobStatus,
  SegmentationPreviewRequest,
  SegmentationPreviewResponse,
  SegmentationUndoRequest,
  SegmentationUndoResponse,
  SimulateRequest,
  SplitPartsRequest,
  SplitPartsResponse,
  SuggestMaterialsRequest,
  TrajectoryResultSet,
  TrajectorySimulateRequest,
  ValidationReport,
} from "../types/api";

const BASE = "/api";

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

async function parseError(res: Response): Promise<ApiError> {
  let detail = `${res.status} ${res.statusText}`;
  try {
    const data: unknown = await res.json();
    if (data !== null && typeof data === "object" && "detail" in data) {
      const d = (data as { detail: unknown }).detail;
      // Some endpoints (e.g. delete-conflict) return a structured detail with
      // a human message + prim_ids; surface the message when present.
      if (typeof d === "string") detail = d;
      else if (d !== null && typeof d === "object" && "message" in d) {
        detail = String((d as { message: unknown }).message);
      } else detail = JSON.stringify(d);
    }
  } catch {
    // non-JSON error body; keep the status text
  }
  return new ApiError(res.status, detail);
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const init: RequestInit = { method };
  if (body !== undefined) {
    init.body = JSON.stringify(body);
    init.headers = { "Content-Type": "application/json" };
  }
  let res: Response;
  try {
    res = await fetch(BASE + path, init);
  } catch (err) {
    throw new ApiError(0, `backend unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

/** Multipart POST (FormData). Do NOT set Content-Type: the browser adds the
 *  multipart boundary. Used by the scene-import upload. */
async function postForm<T>(path: string, form: FormData): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, { method: "POST", body: form });
  } catch (err) {
    throw new ApiError(0, `backend unreachable: ${err instanceof Error ? err.message : String(err)}`);
  }
  if (!res.ok) throw await parseError(res);
  return (await res.json()) as T;
}

export const api = {
  // health
  health: () => request<HealthResponse>("GET", "/health"),

  // solver backends + their capability bags (GET /api/backends).
  listBackends: () => request<BackendCapabilities[]>("GET", "/backends"),

  // compute engines
  getEngines: (refresh = false) =>
    request<EngineListResponse>("GET", `/engines${refresh ? "?refresh=true" : ""}`),

  // projects
  listProjects: () => request<ProjectInfo[]>("GET", "/projects"),
  createProject: (req: ProjectCreateRequest) => request<ProjectInfo>("POST", "/projects", req),
  getProject: (pid: string) => request<ProjectInfo>("GET", `/projects/${pid}`),
  // Permanently remove a project folder (404 on unknown id).
  deleteProject: (pid: string) =>
    request<ProjectDeleteResponse>("DELETE", `/projects/${pid}`),
  // Import a Mitsuba/Sionna scene XML (+ optional companion mesh files) as a
  // new project. `form` carries: file (the .xml), project_id, name,
  // environment, and zero or more `meshes` file parts.
  importScene: (form: FormData) => postForm<ProjectInfo>("/projects/import", form),
  // One-shot OpenStreetMap import: building footprints in a rectangle around
  // a lat/lon, extruded + RF-preassigned (needs internet for Overpass).
  importOsm: (req: OsmImportRequest) =>
    request<OsmImportResponse>("POST", "/projects/import-osm", req),

  // scene
  getScene: (pid: string) => request<Scene>("GET", `/projects/${pid}/scene`),
  sceneBounds: (pid: string) => request<SceneBounds>("GET", `/projects/${pid}/scene/bounds`),
  putScene: (pid: string, scene: Scene) => request<Scene>("PUT", `/projects/${pid}/scene`, scene),
  validateScene: (pid: string) =>
    request<ValidationReport>("POST", `/projects/${pid}/scene/validate`),

  // rf materials + assignment
  getMaterials: (pid: string) => request<RFMaterialLibrary>("GET", `/projects/${pid}/rf/materials`),
  putMaterial: (pid: string, mat: RFMaterial) =>
    request<RFMaterialLibrary>("PUT", `/projects/${pid}/rf/materials/${mat.id}`, mat),
  deleteMaterial: (pid: string, materialId: string) =>
    request<RFMaterialLibrary>("DELETE", `/projects/${pid}/rf/materials/${materialId}`),
  assign: (pid: string, req: AssignRequest) =>
    request<AssignResponse>("POST", `/projects/${pid}/rf/assign`, req),
  unassign: (pid: string, primIds: string[]) =>
    request<AssignResponse>("POST", `/projects/${pid}/rf/unassign`, { prim_ids: primIds }),
  batchAssign: (pid: string, req: BatchAssignRequest) =>
    request<AssignResponse>("POST", `/projects/${pid}/rf/batch-assign`, req),

  // ai
  aiStatus: (pid: string) => request<AIProviderStatus[]>("GET", `/projects/${pid}/ai/status`),
  // Per-provider selectable models (drives the model picker in the AI panel).
  aiModels: (pid: string) => request<AIModelsResponse>("GET", `/projects/${pid}/ai/models`),
  suggestMaterials: (pid: string, req: SuggestMaterialsRequest) =>
    request<MaterialSuggestionResponse>("POST", `/projects/${pid}/ai/suggest-materials`, req),
  applySuggestions: (pid: string, req: ApplySuggestionsRequest) =>
    request<AssignResponse>("POST", `/projects/${pid}/ai/apply-suggestions`, req),

  // material segmentation (multi-material building split)
  // color_heuristic / user_png answer inline (SegmentationPreviewResponse);
  // vlm_tile_vote returns a {job_id} to poll via segmentationJob.
  previewSegmentation: (pid: string, req: SegmentationPreviewRequest) =>
    request<SegmentationPreviewResponse | SegmentationJobStart>(
      "POST",
      `/projects/${pid}/segmentation/preview`,
      req,
    ),
  segmentationJob: (pid: string, jobId: string) =>
    request<SegmentationJobStatus>("GET", `/projects/${pid}/segmentation/jobs/${jobId}`),
  uploadSegmentationMask: (pid: string, form: FormData) =>
    postForm<MaskUploadResponse>(`/projects/${pid}/segmentation/upload-mask`, form),
  applySegmentation: (pid: string, req: SegmentationApplyRequest) =>
    request<SegmentationApplyResponse>("POST", `/projects/${pid}/segmentation/apply`, req),
  splitParts: (pid: string, req: SplitPartsRequest) =>
    request<SplitPartsResponse>("POST", `/projects/${pid}/segmentation/split-parts`, req),
  undoSegmentation: (pid: string, req: SegmentationUndoRequest) =>
    request<SegmentationUndoResponse>("POST", `/projects/${pid}/segmentation/undo`, req),

  // SEAM-Agent (AI material authoring): start an agentic job over multi-view
  // captures of one prim, poll its live activity trace, then apply the accepted
  // segments (same GLB-rewrite + undo semantics as the material split).
  agentStart: (pid: string, req: AgentStartRequest) =>
    request<AgentStartResponse>("POST", `/projects/${pid}/agent/material-assignment/start`, req),
  agentTrace: (pid: string, jobId: string) =>
    request<AgentTrace>("GET", `/projects/${pid}/agent/material-assignment/${jobId}/trace`),
  agentApply: (pid: string, jobId: string, req: AgentApplyRequest) =>
    request<AgentApplyResponse>(
      "POST",
      `/projects/${pid}/agent/material-assignment/${jobId}/apply`,
      req,
    ),

  // calibration: RF-sensing disambiguation (rank candidate materials by fit).
  disambiguate: (pid: string, req: DisambiguationRequest) =>
    request<DisambiguationReport>("POST", `/projects/${pid}/calibrate/disambiguate`, req),

  // analysis: material-aware vs single-material baseline channel impact.
  materialImpact: (pid: string, req: MaterialImpactRequest) =>
    request<MaterialImpactReport>("POST", `/projects/${pid}/analyze/material-impact`, req),

  // compile
  compileSionna: (pid: string) => request<CompileResult>("POST", `/projects/${pid}/compile/sionna`),

  // simulation + results
  simulatePaths: (pid: string, req: SimulateRequest = {}) =>
    request<PathResultSet>("POST", `/projects/${pid}/simulate/paths`, req),
  simulateRadioMap: (pid: string, req: SimulateRequest = {}) =>
    request<RadioMapResultSet>("POST", `/projects/${pid}/simulate/radio-map`, req),
  getPathResults: (pid: string) => request<PathResultSet>("GET", `/projects/${pid}/results/paths`),
  getRadioMap: (pid: string) =>
    request<RadioMapResultSet>("GET", `/projects/${pid}/results/radio-map`),
  // Prune stored result files: keep the newest `keep_latest` per kind (0 = drop
  // all), `kinds` null = every kind. Returns the removed/kept result uris.
  pruneResults: (pid: string, req: ResultsPruneRequest = {}) =>
    request<ResultsPruneResponse>("POST", `/projects/${pid}/results/prune`, req),

  // mesh radio map: metric draped onto the triangles of selected surfaces.
  simulateMeshRadioMap: (pid: string, req: MeshRadioMapRequest) =>
    request<MeshRadioMapResultSet>("POST", `/projects/${pid}/simulate/mesh-radio-map`, req),
  getMeshRadioMapResult: (pid: string, resultId?: string) =>
    request<MeshRadioMapResultSet>(
      "GET",
      `/projects/${pid}/results/mesh-radio-map${resultId ? `?result_id=${encodeURIComponent(resultId)}` : ""}`,
    ),
  simulateTrajectory: (pid: string, req: TrajectorySimulateRequest) =>
    request<TrajectoryResultSet>("POST", `/projects/${pid}/simulate/trajectory`, req),
  getTrajectory: (pid: string) =>
    request<TrajectoryResultSet>("GET", `/projects/${pid}/results/trajectory`),

  // scenario playback (V2X): actors + devices moved per frame, links + optional paths.
  simulateScenario: (pid: string, req: ScenarioSimulateRequest = {}) =>
    request<ScenarioResultSet>("POST", `/projects/${pid}/simulate/scenario`, req),
  getScenario: (pid: string) =>
    request<ScenarioResultSet>("GET", `/projects/${pid}/results/scenario`),

  // channel analysis: link budget + CIR/CFR + 38.901 path-loss comparison.
  analyzeChannel: (pid: string, req: ChannelAnalysisRequest = {}) =>
    request<ChannelAnalysisResult>("POST", `/projects/${pid}/analyze/channel`, req),

  simulateBeamforming: (pid: string, req: BeamformingRequest = {}) =>
    request<BeamformingResult>("POST", `/projects/${pid}/simulate/beamforming`, req),

  // AODT RFData export
  exportRfdata: (pid: string, req: SimulateRequest = {}) =>
    request<RFDataExportSummary>("POST", `/projects/${pid}/export/rfdata`, req),

  // ML ground-truth datasets: sweep a UE, solve per-position, export arrays.
  generateDataset: (pid: string, req: DatasetGenerateRequest) =>
    request<DatasetInfo>("POST", `/projects/${pid}/datasets/generate`, req),
  listDatasets: (pid: string) =>
    request<DatasetListResponse>("GET", `/projects/${pid}/datasets`),
  datasetFileUrl: (pid: string, datasetId: string, filename: string) =>
    `${BASE}/projects/${pid}/datasets/${datasetId}/files/${filename}`,

  // Mitsuba path-traced render of the RF scene; resolves to an object URL.
  renderScene: async (
    pid: string,
    req: { camera_position: number[]; look_at: number[]; fov_deg?: number; width?: number; height?: number; spp?: number },
  ): Promise<string> => {
    const res = await fetch(`${BASE}/projects/${pid}/render`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(req),
    });
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`;
      try {
        const data = (await res.json()) as { detail?: unknown };
        if (typeof data.detail === "string") detail = data.detail;
      } catch { /* non-JSON */ }
      throw new ApiError(res.status, detail);
    }
    return URL.createObjectURL(await res.blob());
  },

  // static project assets (GLB, textures)
  assetUrl: (pid: string, uri: string) => `${BASE}/projects/${pid}/assets/${uri}`,
};
