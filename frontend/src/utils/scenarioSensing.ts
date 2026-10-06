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

/** Compact run-row chip: "det 98% · ≥3 links 97% · med 0.41 m". */
export function formatSensingChip(sum: ScenarioSensingSummary): string {
  return (
    `det ${pct(sum.detection_rate)} · ≥3 links ${pct(sum.frames_ge3_links_rate)}` +
    ` · med ${meters(sum.median_position_error_m)}`
  );
}

/** Suffix for the scenario success notice: " · sensing det 98% · med err 0.41 m". */
export function formatSensingNotice(sum: ScenarioSensingSummary): string {
  return ` · sensing det ${pct(sum.detection_rate)} · med err ${meters(sum.median_position_error_m)}`;
}
