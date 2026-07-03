import { create } from "zustand";
import { api, ApiError } from "../api/client";
import { defaultSimConfig, normalizeConfig } from "../simConfig";
import {
  ENV_RADIOMAP_DEPTH,
  presetForEnvironment,
  resolveEnvironment,
} from "../envPresets";
import type { ResolvedEnvironment } from "../envPresets";
import type {
  Actor,
  ActorKind,
  AIProviderStatus,
  AssignRequest,
  BeamformingMode,
  BeamformingResult,
  ChannelAnalysisResult,
  CompileResult,
  Device,
  EngineInfo,
  Environment,
  HealthResponse,
  MaterialSuggestionResponse,
  PathResultSet,
  PathType,
  ProjectInfo,
  RadioMapResultSet,
  RFMaterial,
  RFMaterialLibrary,
  ScenarioResultSet,
  Scene,
  SimulationConfig,
  SuggestionDecision,
  TrajectoryResultSet,
  ValidationReport,
  Vec3,
} from "../types/api";
import { ACTOR_DEFAULTS } from "../actorDefaults";
import {
  defaultViewportSettings,
  loadViewportSettings,
  saveViewportSettings,
} from "../viewportSettings";
import type { ViewportSettings } from "../viewportSettings";
import { CONFIG_PRESETS } from "../configPresets";
import type { ConfigPresetId } from "../configPresets";

export type Mode = "visual" | "rf" | "validation" | "ai" | "results";

/** Compute-targets that support debounced auto-update on scene changes. */
type AutoTarget = "paths" | "radioMap" | "beamforming" | "channel";

/** How the 3D viewer colors ray path polylines. */
export type ColorBy = "type" | "power" | "depth";

const AUTO_DEBOUNCE_MS = 500;

interface AppState {
  projects: ProjectInfo[];
  projectId: string | null;
  scene: Scene | null;
  /** scene.environment resolved to a concrete indoor/outdoor (auto → inferred).
   *  Exposed so Viewer3D can pick a camera/marker scale without re-inferring. */
  resolvedEnvironment: ResolvedEnvironment;
  materials: RFMaterialLibrary | null;
  health: HealthResponse | null;
  aiStatuses: AIProviderStatus[];
  // Installed compute engines (builtin + alternate sionna-rt venvs).
  engines: EngineInfo[];
  mode: Mode;
  selection: string[];
  selectedDeviceId: string | null;
  selectedActorId: string | null;
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
  autoBeamforming: boolean;
  // Auto-rerun of the last channel analysis (needs one manual run first to
  // establish the TX/RX pair).
  autoChannel: boolean;
  // Beamforming array sizes (SolverControls GLOBAL / beamforming card).
  bfTxRows: number;
  bfTxCols: number;
  bfRxRows: number;
  bfRxCols: number;
  // Beamforming mode + codebook sweep params (contract: codebook_sweep default).
  bfMode: BeamformingMode;
  bfSweepStartDeg: number;
  bfSweepStopDeg: number;
  bfSweepStepDeg: number;

  // --- viewport lighting/helpers (per-project, localStorage-persisted) ---
  viewport: ViewportSettings;

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
  trajLoop: boolean;

  // --- scenario playback (V2X) ---
  scenario: ScenarioResultSet | null;
  scenarioFrame: number;
  scenarioPlaying: boolean;
  scenarioSpeed: number;
  scenarioLoop: boolean;

  // --- channel analysis ---
  channelResult: ChannelAnalysisResult | null;

  // --- live sync + AI screenshot groundwork ---
  liveMode: boolean;
  /** Latest viewport JPEG data URL (captured on demand from Viewer3D). Kept in
   *  the store as VLM groundwork; not yet wired to a request (contract gap). */
  lastViewportShot: string | null;
  /** When ON, the AI suggest-materials request *would* attach the viewport;
   *  currently blocked by the StrictModel contract (see reported gap). */
  sendScreenshot: boolean;
  // Forced AI provider for suggestions; null = server picks the best available.
  aiProvider: string | null;

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

  // environment mode (Toolbar Environment select)
  setEnvironment: (environment: Environment) => Promise<void>;

  // solver config
  setPathsConfig: (patch: Partial<SimulationConfig>) => void;
  setRadioMapConfig: (patch: Partial<SimulationConfig>) => void;
  setAuto: (target: AutoTarget, on: boolean) => void;
  saveProjectDefault: () => Promise<void>;
  setBeamArray: (patch: Partial<
    Pick<AppState, "bfTxRows" | "bfTxCols" | "bfRxRows" | "bfRxCols">
  >) => void;
  setBeamforming: (patch: Partial<
    Pick<AppState, "bfMode" | "bfSweepStartDeg" | "bfSweepStopDeg" | "bfSweepStepDeg">
  >) => void;

  // config presets (SolverControls Preset dropdown)
  applyConfigPreset: (id: ConfigPresetId) => void;

  // viewport lighting/helpers
  setViewport: (patch: Partial<ViewportSettings>) => void;
  resetViewport: () => void;

  // device editing
  updateDevice: (deviceId: string, patch: Partial<Device>) => Promise<void>;
  addDevice: (kind: "tx" | "rx", position?: Vec3) => Promise<void>;
  deleteDevice: (deviceId: string) => Promise<void>;
  clearDevices: () => Promise<void>;

  // actor editing
  addActor: (kind: ActorKind) => Promise<void>;
  updateActor: (actorId: string, patch: Partial<Actor>) => Promise<void>;
  deleteActor: (actorId: string) => Promise<void>;
  selectActor: (actorId: string) => void;

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
  setTrajLoop: (loop: boolean) => void;

  // scenario playback
  simulateScenario: (params: {
    num_frames: number;
    dt_s: number;
    include_paths: boolean;
  }) => Promise<void>;
  setScenarioFrame: (frame: number) => void;
  setScenarioPlaying: (playing: boolean) => void;
  setScenarioSpeed: (speed: number) => void;
  setScenarioLoop: (loop: boolean) => void;

  // channel analysis
  analyzeChannel: (txId: string, rxId: string, numCfrPoints?: number) => Promise<void>;
  clearChannel: () => void;

  // live sync + AI screenshot groundwork
  setLiveMode: (on: boolean) => void;
  setSendScreenshot: (on: boolean) => void;
  setAiProvider: (name: string | null) => void;
  /** Viewer3D registers a canvas snapshot fn here; store calls it on demand. */
  registerViewportCapture: (fn: (() => string | null) | null) => void;
  captureViewport: () => string | null;

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

  /** Defensive: older/deployed backends may omit `actors` (and, in principle,
   *  devices/prims) or send `environment: null` from GET/PUT scene even though
   *  the pinned type marks them present. Coalesce the list fields to [] and a
   *  null environment to "auto" so every consumer can safely map over them and
   *  resolveEnvironment never yields undefined. Idempotent and cheap. */
  function normalizeScene(scene: Scene): Scene {
    const env = (scene.environment ?? "auto") as Environment;
    if (scene.actors && scene.devices && scene.prims && scene.environment) return scene;
    return {
      ...scene,
      environment: env,
      prims: scene.prims ?? [],
      devices: scene.devices ?? [],
      actors: scene.actors ?? [],
    };
  }

  async function refetchSceneInner(): Promise<void> {
    const pid = get().projectId;
    if (!pid) return;
    set({ scene: normalizeScene(await api.getScene(pid)) });
  }

  /** Beamforming is only valid for the current geometry/materials; any scene
   *  edit invalidates it (guide item 5). */
  function invalidateBeamforming(): void {
    if (get().beamforming !== null) set({ beamforming: null });
  }

  // --- auto-update: debounced, non-overlapping recompute per target ---
  const autoTimers: Partial<Record<AutoTarget, ReturnType<typeof setTimeout>>> = {};
  // The last channel analysis the user ran; auto-update re-runs this pair.
  let lastChannelArgs: { txId: string; rxId: string; numCfrPoints?: number } | null = null;

  function autoEnabled(target: AutoTarget): boolean {
    switch (target) {
      case "paths":
        return get().autoPaths;
      case "radioMap":
        return get().autoRadioMap;
      case "beamforming":
        return get().autoBeamforming;
      case "channel":
        return get().autoChannel && lastChannelArgs !== null;
    }
  }

  function autoRun(target: AutoTarget): void {
    switch (target) {
      case "paths":
        void get().simulatePaths();
        return;
      case "radioMap":
        void get().simulateRadioMap();
        return;
      case "beamforming":
        void get().runBeamforming();
        return;
      case "channel": {
        const a = lastChannelArgs;
        if (a) void get().analyzeChannel(a.txId, a.rxId, a.numCfrPoints);
        return;
      }
    }
  }

  function scheduleAuto(target: AutoTarget): void {
    if (!autoEnabled(target)) return;
    const pending = autoTimers[target];
    if (pending) clearTimeout(pending);
    autoTimers[target] = setTimeout(() => {
      delete autoTimers[target];
      // Never overlap: if an action is mid-flight, re-arm the timer so the
      // recompute lands once the app is idle rather than being dropped. The
      // shared busy gate also serializes the targets against each other.
      if (get().busy !== null) {
        scheduleAuto(target);
        return;
      }
      if (!autoEnabled(target)) return;
      autoRun(target);
    }, AUTO_DEBOUNCE_MS);
  }

  function setLastChannelArgs(args: typeof lastChannelArgs): void {
    lastChannelArgs = args;
  }

  /** After any scene change (device/actor/material edit, or a live-sync move):
   *  invalidate beamforming (stale for the new geometry), keep the
   *  auto-inferred environment fresh (device spread may have changed), and
   *  fire every auto recompute that is enabled. */
  function afterSceneEdit(): void {
    invalidateBeamforming();
    refreshResolvedEnv();
    scheduleAuto("paths");
    scheduleAuto("radioMap");
    scheduleAuto("beamforming");
    scheduleAuto("channel");
  }

  /** PUT a mutated scene, refresh local copy, then run edit side-effects. */
  async function putSceneAndRefresh(scene: Scene): Promise<void> {
    const pid = get().projectId;
    if (!pid) return;
    set({ scene: normalizeScene(await api.putScene(pid, scene)) });
    await revalidateIfOpen();
  }

  /** Recompute resolvedEnvironment from the current scene (cheap; call after
   *  scene loads or the environment changes). */
  function refreshResolvedEnv(): void {
    const scene = get().scene;
    if (!scene) return;
    set({ resolvedEnvironment: resolveEnvironment(scene.environment, scene) });
  }

  /** Apply an environment preset onto the two solver configs. Session-only:
   *  this patches pathsConfig/radioMapConfig in the store, it does NOT persist
   *  a project default (the user still Saves that explicitly). */
  function applyEnvPreset(environment: Environment): void {
    const scene = get().scene;
    const resolved: ResolvedEnvironment = resolveEnvironment(environment, scene);
    const preset = presetForEnvironment(environment, scene);
    const { pathsConfig, radioMapConfig } = get();
    set({
      pathsConfig: { ...pathsConfig, ...preset.paths },
      radioMapConfig: {
        ...radioMapConfig,
        ...preset.paths,
        max_depth: ENV_RADIOMAP_DEPTH[resolved],
        radio_map: { ...radioMapConfig.radio_map, ...preset.radioMap },
      },
    });
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

  /** Next free actor id for a kind: car_001 / human_001 / obj_001 (custom). */
  function nextActorId(kind: ActorKind): string {
    const prefix = kind === "car" ? "car" : kind === "human" ? "human" : "obj";
    const ids = new Set((get().scene?.actors ?? []).map((a) => a.id));
    for (let n = 1; n < 1000; n++) {
      const id = `${prefix}_${String(n).padStart(3, "0")}`;
      if (!ids.has(id)) return id;
    }
    return `${prefix}_${Date.now()}`;
  }

  /** Scene center from device + prim extents; a new actor drops here. */
  function sceneCenter(): Vec3 {
    const scene = get().scene;
    const pts: Vec3[] = [];
    for (const d of scene?.devices ?? []) pts.push(d.position);
    for (const p of scene?.prims ?? []) {
      if (p.type === "mesh_primitive") pts.push(p.transform.translation);
    }
    if (pts.length === 0) return [0, 0, 0];
    const c: Vec3 = [0, 0, 0];
    for (const p of pts) {
      c[0] += p[0];
      c[1] += p[1];
      c[2] += p[2];
    }
    // Actors sit on the ground plane (z = base contact), so drop z to 0.
    return [c[0] / pts.length, c[1] / pts.length, 0];
  }

  // --- live sync: poll GET /scene every 2s, refresh device/actor positions ---
  let liveTimer: ReturnType<typeof setInterval> | null = null;

  function stopLivePoll(): void {
    if (liveTimer) {
      clearInterval(liveTimer);
      liveTimer = null;
    }
  }

  function startLivePoll(): void {
    stopLivePoll();
    liveTimer = setInterval(() => {
      const { projectId, liveMode } = get();
      if (!projectId || !liveMode) {
        stopLivePoll();
        return;
      }
      // Silent refresh (no busy spinner): pull the latest scene and merge only
      // device/actor positions so an in-flight edit form is not clobbered.
      void api
        .getScene(projectId)
        .then((raw) => {
          const fresh = normalizeScene(raw);
          const cur = get().scene;
          if (!cur || !get().liveMode) return;
          const devPos = new Map(fresh.devices.map((d) => [d.id, d.position]));
          const actPos = new Map(
            fresh.actors.map((a) => [a.id, { position: a.position, orientation_deg: a.orientation_deg }]),
          );
          const vecEq = (a: number[] | null | undefined, b: number[] | null | undefined) => {
            const x = a ?? [0, 0, 0];
            const y = b ?? [0, 0, 0];
            return x.length === y.length && x.every((v, i) => Math.abs(v - y[i]) < 1e-9);
          };
          const moved =
            cur.devices.some((d) => devPos.has(d.id) && !vecEq(devPos.get(d.id), d.position)) ||
            cur.actors.some((a) => {
              const p = actPos.get(a.id);
              return (
                p !== undefined &&
                (!vecEq(p.position, a.position) || !vecEq(p.orientation_deg, a.orientation_deg))
              );
            });
          const devices = cur.devices.map((d) =>
            devPos.has(d.id) ? { ...d, position: devPos.get(d.id)! } : d,
          );
          const actors = cur.actors.map((a) => {
            const p = actPos.get(a.id);
            return p ? { ...a, position: p.position, orientation_deg: p.orientation_deg } : a;
          });
          set({ scene: { ...cur, devices, actors } });
          // A real position/orientation change from the outside world is a
          // scene edit like any other: stale results invalidate and every
          // enabled auto target recomputes (closed-loop live mode). The
          // debounce coalesces consecutive poll deltas while something moves.
          if (moved) afterSceneEdit();
        })
        .catch(() => {
          // transient poll failure: keep the last good scene, try again next tick
        });
    }, 2000);
  }

  // Viewport screenshot capture fn, registered by Viewer3D (VLM groundwork).
  let viewportCapture: (() => string | null) | null = null;

  return {
    projects: [],
    projectId: null,
    scene: null,
    resolvedEnvironment: "outdoor",
    materials: null,
    health: null,
    aiStatuses: [],
    engines: [],
    mode: "visual",
    selection: [],
    selectedDeviceId: null,
    selectedActorId: null,
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
    autoBeamforming: false,
    autoChannel: false,
    bfTxRows: 4,
    bfTxCols: 4,
    bfRxRows: 4,
    bfRxCols: 4,
    bfMode: "codebook_sweep",
    bfSweepStartDeg: -60,
    bfSweepStopDeg: 60,
    bfSweepStepDeg: 10,

    viewport: defaultViewportSettings(),

    pathTypeFilter: "all",
    strongestN: 50,
    minPowerDbm: null,
    colorBy: "type",
    lineWidthByPower: false,

    trajectory: null,
    trajFrame: 0,
    trajPlaying: false,
    trajSpeed: 1,
    trajLoop: false,

    scenario: null,
    scenarioFrame: 0,
    scenarioPlaying: false,
    scenarioSpeed: 1,
    scenarioLoop: false,

    channelResult: null,

    liveMode: false,
    lastViewportShot: null,
    sendScreenshot: false,
    aiProvider: null,

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
      // Engine registry loads out-of-band: probing alternate venvs can take
      // tens of seconds (cold sionna.rt import) and must not block boot.
      void api
        .getEngines()
        .then((r) => set({ engines: r.engines }))
        .catch(() => set({ engines: [] }));
      if (projects && projects.length > 0) {
        await get().openProject(projects[0].project_id);
      }
    },

    openProject: async (projectId) => {
      await run(`Opening ${projectId}…`, async () => {
        const [rawScene, materials] = await Promise.all([
          api.getScene(projectId),
          api.getMaterials(projectId),
        ]);
        const scene = normalizeScene(rawScene);
        // Seed both solver panels from the project's stored default config,
        // filling any missing (older-scene) fields with backend defaults.
        const stored = scene.simulation_configs[0];
        const seed = normalizeConfig(stored);
        set({
          projectId,
          scene,
          resolvedEnvironment: resolveEnvironment(scene.environment, scene),
          materials,
          selection: [],
          selectedDeviceId: null,
          selectedActorId: null,
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
          autoBeamforming: false,
          autoChannel: false,
          // Trajectory playback state resets per project.
          trajectory: null,
          trajFrame: 0,
          trajPlaying: false,
          trajLoop: false,
          // Scenario/channel state resets per project.
          scenario: null,
          scenarioFrame: 0,
          scenarioPlaying: false,
          scenarioLoop: false,
          channelResult: null,
          // Live sync is opt-in and reset per project (stops any prior poll).
          liveMode: false,
          // Viewport lighting/helpers are per-project (localStorage-backed).
          viewport: loadViewportSettings(projectId),
        });
        // Switching projects must stop a running live poll from the old one,
        // and the remembered channel-analysis pair belongs to the old scene.
        stopLivePoll();
        setLastChannelArgs(null);
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
        // Latest stored scenario (404-tolerant: endpoint may 404/501 or be absent).
        try {
          set({ scenario: await api.getScenario(projectId), scenarioFrame: 0 });
        } catch {
          // no scenario yet; ignore silently
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
      set({ selection: next, selectedDeviceId: null, selectedActorId: null });
    },

    selectDevice: (deviceId) =>
      set({ selectedDeviceId: deviceId, selection: [], selectedActorId: null }),

    selectActor: (actorId) =>
      set({ selectedActorId: actorId, selection: [], selectedDeviceId: null }),

    clearSelection: () =>
      set({ selection: [], selectedDeviceId: null, selectedActorId: null }),

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
      const {
        bfTxRows,
        bfTxCols,
        bfRxRows,
        bfRxCols,
        bfMode,
        bfSweepStartDeg,
        bfSweepStopDeg,
        bfSweepStepDeg,
        pathsConfig,
      } = get();
      await run("Computing beamforming…", async () => {
        const r = await api.simulateBeamforming(pid, {
          config: pathsConfig,
          tx_rows: bfTxRows,
          tx_cols: bfTxCols,
          rx_rows: bfRxRows,
          rx_cols: bfRxCols,
          mode: bfMode,
          // Sweep params only meaningful for codebook_sweep, but harmless to
          // send for the analytic modes (the backend ignores them there).
          sweep_start_deg: bfSweepStartDeg,
          sweep_stop_deg: bfSweepStopDeg,
          sweep_step_deg: bfSweepStepDeg,
        });
        const fmt = (v: number | null) => (v === null ? "n/a" : `${v.toFixed(1)} dB`);
        const parts = [
          `Beamforming ${r.tx_array[0]}x${r.tx_array[1]}→${r.rx_array[0]}x${r.rx_array[1]} (${r.backend})`,
        ];
        if (r.mode === "codebook_sweep") {
          parts.push(`codebook ${fmt(r.codebook_gain_db)}`);
          if (r.best_tx_angle_deg !== null && r.best_rx_angle_deg !== null) {
            parts.push(`best TX ${r.best_tx_angle_deg.toFixed(0)}° / RX ${r.best_rx_angle_deg.toFixed(0)}°`);
          }
        } else if (r.mode === "svd") {
          parts.push(`SVD ${fmt(r.svd_gain_db)}`);
        } else {
          parts.push(`TX-MRT ${fmt(r.tx_mrt_gain_db)}`);
        }
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

    setAiProvider: (name) => set({ aiProvider: name }),

    suggestMaterials: async () => {
      const pid = get().projectId;
      if (!pid) return;
      const { selection, sendScreenshot, aiProvider } = get();
      // VLM: when the user opts in, capture the viewport as a downscaled JPEG
      // and attach it to the request. The pinned SuggestMaterialsRequest now
      // carries screenshot_data_url, so the VLM provider can see the scene.
      const shot = sendScreenshot ? get().captureViewport() : null;
      await run("Requesting RF material suggestions…", async () => {
        const resp = await api.suggestMaterials(pid, {
          prim_ids: selection.length > 0 ? selection : null,
          // null = server picks the best available provider.
          provider: aiProvider,
          screenshot_data_url: shot,
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

    // ---------------------------------------------------- environment

    setEnvironment: async (environment) => {
      const { projectId, scene } = get();
      if (!projectId || !scene) return;
      if (scene.environment === environment) {
        // No-op PUT avoided, but still (re)apply the preset so the user gets the
        // solver defaults for the current mode, and refresh the resolved value.
        applyEnvPreset(environment);
        refreshResolvedEnv();
        return;
      }
      await run("Updating environment…", async () => {
        const next: Scene = { ...scene, environment };
        set({ scene: normalizeScene(await api.putScene(projectId, next)) });
        // Apply presets (session-only) and expose the resolved value.
        applyEnvPreset(environment);
        refreshResolvedEnv();
        await revalidateIfOpen();
        set({ notice: `Environment set to ${environment}` });
      });
    },

    // ---------------------------------------------------- solver config

    setPathsConfig: (patch) => set({ pathsConfig: { ...get().pathsConfig, ...patch } }),
    setRadioMapConfig: (patch) => set({ radioMapConfig: { ...get().radioMapConfig, ...patch } }),

    setAuto: (target, on) => {
      if (target === "paths") set({ autoPaths: on });
      else if (target === "radioMap") set({ autoRadioMap: on });
      else if (target === "beamforming") set({ autoBeamforming: on });
      else set({ autoChannel: on });
      // Arming a target recomputes immediately so the result matches the
      // current scene (channel silently waits for its first manual run).
      if (on) scheduleAuto(target);
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
        set({
          scene: normalizeScene(await api.putScene(projectId, next)),
          notice: "Saved as project default config",
        });
      });
    },

    setBeamArray: (patch) => set(patch),
    setBeamforming: (patch) => set(patch),

    // ---------------------------------------------------- config presets

    applyConfigPreset: (id) => {
      // "Custom" is a display-only sentinel: keep whatever the user has set.
      if (id === "custom") return;
      const preset = CONFIG_PRESETS[id];
      const { pathsConfig, radioMapConfig } = get();
      // Apply the same solver fields to both configs. The radio-map grid patch
      // lands on both too: the paths solver ignores radio_map, but detectPreset
      // reads pathsConfig.radio_map, so both must carry the preset's grid for
      // the select to reflect the preset as active after applying it.
      // Backend/tx/rx selections are untouched.
      set({
        pathsConfig: {
          ...pathsConfig,
          ...preset.config,
          radio_map: { ...pathsConfig.radio_map, ...preset.radioMap },
        },
        radioMapConfig: {
          ...radioMapConfig,
          ...preset.config,
          radio_map: { ...radioMapConfig.radio_map, ...preset.radioMap },
        },
      });
    },

    // ---------------------------------------------------- viewport

    setViewport: (patch) => {
      const next = { ...get().viewport, ...patch };
      set({ viewport: next });
      const pid = get().projectId;
      if (pid) saveViewportSettings(pid, next);
    },

    resetViewport: () => {
      const next = defaultViewportSettings();
      set({ viewport: next });
      const pid = get().projectId;
      if (pid) saveViewportSettings(pid, next);
    },

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

    addDevice: async (kind, position) => {
      const scene = get().scene;
      if (!scene) return;
      const id = nextDeviceId(kind);
      const isTx = kind === "tx";
      const device: Device = {
        id,
        name: isTx ? "Transmitter" : "Receiver",
        kind,
        // Explicit position (K/L hotkey placement at the surface hit, Sionna
        // RT GUI convention) wins over the kind defaults.
        position: position ?? (isTx ? [0, 0, 10] : [10, 0, 1.5]),
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

    // ---------------------------------------------------- actor editing

    addActor: async (kind) => {
      const scene = get().scene;
      if (!scene) return;
      const id = nextActorId(kind);
      const d = ACTOR_DEFAULTS[kind];
      // Seed the kind's defaults client-side; backend re-applies them anyway.
      const actor: Actor = {
        id,
        name: kind === "car" ? "Car" : kind === "human" ? "Human" : "Object",
        kind,
        shape: { type: "box", size_m: [...d.size_m], mesh_ref: null },
        rf_material_id: d.rf_material_id,
        position: sceneCenter(),
        orientation_deg: [0, 0, 0],
        trajectory: null,
        attached_device_ids: [],
        color: d.color,
      };
      await run(`Adding ${id}…`, async () => {
        await putSceneAndRefresh({ ...scene, actors: [...scene.actors, actor] });
        set({ selectedActorId: id, selection: [], selectedDeviceId: null, notice: `Added ${id}` });
      });
      afterSceneEdit();
    },

    updateActor: async (actorId, patch) => {
      const scene = get().scene;
      if (!scene) return;
      const actors = scene.actors.map((a) => (a.id === actorId ? { ...a, ...patch } : a));
      await run("Updating actor…", async () => {
        await putSceneAndRefresh({ ...scene, actors });
        set({ notice: `Updated ${actorId}` });
      });
      afterSceneEdit();
    },

    deleteActor: async (actorId) => {
      const scene = get().scene;
      if (!scene) return;
      const actors = scene.actors.filter((a) => a.id !== actorId);
      await run(`Removing ${actorId}…`, async () => {
        await putSceneAndRefresh({ ...scene, actors });
        if (get().selectedActorId === actorId) set({ selectedActorId: null });
        set({ notice: `Removed ${actorId}` });
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
          // Request per-waypoint ray paths so the viewer can render live rays
          // during playback/scrub (feature: trajectory live rays).
          include_paths: true,
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
    setTrajLoop: (loop) => set({ trajLoop: loop }),

    // ---------------------------------------------------- scenario playback

    simulateScenario: async ({ num_frames, dt_s, include_paths }) => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Simulating scenario…", async () => {
        const result = await api.simulateScenario(pid, {
          config: get().pathsConfig,
          num_frames,
          dt_s,
          include_paths,
        });
        set({
          scenario: result,
          scenarioFrame: 0,
          scenarioPlaying: false,
          mode: "results",
          notice: `Scenario: ${result.frames.length} frame(s) via ${result.backend} backend`,
        });
        await refetchSceneInner(); // a ResultSetRef (kind 'scenario') was appended
      });
    },

    setScenarioFrame: (frame) => {
      const sc = get().scenario;
      const max = sc ? sc.frames.length - 1 : 0;
      set({ scenarioFrame: Math.max(0, Math.min(max, frame)) });
    },
    setScenarioPlaying: (playing) => set({ scenarioPlaying: playing }),
    setScenarioSpeed: (speed) => set({ scenarioSpeed: speed }),
    setScenarioLoop: (loop) => set({ scenarioLoop: loop }),

    // ---------------------------------------------------- channel analysis

    analyzeChannel: async (txId, rxId, numCfrPoints) => {
      const pid = get().projectId;
      if (!pid) return;
      // Remember the pair so auto-update can re-run the same analysis after
      // scene changes (device moves, live sync, material edits).
      setLastChannelArgs({ txId, rxId, numCfrPoints });
      await run("Analyzing channel…", async () => {
        const result = await api.analyzeChannel(pid, {
          config: get().pathsConfig,
          tx_id: txId,
          rx_id: rxId,
          // Only send num_cfr_points when provided so the backend default holds.
          ...(numCfrPoints !== undefined ? { num_cfr_points: numCfrPoints } : {}),
        });
        set({
          channelResult: result,
          notice:
            `Channel ${result.tx_id}→${result.rx_id}: ${result.num_paths} path(s) via ` +
            `${result.backend} backend`,
        });
      });
    },

    clearChannel: () => {
      // An explicit clear also forgets the pair so auto-update stops
      // resurrecting an analysis the user dismissed.
      setLastChannelArgs(null);
      set({ channelResult: null });
    },

    // -------------------------------------------- live sync + screenshot

    setLiveMode: (on) => {
      set({ liveMode: on });
      if (on) startLivePoll();
      else stopLivePoll();
    },

    setSendScreenshot: (on) => set({ sendScreenshot: on }),

    registerViewportCapture: (fn) => {
      viewportCapture = fn;
    },

    captureViewport: () => {
      const shot = viewportCapture ? viewportCapture() : null;
      if (shot) set({ lastViewportShot: shot });
      return shot;
    },

    dismissError: () => set({ error: null }),
    dismissNotice: () => set({ notice: null }),
  };
});
