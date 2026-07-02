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

  const panel = usePanelSize();

  useEffect(() => {
    if (!booted) {
      booted = true;
      void init();
    }
  }, [init]);

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
        <>
          <SolverControls />
          <ResultExplorer />
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
          </aside>
          <PanelHandle
            side="left"
            active={panel.dragging === "left"}
            onStart={panel.startDrag}
            onReset={panel.reset}
          />
          <main className="viewer-wrap">
            <Viewer3D />
          </main>
          <PanelHandle
            side="right"
            active={panel.dragging === "right"}
            onStart={panel.startDrag}
            onReset={panel.reset}
          />
          <aside className="sidebar right">{rightPanel}</aside>
        </div>
      ) : (
        <div className="empty-app">
          {busy ? (
            <p>{busy}</p>
          ) : projects.length === 0 ? (
            <>
              <h2>No projects found</h2>
              <p>
                Add a project folder under <code>projects/</code> or{" "}
                <code>examples/demo_project/</code> and reload, or make sure the backend is
                running on port 8000.
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
    </div>
  );
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
