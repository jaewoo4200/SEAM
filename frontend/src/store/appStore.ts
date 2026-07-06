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
  MeshRadioMapResultSet,
  PathResultSet,
  PathType,
  ProjectInfo,
  RadioMapResultSet,
  RFMaterial,
  RFMaterialLibrary,
  ScenarioResultSet,
  Scene,
  SceneBounds,
  SimulationConfig,
  SuggestionDecision,
  TrajectoryResultSet,
  UERoute,
  ValidationReport,
  Vec3,
} from "../types/api";
import { trajectorySteps } from "../trajectoryUtils";
import { ACTOR_DEFAULTS } from "../actorDefaults";
import {
  defaultViewportSettings,
  hasViewportSettings,
  loadViewportSettings,
  saveViewportSettings,
} from "../viewportSettings";
import type { ViewportSettings } from "../viewportSettings";
import { CONFIG_PRESETS } from "../configPresets";
import type { ConfigPresetId } from "../configPresets";
import { clampRect, loadPanelLayout, savePanelLayout } from "../panelLayout";
import type { DockTarget, FloatRect, PanelLayout } from "../panelLayout";

// Loaded once at store creation; normalization guards stale localStorage.
const initialPanelLayout = loadPanelLayout();

// Dev-only store handle for interaction tests (same spirit as __stwScene).
declare global {
  interface Window {
    __stwStore?: unknown;
  }
}

// Monotonic pick-request token (module scope: survives store updates).
let pickCounter = 0;

export type Mode = "visual" | "rf" | "validation" | "ai" | "results";

// ------------------------------------------------------------ viewport pick
// Generic click-to-place: any panel can request N world-space points from the
// 3D viewport (trajectory endpoints, dataset region corners, waypoints...).

export type PickTarget = "surface" | "ground";

export interface PickRequest {
  /** Monotonic token guarding stale completions/cancels. */
  id: number;
  /** Shown in the viewport banner, e.g. "Trajectory start → end". */
  label: string;
  /** Number of points to collect before completing (1, 2, or more), or
   *  "multi": collect indefinitely until Esc, which COMPLETES with the points
   *  placed so far (>= 2) instead of cancelling. */
  count: number | "multi";
  /** Meters added along world +Z to the raycast hit (terrain snap height). */
  heightOffset: number;
  /** 'surface' = mesh-first with z=0 ground fallback; 'ground' = force z=0 plane. */
  target: PickTarget;
  onComplete: (pts: Vec3[]) => void;
  onCancel?: () => void;
}

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
  meshRadioMap: MeshRadioMapResultSet | null;
  beamforming: BeamformingResult | null;
  selectedPathId: string | null;
  // Result-overlay visibility toggles (Result mode).
  showPaths: boolean;
  showRadioMap: boolean;
  showMeshRadioMap: boolean;
  showBeamforming: boolean;
  /** Scenario playback takes over the device/actor layers only while ON.
   *  Off by default when a persisted scenario merely loads with the project
   *  (a stored result must not hijack the viewport). */
  showScenario: boolean;

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
  // Device ids whose links are hidden in ray overlays/tables (filter chips).
  hiddenLinkDevices: string[];
  // RF material ids to keep in ray overlays/tables (empty = all). A path is
  // kept if any interaction hits one of these materials.
  materialFilter: string[];
  /** Prim ids hidden in the 3D viewer (eye toggle in the scene tree). */
  hiddenPrims: string[];
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

  // --- result provenance / staleness ---
  /** Bumped on every scene edit (device/actor/material moves, live sync).
   *  Results remember the epoch they were computed at; a mismatch means the
   *  scene changed since - shown as a "stale" badge instead of silently
   *  presenting outdated numbers. */
  sceneEpoch: number;
  resultEpochs: {
    paths?: number;
    channel?: number;
    trajectory?: number;
    beamforming?: number;
    mesh_radio_map?: number;
  };

  // --- viewport pick mode (click-to-place) ---
  pick: PickRequest | null;
  /** Points collected so far for the active pick (drives live markers). */
  pickPoints: Vec3[];
  /** Planned trajectory segment previewed in the viewer (dashed line). */
  trajPreview: Vec3[] | null;
  /** Arms the viewer's ghost device placement from UI buttons (mouse
   *  discoverability for the K/L hotkey flow); the viewer clears it. */
  placeArm: "tx" | "rx" | null;
  /** True while a gizmo axis is actively dragged (live-poll merge pauses). */
  gizmoDragging: boolean;

  // --- scene bounds (fetched per project; seeds sane coordinate defaults) ---
  sceneBounds: SceneBounds | null;

  // --- dockable panel layout (attach/detach, photo-editor style) ---
  panelLayout: PanelLayout;
  panelZ: string[];

  // --- live sync + AI screenshot groundwork ---
  liveMode: boolean;
  /** Latest viewport JPEG data URL (captured on demand from Viewer3D). */
  lastViewportShot: string | null;
  /** When ON, suggest-materials attaches the viewport capture as
   *  screenshot_data_url so vision-capable providers see the scene. */
  sendScreenshot: boolean;
  /** When ON, the server extracts per-prim texture crops from the visual GLB
   *  and attaches them as extra image evidence (paper roadmap #4). */
  sendTextureCrops: boolean;
  // Forced AI provider for suggestions; null = server picks the best available.
  aiProvider: string | null;

  busy: string | null;
  error: string | null;
  notice: string | null;

  init: () => Promise<void>;
  openProject: (projectId: string) => Promise<void>;
  /** Permanently delete the currently open project, reload the project list,
   *  and open the first remaining one (or fall back to the empty state). */
  deleteCurrentProject: () => Promise<void>;
  refetchScene: () => Promise<void>;
  setMode: (mode: Mode) => void;
  selectPrim: (primId: string, additive?: boolean) => void;
  selectDevice: (deviceId: string) => void;
  clearSelection: () => void;
  selectPath: (pathId: string | null) => void;
  toggleOverlay: (
    kind: "paths" | "radioMap" | "meshRadioMap" | "beamforming" | "trajectoryRays",
  ) => void;
  /** Per-frame trajectory rays overlay (include_paths results). Independent of
   *  the static Rays toggle: computing a trajectory turns it ON, computing
   *  static paths turns it OFF (latest computation wins), user can re-toggle. */
  showTrajectoryRays: boolean;
  runValidation: () => Promise<void>;
  compileRF: () => Promise<void>;
  simulatePaths: () => Promise<void>;
  simulateRadioMap: () => Promise<void>;
  /** Mesh radio map over the current selection (uses selection as prim_ids).
   *  `maxTriangles` caps the sampled triangles per surface (denser paint costs
   *  more; the backend subsamples with a uniform stride to stay under it). */
  simulateMeshRadioMap: (maxTriangles?: number) => Promise<void>;
  /** Best-effort silent fetch of the latest stored mesh radio map (project open). */
  fetchLatestMeshRadioMap: () => Promise<void>;
  removePaths: () => void;
  removeRadioMap: () => void;
  removeMeshRadioMap: () => void;
  removeScenario: () => void;
  exportRfdata: () => Promise<void>;
  runBeamforming: () => Promise<void>;
  assignMaterial: (req: AssignRequest) => Promise<void>;
  saveMaterial: (mat: RFMaterial) => Promise<void>;
  suggestMaterials: () => Promise<void>;
  /** Replace the AI-suggestion state directly (rule-generation flows produce
   *  MaterialSuggestionResponse outside suggestMaterials). Resets decisions. */
  setSuggestions: (resp: MaterialSuggestionResponse | null) => void;
  setDecision: (primId: string, decision: SuggestionDecision | null) => void;
  applyDecisions: () => Promise<void>;

  // viewport pick mode
  requestPick: (req: Omit<PickRequest, "id">) => number;
  addPickPoint: (p: Vec3) => void;
  cancelPick: (id?: number) => void;
  /** Esc handler: a "multi" pick with >=2 points COMPLETES with them;
   *  anything else cancels (finite picks keep their all-or-nothing UX). */
  finishPick: () => void;
  setTrajPreview: (seg: Vec3[] | null) => void;
  armPlacement: (kind: "tx" | "rx" | null) => void;
  setGizmoDragging: (on: boolean) => void;

  // dockable panels
  setPanelDock: (id: string, dock: DockTarget) => void;
  setPanelFloatRect: (id: string, rect: FloatRect) => void;
  raisePanel: (id: string) => void;
  resetPanelLayout: () => void;

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
  toggleLinkDevice: (id: string) => void;
  setHiddenLinkDevices: (ids: string[]) => void;
  toggleMaterialFilter: (id: string) => void;
  setMaterialFilter: (ids: string[]) => void;
  togglePrimVisibility: (primId: string) => void;
  setStrongestN: (n: number) => void;
  setMinPowerDbm: (p: number | null) => void;
  setColorBy: (c: ColorBy) => void;
  setLineWidthByPower: (on: boolean) => void;

  // trajectory
  simulateTrajectory: (params: {
    start_m?: Vec3;
    end_m?: Vec3;
    num_points: number;
    dt_s: number;
    ue_id?: string | null;
    follow_terrain?: boolean;
    follow_height_m?: number;
    /** Multi-UE routes; when set the start/end/ue_id fields are ignored. */
    routes?: UERoute[];
  }) => Promise<void>;
  /** Drop the loaded trajectory result (overlay clears immediately). */
  removeTrajectory: () => void;
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
  analyzeChannel: (
    txId: string,
    rxId: string,
    numCfrPoints?: number,
    scsKhz?: number,
  ) => Promise<void>;
  clearChannel: () => void;

  // live sync + AI screenshot groundwork
  setLiveMode: (on: boolean) => void;
  setSendScreenshot: (on: boolean) => void;
  setSendTextureCrops: (on: boolean) => void;
  setAiProvider: (name: string | null) => void;
  /** Viewer3D registers a canvas snapshot fn here; store calls it on demand. */
  registerViewportCapture: (fn: (() => string | null) | null) => void;
  captureViewport: () => string | null;
  /** Multi-view variant: 4 azimuth views around the scene center (paper #3).
   *  Viewer3D registers it; the store calls it when sendScreenshot is on. */
  registerMultiViewCapture: (fn: (() => string[]) | null) => void;
  captureMultiView: () => string[];

  dismissError: () => void;
  dismissNotice: () => void;
}

export const useAppStore = create<AppState>()((set, get) => {
  /** Run an async action with busy/error bookkeeping. Returns undefined on failure. */
  async function run<T>(label: string, fn: () => Promise<T>): Promise<T | undefined> {
    // An unread error must survive unrelated actions (audit: starting any
    // action silently wiped the previous failure before the user saw it).
    // Errors clear only via explicit dismiss or replacement by a newer one;
    // stale success notices do get cleared.
    set({ busy: label, notice: null });
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
  let lastChannelArgs: {
    txId: string;
    rxId: string;
    numCfrPoints?: number;
    scsKhz?: number;
  } | null = null;

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

  // True while a debounced auto-recompute is running: those refresh result
  // DATA but must never yank the user into Results mode mid-edit (audit B1).
  let autoInFlight = false;

  function autoRun(target: AutoTarget): void {
    const done = () => {
      autoInFlight = false;
    };
    autoInFlight = true;
    switch (target) {
      case "paths":
        void get().simulatePaths().finally(done);
        return;
      case "radioMap":
        void get().simulateRadioMap().finally(done);
        return;
      case "beamforming":
        void get().runBeamforming().finally(done);
        return;
      case "channel": {
        const a = lastChannelArgs;
        if (a) {
          void get().analyzeChannel(a.txId, a.rxId, a.numCfrPoints, a.scsKhz).finally(done);
        } else {
          done();
        }
        return;
      }
    }
  }

  /** Stamp a result kind as computed at the current scene epoch. */
  function stampResult(
    kind: "paths" | "channel" | "trajectory" | "beamforming" | "mesh_radio_map",
  ): void {
    set({ resultEpochs: { ...get().resultEpochs, [kind]: get().sceneEpoch } });
  }

  /** Mode patch for result-producing actions: user-initiated runs jump to
   *  Results; auto reruns leave the current mode alone. */
  function resultsMode(): { mode?: Mode } {
    return autoInFlight ? {} : { mode: "results" };
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
    set({ sceneEpoch: get().sceneEpoch + 1 });
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
          // Never merge positions while the user is dragging a gizmo: the
          // poll would snap the marker back mid-drag (audit finding).
          if (get().gizmoDragging) return;
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
  // Multi-view capture fn (4 azimuth views), registered by Viewer3D.
  let multiViewCapture: (() => string[]) | null = null;

  // --- live-event WebSocket: server pushes sim/compile start/finish notices ---
  // Entirely best-effort: the endpoint lands this wave on the backend; every
  // failure path (unsupported protocol, connect error, malformed frame) is
  // swallowed so its absence is silent and never crashes the app.
  let eventSocket: WebSocket | null = null;

  function closeEventSocket(): void {
    if (eventSocket) {
      try {
        // Drop handlers first so onclose/onerror don't fire during teardown.
        eventSocket.onopen = null;
        eventSocket.onmessage = null;
        eventSocket.onerror = null;
        eventSocket.onclose = null;
        eventSocket.close();
      } catch {
        // already closing/closed
      }
      eventSocket = null;
    }
  }

  function connectEventSocket(projectId: string): void {
    closeEventSocket();
    if (typeof WebSocket === "undefined") return;
    let url: string;
    try {
      // Derive from the API client base ("/api") against the current origin so
      // the same dev-proxy / reverse-proxy host serves the socket.
      const base = new URL("/api", window.location.href);
      base.protocol = base.protocol === "https:" ? "wss:" : "ws:";
      url = `${base.origin}/ws/projects/${encodeURIComponent(projectId)}/events`;
    } catch {
      return;
    }
    let sock: WebSocket;
    try {
      sock = new WebSocket(url);
    } catch {
      return; // construction can throw on a malformed URL / blocked scheme
    }
    eventSocket = sock;
    sock.onerror = () => {
      // Absence of the endpoint is expected while the backend side lands;
      // stay silent rather than surfacing a connection error to the user.
    };
    sock.onmessage = (ev) => {
      // A stale socket from a prior project must not post into the new one.
      if (eventSocket !== sock || get().projectId !== projectId) return;
      let msg: { type?: unknown; [k: string]: unknown };
      try {
        const parsed: unknown = JSON.parse(String(ev.data));
        if (parsed === null || typeof parsed !== "object") return;
        msg = parsed as { type?: unknown };
      } catch {
        return; // malformed frame: ignore
      }
      const type = typeof msg.type === "string" ? msg.type : "";
      if (type === "simulation_finished") {
        const backend = typeof msg.backend === "string" ? ` (${msg.backend})` : "";
        set({ notice: `Simulation finished${backend}` });
      } else if (type === "compile_finished") {
        set({ notice: "Compile finished" });
      }
      // start events are intentionally quiet (the busy spinner already shows
      // in-app runs); only completions from the outside world raise a notice.
    };
  }

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
    meshRadioMap: null,
    beamforming: null,
    selectedPathId: null,
    showPaths: true,
    showRadioMap: true,
    showMeshRadioMap: true,
    showBeamforming: true,
    showTrajectoryRays: true,
    showScenario: false,

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
    hiddenLinkDevices: [],
    materialFilter: [],
    hiddenPrims: [],
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

    sceneEpoch: 0,
    resultEpochs: {},

    pick: null,
    pickPoints: [],
    trajPreview: null,
    placeArm: null,
    gizmoDragging: false,
    sceneBounds: null,
    panelLayout: initialPanelLayout.layout,
    panelZ: initialPanelLayout.z,

    liveMode: false,
    lastViewportShot: null,
    sendScreenshot: false,
    sendTextureCrops: false,
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
      // A point picked against the old scene must never land in the new one.
      get().cancelPick();
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
        // The environment split exists for a reason (owner directive): an
        // indoor project must OPEN with the indoor solver/grid defaults, not
        // only after the user re-picks the env dropdown. The preset overlays
        // just its env-signature fields; other stored values survive.
        const resolvedForOpen = resolveEnvironment(scene.environment, scene);
        const envPreset = presetForEnvironment(scene.environment, scene);
        const seeded = { ...seed, ...envPreset.paths };
        const seededRm = {
          ...seed,
          ...envPreset.paths,
          max_depth: ENV_RADIOMAP_DEPTH[resolvedForOpen],
          radio_map: { ...seed.radio_map, ...envPreset.radioMap },
        };
        set({
          projectId,
          scene,
          resolvedEnvironment: resolvedForOpen,
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
          meshRadioMap: null,
          beamforming: null,
          selectedPathId: null,
          // Overlay visibility starts fresh per project.
          hiddenLinkDevices: [],
          materialFilter: [],
          // Show every prim the scene defines; hiding is the user's call via
          // the eye toggle (auto-hiding "helper" prims kept surprising users).
          hiddenPrims: [],
          showPaths: true,
          showRadioMap: true,
          showMeshRadioMap: true,
          showBeamforming: true,
          showTrajectoryRays: true,
          // A persisted scenario loads for playback ON DEMAND - it must not
          // take over the viewport just because the project has one stored.
          showScenario: false,
          pathsConfig: seeded,
          radioMapConfig: seededRm,
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
          // Fresh epoch per project; persisted results are epoch-0 (fresh).
          sceneEpoch: 0,
          resultEpochs: {},
          // Pick state is per-scene; bounds refetch below.
          pick: null,
          pickPoints: [],
          sceneBounds: null,
          // Viewport lighting/helpers are per-project (localStorage-backed).
          // First open (nothing persisted): the slice height defaults to a
          // human-height cut indoors instead of the outdoor 2 m.
          viewport: (() => {
            const vp = loadViewportSettings(projectId);
            if (!hasViewportSettings(projectId) && resolvedForOpen === "indoor") {
              vp.sliceZ = 1.2;
            }
            return vp;
          })(),
        });
        // Scene bounds seed sane coordinate defaults (dataset region,
        // trajectory endpoints). 404 = no mesh/devices; leave null.
        void api
          .sceneBounds(projectId)
          .then((b) => {
            if (get().projectId === projectId) set({ sceneBounds: b });
          })
          .catch(() => undefined);
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
      // Latest stored mesh radio map + live-event socket: both out-of-band and
      // fully best-effort so a missing endpoint (this wave still landing on the
      // backend) never blocks or breaks project open.
      void get().fetchLatestMeshRadioMap();
      connectEventSocket(projectId);
    },

    deleteCurrentProject: async () => {
      const pid = get().projectId;
      if (!pid) return;
      await run(`Deleting ${pid}…`, async () => {
        await api.deleteProject(pid);
        // Tearing down the deleted project cleanly: stop its live poll/event
        // socket before we swap in (or clear) the project so a stale socket
        // can't post into whatever opens next.
        stopLivePoll();
        closeEventSocket();
        // Reload the list, then open the first survivor. openProject resets the
        // full per-project view state, so no manual scene/result cleanup here.
        const list = await api.listProjects();
        set({ projects: list });
        const next = list.find((p) => p.project_id !== pid) ?? list[0] ?? null;
        if (next) {
          await get().openProject(next.project_id);
          set({ notice: `Deleted project ${pid}` });
        } else {
          // No projects left: fall back to the empty state (mirror the fields
          // openProject would otherwise have refreshed).
          set({
            projectId: null,
            scene: null,
            materials: null,
            selection: [],
            selectedDeviceId: null,
            selectedActorId: null,
            pathResults: null,
            radioMap: null,
            meshRadioMap: null,
            beamforming: null,
            trajectory: null,
            scenario: null,
            channelResult: null,
            sceneBounds: null,
            notice: `Deleted project ${pid} (no projects remaining)`,
          });
        }
      });
    },

    refetchScene: async () => {
      await run("Refreshing scene…", refetchSceneInner);
    },

    setMode: (mode) => {
      // A mode switch may unmount the panel that armed an in-flight pick;
      // cancel and SAY so - a silently vanished crosshair reads as a bug.
      const hadPick = get().pick;
      get().cancelPick();
      set({ mode, ...(hadPick ? { notice: `Pick cancelled (${hadPick.label})` } : {}) });
    },

    // ---------------------------------------------------- viewport pick mode

    requestPick: (req) => {
      const prev = get().pick;
      if (prev) prev.onCancel?.();
      const id = ++pickCounter;
      // Arming a pick puts the viewport in a clean state: an active gizmo
      // would fight the capture-phase pick click for the same pointer.
      get().clearSelection();
      set({ pick: { ...req, id }, pickPoints: [] });
      return id;
    },

    addPickPoint: (p) => {
      const { pick, pickPoints } = get();
      if (!pick) return;
      const next = [...pickPoints, p];
      if (pick.count !== "multi" && next.length >= pick.count) {
        // Clear FIRST so the store is idle when the consumer's setState runs
        // (onComplete must not observe a still-active pick).
        set({ pick: null, pickPoints: [] });
        // Picked points are SURFACE hits (they match the crosshair on screen);
        // the height offset is applied here, at commit time.
        pick.onComplete(
          next
            .slice(0, pick.count)
            .map((pt): Vec3 => [pt[0], pt[1], pt[2] + pick.heightOffset]),
        );
      } else {
        set({ pickPoints: next });
      }
    },

    cancelPick: (id) => {
      const { pick } = get();
      if (!pick || (id !== undefined && pick.id !== id)) return;
      set({ pick: null, pickPoints: [] });
      pick.onCancel?.();
    },

    finishPick: () => {
      const { pick, pickPoints } = get();
      if (!pick) return;
      if (pick.count === "multi" && pickPoints.length >= 2) {
        set({ pick: null, pickPoints: [] });
        pick.onComplete(
          pickPoints.map((pt): Vec3 => [pt[0], pt[1], pt[2] + pick.heightOffset]),
        );
      } else {
        get().cancelPick();
      }
    },

    setTrajPreview: (seg) => set({ trajPreview: seg }),

    armPlacement: (kind) => {
      // Placement and pick both own viewport clicks; arming one cancels the other.
      if (kind) get().cancelPick();
      set({ placeArm: kind });
    },

    setGizmoDragging: (on) => {
      if (get().gizmoDragging !== on) set({ gizmoDragging: on });
    },

    // ------------------------------------------------------- dockable panels

    setPanelDock: (id, dock) => {
      const layout = { ...get().panelLayout };
      if (!layout[id]) return;
      layout[id] = { ...layout[id], dock };
      const panelZ = [...get().panelZ.filter((p) => p !== id), id];
      set({ panelLayout: layout, panelZ });
      savePanelLayout({ layout, z: panelZ });
    },

    setPanelFloatRect: (id, rect) => {
      const layout = { ...get().panelLayout };
      if (!layout[id]) return;
      layout[id] = { ...layout[id], float: clampRect(rect) };
      set({ panelLayout: layout });
      savePanelLayout({ layout, z: get().panelZ });
    },

    raisePanel: (id) => {
      const z = get().panelZ;
      if (z[z.length - 1] === id) return;
      const panelZ = [...z.filter((p) => p !== id), id];
      set({ panelZ });
      savePanelLayout({ layout: get().panelLayout, z: panelZ });
    },

    resetPanelLayout: () => {
      try {
        localStorage.removeItem("stw.panelLayout.v1");
      } catch {
        // best-effort
      }
      const fresh = loadPanelLayout();
      set({ panelLayout: fresh.layout, panelZ: fresh.z });
    },

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
      else if (kind === "meshRadioMap") set({ showMeshRadioMap: !get().showMeshRadioMap });
      else if (kind === "trajectoryRays")
        set({ showTrajectoryRays: !get().showTrajectoryRays });
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
          // Latest computation wins: a fresh static solve is what the user
          // wants to see, so the trajectory-ray takeover steps aside (both
          // remain independently toggleable in the overlay row).
          showPaths: true,
          showTrajectoryRays: false,
          ...resultsMode(),
          notice: `Simulated ${result.paths.length} path(s) via ${result.backend} backend`,
        });
        stampResult("paths");
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
          ...resultsMode(),
          notice: `Radio map computed via ${result.backend} backend`,
        });
        await refetchSceneInner();
      });
    },

    simulateMeshRadioMap: async (maxTriangles) => {
      const pid = get().projectId;
      if (!pid) return;
      const primIds = get().selection;
      if (primIds.length === 0) {
        // Guard: the button is disabled without a selection, but a keyboard/API
        // caller could still reach here. Surface the requirement, don't POST an
        // invalid (empty prim_ids) request.
        set({ error: "Select at least one surface prim before running a mesh radio map" });
        return;
      }
      await run("Simulating mesh radio map…", async () => {
        const result = await api.simulateMeshRadioMap(pid, {
          config: get().radioMapConfig,
          prim_ids: primIds,
          // Only send max_triangles when provided so the backend default holds.
          ...(maxTriangles !== undefined ? { max_triangles: maxTriangles } : {}),
        });
        const tris = result.surfaces.reduce((n, s) => n + s.triangle_count, 0);
        set({
          meshRadioMap: result,
          showMeshRadioMap: true,
          ...resultsMode(),
          notice:
            `Mesh radio map: ${result.surfaces.length} surface(s), ${tris} triangle(s) via ` +
            `${result.backend} backend`,
        });
        stampResult("mesh_radio_map");
        await refetchSceneInner(); // a ResultSetRef (kind 'mesh_radio_map') was appended
      });
    },

    fetchLatestMeshRadioMap: async () => {
      const pid = get().projectId;
      if (!pid) return;
      try {
        const result = await api.getMeshRadioMapResult(pid);
        // Guard against a project switch racing the fetch.
        if (get().projectId === pid) set({ meshRadioMap: result });
      } catch {
        // no mesh radio map yet (or endpoint not landed): ignore silently
      }
    },

    removePaths: () => set({ pathResults: null, selectedPathId: null }),

    removeScenario: () =>
      set({ scenario: null, scenarioFrame: 0, scenarioPlaying: false, showScenario: false }),
    removeRadioMap: () => set({ radioMap: null }),
    removeMeshRadioMap: () => set({ meshRadioMap: null }),
    removeTrajectory: () =>
      set({ trajectory: null, trajFrame: 0, trajPlaying: false }),

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
        set({ beamforming: r, showBeamforming: true, ...resultsMode(), notice: parts.join(" · ") });
        stampResult("beamforming");
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
      const { selection, sendScreenshot, sendTextureCrops, aiProvider } = get();
      // VLM: when the user opts in, capture 4 azimuth views around the scene
      // (paper roadmap #3) so the provider sees the geometry from every side.
      // Falls back to a single current-view capture when multi-view is
      // unavailable (no scene bounds / capture fn not registered yet). The
      // pinned SuggestMaterialsRequest carries screenshot_data_urls (multi) and
      // keeps screenshot_data_url (single) for back-compat.
      const shots = sendScreenshot ? get().captureMultiView() : [];
      const single = shots.length > 0 ? null : sendScreenshot ? get().captureViewport() : null;
      await run("Requesting RF material suggestions…", async () => {
        const resp = await api.suggestMaterials(pid, {
          prim_ids: selection.length > 0 ? selection : null,
          // null = server picks the best available provider.
          provider: aiProvider,
          screenshot_data_url: single,
          screenshot_data_urls: shots.length > 0 ? shots : null,
          attach_texture_crops: sendTextureCrops,
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
        // Re-selecting the active environment is a no-op: silently re-applying
        // the preset here wiped hand-tuned solver knobs (audit M5).
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
        set({
          notice:
            `Environment set to ${environment} - solver defaults for this ` +
            "mode were applied to Paths/Radio map",
        });
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
        // The overwrite must be visible (audit M5): both solver configs were
        // just replaced by the preset's fields.
        notice: `Applied preset "${preset.label}" to Paths + Radio map configs`,
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
        // RT GUI convention) wins. The fallback is env/bounds-aware: a 10 m
        // mast default put indoor TXs above the ceiling (outside the room).
        position:
          position ??
          (() => {
            const b = get().sceneBounds;
            const indoor = get().resolvedEnvironment === "indoor";
            if (!b) return (isTx ? [0, 0, 10] : [10, 0, 1.5]) as Vec3;
            const cx = (b.min[0] + b.max[0]) / 2;
            const cy = (b.min[1] + b.max[1]) / 2;
            const txZ = indoor ? Math.min(b.max[2] - 0.3, b.min[2] + 2.5) : 10;
            const off = Math.min(5, (b.max[0] - b.min[0]) / 4);
            return (isTx
              ? [cx, cy, Math.max(b.min[2] + 0.5, txZ)]
              : [cx + off, cy, b.min[2] + 1.5]) as Vec3;
          })(),
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
    toggleLinkDevice: (id) => {
      const cur = get().hiddenLinkDevices;
      set({
        hiddenLinkDevices: cur.includes(id)
          ? cur.filter((x) => x !== id)
          : [...cur, id],
      });
    },
    setHiddenLinkDevices: (ids) => set({ hiddenLinkDevices: ids }),
    toggleMaterialFilter: (id) => {
      const cur = get().materialFilter;
      set({
        materialFilter: cur.includes(id)
          ? cur.filter((x) => x !== id)
          : [...cur, id],
      });
    },
    setMaterialFilter: (ids) => set({ materialFilter: ids }),

    togglePrimVisibility: (primId) => {
      const cur = get().hiddenPrims;
      set({
        hiddenPrims: cur.includes(primId)
          ? cur.filter((id) => id !== primId)
          : [...cur, primId],
      });
    },
    setStrongestN: (n) => set({ strongestN: n }),
    setMinPowerDbm: (p) => set({ minPowerDbm: p }),
    setColorBy: (c) => set({ colorBy: c }),
    setLineWidthByPower: (on) => set({ lineWidthByPower: on }),

    // ---------------------------------------------------- trajectory

    simulateTrajectory: async ({ start_m, end_m, num_points, dt_s, ue_id, follow_terrain, follow_height_m, routes }) => {
      const pid = get().projectId;
      if (!pid) return;
      await run("Simulating trajectory…", async () => {
        const result = await api.simulateTrajectory(pid, {
          config: get().pathsConfig,
          ue_id: ue_id ?? null,
          start_m: routes ? null : start_m,
          end_m: routes ? null : end_m,
          routes: routes ?? null,
          num_points,
          dt_s,
          follow_terrain: follow_terrain ?? false,
          ...(follow_height_m !== undefined ? { follow_height_m } : {}),
          // Request per-waypoint ray paths so the viewer can render live rays
          // during playback/scrub (feature: trajectory live rays).
          include_paths: true,
        });
        set({
          trajectory: result,
          trajFrame: 0,
          trajPlaying: false,
          // Latest computation wins: show the fresh per-frame rays.
          showTrajectoryRays: true,
          ...resultsMode(),
          notice: `Trajectory: ${result.samples.length} sample(s) via ${result.backend} backend`,
        });
        stampResult("trajectory");
        await refetchSceneInner();
      });
    },

    setTrajFrame: (frame) => {
      const traj = get().trajectory;
      const max = traj ? Math.max(0, trajectorySteps(traj) - 1) : 0;
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
          showScenario: true,
          scenarioFrame: 0,
          scenarioPlaying: false,
          ...resultsMode(),
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

    analyzeChannel: async (txId, rxId, numCfrPoints, scsKhz) => {
      const pid = get().projectId;
      if (!pid) return;
      // Remember the pair (and SCS) so auto-update can re-run the same analysis
      // after scene changes (device moves, live sync, material edits).
      setLastChannelArgs({ txId, rxId, numCfrPoints, scsKhz });
      await run("Analyzing channel…", async () => {
        const result = await api.analyzeChannel(pid, {
          config: get().pathsConfig,
          tx_id: txId,
          rx_id: rxId,
          // Only send num_cfr_points when provided so the backend default holds.
          ...(numCfrPoints !== undefined ? { num_cfr_points: numCfrPoints } : {}),
          // Likewise the SCS: omit to let the backend default (30 kHz) hold.
          ...(scsKhz !== undefined ? { subcarrier_spacing_khz: scsKhz } : {}),
        });
        // A debounced/auto analysis can land AFTER a project switch; writing
        // it would show the old scene's channel in the new project (audit B4).
        if (get().projectId !== pid) return;
        set({
          channelResult: result,
          notice:
            `Channel ${result.tx_id}→${result.rx_id}: ${result.num_paths} path(s) via ` +
            `${result.backend} backend`,
        });
        stampResult("channel");
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
    setSuggestions: (resp) => set({ suggestions: resp, decisions: {} }),
    setSendTextureCrops: (on) => set({ sendTextureCrops: on }),

    registerViewportCapture: (fn) => {
      viewportCapture = fn;
    },

    captureViewport: () => {
      const shot = viewportCapture ? viewportCapture() : null;
      if (shot) set({ lastViewportShot: shot });
      return shot;
    },

    registerMultiViewCapture: (fn) => {
      multiViewCapture = fn;
    },

    captureMultiView: () => {
      const shots = multiViewCapture ? multiViewCapture() : [];
      // Remember the first view as the "last shot" thumbnail (parity with the
      // single-capture path).
      if (shots.length > 0) set({ lastViewportShot: shots[0] });
      return shots;
    },

    dismissError: () => set({ error: null }),
    dismissNotice: () => set({ notice: null }),
  };
});

if (import.meta.env.DEV) {
  window.__stwStore = useAppStore;
}
