import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import type { Mode } from "../store/appStore";
import type { Environment } from "../types/api";

const MODES: { id: Mode; label: string }[] = [
  { id: "visual", label: "Visual" },
  { id: "rf", label: "RF Materials" },
  { id: "validation", label: "Validation" },
  { id: "ai", label: "AI Assist" },
  { id: "results", label: "Results" },
];

const ENVIRONMENTS: { id: Environment; label: string }[] = [
  { id: "auto", label: "Auto" },
  { id: "indoor", label: "Indoor" },
  { id: "outdoor", label: "Outdoor" },
];

export default function Toolbar() {
  const projects = useAppStore((s) => s.projects);
  const projectId = useAppStore((s) => s.projectId);
  const openProject = useAppStore((s) => s.openProject);
  const mode = useAppStore((s) => s.mode);
  const setMode = useAppStore((s) => s.setMode);
  const scene = useAppStore((s) => s.scene);
  const resolvedEnvironment = useAppStore((s) => s.resolvedEnvironment);
  const setEnvironment = useAppStore((s) => s.setEnvironment);
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

  const environment: Environment = scene?.environment ?? "auto";
  const disabled = !projectId || busy !== null;

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

      <label
        className="env-select"
        title={
          environment === "auto"
            ? `Environment: auto (inferred ${resolvedEnvironment})`
            : "Scene environment — applies indoor/outdoor solver presets"
        }
      >
        <span className="env-select-label">Env</span>
        <select
          value={environment}
          disabled={disabled}
          onChange={(e) => void setEnvironment(e.target.value as Environment)}
        >
          {ENVIRONMENTS.map((env) => (
            <option key={env.id} value={env.id}>
              {env.label}
              {env.id === "auto" ? ` (${resolvedEnvironment})` : ""}
            </option>
          ))}
        </select>
      </label>

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
        <ActionsMenu
          disabled={disabled}
          items={[
            {
              label: "Validate",
              onClick: () => {
                setMode("validation");
                void runValidation();
              },
            },
            { label: "Compile RF", onClick: () => void compileRF() },
            {
              label: "Beamforming",
              title:
                "4x4 MIMO beamforming gain (TX-MRT and both-ends SVD) over the first TX->RX link",
              onClick: () => void runBeamforming(),
            },
            {
              label: "Export RFData",
              title:
                "Export the AODT-viewer RFData bundle (scenario_meta, devices, paths, trajectory, radio_map, calibration)",
              onClick: () => void exportRfdata(),
            },
          ]}
        />
        <button
          className="primary"
          disabled={disabled}
          onClick={() => void simulatePaths()}
        >
          Simulate Paths
        </button>
      </span>
    </header>
  );
}

interface ActionItem {
  label: string;
  onClick: () => void;
  title?: string;
}

/** Collapses the secondary toolbar actions into a single dropdown so the bar
 *  stays tidy. Simulate Paths remains a standalone primary button. */
function ActionsMenu({ disabled, items }: { disabled: boolean; items: ActionItem[] }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocDown(e: PointerEvent): void {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    }
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") setOpen(false);
    }
    document.addEventListener("pointerdown", onDocDown);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", onDocDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  // Close the menu if the toolbar becomes disabled mid-open (e.g. an action
  // kicked off a busy state).
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  return (
    <span className="actions-menu" ref={wrapRef}>
      <button
        className={"actions-trigger" + (open ? " open" : "")}
        disabled={disabled}
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((v) => !v)}
      >
        Actions <span className="actions-caret">▾</span>
      </button>
      {open && (
        <div className="actions-dropdown" role="menu">
          {items.map((item) => (
            <button
              key={item.label}
              role="menuitem"
              title={item.title}
              disabled={disabled}
              onClick={() => {
                setOpen(false);
                item.onClick();
              }}
            >
              {item.label}
            </button>
          ))}
        </div>
      )}
    </span>
  );
}
