/** Multi-UE trajectory helpers.
 *
 * A TrajectoryResultSet's samples are STEP-MAJOR when several UEs are routed
 * together: [step0-ue0, step0-ue1, ..., step1-ue0, ...] (backend contract).
 * Single-UE results are the degenerate 1-UE case, so every consumer
 * (playback slider, viewer overlay, metrics charts) goes through these
 * helpers instead of assuming one sample per frame.
 */

import type { TrajectoryResultSet, TrajectorySample } from "./types/api";

/** Distinct UE ids in first-appearance order (routes order). */
export function trajectoryUeIds(t: TrajectoryResultSet | null): string[] {
  if (!t) return [];
  const ids: string[] = [];
  for (const s of t.samples) {
    if (!ids.includes(s.ue_id)) ids.push(s.ue_id);
    else break; // step-major: the first repeat means we've seen them all
  }
  return ids;
}

/** Number of playback steps (frames). */
export function trajectorySteps(t: TrajectoryResultSet | null): number {
  if (!t || t.samples.length === 0) return 0;
  const n = Math.max(1, trajectoryUeIds(t).length);
  return Math.ceil(t.samples.length / n);
}

/** All UEs' samples at one step (length = #UEs; clamped to the last step). */
export function samplesAtStep(
  t: TrajectoryResultSet | null,
  step: number,
): TrajectorySample[] {
  if (!t || t.samples.length === 0) return [];
  const n = Math.max(1, trajectoryUeIds(t).length);
  const steps = Math.ceil(t.samples.length / n);
  const s = Math.max(0, Math.min(steps - 1, step));
  return t.samples.slice(s * n, s * n + n);
}

/** One UE's sample series over every step (for charts/trails). */
export function samplesForUe(
  t: TrajectoryResultSet | null,
  ueId: string,
): TrajectorySample[] {
  if (!t) return [];
  return t.samples.filter((s) => s.ue_id === ueId);
}
