/**
 * Diverging Doppler palette for sensing echo paths: blue = receding
 * (negative Hz), gray = no radial motion, red = approaching (positive Hz).
 * Symmetric around 0 so equal |Doppler| reads as equal saturation.
 */

import type { RayPath } from "../types/api";

export const DOPPLER_NEGATIVE = "#2166ac";
export const DOPPLER_ZERO = "#9e9e9e";
export const DOPPLER_POSITIVE = "#b2182b";

function hexToRgb(hex: string): [number, number, number] {
  const v = parseInt(hex.slice(1), 16);
  return [(v >> 16) & 255, (v >> 8) & 255, v & 255];
}

const NEG = hexToRgb(DOPPLER_NEGATIVE);
const ZERO = hexToRgb(DOPPLER_ZERO);
const POS = hexToRgb(DOPPLER_POSITIVE);

function lerpRgb(a: [number, number, number], b: [number, number, number], t: number): string {
  const c = a.map((x, i) => Math.round(x + (b[i] - x) * t));
  return `#${c.map((x) => x.toString(16).padStart(2, "0")).join("")}`;
}

/** Color of one path's Doppler on a ±maxAbsHz scale (null/non-finite → gray). */
export function dopplerColor(hz: number | null | undefined, maxAbsHz: number): string {
  if (hz === null || hz === undefined || !Number.isFinite(hz)) return DOPPLER_ZERO;
  const scale = maxAbsHz > 0 ? maxAbsHz : 1;
  const t = 0.5 + 0.5 * Math.max(-1, Math.min(1, hz / scale));
  return t < 0.5 ? lerpRgb(NEG, ZERO, t / 0.5) : lerpRgb(ZERO, POS, (t - 0.5) / 0.5);
}

/** Symmetric legend range: max |doppler_hz| over the sensing paths, ≥ 1 Hz. */
export function sensingDopplerRange(paths: RayPath[]): number {
  let max = 0;
  for (const p of paths) {
    if (p.target_id == null || p.doppler_hz == null || !Number.isFinite(p.doppler_hz)) continue;
    max = Math.max(max, Math.abs(p.doppler_hz));
  }
  return Math.max(1, max);
}

/** Legend bar gradient: blue at the bottom (−max) to red at the top (+max). */
export function dopplerGradientCss(): string {
  return `linear-gradient(to top, ${DOPPLER_NEGATIVE}, ${DOPPLER_ZERO}, ${DOPPLER_POSITIVE})`;
}
