import { useAppStore } from "../store/appStore";
import {
  MaterialSelect,
  Row,
  SEVERITY_COLORS,
  StatusBadge,
  Swatch,
  formatVec,
  materialById,
  rgbaToCss,
} from "./common";
import type { Device, Prim } from "../types/api";

function DeviceCard({ device }: { device: Device }) {
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
      <Row label="Position">
        <span className="mono">{formatVec(device.position)} m</span>
      </Row>
      <Row label="Orientation">
        <span className="mono">{formatVec(device.orientation_deg, 1)} °</span>
      </Row>
      {device.kind === "tx" && <Row label="Power">{device.power_dbm.toFixed(1)} dBm</Row>}
      <Row label="Antenna">
        {device.antenna.pattern} · {device.antenna.polarization} ·{" "}
        {device.antenna.num_rows}×{device.antenna.num_cols}
      </Row>
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
