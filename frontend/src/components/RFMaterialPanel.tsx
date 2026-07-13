import { useEffect, useRef, useState, type ChangeEvent } from "react";
import { useAppStore } from "../store/appStore";
import { api, ApiError } from "../api/client";
import { Swatch } from "./common";
import type { RFMaterial } from "../types/api";

const ID_PATTERN = /^[a-z0-9_]+$/;

function slugifyId(name: string): string {
  return name.trim().toLowerCase().replace(/[^a-z0-9_]+/g, "_").replace(/^_+|_+$/g, "");
}

interface DraftFields {
  display_name: string;
  relative_permittivity: string;
  conductivity_s_per_m: string;
  thickness_m: string;
  scattering_coefficient: string;
  xpd_coefficient: string;
  preview_color: string;
}

function draftFrom(mat: RFMaterial): DraftFields {
  return {
    display_name: mat.display_name,
    relative_permittivity: mat.relative_permittivity?.toString() ?? "",
    conductivity_s_per_m: mat.conductivity_s_per_m?.toString() ?? "",
    thickness_m: mat.thickness_m?.toString() ?? "",
    scattering_coefficient: mat.scattering_coefficient.toString(),
    xpd_coefficient: mat.xpd_coefficient.toString(),
    preview_color: mat.preview_color,
  };
}

function MaterialEditor({
  material,
  onSave,
  onDelete,
  disabled,
}: {
  material: RFMaterial;
  onSave: (mat: RFMaterial) => void;
  onDelete: () => Promise<string | null>;
  disabled: boolean;
}) {
  const [draft, setDraft] = useState<DraftFields>(() => draftFrom(material));
  const [fieldError, setFieldError] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [deleting, setDeleting] = useState(false);
  // Two-step armed confirm for the permanent delete (mirrors the assign
  // overwrite guard below — a hand-tuned material shouldn't die to one click).
  const [deleteArmed, setDeleteArmed] = useState(false);
  const deleteTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disarmDelete = () => {
    if (deleteTimer.current) {
      clearTimeout(deleteTimer.current);
      deleteTimer.current = null;
    }
    setDeleteArmed(false);
  };
  useEffect(() => disarmDelete, []);

  const numField = (key: keyof DraftFields, label: string) => (
    <label>
      {label}
      <input
        type="text"
        value={draft[key]}
        onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
      />
    </label>
  );

  const save = () => {
    const parse = (raw: string, name: string): number | null => {
      if (raw.trim() === "") return null;
      const n = Number(raw);
      if (Number.isNaN(n)) throw new Error(`${name} is not a number`);
      return n;
    };
    try {
      const updated: RFMaterial = {
        ...material,
        display_name: draft.display_name.trim() || material.id,
        relative_permittivity: parse(draft.relative_permittivity, "relative permittivity"),
        conductivity_s_per_m: parse(draft.conductivity_s_per_m, "conductivity"),
        thickness_m: parse(draft.thickness_m, "thickness"),
        scattering_coefficient: parse(draft.scattering_coefficient, "scattering coefficient") ?? 0,
        xpd_coefficient: parse(draft.xpd_coefficient, "xpd coefficient") ?? 0,
        preview_color: draft.preview_color,
        builtin: false,
      };
      setFieldError(null);
      onSave(updated);
    } catch (err) {
      setFieldError(err instanceof Error ? err.message : String(err));
    }
  };

  return (
    <div className="mat-editor">
      <h4>
        Edit · {material.display_name} <span className="mono">({material.id})</span>
      </h4>
      <div className="field-grid">
        <label>
          Display name
          <input
            type="text"
            value={draft.display_name}
            onChange={(e) => setDraft({ ...draft, display_name: e.target.value })}
          />
        </label>
        {numField("relative_permittivity", "Relative permittivity εr")}
        {numField("conductivity_s_per_m", "Conductivity σ (S/m)")}
        {numField("thickness_m", "Thickness (m)")}
        {numField("scattering_coefficient", "Scattering coeff (0–1)")}
        {numField("xpd_coefficient", "XPD coeff (0–1)")}
        <label>
          Preview color
          <input
            type="color"
            value={draft.preview_color}
            onChange={(e) => setDraft({ ...draft, preview_color: e.target.value })}
          />
        </label>
      </div>
      <div className="editor-actions">
        <button className="primary" onClick={save} disabled={disabled}>
          Save material
        </button>
        {material.builtin ? (
          <span className="hint">Builtin material — cannot be deleted.</span>
        ) : (
          <button
            className="danger"
            disabled={disabled || deleting}
            title={
              deleteArmed
                ? "Click again to permanently delete this material"
                : "Delete this custom material from the project library"
            }
            onBlur={disarmDelete}
            onClick={() => {
              if (!deleteArmed) {
                setDeleteError(null);
                setDeleteArmed(true);
                deleteTimer.current = setTimeout(() => setDeleteArmed(false), 4000);
                return;
              }
              disarmDelete();
              setDeleteError(null);
              setDeleting(true);
              void onDelete()
                .then((err) => setDeleteError(err))
                .finally(() => setDeleting(false));
            }}
          >
            {deleting ? "Deleting…" : deleteArmed ? "Confirm delete?" : "Delete material"}
          </button>
        )}
        {fieldError && <span className="field-error">{fieldError}</span>}
        {deleteError && <span className="field-error">{deleteError}</span>}
      </div>
      <p className="hint">
        Model: {material.model}
        {material.itu_name ? ` · ITU: ${material.itu_name}` : ""} · empty εr/σ fields fall back to
        the ITU frequency-dependent model at simulation time.
      </p>
    </div>
  );
}

export default function RFMaterialPanel() {
  const materials = useAppStore((s) => s.materials);
  const selection = useAppStore((s) => s.selection);
  const scene = useAppStore((s) => s.scene);
  const projectId = useAppStore((s) => s.projectId);
  const assignMaterial = useAppStore((s) => s.assignMaterial);
  const saveMaterial = useAppStore((s) => s.saveMaterial);
  const refetchScene = useAppStore((s) => s.refetchScene);
  const openProject = useAppStore((s) => s.openProject);
  const notify = useAppStore((s) => s.notify);
  const notifyError = useAppStore((s) => s.notifyError);
  const busy = useAppStore((s) => s.busy);
  // Hidden file input for "Import library" (component-local; no store field).
  const importInputRef = useRef<HTMLInputElement | null>(null);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [unassignError, setUnassignError] = useState<string | null>(null);
  const [unassigning, setUnassigning] = useState(false);
  // Inline "new material" mini-form (replaces window.prompt/alert).
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);

  const list = materials?.materials ?? [];
  const active = list.find((m) => m.id === activeId) ?? null;

  // Overwrite guard: how many selected prims already carry a DIFFERENT,
  // user-confirmed RF material. Assigning over those silently discards a manual
  // decision, so the assign button becomes a two-step confirm when this is > 0.
  const primById = new Map((scene?.prims ?? []).map((p) => [p.id, p]));
  const overwriteCount = active
    ? selection.filter((id) => {
        const rf = primById.get(id)?.rf;
        return (
          rf?.assignment_status === "user_confirmed" && rf.material_id !== active.id
        );
      }).length
    : 0;

  // Two-step confirm state (mirrors the inline armed-confirm pattern used by
  // SceneTree/InspectorPanel): first click arms, auto-reverts after ~4s.
  const [confirmOverwrite, setConfirmOverwrite] = useState(false);
  const confirmTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disarmOverwrite = () => {
    if (confirmTimer.current) {
      clearTimeout(confirmTimer.current);
      confirmTimer.current = null;
    }
    setConfirmOverwrite(false);
  };
  useEffect(() => disarmOverwrite, []);
  // Never leave the confirm armed once the reason to confirm is gone (selection
  // changed, a different material row was picked, or overwrites dropped to 0).
  useEffect(() => {
    if (overwriteCount === 0) disarmOverwrite();
  }, [overwriteCount, activeId]);

  const doAssign = () => {
    if (!active) return;
    disarmOverwrite();
    void assignMaterial({
      prim_ids: selection,
      rf_material_id: active.id,
      assignment_status: "user_confirmed",
      sources: ["user"],
    });
  };

  const onAssignClick = () => {
    if (!active) return;
    if (overwriteCount > 0 && !confirmOverwrite) {
      setConfirmOverwrite(true);
      confirmTimer.current = setTimeout(() => setConfirmOverwrite(false), 4000);
      return;
    }
    doAssign();
  };

  // Unassign guard: clearing a user_confirmed (or measurement_calibrated)
  // binding discards the same manual decision the Assign overwrite guard
  // protects, so Unassign gets its own two-step confirm when any is selected.
  const confirmedCount = selection.filter((id) => {
    const st = primById.get(id)?.rf?.assignment_status;
    return st === "user_confirmed" || st === "measurement_calibrated";
  }).length;
  const [confirmUnassign, setConfirmUnassign] = useState(false);
  const unassignTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disarmUnassign = () => {
    if (unassignTimer.current) {
      clearTimeout(unassignTimer.current);
      unassignTimer.current = null;
    }
    setConfirmUnassign(false);
  };
  useEffect(() => disarmUnassign, []);
  useEffect(() => {
    if (confirmedCount === 0) disarmUnassign();
  }, [confirmedCount]);

  const unassignSelection = async () => {
    if (!projectId || selection.length === 0) return;
    setUnassignError(null);
    setUnassigning(true);
    try {
      await api.unassign(projectId, selection);
      // Refresh the scene so prim RF bindings reflect the cleared assignment.
      await refetchScene();
    } catch (err) {
      setUnassignError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setUnassigning(false);
    }
  };

  /** Delete a custom material. Returns an error string (e.g. the 409 "still
   *  assigned" message) to surface inline, or null on success. On success the
   *  project is reloaded so the library list drops the material. */
  const deleteMaterial = async (materialId: string): Promise<string | null> => {
    if (!projectId) return "no project open";
    try {
      await api.deleteMaterial(projectId, materialId);
      if (activeId === materialId) setActiveId(null);
      // Reload materials (and scene) via the project open action.
      await openProject(projectId);
      return null;
    } catch (err) {
      return err instanceof ApiError ? err.message : String(err);
    }
  };

  // Portable material library (idea #12): calibrated materials shouldn't be
  // trapped in one project. Export the whole library to a JSON file, or import
  // one back in — colliding ids are renamed by the server, never overwritten.
  const exportLibrary = async () => {
    if (!projectId) return;
    try {
      const lib = await api.exportMaterials(projectId);
      const json = JSON.stringify(lib, null, 2);
      const url = URL.createObjectURL(new Blob([json], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = `${projectId}_rf_materials.json`;
      document.body.appendChild(a);
      a.click();
      a.remove();
      URL.revokeObjectURL(url);
      notify(`Exported ${lib.materials.length} materials`);
    } catch (err) {
      notifyError(err instanceof ApiError ? err.message : String(err));
    }
  };

  const onImportFile = async (e: ChangeEvent<HTMLInputElement>) => {
    // Capture the element synchronously; the async body runs after React has
    // moved on, so we hold our own reference to reset value in `finally`.
    const input = e.currentTarget;
    const file = input.files?.[0];
    if (!file) return;
    if (!projectId) {
      input.value = "";
      return;
    }
    try {
      const parsed: unknown = JSON.parse(await file.text());
      // Tolerate either a { materials: [...] } wrapper or a bare [...] array.
      const materials: unknown = Array.isArray(parsed)
        ? parsed
        : (parsed as { materials?: unknown } | null)?.materials;
      if (
        !Array.isArray(materials) ||
        !materials.every(
          (m) => m != null && typeof m === "object" && typeof (m as { id?: unknown }).id === "string",
        )
      ) {
        notifyError("Not a material library file");
        return;
      }
      const res = await api.importMaterials(projectId, { materials: materials as RFMaterial[] });
      // Reload the project's materials via the same action edits/deletes use.
      await openProject(projectId);
      notify(
        `Imported ${res.imported.length}, renamed ${Object.keys(res.renamed).length}, skipped ${res.skipped.length}`,
      );
    } catch (err) {
      if (err instanceof ApiError) notifyError(err.message);
      else if (err instanceof SyntaxError) notifyError("Not a material library file");
      else notifyError(String(err));
    } finally {
      // Reset so re-picking the same file fires onChange again.
      input.value = "";
    }
  };

  // Auto-slug the id from the typed name (mirrors the old prompt default) and
  // surface duplicate / invalid errors inline instead of via window.alert.
  const newId = slugifyId(newName);

  const openCreate = () => {
    setNewName("");
    setCreateError(null);
    setCreating(true);
  };
  const cancelCreate = () => {
    setCreating(false);
    setNewName("");
    setCreateError(null);
  };

  const createCustom = () => {
    const id = newId;
    if (!id) {
      setCreateError("Enter a name for the new material.");
      return;
    }
    if (!ID_PATTERN.test(id)) {
      setCreateError("Invalid id: use only lowercase letters, digits, and underscores.");
      return;
    }
    if (list.some((m) => m.id === id)) {
      setCreateError(`Material "${id}" already exists.`);
      return;
    }
    const base: RFMaterial = active ?? {
      id,
      display_name: id,
      category: "custom",
      model: "constant",
      itu_name: null,
      relative_permittivity: 3.0,
      conductivity_s_per_m: 0.01,
      thickness_m: 0.1,
      scattering_coefficient: 0.0,
      xpd_coefficient: 0.0,
      transmissive: true,
      preview_color: "#9e9e9e",
      notes: "",
      builtin: false,
    };
    const created: RFMaterial = {
      ...base,
      id,
      display_name: newName.trim() || id,
      itu_name: null,
      builtin: false,
      notes: active ? `Custom material cloned from ${active.id}.` : "Custom material.",
    };
    setCreateError(null);
    void saveMaterial(created).then(() => {
      setActiveId(id);
      setCreating(false);
      setNewName("");
    });
  };

  return (
    <div className="panel">
      <h3 className="panel-title">RF material library</h3>
      <div className="panel-actions">
        <button
          onClick={() => void exportLibrary()}
          disabled={!projectId || busy !== null}
          title="Download this project's RF material library as a portable JSON file"
        >
          Export library
        </button>
        <button
          onClick={() => importInputRef.current?.click()}
          disabled={!projectId || busy !== null}
          title="Merge a material library JSON file into this project (colliding ids are renamed)"
        >
          Import library
        </button>
        <input
          ref={importInputRef}
          type="file"
          accept="application/json,.json"
          style={{ display: "none" }}
          onChange={(e) => void onImportFile(e)}
        />
      </div>
      {list.length === 0 ? (
        <div className="empty-state">No RF materials loaded</div>
      ) : (
        <table className="mat-table">
          <thead>
            <tr>
              <th></th>
              <th>id</th>
              <th>category</th>
              <th>model</th>
              <th>εr / σ</th>
            </tr>
          </thead>
          <tbody>
            {list.map((m) => (
              <tr
                key={m.id}
                className={m.id === activeId ? "active" : ""}
                onClick={() => setActiveId(m.id === activeId ? null : m.id)}
                title={m.notes || m.display_name}
              >
                <td>
                  <Swatch color={m.preview_color} />
                </td>
                <td className="mono">{m.id}</td>
                <td>{m.category}</td>
                <td>{m.model === "itu_frequency_dependent" ? "ITU" : "const"}</td>
                <td className="mono">
                  {m.relative_permittivity ?? "—"} / {m.conductivity_s_per_m ?? "—"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="assign-hint-row">
        <span className={"count-chip" + (selection.length > 0 ? " active" : "")}>
          {selection.length} selected
        </span>
        {selection.length === 0 && (
          <span className="hint assign-steps">
            1. Select surfaces in the tree/viewport (Ctrl-click adds) · 2. Pick a material row ·
            3. Assign
          </span>
        )}
      </div>

      <div className="panel-actions">
        <button
          className={confirmOverwrite ? "danger" : "primary"}
          disabled={!active || selection.length === 0 || busy !== null}
          title={
            selection.length === 0
              ? "Select prims in the scene tree (ctrl-click for multi-select) first"
              : !active
                ? "Pick a material row first"
                : confirmOverwrite
                  ? `Overwrite ${overwriteCount} user-confirmed assignment(s) with ${active.id}`
                  : overwriteCount > 0
                    ? `${overwriteCount} selected prim(s) already have a different confirmed material`
                    : `Assign ${active.id} to ${selection.length} prim(s)`
          }
          onClick={onAssignClick}
          onBlur={disarmOverwrite}
        >
          {confirmOverwrite
            ? `${overwriteCount} assigned — overwrite?`
            : `Assign to selection (${selection.length})`}
        </button>
        <button
          className={confirmUnassign ? "danger" : undefined}
          disabled={selection.length === 0 || busy !== null || unassigning}
          title={
            selection.length === 0
              ? "Select assigned prims to clear their RF material"
              : confirmUnassign
                ? "Click again to clear, including manually confirmed assignments"
                : `Clear the RF material on ${selection.length} prim(s)`
          }
          onBlur={disarmUnassign}
          onClick={() => {
            if (confirmedCount > 0 && !confirmUnassign) {
              setConfirmUnassign(true);
              unassignTimer.current = setTimeout(() => setConfirmUnassign(false), 4000);
              return;
            }
            disarmUnassign();
            void unassignSelection();
          }}
        >
          {unassigning
            ? "Unassigning…"
            : confirmUnassign
              ? `${confirmedCount} confirmed — clear anyway?`
              : `Unassign selection (${selection.length})`}
        </button>
        <button
          onClick={() => (creating ? cancelCreate() : openCreate())}
          disabled={busy !== null}
        >
          New custom material
        </button>
      </div>
      {unassignError && <div className="field-error">{unassignError}</div>}

      {creating && (
        <div className="mat-editor">
          <h4>New custom material{active ? ` (cloned from ${active.id})` : ""}</h4>
          <label className="solver-field">
            <span className="solver-field-label">Name</span>
            <span className="solver-field-input">
              <input
                type="text"
                autoFocus
                value={newName}
                placeholder="Custom material"
                disabled={busy !== null}
                onChange={(e) => {
                  setNewName(e.target.value);
                  if (createError) setCreateError(null);
                }}
                onKeyDown={(e) => {
                  if (e.key === "Enter") createCustom();
                  else if (e.key === "Escape") cancelCreate();
                }}
              />
            </span>
          </label>
          {newId && !createError && (
            <p className="hint">
              id: <span className="mono">{newId}</span>
            </p>
          )}
          {createError && <span className="field-error">{createError}</span>}
          <div className="panel-actions">
            <button className="primary" onClick={createCustom} disabled={busy !== null}>
              Create
            </button>
            <button onClick={cancelCreate} disabled={busy !== null}>
              Cancel
            </button>
          </div>
        </div>
      )}

      {active && (
        <MaterialEditor
          key={active.id}
          material={active}
          onSave={(mat) => void saveMaterial(mat)}
          onDelete={() => deleteMaterial(active.id)}
          disabled={busy !== null}
        />
      )}
    </div>
  );
}
