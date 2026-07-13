import { useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import { api, ApiError } from "../api/client";
import BeamSweepHeatmap from "./BeamSweepHeatmap";
import AngularPlot from "./AngularPlot";
import { EpochStaleChip, PATH_COLORS, SELECTED_PATH_COLOR, formatVec, materialById } from "./common";
import { LineChart, exportCsv } from "../charts";
import { filterPaths, pathColor, pathDepth, powerRange } from "../pathFilter";
import { meshRadioMapRange } from "./MeshRadioMapOverlay";
import { UE_COLORS, samplesForUe, trajectorySteps, trajectoryUeIds } from "../trajectoryUtils";
import type { ColorBy } from "../store/appStore";
import type {
  BeamformingResult,
  DatasetInfo,
  DatasetGenerateRequest,
  DatasetSampling,
  LinkMetrics,
  PathType,
  RadioMapResultSet,
  RadioMapSweepResult,
  RayPath,
  ResultSetRef,
  RFMaterialLibrary,
  ScenarioResultSet,
  TrajectoryResultSet,
  UERoute,
  Vec3,
} from "../types/api";

const SELECTED_COLOR = SELECTED_PATH_COLOR;

/** "Scene changed since this was computed" badge. Results never silently
 *  present outdated numbers: the chip appears as soon as any scene edit
 *  (device/actor/material move, live sync) postdates the computation. */
export function StaleChip({
  kind,
}: {
  kind: "paths" | "channel" | "trajectory" | "beamforming" | "mesh_radio_map";
}) {
  const sceneEpoch = useAppStore((st) => st.sceneEpoch);
  const at = useAppStore((st) => st.resultEpochs[kind]);
  if (at === undefined || at === sceneEpoch) return null;
  return (
    <span
      className="stale-chip"
      title="The scene was edited after this result was computed — re-run to refresh"
    >
      ⚠ stale
    </span>
  );
}

type SortKey = "path_id" | "path_type" | "power_dbm" | "delay_ns" | "interactions";

function DelayPowerScatter({
  paths,
  selectedPathId,
  onSelect,
}: {
  paths: RayPath[];
  selectedPathId: string | null;
  onSelect: (pathId: string) => void;
}) {
  const W = 312;
  const H = 180;
  const L = 34; // left margin for power labels
  const B = 22; // bottom margin for delay labels

  const { dMin, dMax, pMin, pMax } = useMemo(() => {
    let dMin = Infinity, dMax = -Infinity, pMin = Infinity, pMax = -Infinity;
    for (const p of paths) {
      dMin = Math.min(dMin, p.delay_ns);
      dMax = Math.max(dMax, p.delay_ns);
      pMin = Math.min(pMin, p.power_dbm);
      pMax = Math.max(pMax, p.power_dbm);
    }
    if (!Number.isFinite(dMin)) {
      dMin = 0; dMax = 1; pMin = -100; pMax = 0;
    }
    if (dMax - dMin < 1e-9) dMax = dMin + 1;
    if (pMax - pMin < 1e-9) pMax = pMin + 1;
    return { dMin, dMax, pMin, pMax };
  }, [paths]);

  const x = (delay: number) => L + ((delay - dMin) / (dMax - dMin)) * (W - L - 10);
  const y = (power: number) => 8 + (1 - (power - pMin) / (pMax - pMin)) * (H - B - 16);

  return (
    <div className="scatter-wrap">
      <h4>Delay vs power</h4>
      <svg width={W} height={H}>
        <line className="scatter-axis" x1={L} y1={H - B} x2={W - 4} y2={H - B} />
        <line className="scatter-axis" x1={L} y1={4} x2={L} y2={H - B} />
        <text className="scatter-label" x={L} y={H - 8}>
          {dMin.toFixed(1)} ns
        </text>
        <text className="scatter-label" x={W - 8} y={H - 8} textAnchor="end">
          {dMax.toFixed(1)} ns
        </text>
        <text className="scatter-label" x={L - 4} y={14} textAnchor="end">
          {pMax.toFixed(0)}
        </text>
        <text className="scatter-label" x={L - 4} y={H - B} textAnchor="end">
          {pMin.toFixed(0)}
        </text>
        <text className="scatter-label" x={L - 4} y={(H - B) / 2} textAnchor="end">
          dBm
        </text>
        {paths.map((p) => {
          const selected = p.path_id === selectedPathId;
          return (
            <circle
              key={p.path_id}
              cx={x(p.delay_ns)}
              cy={y(p.power_dbm)}
              r={selected ? 6 : 3.5}
              fill={selected ? SELECTED_COLOR : PATH_COLORS[p.path_type]}
              stroke={selected ? "#ffffff" : "none"}
              onClick={() => onSelect(p.path_id)}
            >
              <title>
                {p.path_id}: {p.delay_ns.toFixed(1)} ns, {p.power_dbm.toFixed(1)} dBm
              </title>
            </circle>
          );
        })}
      </svg>
    </div>
  );
}

function PathDetail({ path }: { path: RayPath }) {
  const selectPrim = useAppStore((s) => s.selectPrim);
  // Em-dash when an angle pair is unavailable (backend could not report it).
  const deg = (v: number | null | undefined) =>
    v === null || v === undefined ? "—" : `${v.toFixed(1)}°`;
  const aod = path.aod_deg;
  const aoa = path.aoa_deg;
  return (
    <div className="path-detail">
      <h4>
        {path.path_id} · <span style={{ color: PATH_COLORS[path.path_type] }}>{path.path_type}</span>
      </h4>
      <div className="results-meta">
        <span className="mono">{path.tx_id}</span> → <span className="mono">{path.rx_id}</span> ·{" "}
        {path.power_dbm.toFixed(1)} dBm
        {path.path_gain_db !== null && path.path_gain_db !== undefined && (
          <> · gain {path.path_gain_db.toFixed(1)} dB</>
        )}{" "}
        · {path.delay_ns.toFixed(1)} ns · phase {path.phase_rad.toFixed(2)} rad
      </div>
      <div className="results-meta">
        AoD az/el <span className="mono">{deg(aod?.[0])}</span> /{" "}
        <span className="mono">{deg(aod?.[1])}</span> · AoA az/el{" "}
        <span className="mono">{deg(aoa?.[0])}</span> / <span className="mono">{deg(aoa?.[1])}</span>
      </div>
      <h4>Vertices ({path.vertices.length})</h4>
      <ol>
        {path.vertices.map((v, i) => (
          <li key={i}>{formatVec(v)}</li>
        ))}
      </ol>
      {path.interactions.length > 0 && (
        <>
          <h4>Interactions</h4>
          {path.interactions.map((it, i) => (
            <div key={i} className="issue-row" style={{ cursor: it.prim_id ? "pointer" : "default" }}>
              <span className="issue-code">{it.type}</span>
              <span>
                {it.prim_id ? (
                  <span
                    className="issue-prim"
                    onClick={() => it.prim_id && selectPrim(it.prim_id)}
                    title="Select prim"
                  >
                    {it.prim_id}
                  </span>
                ) : (
                  <span style={{ color: "var(--muted)" }}>unmapped surface</span>
                )}
                {it.rf_material_id && <span className="mono"> · {it.rf_material_id}</span>}
                <span className="mono" style={{ color: "var(--muted)" }}>
                  {" "}
                  @ {formatVec(it.point)}
                </span>
              </span>
            </div>
          ))}
        </>
      )}
    </div>
  );
}

// ------------------------------------------------- viewer render controls

function ViewerControls({ range }: { range: { min: number; max: number } }) {
  const strongestN = useAppStore((s) => s.strongestN);
  const setStrongestN = useAppStore((s) => s.setStrongestN);
  const minPowerDbm = useAppStore((s) => s.minPowerDbm);
  const setMinPowerDbm = useAppStore((s) => s.setMinPowerDbm);
  const colorBy = useAppStore((s) => s.colorBy);
  const setColorBy = useAppStore((s) => s.setColorBy);
  const lineWidthByPower = useAppStore((s) => s.lineWidthByPower);
  const setLineWidthByPower = useAppStore((s) => s.setLineWidthByPower);

  const minEnabled = minPowerDbm !== null;
  // Store sentinel 0 = unlimited ("All"). The slider (min 5) can't express it,
  // so an "All" checkbox toggles the sentinel and disables the slider; any
  // slider change re-enables it with a concrete value.
  const showAll = strongestN === 0;

  return (
    <div className="viewer-controls">
      <div className="strongest-n-row">
        <label className="solver-slider">
          <span className="solver-slider-head">
            <span>Strongest N</span>
            <span className="mono solver-slider-value">{showAll ? "all" : strongestN}</span>
          </span>
          <input
            type="range"
            min={5}
            max={200}
            step={5}
            value={showAll ? 5 : strongestN}
            disabled={showAll}
            onChange={(e) => setStrongestN(Number(e.target.value))}
          />
        </label>
        <label className="solver-check strongest-n-all">
          <input
            type="checkbox"
            checked={showAll}
            onChange={(e) => setStrongestN(e.target.checked ? 0 : 5)}
          />
          All
        </label>
      </div>

      <label className="solver-check">
        <input
          type="checkbox"
          checked={minEnabled}
          onChange={(e) =>
            setMinPowerDbm(e.target.checked ? Math.round(range.min) : null)
          }
        />
        Min power
        <input
          type="number"
          className="min-power-input"
          value={minEnabled ? minPowerDbm : ""}
          step={1}
          disabled={!minEnabled}
          onChange={(e) => {
            // Clearing the field must not snap to 0 dBm (which would hide rays).
            // Ignore empty input and keep the current threshold until a real
            // number is entered.
            const raw = e.target.value;
            if (raw === "") return;
            setMinPowerDbm(Number(raw));
          }}
        />
        <span className="solver-unit">dBm</span>
      </label>

      <label className="solver-field">
        <span className="solver-field-label">Color by</span>
        <select value={colorBy} onChange={(e) => setColorBy(e.target.value as ColorBy)}>
          <option value="type">type</option>
          <option value="power">power</option>
          <option value="depth">depth</option>
        </select>
      </label>

      <label className="solver-check">
        <input
          type="checkbox"
          checked={lineWidthByPower}
          onChange={(e) => setLineWidthByPower(e.target.checked)}
        />
        Line width by power
      </label>
    </div>
  );
}

// ------------------------------------------------------- trajectory section

function firstRxPosition(): Vec3 {
  const scene = useAppStore.getState().scene;
  const rx = scene?.devices.find((d) => d.kind === "rx");
  return rx ? rx.position : [10, 0, 1.5];
}

/** Span for auto-seeded trajectories: most of the scene, capped at 30 m. */
function defaultSpan(): number {
  const b = useAppStore.getState().sceneBounds;
  if (!b) return 30;
  const ext = Math.max(b.max[0] - b.min[0], b.max[1] - b.min[1]);
  return Math.max(1, Math.min(30, ext * 0.6));
}

/** Seed start/end from the first RX walking toward the scene center so the
 *  path stays inside the geometry (audit: +30 m constant left small rooms). */
function seededEndpoints(): { start: Vec3; end: Vec3 } {
  const start = firstRxPosition();
  const b = useAppStore.getState().sceneBounds;
  const span = defaultSpan();
  let dir: [number, number] = [1, 0];
  if (b) {
    const cx = (b.min[0] + b.max[0]) / 2;
    const cy = (b.min[1] + b.max[1]) / 2;
    const dx = cx - start[0];
    const dy = cy - start[1];
    const len = Math.hypot(dx, dy);
    if (len > 0.5) dir = [dx / len, dy / len];
  }
  return { start, end: [start[0] + dir[0] * span, start[1] + dir[1] * span, start[2]] };
}

export function TrajectorySection() {
  const trajectory = useAppStore((s) => s.trajectory);
  const trajFrame = useAppStore((s) => s.trajFrame);
  const trajPlaying = useAppStore((s) => s.trajPlaying);
  const trajSpeed = useAppStore((s) => s.trajSpeed);
  const trajLoop = useAppStore((s) => s.trajLoop);
  const setTrajFrame = useAppStore((s) => s.setTrajFrame);
  const setTrajPlaying = useAppStore((s) => s.setTrajPlaying);
  const setTrajSpeed = useAppStore((s) => s.setTrajSpeed);
  const setTrajLoop = useAppStore((s) => s.setTrajLoop);
  const simulateTrajectory = useAppStore((s) => s.simulateTrajectory);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);
  const requestPick = useAppStore((s) => s.requestPick);
  const pickLabel = useAppStore((s) => s.pick?.label ?? null);
  const sceneBounds = useAppStore((s) => s.sceneBounds);

  // Default: first RX walking toward the scene center, span scaled to the
  // scene (falls back to +30 m in X when bounds are unknown).
  const [start, setStart] = useState<Vec3>(() => seededEndpoints().start);
  const [end, setEnd] = useState<Vec3>(() => seededEndpoints().end);
  const [numPoints, setNumPoints] = useState(8);
  const [dt, setDt] = useState(0.1);
  const [followTerrain, setFollowTerrain] = useState(false);
  // Multi-UE routes drawn freehand in the viewport. Non-empty routes replace
  // the straight start->end line entirely (each polyline is resampled to
  // num_points steps server-side; all UEs move together per step).
  const scene = useAppStore((s) => s.scene);
  const rxIds = useMemo(
    () => (scene?.devices ?? []).filter((d) => d.kind === "rx").map((d) => d.id),
    [scene],
  );
  const [routes, setRoutes] = useState<UERoute[]>([]);
  // Import warnings kept per UE so they persist on the route row (e.g. a
  // waypoint that sits under the terrain surface), not just in the toast.
  const [routeWarnings, setRouteWarnings] = useState<Record<string, string[]>>({});
  const [routeUe, setRouteUe] = useState<string>("");
  // Fixed + moving UEs in one solve (multi-UE routes only).
  const [includeStaticRx, setIncludeStaticRx] = useState(false);
  // Waypoint JSON import: the backend normalizes cartesian/geographic points
  // to Z-up meters and flags waypoints that sit under the scene surface.
  const trajFileRef = useRef<HTMLInputElement>(null);
  const importTrajectoryFile = async (e: { target: HTMLInputElement }) => {
    const file = e.target.files?.[0];
    e.target.value = ""; // same file can be re-picked later
    if (!file || !projectId || !drawUe) return;
    try {
      const parsed: unknown = JSON.parse(await file.text());
      const body =
        parsed !== null && typeof parsed === "object" && "points" in (parsed as object)
          ? { ue_id: drawUe, agl_m: ueHeight, ...(parsed as object) }
          : { ue_id: drawUe, points: parsed, agl_m: ueHeight };
      const resp = await api.importTrajectory(projectId, body);
      if (resp.waypoints.length < 2) {
        useAppStore.setState({ error: "imported trajectory has fewer than 2 usable waypoints" });
        return;
      }
      setRoutes((rs) => [
        ...rs.filter((r) => r.ue_id !== drawUe),
        {
          ue_id: drawUe,
          waypoints: resp.waypoints,
          // Carry per-waypoint orientation through to the solve when present.
          ...(resp.orientations_deg ? { orientations_deg: resp.orientations_deg } : {}),
        },
      ]);
      // Persist any import warnings (e.g. underground waypoints) on the route
      // row so they don't vanish with the toast.
      setRouteWarnings((w) => ({ ...w, [drawUe]: resp.warnings }));
      const oriented = resp.orientations_deg?.some((o) => o !== null) ?? false;
      useAppStore.setState({
        notice:
          `Imported ${resp.waypoints.length} waypoint(s) for ${drawUe}` +
          (oriented ? " (with orientation)" : "") +
          (resp.warnings.length > 0 ? ` · ⚠ ${resp.warnings.join(" · ")}` : ""),
      });
    } catch (err) {
      useAppStore.setState({
        error: `trajectory import failed: ${err instanceof Error ? err.message : String(err)}`,
      });
    }
  };
  // Height above the clicked surface for picked/drawn waypoints (and the
  // follow-terrain snap height). Default = the first RX's authored height.
  const [ueHeight, setUeHeight] = useState<number>(() => {
    const z = firstRxPosition()[2];
    return Number.isFinite(z) && z > 0 ? Math.round(z * 100) / 100 : 1.5;
  });
  const drawUe = routeUe || rxIds.find((id) => !routes.some((r) => r.ue_id === id)) || rxIds[0] || "";

  const drawRoute = () => {
    if (!drawUe) return;
    requestPick({
      label: `Route for ${drawUe}`,
      count: "multi",
      target: "surface",
      heightOffset: ueHeight,
      onComplete: (pts) => {
        setRoutes((rs) => [
          ...rs.filter((r) => r.ue_id !== drawUe),
          { ue_id: drawUe, waypoints: pts },
        ]);
        // Drawn routes come from viewport SURFACE picks, so the waypoints ride
        // the terrain by construction; default follow-terrain ON so the solve
        // drapes them (outdoor) instead of burying the path through hills. The
        // user can still uncheck it (indoor rooms). Only auto-set until the
        // user has manually touched the checkbox.
        if (!followTouched.current) setFollowTerrain(true);
      },
    });
  };
  // Tracks whether the user has manually toggled the "Follow terrain" checkbox.
  // Provenance-driven defaults (drawn/picked route → ON, numeric entry → OFF)
  // only apply until the user takes over; after that we never override them.
  const followTouched = useRef(false);
  // Bounds usually arrive async right after project open; re-seed the
  // defaults once when they land unless the user already edited the fields.
  const touched = useRef(false);
  useEffect(() => {
    if (sceneBounds && !touched.current) {
      const seeded = seededEndpoints();
      setStart(seeded.start);
      setEnd(seeded.end);
    }
  }, [sceneBounds]);

  // Live preview of the planned path in the 3D viewer (cleared on unmount):
  // the last drawn route's polyline, or the straight start->end segment.
  const setTrajPreview = useAppStore((s) => s.setTrajPreview);
  useEffect(() => {
    const lastRoute = routes[routes.length - 1];
    setTrajPreview(lastRoute ? (lastRoute.waypoints as Vec3[]) : [start, end]);
    return () => setTrajPreview(null);
  }, [start, end, routes, setTrajPreview]);

  const pickBoth = () => {
    requestPick({
      label: "Trajectory start → end",
      count: 2,
      target: "surface",
      heightOffset: ueHeight,
      onComplete: (pts) => {
        touched.current = true;
        setStart(pts[0]);
        setEnd(pts[1]);
        // Endpoints picked off the scene surface: default follow-terrain ON so
        // the straight line between them is draped onto the ground rather than
        // cutting through it (until the user overrides the checkbox).
        if (!followTouched.current) setFollowTerrain(true);
      },
    });
  };

  // Playback timer: advance frames by dt*1000/speed; stop at the last frame.
  const dtRef = useRef(dt);
  dtRef.current = dt;
  useEffect(() => {
    if (!trajPlaying || !trajectory) return;
    const period = Math.max(30, (dtRef.current * 1000) / trajSpeed);
    const timer = setInterval(() => {
      const st = useAppStore.getState();
      const last = Math.max(0, trajectorySteps(st.trajectory) - 1);
      if (st.trajFrame >= last) {
        // Loop: wrap back to the start and keep playing; otherwise stop.
        if (st.trajLoop) {
          st.setTrajFrame(0);
        } else {
          st.setTrajPlaying(false);
        }
        return;
      }
      st.setTrajFrame(st.trajFrame + 1);
    }, period);
    return () => clearInterval(timer);
  }, [trajPlaying, trajSpeed, trajectory]);

  const disabled = busy !== null;
  // KPI card follows kpiUe AT ITS OWN frame (per-UE scrub bars can diverge
  // from the master frame).
  const ueIds = trajectoryUeIds(trajectory);
  const trajUeFrames = useAppStore((s) => s.trajUeFrames);
  const [kpiUe, setKpiUe] = useState<string>("");
  const kpiUeId = kpiUe || ueIds[0] || "";
  const kpiSamples = samplesForUe(trajectory, kpiUeId);
  const kpiFrame = Math.max(
    0,
    Math.min(kpiSamples.length - 1, trajUeFrames[kpiUeId] ?? trajFrame),
  );
  const sample = kpiSamples[kpiFrame] ?? null;

  const vecField = (label: string, v: Vec3, onChange: (v: Vec3) => void) => (
    <label className="solver-field">
      <span className="solver-field-label">{label}</span>
      <span className="traj-vec">
        {[0, 1, 2].map((i) => (
          <input
            key={i}
            type="number"
            step={1}
            value={v[i]}
            disabled={disabled}
            onChange={(e) => {
              const next: Vec3 = [...v];
              next[i] = Number(e.target.value);
              onChange(next);
            }}
          />
        ))}
      </span>
    </label>
  );

  const kpi = (label: string, value: string, title?: string) => (
    <div className="traj-kpi" title={title}>
      <span className="traj-kpi-label">{label}</span>
      <span className="traj-kpi-value mono">{value}</span>
    </div>
  );

  const fmt = (v: number | null, unit: string, digits = 1) =>
    v === null ? "—" : `${v.toFixed(digits)} ${unit}`;

  const picking = pickLabel === "Trajectory start → end";
  const drawing = pickLabel?.startsWith("Route for ") ?? false;
  return (
    <div className="traj-section">
      {routes.length === 0 && (
        <>
          <div className="panel-actions">
            <button
              className={"primary" + (picking ? " picking" : "")}
              disabled={disabled}
              title="Click two points in the 3D view: first the start, then the end (Esc cancels)"
              onClick={pickBoth}
            >
              {picking ? "Click start, then end… (Esc)" : "⌖ Pick start → end in viewport"}
            </button>
            <button
              disabled={disabled}
              title="Seed the Start/End fields: start at the current first RX position, walking toward the scene center"
              onClick={() => {
                touched.current = true;
                const seeded = seededEndpoints();
                setStart(seeded.start);
                setEnd(seeded.end);
                // Numeric/straight-line entry: default follow-terrain OFF (the
                // endpoints are absolute coordinates, not surface picks).
                if (!followTouched.current) setFollowTerrain(false);
              }}
            >
              Start at RX
            </button>
          </div>
          {vecField("Start", start, (v) => {
            touched.current = true;
            setStart(v);
            if (!followTouched.current) setFollowTerrain(false);
          })}
          {vecField("End", end, (v) => {
            touched.current = true;
            setEnd(v);
            if (!followTouched.current) setFollowTerrain(false);
          })}
          <p className="hint">
            X east · Y north · Z up (m). The dashed yellow line in the viewer
            previews this path.
          </p>
        </>
      )}
      <div className="panel-actions">
        {rxIds.length > 1 && (
          <select
            value={drawUe}
            disabled={disabled || drawing}
            title="RX device the drawn route belongs to"
            onChange={(e) => setRouteUe(e.target.value)}
          >
            {rxIds.map((id) => (
              <option key={id} value={id}>
                {id}
                {routes.some((r) => r.ue_id === id) ? " ✓" : ""}
              </option>
            ))}
          </select>
        )}
        <button
          className={drawing ? "picking" : ""}
          disabled={disabled || !drawUe}
          title="Click waypoints one by one in the 3D view; Esc finishes the route (>= 2 points)"
          onClick={drawRoute}
        >
          {drawing ? "Click points… Esc finishes" : "Draw route (Esc finishes)"}
        </button>
        <button
          disabled={disabled || !drawUe || !projectId}
          title="Load waypoints from a JSON file (cartesian x/y/z or geographic lat/lon — auto-detected; underground points are flagged). See docs/point_import.md"
          onClick={() => trajFileRef.current?.click()}
        >
          ⤓ Import JSON
        </button>
        <input
          ref={trajFileRef}
          type="file"
          accept="application/json,.json"
          style={{ display: "none" }}
          onChange={(e) => void importTrajectoryFile(e)}
        />
      </div>
      {routes.length > 0 && (
        <div className="traj-routes">
          {routes.map((r) => (
            <div key={r.ue_id} className="traj-route-row">
              <span className="mono">{r.ue_id}</span>
              <span className="hint">{r.waypoints.length} pts</span>
              {r.orientations_deg?.some((o) => o !== null) && (
                <span className="hint" title="per-waypoint antenna orientation imported">
                  · oriented
                </span>
              )}
              {(routeWarnings[r.ue_id]?.length ?? 0) > 0 && (
                <span
                  className="field-error"
                  style={{ fontSize: 11 }}
                  title={routeWarnings[r.ue_id].join("\n")}
                >
                  ⚠ {routeWarnings[r.ue_id].length}
                </span>
              )}
              <button
                className="row-del"
                title="Remove this route"
                disabled={disabled}
                onClick={() => {
                  setRoutes((rs) => rs.filter((x) => x.ue_id !== r.ue_id));
                  setRouteWarnings((w) => {
                    const next = { ...w };
                    delete next[r.ue_id];
                    return next;
                  });
                }}
              >
                ×
              </button>
            </div>
          ))}
          <p className="hint">
            Each route is resampled to the ‘Num points’ step count; all UEs move together
            per step (one solve per step). Remove every route to go back to the
            straight start → end line.
          </p>
        </div>
      )}
      <label
        className="solver-check"
        title="Drape the path onto the scene surface + UE height (outdoor); disable for indoor rooms."
      >
        <input
          type="checkbox"
          checked={followTerrain}
          disabled={disabled}
          onChange={(e) => {
            // A manual toggle takes over: provenance defaults stop overriding it.
            followTouched.current = true;
            setFollowTerrain(e.target.checked);
          }}
        />
        Follow terrain
        <span className="hint" style={{ marginLeft: 6 }}>
          drape each waypoint onto the surface below + UE height (outdoor slopes)
        </span>
      </label>
      {routes.length > 0 && (
        <label
          className="solver-check"
          title="Also solve every un-routed RX at its fixed position each step, so fixed and moving UEs share one per-frame link table"
        >
          <input
            type="checkbox"
            checked={includeStaticRx}
            disabled={disabled}
            onChange={(e) => setIncludeStaticRx(e.target.checked)}
          />
          Include fixed UEs
          <span className="hint" style={{ marginLeft: 6 }}>
            un-routed RX devices join every step at their scene position
          </span>
        </label>
      )}
      <label className="solver-field">
        <span className="solver-field-label">Num points</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={2}
            max={200}
            step={1}
            value={numPoints}
            disabled={disabled}
            onChange={(e) => setNumPoints(Math.max(2, Math.min(200, Number(e.target.value))))}
          />
        </span>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">dt</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={0.001}
            step={0.05}
            value={dt}
            disabled={disabled}
            onChange={(e) => setDt(Math.max(0.001, Number(e.target.value)))}
          />
          <span className="solver-unit">s</span>
        </span>
      </label>
      <label
        className="solver-field"
        title="Height above the clicked surface for picked/drawn waypoints (and the follow-terrain snap height) — keeps the UE above bumpy ground"
      >
        <span className="solver-field-label">UE height</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={0}
            step={0.1}
            value={ueHeight}
            disabled={disabled}
            onChange={(e) => setUeHeight(Math.max(0, Number(e.target.value)))}
          />
          <span className="solver-unit">m</span>
        </span>
      </label>
      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || disabled}
          onClick={() =>
            void simulateTrajectory(
              routes.length > 0
                ? {
                    routes,
                    num_points: numPoints,
                    dt_s: dt,
                    follow_terrain: followTerrain,
                    follow_height_m: ueHeight,
                    include_static_rx: includeStaticRx,
                  }
                : { start_m: start, end_m: end, num_points: numPoints, dt_s: dt, follow_terrain: followTerrain, follow_height_m: ueHeight },
            )
          }
        >
          Simulate trajectory{routes.length > 1 ? ` (${routes.length} UEs)` : ""}
        </button>
      </div>

      {trajectory && trajectory.samples.length > 0 && (
        <PlaybackTrajectory
          trajectory={trajectory}
          trajFrame={trajFrame}
          trajPlaying={trajPlaying}
          trajSpeed={trajSpeed}
          trajLoop={trajLoop}
          setTrajFrame={setTrajFrame}
          setTrajPlaying={setTrajPlaying}
          setTrajSpeed={setTrajSpeed}
          setTrajLoop={setTrajLoop}
          sample={sample}
          ueIds={ueIds}
          kpiUe={kpiUe || ueIds[0] || ""}
          setKpiUe={setKpiUe}
          kpi={kpi}
          fmt={fmt}
        />
      )}
    </div>
  );
}

function PlaybackTrajectory({
  trajectory,
  trajFrame,
  trajPlaying,
  trajSpeed,
  trajLoop,
  setTrajFrame,
  setTrajPlaying,
  setTrajSpeed,
  setTrajLoop,
  sample,
  ueIds,
  kpiUe,
  setKpiUe,
  kpi,
  fmt,
}: {
  trajectory: TrajectoryResultSet;
  trajFrame: number;
  trajPlaying: boolean;
  trajSpeed: number;
  trajLoop: boolean;
  setTrajFrame: (f: number) => void;
  setTrajPlaying: (p: boolean) => void;
  setTrajSpeed: (s: number) => void;
  setTrajLoop: (l: boolean) => void;
  sample: TrajectoryResultSet["samples"][number] | null;
  ueIds: string[];
  kpiUe: string;
  setKpiUe: (ue: string) => void;
  kpi: (label: string, value: string, title?: string) => JSX.Element;
  fmt: (v: number | null, unit: string, digits?: number) => string;
}) {
  const last = Math.max(0, trajectorySteps(trajectory) - 1);
  const frame = Math.min(trajFrame, last);
  const atEnd = frame >= last;
  const hasFramePaths = (sample?.paths?.length ?? 0) > 0;
  // "SINR" once any sample carries co-channel interference from another TX;
  // otherwise the metric is plain SNR (interference-free link).
  const hasInterference = trajectory.samples.some((s) => s.interference_dbm != null);

  return (
    <div className="traj-playback">
      <div className="results-meta">
        <span className="mono">{trajectory.ue_id}</span>{" "}
        <span
          className="traj-kind"
          title="Time-series over a moving receiver (each waypoint re-solved) — distinct from the fixed-device results above"
        >
          moving UE
        </span>{" "}
        <StaleChip kind="trajectory" /> · {trajectory.samples.length} sample(s)
        {ueIds.length > 1 && <> · {ueIds.length} UEs</>} · backend{" "}
        <span className="mono">{trajectory.backend}</span>
        {hasFramePaths && <> · live rays</>}
        <button
          className="row-del"
          style={{ marginLeft: 8 }}
          title="Remove this trajectory result (clears the marker/trail/ray overlay immediately)"
          onClick={() => useAppStore.getState().removeTrajectory()}
        >
          ✕ Remove
        </button>
      </div>
      {ueIds.length > 1 && (
        <>
          {/* Per-UE scrub bars: each UE can be positioned independently; the
              master ▶ all / slider resets them and moves everyone together. */}
          <div className="traj-ue-bars">
            {ueIds.map((id, i) => (
              <UeScrubBar key={id} ueId={id} color={UE_COLORS[i % UE_COLORS.length]} />
            ))}
          </div>
          <label className="solver-field">
            <span className="solver-field-label">KPI UE</span>
            <select value={kpiUe} onChange={(e) => setKpiUe(e.target.value)}>
              {ueIds.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </label>
        </>
      )}
      <div className="traj-transport">
        <button
          onClick={() => {
            if (atEnd && !trajPlaying) setTrajFrame(0);
            setTrajPlaying(!trajPlaying);
          }}
          title={
            ueIds.length > 1
              ? trajPlaying
                ? "Pause all UEs"
                : "Play ALL UEs together (resets individual scrubs)"
              : trajPlaying
                ? "Pause"
                : "Play"
          }
        >
          {trajPlaying ? "⏸" : ueIds.length > 1 ? "▶ all" : "▶"}
        </button>
        <input
          type="range"
          min={0}
          max={last}
          step={1}
          value={frame}
          onChange={(e) => setTrajFrame(Number(e.target.value))}
        />
        <span className="mono traj-frame-num">
          {frame + 1}/{last + 1}
        </span>
        <button
          className={"traj-loop" + (trajLoop ? " active" : "")}
          onClick={() => setTrajLoop(!trajLoop)}
          title={trajLoop ? "Repeat on" : "Repeat off"}
        >
          ⟳
        </button>
        <select
          value={trajSpeed}
          onChange={(e) => setTrajSpeed(Number(e.target.value))}
          title="Playback speed"
        >
          {[0.5, 1, 2, 4].map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
      </div>
      {sample && (
        <div className="traj-kpis">
          {kpi("t", `${sample.time_s.toFixed(2)} s`)}
          {kpi("pos", formatVec(sample.position, 1))}
          {kpi("RSS", fmt(sample.rss_dbm, "dBm"))}
          {kpi("Path gain", fmt(sample.path_gain_db, "dB"))}
          {kpi(
            hasInterference ? "SINR" : "SNR",
            fmt(sample.sinr_db, "dB"),
            hasInterference
              ? "S/(I+N) incl. co-channel interference from other TXs"
              : "SNR (no interference model — SINR equals SNR here)",
          )}
          {hasInterference &&
            kpi("Interference", fmt(sample.interference_dbm ?? null, "dBm"))}
          {kpi("RMS delay", fmt(sample.rms_delay_spread_ns, "ns", 2))}
          {kpi("Paths", String(sample.path_count))}
        </div>
      )}
    </div>
  );
}

// -------------------------------------------------------- scenario section

function LinkMetricsTable({ links }: { links: LinkMetrics[] }) {
  const fmt = (v: number | null, digits = 1) => (v === null ? "—" : v.toFixed(digits));
  if (links.length === 0) return <p className="hint">No links this frame.</p>;
  // Show "SINR" once any link carries co-channel interference from another TX,
  // otherwise the metric collapses to plain SNR. (Scenario LinkMetrics don't
  // carry interference today; read it defensively so this stays correct if the
  // backend adds it.)
  const hasInterference = links.some(
    (l) => (l as { interference_dbm?: number | null }).interference_dbm != null,
  );
  return (
    <table className="results-table">
      <thead>
        <tr>
          <th>tx</th>
          <th>rx</th>
          <th>RSS</th>
          {hasInterference ? (
            <th title="S/(I+N) incl. co-channel interference from other TXs">SINR</th>
          ) : (
            <th title="SNR (no interference model — SINR equals SNR here)">SNR</th>
          )}
          <th>#p</th>
        </tr>
      </thead>
      <tbody>
        {links.map((l, i) => (
          <tr key={`${l.tx_id}_${l.rx_id}_${i}`}>
            <td className="mono">{l.tx_id}</td>
            <td className="mono">{l.rx_id}</td>
            <td className="mono">{fmt(l.rss_dbm)}</td>
            <td className="mono">{fmt(l.sinr_db)}</td>
            <td className="mono">{l.path_count}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function ScenarioPlayback({ scenario }: { scenario: ScenarioResultSet }) {
  const scenarioFrame = useAppStore((s) => s.scenarioFrame);
  const scenarioPlaying = useAppStore((s) => s.scenarioPlaying);
  const scenarioSpeed = useAppStore((s) => s.scenarioSpeed);
  const scenarioLoop = useAppStore((s) => s.scenarioLoop);
  const setScenarioFrame = useAppStore((s) => s.setScenarioFrame);
  const setScenarioPlaying = useAppStore((s) => s.setScenarioPlaying);
  const setScenarioSpeed = useAppStore((s) => s.setScenarioSpeed);
  const setScenarioLoop = useAppStore((s) => s.setScenarioLoop);

  const last = scenario.frames.length - 1;
  const frameIdx = Math.min(scenarioFrame, last);
  const frame = scenario.frames[frameIdx];
  const atEnd = frameIdx >= last;

  // Frame dt for the playback period (from the frame times, fallback 0.1 s).
  const dt =
    scenario.frames.length > 1
      ? Math.max(0.001, scenario.frames[1].time_s - scenario.frames[0].time_s)
      : 0.1;

  useEffect(() => {
    if (!scenarioPlaying) return;
    const period = Math.max(30, (dt * 1000) / scenarioSpeed);
    const timer = setInterval(() => {
      const st = useAppStore.getState();
      const lastFrame = (st.scenario?.frames.length ?? 1) - 1;
      if (st.scenarioFrame >= lastFrame) {
        if (st.scenarioLoop) {
          st.setScenarioFrame(0);
        } else {
          st.setScenarioPlaying(false);
        }
        return;
      }
      st.setScenarioFrame(st.scenarioFrame + 1);
    }, period);
    return () => clearInterval(timer);
  }, [scenarioPlaying, scenarioSpeed, dt]);

  return (
    <div className="traj-playback">
      <div className="results-meta">
        {scenario.frames.length} frame(s) · backend <span className="mono">{scenario.backend}</span>
      </div>
      <div className="traj-transport">
        <button
          onClick={() => {
            if (atEnd && !scenarioPlaying) setScenarioFrame(0);
            setScenarioPlaying(!scenarioPlaying);
          }}
          title={scenarioPlaying ? "Pause" : "Play"}
        >
          {scenarioPlaying ? "⏸" : "▶"}
        </button>
        <input
          type="range"
          min={0}
          max={last}
          step={1}
          value={frameIdx}
          onChange={(e) => setScenarioFrame(Number(e.target.value))}
        />
        <span className="mono traj-frame-num">
          {frameIdx + 1}/{last + 1}
        </span>
        <button
          className={"traj-loop" + (scenarioLoop ? " active" : "")}
          onClick={() => setScenarioLoop(!scenarioLoop)}
          title={scenarioLoop ? "Repeat on" : "Repeat off"}
        >
          ⟳
        </button>
        <select
          value={scenarioSpeed}
          onChange={(e) => setScenarioSpeed(Number(e.target.value))}
          title="Playback speed"
        >
          {[0.5, 1, 2, 4].map((s) => (
            <option key={s} value={s}>
              {s}×
            </option>
          ))}
        </select>
      </div>
      <div className="results-meta">
        t = <span className="mono">{frame.time_s.toFixed(2)} s</span> ·{" "}
        {frame.actor_states.length} actor(s) · {frame.device_states.length} device(s)
        {frame.paths && <> · {frame.paths.length} path(s)</>}
      </div>
      <h4 style={{ marginTop: 8 }}>Link metrics</h4>
      <LinkMetricsTable links={frame.links} />
    </div>
  );
}

/** One UE's independent scrub bar (multi-UE playback): missing override =
 *  follows the master frame; sliding sets only this UE's frame. */
function UeScrubBar({ ueId, color }: { ueId: string; color: string }) {
  const trajectory = useAppStore((s) => s.trajectory);
  const trajFrame = useAppStore((s) => s.trajFrame);
  const frameOverride = useAppStore((s) => s.trajUeFrames[ueId]);
  const setTrajUeFrame = useAppStore((s) => s.setTrajUeFrame);
  const last = Math.max(0, trajectorySteps(trajectory) - 1);
  const frame = Math.min(frameOverride ?? trajFrame, last);
  return (
    <div className="traj-ue-bar" title={`Scrub ${ueId} independently`}>
      <span className="traj-ue-dot" style={{ background: color }} />
      <span className="mono traj-ue-name">{ueId}</span>
      <input
        type="range"
        min={0}
        max={last}
        step={1}
        value={frame}
        onChange={(e) => setTrajUeFrame(ueId, Number(e.target.value))}
      />
      <span className="mono traj-frame-num">
        {frame + 1}/{last + 1}
      </span>
    </div>
  );
}

/** "Trajectory rays" checkbox: the per-frame rays of an include_paths
 *  trajectory result, independent of the static Rays toggle (the latest
 *  computation flips these automatically; this is the manual override). */
function TrajectoryRaysToggle() {
  const trajectory = useAppStore((s) => s.trajectory);
  const showTrajectoryRays = useAppStore((s) => s.showTrajectoryRays);
  const toggleOverlay = useAppStore((s) => s.toggleOverlay);
  const has =
    trajectory !== null &&
    trajectory.samples.some((s) => (s.paths?.length ?? 0) > 0);
  return (
    <label
      className={has ? "" : "disabled"}
      title="Per-waypoint rays from the trajectory result (independent of the static Rays toggle)"
    >
      <input
        type="checkbox"
        checked={showTrajectoryRays}
        disabled={!has}
        onChange={() => toggleOverlay("trajectoryRays")}
      />{" "}
      Trajectory rays
    </label>
  );
}

/** "Scenario" checkbox in the overlay-toggles row: mirrors Rays/Radio map.
 *  Checking it hands the device/actor layers to scenario playback. */
function ScenarioOverlayToggle() {
  const scenario = useAppStore((s) => s.scenario);
  const showScenario = useAppStore((s) => s.showScenario);
  const has = scenario !== null && scenario.frames.length > 0;
  return (
    <label className={has ? "" : "disabled"} title="Scenario playback replaces the static devices/actors while ON">
      <input
        type="checkbox"
        checked={showScenario}
        disabled={!has}
        onChange={() => useAppStore.setState({ showScenario: !showScenario })}
      />{" "}
      Scenario
    </label>
  );
}

export function ScenarioSection() {
  const scenario = useAppStore((s) => s.scenario);
  const simulateScenario = useAppStore((s) => s.simulateScenario);
  const removeScenario = useAppStore((s) => s.removeScenario);
  const showScenario = useAppStore((s) => s.showScenario);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);
  const disabled = busy !== null;

  const [numFrames, setNumFrames] = useState(20);
  const [dt, setDt] = useState(0.1);
  const [includePaths, setIncludePaths] = useState(false);

  return (
    <div className="traj-section">
      <p className="hint">
        Animates each actor along its own waypoint trajectory (set per actor
        in Visual mode) frame by frame. To sweep a single receiver along a
        line, use UE trajectory instead. While the playback overlay is active it
        temporarily replaces the static device/actor markers.
      </p>
      <label className="solver-field">
        <span className="solver-field-label">Num frames</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={1}
            max={500}
            step={1}
            value={numFrames}
            disabled={disabled}
            onChange={(e) => setNumFrames(Math.max(1, Math.min(500, Number(e.target.value))))}
          />
        </span>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">dt</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={0.001}
            step={0.05}
            value={dt}
            disabled={disabled}
            onChange={(e) => setDt(Math.max(0.001, Number(e.target.value)))}
          />
          <span className="solver-unit">s</span>
        </span>
      </label>
      <label className="solver-check">
        <input
          type="checkbox"
          checked={includePaths}
          disabled={disabled}
          onChange={(e) => setIncludePaths(e.target.checked)}
        />
        Include paths (per frame)
      </label>
      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || disabled}
          onClick={() =>
            void simulateScenario({ num_frames: numFrames, dt_s: dt, include_paths: includePaths })
          }
        >
          Simulate scenario
        </button>
        {scenario && scenario.frames.length > 0 && (
          <>
            <button
              disabled={disabled}
              title="Hand the viewport to scenario playback / give it back to the static scene"
              onClick={() => useAppStore.setState({ showScenario: !showScenario })}
            >
              {showScenario ? "Hide playback" : "Show playback"}
            </button>
            <button
              disabled={disabled}
              title="Discard the loaded scenario result (viewport returns to normal)"
              onClick={removeScenario}
            >
              Clear
            </button>
            <EpochStaleChip kind="scenario" />
          </>
        )}
      </div>

      {scenario && scenario.frames.length > 0 && <ScenarioPlayback scenario={scenario} />}
    </div>
  );
}

// --------------------------------------------------------- ML dataset section

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

function formatCreatedAt(iso: string | null | undefined): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
}

/** Per-row dataset delete: two-step (× → ✓?) inline confirm, auto-reverting
 *  after ~4s. Mirrors SceneTree's RowDeleteButton armed-confirm pattern. */
function DatasetDeleteButton({
  disabled,
  onConfirm,
}: {
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
      className={"row-del" + (armed ? " armed" : "")}
      disabled={disabled}
      title={armed ? "Confirm delete dataset" : "Delete dataset"}
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
      {armed ? "✓?" : "×"}
    </button>
  );
}

export function MlDatasetSection() {
  const projectId = useAppStore((s) => s.projectId);
  const deleteDataset = useAppStore((s) => s.deleteDataset);
  // READ-ONLY: passed as the solver config for dataset generation.
  const pathsConfig = useAppStore((s) => s.pathsConfig);
  const sceneBounds = useAppStore((s) => s.sceneBounds);
  const scene = useAppStore((s) => s.scene);
  const requestPick = useAppStore((s) => s.requestPick);
  const pickLabel = useAppStore((s) => s.pick?.label ?? null);

  const [name, setName] = useState("dataset");
  const [mode, setMode] = useState<DatasetSampling["mode"]>("random");
  const [numSamples, setNumSamples] = useState(256);
  const [cfrPoints, setCfrPoints] = useState(128);
  const [heightM, setHeightM] = useState(1.5);
  // Region min/max XY for random/grid. Seeded from the real scene bounds when
  // they arrive (audit blocker: the old ±50 m constants sampled outside every
  // indoor scene, producing all-zero datasets).
  const [regionMinX, setRegionMinX] = useState(-50);
  const [regionMinY, setRegionMinY] = useState(-50);
  const [regionMaxX, setRegionMaxX] = useState(50);
  const [regionMaxY, setRegionMaxY] = useState(50);
  const [gridSpacing, setGridSpacing] = useState(2);
  // trajectory start/end XYZ.
  const [start, setStart] = useState<Vec3>([-50, 0, 1.5]);
  const [end, setEnd] = useState<Vec3>([50, 0, 1.5]);
  const [seed, setSeed] = useState(0);
  const [includePaths, setIncludePaths] = useState(false);
  const [followTerrain, setFollowTerrain] = useState(false);
  // Sample along a scene actor's authored trajectory (its own dt/speed). When
  // set it overrides the region / start-end below (precedence: actor > region).
  const [actorId, setActorId] = useState("");
  // Finite-difference step behind the velocity/doppler labels the backend adds.
  const [dtS, setDtS] = useState(0.1);
  const actorsWithTrajectory = useMemo(
    () => (scene?.actors ?? []).filter((a) => a.trajectory !== null),
    [scene],
  );

  const touched = useRef(false);
  const fitToScene = () => {
    const b = useAppStore.getState().sceneBounds;
    if (!b) return;
    // Nudge 0.3 m inside the AABB so wall-hugging samples don't start embedded
    // in the boundary geometry.
    const pad = Math.min(0.3, (b.max[0] - b.min[0]) / 10, (b.max[1] - b.min[1]) / 10);
    const r2 = (v: number) => Math.round(v * 100) / 100;
    setRegionMinX(r2(b.min[0] + pad));
    setRegionMinY(r2(b.min[1] + pad));
    setRegionMaxX(r2(b.max[0] - pad));
    setRegionMaxY(r2(b.max[1] - pad));
    // UE height: 1.5 m if it fits inside the scene's Z range, else mid-height.
    const h = b.min[2] + 1.5 < b.max[2] ? 1.5 : Math.max(0.1, (b.max[2] - b.min[2]) / 2);
    setHeightM(r2(h));
    // Grid spacing scales with the region: a fixed 2 m grid in a 6 m room
    // yields ~9 points; ~20 cells across the larger side is a useful sweep.
    const ext = Math.max(b.max[0] - b.min[0], b.max[1] - b.min[1]);
    setGridSpacing(r2(Math.min(5, Math.max(0.25, ext / 20))));
    // Trajectory default: diagonal across the region at UE height.
    setStart([r2(b.min[0] + pad), r2(b.min[1] + pad), r2(h)]);
    setEnd([r2(b.max[0] - pad), r2(b.max[1] - pad), r2(h)]);
  };
  // Bounds arrive async after project open; seed once unless the user
  // already edited the coordinates.
  useEffect(() => {
    if (sceneBounds && !touched.current) fitToScene();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sceneBounds]);

  const pickRegion = () => {
    requestPick({
      label: "Dataset region (two corners)",
      count: 2,
      target: "surface",
      heightOffset: 0,
      onComplete: ([a, b]) => {
        touched.current = true;
        setRegionMinX(Math.min(a[0], b[0]));
        setRegionMaxX(Math.max(a[0], b[0]));
        setRegionMinY(Math.min(a[1], b[1]));
        setRegionMaxY(Math.max(a[1], b[1]));
      },
    });
  };
  const pickPath = () => {
    requestPick({
      label: "Dataset path start → end",
      count: 2,
      target: "surface",
      heightOffset: heightM,
      onComplete: (pts) => {
        touched.current = true;
        setStart(pts[0]);
        setEnd(pts[1]);
      },
    });
  };

  const [datasets, setDatasets] = useState<DatasetInfo[]>([]);
  const [generating, setGenerating] = useState(false);
  const [genError, setGenError] = useState<string | null>(null);
  const [genWarnings, setGenWarnings] = useState<string[]>([]);
  const [listError, setListError] = useState<string | null>(null);

  // Fetch existing datasets on mount / project change.
  useEffect(() => {
    if (!projectId) {
      setDatasets([]);
      return;
    }
    let cancelled = false;
    setListError(null);
    api
      .listDatasets(projectId)
      .then((res) => {
        if (!cancelled) setDatasets(res.datasets);
      })
      .catch((err: unknown) => {
        if (!cancelled) {
          setListError(err instanceof ApiError ? err.message : String(err));
        }
      });
    return () => {
      cancelled = true;
    };
  }, [projectId]);

  const numField = (
    label: string,
    value: number,
    onChange: (v: number) => void,
    opts: { min?: number; max?: number; step?: number; unit?: string } = {},
  ) => (
    <label className="solver-field">
      <span className="solver-field-label">{label}</span>
      <span className="solver-field-input">
        <input
          type="number"
          min={opts.min}
          max={opts.max}
          step={opts.step ?? 1}
          value={value}
          disabled={generating}
          onChange={(e) => onChange(Number(e.target.value))}
        />
        {opts.unit && <span className="solver-unit">{opts.unit}</span>}
      </span>
    </label>
  );

  const vecField = (label: string, v: Vec3, onChange: (v: Vec3) => void) => (
    <label className="solver-field">
      <span className="solver-field-label">{label}</span>
      <span className="traj-vec">
        {[0, 1, 2].map((i) => (
          <input
            key={i}
            type="number"
            step={1}
            value={v[i]}
            disabled={generating}
            onChange={(e) => {
              const next: Vec3 = [...v];
              next[i] = Number(e.target.value);
              onChange(next);
            }}
          />
        ))}
      </span>
    </label>
  );

  const onGenerate = async () => {
    if (!projectId) return;
    setGenerating(true);
    setGenError(null);
    setGenWarnings([]);
    const sampling: DatasetSampling = {
      mode,
      height_m: heightM,
      num_samples: numSamples,
      grid_spacing_m: gridSpacing,
      seed,
      follow_terrain: followTerrain,
      dt_s: dtS,
    };
    if (actorId) {
      // Actor flight path wins (precedence: waypoints > actor_id > start/end);
      // leave region/start-end unset so the backend samples the actor's route.
      sampling.actor_id = actorId;
    } else if (mode === "random" || mode === "grid") {
      sampling.region_min = [regionMinX, regionMinY, 0];
      sampling.region_max = [regionMaxX, regionMaxY, heightM + 1];
    } else {
      sampling.start_m = start;
      sampling.end_m = end;
    }
    const req: DatasetGenerateRequest = {
      name,
      config: pathsConfig,
      sampling,
      num_cfr_points: cfrPoints,
      include_paths: includePaths,
    };
    try {
      const info = await api.generateDataset(projectId, req);
      setDatasets((prev) => [info, ...prev]);
      setGenWarnings(info.warnings);
    } catch (err: unknown) {
      setGenError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setGenerating(false);
    }
  };

  const regionMode = mode === "random" || mode === "grid";

  return (
    <div className="traj-section">
      <label className="solver-field">
        <span className="solver-field-label">Name</span>
        <span className="solver-field-input">
          <input
            type="text"
            value={name}
            disabled={generating}
            onChange={(e) => setName(e.target.value)}
          />
        </span>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Sampling mode</span>
        <select
          value={mode}
          disabled={generating}
          onChange={(e) => setMode(e.target.value as DatasetSampling["mode"])}
        >
          <option value="random">random</option>
          <option value="grid">grid</option>
          <option value="trajectory">trajectory</option>
        </select>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Actor flight path</span>
        <select
          value={actorId}
          disabled={generating}
          title="Sample along a scene actor's authored trajectory (overrides region / start-end)"
          onChange={(e) => setActorId(e.target.value)}
        >
          <option value="">— none —</option>
          {actorsWithTrajectory.map((a) => (
            <option key={a.id} value={a.id}>
              {a.name || a.id}
            </option>
          ))}
        </select>
      </label>
      {actorsWithTrajectory.length === 0 && (
        <p className="hint">No actors with a trajectory — assign one in Visual mode to enable flight-path sampling.</p>
      )}
      {numField("dt", dtS, (v) => setDtS(v), { min: 0.001, step: 0.05, unit: "s" })}
      <p className="hint">
        Sampling precedence: waypoints &gt; actor flight path &gt; start/end (or region).
        dt is the finite-difference step behind the velocity / Doppler labels.
        {actorId && " Actor flight path is active — the region / start-end below are ignored."}
      </p>
      {numField("Num samples", numSamples, (v) => setNumSamples(v), { min: 1, max: 20000 })}
      {numField("CFR points", cfrPoints, (v) => setCfrPoints(v), { min: 2, max: 4096 })}
      {numField("Height", heightM, (v) => setHeightM(v), { step: 0.1, unit: "m" })}

      {regionMode && (
        <>
          <div className="panel-actions">
            <button
              className={pickLabel === "Dataset region (two corners)" ? "picking" : ""}
              disabled={generating}
              title="Click two opposite corners of the UE sampling region in the 3D view (Esc cancels)"
              onClick={pickRegion}
            >
              {pickLabel === "Dataset region (two corners)"
                ? "Click 2 corners… (Esc)"
                : "⌖ Pick region in viewport"}
            </button>
            <button
              disabled={generating || !sceneBounds}
              title={
                sceneBounds
                  ? "Set the region and height to cover the whole scene"
                  : "Scene bounds unavailable (no visual mesh)"
              }
              onClick={() => {
                touched.current = true;
                fitToScene();
              }}
            >
              Fit to scene
            </button>
          </div>
          <div className="solver-array-grid">
            <span className="solver-array-label">Region min</span>
            <span className="traj-vec">
              <input
                type="number"
                step={1}
                value={regionMinX}
                disabled={generating}
                onChange={(e) => {
                  touched.current = true;
                  setRegionMinX(Number(e.target.value));
                }}
              />
              <input
                type="number"
                step={1}
                value={regionMinY}
                disabled={generating}
                onChange={(e) => {
                  touched.current = true;
                  setRegionMinY(Number(e.target.value));
                }}
              />
            </span>
            <span className="solver-array-label">Region max</span>
            <span className="traj-vec">
              <input
                type="number"
                step={1}
                value={regionMaxX}
                disabled={generating}
                onChange={(e) => {
                  touched.current = true;
                  setRegionMaxX(Number(e.target.value));
                }}
              />
              <input
                type="number"
                step={1}
                value={regionMaxY}
                disabled={generating}
                onChange={(e) => {
                  touched.current = true;
                  setRegionMaxY(Number(e.target.value));
                }}
              />
            </span>
          </div>
          {sceneBounds && (
            <p className="hint">
              Scene spans [{sceneBounds.min[0].toFixed(1)}, {sceneBounds.min[1].toFixed(1)}]
              …[{sceneBounds.max[0].toFixed(1)}, {sceneBounds.max[1].toFixed(1)}] m — samples
              outside it get zero paths.
            </p>
          )}
          {mode === "grid" &&
            numField("Grid spacing", gridSpacing, (v) => setGridSpacing(v), {
              min: 0.1,
              step: 0.5,
              unit: "m",
            })}
        </>
      )}

      {mode === "trajectory" && (
        <>
          <div className="panel-actions">
            <button
              className={pickLabel === "Dataset path start → end" ? "picking" : ""}
              disabled={generating}
              title="Click the path start, then the end, in the 3D view (Esc cancels)"
              onClick={pickPath}
            >
              {pickLabel === "Dataset path start → end"
                ? "Click start, then end… (Esc)"
                : "⌖ Pick path in viewport"}
            </button>
          </div>
          {vecField("Start", start, (v) => {
            touched.current = true;
            setStart(v);
          })}
          {vecField("End", end, (v) => {
            touched.current = true;
            setEnd(v);
          })}
        </>
      )}

      {numField("Seed", seed, (v) => setSeed(v), { min: 0 })}
      <label className="solver-check">
        <input
          type="checkbox"
          checked={includePaths}
          disabled={generating}
          onChange={(e) => setIncludePaths(e.target.checked)}
        />
        Include paths
      </label>
      <label className="solver-check">
        <input
          type="checkbox"
          checked={followTerrain}
          disabled={generating}
          onChange={(e) => setFollowTerrain(e.target.checked)}
        />
        Follow terrain
        <span className="hint" style={{ marginLeft: 6 }}>
          snap sample heights to the surface below (outdoor slopes)
        </span>
      </label>

      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || generating}
          onClick={() => void onGenerate()}
        >
          {generating ? "Generating…" : "Generate dataset"}
        </button>
        {generating && <span className="hint">Generating…</span>}
      </div>

      {genError && <p className="hint">Generate failed: {genError}</p>}
      {genWarnings.length > 0 && (
        <div className={"ai-note" + (genWarnings.some((w) => w.includes("zero paths")) ? " warn" : "")}>
          {genWarnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}

      {listError && <p className="hint">Could not load datasets: {listError}</p>}
      {!listError && datasets.length === 0 && (
        <p className="hint">No datasets yet.</p>
      )}
      {datasets.length > 0 && (
        <table className="results-table">
          <thead>
            <tr>
              <th>name</th>
              <th>#</th>
              <th>created</th>
              <th>size</th>
              <th>files</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {datasets.map((d) => {
              const zeroRaw = d.metadata?.num_zero_path_samples;
              const zero = typeof zeroRaw === "number" ? zeroRaw : 0;
              return (
              <tr key={d.dataset_id}>
                <td className="mono">
                  {d.name}
                  {zero > 0 && (
                    <span
                      className="dataset-flag"
                      title={`${zero}/${d.num_samples} samples have zero paths (UE outside the scene or occluded)`}
                    >
                      ⚠ {zero} zero-path
                    </span>
                  )}
                </td>
                <td className="mono">{d.num_samples}</td>
                <td className="mono">{formatCreatedAt(d.created_at)}</td>
                <td className="mono">{formatBytes(d.size_bytes)}</td>
                <td>
                  {["dataset.npz", "metadata.json"]
                    .filter((f) => d.files.includes(f))
                    .map((f) => (
                      <a
                        key={f}
                        className="mono"
                        href={projectId ? api.datasetFileUrl(projectId, d.dataset_id, f) : "#"}
                        download
                        style={{ marginRight: 8 }}
                      >
                        {f === "dataset.npz" ? "npz" : "json"}
                      </a>
                    ))}
                </td>
                <td>
                  <DatasetDeleteButton
                    disabled={generating}
                    onConfirm={() => {
                      void deleteDataset(d.dataset_id).then((ok) => {
                        if (ok) {
                          setDatasets((prev) =>
                            prev.filter((x) => x.dataset_id !== d.dataset_id),
                          );
                        }
                      });
                    }}
                  />
                </td>
              </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

// ------------------------------------------------------- beamforming card

/** Mode-aware beamforming result card. For codebook_sweep it shows the best
 *  TX/RX beam angles + codebook gain and a jet heatmap of the sweep grid; for
 *  the analytic modes it shows the single-element reference and the mode gain. */
function BeamformingCard({ beamforming: b }: { beamforming: BeamformingResult }) {
  const dB = (v: number | null, signed = true) =>
    v === null ? "—" : `${signed && v >= 0 ? "+" : ""}${v.toFixed(1)} dB`;
  const isSweep = b.mode === "codebook_sweep";

  return (
    <div className="beamforming-card">
      <h4>
        Beamforming {b.tx_array[0]}×{b.tx_array[1]} → {b.rx_array[0]}×{b.rx_array[1]}
        <span className="mono"> · {b.mode} · {b.backend}</span>
      </h4>
      <div className="results-meta">
        single element{" "}
        <span className="mono">
          {b.single_element_dbm === null ? "—" : `${b.single_element_dbm.toFixed(1)} dBm`}
        </span>{" "}
        {isSweep ? (
          <>
            · codebook <span className="mono">{dB(b.codebook_gain_db)}</span>
            {b.best_tx_angle_deg !== null && b.best_rx_angle_deg !== null && (
              <>
                {" "}
                · best TX{" "}
                <span className="mono">{b.best_tx_angle_deg.toFixed(0)}°</span> / RX{" "}
                <span className="mono">{b.best_rx_angle_deg.toFixed(0)}°</span>
              </>
            )}
          </>
        ) : b.mode === "svd" ? (
          <>
            · SVD <span className="mono">{dB(b.svd_gain_db)}</span>
          </>
        ) : (
          <>
            · TX-MRT <span className="mono">{dB(b.tx_mrt_gain_db)}</span>
          </>
        )}{" "}
        · {b.num_paths} path(s)
      </div>
      {isSweep && b.sweep_gain_db && b.sweep_gain_db.length > 0 && (
        <BeamSweepHeatmap result={b} />
      )}
      {b.warnings.length > 0 && (
        <div className="ai-note">
          {b.warnings.map((w, i) => (
            <div key={i}>{w}</div>
          ))}
        </div>
      )}
    </div>
  );
}

// ------------------------------------------------- filtered-paths CSV export

/** Export the CURRENT filtered path set as CSV (same download helper as the
 *  paper charts). One row per path; interaction materials are joined. */
function exportFilteredPathsCsv(paths: RayPath[]): void {
  const num = (v: number | null | undefined) =>
    v === null || v === undefined ? null : Math.round(v * 1000) / 1000;
  const rows = paths.map((p) => {
    const mats = [
      ...new Set(
        p.interactions.map((it) => it.rf_material_id).filter((m): m is string => m !== null),
      ),
    ].join(" ");
    return [
      p.path_id,
      p.tx_id,
      p.rx_id,
      p.path_type,
      num(p.power_dbm),
      num(p.path_gain_db),
      num(p.delay_ns),
      num(p.aod_deg?.[0] ?? null),
      num(p.aod_deg?.[1] ?? null),
      num(p.aoa_deg?.[0] ?? null),
      num(p.aoa_deg?.[1] ?? null),
      p.interactions.length,
      mats,
    ] as (string | number | null)[];
  });
  exportCsv(
    "filtered_paths",
    [
      "path_id",
      "tx",
      "rx",
      "type",
      "power_dbm",
      "path_gain_db",
      "delay_ns",
      "aod_az",
      "aod_el",
      "aoa_az",
      "aoa_el",
      "n_interactions",
      "interaction_materials",
    ],
    rows,
  );
}

// ----------------------------------------------- material-hit filter chips

/** Toggleable chips of the distinct interaction materials in the current
 *  result. Empty selection = all (mirrors the pathType "all" chip pattern). */
function MaterialFilterChips({
  present,
  materialFilter,
  toggle,
  clearAll,
  library,
}: {
  present: string[];
  materialFilter: string[];
  toggle: (id: string) => void;
  clearAll: () => void;
  library: RFMaterialLibrary | null;
}) {
  if (present.length === 0) return null;
  const allActive = materialFilter.length === 0;
  return (
    <div className="chips">
      <span className="overlay-toggles-label">Materials:</span>
      <span
        className={"chip clickable" + (allActive ? " active" : "")}
        onClick={clearAll}
      >
        all
      </span>
      {present.map((id) => {
        const mat = materialById(library, id);
        const color = mat?.preview_color ?? "#3a4450";
        const on = materialFilter.includes(id);
        return (
          <span
            key={id}
            className={"chip clickable" + (on ? " active" : "")}
            style={on ? { borderColor: color, color } : {}}
            title={mat ? `${mat.display_name} (${id})` : id}
            onClick={() => toggle(id)}
          >
            <span className="dot" style={{ background: color }} /> {mat?.display_name ?? id}
          </span>
        );
      })}
    </div>
  );
}

// ----------------------------------------------------- collapsible section

/** Lightweight collapsible wrapper (no index.css dependency): a header row that
 *  toggles its children. Inline-styled to stay self-contained. */
function Collapsible({
  title,
  defaultOpen = false,
  children,
}: {
  title: string;
  defaultOpen?: boolean;
  children: React.ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div style={{ marginTop: 10 }}>
      <div
        onClick={() => setOpen((o) => !o)}
        style={{
          cursor: "pointer",
          userSelect: "none",
          fontWeight: 600,
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        <span style={{ fontSize: "0.8em", opacity: 0.7 }}>{open ? "▾" : "▸"}</span>
        {title}
      </div>
      {open && <div style={{ marginTop: 8 }}>{children}</div>}
    </div>
  );
}

// ------------------------------------------------------- mesh radio map

/** "Mesh radio map" section: run over the current selection, toggle the
 *  overlay, and show a jet legend with the metric range + tx/backend line. */
function MeshRadioMapSection() {
  const meshRadioMap = useAppStore((s) => s.meshRadioMap);
  const simulateMeshRadioMap = useAppStore((s) => s.simulateMeshRadioMap);
  const removeMeshRadioMap = useAppStore((s) => s.removeMeshRadioMap);
  const showMeshRadioMap = useAppStore((s) => s.showMeshRadioMap);
  const toggleOverlay = useAppStore((s) => s.toggleOverlay);
  const selection = useAppStore((s) => s.selection);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);
  const disabled = !projectId || busy !== null;
  const noSelection = selection.length === 0;

  // Per-surface triangle cap sent as max_triangles (default 2000). Larger
  // budgets paint denser but cost more; the backend subsamples with a uniform
  // stride to stay under it.
  const [maxTriangles, setMaxTriangles] = useState(2000);

  const range = meshRadioMap ? meshRadioMapRange(meshRadioMap) : null;
  const unit = meshRadioMap?.metric === "rss_dbm" ? "dBm" : "dB";
  const totalTris = meshRadioMap
    ? meshRadioMap.surfaces.reduce((n, s) => n + s.triangle_count, 0)
    : 0;

  return (
    <Collapsible title="Mesh radio map">
      <p className="hint">
        Samples the RF metric on the triangles of the selected surface prims and
        drapes it on the geometry (distinct from the flat radio-map plane).
      </p>
      <label className="solver-field">
        <span className="solver-field-label">Triangle budget</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={1}
            max={20000}
            step={100}
            value={maxTriangles}
            disabled={disabled}
            onChange={(e) =>
              setMaxTriangles(Math.max(1, Math.min(20000, Number(e.target.value))))
            }
          />
        </span>
      </label>
      <p className="hint">
        Large meshes are subsampled; raise the budget for denser paint.
      </p>
      <div className="panel-actions">
        <button
          className="primary"
          disabled={disabled || noSelection}
          title={
            noSelection
              ? "Select one or more surface prims first (in Visual/RF mode)"
              : `Run over ${selection.length} selected prim(s)`
          }
          onClick={() => void simulateMeshRadioMap(maxTriangles)}
        >
          Run mesh radio map
        </button>
        {meshRadioMap && meshRadioMap.surfaces.length > 0 && (
          <button
            disabled={disabled}
            title="Discard the mesh radio map result"
            onClick={removeMeshRadioMap}
          >
            Clear
          </button>
        )}
      </div>
      {noSelection && (
        <p className="hint">No surfaces selected — the run button is disabled.</p>
      )}
      {meshRadioMap && meshRadioMap.surfaces.length > 0 && (
        <>
          <div className="overlay-toggles">
            <span className="overlay-toggles-label">Show:</span>
            <label>
              <input
                type="checkbox"
                checked={showMeshRadioMap}
                onChange={() => toggleOverlay("meshRadioMap")}
              />{" "}
              Mesh map
            </label>
          </div>
          <div className="results-meta">
            <StaleChip kind="mesh_radio_map" /> · tx{" "}
            <span className="mono">{meshRadioMap.tx_id}</span> · backend{" "}
            <span className="mono">{meshRadioMap.backend}</span> ·{" "}
            {meshRadioMap.surfaces.length} surface(s) · {totalTris} triangle(s)
          </div>
          {range && (
            <div className="results-meta">
              {meshRadioMap.metric === "rss_dbm" ? "RSS" : "Path gain"} range{" "}
              <span className="mono">
                {range[0].toFixed(1)} … {range[1].toFixed(1)} {unit}
              </span>{" "}
              (jet: low → high)
            </div>
          )}
          {meshRadioMap.warnings.length > 0 && (
            <div className="ai-note">
              {meshRadioMap.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}
        </>
      )}
    </Collapsible>
  );
}

/** "Prune results" button + inline confirm. Keeps the newest result per kind
 *  (keep_latest: 1) and drops the rest on disk, then refreshes the loaded
 *  results so any overlay whose result vanished is cleared. */
function PruneResultsButton({
  projectId,
  disabled,
  onPruned,
}: {
  projectId: string | null;
  disabled: boolean;
  onPruned: (removed: number) => Promise<void>;
}) {
  const [confirming, setConfirming] = useState(false);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const run = async () => {
    if (!projectId) return;
    setRunning(true);
    setError(null);
    try {
      const res = await api.pruneResults(projectId, { keep_latest: 1 });
      setConfirming(false);
      await onPruned(res.removed.length);
    } catch (err) {
      setError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  if (!confirming) {
    return (
      <button
        disabled={disabled}
        title="Delete older stored result files, keeping the latest one of each kind"
        onClick={() => {
          setError(null);
          setConfirming(true);
        }}
      >
        Prune results
      </button>
    );
  }

  return (
    <span className="confirm-inline">
      <span className="confirm-inline-text">Keep latest 1 per kind, delete the rest?</span>
      <button className="danger" disabled={running} onClick={() => void run()}>
        {running ? "Pruning…" : "Prune"}
      </button>
      <button disabled={running} onClick={() => setConfirming(false)}>
        Cancel
      </button>
      {error && <span className="field-error">{error}</span>}
    </span>
  );
}

/** True when a path is absolute (outside the project dir), so we can't link it
 *  through the project asset endpoint: leading POSIX slash, Windows drive
 *  (C:\), or UNC (\\host). Project-relative export dirs (e.g. "export/rfdata")
 *  return false and their files are linkable via api.assetUrl. */
function isAbsolutePath(p: string): boolean {
  return /^(\/|\\\\|[A-Za-z]:[\\/])/.test(p);
}

/** Durable, dismissible row surfacing the destination of the last RFData
 *  export (the transient notice disappears; this persists until dismissed or
 *  a project switch). Links each file through the project asset endpoint when
 *  the export lives under the project dir. */
function RfdataExportRow() {
  const lastRfdataExport = useAppStore((s) => s.lastRfdataExport);
  const dismissRfdataExport = useAppStore((s) => s.dismissRfdataExport);
  const projectId = useAppStore((s) => s.projectId);
  if (!lastRfdataExport) return null;
  const { export_dir, files } = lastRfdataExport;
  // Files are servable only when the export dir is project-relative (the
  // backend returns "export/rfdata"); an absolute dir lives outside the
  // project's asset root, so we show the names as plain text.
  const linkable = projectId !== null && !isAbsolutePath(export_dir);
  return (
    <div className="ai-note rfdata-export-row">
      <div className="rfdata-export-head">
        <span>
          Exported RFData to <span className="mono">{export_dir}</span>
        </span>
        <button
          className="row-del"
          title="Dismiss"
          onClick={dismissRfdataExport}
        >
          ×
        </button>
      </div>
      {files.length > 0 && (
        <div className="rfdata-export-files">
          {files.map((f) => {
            // Show the basename; link the project-relative path when possible.
            const name = f.split(/[\\/]/).pop() ?? f;
            return linkable ? (
              <a
                key={f}
                className="mono"
                href={api.assetUrl(projectId!, f)}
                download
                style={{ marginRight: 8 }}
              >
                {name}
              </a>
            ) : (
              <span key={f} className="mono" style={{ marginRight: 8 }}>
                {name}
              </span>
            );
          })}
        </div>
      )}
    </div>
  );
}

// ----------------------------------------------------- radio-map colormaps

/** Jet colormap (blue → cyan → green → yellow → red), t in 0..1. Matches the
 *  viewer's radio-map convention so exported/compare heatmaps read the same. */
function jet(t: number): [number, number, number] {
  const x = Math.max(0, Math.min(1, t));
  const four = 4 * x;
  const clamp = (v: number) => Math.round(Math.max(0, Math.min(1, v)) * 255);
  return [
    clamp(Math.min(four - 1.5, -four + 4.5)),
    clamp(Math.min(four - 0.5, -four + 3.5)),
    clamp(Math.min(four + 0.5, -four + 2.5)),
  ];
}

/** Diverging blue → white → red colormap for the B−A delta, t in 0..1 with
 *  0.5 = zero. Used for the compare delta heatmap. */
function diverging(t: number): [number, number, number] {
  const x = Math.max(0, Math.min(1, t));
  const lerp = (a: number, b: number, k: number) => Math.round(a + (b - a) * k);
  if (x < 0.5) {
    const k = x / 0.5; // blue (49,130,189) → white
    return [lerp(49, 255, k), lerp(130, 255, k), lerp(189, 255, k)];
  }
  const k = (x - 0.5) / 0.5; // white → red (222,45,38)
  return [lerp(255, 222, k), lerp(255, 45, k), lerp(255, 38, k)];
}

/** Coverage (fraction of non-null cells >= threshold) + mean of a radio map. */
function radioMapStats(
  rm: RadioMapResultSet,
  thresholdDbm: number | null,
): { coverage: number | null; mean: number | null; count: number } {
  let total = 0;
  let above = 0;
  let sum = 0;
  for (const row of rm.values) {
    for (const v of row) {
      if (v === null || v === undefined || !Number.isFinite(v)) continue;
      total += 1;
      sum += v;
      if (thresholdDbm !== null && v >= thresholdDbm) above += 1;
    }
  }
  return {
    coverage: thresholdDbm === null || total === 0 ? null : above / total,
    mean: total ? sum / total : null,
    count: total,
  };
}

/** Non-null value range of a radio-map grid ([0,1] fallback when empty). */
function gridRange(rm: RadioMapResultSet): [number, number] {
  let min = Infinity;
  let max = -Infinity;
  for (const row of rm.values) {
    for (const v of row) {
      if (v === null || v === undefined || !Number.isFinite(v)) continue;
      if (v < min) min = v;
      if (v > max) max = v;
    }
  }
  return Number.isFinite(min) ? [min, max] : [0, 1];
}

/** Small jet/diverging heatmap of a value grid. Canvas rows grow downward and
 *  world +Y is row iy, so we flip vertically (same as the viewer texture). */
function HeatmapCanvas({
  values,
  nx,
  ny,
  min,
  max,
  colorFn,
  cell = 3,
  title,
}: {
  values: (number | null)[][];
  nx: number;
  ny: number;
  min: number;
  max: number;
  colorFn: (t: number) => [number, number, number];
  cell?: number;
  title?: string;
}) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    canvas.width = nx * cell;
    canvas.height = ny * cell;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const span = max > min ? max - min : 1;
    for (let iy = 0; iy < ny; iy++) {
      const row = values[iy] ?? [];
      for (let ix = 0; ix < nx; ix++) {
        const v = row[ix];
        if (v === null || v === undefined || !Number.isFinite(v)) continue;
        const [r, g, b] = colorFn((v - min) / span);
        ctx.fillStyle = `rgb(${r},${g},${b})`;
        ctx.fillRect(ix * cell, (ny - 1 - iy) * cell, cell, cell);
      }
    }
  }, [values, nx, ny, min, max, colorFn, cell]);
  return (
    <canvas
      ref={ref}
      title={title}
      style={{
        imageRendering: "pixelated",
        border: "1px solid var(--border, #333)",
        maxWidth: "100%",
        height: "auto",
      }}
    />
  );
}

/** Export a radio map as a jet heatmap PNG (canvas → toBlob → download). */
function downloadRadioMapPng(rm: RadioMapResultSet): void {
  const { nx, ny } = rm.grid;
  const cell = 4;
  const canvas = document.createElement("canvas");
  canvas.width = nx * cell;
  canvas.height = ny * cell;
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, canvas.width, canvas.height);
  const [min, max] = gridRange(rm);
  const span = max > min ? max - min : 1;
  for (let iy = 0; iy < ny; iy++) {
    const row = rm.values[iy] ?? [];
    for (let ix = 0; ix < nx; ix++) {
      const v = row[ix];
      if (v === null || v === undefined || !Number.isFinite(v)) continue;
      const [r, g, b] = jet((v - min) / span);
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(ix * cell, (ny - 1 - iy) * cell, cell, cell);
    }
  }
  canvas.toBlob((blob) => {
    if (!blob) return;
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `radio_map_${rm.result_id}.png`;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 5000);
  }, "image/png");
}

// ------------------------------------------------- A/B radio-map compare

/** Pick two stored radio_map runs and render A, B, and the per-cell B−A delta
 *  as small heatmaps, with coverage/mean summary + a config-provenance chip.
 *  Also exports the currently-loaded radio map (store `radioMap`) as a PNG. */
function RadioMapCompareSection() {
  const scene = useAppStore((s) => s.scene);
  const projectId = useAppStore((s) => s.projectId);
  const radioMap = useAppStore((s) => s.radioMap);
  const notifyError = useAppStore((s) => s.notifyError);

  const radioRefs = useMemo(() => {
    const refs = (scene?.result_sets ?? []).filter((r) => r.kind === "radio_map");
    return [...refs].sort(
      (a, b) => (Date.parse(b.created_at ?? "") || 0) - (Date.parse(a.created_at ?? "") || 0),
    );
  }, [scene]);

  const [aId, setAId] = useState("");
  const [bId, setBId] = useState("");
  const [gridA, setGridA] = useState<RadioMapResultSet | null>(null);
  const [gridB, setGridB] = useState<RadioMapResultSet | null>(null);
  const [threshold, setThreshold] = useState("-90");
  const [loading, setLoading] = useState(false);

  const thresholdVal = threshold.trim() === "" ? null : Number(threshold);

  const compare = async () => {
    if (!projectId || !aId || !bId) return;
    setLoading(true);
    try {
      const [ra, rb] = await Promise.all([
        api.getRadioMap(projectId, aId),
        api.getRadioMap(projectId, bId),
      ]);
      setGridA(ra);
      setGridB(rb);
    } catch (err) {
      notifyError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const dimsMatch =
    gridA !== null &&
    gridB !== null &&
    gridA.grid.nx === gridB.grid.nx &&
    gridA.grid.ny === gridB.grid.ny;

  // Shared jet scale so A and B are directly comparable; symmetric diverging
  // scale for the delta.
  const shared = useMemo(() => {
    if (!gridA || !gridB) return null;
    const [aMin, aMax] = gridRange(gridA);
    const [bMin, bMax] = gridRange(gridB);
    return { min: Math.min(aMin, bMin), max: Math.max(aMax, bMax) };
  }, [gridA, gridB]);

  const delta = useMemo(() => {
    if (!gridA || !gridB || !dimsMatch) return null;
    const nx = gridA.grid.nx;
    const ny = gridA.grid.ny;
    const cells: (number | null)[][] = [];
    let m = 0;
    for (let iy = 0; iy < ny; iy++) {
      const rowA = gridA.values[iy] ?? [];
      const rowB = gridB.values[iy] ?? [];
      const out: (number | null)[] = [];
      for (let ix = 0; ix < nx; ix++) {
        const a = rowA[ix];
        const b = rowB[ix];
        if (a === null || a === undefined || b === null || b === undefined) {
          out.push(null);
        } else {
          const d = b - a;
          out.push(d);
          m = Math.max(m, Math.abs(d));
        }
      }
      cells.push(out);
    }
    return { cells, nx, ny, m: m > 0 ? m : 1 };
  }, [gridA, gridB, dimsMatch]);

  const statA = gridA ? radioMapStats(gridA, thresholdVal) : null;
  const statB = gridB ? radioMapStats(gridB, thresholdVal) : null;
  const configSame =
    gridA && gridB ? gridA.simulation_config_id === gridB.simulation_config_id : null;

  const pct = (v: number | null) => (v === null ? "—" : `${(v * 100).toFixed(1)}%`);
  const dbm = (v: number | null) => (v === null ? "—" : `${v.toFixed(1)} dBm`);

  return (
    <Collapsible title="A/B radio-map compare">
      <p className="hint">
        Pick two stored radio-map runs and diff them cell-by-cell. Delta is B−A
        (diverging blue → white → red); A and B share one jet scale.
      </p>
      {radioMap && (
        <div className="panel-actions">
          <button
            title="Export the currently-loaded radio map as a jet heatmap PNG"
            onClick={() => downloadRadioMapPng(radioMap)}
          >
            Download current map PNG
          </button>
        </div>
      )}
      {radioRefs.length < 2 ? (
        <p className="hint">Need at least two stored radio-map runs to compare.</p>
      ) : (
        <>
          <label className="solver-field">
            <span className="solver-field-label">Run A</span>
            <select value={aId} disabled={loading} onChange={(e) => setAId(e.target.value)}>
              <option value="">— pick a run —</option>
              {radioRefs.map((r) => (
                <option key={r.result_id} value={r.result_id}>
                  {r.label ?? r.result_id} · {formatCreatedAt(r.created_at)}
                </option>
              ))}
            </select>
          </label>
          <label className="solver-field">
            <span className="solver-field-label">Run B</span>
            <select value={bId} disabled={loading} onChange={(e) => setBId(e.target.value)}>
              <option value="">— pick a run —</option>
              {radioRefs.map((r) => (
                <option key={r.result_id} value={r.result_id}>
                  {r.label ?? r.result_id} · {formatCreatedAt(r.created_at)}
                </option>
              ))}
            </select>
          </label>
          <label className="solver-field">
            <span className="solver-field-label">Coverage ≥</span>
            <span className="solver-field-input">
              <input
                type="number"
                step={1}
                value={threshold}
                disabled={loading}
                onChange={(e) => setThreshold(e.target.value)}
              />
              <span className="solver-unit">dBm</span>
            </span>
          </label>
          <div className="panel-actions">
            <button
              className="primary"
              disabled={loading || !aId || !bId || aId === bId}
              onClick={() => void compare()}
            >
              {loading ? "Loading…" : "Compare"}
            </button>
          </div>
        </>
      )}

      {gridA && gridB && !dimsMatch && (
        <p className="hint">
          Grids differ in size ({gridA.grid.nx}×{gridA.grid.ny} vs {gridB.grid.nx}×
          {gridB.grid.ny}); cannot diff cell-by-cell.
        </p>
      )}

      {gridA && gridB && dimsMatch && shared && delta && (
        <>
          <div style={{ display: "flex", gap: 12, flexWrap: "wrap", marginTop: 8 }}>
            <div>
              <div className="hint">A</div>
              <HeatmapCanvas
                values={gridA.values}
                nx={gridA.grid.nx}
                ny={gridA.grid.ny}
                min={shared.min}
                max={shared.max}
                colorFn={jet}
                title={gridA.label ?? gridA.result_id}
              />
            </div>
            <div>
              <div className="hint">B</div>
              <HeatmapCanvas
                values={gridB.values}
                nx={gridB.grid.nx}
                ny={gridB.grid.ny}
                min={shared.min}
                max={shared.max}
                colorFn={jet}
                title={gridB.label ?? gridB.result_id}
              />
            </div>
            <div>
              <div className="hint">Δ B−A (±{delta.m.toFixed(1)} dB)</div>
              <HeatmapCanvas
                values={delta.cells}
                nx={delta.nx}
                ny={delta.ny}
                min={-delta.m}
                max={delta.m}
                colorFn={diverging}
                title="B − A (dB)"
              />
            </div>
          </div>
          <div className="results-meta">
            A: coverage <span className="mono">{pct(statA?.coverage ?? null)}</span> · mean{" "}
            <span className="mono">{dbm(statA?.mean ?? null)}</span> · B: coverage{" "}
            <span className="mono">{pct(statB?.coverage ?? null)}</span> · mean{" "}
            <span className="mono">{dbm(statB?.mean ?? null)}</span>
          </div>
          <div className="results-meta">
            <span
              className="chip"
              title="Both runs used the same simulation config id"
              style={configSame ? { borderColor: "#66bb6a", color: "#66bb6a" } : {}}
            >
              {configSame ? "config identical" : "config differs"}
            </span>
          </div>
        </>
      )}
    </Collapsible>
  );
}

// ----------------------------------------------- radio-map altitude sweep

/** Solve a planar radio map at several altitudes in one call, then chart
 *  coverage-vs-altitude. The persisted per-height runs auto-appear in the run
 *  history (labeled "h=X m" server-side) after the scene refresh. */
function AltitudeSweepSection() {
  const projectId = useAppStore((s) => s.projectId);
  const notify = useAppStore((s) => s.notify);
  const notifyError = useAppStore((s) => s.notifyError);
  const refetchScene = useAppStore((s) => s.refetchScene);
  const busy = useAppStore((s) => s.busy);

  const [heightsStr, setHeightsStr] = useState("30,60,90,120");
  const [thresholdStr, setThresholdStr] = useState("-90");
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<RadioMapSweepResult | null>(null);

  const heights = useMemo(
    () =>
      heightsStr
        .split(",")
        .map((s) => Number(s.trim()))
        .filter((v) => Number.isFinite(v)),
    [heightsStr],
  );
  const disabled = !projectId || busy !== null || running || heights.length === 0;

  const run = async () => {
    if (!projectId || heights.length === 0) return;
    setRunning(true);
    try {
      const threshold_db = thresholdStr.trim() === "" ? null : Number(thresholdStr);
      const res = await api.simulateRadioMapSweep(projectId, {
        heights_m: heights,
        threshold_db,
      });
      setResult(res);
      notify(`Altitude sweep: ${res.runs.length} run(s) at ${heights.join(", ")} m`);
      await refetchScene();
    } catch (err) {
      notifyError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setRunning(false);
    }
  };

  // Coverage-vs-altitude series (null threshold → no coverage, chart omitted).
  const coverageSeries = useMemo(() => {
    if (!result) return null;
    const pts = result.coverage.filter((c) => c.coverage !== null);
    if (pts.length === 0) return null;
    return [
      {
        label: "coverage %",
        x: pts.map((c) => c.height_m),
        y: pts.map((c) => (c.coverage === null ? null : c.coverage * 100)),
      },
    ];
  }, [result]);

  return (
    <Collapsible title="Radio-map altitude sweep">
      <p className="hint">
        One planar radio-map solve per altitude. Persisted runs appear in the run
        history labeled <span className="mono">h=X m</span>.
      </p>
      <label className="solver-field">
        <span className="solver-field-label">Heights</span>
        <span className="solver-field-input">
          <input
            type="text"
            value={heightsStr}
            disabled={running}
            placeholder="30,60,90,120"
            onChange={(e) => setHeightsStr(e.target.value)}
          />
          <span className="solver-unit">m</span>
        </span>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Coverage ≥</span>
        <span className="solver-field-input">
          <input
            type="number"
            step={1}
            value={thresholdStr}
            disabled={running}
            onChange={(e) => setThresholdStr(e.target.value)}
          />
          <span className="solver-unit">dBm</span>
        </span>
      </label>
      <div className="panel-actions">
        <button className="primary" disabled={disabled} onClick={() => void run()}>
          {running ? "Sweeping…" : "Run altitude sweep"}
        </button>
      </div>
      {result && (
        <>
          {coverageSeries ? (
            <LineChart
              title="Coverage vs altitude"
              name="coverage_vs_altitude"
              xLabel="Altitude (m)"
              yLabel="Coverage (%)"
              series={coverageSeries}
              width={420}
              height={220}
            />
          ) : (
            <p className="hint">No threshold set — coverage not computed. Run ids below.</p>
          )}
          <table className="results-table">
            <thead>
              <tr>
                <th>height</th>
                <th>result_id</th>
              </tr>
            </thead>
            <tbody>
              {result.runs.map((r) => (
                <tr key={r.result_id}>
                  <td className="mono">{r.height_m} m</td>
                  <td className="mono">{r.result_id}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </Collapsible>
  );
}

// -------------------------------------------------------- run history browser

/** Kinds in display order for the run-history grouping. */
const RUN_HISTORY_KINDS: ResultSetRef["kind"][] = [
  "paths",
  "radio_map",
  "trajectory",
  "mesh_radio_map",
  "scenario",
  "channel",
];

/** One run row with an inline-editable label. Committing (blur/Enter) PATCHes
 *  the label; labeled runs are spared by pruning. */
function RunHistoryRow({
  refItem,
  disabled,
  onLoad,
  onLabel,
}: {
  refItem: ResultSetRef;
  disabled: boolean;
  onLoad: (ref: ResultSetRef) => void;
  onLabel: (ref: ResultSetRef, label: string | null) => void;
}) {
  const [label, setLabel] = useState(refItem.label ?? "");
  // Re-seed when the underlying ref changes (e.g. after a scene refresh).
  useEffect(() => setLabel(refItem.label ?? ""), [refItem.label, refItem.result_id]);

  const commit = () => {
    const next = label.trim();
    const current = refItem.label ?? "";
    if (next === current) return;
    onLabel(refItem, next === "" ? null : next);
  };

  return (
    <tr>
      <td>
        <input
          type="text"
          className="run-label-input"
          value={label}
          placeholder="(unlabeled)"
          disabled={disabled}
          style={{ width: 120 }}
          onChange={(e) => setLabel(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") (e.target as HTMLInputElement).blur();
          }}
        />
      </td>
      <td className="mono" title={refItem.result_id}>
        {refItem.result_id}
      </td>
      <td className="mono">{refItem.backend}</td>
      <td className="mono">{formatCreatedAt(refItem.created_at)}</td>
      <td className="mono">
        {refItem.size_bytes != null ? formatBytes(refItem.size_bytes) : "—"}
      </td>
      <td>
        <button
          disabled={disabled}
          title="Load this stored run into its overlay and jump to Results"
          onClick={() => onLoad(refItem)}
        >
          Load
        </button>
      </td>
    </tr>
  );
}

/** Collapsible run-history browser: every stored ResultSetRef grouped by kind
 *  (newest first), inline label editing, per-row Load, total on-disk size, and
 *  a keep-latest-N prune. */
function RunHistorySection() {
  const scene = useAppStore((s) => s.scene);
  const projectId = useAppStore((s) => s.projectId);
  const activateResult = useAppStore((s) => s.activateResult);
  const refetchScene = useAppStore((s) => s.refetchScene);
  const notify = useAppStore((s) => s.notify);
  const notifyError = useAppStore((s) => s.notifyError);
  const busy = useAppStore((s) => s.busy);
  const disabled = !projectId || busy !== null;

  const [keepN, setKeepN] = useState(3);
  const [pruning, setPruning] = useState(false);

  const refs = scene?.result_sets ?? [];
  const totalBytes = useMemo(
    () => refs.reduce((n, r) => n + (r.size_bytes ?? 0), 0),
    [refs],
  );

  // Group by kind, newest first within each group.
  const grouped = useMemo(() => {
    const byKind = new Map<ResultSetRef["kind"], ResultSetRef[]>();
    for (const r of refs) {
      const list = byKind.get(r.kind) ?? [];
      list.push(r);
      byKind.set(r.kind, list);
    }
    for (const list of byKind.values()) {
      list.sort(
        (a, b) => (Date.parse(b.created_at ?? "") || 0) - (Date.parse(a.created_at ?? "") || 0),
      );
    }
    return byKind;
  }, [refs]);

  const onLabel = (ref: ResultSetRef, label: string | null) => {
    if (!projectId) return;
    void api
      .labelResult(projectId, ref.result_id, { label })
      .then(async () => {
        notify(label ? `Labeled run "${label}"` : "Cleared run label");
        await refetchScene();
      })
      .catch((err: unknown) =>
        notifyError(err instanceof ApiError ? err.message : String(err)),
      );
  };

  const prune = async () => {
    if (!projectId) return;
    setPruning(true);
    try {
      const res = await api.pruneResults(projectId, { keep_latest: keepN });
      notify(`Pruned ${res.removed.length} result file(s)`);
      await refetchScene();
    } catch (err) {
      notifyError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setPruning(false);
    }
  };

  return (
    <Collapsible title="Run history">
      {refs.length === 0 ? (
        <p className="hint">No stored runs yet.</p>
      ) : (
        <>
          <div className="results-meta">
            {refs.length} stored run(s) · total on disk{" "}
            <span className="mono">{formatBytes(totalBytes)}</span>
          </div>
          {RUN_HISTORY_KINDS.filter((k) => (grouped.get(k)?.length ?? 0) > 0).map((kind) => (
            <div key={kind} style={{ marginTop: 8 }}>
              <h4>
                {kind} ({grouped.get(kind)!.length})
              </h4>
              <table className="results-table">
                <thead>
                  <tr>
                    <th>label</th>
                    <th>result_id</th>
                    <th>backend</th>
                    <th>created</th>
                    <th>size</th>
                    <th></th>
                  </tr>
                </thead>
                <tbody>
                  {grouped.get(kind)!.map((r) => (
                    <RunHistoryRow
                      key={r.result_id}
                      refItem={r}
                      disabled={disabled}
                      onLoad={(ref) => void activateResult(ref)}
                      onLabel={onLabel}
                    />
                  ))}
                </tbody>
              </table>
            </div>
          ))}
        </>
      )}
      <div className="panel-actions" style={{ marginTop: 10 }}>
        <label className="solver-field">
          <span className="solver-field-label">Keep latest</span>
          <span className="solver-field-input">
            <input
              type="number"
              min={0}
              max={100}
              step={1}
              value={keepN}
              disabled={disabled || pruning}
              onChange={(e) => setKeepN(Math.max(0, Math.min(100, Number(e.target.value))))}
            />
            <span className="solver-unit">per kind</span>
          </span>
        </label>
        <button
          className="danger"
          disabled={disabled || pruning}
          title="Delete older stored result files, keeping the newest N of each kind"
          onClick={() => void prune()}
        >
          {pruning ? "Pruning…" : "Prune old results"}
        </button>
      </div>
      <p className="hint">Labeled runs are spared server-side even if they fall outside the keep window.</p>
    </Collapsible>
  );
}

export default function ResultExplorer() {
  const pathResults = useAppStore((s) => s.pathResults);
  const selectedPathId = useAppStore((s) => s.selectedPathId);
  const selectPath = useAppStore((s) => s.selectPath);
  const simulatePaths = useAppStore((s) => s.simulatePaths);
  const simulateRadioMap = useAppStore((s) => s.simulateRadioMap);
  const runBeamforming = useAppStore((s) => s.runBeamforming);
  const radioMap = useAppStore((s) => s.radioMap);
  const beamforming = useAppStore((s) => s.beamforming);
  const materials = useAppStore((s) => s.materials);
  const showPaths = useAppStore((s) => s.showPaths);
  const showRadioMap = useAppStore((s) => s.showRadioMap);
  const showBeamforming = useAppStore((s) => s.showBeamforming);
  const toggleOverlay = useAppStore((s) => s.toggleOverlay);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);

  // Filter state lives in the store so the viewer and table stay in sync.
  const filter = useAppStore((s) => s.pathTypeFilter);
  const setFilter = useAppStore((s) => s.setPathTypeFilter);
  const strongestN = useAppStore((s) => s.strongestN);
  const setStrongestN = useAppStore((s) => s.setStrongestN);
  const minPowerDbm = useAppStore((s) => s.minPowerDbm);
  const setMinPowerDbm = useAppStore((s) => s.setMinPowerDbm);
  const colorBy = useAppStore((s) => s.colorBy);
  const hiddenLinkDevices = useAppStore((s) => s.hiddenLinkDevices);
  const toggleLinkDevice = useAppStore((s) => s.toggleLinkDevice);
  const setHiddenLinkDevices = useAppStore((s) => s.setHiddenLinkDevices);
  const materialFilter = useAppStore((s) => s.materialFilter);
  const toggleMaterialFilter = useAppStore((s) => s.toggleMaterialFilter);
  const setMaterialFilter = useAppStore((s) => s.setMaterialFilter);

  // Reset every path filter to its default so a hidden set becomes visible again.
  const resetFilters = () => {
    setFilter("all");
    setStrongestN(50);
    setMinPowerDbm(null);
    setHiddenLinkDevices([]);
    setMaterialFilter([]);
  };

  // After a prune, re-pull the latest of each result kind. A kind whose file
  // was removed now 404s, so we null it out (clearing its overlay) rather than
  // leaving a stale result — and its "selected path" if the paths set vanished.
  const refreshAfterPrune = async (removed: number) => {
    if (!projectId) return;
    const [paths, rmap, mesh, traj, scen] = await Promise.all([
      api.getPathResults(projectId).catch(() => null),
      api.getRadioMap(projectId).catch(() => null),
      // Fetch mesh directly (not the store's best-effort fetch, which never
      // nulls) so a pruned-away mesh map clears its overlay too.
      api.getMeshRadioMapResult(projectId).catch(() => null),
      api.getTrajectory(projectId).catch(() => null),
      api.getScenario(projectId).catch(() => null),
    ]);
    // Guard against a project switch racing the prune refresh.
    if (useAppStore.getState().projectId !== projectId) return;
    useAppStore.setState((st) => ({
      pathResults: paths,
      selectedPathId: paths ? st.selectedPathId : null,
      radioMap: rmap,
      meshRadioMap: mesh,
      trajectory: traj,
      trajFrame: 0,
      scenario: scen,
      scenarioFrame: 0,
      showScenario: scen ? st.showScenario : false,
      notice: removed > 0 ? `Pruned ${removed} result file(s)` : "No results to prune",
    }));
  };

  const [sortKey, setSortKey] = useState<SortKey>("power_dbm");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);

  const presentTypes = useMemo(() => {
    const types = new Set<PathType>();
    for (const p of pathResults?.paths ?? []) types.add(p.path_type);
    return [...types];
  }, [pathResults]);

  // Device ids participating in any path, for the AODT-style filter chips.
  const linkDevices = useMemo(() => {
    const txs = new Set<string>();
    const rxs = new Set<string>();
    for (const p of pathResults?.paths ?? []) {
      txs.add(p.tx_id);
      rxs.add(p.rx_id);
    }
    return { txs: [...txs].sort(), rxs: [...rxs].sort() };
  }, [pathResults]);

  // Distinct RF materials hit by any interaction in the current result, for the
  // material-hit filter chips (mirrors the per-link chips).
  const presentMaterials = useMemo(() => {
    const ids = new Set<string>();
    for (const p of pathResults?.paths ?? []) {
      for (const it of p.interactions) {
        if (it.rf_material_id) ids.add(it.rf_material_id);
      }
    }
    return [...ids].sort();
  }, [pathResults]);

  // The set the viewer draws (type + material + min power + strongest N).
  const visible = useMemo(
    () =>
      filterPaths(pathResults?.paths ?? [], {
        pathTypeFilter: filter,
        strongestN,
        minPowerDbm,
        hiddenLinkDevices,
        materialFilter,
      }),
    [pathResults, filter, strongestN, minPowerDbm, hiddenLinkDevices, materialFilter],
  );
  const range = useMemo(() => powerRange(visible), [visible]);

  const sorted = useMemo(() => {
    const value = (p: RayPath): string | number => {
      switch (sortKey) {
        case "path_id":
          return p.path_id;
        case "path_type":
          return p.path_type;
        case "power_dbm":
          return p.power_dbm;
        case "delay_ns":
          return p.delay_ns;
        case "interactions":
          return p.interactions.length;
      }
    };
    return [...visible].sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      if (va < vb) return -sortDir;
      if (va > vb) return sortDir;
      return 0;
    });
  }, [visible, sortKey, sortDir]);

  const toggleSort = (key: SortKey) => {
    if (sortKey === key) {
      setSortDir((d) => (d === 1 ? -1 : 1));
    } else {
      setSortKey(key);
      setSortDir(key === "path_id" || key === "path_type" ? 1 : -1);
    }
  };

  const sortMark = (key: SortKey) => (sortKey === key ? (sortDir === 1 ? " ↑" : " ↓") : "");

  const selectedPath = pathResults?.paths.find((p) => p.path_id === selectedPathId) ?? null;

  return (
    <>
      {/* Channel/Trajectory/Scenario/ML-dataset cards are dockable panels now
          (PanelHost registry): they render in the sidebar of the user's
          choice or float over the viewport, and survive mode switches. */}
      <div className="panel">
        <h3 className="panel-title">Results</h3>
      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || busy !== null}
          onClick={() => void simulatePaths()}
        >
          Simulate paths
        </button>
        <button
          disabled={!projectId || busy !== null}
          onClick={() => void simulateRadioMap()}
          title="Compute a planar radio map (shown as a heatmap in the viewer)"
        >
          Simulate radio map
        </button>
        <button
          disabled={!projectId || busy !== null}
          onClick={() => void runBeamforming()}
          title="MIMO beamforming gain (TX-MRT and both-ends SVD)"
        >
          Beamforming
        </button>
        <PruneResultsButton
          projectId={projectId}
          disabled={!projectId || busy !== null}
          onPruned={refreshAfterPrune}
        />
      </div>

      <RfdataExportRow />

      {(linkDevices.txs.length > 1 || linkDevices.rxs.length > 1) && (
        // AODT-style per-link filter chips: toggle a TX/RX to hide its links.
        <div className="link-chips">
          <span className="overlay-toggles-label">Links:</span>
          {[...linkDevices.txs, ...linkDevices.rxs].map((id) => {
            const isTx = linkDevices.txs.includes(id);
            const off = hiddenLinkDevices.includes(id);
            return (
              <button
                key={id}
                className={"link-chip" + (off ? " off" : "") + (isTx ? " tx" : " rx")}
                title={(off ? "Show" : "Hide") + " links of " + id}
                onClick={() => toggleLinkDevice(id)}
              >
                {id}
              </button>
            );
          })}
          <button className="link-chip" onClick={() => setHiddenLinkDevices([])}>
            All
          </button>
        </div>
      )}
      <div className="overlay-toggles">
        <span className="overlay-toggles-label">Show:</span>
        <label className={pathResults ? "" : "disabled"}>
          <input
            type="checkbox"
            checked={showPaths}
            disabled={!pathResults}
            onChange={() => toggleOverlay("paths")}
          />{" "}
          Rays
        </label>
        <label className={radioMap ? "" : "disabled"}>
          <input
            type="checkbox"
            checked={showRadioMap}
            disabled={!radioMap}
            onChange={() => toggleOverlay("radioMap")}
          />{" "}
          Radio map
        </label>
        <label className={beamforming ? "" : "disabled"}>
          <input
            type="checkbox"
            checked={showBeamforming}
            disabled={!beamforming}
            onChange={() => toggleOverlay("beamforming")}
          />{" "}
          Beamforming
        </label>
        <TrajectoryRaysToggle />
        <ScenarioOverlayToggle />
      </div>

      {beamforming && showBeamforming && <BeamformingCard beamforming={beamforming} />}

      {!pathResults ? (
        <div className="empty-state">
          No path results yet. Run a simulation — the mock backend works without Sionna or a GPU.
        </div>
      ) : (
        <>
          <div className="results-meta">
            <span className="mono">{pathResults.result_id}</span> <StaleChip kind="paths" /> · backend{" "}
            <span className="mono">{pathResults.backend}</span> · config{" "}
            <span className="mono">{pathResults.simulation_config_id}</span>
            {pathResults.created_at && <> · {new Date(pathResults.created_at).toLocaleString()}</>}
            {" · "}
            {pathResults.paths.length} path(s)
            {visible.length !== pathResults.paths.length && (
              <> · showing {visible.length}</>
            )}
            {radioMap && (
              <>
                {" "}
                · radio map <span className="mono">{radioMap.result_id}</span>
                {" · "}
                {radioMap.metric === "sinr_db"
                  ? "SINR (dB)"
                  : radioMap.metric === "rss_dbm"
                    ? "RSS (dBm)"
                    : "Path gain (dB)"}
              </>
            )}
          </div>
          {radioMap && radioMap.serving_tx && (
            <div className="results-meta">
              serving-TX association available ({radioMap.tx_ids.length} TX)
            </div>
          )}
          {pathResults.warnings.length > 0 && (
            <div className="ai-note">
              {pathResults.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}

          <ViewerControls range={range} />

          <div className="chips">
            <span
              className={"chip clickable" + (filter === "all" ? " active" : "")}
              onClick={() => setFilter("all")}
            >
              all ({pathResults.paths.length})
            </span>
            {presentTypes.map((t) => (
              <span
                key={t}
                className={"chip clickable" + (filter === t ? " active" : "")}
                style={filter === t ? { borderColor: PATH_COLORS[t], color: PATH_COLORS[t] } : {}}
                onClick={() => setFilter(t)}
              >
                {t} ({pathResults.paths.filter((p) => p.path_type === t).length})
              </span>
            ))}
          </div>

          <MaterialFilterChips
            present={presentMaterials}
            materialFilter={materialFilter}
            toggle={toggleMaterialFilter}
            clearAll={() => setMaterialFilter([])}
            library={materials}
          />

          {visible.length === 0 && pathResults.paths.length > 0 ? (
            // All paths hidden by the active filters: the table/scatter would
            // silently vanish, so surface a recoverable empty state (F10).
            <div className="empty-state filters-empty">
              <div>All {pathResults.paths.length} paths hidden by current filters</div>
              <button className="primary" style={{ marginTop: 8 }} onClick={resetFilters}>
                Reset filters
              </button>
            </div>
          ) : (
            <>
              <div className="panel-actions">
                <button
                  disabled={visible.length === 0}
                  title="Download the currently filtered paths as CSV"
                  onClick={() => exportFilteredPathsCsv(visible)}
                >
                  Export filtered CSV ({visible.length})
                </button>
              </div>
              <table className="results-table">
                <thead>
                  <tr>
                    <th onClick={() => toggleSort("path_id")}>path{sortMark("path_id")}</th>
                    <th onClick={() => toggleSort("path_type")}>type{sortMark("path_type")}</th>
                    <th onClick={() => toggleSort("power_dbm")}>dBm{sortMark("power_dbm")}</th>
                    <th onClick={() => toggleSort("delay_ns")}>ns{sortMark("delay_ns")}</th>
                    <th onClick={() => toggleSort("interactions")}>#int{sortMark("interactions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {sorted.map((p) => (
                    <tr
                      key={p.path_id}
                      className={p.path_id === selectedPathId ? "selected" : ""}
                      onClick={() => selectPath(p.path_id === selectedPathId ? null : p.path_id)}
                    >
                      <td className="mono">{p.path_id}</td>
                      <td>
                        <span className="path-type">
                          <span
                            className="dot"
                            style={{ background: pathColor(p, colorBy, range) }}
                            title={colorBy === "depth" ? `depth ${pathDepth(p)}` : undefined}
                          />
                          {p.path_type}
                        </span>
                      </td>
                      <td className="mono">{p.power_dbm.toFixed(1)}</td>
                      <td className="mono">{p.delay_ns.toFixed(1)}</td>
                      <td className="mono">{p.interactions.length}</td>
                    </tr>
                  ))}
                </tbody>
              </table>

              {selectedPath && <PathDetail path={selectedPath} />}

              {visible.length > 0 && (
                <DelayPowerScatter
                  paths={visible}
                  selectedPathId={selectedPathId}
                  onSelect={(id) => selectPath(id)}
                />
              )}

              <Collapsible title="AoA / AoD">
                <AngularPlot paths={visible} />
              </Collapsible>
            </>
          )}
        </>
      )}

      <MeshRadioMapSection />
      <AltitudeSweepSection />
      <RadioMapCompareSection />
      <RunHistorySection />
      </div>
    </>
  );
}
