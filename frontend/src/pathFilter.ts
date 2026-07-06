/**
 * Shared ray-path filtering + coloring so the 3D viewer and the results table
 * stay in sync (store-driven, guide item 3).
 */

import { PATH_COLORS } from "./components/common";
import type { ColorBy } from "./store/appStore";
import type { PathType, RayPath } from "./types/api";

export interface PathFilterParams {
  pathTypeFilter: PathType | "all";
  strongestN: number;
  minPowerDbm: number | null;
  // Device ids whose links are hidden (AODT-style per-TX/RX filter chips).
  hiddenLinkDevices?: string[];
  // RF material ids to keep (empty = all). A path passes if ANY of its
  // interactions hit a material in this set (interaction-material chips).
  materialFilter?: string[];
}

/**
 * Apply type filter -> min-power threshold -> keep the strongest N by power
 * PER TX->RX LINK. A global top-N would let one strong short link crowd out
 * every path of weaker links in multi-TX/RX scenes (reported as "rays missing
 * in Results with multiple devices"); per-link capping keeps each link
 * represented. Returned order is strongest-first within the concatenation.
 */
export function filterPaths(paths: RayPath[], p: PathFilterParams): RayPath[] {
  let out = paths;
  if (p.hiddenLinkDevices && p.hiddenLinkDevices.length > 0) {
    const hidden = new Set(p.hiddenLinkDevices);
    out = out.filter((path) => !hidden.has(path.tx_id) && !hidden.has(path.rx_id));
  }
  if (p.pathTypeFilter !== "all") {
    out = out.filter((path) => path.path_type === p.pathTypeFilter);
  }
  if (p.materialFilter && p.materialFilter.length > 0) {
    const keep = new Set(p.materialFilter);
    out = out.filter((path) =>
      path.interactions.some((it) => it.rf_material_id !== null && keep.has(it.rf_material_id)),
    );
  }
  if (p.minPowerDbm !== null) {
    const min = p.minPowerDbm;
    out = out.filter((path) => path.power_dbm >= min);
  }
  const byLink = new Map<string, RayPath[]>();
  for (const path of out) {
    const key = `${path.tx_id}|${path.rx_id}`;
    const bucket = byLink.get(key);
    if (bucket) bucket.push(path);
    else byLink.set(key, [path]);
  }
  const result: RayPath[] = [];
  for (const bucket of byLink.values()) {
    bucket.sort((a, b) => b.power_dbm - a.power_dbm);
    result.push(
      ...(p.strongestN > 0 && bucket.length > p.strongestN
        ? bucket.slice(0, p.strongestN)
        : bucket),
    );
  }
  return result;
}

/** Min/max power over a set of paths (finite fallback when empty). */
export function powerRange(paths: RayPath[]): { min: number; max: number } {
  let min = Infinity;
  let max = -Infinity;
  for (const p of paths) {
    if (p.power_dbm < min) min = p.power_dbm;
    if (p.power_dbm > max) max = p.power_dbm;
  }
  if (!Number.isFinite(min)) return { min: -120, max: 0 };
  if (max - min < 1e-9) max = min + 1;
  return { min, max };
}

// Jet colormap (blue -> cyan -> green -> yellow -> red), matching the viewer's
// radio-map palette so "color by power" reads consistently.
const JET: [number, number, number][] = [
  [0, 0, 131],
  [0, 60, 170],
  [5, 255, 255],
  [255, 255, 0],
  [250, 0, 0],
  [128, 0, 0],
];

export function jetCss(t: number): string {
  const x = Math.min(1, Math.max(0, t)) * (JET.length - 1);
  const i = Math.floor(x);
  const f = x - i;
  const a = JET[i];
  const b = JET[Math.min(i + 1, JET.length - 1)];
  return `rgb(${Math.round(a[0] + (b[0] - a[0]) * f)}, ${Math.round(
    a[1] + (b[1] - a[1]) * f,
  )}, ${Math.round(a[2] + (b[2] - a[2]) * f)})`;
}

// Discrete depth palette: 0 cyan, 1 magenta, 2 orange, 3+ red.
const DEPTH_COLORS = ["#00e5ff", "#ff00ff", "#ff9800", "#ef5350"];

/** Interaction depth = number of surface interactions along the path. */
export function pathDepth(path: RayPath): number {
  return path.interactions.length;
}

export function depthColor(depth: number): string {
  return DEPTH_COLORS[Math.min(depth, DEPTH_COLORS.length - 1)];
}

/** Color for a path under the current color-by mode. */
export function pathColor(
  path: RayPath,
  colorBy: ColorBy,
  range: { min: number; max: number },
): string {
  if (colorBy === "power") {
    return jetCss((path.power_dbm - range.min) / (range.max - range.min));
  }
  if (colorBy === "depth") {
    return depthColor(pathDepth(path));
  }
  return PATH_COLORS[path.path_type];
}

/** Line width (1..4) mapped from the path power over the visible range. */
export function powerWidth(
  path: RayPath,
  range: { min: number; max: number },
): number {
  const t = (path.power_dbm - range.min) / (range.max - range.min);
  return 1 + Math.min(1, Math.max(0, t)) * 3;
}
