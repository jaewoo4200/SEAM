import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import type { Mode } from "../store/appStore";
import { api, ApiError } from "../api/client";
import type { Environment } from "../types/api";

const PROJECT_ID_PATTERN = /^[a-z0-9_-]+$/;

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
  const init = useAppStore((s) => s.init);
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

      <ImportSceneButton
        disabled={busy !== null}
        existingIds={projects.map((p) => p.project_id)}
        onImported={async (newId) => {
          await init();
          await openProject(newId);
        }}
      />

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

function slugifyId(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
}

/** "Import" button + inline popover: upload a Mitsuba/Sionna .xml (with any
 *  companion .ply/.obj meshes) as a new project. On success reloads the project
 *  list and opens the new project. Errors surface inline in the popover. */
function ImportSceneButton({
  disabled,
  existingIds,
  onImported,
}: {
  disabled: boolean;
  existingIds: string[];
  onImported: (newId: string) => Promise<void>;
}) {
  const [open, setOpen] = useState(false);
  const [xml, setXml] = useState<File | null>(null);
  const [meshes, setMeshes] = useState<File[]>([]);
  const [projectId, setProjectId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [name, setName] = useState("");
  const [environment, setEnvironment] = useState<Environment>("auto");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const wrapRef = useRef<HTMLSpanElement>(null);

  useEffect(() => {
    if (!open) return;
    function onDocDown(e: PointerEvent): void {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
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

  const reset = () => {
    setXml(null);
    setMeshes([]);
    setProjectId("");
    setIdTouched(false);
    setName("");
    setEnvironment("auto");
    setError(null);
    setSubmitting(false);
  };

  // Auto-derive the id from the name until the user edits the id field.
  const effectiveId = idTouched ? projectId : slugifyId(name);
  const idValid = effectiveId.length > 0 && PROJECT_ID_PATTERN.test(effectiveId);
  const dupId = existingIds.includes(effectiveId);
  const canSubmit = xml !== null && idValid && !dupId && !submitting;

  const submit = async () => {
    if (!xml || !idValid) return;
    setSubmitting(true);
    setError(null);
    const form = new FormData();
    form.append("file", xml, xml.name);
    form.append("project_id", effectiveId);
    form.append("name", name.trim() || effectiveId);
    form.append("environment", environment);
    for (const m of meshes) form.append("meshes", m, m.name);
    try {
      const info = await api.importScene(form);
      setOpen(false);
      reset();
      await onImported(info.project_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setSubmitting(false);
    }
  };

  return (
    <span className="import-scene" ref={wrapRef}>
      <button
        className="import-trigger"
        disabled={disabled}
        title="Import a Mitsuba/Sionna scene XML as a new project"
        onClick={() => {
          setOpen((v) => !v);
          setError(null);
        }}
      >
        Import
      </button>
      {open && (
        <div className="import-popover" role="dialog" aria-label="Import scene">
          <h4>Import scene</h4>
          <label>
            Scene XML
            <input
              type="file"
              accept=".xml"
              onChange={(e) => {
                const f = e.target.files?.[0] ?? null;
                setXml(f);
                if (f && !name) setName(f.name.replace(/\.xml$/i, ""));
              }}
            />
          </label>
          <label>
            Mesh files (optional .ply/.obj)
            <input
              type="file"
              accept=".ply,.obj,.stl"
              multiple
              onChange={(e) => setMeshes(Array.from(e.target.files ?? []))}
            />
          </label>
          <label>
            Name
            <input
              type="text"
              value={name}
              placeholder="My Scene"
              onChange={(e) => setName(e.target.value)}
            />
          </label>
          <label>
            Project id
            <input
              type="text"
              value={effectiveId}
              placeholder="my_scene"
              onChange={(e) => {
                setIdTouched(true);
                setProjectId(e.target.value);
              }}
            />
          </label>
          <label>
            Environment
            <select
              value={environment}
              onChange={(e) => setEnvironment(e.target.value as Environment)}
            >
              {ENVIRONMENTS.map((env) => (
                <option key={env.id} value={env.id}>
                  {env.label}
                </option>
              ))}
            </select>
          </label>
          {effectiveId.length > 0 && !idValid && (
            <span className="field-error">id: lowercase letters, digits, - and _ only</span>
          )}
          {dupId && <span className="field-error">a project “{effectiveId}” already exists</span>}
          {error && <span className="field-error">{error}</span>}
          <div className="import-actions">
            <button className="primary" disabled={!canSubmit} onClick={() => void submit()}>
              {submitting ? "Importing…" : "Import"}
            </button>
            <button
              onClick={() => {
                setOpen(false);
                reset();
              }}
            >
              Cancel
            </button>
          </div>
          <p className="hint">
            Self-contained scenes: upload the .xml with its referenced meshes. For large
            multi-file bundles use examples/scripts/import_bundle_scene.py.
          </p>
        </div>
      )}
    </span>
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
