/**
 * Beam codebook-sweep heatmap.
 *
 * Renders BeamformingResult.sweep_gain_db (a [rx][tx] grid of gains in dB) as a
 * jet-colormapped SVG heatmap: x axis = TX beam angle, y axis = RX beam angle.
 * The best (TX, RX) pair is marked with a crosshair. Colors use the same jet
 * palette as the radio-map/legend so the whole app reads consistently.
 */

import { useMemo } from "react";
import { jetCss } from "../pathFilter";
import type { BeamformingResult } from "../types/api";

interface Extent {
  min: number;
  max: number;
}

function gainExtent(grid: (number | null)[][]): Extent {
  let min = Infinity;
  let max = -Infinity;
  for (const row of grid) {
    for (const v of row) {
      if (v !== null && Number.isFinite(v)) {
        if (v < min) min = v;
        if (v > max) max = v;
      }
    }
  }
  if (!Number.isFinite(min)) return { min: 0, max: 1 };
  if (max - min < 1e-9) max = min + 1;
  return { min, max };
}

export default function BeamSweepHeatmap({ result }: { result: BeamformingResult }) {
  const grid = result.sweep_gain_db;
  const angles = result.sweep_angles_deg;

  const extent = useMemo(() => (grid ? gainExtent(grid) : { min: 0, max: 1 }), [grid]);

  if (!grid || grid.length === 0 || angles.length === 0) {
    return <p className="hint">No sweep grid returned.</p>;
  }

  const nRx = grid.length;
  const nTx = grid[0]?.length ?? 0;
  if (nTx === 0) return <p className="hint">No sweep grid returned.</p>;

  // Layout. Cells are drawn as a fixed-size grid; the whole SVG scrolls inside
  // its container on narrow panels.
  const cell = Math.max(8, Math.min(22, Math.round(240 / Math.max(nTx, nRx))));
  const L = 40; // left margin (rx angle labels)
  const B = 26; // bottom margin (tx angle labels)
  const T = 8;
  const R = 8;
  const gw = nTx * cell;
  const gh = nRx * cell;
  const W = L + gw + R;
  const H = T + gh + B;

  // Best-pair indices: match the reported best angles to the sweep axis. Row =
  // rx angle, col = tx angle. Falls back to argmax over the grid if the angles
  // aren't present.
  const idxOf = (deg: number | null): number | null => {
    if (deg === null) return null;
    let best = 0;
    let bestD = Infinity;
    for (let i = 0; i < angles.length; i++) {
      const d = Math.abs(angles[i] - deg);
      if (d < bestD) {
        bestD = d;
        best = i;
      }
    }
    return best;
  };
  let bestTx = idxOf(result.best_tx_angle_deg);
  let bestRx = idxOf(result.best_rx_angle_deg);
  if (bestTx === null || bestRx === null) {
    let bv = -Infinity;
    for (let r = 0; r < nRx; r++) {
      for (let t = 0; t < nTx; t++) {
        const v = grid[r]?.[t];
        if (v !== null && v !== undefined && v > bv) {
          bv = v;
          bestRx = r;
          bestTx = t;
        }
      }
    }
  }

  const norm = (v: number) => (v - extent.min) / (extent.max - extent.min);

  // Axis tick labels: first, middle, last angle on each axis.
  const tickIdx = [0, Math.floor((angles.length - 1) / 2), angles.length - 1].filter(
    (v, i, a) => a.indexOf(v) === i,
  );

  return (
    <div className="scatter-wrap beam-heatmap">
      <h4>Codebook sweep (gain dB)</h4>
      <div className="beam-heatmap-scroll">
        <svg width={W} height={H}>
          {/* cells */}
          {grid.map((row, r) =>
            row.map((v, t) => {
              const x = L + t * cell;
              // Row 0 is the first rx angle; draw it at the top so higher rx
              // angles read downward (matching the label order top→bottom).
              const y = T + r * cell;
              const fill = v === null || !Number.isFinite(v) ? "#1b2531" : jetCss(norm(v));
              return (
                <rect key={`${r}_${t}`} x={x} y={y} width={cell} height={cell} fill={fill}>
                  <title>
                    TX {angles[t]?.toFixed(0)}° · RX {angles[r]?.toFixed(0)}°:{" "}
                    {v === null ? "n/a" : `${v.toFixed(1)} dB`}
                  </title>
                </rect>
              );
            }),
          )}
          {/* best-pair crosshair */}
          {bestTx !== null && bestRx !== null && (
            <g className="beam-crosshair">
              <rect
                x={L + bestTx * cell}
                y={T + bestRx * cell}
                width={cell}
                height={cell}
                fill="none"
                stroke="#ffffff"
                strokeWidth={2}
              />
              <line
                x1={L + bestTx * cell + cell / 2}
                y1={T}
                x2={L + bestTx * cell + cell / 2}
                y2={T + gh}
                stroke="#ffee58"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
              <line
                x1={L}
                y1={T + bestRx * cell + cell / 2}
                x2={L + gw}
                y2={T + bestRx * cell + cell / 2}
                stroke="#ffee58"
                strokeWidth={1}
                strokeDasharray="3 3"
              />
            </g>
          )}
          {/* axes */}
          <line className="scatter-axis" x1={L} y1={T + gh} x2={L + gw} y2={T + gh} />
          <line className="scatter-axis" x1={L} y1={T} x2={L} y2={T + gh} />
          {/* tx angle labels (x) */}
          {tickIdx.map((t) => (
            <text
              key={`tx_${t}`}
              className="scatter-label"
              x={L + t * cell + cell / 2}
              y={T + gh + 14}
              textAnchor="middle"
            >
              {angles[t]?.toFixed(0)}
            </text>
          ))}
          <text className="scatter-label" x={L + gw / 2} y={H - 2} textAnchor="middle">
            TX angle (°)
          </text>
          {/* rx angle labels (y) */}
          {tickIdx.map((r) => (
            <text
              key={`rx_${r}`}
              className="scatter-label"
              x={L - 4}
              y={T + r * cell + cell / 2 + 3}
              textAnchor="end"
            >
              {angles[r]?.toFixed(0)}
            </text>
          ))}
        </svg>
      </div>
      <div className="beam-heatmap-legend">
        <span className="mono">{extent.min.toFixed(1)}</span>
        <span
          className="beam-heatmap-bar"
          style={{
            background: `linear-gradient(to right, ${Array.from({ length: 9 }, (_, i) =>
              jetCss(i / 8),
            ).join(", ")})`,
          }}
        />
        <span className="mono">{extent.max.toFixed(1)} dB</span>
      </div>
    </div>
  );
}
