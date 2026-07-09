import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import type { Mode } from "../store/appStore";
import { api, ApiError } from "../api/client";
import OsmAreaPicker from "./OsmAreaPicker";
import { PANEL_REGISTRY } from "./PanelHost";
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
  const deleteCurrentProject = useAppStore((s) => s.deleteCurrentProject);

  // Destructive delete confirm: the modal is armed from the Actions menu and
  // requires the user to type the exact project id to enable the red button.
  const [deleteOpen, setDeleteOpen] = useState(false);

  const sionnaAvailable =
    health?.backends.some((b) => b.name === "sionna" && b.available) ?? false;
  const providers = aiStatuses.length > 0 ? aiStatuses : (health?.ai_providers ?? []);
  const activeProvider = providers.find((p) => p.available);

  const environment: Environment = scene?.environment ?? "auto";
  const disabled = !projectId || busy !== null;

  return (
    <header className="toolbar">
      <span className="app-title">SEAM Studio</span>

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

      <PanelsMenu />

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
          style={{ background: sionnaAvailable ? "var(--ok)" : "var(--off)" }}
        />
        {sionnaAvailable ? "Sionna" : "Mock only"}
      </span>
      <span className="health-chip" title="AI suggestion provider">
        <span
          className="dot"
          style={{ background: activeProvider ? "var(--ok)" : "var(--off)" }}
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
                "4x4 MIMO beamforming gain (TX-MRT and both-ends SVD) over the first TX→RX link",
              onClick: () => void runBeamforming(),
            },
            {
              label: "Export RFData",
              title:
                "Export the AODT-viewer RFData bundle (scenario_meta, devices, paths, trajectory, radio_map, calibration)",
              onClick: () => void exportRfdata(),
            },
            {
              label: "Delete project…",
              danger: true,
              title: "Permanently remove this project folder (asks for confirmation)",
              onClick: () => setDeleteOpen(true),
            },
          ]}
        />
        <button
          className="primary"
          disabled={disabled}
          onClick={() => void simulatePaths()}
        >
          Simulate paths
        </button>
      </span>

      {deleteOpen && projectId && (
        <DeleteProjectModal
          projectId={projectId}
          busy={busy !== null}
          onCancel={() => setDeleteOpen(false)}
          onConfirm={async () => {
            setDeleteOpen(false);
            await deleteCurrentProject();
          }}
        />
      )}
    </header>
  );
}

/** Destructive delete confirm modal: shows the project id and requires the user
 *  to type it exactly before the red Delete button enables (guards against an
 *  accidental irreversible folder removal). Esc / backdrop / Cancel dismiss. */
function DeleteProjectModal({
  projectId,
  busy,
  onCancel,
  onConfirm,
}: {
  projectId: string;
  busy: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const [typed, setTyped] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  const match = typed === projectId;

  useEffect(() => {
    inputRef.current?.focus();
    function onKey(e: KeyboardEvent): void {
      if (e.key === "Escape") onCancel();
    }
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [onCancel]);

  return (
    <div className="modal-backdrop" onPointerDown={onCancel}>
      <div
        className="modal-card"
        role="dialog"
        aria-label="Delete project"
        aria-modal="true"
        onPointerDown={(e) => e.stopPropagation()}
      >
        <h4>Delete project</h4>
        <p className="hint">
          This permanently removes the project folder <span className="mono">{projectId}</span>{" "}
          and all of its scene, materials, and results. This cannot be undone.
        </p>
        <label className="confirm-field">
          <span>
            Type <span className="mono">{projectId}</span> to confirm
          </span>
          <input
            ref={inputRef}
            type="text"
            value={typed}
            placeholder={projectId}
            disabled={busy}
            onChange={(e) => setTyped(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && match && !busy) onConfirm();
            }}
          />
        </label>
        <div className="confirm-actions">
          <button
            className="danger"
            disabled={!match || busy}
            onClick={onConfirm}
          >
            {busy ? "Deleting…" : "Delete project"}
          </button>
          <button disabled={busy} onClick={onCancel}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  );
}

function slugifyId(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9_-]+/g, "_").replace(/^_+|_+$/g, "");
}

/** True when the chosen primary file is a zipped scene folder (whole bundle:
 *  XML + mesh subdirs + textures) rather than a bare .xml. */
function isZip(file: File): boolean {
  return /\.zip$/i.test(file.name);
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
  // A store flag lets other surfaces (the empty-state "Import a scene" CTA in
  // App.tsx) pop this open; we mirror it into local state then reset the flag.
  const importOpen = useAppStore((s) => s.importOpen);
  const setImportOpen = useAppStore((s) => s.setImportOpen);
  const [source, setSource] = useState<"xml" | "osm">("xml");
  const [xml, setXml] = useState<File | null>(null);
  const [meshes, setMeshes] = useState<File[]>([]);
  const [projectId, setProjectId] = useState("");
  const [idTouched, setIdTouched] = useState(false);
  const [name, setName] = useState("");
  const [environment, setEnvironment] = useState<Environment>("auto");
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  // OSM import fields: center coordinate + rectangle size. Default: Hanyang
  // University, Seoul (arbitrary sensible urban starting view).
  const [osmLat, setOsmLat] = useState("37.5576");
  const [osmLon, setOsmLon] = useState("127.0453");
  const [osmW, setOsmW] = useState(500);
  const [osmH, setOsmH] = useState(500);
  const [osmBldgH, setOsmBldgH] = useState(10);
  const [osmSelecting, setOsmSelecting] = useState(false);
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

  // Open (or close) the popover to follow the store flag, and clear the flag
  // once consumed so a later close doesn't immediately re-open it.
  useEffect(() => {
    if (importOpen && !open) {
      setOpen(true);
      setError(null);
    }
    if (importOpen) setImportOpen(false);
  }, [importOpen, open, setImportOpen]);

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
  const osmValid =
    Number.isFinite(Number(osmLat)) &&
    Math.abs(Number(osmLat)) <= 90 &&
    Number.isFinite(Number(osmLon)) &&
    Math.abs(Number(osmLon)) <= 180;
  const canSubmit =
    (source === "xml" ? xml !== null : osmValid) && idValid && !dupId && !submitting;

  const submitOsm = async () => {
    if (!idValid || !osmValid) return;
    setSubmitting(true);
    setError(null);
    try {
      const info = await api.importOsm({
        project_id: effectiveId,
        name: name.trim() || effectiveId,
        lat: Number(osmLat),
        lon: Number(osmLon),
        width_m: osmW,
        height_m: osmH,
        default_building_height_m: osmBldgH,
      });
      setOpen(false);
      reset();
      await onImported(info.project_id);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
      setSubmitting(false);
    }
  };

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
        <div
          className={"import-popover" + (source === "osm" ? " wide" : "")}
          role="dialog"
          aria-label="Import scene"
        >
          <h4>Import scene</h4>
          <div className="mode-tabs import-source-tabs">
            <button
              className={source === "xml" ? "active" : ""}
              onClick={() => setSource("xml")}
            >
              Mitsuba XML
            </button>
            <button
              className={source === "osm" ? "active" : ""}
              onClick={() => setSource("osm")}
            >
              OpenStreetMap
            </button>
          </div>
          {source === "xml" && (
            <>
              <label>
                Scene XML or .zip bundle
                <input
                  type="file"
                  accept=".xml,.zip"
                  onChange={(e) => {
                    const f = e.target.files?.[0] ?? null;
                    setXml(f);
                    // A zip carries its own meshes/textures — drop any picked
                    // companion meshes so they aren't sent alongside it.
                    if (f && isZip(f)) setMeshes([]);
                    if (f && !name) setName(f.name.replace(/\.(xml|zip)$/i, ""));
                  }}
                />
              </label>
              {/* Companion mesh files only apply to the single-XML path; a zip
                  bundle already contains its meshes at their relative paths. */}
              {!(xml && isZip(xml)) && (
                <label>
                  Mesh files (optional .ply/.obj)
                  <input
                    type="file"
                    accept=".ply,.obj,.stl"
                    multiple
                    onChange={(e) => setMeshes(Array.from(e.target.files ?? []))}
                  />
                </label>
              )}
            </>
          )}
          {source === "osm" && (
            <>
              <OsmAreaPicker
                area={{
                  lat: Number(osmLat) || 0,
                  lon: Number(osmLon) || 0,
                  widthM: osmW,
                  heightM: osmH,
                }}
                selecting={osmSelecting}
                onArea={(a) => {
                  setOsmLat(String(a.lat));
                  setOsmLon(String(a.lon));
                  setOsmW(Math.max(50, Math.min(3000, a.widthM)));
                  setOsmH(Math.max(50, Math.min(3000, a.heightM)));
                  setOsmSelecting(false);
                }}
              />
              <button
                className={osmSelecting ? "picking" : ""}
                onClick={() => setOsmSelecting((v) => !v)}
                title="Arm rectangle selection, then drag on the map; the coordinate and size fields fill in automatically"
              >
                {osmSelecting ? "Drag a rectangle on the map…" : "▭ Select area on map"}
              </button>
              <div className="osm-grid">
                <label>
                  Latitude
                  <input type="text" value={osmLat} onChange={(e) => setOsmLat(e.target.value)} />
                </label>
                <label>
                  Longitude
                  <input type="text" value={osmLon} onChange={(e) => setOsmLon(e.target.value)} />
                </label>
                <label>
                  Width (m, E–W)
                  <input
                    type="number"
                    min={50}
                    max={3000}
                    value={osmW}
                    onChange={(e) => setOsmW(Math.max(50, Math.min(3000, Number(e.target.value))))}
                  />
                </label>
                <label>
                  Height (m, N–S)
                  <input
                    type="number"
                    min={50}
                    max={3000}
                    value={osmH}
                    onChange={(e) => setOsmH(Math.max(50, Math.min(3000, Number(e.target.value))))}
                  />
                </label>
                <label>
                  Default bldg height (m)
                  <input
                    type="number"
                    min={3}
                    value={osmBldgH}
                    onChange={(e) => setOsmBldgH(Math.max(3, Number(e.target.value)))}
                  />
                </label>
              </div>
              <p className="hint">
                Fetches building footprints in a rectangle around the coordinate
                from OpenStreetMap (Overpass API — needs internet), extrudes them
                with OSM height/levels tags, and pre-assigns RF materials
                (concrete buildings, 28 GHz-safe ground). Paste any coordinate
                from Google Maps.
              </p>
            </>
          )}
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
            <button
              className="primary"
              disabled={!canSubmit}
              onClick={() => void (source === "osm" ? submitOsm() : submit())}
            >
              {submitting
                ? source === "osm"
                  ? "Fetching OSM…"
                  : "Importing…"
                : "Import"}
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
            Either a self-contained .xml plus its referenced mesh files, or a .zip of
            the whole scene folder (XML + meshes/ + textures/). Zipped bundles keep
            their textures, so they show up in the viewer and feed the AI material
            suggestions.
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
  /** Renders the item with the destructive .danger accent (red). */
  danger?: boolean;
}

/** Collapses the secondary toolbar actions into a single dropdown so the bar
 *  stays tidy. Simulate paths remains a standalone primary button. */
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
              className={item.danger ? "danger" : undefined}
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

const DOCK_LABEL: Record<"left" | "right" | "float", string> = {
  left: "left dock",
  right: "right dock",
  float: "floating",
};

/** "Panels ▾" dropdown: lists the dockable panel registry with each panel's
 *  current dock state, and lets the user reach any panel from ANY mode (the
 *  docked cards are hidden outside Results). Clicking a row floats the panel,
 *  or — if it is already floating — raises (focuses) it. Quick left/right dock
 *  buttons reuse the same store actions as the panel cards; no forked layout
 *  model. */
function PanelsMenu() {
  const layout = useAppStore((s) => s.panelLayout);
  const setPanelDock = useAppStore((s) => s.setPanelDock);
  const raisePanel = useAppStore((s) => s.raisePanel);
  const [open, setOpen] = useState(false);
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

  return (
    <span className="actions-menu panels-menu" ref={wrapRef}>
      <button
        className={"actions-trigger" + (open ? " open" : "")}
        aria-haspopup="menu"
        aria-expanded={open}
        title="Show or detach a dockable panel (works in any mode)"
        onClick={() => setOpen((v) => !v)}
      >
        Panels <span className="actions-caret">▾</span>
      </button>
      {open && (
        <div className="actions-dropdown panels-dropdown" role="menu">
          {PANEL_REGISTRY.map((def) => {
            const dock = layout[def.id]?.dock ?? "right";
            return (
              <div key={def.id} className="panels-row" role="menuitem">
                <button
                  className="panels-row-main"
                  title={
                    dock === "float"
                      ? `Focus the floating "${def.title}" window`
                      : `Detach "${def.title}" as a floating window`
                  }
                  onClick={() => {
                    if (dock === "float") raisePanel(def.id);
                    else setPanelDock(def.id, "float");
                  }}
                >
                  <span className="panels-row-title">{def.title}</span>
                  <span className="panels-row-state">{DOCK_LABEL[dock]}</span>
                </button>
                <span className="panels-row-docks">
                  <button
                    className={dock === "left" ? "active" : undefined}
                    title="Dock to left sidebar"
                    onClick={() => setPanelDock(def.id, "left")}
                  >
                    ◧
                  </button>
                  <button
                    className={dock === "right" ? "active" : undefined}
                    title="Dock to right sidebar"
                    onClick={() => setPanelDock(def.id, "right")}
                  >
                    ◨
                  </button>
                </span>
              </div>
            );
          })}
        </div>
      )}
    </span>
  );
}
