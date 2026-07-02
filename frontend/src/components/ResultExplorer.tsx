import { useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import ChannelPanel from "./ChannelPanel";
import { PATH_COLORS, SELECTED_PATH_COLOR, formatVec } from "./common";
import { filterPaths, pathColor, pathDepth, powerRange } from "../pathFilter";
import type { ColorBy } from "../store/appStore";
import type {
  LinkMetrics,
  PathType,
  RayPath,
  ScenarioResultSet,
  TrajectoryResultSet,
  Vec3,
} from "../types/api";

const SELECTED_COLOR = SELECTED_PATH_COLOR;

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
  return (
    <div className="path-detail">
      <h4>
        {path.path_id} · <span style={{ color: PATH_COLORS[path.path_type] }}>{path.path_type}</span>
      </h4>
      <div className="results-meta">
        <span className="mono">{path.tx_id}</span> → <span className="mono">{path.rx_id}</span> ·{" "}
        {path.power_dbm.toFixed(1)} dBm · {path.delay_ns.toFixed(1)} ns · phase{" "}
        {path.phase_rad.toFixed(2)} rad
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

  return (
    <div className="viewer-controls">
      <label className="solver-slider">
        <span className="solver-slider-head">
          <span>Strongest N</span>
          <span className="mono solver-slider-value">{strongestN}</span>
        </span>
        <input
          type="range"
          min={5}
          max={200}
          step={5}
          value={strongestN}
          onChange={(e) => setStrongestN(Number(e.target.value))}
        />
      </label>

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
          onChange={(e) => setMinPowerDbm(Number(e.target.value))}
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

function TrajectorySection() {
  const trajectory = useAppStore((s) => s.trajectory);
  const trajFrame = useAppStore((s) => s.trajFrame);
  const trajPlaying = useAppStore((s) => s.trajPlaying);
  const trajSpeed = useAppStore((s) => s.trajSpeed);
  const setTrajFrame = useAppStore((s) => s.setTrajFrame);
  const setTrajPlaying = useAppStore((s) => s.setTrajPlaying);
  const setTrajSpeed = useAppStore((s) => s.setTrajSpeed);
  const simulateTrajectory = useAppStore((s) => s.simulateTrajectory);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);

  // Default: current first rx position -> +30 m in x.
  const [start, setStart] = useState<Vec3>(() => firstRxPosition());
  const [end, setEnd] = useState<Vec3>(() => {
    const s = firstRxPosition();
    return [s[0] + 30, s[1], s[2]];
  });
  const [numPoints, setNumPoints] = useState(8);
  const [dt, setDt] = useState(0.1);

  // Playback timer: advance frames by dt*1000/speed; stop at the last frame.
  const dtRef = useRef(dt);
  dtRef.current = dt;
  useEffect(() => {
    if (!trajPlaying || !trajectory) return;
    const period = Math.max(30, (dtRef.current * 1000) / trajSpeed);
    const timer = setInterval(() => {
      const st = useAppStore.getState();
      const last = (st.trajectory?.samples.length ?? 1) - 1;
      if (st.trajFrame >= last) {
        st.setTrajPlaying(false);
        return;
      }
      st.setTrajFrame(st.trajFrame + 1);
    }, period);
    return () => clearInterval(timer);
  }, [trajPlaying, trajSpeed, trajectory]);

  const disabled = busy !== null;
  const sample = trajectory?.samples[Math.min(trajFrame, trajectory.samples.length - 1)] ?? null;

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

  const kpi = (label: string, value: string) => (
    <div className="traj-kpi">
      <span className="traj-kpi-label">{label}</span>
      <span className="traj-kpi-value mono">{value}</span>
    </div>
  );

  const fmt = (v: number | null, unit: string, digits = 1) =>
    v === null ? "n/a" : `${v.toFixed(digits)} ${unit}`;

  return (
    <div className="traj-section">
      <h4>Trajectory</h4>
      {vecField("Start", start, setStart)}
      {vecField("End", end, setEnd)}
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
      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || disabled}
          onClick={() =>
            void simulateTrajectory({ start_m: start, end_m: end, num_points: numPoints, dt_s: dt })
          }
        >
          Simulate trajectory
        </button>
      </div>

      {trajectory && trajectory.samples.length > 0 && (
        <PlaybackTrajectory
          trajectory={trajectory}
          trajFrame={trajFrame}
          trajPlaying={trajPlaying}
          trajSpeed={trajSpeed}
          setTrajFrame={setTrajFrame}
          setTrajPlaying={setTrajPlaying}
          setTrajSpeed={setTrajSpeed}
          sample={sample}
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
  setTrajFrame,
  setTrajPlaying,
  setTrajSpeed,
  sample,
  kpi,
  fmt,
}: {
  trajectory: TrajectoryResultSet;
  trajFrame: number;
  trajPlaying: boolean;
  trajSpeed: number;
  setTrajFrame: (f: number) => void;
  setTrajPlaying: (p: boolean) => void;
  setTrajSpeed: (s: number) => void;
  sample: TrajectoryResultSet["samples"][number] | null;
  kpi: (label: string, value: string) => JSX.Element;
  fmt: (v: number | null, unit: string, digits?: number) => string;
}) {
  const last = trajectory.samples.length - 1;
  const frame = Math.min(trajFrame, last);
  const atEnd = frame >= last;

  return (
    <div className="traj-playback">
      <div className="results-meta">
        <span className="mono">{trajectory.ue_id}</span> · {trajectory.samples.length} sample(s) ·
        backend <span className="mono">{trajectory.backend}</span>
      </div>
      <div className="traj-transport">
        <button
          onClick={() => {
            if (atEnd && !trajPlaying) setTrajFrame(0);
            setTrajPlaying(!trajPlaying);
          }}
          title={trajPlaying ? "Pause" : "Play"}
        >
          {trajPlaying ? "⏸" : "▶"}
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
          {kpi("SINR", fmt(sample.sinr_db, "dB"))}
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
  return (
    <table className="results-table">
      <thead>
        <tr>
          <th>tx</th>
          <th>rx</th>
          <th>RSS</th>
          <th>SINR</th>
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
  const setScenarioFrame = useAppStore((s) => s.setScenarioFrame);
  const setScenarioPlaying = useAppStore((s) => s.setScenarioPlaying);
  const setScenarioSpeed = useAppStore((s) => s.setScenarioSpeed);

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
        st.setScenarioPlaying(false);
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

function ScenarioSection() {
  const scenario = useAppStore((s) => s.scenario);
  const simulateScenario = useAppStore((s) => s.simulateScenario);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);
  const disabled = busy !== null;

  const [numFrames, setNumFrames] = useState(20);
  const [dt, setDt] = useState(0.1);
  const [includePaths, setIncludePaths] = useState(false);

  return (
    <div className="traj-section">
      <h4>Scenario (V2X)</h4>
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
      </div>

      {scenario && scenario.frames.length > 0 && <ScenarioPlayback scenario={scenario} />}
    </div>
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
  const minPowerDbm = useAppStore((s) => s.minPowerDbm);
  const colorBy = useAppStore((s) => s.colorBy);

  const [sortKey, setSortKey] = useState<SortKey>("power_dbm");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);

  const presentTypes = useMemo(() => {
    const types = new Set<PathType>();
    for (const p of pathResults?.paths ?? []) types.add(p.path_type);
    return [...types];
  }, [pathResults]);

  // The set the viewer draws (type filter + min power + strongest N).
  const visible = useMemo(
    () =>
      filterPaths(pathResults?.paths ?? [], {
        pathTypeFilter: filter,
        strongestN,
        minPowerDbm,
      }),
    [pathResults, filter, strongestN, minPowerDbm],
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
      <ChannelPanel />
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
      </div>

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
      </div>

      {beamforming && showBeamforming && (
        <div className="beamforming-card">
          <h4>
            Beamforming {beamforming.tx_array[0]}×{beamforming.tx_array[1]} →{" "}
            {beamforming.rx_array[0]}×{beamforming.rx_array[1]}
            <span className="mono"> · {beamforming.backend}</span>
          </h4>
          <div className="results-meta">
            single element{" "}
            <span className="mono">
              {beamforming.single_element_dbm === null
                ? "n/a"
                : `${beamforming.single_element_dbm.toFixed(1)} dBm`}
            </span>{" "}
            · TX-MRT{" "}
            <span className="mono">
              {beamforming.tx_mrt_gain_db === null
                ? "n/a"
                : `+${beamforming.tx_mrt_gain_db.toFixed(1)} dB`}
            </span>{" "}
            · SVD{" "}
            <span className="mono">
              {beamforming.svd_gain_db === null
                ? "n/a"
                : `+${beamforming.svd_gain_db.toFixed(1)} dB`}
            </span>{" "}
            · {beamforming.num_paths} path(s)
          </div>
          {beamforming.warnings.length > 0 && (
            <div className="ai-note">
              {beamforming.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}
        </div>
      )}

      {!pathResults ? (
        <div className="empty-state">
          No path results yet. Run a simulation — the mock backend works without Sionna or a GPU.
        </div>
      ) : (
        <>
          <div className="results-meta">
            <span className="mono">{pathResults.result_id}</span> · backend{" "}
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
              </>
            )}
          </div>
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
        </>
      )}

        <TrajectorySection />
        <ScenarioSection />
      </div>
    </>
  );
}
