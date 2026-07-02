import { create } from "zustand";
import { api, ApiError } from "../api/client";
import type {
  AIProviderStatus,
  AssignRequest,
  CompileResult,
  HealthResponse,
  MaterialSuggestionResponse,
  PathResultSet,
  ProjectInfo,
  RadioMapResultSet,
  RFMaterial,
  RFMaterialLibrary,
  Scene,
  SuggestionDecision,
  ValidationReport,
} from "../types/api";

export type Mode = "visual" | "rf" | "validation" | "ai" | "results";

interface AppState {
  projects: ProjectInfo[];
  projectId: string | null;
  scene: Scene | null;
  materials: RFMaterialLibrary | null;
  health: HealthResponse | null;
  aiStatuses: AIProviderStatus[];
  mode: Mode;
  selection: string[];
  selectedDeviceId: string | null;
  validation: ValidationReport | null;
  compileResult: CompileResult | null;
  suggestions: MaterialSuggestionResponse | null;
  decisions: Record<string, SuggestionDecision>;
  pathResults: PathResultSet | null;
  radioMap: RadioMapResultSet | null;
  selectedPathId: string | null;
  busy: string | null;
  error: string | null;
  notice: string | null;

  init: () => Promise<void>;
  openProject: (projectId: string) => Promise<void>;
  refetchScene: () => Promise<void>;
  setMode: (mode: Mode) => void;
  selectPrim: (primId: string, additive?: boolean) => void;
  selectDevice: (deviceId: string) => void;
  clearSelection: () => void;
  selectPath: (pathId: string | null) => void;
  runValidation: () => Promise<void>;
  compileRF: () => Promise<void>;
  simulatePaths: () => Promise<void>;
  simulateRadioMap: () => Promise<void>;
  assignMaterial: (req: AssignRequest) => Promise<void>;
  saveMaterial: (mat: RFMaterial) => Promise<void>;
  suggestMaterials: () => Promise<void>;
  setDecision: (primId: string, decision: SuggestionDecision | null) => void;
  applyDecisions: () => Promise<void>;
  dismissError: () => void;
  dismissNotice: () => void;
}

export const useAppStore = create<AppState>()((set, get) => {
  /** Run an async action with busy/error bookkeeping. Returns undefined on failure. */
  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
    set({ busy: label, error: null });
    try {
      return await fn();
    } catch (err) {
      set({ error: err instanceof Error ? err.message : String(err) });
      return undefined;
    } finally {
      set({ busy: null });
    }
  }

  /** Re-run validation after scene mutations, but only if the panel already has a report. */
  async function revalidateIfOpen(): Promise<void> {
    const { projectId, validation } = get();
    if (!projectId || validation === null) return;
    try {
      set({ validation: await api.validateScene(projectId) });
    } catch {
      // validation refresh is best-effort; the explicit button surfaces errors
    }
  }

  async function refetchSceneInner(): Promise<void> {
    const pid = get().projectId;
    if (!pid) return;
    set({ scene: await api.getScene(pid) });
  }

  return {
    projects: [],
    projectId: null,
    scene: null,
    materials: null,
    health: null,
    aiStatuses: [],
    mode: "visual",
    selection: [],
    selectedDeviceId: null,
    validation: null,
    compileResult: null,
    suggestions: null,
    decisions: {},
    pathResults: null,
    radioMap: null,
    selectedPathId: null,
    busy: null,
    error: null,
    notice: null,

    init: async () => {
      const projects = await run("Loading projects…", async () => {
        const health = await api.health().catch(() => null);
        const list = await api.listProjects();
        set({ health, projects: list });
        return list;
      });
      if (projects && projects.length > 0) {
        await get().openProject(projects[0].project_id);
      }
    },

    openProject: async (projectId) => {
      await run(`Opening ${projectId}…`, async () => {
        const [scene, materials] = await Promise.all([
          api.getScene(projectId),
          api.getMaterials(projectId),
        ]);
        set({
          projectId,
          scene,
          materials,
          selection: [],
          selectedDeviceId: null,
          validation: null,
          compileResult: null,
          suggestions: null,
          decisions: {},
          pathResults: null,
          radioMap: null,
          selectedPathId: null,
        });
        // Provider statuses: prefer the dedicated endpoint, fall back to health.
        try {
          set({ aiStatuses: await api.aiStatus(projectId) });
        } catch {
          set({ aiStatuses: get().health?.ai_providers ?? [] });
        }
        // Latest stored results; a project without results 404s - that is fine.
        try {
          set({ pathResults: await api.getPathResults(projectId) });
        } catch (err) {
          if (!(err instanceof ApiError && (err.status === 404 || err.status === 501))) throw err;
        }
        try {
          set({ radioMap: await api.getRadioMap(projectId) });
        } catch {
          // radio maps are a nice-to-have; ignore silently
        }
      });
    },

    refetchScene: async () => {
      await run("Refreshing scene…", refetchSceneInner);
    },

    setMode: (mode) => set({ mode }),

    selectPrim: (primId, additive = false) => {
      const { selection } = get();
      let next: string[];
      if (additive) {
        next = selection.includes(primId)
          ? selection.filter((id) => id !== primId)
          : [...selection, primId];
      } else {
        next = [primId];
      }
      set({ selection: next, selectedDeviceId: null });
    },

    selectDevice: (deviceId) => set({ selectedDeviceId: deviceId, selection: [] }),

    clearSelection: () => set({ selection: [], selectedDeviceId: null }),

    selectPath: (pathId) => set({ selectedPathId: pathId }),

    runValidation: async () => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Validating scene…", async () => {
        set({ validation: await api.validateScene(pid) });
      });
    },

    compileRF: async () => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Compiling RF projection…", async () => {
        const result = await api.compileSionna(pid);
        set({ compileResult: result });
        if (result.ok) {
          set({
            notice:
              `RF projection compiled: ${result.generated_files.length} file(s), ` +
              `${result.material_groups.length} material group(s)` +
              (result.warnings.length > 0 ? `, ${result.warnings.length} warning(s)` : ""),
          });
        } else {
          set({
            error:
              "RF compile failed" +
              (result.errors.length > 0 ? `: ${result.errors.join("; ")}` : ""),
          });
        }
      });
    },

    simulatePaths: async () => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Simulating paths…", async () => {
        const result = await api.simulatePaths(pid, {});
        set({
          pathResults: result,
          selectedPathId: null,
          mode: "results",
          notice: `Simulated ${result.paths.length} path(s) via ${result.backend} backend`,
        });
        await refetchSceneInner(); // a ResultSetRef was appended to the scene
      });
    },

    simulateRadioMap: async () => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Simulating radio map…", async () => {
        const result = await api.simulateRadioMap(pid, {});
        set({
          radioMap: result,
          mode: "results",
          notice: `Radio map computed via ${result.backend} backend`,
        });
        await refetchSceneInner();
      });
    },

    assignMaterial: async (req) => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Assigning RF material…", async () => {
        const resp = await api.assign(pid, req);
        await refetchSceneInner();
        const parts = [`Assigned ${req.rf_material_id} to ${resp.updated_prim_ids.length} prim(s)`];
        if (resp.skipped_prim_ids.length > 0) parts.push(`${resp.skipped_prim_ids.length} skipped`);
        if (resp.warnings.length > 0) parts.push(...resp.warnings);
        set({ notice: parts.join(" · ") });
        await revalidateIfOpen();
      });
    },

    saveMaterial: async (mat) => {
      const pid = get().projectId;
      if (!pid) return;
      await run(`Saving material ${mat.id}…`, async () => {
        await api.putMaterial(pid, mat);
        set({ materials: await api.getMaterials(pid), notice: `Saved RF material ${mat.id}` });
        await revalidateIfOpen();
      });
    },

    suggestMaterials: async () => {
      const pid = get().projectId;
      if (!pid) return;
      const { selection } = get();
      await run("Requesting RF material suggestions…", async () => {
        const resp = await api.suggestMaterials(pid, {
          prim_ids: selection.length > 0 ? selection : null,
        });
        set({ suggestions: resp, decisions: {} });
      });
    },

    setDecision: (primId, decision) => {
      const next = { ...get().decisions };
      if (decision === null) {
        delete next[primId];
      } else {
        next[primId] = decision;
      }
      set({ decisions: next });
    },

    applyDecisions: async () => {
      const pid = get().projectId;
      const { decisions, suggestions } = get();
      const list = Object.values(decisions);
      if (!pid || list.length === 0 || !suggestions) return;
      await run("Applying suggestion decisions…", async () => {
        const resp = await api.applySuggestions(pid, {
          decisions: list,
          suggestions: suggestions.suggestions,
          provider: suggestions.provider,
          model: suggestions.model,
        });
        await refetchSceneInner();
        set({
          suggestions: null,
          decisions: {},
          notice:
            `Applied ${list.length} decision(s): ${resp.updated_prim_ids.length} prim(s) updated` +
            (resp.warnings.length > 0 ? ` · ${resp.warnings.join(" · ")}` : ""),
        });
        await revalidateIfOpen();
      });
    },

    dismissError: () => set({ error: null }),
    dismissNotice: () => set({ notice: null }),
  };
});
