import { useEffect } from "react";
import { useAppStore } from "./store/appStore";
import { usePanelSize } from "./usePanelSize";
import type { PanelSide } from "./usePanelSize";
import Toolbar from "./components/Toolbar";
import SceneTree from "./components/SceneTree";
import Viewer3D from "./components/Viewer3D";
import InspectorPanel from "./components/InspectorPanel";
import RFMaterialPanel from "./components/RFMaterialPanel";
import ValidationPanel from "./components/ValidationPanel";
import AISuggestionPanel from "./components/AISuggestionPanel";
import ResultExplorer from "./components/ResultExplorer";
import SolverControls from "./components/SolverControls";
import { FloatingLayer, PanelHost } from "./components/PanelHost";

// Guard against React 18 StrictMode double-mount kicking off two boots.
let booted = false;

export default function App() {
  const init = useAppStore((s) => s.init);
  const projects = useAppStore((s) => s.projects);
  const projectId = useAppStore((s) => s.projectId);
  const mode = useAppStore((s) => s.mode);
  const busy = useAppStore((s) => s.busy);
  const error = useAppStore((s) => s.error);
  const notice = useAppStore((s) => s.notice);
  const dismissError = useAppStore((s) => s.dismissError);
  const dismissNotice = useAppStore((s) => s.dismissNotice);
  const setImportOpen = useAppStore((s) => s.setImportOpen);
  const undo = useAppStore((s) => s.undo);
  const undoDepth = useAppStore((s) => s.undoDepth);
  const solveProgress = useAppStore((s) => s.solveProgress);
  const cancelSolve = useAppStore((s) => s.cancelSolve);

  const panel = usePanelSize();

  useEffect(() => {
    if (!booted) {
      booted = true;
      void init();
    }
  }, [init]);

  // Success notices auto-dismiss; errors stay until acted on (store contract).
  useEffect(() => {
    if (!notice) return;
    const t = setTimeout(() => dismissNotice(), 5000);
    return () => clearTimeout(t);
  }, [notice, dismissNotice]);

  // Global keyboard shortcuts: Ctrl/Cmd+Z undoes the last scene edit; Delete /
  // Backspace removes the selected device or actor. Both are suppressed while a
  // form field is focused so typing (and the browser's own undo) is untouched.
  useEffect(() => {
    function isEditableTarget(t: EventTarget | null): boolean {
      const el = t as HTMLElement | null;
      if (!el) return false;
      const tag = el.tagName;
      return (
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        tag === "SELECT" ||
        el.isContentEditable === true
      );
    }
    function onKeyDown(e: KeyboardEvent): void {
      if (isEditableTarget(e.target)) return;
      const state = useAppStore.getState();
      if ((e.ctrlKey || e.metaKey) && !e.shiftKey && (e.key === "z" || e.key === "Z")) {
        e.preventDefault();
        void state.undo();
      } else if (e.key === "Delete" || e.key === "Backspace") {
        // Remove the selected radio device / actor (geometry prims are not
        // deletable — they are the imported scene mesh).
        if (state.selectedDeviceId) {
          e.preventDefault();
          void state.deleteDevice(state.selectedDeviceId);
        } else if (state.selectedActorId) {
          e.preventDefault();
          void state.deleteActor(state.selectedActorId);
        }
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  let rightPanel;
  switch (mode) {
    case "visual":
      rightPanel = <InspectorPanel />;
      break;
    case "rf":
      rightPanel = (
        <>
          <RFMaterialPanel />
          <InspectorPanel />
        </>
      );
      break;
    case "validation":
      rightPanel = <ValidationPanel />;
      break;
    case "ai":
      rightPanel = <AISuggestionPanel />;
      break;
    case "results":
      rightPanel = (
        // Keyed by project so every panel's local form state (trajectory
        // endpoints, scenario/dataset params, channel selects) resets per
        // scene instead of leaking coordinates across projects.
        <>
          <SolverControls key={`sc-${projectId}`} />
          <ResultExplorer key={`re-${projectId}`} />
        </>
      );
      break;
  }

  return (
    <div className="app">
      <Toolbar />
      {projectId ? (
        <div
          className="app-body"
          style={{
            gridTemplateColumns: `${panel.left}px 4px 1fr 4px ${panel.right}px`,
          }}
        >
          <aside className="sidebar left">
            <SceneTree />
            {/* Dockable cards moved to the left sidebar live below the tree
                and stay visible across mode-tab switches. */}
            <PanelHost side="left" />
          </aside>
          <PanelHandle
            side="left"
            active={panel.dragging === "left"}
            onStart={panel.startDrag}
            onReset={panel.reset}
          />
          <main className="viewer-wrap">
            <Viewer3D />
            {/* Floating (detached) panels overlay the viewport in any mode. */}
            <FloatingLayer />
          </main>
          <PanelHandle
            side="right"
            active={panel.dragging === "right"}
            onStart={panel.startDrag}
            onReset={panel.reset}
          />
          <aside className="sidebar right">
            {rightPanel}
            {/* Dockable cards docked right render in EVERY mode so playback,
                trajectory previews, and live params survive tab switches
                (audit B2: they used to die outside Results). */}
            <PanelHost side="right" />
          </aside>
        </div>
      ) : (
        <div className="empty-app">
          {busy ? (
            <p>{busy}</p>
          ) : projects.length === 0 ? (
            <>
              <h2>No projects found</h2>
              <p>Import a Mitsuba/Sionna scene (or an OpenStreetMap area) to get started.</p>
              <button className="primary" onClick={() => setImportOpen(true)}>
                Import a scene
              </button>
              <p className="hint">
                Or drop a project folder under <code>examples/demo_project/</code> and reload.
                Make sure the backend is running on port 8000.
              </p>
            </>
          ) : (
            <p>Select a project from the toolbar.</p>
          )}
        </div>
      )}
      {(error || notice) && (
        <div className={"toast " + (error ? "toast-error" : "toast-notice")}>
          <span>{error ?? notice}</span>
          <button onClick={error ? dismissError : dismissNotice} title="Dismiss">
            ×
          </button>
        </div>
      )}
      {projectId && undoDepth > 0 && (
        <button
          className="undo-fab"
          onClick={() => void undo()}
          title="Undo last scene change (Ctrl+Z)"
        >
          ↶ Undo
        </button>
      )}
      {solveProgress && (
        <div className="solve-progress" role="status" aria-live="polite">
          <div className="solve-progress-head">
            <span>
              {solveProgressLabel(solveProgress.kind)}
              {solveProgress.total > 0
                ? ` — ${solveProgress.done}/${solveProgress.total}`
                : "…"}
            </span>
            <button onClick={() => void cancelSolve()} title="Cancel solve">
              Cancel
            </button>
          </div>
          <div className="solve-progress-track">
            <div
              className="solve-progress-fill"
              style={{
                width:
                  solveProgress.total > 0
                    ? `${Math.round((solveProgress.done / solveProgress.total) * 100)}%`
                    : "100%",
                // total 0 = "started, no count yet": show an indeterminate full
                // bar (a pulse via CSS) rather than a misleading 0%.
                opacity: solveProgress.total > 0 ? 1 : 0.5,
              }}
            />
          </div>
        </div>
      )}
    </div>
  );
}

/** Human label for the solve-progress bar's `kind` (matches the backend's
 *  publish_event kinds). */
function solveProgressLabel(kind: string): string {
  const map: Record<string, string> = {
    trajectory: "Simulating trajectory",
    dataset: "Generating dataset",
    mesh_radio_map: "Simulating mesh radio map",
    radio_map: "Simulating radio map",
    radio_map_sweep: "Altitude sweep",
    scenario: "Simulating scenario",
    paths: "Simulating paths",
  };
  return map[kind] ?? "Solving";
}

/** Thin draggable strip between a sidebar and the canvas. Drag resizes the
 *  panel; double-click resets it to the default width. */
function PanelHandle({
  side,
  active,
  onStart,
  onReset,
}: {
  side: PanelSide;
  active: boolean;
  onStart: (side: PanelSide, e: React.PointerEvent) => void;
  onReset: (side: PanelSide) => void;
}) {
  return (
    <div
      className={"panel-handle" + (active ? " active" : "")}
      role="separator"
      aria-orientation="vertical"
      title="Drag to resize · double-click to reset"
      onPointerDown={(e) => onStart(side, e)}
      onDoubleClick={() => onReset(side)}
    />
  );
}
