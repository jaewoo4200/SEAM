// Current communication paths for both the viewport and its results table.
import { useMemo } from "react";
import { api } from "./api/client";
import { useAppStore, type AppState } from "./store/appStore";
import { samplesForUe, trajectoryUeIds } from "./trajectoryUtils";
import type { PathResultSet, PlaybackResultSet, RayPath, ScenarioResultSet, SimulationConfig, TrajectoryResultSet, Vec3 } from "./types/api";

type MotionResult = TrajectoryResultSet | ScenarioResultSet | PlaybackResultSet;
type DisplayResult = Omit<PathResultSet, "kind"> & {
  kind: "paths" | MotionResult["kind"];
  frameLabel?: string;
};
export interface DragPathPreview {
  token: object;
  project: string;
  config: SimulationConfig;
  epoch: number;
  move: { kind: "actor" | "device"; id: string; position: Vec3 };
  result: PathResultSet & { frameLabel: string };
}

export function activeDragPreview(state: AppState) {
  const preview = state.dragPathPreview;
  return preview && preview.project === state.projectId && state.autoPaths && state.showPaths &&
    (state.mode === "visual" || state.mode === "results") ? preview : null;
}

export function communicationRaySource(state: AppState): DisplayResult["kind"] {
  if (activeDragPreview(state)) return "paths";
  if (state.showScenario && state.scenario?.frames.length) return "scenario";
  if (state.showPlayback && state.playback?.frames.length) return "playback";
  const moving = state.showTrajectoryRays || state.trajPlaying || state.trajFrame > 0 ||
    Object.keys(state.trajUeFrames).length > 0;
  return moving && state.trajectory?.samples.length ? "trajectory" : "paths";
}

// Older runs and independently computed UE tables may exceed the current TX cap.
function limitPerTransmitter(paths: RayPath[], limit: number) {
  if (paths.length <= limit) return paths;
  const groups = new Map<string, RayPath[]>();
  for (const path of paths) {
    const group = groups.get(path.tx_id);
    if (group) group.push(path);
    else groups.set(path.tx_id, [path]);
  }
  const dropped = new Set<RayPath>();
  for (const group of groups.values()) {
    if (group.length <= limit) continue;
    group.sort((a, b) => b.power_dbm - a.power_dbm);
    for (const path of group.slice(limit)) dropped.add(path);
  }
  return paths.filter((path) => !dropped.has(path));
}

function frameResult(result: MotionResult | PathResultSet | null, step: number,
  overrides: Record<string, number> | null, limit: number): DisplayResult | null {
  if (!result || result.kind === "paths") return result;
  let paths: RayPath[], frameLabel: string;
  if (result.kind === "trajectory") {
    const frames = trajectoryUeIds(result).map((id) => {
      const samples = samplesForUe(result, id);
      const index = Math.max(0, Math.min(samples.length - 1, overrides?.[id] ?? step));
      return { id, index, paths: samples[index]?.paths ?? [] };
    });
    paths = frames.flatMap((f) => f.paths);
    frameLabel = frames.length === 0 ? "" : frames.every((f) => f.index === frames[0].index)
      ? `frame ${frames[0].index + 1}`
      : frames.map((f) => `${f.id} frame ${f.index + 1}`).join(", ");
    if (frames.length > 1) {
      const seen = new Set<string>(), repeated = new Set<string>();
      for (const { path_id } of paths) {
        if (seen.has(path_id)) repeated.add(path_id);
        seen.add(path_id);
      }
      paths = paths.map((path) => repeated.has(path.path_id)
        ? { ...path, path_id: `${path.tx_id}/${path.rx_id}/${path.path_id}` } : path);
    }
  } else {
    const index = Math.max(0, Math.min(result.frames.length - 1, step));
    paths = result.frames[index]?.paths ?? [];
    frameLabel = `frame ${index + 1}`;
  }
  return { ...result, paths: limitPerTransmitter(paths, limit), frameLabel };
}

export function useCommunicationRayResult() {
  const kind = useAppStore(communicationRaySource);
  const result = useAppStore((s) => kind === "paths" ? activeDragPreview(s)?.result ?? s.pathResults : s[kind]);
  const step = useAppStore((s) => kind === "trajectory" ? s.trajFrame :
    kind === "scenario" ? s.scenarioFrame : kind === "playback" ? s.playbackFrame : 0);
  const overrides = useAppStore((s) => kind === "trajectory" ? s.trajUeFrames : null);
  const limit = useAppStore((s) => s.pathsConfig.max_returned_paths_per_tx ?? s.pathsConfig.max_num_paths_per_src);
  return useMemo(() => frameResult(result, step, overrides, limit), [result, step, overrides, limit]);
}

// Coalesce pointer events while one transient solve is in flight, so only the
// newest pose can publish rays. The authored scene is saved by the mouse-up handler.
let queued: DragPathPreview | null = null;
let timer: ReturnType<typeof setTimeout> | null = null;
let busy = false, nextSolve = 0;

function scheduleDragSolve() {
  if (!queued || busy || timer !== null) return;
  timer = setTimeout(async () => {
    timer = null;
    const job = queued;
    queued = null;
    const current = () => {
      const state = useAppStore.getState();
      return activeDragPreview(state) === job && state.pathsConfig === job?.config && state.sceneEpoch === job?.epoch;
    };
    if (!job || !current()) return;
    busy = true;
    nextSolve = performance.now() + 120;
    try {
      const response = await api.simulatePaths(job.project, { config: job.config, persist: false, preview_move: job.move });
      if (current()) useAppStore.setState({ dragPathPreview: { ...job, result: { ...response, frameLabel: "live pose" } } });
    } catch (error) {
      if (current()) useAppStore.setState({ dragPathPreview: { ...job, result: {
        ...job.result, frameLabel: "live pose", warnings: [error instanceof Error ? error.message : String(error)],
      } } });
    } finally {
      busy = false;
      scheduleDragSolve();
    }
  }, Math.max(0, nextSolve - performance.now()));
}

export function queueDragPosition(token: object, move: DragPathPreview["move"]) {
  const state = useAppStore.getState();
  if (!state.projectId || !state.autoPaths || !state.showPaths || (state.mode !== "visual" && state.mode !== "results")) return;
  const old = state.dragPathPreview;
  if (old?.token === token && old.config === state.pathsConfig && old.epoch === state.sceneEpoch &&
      move.position.every((value, i) => value === old.move.position[i])) return;
  queued = { token, move, project: state.projectId, config: state.pathsConfig, epoch: state.sceneEpoch, result: {
    kind: "paths", result_id: `Moving ${move.id}`, simulation_config_id: state.pathsConfig.id, backend: state.pathsConfig.backend,
    created_at: null, metadata: {}, paths: [], warnings: [], frameLabel: "solving current pose",
  } };
  useAppStore.setState({ dragPathPreview: queued });
  scheduleDragSolve();
}

export function cancelDragPosition(token: object | null, committing = false) {
  if (!token) return;
  if (queued?.token === token) {
    queued = null;
    if (timer !== null) clearTimeout(timer);
    timer = null;
  }
  const preview = useAppStore.getState().dragPathPreview;
  if (preview?.token === token) {
    useAppStore.setState({ dragPathPreview: committing ? {
      ...preview, result: { ...preview.result, frameLabel: "updating pose", paths: [] },
    } : null });
  }
}
