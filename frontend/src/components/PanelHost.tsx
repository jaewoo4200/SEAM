/** Dockable/floating panel shell (photo-editor style attach/detach).
 *
 * Registered panels can live in the right sidebar (default), the left
 * sidebar, or float over the viewport as draggable, resizable windows.
 * Floating and left-docked panels survive mode-tab switches (they are hosted
 * here, not inside the mode-switched right panel), so e.g. the Trajectory
 * panel can stay open while editing the scene in Visual mode.
 *
 * No dependencies: drag/resize use pointer capture (same pattern as
 * usePanelSize), persistence is hand-rolled in panelLayout.ts.
 */

import { useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { useAppStore } from "../store/appStore";
import {
  clampRect,
  FLOAT_MIN_H,
  FLOAT_MIN_W,
} from "../panelLayout";
import type { DockTarget, FloatRect } from "../panelLayout";
import ChannelPanel from "./ChannelPanel";
import MetricsPanel from "./MetricsPanel";
import {
  MlDatasetSection,
  ScenarioSection,
  TrajectorySection,
} from "./ResultExplorer";

export interface PanelDef {
  id: string;
  title: string;
  render: () => ReactNode;
}

/** Order here is the default vertical order in the sidebar. Ids must match
 *  DOCKABLE_PANELS in panelLayout.ts (the persistence source of truth). */
export const PANEL_REGISTRY: PanelDef[] = [
  { id: "metrics", title: "Metrics dashboard", render: () => <MetricsPanel /> },
  { id: "channel", title: "Channel analysis", render: () => <ChannelPanel /> },
  { id: "trajectory", title: "UE trajectory", render: () => <TrajectorySection /> },
  { id: "scenario", title: "Scenario playback", render: () => <ScenarioSection /> },
  { id: "mlDataset", title: "ML dataset", render: () => <MlDatasetSection /> },
];

/** Docked wrapper: title bar with detach/move controls around the content. */
function PanelCard({ def }: { def: PanelDef }) {
  const projectId = useAppStore((s) => s.projectId);
  const setPanelDock = useAppStore((s) => s.setPanelDock);
  const dock = useAppStore((s) => s.panelLayout[def.id]?.dock ?? "right");
  const [collapsed, setCollapsed] = useState(false);
  return (
    <div className="dock-card">
      <div className="dock-card-header">
        <button
          className="dock-card-collapse"
          onClick={() => setCollapsed((c) => !c)}
          title={collapsed ? "Expand" : "Collapse"}
        >
          {collapsed ? "▸" : "▾"}
        </button>
        <span className="dock-card-title">{def.title}</span>
        <span className="dock-card-actions">
          {dock !== "left" && (
            <button title="Dock to left sidebar" onClick={() => setPanelDock(def.id, "left")}>
              ◧
            </button>
          )}
          {dock !== "right" && (
            <button title="Dock to right sidebar" onClick={() => setPanelDock(def.id, "right")}>
              ◨
            </button>
          )}
          <button title="Detach as floating window" onClick={() => setPanelDock(def.id, "float")}>
            ⧉
          </button>
        </span>
      </div>
      {/* display:none (not unmount): collapsing must not kill playback
          timers or the trajectory preview publisher (audit B3). */}
      <div key={projectId ?? "none"} style={collapsed ? { display: "none" } : undefined}>
        {def.render()}
      </div>
    </div>
  );
}

/** Renders the registry panels docked to one sidebar side, in registry order. */
export function PanelHost({ side }: { side: "left" | "right" }) {
  const layout = useAppStore((s) => s.panelLayout);
  const panels = PANEL_REGISTRY.filter((d) => (layout[d.id]?.dock ?? "right") === side);
  if (panels.length === 0) return null;
  return (
    <>
      {panels.map((def) => (
        <PanelCard key={def.id} def={def} />
      ))}
    </>
  );
}

/** One floating window: pointer-capture drag on the title bar, resize grip,
 *  z-raise on any pointerdown, viewport clamping on commit + window resize. */
function FloatingWindow({ def }: { def: PanelDef }) {
  const projectId = useAppStore((s) => s.projectId);
  const rect = useAppStore((s) => s.panelLayout[def.id]?.float) ?? {
    x: 320,
    y: 90,
    w: 380,
    h: 420,
  };
  const zIndex = useAppStore((s) => 40 + s.panelZ.indexOf(def.id));
  const setPanelDock = useAppStore((s) => s.setPanelDock);
  const setPanelFloatRect = useAppStore((s) => s.setPanelFloatRect);
  const raisePanel = useAppStore((s) => s.raisePanel);
  // Live rect during a drag/resize (committed to the store on pointerup).
  const [live, setLive] = useState<FloatRect | null>(null);
  const gesture = useRef<{
    kind: "move" | "resize";
    startX: number;
    startY: number;
    orig: FloatRect;
  } | null>(null);
  const liveRef = useRef<FloatRect | null>(null);
  liveRef.current = live;

  // Window-level listeners during a gesture (registered in begin, removed on
  // up) so the drag keeps tracking when the cursor leaves the title bar —
  // no pointer capture needed, which also keeps synthetic/CDP input working.
  const begin = (kind: "move" | "resize") => (e: React.PointerEvent) => {
    if (e.button !== 0 || gesture.current) return;
    e.preventDefault();
    gesture.current = { kind, startX: e.clientX, startY: e.clientY, orig: rect };
    const onMove = (ev: PointerEvent) => {
      const g = gesture.current;
      if (!g) return;
      const dx = ev.clientX - g.startX;
      const dy = ev.clientY - g.startY;
      const next =
        g.kind === "move"
          ? { ...g.orig, x: g.orig.x + dx, y: g.orig.y + dy }
          : {
              ...g.orig,
              w: Math.max(FLOAT_MIN_W, g.orig.w + dx),
              h: Math.max(FLOAT_MIN_H, g.orig.h + dy),
            };
      // Ref updated synchronously: a pointerup in the same frame as the last
      // move must commit this rect, not the last RENDERED one.
      liveRef.current = next;
      setLive(next);
    };
    const onUp = () => {
      window.removeEventListener("pointermove", onMove);
      window.removeEventListener("pointerup", onUp);
      window.removeEventListener("pointercancel", onUp);
      if (!gesture.current) return;
      gesture.current = null;
      const final = liveRef.current;
      setLive(null);
      if (final) setPanelFloatRect(def.id, final);
    };
    window.addEventListener("pointermove", onMove);
    window.addEventListener("pointerup", onUp);
    window.addEventListener("pointercancel", onUp);
  };

  const r = live ?? rect;
  return (
    <div
      className="floating-window"
      style={{ left: r.x, top: r.y, width: r.w, height: r.h, zIndex }}
      onPointerDown={() => raisePanel(def.id)}
    >
      <div className="fw-title" onPointerDown={begin("move")}>
        <span>{def.title}</span>
        <span className="fw-actions">
          <button title="Dock to left sidebar" onPointerDown={(e) => e.stopPropagation()} onClick={() => setPanelDock(def.id, "left")}>
            ◧
          </button>
          <button title="Dock to right sidebar" onPointerDown={(e) => e.stopPropagation()} onClick={() => setPanelDock(def.id, "right")}>
            ◨
          </button>
        </span>
      </div>
      <div className="fw-body" key={projectId ?? "none"}>
        {def.render()}
      </div>
      <div className="fw-resize" title="Drag to resize" onPointerDown={begin("resize")} />
    </div>
  );
}

/** Overlay layer for all floating panels. pointer-events:none on the layer,
 *  auto on each window, so the gaps still orbit the camera. Re-clamps every
 *  float when the browser window shrinks so title bars stay reachable. */
export function FloatingLayer() {
  const layout = useAppStore((s) => s.panelLayout);
  const floats = PANEL_REGISTRY.filter((d) => layout[d.id]?.dock === "float");

  useEffect(() => {
    const onResize = () => {
      const { panelLayout, setPanelFloatRect } = useAppStore.getState();
      for (const def of PANEL_REGISTRY) {
        const p = panelLayout[def.id];
        if (p?.dock !== "float") continue;
        const clamped = clampRect(p.float);
        if (
          clamped.x !== p.float.x ||
          clamped.y !== p.float.y ||
          clamped.w !== p.float.w ||
          clamped.h !== p.float.h
        ) {
          setPanelFloatRect(def.id, clamped);
        }
      }
    };
    window.addEventListener("resize", onResize);
    return () => window.removeEventListener("resize", onResize);
  }, []);

  if (floats.length === 0) return null;
  return (
    <div className="floating-layer">
      {floats.map((def) => (
        <FloatingWindow key={def.id} def={def} />
      ))}
    </div>
  );
}

export type { DockTarget };
