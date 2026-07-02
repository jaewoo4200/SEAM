import { useState } from "react";
import { useAppStore } from "../store/appStore";
import { Swatch } from "./common";
import type { RFMaterial } from "../types/api";

const ID_PATTERN = /^[a-z0-9_]+$/;

interface DraftFields {
  relative_permittivity: string;
  conductivity_s_per_m: string;
  thickness_m: string;
  scattering_coefficient: string;
  xpd_coefficient: string;
  preview_color: string;
}

function draftFrom(mat: RFMaterial): DraftFields {
  return {
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
  disabled,
}: {
  material: RFMaterial;
  onSave: (mat: RFMaterial) => void;
  disabled: boolean;
}) {
  const [draft, setDraft] = useState<DraftFields>(() => draftFrom(material));
  const [fieldError, setFieldError] = useState<string | null>(null);

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
        {fieldError && <span className="field-error">{fieldError}</span>}
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
  const assignMaterial = useAppStore((s) => s.assignMaterial);
  const saveMaterial = useAppStore((s) => s.saveMaterial);
  const busy = useAppStore((s) => s.busy);
  const [activeId, setActiveId] = useState<string | null>(null);

  const list = materials?.materials ?? [];
  const active = list.find((m) => m.id === activeId) ?? null;

  const createCustom = () => {
    const id = window.prompt("New material id (lowercase a-z, 0-9, _):", "custom_material");
    if (!id) return;
    if (!ID_PATTERN.test(id)) {
      window.alert("Invalid id: use only lowercase letters, digits, and underscores.");
      return;
    }
    if (list.some((m) => m.id === id)) {
      window.alert(`Material "${id}" already exists.`);
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
      display_name: id,
      itu_name: null,
      builtin: false,
      notes: active ? `Custom material cloned from ${active.id}.` : "Custom material.",
    };
    void saveMaterial(created).then(() => setActiveId(id));
  };

  return (
    <div className="panel">
      <h3 className="panel-title">RF material library</h3>
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
                  {m.relative_permittivity ?? "–"} / {m.conductivity_s_per_m ?? "–"}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <div className="panel-actions">
        <button
          className="primary"
          disabled={!active || selection.length === 0 || busy !== null}
          title={
            selection.length === 0
              ? "Select prims in the scene tree (ctrl-click for multi-select) first"
              : active
                ? `Assign ${active.id} to ${selection.length} prim(s)`
                : "Pick a material row first"
          }
          onClick={() => {
            if (!active) return;
            void assignMaterial({
              prim_ids: selection,
              rf_material_id: active.id,
              assignment_status: "user_confirmed",
              sources: ["user"],
            });
          }}
        >
          Assign to selection ({selection.length})
        </button>
        <button onClick={createCustom} disabled={busy !== null}>
          New custom material
        </button>
      </div>

      {active && (
        <MaterialEditor
          key={active.id}
          material={active}
          onSave={(mat) => void saveMaterial(mat)}
          disabled={busy !== null}
        />
      )}
    </div>
  );
}
