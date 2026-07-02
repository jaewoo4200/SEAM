/**
 * Thin typed fetch wrappers for every backend endpoint.
 *
 * All URLs are relative "/api/..." so the Vite dev proxy (and any production
 * reverse proxy) can route them to the FastAPI backend without CORS.
 * Non-2xx responses throw ApiError carrying the backend's `detail` message.
 */

import type {
  AIProviderStatus,
  ApplySuggestionsRequest,
  AssignRequest,
  AssignResponse,
  BatchAssignRequest,
  CompileResult,
  HealthResponse,
  MaterialSuggestionResponse,
  PathResultSet,
  ProjectCreateRequest,
  ProjectInfo,
  RadioMapResultSet,
  RFMaterial,
  RFMaterialLibrary,
  Scene,
  SimulateRequest,
  SuggestMaterialsRequest,
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
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data: unknown = await res.json();
      if (data !== null && typeof data === "object" && "detail" in data) {
        const d = (data as { detail: unknown }).detail;
        detail = typeof d === "string" ? d : JSON.stringify(d);
      }
    } catch {
      // non-JSON error body; keep the status text
    }
    throw new ApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export const api = {
  // health
  health: () => request<HealthResponse>("GET", "/health"),

  // projects
  listProjects: () => request<ProjectInfo[]>("GET", "/projects"),
  createProject: (req: ProjectCreateRequest) => request<ProjectInfo>("POST", "/projects", req),
  getProject: (pid: string) => request<ProjectInfo>("GET", `/projects/${pid}`),

  // scene
  getScene: (pid: string) => request<Scene>("GET", `/projects/${pid}/scene`),
  putScene: (pid: string, scene: Scene) => request<Scene>("PUT", `/projects/${pid}/scene`, scene),
  validateScene: (pid: string) =>
    request<ValidationReport>("POST", `/projects/${pid}/scene/validate`),

  // rf materials + assignment
  getMaterials: (pid: string) => request<RFMaterialLibrary>("GET", `/projects/${pid}/rf/materials`),
  putMaterial: (pid: string, mat: RFMaterial) =>
    request<RFMaterial>("PUT", `/projects/${pid}/rf/materials/${mat.id}`, mat),
  assign: (pid: string, req: AssignRequest) =>
    request<AssignResponse>("POST", `/projects/${pid}/rf/assign`, req),
  batchAssign: (pid: string, req: BatchAssignRequest) =>
    request<AssignResponse>("POST", `/projects/${pid}/rf/batch-assign`, req),

  // ai
  aiStatus: (pid: string) => request<AIProviderStatus[]>("GET", `/projects/${pid}/ai/status`),
  suggestMaterials: (pid: string, req: SuggestMaterialsRequest) =>
    request<MaterialSuggestionResponse>("POST", `/projects/${pid}/ai/suggest-materials`, req),
  applySuggestions: (pid: string, req: ApplySuggestionsRequest) =>
    request<AssignResponse>("POST", `/projects/${pid}/ai/apply-suggestions`, req),

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

  // static project assets (GLB, textures)
  assetUrl: (pid: string, uri: string) => `${BASE}/projects/${pid}/assets/${uri}`,
};
