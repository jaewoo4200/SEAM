import { useAppStore } from "../store/appStore";
import type { Mode } from "../store/appStore";

const MODES: { id: Mode; label: string }[] = [
  { id: "visual", label: "Visual" },
  { id: "rf", label: "RF Materials" },
  { id: "validation", label: "Validation" },
  { id: "ai", label: "AI Assist" },
  { id: "results", label: "Results" },
];

export default function Toolbar() {
  const projects = useAppStore((s) => s.projects);
  const projectId = useAppStore((s) => s.projectId);
  const openProject = useAppStore((s) => s.openProject);
  const mode = useAppStore((s) => s.mode);
  const setMode = useAppStore((s) => s.setMode);
  const health = useAppStore((s) => s.health);
  const aiStatuses = useAppStore((s) => s.aiStatuses);
  const busy = useAppStore((s) => s.busy);
  const runValidation = useAppStore((s) => s.runValidation);
  const compileRF = useAppStore((s) => s.compileRF);
  const simulatePaths = useAppStore((s) => s.simulatePaths);
  const exportRfdata = useAppStore((s) => s.exportRfdata);
  const runBeamforming = useAppStore((s) => s.runBeamforming);

  const sionnaAvailable =
    health?.backends.some((b) => b.name === "sionna" && b.available) ?? false;
  const providers = aiStatuses.length > 0 ? aiStatuses : (health?.ai_providers ?? []);
  const activeProvider = providers.find((p) => p.available);

  return (
    <header className="toolbar">
      <span className="app-title">SionnaTwin Studio</span>

      <select
        value={projectId ?? ""}
        disabled={projects.length === 0 || busy !== null}
        onChange={(e) => {
          if (e.target.value) void openProject(e.target.value);
        }}
        title="Project"
      >
        {projects.length === 0 && <option value="">no projects</option>}
        {projects.map((p) => (
          <option key={p.project_id} value={p.project_id}>
            {p.name || p.project_id}
          </option>
        ))}
      </select>

      <nav className="mode-tabs">
        {MODES.map((m) => (
          <button
            key={m.id}
            className={mode === m.id ? "active" : ""}
            onClick={() => setMode(m.id)}
          >
            {m.label}
          </button>
        ))}
      </nav>

      <span className="spacer" />

      {busy && <span className="busy-indicator">{busy}</span>}

      <span className="health-chip" title="Ray-tracing backend availability">
        <span
          className="dot"
          style={{ background: sionnaAvailable ? "#66bb6a" : "#78909c" }}
        />
        {sionnaAvailable ? "Sionna" : "Mock only"}
      </span>
      <span className="health-chip" title="AI suggestion provider">
        <span
          className="dot"
          style={{ background: activeProvider ? "#66bb6a" : "#78909c" }}
        />
        {activeProvider ? activeProvider.name : "AI off"}
      </span>

      <span className="toolbar-actions">
        <button
          disabled={!projectId || busy !== null}
          onClick={() => {
            setMode("validation");
            void runValidation();
          }}
        >
          Validate
        </button>
        <button disabled={!projectId || busy !== null} onClick={() => void compileRF()}>
          Compile RF
        </button>
        <button
          className="primary"
          disabled={!projectId || busy !== null}
          onClick={() => void simulatePaths()}
        >
          Simulate Paths
        </button>
        <button
          disabled={!projectId || busy !== null}
          title="4x4 MIMO beamforming gain (TX-MRT and both-ends SVD) over the first TX->RX link"
          onClick={() => void runBeamforming()}
        >
          Beamforming
        </button>
        <button
          disabled={!projectId || busy !== null}
          title="Export the AODT-viewer RFData bundle (scenario_meta, devices, paths, trajectory, radio_map, calibration)"
          onClick={() => void exportRfdata()}
        >
          Export RFData
        </button>
      </span>
    </header>
  );
}
