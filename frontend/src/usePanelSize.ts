/**
 * usePanelSize — drag-resizable left/right sidebar widths for the app shell.
 *
 * Widths live in component state (not the global store) and are persisted to
 * localStorage under 'stw.panelWidths' so they survive reloads. The hook hands
 * back:
 *   - `left` / `right`      current widths in px (clamped to the allowed range)
 *   - `startDrag(side, e)`  pointer-down handler for a drag handle
 *   - `reset(side)`         double-click handler that restores the default
 *   - `dragging`            which handle is being dragged (for hover styling)
 *
 * The drag uses pointer capture on the window so the gesture keeps tracking
 * even when the cursor leaves the thin handle strip or moves over the canvas.
 * Three-fiber's own ResizeObserver handles the canvas resizing that results
 * from the grid columns changing, so we don't touch the canvas here.
 */

import { useCallback, useEffect, useRef, useState } from "react";

export const LEFT_MIN = 200;
export const LEFT_MAX = 480;
export const LEFT_DEFAULT = 280;

export const RIGHT_MIN = 260;
export const RIGHT_MAX = 640;
export const RIGHT_DEFAULT = 340;

const STORAGE_KEY = "stw.panelWidths";

export type PanelSide = "left" | "right";

interface PanelWidths {
  left: number;
  right: number;
}

function clamp(v: number, lo: number, hi: number): number {
  return Math.max(lo, Math.min(hi, v));
}

function clampSide(side: PanelSide, v: number): number {
  return side === "left"
    ? clamp(v, LEFT_MIN, LEFT_MAX)
    : clamp(v, RIGHT_MIN, RIGHT_MAX);
}

function defaultWidths(): PanelWidths {
  return { left: LEFT_DEFAULT, right: RIGHT_DEFAULT };
}

/** Read + clamp persisted widths; fall back to defaults on any parse trouble. */
function loadWidths(): PanelWidths {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return defaultWidths();
    const parsed = JSON.parse(raw) as Partial<PanelWidths>;
    return {
      left: clampSide("left", Number(parsed.left ?? LEFT_DEFAULT)),
      right: clampSide("right", Number(parsed.right ?? RIGHT_DEFAULT)),
    };
  } catch {
    return defaultWidths();
  }
}

export interface UsePanelSize {
  left: number;
  right: number;
  dragging: PanelSide | null;
  startDrag: (side: PanelSide, e: React.PointerEvent) => void;
  reset: (side: PanelSide) => void;
}

export function usePanelSize(): UsePanelSize {
  const [widths, setWidths] = useState<PanelWidths>(loadWidths);
  const [dragging, setDragging] = useState<PanelSide | null>(null);

  // Live drag state kept in a ref so the window listeners (registered once per
  // drag) always read fresh anchor values without re-subscribing on every move.
  const drag = useRef<{
    side: PanelSide;
    startX: number;
    startWidth: number;
  } | null>(null);

  // Persist on change (clamped values only ever reach state, so this is safe).
  useEffect(() => {
    try {
      localStorage.setItem(STORAGE_KEY, JSON.stringify(widths));
    } catch {
      // storage may be unavailable (private mode); widths still work in-session
    }
  }, [widths]);

  useEffect(() => {
    if (dragging === null) return;

    function onMove(e: PointerEvent): void {
      const d = drag.current;
      if (!d) return;
      // Left handle grows the panel as the cursor moves right; the right handle
      // is a mirror (panel is anchored to the viewport's right edge).
      const delta = e.clientX - d.startX;
      const raw = d.side === "left" ? d.startWidth + delta : d.startWidth - delta;
      const next = clampSide(d.side, raw);
      setWidths((w) => (w[d.side] === next ? w : { ...w, [d.side]: next }));
    }

    function onUp(): void {
      drag.current = null;
      setDragging(null);
    }

    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
    return () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
    };
  }, [dragging]);

  const startDrag = useCallback(
    (side: PanelSide, e: React.PointerEvent) => {
      e.preventDefault();
      drag.current = {
        side,
        startX: e.clientX,
        startWidth: side === "left" ? widths.left : widths.right,
      };
      setDragging(side);
    },
    [widths.left, widths.right],
  );

  const reset = useCallback((side: PanelSide) => {
    setWidths((w) => ({
      ...w,
      [side]: side === "left" ? LEFT_DEFAULT : RIGHT_DEFAULT,
    }));
  }, []);

  return { left: widths.left, right: widths.right, dragging, startDrag, reset };
}
