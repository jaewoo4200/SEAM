import { useEffect, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import SegmentationPanel from "./SegmentationPanel";
import SeamAgentPanel from "./SeamAgentPanel";
import {
  MaterialSelect,
  Row,
  SEVERITY_COLORS,
  StatusBadge,
  Swatch,
  materialById,
  rgbaToCss,
} from "./common";
import type {
  Actor,
  ActorTrajectory,
  Antenna,
  Device,
  Prim,
  Vec3,
} from "../types/api";

const ANTENNA_PATTERNS = ["iso", "dipole", "hw_dipole", "tr38901"];
const POLARIZATIONS: Antenna["polarization"][] = ["V", "H", "VH", "cross"];

/** Two-step inline delete: first click arms ("Confirm delete?"), the second
 *  runs the action. Auto-reverts after ~4s or on blur (matches the inline-form
 *  confirm style used elsewhere instead of a blocking window.confirm). */
function DeleteConfirmButton({
  label,
  disabled,
  onConfirm,
}: {
  label: string;
  disabled: boolean;
  onConfirm: () => void;
}) {
  const [armed, setArmed] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const disarm = () => {
    if (timer.current) {
      clearTimeout(timer.current);
      timer.current = null;
    }
    setArmed(false);
  };
  useEffect(() => disarm, []);
  return (
    <button
      className={armed ? "danger" : "on-reject"}
      disabled={disabled}
      title={armed ? `Click again to ${label}` : label}
      onClick={() => {
        if (armed) {
          disarm();
          onConfirm();
        } else {
          setArmed(true);
          timer.current = setTimeout(() => setArmed(false), 4000);
        }
      }}
      onBlur={disarm}
    >
      {armed ? "Confirm?" : "Delete"}
    </button>
  );
}

interface DeviceDraft {
  name: string;
  x: string;
  y: string;
  z: string;
  power_dbm: string;
  pattern: string;
  polarization: Antenna["polarization"];
  num_rows: string;
  num_cols: string;
  vx: string;
  vy: string;
  vz: string;
  v_spacing: string;
  h_spacing: string;
}

function draftFromDevice(d: Device): DeviceDraft {
  return {
    name: d.name,
    x: String(d.position[0]),
    y: String(d.position[1]),
    z: String(d.position[2]),
    power_dbm: String(d.power_dbm),
    pattern: d.antenna.pattern,
    polarization: d.antenna.polarization,
    vx: String(d.velocity_m_s?.[0] ?? 0),
    vy: String(d.velocity_m_s?.[1] ?? 0),
    vz: String(d.velocity_m_s?.[2] ?? 0),
    num_rows: String(d.antenna.num_rows),
    num_cols: String(d.antenna.num_cols),
    v_spacing: String(d.antenna.vertical_spacing ?? 0.5),
    h_spacing: String(d.antenna.horizontal_spacing ?? 0.5),
  };
}

/** Editable device inspector (AODT / sionna-rt-gui parity). */
function DeviceCard({ device }: { device: Device }) {
  const updateDevice = useAppStore((s) => s.updateDevice);
  const deleteDevice = useAppStore((s) => s.deleteDevice);
  const surfaceZAt = useAppStore((s) => s.surfaceZAt);
  const busy = useAppStore((s) => s.busy);
  const [draft, setDraft] = useState<DeviceDraft>(() => draftFromDevice(device));
  const [err, setErr] = useState<string | null>(null);
  const [aglDraft, setAglDraft] = useState("");

  // Live height-above-surface for the DRAFT position: raycast straight down
  // from (x, y) onto the scene mesh below the device (RX -> terrain, TX ->
  // the rooftop it stands on). null = no surface there / viewer not mounted.
  const dx = Number(draft.x);
  const dy = Number(draft.y);
  const dz = Number(draft.z);
  const surfaceZ =
    Number.isFinite(dx) && Number.isFinite(dy) && Number.isFinite(dz)
      ? surfaceZAt(dx, dy, dz)
      : null;
  const agl = surfaceZ !== null && Number.isFinite(dz) ? dz - surfaceZ : null;

  // The probe raycasts the 3D viewer's meshes, which mount AFTER the card on
  // slow GLB loads — and nothing re-renders the card when they land, so the
  // AGL used to stay blank forever (reported as "m 값이 안 찍힘"). Poll until
  // the probe answers, then stop.
  const [, probeTick] = useState(0);
  useEffect(() => {
    if (surfaceZ !== null) return;
    const t = setInterval(() => probeTick((n) => n + 1), 1000);
    return () => clearInterval(t);
  }, [surfaceZ]);

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
        vertical_spacing: num(draft.v_spacing, "v spacing"),
        horizontal_spacing: num(draft.h_spacing, "h spacing"),
      };
      setErr(null);
      const vel: Vec3 = [num(draft.vx, "vx"), num(draft.vy, "vy"), num(draft.vz, "vz")];
      void updateDevice(device.id, {
        name: draft.name,
        position,
        velocity_m_s: vel.some((v) => v !== 0) ? vel : null,
        power_dbm: num(draft.power_dbm, "power"),
        antenna,
      });
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    }
  };

  const numInput = (key: keyof DeviceDraft, label: string, step = 0.1, title?: string) => (
    <label title={title}>
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

  // Shared unit/axis hint strings (F8): keep the terse field labels while
  // documenting the frame + units on hover.
  const POS_HINT = "Z-up ENU meters (X east · Y north · Z up)";
  const VEL_HINT = "Z-up ENU m/s — drives per-path Doppler in the solver";
  const SPACING_HINT =
    "in wavelengths (λ) at the carrier frequency (set in Simulation > GLOBAL)";

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
      <Row label="Above surface">
        {agl !== null ? (
          <span className="mono" title="height over the mesh directly below (terrain or rooftop)">
            {agl.toFixed(2)} m
          </span>
        ) : (
          <span className="hint" title="the 3D mesh has not finished loading (or there is no surface under this X/Y)">
            — waiting for the 3D mesh…
          </span>
        )}
      </Row>

      <div className="mat-editor" style={{ marginTop: 10 }}>
        <h4>Edit device</h4>
        <div className="field-grid">
          <label>
            Name
            <input
              value={draft.name}
              disabled={disabled}
              placeholder={device.id}
              onChange={(e) => setDraft({ ...draft, name: e.target.value })}
            />
          </label>
          {numInput("x", "X (m)", 0.1, POS_HINT)}
          {numInput("y", "Y (m)", 0.1, POS_HINT)}
          {numInput("z", "Z (m)", 0.1, POS_HINT)}
          <label
            title={
              "Height above the scene surface directly below (X, Y): terrain " +
              "for a ground device, the rooftop for one placed on a building. " +
              "Type a height and press Enter (or blur) to set Z = surface + height."
            }
          >
            Above surface (m)
            <input
              type="number"
              step={0.1}
              placeholder={agl !== null ? agl.toFixed(2) : "no surface below"}
              value={aglDraft}
              disabled={disabled || surfaceZ === null}
              onChange={(e) => setAglDraft(e.target.value)}
              onBlur={() => {
                const h = Number(aglDraft);
                if (aglDraft.trim() !== "" && Number.isFinite(h) && surfaceZ !== null) {
                  setDraft({ ...draft, z: String(Number((surfaceZ + h).toFixed(3))) });
                }
                setAglDraft("");
              }}
              onKeyDown={(e) => {
                if (e.key === "Enter") (e.target as HTMLInputElement).blur();
              }}
            />
          </label>
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
          {numInput("v_spacing", "V spacing (λ)", 0.05, SPACING_HINT)}
          {numInput("h_spacing", "H spacing (λ)", 0.05, SPACING_HINT)}
          {numInput("vx", "Vel X (m/s)", 0.5, VEL_HINT)}
          {numInput("vy", "Vel Y (m/s)", 0.5, VEL_HINT)}
          {numInput("vz", "Vel Z (m/s)", 0.5, VEL_HINT)}
        </div>
        <p className="hint">
          Positions &amp; velocity are Z-up ENU (X east · Y north · Z up). Velocity drives
          per-path Doppler; V/H spacing is in wavelengths at the carrier frequency (Simulation &gt;
          GLOBAL).
        </p>
        <div className="editor-actions">
          <button className="primary" onClick={apply} disabled={disabled}>
            Apply
          </button>
          <DeleteConfirmButton
            label="delete this radio device"
            disabled={disabled}
            onConfirm={() => void deleteDevice(device.id)}
          />
          {err && <span className="field-error">{err}</span>}
        </div>
      </div>

      <Row label="Color">
        <Swatch color={device.color} /> <span className="mono">{device.color}</span>
      </Row>
    </div>
  );
}

// --------------------------------------------------------------- actors

interface ActorDraft {
  name: string;
  x: string;
  y: string;
  z: string;
  yaw: string;
  l: string;
  w: string;
  h: string;
}

function draftFromActor(a: Actor): ActorDraft {
  return {
    name: a.name,
    x: String(a.position[0]),
    y: String(a.position[1]),
    z: String(a.position[2]),
    // Orientation is [yaw, pitch, roll] (yaw about +Z); the UI edits index 0.
    yaw: String(a.orientation_deg[0]),
    l: String(a.shape.size_m[0]),
    w: String(a.shape.size_m[1]),
    h: String(a.shape.size_m[2]),
  };
}

/** Editable actor inspector: pose, size, RF material, attached devices, trajectory. */
function ActorCard({ actor }: { actor: Actor }) {
  const materials = useAppStore((s) => s.materials);
  const scene = useAppStore((s) => s.scene);
  const updateActor = useAppStore((s) => s.updateActor);
  const deleteActor = useAppStore((s) => s.deleteActor);
  const busy = useAppStore((s) => s.busy);
  const [draft, setDraft] = useState<ActorDraft>(() => draftFromActor(actor));
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    setDraft(draftFromActor(actor));
    setErr(null);
  }, [actor]);

  const disabled = busy !== null;
  const devices = scene?.devices ?? [];

  const applyPose = () => {
    const num = (raw: string, name: string): number => {
      const n = Number(raw);
      if (Number.isNaN(n)) throw new Error(`${name} is not a number`);
      return n;
    };
    const pos = (raw: string, name: string): number => {
      const n = num(raw, name);
      if (!(n > 0)) throw new Error(`${name} must be > 0`);
      return n;
    };
    // Pose (name/position/yaw) commits independently of size. If the size
    // fields are invalid we still apply the pose and surface the size error
    // inline, so pose edits are never silently discarded.
    let position: Vec3;
    let orientation_deg: Vec3;
    try {
      position = [num(draft.x, "X"), num(draft.y, "Y"), num(draft.z, "Z")];
      orientation_deg = [
        num(draft.yaw, "Yaw"),
        actor.orientation_deg[1],
        actor.orientation_deg[2],
      ];
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
      return;
    }

    let size_m: Vec3 | null = null;
    let sizeErr: string | null = null;
    try {
      size_m = [pos(draft.l, "Length"), pos(draft.w, "Width"), pos(draft.h, "Height")];
    } catch (e) {
      sizeErr = e instanceof Error ? e.message : String(e);
    }

    if (size_m) {
      setErr(null);
      void updateActor(actor.id, {
        name: draft.name,
        position,
        orientation_deg,
        shape: { ...actor.shape, size_m },
      });
    } else {
      // Sizes invalid: commit pose + name only, keep the existing shape.
      setErr(`sizes not applied: ${sizeErr}`);
      void updateActor(actor.id, {
        name: draft.name,
        position,
        orientation_deg,
      });
    }
  };

  const numInput = (key: keyof ActorDraft, label: string, step = 0.1, title?: string) => (
    <label title={title}>
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

  const POS_HINT = "Z-up ENU meters (X east · Y north · Z up)";

  const toggleDevice = (deviceId: string) => {
    const cur = actor.attached_device_ids;
    const next = cur.includes(deviceId)
      ? cur.filter((id) => id !== deviceId)
      : [...cur, deviceId];
    void updateActor(actor.id, { attached_device_ids: next });
  };

  return (
    <div className="panel">
      <h3 className="panel-title">
        Actor · <span className="mono">{actor.id}</span>
      </h3>
      <Row label="Kind">
        <span
          className="badge"
          style={{ borderColor: actor.color ?? "#a78bfa", color: actor.color ?? "#a78bfa" }}
        >
          {actor.kind}
        </span>
      </Row>

      <div className="mat-editor" style={{ marginTop: 10 }}>
        <h4>Pose &amp; size</h4>
        <label style={{ display: "flex", flexDirection: "column", gap: 2, marginBottom: 6 }}>
          Name
          <input
            type="text"
            value={draft.name}
            disabled={disabled}
            onChange={(e) => setDraft({ ...draft, name: e.target.value })}
          />
        </label>
        <div className="field-grid">
          {numInput("x", "X (m)", 0.1, POS_HINT)}
          {numInput("y", "Y (m)", 0.1, POS_HINT)}
          {numInput("z", "Z (m)", 0.1, POS_HINT)}
          {numInput("yaw", "Yaw (°)", 5, "Rotation about +Z (Z-up ENU)")}
          {numInput("l", "Length (m)")}
          {numInput("w", "Width (m)")}
          {numInput("h", "Height (m)")}
        </div>
        <p className="hint">Position is Z-up ENU meters (X east · Y north · Z up); yaw about +Z.</p>
        <div className="editor-actions">
          <button className="primary" onClick={applyPose} disabled={disabled}>
            Apply
          </button>
          <DeleteConfirmButton
            label="delete this actor"
            disabled={disabled}
            onConfirm={() => void deleteActor(actor.id)}
          />
          {err && <span className="field-error">{err}</span>}
        </div>
      </div>

      <div style={{ marginTop: 12 }}>
        <h4>RF material</h4>
        <MaterialSelect
          library={materials}
          value={actor.rf_material_id}
          placeholder="— assign material —"
          disabled={disabled}
          onSelect={(materialId) => void updateActor(actor.id, { rf_material_id: materialId })}
        />
      </div>

      <div style={{ marginTop: 12 }}>
        <h4>Attached devices</h4>
        {devices.length === 0 ? (
          <p className="hint">No devices in the scene.</p>
        ) : (
          <div className="actor-devices">
            {devices.map((d) => (
              <label key={d.id} className="solver-check">
                <input
                  type="checkbox"
                  checked={actor.attached_device_ids.includes(d.id)}
                  disabled={disabled}
                  onChange={() => toggleDevice(d.id)}
                />
                <span className="device-icon" style={{ color: d.color }}>
                  {d.kind === "tx" ? "▲" : "●"}
                </span>
                <span className="mono">{d.id}</span>
              </label>
            ))}
          </div>
        )}
      </div>

      <ActorTrajectoryEditor actor={actor} />

      <Row label="Color">
        <Swatch color={actor.color ?? "#a78bfa"} />{" "}
        <span className="mono">{actor.color ?? "—"}</span>
      </Row>
    </div>
  );
}

/** Waypoint list editor with per-row XYZ, add/remove, dt_s and loop, plus a
 *  "Record current pos" that appends the actor's current position. */
function ActorTrajectoryEditor({ actor }: { actor: Actor }) {
  const updateActor = useAppStore((s) => s.updateActor);
  const requestPick = useAppStore((s) => s.requestPick);
  const busy = useAppStore((s) => s.busy);
  const disabled = busy !== null;

  const traj: ActorTrajectory = actor.trajectory ?? {
    waypoints: [],
    dt_s: 0.1,
    loop: false,
    mode: "once",
  };
  const enabled = actor.trajectory !== null;
  // Effective playback mode: prefer the new `mode`, fall back to the legacy
  // `loop` bool for older scenes that predate the mode field.
  const mode: NonNullable<ActorTrajectory["mode"]> = traj.mode ?? (traj.loop ? "loop" : "once");

  const commit = (next: ActorTrajectory | null) => void updateActor(actor.id, { trajectory: next });

  // Set the playback mode, keeping the legacy `loop` bool in sync so backends
  // that only read `loop` still behave (loop stays true for loop/pingpong).
  const setMode = (m: NonNullable<ActorTrajectory["mode"]>) =>
    commit({ ...traj, mode: m, loop: m !== "once" });

  const setWaypoint = (i: number, axis: number, value: number) => {
    const waypoints = traj.waypoints.map((wp, j) => {
      if (j !== i) return wp;
      const nwp: Vec3 = [...wp];
      nwp[axis] = value;
      return nwp;
    });
    commit({ ...traj, waypoints });
  };

  const addWaypoint = () => commit({ ...traj, waypoints: [...traj.waypoints, [...actor.position]] });
  const removeWaypoint = (i: number) =>
    commit({ ...traj, waypoints: traj.waypoints.filter((_, j) => j !== i) });
  const recordCurrent = () =>
    commit({ ...traj, waypoints: [...traj.waypoints, [...actor.position]] });

  // Click one point in the viewport and append it as a waypoint. A pick
  // resolves asynchronously, so read the live trajectory from the store on
  // completion rather than the (possibly stale) render-time `traj` closure.
  const pickWaypoint = () =>
    requestPick({
      label: "Actor waypoint",
      count: 1,
      target: "surface",
      heightOffset: 0,
      onComplete: ([p]) => {
        const cur = useAppStore.getState().scene?.actors.find((a) => a.id === actor.id);
        const base: ActorTrajectory = cur?.trajectory ?? { waypoints: [], dt_s: 0.1, loop: false, mode: "once" };
        void updateActor(actor.id, { trajectory: { ...base, waypoints: [...base.waypoints, p] } });
      },
    });

  return (
    <div style={{ marginTop: 12 }}>
      <h4>Trajectory</h4>
      <label className="solver-check" style={{ marginBottom: 6 }}>
        <input
          type="checkbox"
          checked={enabled}
          disabled={disabled}
          onChange={(e) =>
            commit(
              e.target.checked
                ? { waypoints: [[...actor.position]], dt_s: 0.1, loop: false, mode: "once" }
                : null,
            )
          }
        />
        Define waypoints
      </label>

      {enabled && (
        <>
          <div className="actor-waypoints">
            {traj.waypoints.length === 0 && <p className="hint">No waypoints yet.</p>}
            {traj.waypoints.map((wp, i) => (
              <div key={i} className="actor-waypoint-row">
                <span className="mono actor-wp-idx">{i + 1}</span>
                {[0, 1, 2].map((axis) => (
                  <input
                    key={axis}
                    type="number"
                    step={0.5}
                    value={wp[axis]}
                    disabled={disabled}
                    onChange={(e) => setWaypoint(i, axis, Number(e.target.value))}
                  />
                ))}
                <button
                  className="tree-del"
                  disabled={disabled}
                  title="Remove waypoint"
                  onClick={() => removeWaypoint(i)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
          <div className="panel-actions">
            <button disabled={disabled} onClick={addWaypoint}>
              + Waypoint
            </button>
            <button
              disabled={disabled}
              onClick={pickWaypoint}
              title="Click a point in the viewport to append it as a waypoint"
            >
              🎯 Pick waypoint
            </button>
            <button disabled={disabled} onClick={recordCurrent} title="Append the actor's current position">
              Record current pos
            </button>
          </div>
          <p className="hint" style={{ marginTop: 6 }}>
            Animates in Results → Scenario playback.
          </p>
          <label className="solver-field" style={{ marginTop: 6 }}>
            <span className="solver-field-label">dt</span>
            <span className="solver-field-input">
              <input
                type="number"
                min={0.001}
                step={0.05}
                value={traj.dt_s}
                disabled={disabled}
                onChange={(e) => commit({ ...traj, dt_s: Math.max(0.001, Number(e.target.value)) })}
              />
              <span className="solver-unit">s</span>
            </span>
          </label>
          <label className="solver-field" style={{ marginTop: 6 }}>
            <span className="solver-field-label">Mode</span>
            <select
              value={mode}
              disabled={disabled}
              onChange={(e) => setMode(e.target.value as NonNullable<ActorTrajectory["mode"]>)}
            >
              <option value="once">once</option>
              <option value="loop">loop</option>
              <option value="pingpong">pingpong</option>
            </select>
          </label>
        </>
      )}
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

      {/* One home for every way to author this prim's RF material: manual
          assign, AI suggestion (jumps to AI Assist), texture-mask split, and
          the SEAM-Agent — so RF Materials vs AI Assist stops feeling like two
          disconnected places to do the same job. */}
      <div className="authoring-section" style={{ marginTop: 12 }}>
        <h4>Material authoring</h4>
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
        <div className="panel-actions" style={{ marginTop: 4 }}>
          <button
            disabled={busy !== null}
            title="Ask the AI provider to suggest an RF material for this prim (opens AI Assist for review)"
            onClick={() => {
              selectPrim(prim.id);
              useAppStore.getState().setMode("ai");
              void useAppStore.getState().suggestMaterials();
            }}
          >
            ✨ Suggest with AI
          </button>
        </div>

        {/* Multi-material split: only for prims backed by a texture atlas (the
            mask sources classify texels / render tiles from that texture). */}
        {prim.mesh_ref && <SegmentationPanel prim={prim} />}

        {/* SEAM-Agent: AI material authoring over multi-view captures of this
            prim's mesh (gated on mesh_ref like the split above). */}
        {prim.mesh_ref && <SeamAgentPanel prim={prim} />}
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
  const selectedActorId = useAppStore((s) => s.selectedActorId);

  if (selectedDeviceId) {
    const device = scene?.devices.find((d) => d.id === selectedDeviceId);
    if (device) return <DeviceCard device={device} />;
  }

  if (selectedActorId) {
    const actor = scene?.actors.find((a) => a.id === selectedActorId);
    if (actor) return <ActorCard actor={actor} />;
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
