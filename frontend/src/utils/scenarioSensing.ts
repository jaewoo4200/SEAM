/**
 * Read helpers for sensing-over-time scenario results (frame.sensing +
 * metadata.sensing). Everything returns empty / null for scenarios run
 * without sensing, so callers can gate their UI on the result alone.
 */

import type {
  RayPath,
  ScenarioResultSet,
  ScenarioSensingSummary,
  TargetEstimate,
  Vec3,
} from "../types/api";

/** metadata.sensing when it has the run-summary shape, else null. */
export function scenarioSensingSummary(
  s: ScenarioResultSet | null | undefined,
): ScenarioSensingSummary | null {
  const raw = s?.metadata?.["sensing"];
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null;
  const r = raw as Partial<ScenarioSensingSummary>;
  if (!Array.isArray(r.target_ids) || !r.targets || typeof r.targets !== "object") return null;
  return r as ScenarioSensingSummary;
}

// Keyed on the result object: the viewer layer and the legend share one
// array per loaded scenario, so the Doppler scale stays stable across frames.
const echoCache = new WeakMap<ScenarioResultSet, RayPath[]>();

/** Every frame's target echoes (target_id set), in frame order. */
export function scenarioEchoes(s: ScenarioResultSet | null | undefined): RayPath[] {
  if (!s) return [];
  const hit = echoCache.get(s);
  if (hit) return hit;
  const out: RayPath[] = [];
  for (const f of s.frames) {
    for (const p of f.sensing?.echoes ?? []) if (p.target_id != null) out.push(p);
  }
  echoCache.set(s, out);
  return out;
}

function frameEstimate(
  s: ScenarioResultSet,
  frameIdx: number,
  targetId: string,
): TargetEstimate | undefined {
  return s.frames[frameIdx]?.sensing?.estimates.find((e) => e.target_id === targetId);
}

/** Estimated track of one target over frames 0..upToFrame: ok-frame
 *  position_est points, split into a new segment wherever a frame is not ok. */
export function estimateTrackSegments(
  s: ScenarioResultSet,
  targetId: string,
  upToFrame: number,
): Vec3[][] {
  const segments: Vec3[][] = [];
  let current: Vec3[] = [];
  const last = Math.min(upToFrame, s.frames.length - 1);
  for (let i = 0; i <= last; i++) {
    const e = frameEstimate(s, i, targetId);
    if (e && e.status === "ok" && e.position_est) {
      current.push(e.position_est);
    } else if (current.length > 0) {
      segments.push(current);
      current = [];
    }
  }
  if (current.length > 0) segments.push(current);
  return segments;
}

export interface TrackSegment {
  points: Vec3[];
  /** Every edge of this run ends in a coasting (prediction-only) frame. */
  dashed: boolean;
}

/** EKF track of one target over frames 0..upToFrame through track_position.
 *  An edge into a coasting frame is dashed; none / lost (or a missing state)
 *  break the line, and an init starts a new track (not joined to the old). */
export function filteredTrackSegments(
  s: ScenarioResultSet,
  targetId: string,
  upToFrame: number,
): TrackSegment[] {
  const out: TrackSegment[] = [];
  let cur: TrackSegment | null = null;
  const flush = () => {
    if (cur && cur.points.length > 1) out.push(cur);
    cur = null;
  };
  const last = Math.min(upToFrame, s.frames.length - 1);
  for (let i = 0; i <= last; i++) {
    const e = frameEstimate(s, i, targetId);
    const st = e?.track_status;
    const pos = e?.track_position;
    if (!pos || st == null || st === "none" || st === "lost") {
      flush();
      continue;
    }
    if (st === "init" || cur === null) {
      flush();
      cur = { points: [pos], dashed: st === "coasting" };
      continue;
    }
    const dashed = st === "coasting";
    const c: TrackSegment = cur;
    if (c.dashed === dashed || c.points.length === 1) {
      c.dashed = dashed;
      c.points.push(pos);
    } else {
      const prev = c.points[c.points.length - 1];
      flush();
      cur = { points: [prev, pos], dashed };
    }
  }
  flush();
  return out;
}

/** True when any frame of the run carries an EKF track state. */
export function scenarioHasTracking(s: ScenarioResultSet | null | undefined): boolean {
  return !!s?.frames.some((f) =>
    f.sensing?.estimates.some((e) => e.track_status != null),
  );
}

/** True target positions (cuboid centers) over frames 0..upToFrame. */
export function trueTrack(s: ScenarioResultSet, targetId: string, upToFrame: number): Vec3[] {
  const out: Vec3[] = [];
  const last = Math.min(upToFrame, s.frames.length - 1);
  for (let i = 0; i <= last; i++) {
    const e = frameEstimate(s, i, targetId);
    if (e) out.push(e.position_true);
  }
  return out;
}

function pct(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? "—" : `${Math.round(v * 100)}%`;
}

function meters(v: number | null | undefined, digits = 2): string {
  return v == null || !Number.isFinite(v) ? "—" : `${v.toFixed(digits)} m`;
}

/** " · EKF med 0.30 m" when the run tracked, else "". */
function trackSuffix(sum: ScenarioSensingSummary): string {
  return "median_track_position_error_m" in sum
    ? ` · EKF med ${meters(sum.median_track_position_error_m)}`
    : "";
}

/** Compact run-row chip: "det 98% · ≥3 links 97% · med 0.41 m". */
export function formatSensingChip(sum: ScenarioSensingSummary): string {
  return (
    `det ${pct(sum.detection_rate)} · ≥3 links ${pct(sum.frames_ge3_links_rate)}` +
    ` · med ${meters(sum.median_position_error_m)}` +
    trackSuffix(sum)
  );
}

/** Suffix for the scenario success notice: " · sensing det 98% · med err 0.41 m". */
export function formatSensingNotice(sum: ScenarioSensingSummary): string {
  return (
    ` · sensing det ${pct(sum.detection_rate)} · med err ${meters(sum.median_position_error_m)}` +
    trackSuffix(sum)
  );
}
