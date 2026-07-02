import { useMemo, useState } from "react";
import { useAppStore } from "../store/appStore";
import { formatVec } from "./common";
import type { PathType, RayPath } from "../types/api";

const PATH_COLORS: Record<PathType, string> = {
  los: "#66bb6a",
  reflection: "#4fc3f7",
  diffraction: "#ab47bc",
  scattering: "#ffa726",
  transmission: "#f06292",
  mixed: "#eceff1",
};

const SELECTED_COLOR = "#ffee58";

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

export default function ResultExplorer() {
  const pathResults = useAppStore((s) => s.pathResults);
  const selectedPathId = useAppStore((s) => s.selectedPathId);
  const selectPath = useAppStore((s) => s.selectPath);
  const simulatePaths = useAppStore((s) => s.simulatePaths);
  const simulateRadioMap = useAppStore((s) => s.simulateRadioMap);
  const radioMap = useAppStore((s) => s.radioMap);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);

  const [filter, setFilter] = useState<PathType | "all">("all");
  const [sortKey, setSortKey] = useState<SortKey>("power_dbm");
  const [sortDir, setSortDir] = useState<1 | -1>(-1);

  const presentTypes = useMemo(() => {
    const types = new Set<PathType>();
    for (const p of pathResults?.paths ?? []) types.add(p.path_type);
    return [...types];
  }, [pathResults]);

  const filtered = useMemo(() => {
    const paths = (pathResults?.paths ?? []).filter(
      (p) => filter === "all" || p.path_type === filter,
    );
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
    return [...paths].sort((a, b) => {
      const va = value(a);
      const vb = value(b);
      if (va < vb) return -sortDir;
      if (va > vb) return sortDir;
      return 0;
    });
  }, [pathResults, filter, sortKey, sortDir]);

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
      </div>

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
              {filtered.map((p) => (
                <tr
                  key={p.path_id}
                  className={p.path_id === selectedPathId ? "selected" : ""}
                  onClick={() => selectPath(p.path_id === selectedPathId ? null : p.path_id)}
                >
                  <td className="mono">{p.path_id}</td>
                  <td>
                    <span className="path-type">
                      <span className="dot" style={{ background: PATH_COLORS[p.path_type] }} />
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

          {pathResults.paths.length > 0 && (
            <DelayPowerScatter
              paths={pathResults.paths}
              selectedPathId={selectedPathId}
              onSelect={(id) => selectPath(id)}
            />
          )}
        </>
      )}
    </div>
  );
}
