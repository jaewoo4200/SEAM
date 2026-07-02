import { create } from "zustand";
import { api, ApiError } from "../api/client";
import { defaultSimConfig, normalizeConfig } from "../simConfig";
import type {
  AIProviderStatus,
  AssignRequest,
  BeamformingResult,
  CompileResult,
  Device,
  HealthResponse,
  MaterialSuggestionResponse,
  PathResultSet,
  PathType,
  ProjectInfo,
  RadioMapResultSet,
  RFMaterial,
  RFMaterialLibrary,
  Scene,
  SimulationConfig,
  SuggestionDecision,
  TrajectoryResultSet,
  ValidationReport,
  Vec3,
} from "../types/api";

export type Mode = "visual" | "rf" | "validation" | "ai" | "results";

/** Compute-target for the two solver panels + auto-update debounce. */
type AutoTarget = "paths" | "radioMap";

/** How the 3D viewer colors ray path polylines. */
export type ColorBy = "type" | "power" | "depth";

const AUTO_DEBOUNCE_MS = 500;

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
  beamforming: BeamformingResult | null;
  selectedPathId: string | null;
  // Result-overlay visibility toggles (Result mode).
  showPaths: boolean;
  showRadioMap: boolean;
  showBeamforming: boolean;

  // --- solver control surface (SolverControls.tsx) ---
  pathsConfig: SimulationConfig;
  radioMapConfig: SimulationConfig;
  autoPaths: boolean;
  autoRadioMap: boolean;
  // Beamforming array sizes (SolverControls GLOBAL / beamforming card).
  bfTxRows: number;
  bfTxCols: number;
  bfRxRows: number;
  bfRxCols: number;

  // --- viewer ray distinction + filtering (store-driven, shared by table) ---
  pathTypeFilter: PathType | "all";
  strongestN: number;
  minPowerDbm: number | null;
  colorBy: ColorBy;
  lineWidthByPower: boolean;

  // --- trajectory playback ---
  trajectory: TrajectoryResultSet | null;
  trajFrame: number;
  trajPlaying: boolean;
  trajSpeed: number;

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
  toggleOverlay: (kind: "paths" | "radioMap" | "beamforming") => void;
  runValidation: () => Promise<void>;
  compileRF: () => Promise<void>;
  simulatePaths: () => Promise<void>;
  simulateRadioMap: () => Promise<void>;
  removePaths: () => void;
  removeRadioMap: () => void;
  exportRfdata: () => Promise<void>;
  runBeamforming: () => Promise<void>;
  assignMaterial: (req: AssignRequest) => Promise<void>;
  saveMaterial: (mat: RFMaterial) => Promise<void>;
  suggestMaterials: () => Promise<void>;
  setDecision: (primId: string, decision: SuggestionDecision | null) => void;
  applyDecisions: () => Promise<void>;

  // solver config
  setPathsConfig: (patch: Partial<SimulationConfig>) => void;
  setRadioMapConfig: (patch: Partial<SimulationConfig>) => void;
  setAuto: (target: AutoTarget, on: boolean) => void;
  saveProjectDefault: () => Promise<void>;
  setBeamArray: (patch: Partial<
    Pick<AppState, "bfTxRows" | "bfTxCols" | "bfRxRows" | "bfRxCols">
  >) => void;

  // device editing
  updateDevice: (deviceId: string, patch: Partial<Device>) => Promise<void>;
  addDevice: (kind: "tx" | "rx") => Promise<void>;
  deleteDevice: (deviceId: string) => Promise<void>;
  clearDevices: () => Promise<void>;

  // viewer filters
  setPathTypeFilter: (f: PathType | "all") => void;
  setStrongestN: (n: number) => void;
  setMinPowerDbm: (p: number | null) => void;
  setColorBy: (c: ColorBy) => void;
  setLineWidthByPower: (on: boolean) => void;

  // trajectory
  simulateTrajectory: (params: {
    start_m: Vec3;
    end_m: Vec3;
    num_points: number;
    dt_s: number;
    ue_id?: string | null;
  }) => Promise<void>;
  setTrajFrame: (frame: number) => void;
  setTrajPlaying: (playing: boolean) => void;
  setTrajSpeed: (speed: number) => void;

  dismissError: () => void;
  dismissNotice: () => void;
}

export const useAppStore = create<AppState>()((set, get) => {
  /** Run an async action with busy/error bookkeeping. Returns undefined on failure. */
  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
    // Clear the previous notice too: dismissing a later error must not
    // resurrect a stale success toast from an unrelated earlier action.
    set({ busy: label, error: null, notice: null });
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

  /** Beamforming is only valid for the current geometry/materials; any scene
   *  edit invalidates it (guide item 5). */
  function invalidateBeamforming(): void {
    if (get().beamforming !== null) set({ beamforming: null });
  }

  // --- auto-update: debounced, non-overlapping recompute per target ---
  let pathsTimer: ReturnType<typeof setTimeout> | null = null;
  let radioTimer: ReturnType<typeof setTimeout> | null = null;

  function scheduleAuto(target: AutoTarget): void {
    if (target === "paths") {
      if (!get().autoPaths) return;
      if (pathsTimer) clearTimeout(pathsTimer);
      pathsTimer = setTimeout(() => {
        pathsTimer = null;
        // Never overlap: if an action is mid-flight, re-arm the timer so the
        // recompute lands once the app is idle rather than being dropped.
        if (get().busy !== null) {
          scheduleAuto("paths");
          return;
        }
        void get().simulatePaths();
      }, AUTO_DEBOUNCE_MS);
    } else {
      if (!get().autoRadioMap) return;
      if (radioTimer) clearTimeout(radioTimer);
      radioTimer = setTimeout(() => {
        radioTimer = null;
        if (get().busy !== null) {
          scheduleAuto("radioMap");
          return;
        }
        void get().simulateRadioMap();
      }, AUTO_DEBOUNCE_MS);
    }
  }

  /** After a device/material edit: invalidate beamforming and fire any auto
   *  recomputes that are enabled. */
  function afterSceneEdit(): void {
    invalidateBeamforming();
    scheduleAuto("paths");
    scheduleAuto("radioMap");
  }

  /** PUT a mutated scene, refresh local copy, then run edit side-effects. */
  async function putSceneAndRefresh(scene: Scene): Promise<void> {
    const pid = get().projectId;
    if (!pid) return;
    set({ scene: await api.putScene(pid, scene) });
    await revalidateIfOpen();
  }

  /** Next free zero-padded device id for a kind, e.g. tx_003 / rx_001. */
  function nextDeviceId(kind: "tx" | "rx"): string {
    const ids = new Set((get().scene?.devices ?? []).map((d) => d.id));
    for (let n = 1; n < 1000; n++) {
      const id = `${kind}_${String(n).padStart(3, "0")}`;
      if (!ids.has(id)) return id;
    }
    return `${kind}_${Date.now()}`;
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
    beamforming: null,
    selectedPathId: null,
    showPaths: true,
    showRadioMap: true,
    showBeamforming: true,

    pathsConfig: defaultSimConfig(),
    radioMapConfig: defaultSimConfig(),
    autoPaths: false,
    autoRadioMap: false,
    bfTxRows: 4,
    bfTxCols: 4,
    bfRxRows: 4,
    bfRxCols: 4,

    pathTypeFilter: "all",
    strongestN: 50,
    minPowerDbm: null,
    colorBy: "type",
    lineWidthByPower: false,

    trajectory: null,
    trajFrame: 0,
    trajPlaying: false,
    trajSpeed: 1,

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
        // Seed both solver panels from the project's stored default config,
        // filling any missing (older-scene) fields with backend defaults.
        const stored = scene.simulation_configs[0];
        const seed = normalizeConfig(stored);
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
          beamforming: null,
          selectedPathId: null,
          // Overlay visibility starts fresh per project.
          showPaths: true,
          showRadioMap: true,
          showBeamforming: true,
          pathsConfig: seed,
          radioMapConfig: { ...seed },
          // Auto-update is opt-in; reset per project.
          autoPaths: false,
          autoRadioMap: false,
          // Trajectory playback state resets per project.
          trajectory: null,
          trajFrame: 0,
          trajPlaying: false,
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
        // Latest stored trajectory (404-tolerant, guide item 4).
        try {
          set({ trajectory: await api.getTrajectory(projectId), trajFrame: 0 });
        } catch {
          // no trajectory yet; ignore silently
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

    toggleOverlay: (kind) => {
      if (kind === "paths") set({ showPaths: !get().showPaths });
      else if (kind === "radioMap") set({ showRadioMap: !get().showRadioMap });
      else set({ showBeamforming: !get().showBeamforming });
    },

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
        // Send the paths panel config inline so every solver knob applies.
        const result = await api.simulatePaths(pid, { config: get().pathsConfig });
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
        const result = await api.simulateRadioMap(pid, { config: get().radioMapConfig });
        set({
          radioMap: result,
          mode: "results",
          notice: `Radio map computed via ${result.backend} backend`,
        });
        await refetchSceneInner();
      });
    },

    removePaths: () => set({ pathResults: null, selectedPathId: null }),
    removeRadioMap: () => set({ radioMap: null }),

    exportRfdata: async () => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Exporting RFData…", async () => {
        const summary = await api.exportRfdata(pid, {});
        set({
          notice: `Exported ${summary.files.length} RFData files to ${summary.export_dir}`,
        });
      });
    },

    runBeamforming: async () => {
      const pid = get().projectId;
      if (!pid) return;
      const { bfTxRows, bfTxCols, bfRxRows, bfRxCols, pathsConfig } = get();
      await run("Computing beamforming…", async () => {
        const r = await api.simulateBeamforming(pid, {
          config: pathsConfig,
          tx_rows: bfTxRows,
          tx_cols: bfTxCols,
          rx_rows: bfRxRows,
          rx_cols: bfRxCols,
        });
        const fmt = (v: number | null) => (v === null ? "n/a" : `${v.toFixed(1)} dB`);
        const parts = [
          `Beamforming ${r.tx_array[0]}x${r.tx_array[1]}→${r.rx_array[0]}x${r.rx_array[1]} (${r.backend})`,
          `TX-MRT ${fmt(r.tx_mrt_gain_db)}`,
          `SVD ${fmt(r.svd_gain_db)}`,
        ];
        if (r.warnings.length) parts.push(r.warnings[0]);
        set({ beamforming: r, showBeamforming: true, mode: "results", notice: parts.join(" · ") });
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
      afterSceneEdit();
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
      afterSceneEdit();
    },

    // ---------------------------------------------------- solver config

    setPathsConfig: (patch) => set({ pathsConfig: { ...get().pathsConfig, ...patch } }),
    setRadioMapConfig: (patch) => set({ radioMapConfig: { ...get().radioMapConfig, ...patch } }),

    setAuto: (target, on) => {
      if (target === "paths") {
        set({ autoPaths: on });
        if (on) scheduleAuto("paths");
      } else {
        set({ autoRadioMap: on });
        if (on) scheduleAuto("radioMap");
      }
    },

    saveProjectDefault: async () => {
      const { projectId, scene, pathsConfig } = get();
      if (!projectId || !scene) return;
      await run("Saving project default config…", async () => {
        // Write pathsConfig into simulation_configs[0], keeping its id/name so
        // downstream config_id references stay valid.
        const existing = scene.simulation_configs[0];
        const merged: SimulationConfig = {
          ...pathsConfig,
          id: existing?.id ?? pathsConfig.id,
          name: existing?.name ?? pathsConfig.name,
        };
        const configs = scene.simulation_configs.length > 0
          ? [merged, ...scene.simulation_configs.slice(1)]
          : [merged];
        const next: Scene = { ...scene, simulation_configs: configs };
        set({ scene: await api.putScene(projectId, next), notice: "Saved as project default config" });
      });
    },

    setBeamArray: (patch) => set(patch),

    // ---------------------------------------------------- device editing

    updateDevice: async (deviceId, patch) => {
      const scene = get().scene;
      if (!scene) return;
      const devices = scene.devices.map((d) => (d.id === deviceId ? { ...d, ...patch } : d));
      await run("Updating device…", async () => {
        await putSceneAndRefresh({ ...scene, devices });
        set({ notice: `Updated ${deviceId}` });
      });
      afterSceneEdit();
    },

    addDevice: async (kind) => {
      const scene = get().scene;
      if (!scene) return;
      const id = nextDeviceId(kind);
      const isTx = kind === "tx";
      const device: Device = {
        id,
        name: isTx ? "Transmitter" : "Receiver",
        kind,
        position: isTx ? [0, 0, 10] : [10, 0, 1.5],
        orientation_deg: [0, 0, 0],
        power_dbm: 30,
        antenna: {
          pattern: isTx ? "tr38901" : "iso",
          polarization: isTx ? "V" : "cross",
          num_rows: 1,
          num_cols: 1,
        },
        color: isTx ? "#ff0000" : "#2e9bff",
      };
      await run(`Adding ${id}…`, async () => {
        await putSceneAndRefresh({ ...scene, devices: [...scene.devices, device] });
        set({ selectedDeviceId: id, selection: [], notice: `Added ${id}` });
      });
      afterSceneEdit();
    },

    deleteDevice: async (deviceId) => {
      const scene = get().scene;
      if (!scene) return;
      const devices = scene.devices.filter((d) => d.id !== deviceId);
      await run(`Removing ${deviceId}…`, async () => {
        await putSceneAndRefresh({ ...scene, devices });
        if (get().selectedDeviceId === deviceId) set({ selectedDeviceId: null });
        set({ notice: `Removed ${deviceId}` });
      });
      afterSceneEdit();
    },

    clearDevices: async () => {
      const scene = get().scene;
      if (!scene || scene.devices.length === 0) return;
      await run("Clearing radio devices…", async () => {
        await putSceneAndRefresh({ ...scene, devices: [] });
        set({ selectedDeviceId: null, notice: "Cleared all radio devices" });
      });
      afterSceneEdit();
    },

    // ---------------------------------------------------- viewer filters

    setPathTypeFilter: (f) => set({ pathTypeFilter: f }),
    setStrongestN: (n) => set({ strongestN: n }),
    setMinPowerDbm: (p) => set({ minPowerDbm: p }),
    setColorBy: (c) => set({ colorBy: c }),
    setLineWidthByPower: (on) => set({ lineWidthByPower: on }),

    // ---------------------------------------------------- trajectory

    simulateTrajectory: async ({ start_m, end_m, num_points, dt_s, ue_id }) => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Simulating trajectory…", async () => {
        const result = await api.simulateTrajectory(pid, {
          config: get().pathsConfig,
          ue_id: ue_id ?? null,
          start_m,
          end_m,
          num_points,
          dt_s,
        });
        set({
          trajectory: result,
          trajFrame: 0,
          trajPlaying: false,
          mode: "results",
          notice: `Trajectory: ${result.samples.length} sample(s) via ${result.backend} backend`,
        });
        await refetchSceneInner();
      });
    },

    setTrajFrame: (frame) => {
      const traj = get().trajectory;
      const max = traj ? traj.samples.length - 1 : 0;
      set({ trajFrame: Math.max(0, Math.min(max, frame)) });
    },
    setTrajPlaying: (playing) => set({ trajPlaying: playing }),
    setTrajSpeed: (speed) => set({ trajSpeed: speed }),

    dismissError: () => set({ error: null }),
    dismissNotice: () => set({ notice: null }),
  };
});
