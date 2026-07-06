/** Dockable-panel layout persistence (photo-editor style attach/detach).
 *
 * Which side each registered panel is docked to ('left' | 'right' | 'float'),
 * its floating-window rect, and the float stacking order. Hand-persisted to
 * localStorage like viewportSettings.ts (no zustand middleware, matching the
 * codebase convention). Layout is GLOBAL workspace chrome — one key, no
 * projectId — unlike per-project viewport settings.
 */

export type DockTarget = "left" | "right" | "float";

export interface FloatRect {
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface PanelPlacement {
  dock: DockTarget;
  /** Last floating geometry; remembered even while docked. */
  float: FloatRect;
}

export type PanelLayout = Record<string, PanelPlacement>;

export interface PanelLayoutState {
  layout: PanelLayout;
  /** Panel ids in stacking order; last = topmost floating window. */
  z: string[];
}

/** Panels that can be detached. The registry lives in PanelHost.tsx; ids and
 *  default docks are declared here so normalization has a dependency-free
 *  source of truth. */
export const DOCKABLE_PANELS: { id: string; defaultDock: DockTarget }[] = [
  { id: "metrics", defaultDock: "right" },
  { id: "trajectory", defaultDock: "right" },
  { id: "scenario", defaultDock: "right" },
  { id: "mlDataset", defaultDock: "right" },
  { id: "channel", defaultDock: "right" },
];

const KEY = "stw.panelLayout.v1";
export const FLOAT_MIN_W = 260;
export const FLOAT_MIN_H = 140;

function defaultRect(index: number): FloatRect {
  // Cascade new floats down-right from the top-left of the viewer area.
  return { x: 320 + index * 32, y: 90 + index * 32, w: 380, h: 420 };
}

export function clampRect(r: FloatRect): FloatRect {
  const vw = typeof window !== "undefined" ? window.innerWidth : 1280;
  const vh = typeof window !== "undefined" ? window.innerHeight : 800;
  const w = Math.min(Math.max(r.w, FLOAT_MIN_W), vw);
  const h = Math.min(Math.max(r.h, FLOAT_MIN_H), vh);
  // Keep at least 80px of the title bar horizontally and the full bar height
  // vertically reachable so a float can never be lost off-screen.
  const x = Math.min(Math.max(r.x, -(w - 80)), vw - 80);
  const y = Math.min(Math.max(r.y, 40), vh - 32);
  return { x, y, w, h };
}

function isRect(v: unknown): v is FloatRect {
  const r = v as FloatRect;
  return (
    !!r &&
    Number.isFinite(r.x) &&
    Number.isFinite(r.y) &&
    Number.isFinite(r.w) &&
    Number.isFinite(r.h)
  );
}

/** Registry-driven normalization: stored ids not in DOCKABLE_PANELS are
 *  dropped; registry ids missing from storage get defaults (new panels appear
 *  docked). Every field is defensively coerced. */
export function normalizePanelLayout(raw: unknown): PanelLayoutState {
  const rawLayout =
    raw && typeof raw === "object" && "layout" in (raw as object)
      ? (raw as { layout?: unknown }).layout
      : undefined;
  const rawZ =
    raw && typeof raw === "object" && "z" in (raw as object)
      ? (raw as { z?: unknown }).z
      : undefined;

  const layout: PanelLayout = {};
  DOCKABLE_PANELS.forEach((def, i) => {
    const entry = (rawLayout as Record<string, Partial<PanelPlacement>> | undefined)?.[def.id];
    const dock =
      entry && (entry.dock === "left" || entry.dock === "right" || entry.dock === "float")
        ? entry.dock
        : def.defaultDock;
    const float = entry && isRect(entry.float) ? clampRect(entry.float) : defaultRect(i);
    layout[def.id] = { dock, float };
  });

  const known = new Set(DOCKABLE_PANELS.map((d) => d.id));
  const z = Array.isArray(rawZ) ? rawZ.filter((id): id is string => known.has(id as string)) : [];
  for (const def of DOCKABLE_PANELS) if (!z.includes(def.id)) z.push(def.id);
  return { layout, z };
}

export function loadPanelLayout(): PanelLayoutState {
  try {
    const raw = localStorage.getItem(KEY);
    return normalizePanelLayout(raw ? JSON.parse(raw) : null);
  } catch {
    return normalizePanelLayout(null);
  }
}

export function savePanelLayout(state: PanelLayoutState): void {
  try {
    localStorage.setItem(KEY, JSON.stringify(state));
  } catch {
    // Storage full/blocked: layout just won't persist this session.
  }
}
