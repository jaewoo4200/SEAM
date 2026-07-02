import { useEffect, useState } from "react";
import { useAppStore } from "../store/appStore";
import {
  MaterialSelect,
  Row,
  SEVERITY_COLORS,
  StatusBadge,
  Swatch,
  materialById,
  rgbaToCss,
} from "./common";
import type { Antenna, Device, Prim, Vec3 } from "../types/api";

const ANTENNA_PATTERNS = ["iso", "dipole", "hw_dipole", "tr38901"];
const POLARIZATIONS: Antenna["polarization"][] = ["V", "H", "VH", "cross"];

interface DeviceDraft {
  x: string;
  y: string;
  z: string;
  power_dbm: string;
  pattern: string;
  polarization: Antenna["polarization"];
  num_rows: string;
  num_cols: string;
}

function draftFromDevice(d: Device): DeviceDraft {
  return {
    x: String(d.position[0]),
    y: String(d.position[1]),
    z: String(d.position[2]),
    power_dbm: String(d.power_dbm),
    pattern: d.antenna.pattern,
    polarization: d.antenna.polarization,
    num_rows: String(d.antenna.num_rows),
    num_cols: String(d.antenna.num_cols),
  };
}

/** Editable device inspector (AODT / sionna-rt-gui parity). */
function DeviceCard({ device }: { device: Device }) {
  const updateDevice = useAppStore((s) => s.updateDevice);
  const deleteDevice = useAppStore((s) => s.deleteDevice);
  const busy = useAppStore((s) => s.busy);
  const [draft, setDraft] = useState<DeviceDraft>(() => draftFromDevice(device));
  const [err, setErr] = useState<string | null>(null);

  // Reset the form when a different device is selected or the device changes.
  useEffect(() => {
    setDraft(draftFromDevice(device));
    setErr(null);
  }, [device]);

  const disabled = busy !== null;

  const apply = () => {
    const num = (raw: string, name: string): number => {
      const n = Number(raw);
      if (Number.isNaN(n)) throw new Error(`${name} is not a number`);
      return n;
    };
    const int = (raw: string, name: string): number => {
      const n = num(raw, name);
      if (!Number.isInteger(n) || n < 1) throw new Error(`${name} must be a positive integer`);
      return n;
    };
    try {
      const position: Vec3 = [num(draft.x, "X"), num(draft.y, "Y"), num(draft.z, "Z")];
      const antenna: Antenna = {
        pattern: draft.pattern,
        polarization: draft.polarization,
        num_rows: int(draft.num_rows, "rows"),
        num_cols: int(draft.num_cols, "cols"),
      };
      setErr(null);
      void updateDevice(device.id, {
        position,
        power_dbm: num(draft.power_dbm, "power"),
        antenna,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const numInput = (key: keyof DeviceDraft, label: string, step = 0.1) => (
    <label>
      {label}
      <input
        type="number"
        step={step}
        value={draft[key]}
        disabled={disabled}
        onChange={(e) => setDraft({ ...draft, [key]: e.target.value })}
      />
    </label>
  );

  return (
    <div className="panel">
      <h3 className="panel-title">
        Device · <span className="mono">{device.id}</span>
      </h3>
      <Row label="Kind">
        <span className="badge" style={{ borderColor: device.color, color: device.color }}>
          {device.kind === "tx" ? "transmitter" : "receiver"}
        </span>
      </Row>
      {device.name && <Row label="Name">{device.name}</Row>}

      <div className="mat-editor" style={{ marginTop: 10 }}>
        <h4>Edit device</h4>
        <div className="field-grid">
          {numInput("x", "X (m)")}
          {numInput("y", "Y (m)")}
          {numInput("z", "Z (m)")}
          {device.kind === "tx" && numInput("power_dbm", "Power (dBm)", 1)}
          <label>
            Antenna pattern
            <select
              value={draft.pattern}
              disabled={disabled}
              onChange={(e) => setDraft({ ...draft, pattern: e.target.value })}
            >
              {ANTENNA_PATTERNS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          <label>
            Polarization
            <select
              value={draft.polarization}
              disabled={disabled}
              onChange={(e) =>
                setDraft({ ...draft, polarization: e.target.value as Antenna["polarization"] })
              }
            >
              {POLARIZATIONS.map((p) => (
                <option key={p} value={p}>
                  {p}
                </option>
              ))}
            </select>
          </label>
          {numInput("num_rows", "Array rows", 1)}
          {numInput("num_cols", "Array cols", 1)}
        </div>
        <div className="editor-actions">
          <button className="primary" onClick={apply} disabled={disabled}>
            Apply
          </button>
          <button
            className="on-reject"
            onClick={() => void deleteDevice(device.id)}
            disabled={disabled}
            title="Delete this radio device"
          >
            Delete
          </button>
          {err && <span className="field-error">{err}</span>}
        </div>
      </div>

      <Row label="Color">
        <Swatch color={device.color} /> <span className="mono">{device.color}</span>
      </Row>
    </div>
  );
}

function PrimCard({ prim }: { prim: Prim }) {
  const materials = useAppStore((s) => s.materials);
  const selection = useAppStore((s) => s.selection);
  const validation = useAppStore((s) => s.validation);
  const assignMaterial = useAppStore((s) => s.assignMaterial);
  const selectPrim = useAppStore((s) => s.selectPrim);
  const busy = useAppStore((s) => s.busy);

  const rfMat = materialById(materials, prim.rf.material_id);
  const baseColor = rgbaToCss(prim.visual?.base_color_rgba);
  const primIssues = validation?.issues.filter((i) => i.prim_id === prim.id) ?? [];

  // Effective RF parameters: per-prim override, else library default.
  const effective = (override: number | null, libDefault: number | null | undefined) => {
    if (override !== null) return <span>{override}</span>;
    if (libDefault !== null && libDefault !== undefined) {
      return (
        <span>
          {libDefault} <span className="default-tag">(library default)</span>
        </span>
      );
    }
    return <span>—</span>;
  };

  return (
    <div className="panel">
      <h3 className="panel-title">
        <span className="mono">{prim.id}</span>
      </h3>
      <Row label="Name">{prim.name}</Row>
      <Row label="Type">{prim.type}</Row>
      {prim.semantic_tags.length > 0 && <Row label="Tags">{prim.semantic_tags.join(", ")}</Row>}
      {prim.mesh_ref && (
        <Row label="Mesh">
          <span className="mono">
            {prim.mesh_ref.mesh_name}
            {prim.mesh_ref.face_group ? ` / ${prim.mesh_ref.face_group}` : ""}
          </span>
        </Row>
      )}

      <div className="insp-columns" style={{ marginTop: 10 }}>
        <div className="insp-col">
          <h4>Visual material</h4>
          <Row label="Material">{prim.visual?.material_name ?? prim.visual?.material_id ?? "—"}</Row>
          <Row label="Id">
            <span className="mono">{prim.visual?.material_id ?? "—"}</span>
          </Row>
          <Row label="Texture">
            <span className="mono">{prim.visual?.base_color_texture ?? "—"}</span>
          </Row>
          <Row label="Base color">
            {baseColor ? (
              <>
                <Swatch color={baseColor} /> <span className="mono">{baseColor}</span>
              </>
            ) : (
              "—"
            )}
          </Row>
        </div>

        <div className="insp-col">
          <h4>RF material</h4>
          <Row label="Material">
            {rfMat ? (
              <>
                <Swatch color={rfMat.preview_color} /> {rfMat.display_name}
              </>
            ) : (
              (prim.rf.material_id ?? "unassigned")
            )}
          </Row>
          <Row label="Status">
            <StatusBadge status={prim.rf.assignment_status} />
          </Row>
          <Row label="Sources">{prim.rf.assignment_sources.join(", ") || "—"}</Row>
          <Row label="Confidence">
            {prim.rf.confidence !== null ? prim.rf.confidence.toFixed(2) : "—"}
          </Row>
          <Row label="Thickness">{effective(prim.rf.thickness_m, rfMat?.thickness_m)} m</Row>
          <Row label="Scattering">
            {effective(prim.rf.scattering_coefficient, rfMat?.scattering_coefficient)}
          </Row>
          <Row label="XPD">{effective(prim.rf.xpd_coefficient, rfMat?.xpd_coefficient)}</Row>
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <h4>Assign RF material</h4>
        <MaterialSelect
          library={materials}
          value={prim.rf.material_id}
          placeholder="— assign material —"
          disabled={busy !== null}
          onSelect={(materialId) =>
            void assignMaterial({
              prim_ids: selection.length > 1 ? selection : [prim.id],
              rf_material_id: materialId,
              assignment_status: "user_confirmed",
              sources: ["user"],
            })
          }
        />
        <p className="hint">
          {selection.length > 1
            ? `Applies to all ${selection.length} selected prims as user_confirmed.`
            : "Saved to the scene as user_confirmed."}
        </p>
      </div>

      {primIssues.length > 0 && (
        <div className="insp-issues">
          <h4>Validation issues</h4>
          {primIssues.map((issue, i) => (
            <div
              key={i}
              className="issue-row"
              onClick={() => selectPrim(prim.id)}
              style={{ borderLeft: `2px solid ${SEVERITY_COLORS[issue.severity]}` }}
            >
              <span className="issue-code" style={{ color: SEVERITY_COLORS[issue.severity] }}>
                {issue.code}
              </span>
              <span className="issue-msg">{issue.message}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function InspectorPanel() {
  const scene = useAppStore((s) => s.scene);
  const selection = useAppStore((s) => s.selection);
  const selectedDeviceId = useAppStore((s) => s.selectedDeviceId);

  if (selectedDeviceId) {
    const device = scene?.devices.find((d) => d.id === selectedDeviceId);
    if (device) return <DeviceCard device={device} />;
  }

  const primId = selection.length > 0 ? selection[selection.length - 1] : null;
  const prim = primId ? (scene?.prims.find((p) => p.id === primId) ?? null) : null;

  if (!prim) {
    return (
      <div className="empty-state">
        Select an object in the viewer or scene tree to inspect its visual and RF bindings.
      </div>
    );
  }
  return <PrimCard prim={prim} />;
}
