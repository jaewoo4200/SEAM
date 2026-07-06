/**
 * Viewport (lighting + scene helper) settings for the 3D viewer.
 *
 * These are a Unity/Blender-style set of viewport knobs — ambient/hemisphere/
 * directional light intensities, the directional light's azimuth/elevation +
 * color, background color, and toggles for the grid, axes and the textured
 * overlay backdrop. They are persisted per-project in localStorage under
 * 'stw.viewport.<pid>' and consumed by Viewer3D from the app store.
 *
 * Defaults mirror the values that were previously hardcoded in Viewer3D so a
 * fresh project looks identical to before this panel existed.
 */

import type { Vec3 } from "./types/api";

export interface ViewportSettings {
  ambientIntensity: number; // 0..2
  hemisphereIntensity: number; // 0..2
  directionalIntensity: number; // 0..3
  /** Directional light azimuth in degrees, -180..180 (0 = +X, CCW about +Z). */
  directionalAzimuthDeg: number;
  /** Directional light elevation in degrees, 0..90 (0 = horizon, 90 = zenith). */
  directionalElevationDeg: number;
  directionalColor: string;
  backgroundColor: string;
  showGrid: boolean;
  showAxes: boolean;
  showOverlay: boolean;
  /** Horizontal slice plane (Sionna RT GUI parity): clips scene meshes above
   *  sliceZ. Devices/paths/radio map stay visible. Toggle with 'S'. */
  showSlice: boolean;
  sliceZ: number;
  /** Device/actor marker size multiplier (1 = legacy size). */
  markerScale: number;
  /** Radio-map display: colormap + optional fixed dB range (null = auto). */
  rmColormap: RadioMapColormap;
  rmVmin: number | null;
  rmVmax: number | null;
  /** Render speed <-> quality preset (GPU-settings style). Only affects the
   *  RENDER (canvas resolution); picking/solves/BVH are exact regardless. */
  renderQuality: RenderQuality;
  /** Blender-style navigation: wheel zoom moves toward the cursor. */
  zoomToCursor: boolean;
  /** Blender-style "orbit around selection": selecting an object re-pivots
   *  the orbit target to it (F always frames the selection regardless). */
  orbitSelection: boolean;
  /** Distance fog (Blender viewport mist): far geometry fades into the
   *  background color. Range auto-scales with the scene. */
  fogEnabled: boolean;
}

export type RadioMapColormap = "jet" | "viridis" | "plasma" | "turbo";
export const RADIO_MAP_COLORMAPS: RadioMapColormap[] = ["jet", "viridis", "plasma", "turbo"];

export type RenderQuality = "performance" | "balanced" | "quality";
export const RENDER_QUALITIES: RenderQuality[] = ["performance", "balanced", "quality"];

/** Canvas device-pixel-ratio for a render-quality preset: the dominant cost of
 *  drawing a multi-million-triangle imported scene is fill/vertex work, which
 *  scales with resolution. Lossless data-wise - only the on-screen sharpness
 *  changes. */
export function renderQualityDpr(q: RenderQuality): number {
  const device = typeof window === "undefined" ? 1 : window.devicePixelRatio || 1;
  if (q === "performance") return Math.min(device, 0.75);
  if (q === "balanced") return Math.min(device, 1.25);
  return device;
}

/** Radius (m) of the sphere the directional light is placed on. */
export const DIRECTIONAL_RADIUS_M = 60;

/**
 * Defaults = the values Viewer3D previously hardcoded:
 *   ambientLight intensity 0.75
 *   hemisphereLight intensity 0.5
 *   directionalLight intensity 1.1 at position [30, -20, 50]
 *   background #0d1420
 * The default directional position [30, -20, 50] corresponds to
 * azimuth ≈ -33.7°, elevation ≈ 54.2° on any radius; we round to a clean
 * az=-34, el=54 so the sliders land on sensible integers while keeping the
 * light in essentially the same place.
 */
export function defaultViewportSettings(): ViewportSettings {
  return {
    ambientIntensity: 0.75,
    hemisphereIntensity: 0.5,
    directionalIntensity: 1.1,
    directionalAzimuthDeg: -34,
    directionalElevationDeg: 54,
    directionalColor: "#ffffff",
    backgroundColor: "#0d1420",
    showGrid: true,
    showAxes: true,
    showOverlay: true,
    showSlice: false,
    sliceZ: 2.0,
    markerScale: 2.0,
    rmColormap: "jet",
    rmVmin: null,
    rmVmax: null,
    renderQuality: "quality",
    zoomToCursor: true,
    orbitSelection: false,
    fogEnabled: false,
  };
}

/**
 * Derive the directional light's world position (Z-up ENU) from its azimuth +
 * elevation on a fixed radius. Azimuth 0 points along +X and rotates CCW about
 * +Z; elevation lifts toward +Z.
 */
export function directionalPosition(
  azimuthDeg: number,
  elevationDeg: number,
  radius = DIRECTIONAL_RADIUS_M,
): Vec3 {
  const az = (azimuthDeg * Math.PI) / 180;
  const el = (elevationDeg * Math.PI) / 180;
  const cosEl = Math.cos(el);
  return [
    radius * cosEl * Math.cos(az),
    radius * cosEl * Math.sin(az),
    radius * Math.sin(el),
  ];
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

const HEX_RE = /^#[0-9a-fA-F]{6}$/;

/** A viewport settings value coerced back into range/format after a load. */
export function normalizeViewportSettings(
  raw: Partial<ViewportSettings> | null | undefined,
): ViewportSettings {
  const d = defaultViewportSettings();
  if (!raw) return d;
  const color = (v: unknown, fallback: string): string =>
    typeof v === "string" && HEX_RE.test(v) ? v : fallback;
  const num = (v: unknown, fallback: number, lo: number, hi: number): number => {
    const n = Number(v);
    return Number.isFinite(n) ? clamp(n, lo, hi) : fallback;
  };
  const bool = (v: unknown, fallback: boolean): boolean =>
    typeof v === "boolean" ? v : fallback;
  return {
    ambientIntensity: num(raw.ambientIntensity, d.ambientIntensity, 0, 2),
    hemisphereIntensity: num(raw.hemisphereIntensity, d.hemisphereIntensity, 0, 2),
    directionalIntensity: num(raw.directionalIntensity, d.directionalIntensity, 0, 3),
    directionalAzimuthDeg: num(raw.directionalAzimuthDeg, d.directionalAzimuthDeg, -180, 180),
    directionalElevationDeg: num(raw.directionalElevationDeg, d.directionalElevationDeg, 0, 90),
    directionalColor: color(raw.directionalColor, d.directionalColor),
    backgroundColor: color(raw.backgroundColor, d.backgroundColor),
    showGrid: bool(raw.showGrid, d.showGrid),
    showAxes: bool(raw.showAxes, d.showAxes),
    showOverlay: bool(raw.showOverlay, d.showOverlay),
    showSlice: bool(raw.showSlice, d.showSlice),
    markerScale: num(raw.markerScale, d.markerScale, 0.3, 6),
    sliceZ: num(raw.sliceZ, d.sliceZ, -1000, 10000),
    rmColormap: RADIO_MAP_COLORMAPS.includes(raw.rmColormap as RadioMapColormap)
      ? (raw.rmColormap as RadioMapColormap)
      : d.rmColormap,
    rmVmin: raw.rmVmin === null || raw.rmVmin === undefined || !Number.isFinite(Number(raw.rmVmin))
      ? d.rmVmin
      : Number(raw.rmVmin),
    rmVmax: raw.rmVmax === null || raw.rmVmax === undefined || !Number.isFinite(Number(raw.rmVmax))
      ? d.rmVmax
      : Number(raw.rmVmax),
    renderQuality: RENDER_QUALITIES.includes(raw.renderQuality as RenderQuality)
      ? (raw.renderQuality as RenderQuality)
      : d.renderQuality,
    zoomToCursor: bool(raw.zoomToCursor, d.zoomToCursor),
    orbitSelection: bool(raw.orbitSelection, d.orbitSelection),
    fogEnabled: bool(raw.fogEnabled, d.fogEnabled),
  };
}

function storageKey(projectId: string): string {
  return `stw.viewport.${projectId}`;
}

/** Read + normalize persisted viewport settings for a project. */
/** True when the project has persisted viewport settings (used to decide
 *  whether env-derived defaults like sliceZ may be applied). */
export function hasViewportSettings(projectId: string): boolean {
  try {
    return localStorage.getItem(storageKey(projectId)) !== null;
  } catch {
    return false;
  }
}

export function loadViewportSettings(projectId: string): ViewportSettings {
  try {
    const raw = localStorage.getItem(storageKey(projectId));
    if (!raw) return defaultViewportSettings();
    return normalizeViewportSettings(JSON.parse(raw) as Partial<ViewportSettings>);
  } catch {
    return defaultViewportSettings();
  }
}

/** Persist viewport settings for a project (best-effort; ignores storage errors). */
export function saveViewportSettings(projectId: string, settings: ViewportSettings): void {
  try {
    localStorage.setItem(storageKey(projectId), JSON.stringify(settings));
  } catch {
    // storage may be unavailable (private mode); settings still apply in-session
  }
}
