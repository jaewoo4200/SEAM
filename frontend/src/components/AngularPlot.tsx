/**
 * Angular (AoD / AoD) polar scatter — paper-style SVG consistent with
 * charts.tsx (Times New Roman on white, PNG/SVG/CSV export via ChartFrame).
 *
 * Each filtered path contributes up to two markers:
 *   - AoD (angle of departure)  → filled marker
 *   - AoA (angle of arrival)    → hollow marker
 * The polar angle is the azimuth (deg); the radius maps the path power
 * LINEARLY from the visible min..max power (inner ring = weakest, outer ring =
 * strongest), with both bounds annotated on the radial axis. Elevation is not
 * shown positionally but is carried in the CSV export and each marker's title.
 */

import { useMemo, useRef } from "react";
import { ChartFrame, CHART_FONT, CHART_COLORS, exportCsv } from "../charts";
import type { RayPath } from "../types/api";

const AOD_COLOR = CHART_COLORS[0]; // blue
const AOA_COLOR = CHART_COLORS[1]; // red

interface Marker {
  path_id: string;
  az: number;
  el: number;
  power: number;
}

/** Split the filtered paths into AoD / AoA marker lists (skipping nulls). */
function collectMarkers(paths: RayPath[]): { aod: Marker[]; aoa: Marker[] } {
  const aod: Marker[] = [];
  const aoa: Marker[] = [];
  for (const p of paths) {
    if (p.aod_deg) {
      aod.push({ path_id: p.path_id, az: p.aod_deg[0], el: p.aod_deg[1], power: p.power_dbm });
    }
    if (p.aoa_deg) {
      aoa.push({ path_id: p.path_id, az: p.aoa_deg[0], el: p.aoa_deg[1], power: p.power_dbm });
    }
  }
  return { aod, aoa };
}

export default function AngularPlot({ paths }: { paths: RayPath[] }) {
  const ref = useRef<SVGSVGElement>(null);
  const { aod, aoa } = useMemo(() => collectMarkers(paths), [paths]);

  const size = 300;
  const cx = size / 2;
  const cy = size / 2;
  const rMax = size / 2 - 34; // leave room for the outer az labels

  // Power range across BOTH series drives the shared radial scale.
  const { pMin, pMax } = useMemo(() => {
    let pMin = Infinity;
    let pMax = -Infinity;
    for (const m of [...aod, ...aoa]) {
      if (m.power < pMin) pMin = m.power;
      if (m.power > pMax) pMax = m.power;
    }
    if (!Number.isFinite(pMin)) {
      pMin = -120;
      pMax = 0;
    }
    if (pMax - pMin < 1e-9) pMax = pMin + 1;
    return { pMin, pMax };
  }, [aod, aoa]);

  // Azimuth 0° points to +X (east, right); angle grows counter-clockwise like a
  // math polar plot. Screen Y is down, so negate the sine.
  const toXY = (azDeg: number, power: number) => {
    const t = (power - pMin) / (pMax - pMin);
    const r = t * rMax;
    const a = (azDeg * Math.PI) / 180;
    return { x: cx + r * Math.cos(a), y: cy - r * Math.sin(a) };
  };

  const onCsv = () =>
    exportCsv(
      "angular_aod_aoa",
      ["path_id", "series", "azimuth_deg", "elevation_deg", "power_dbm"],
      [
        ...aod.map((m) => [m.path_id, "AoD", m.az, m.el, m.power] as (string | number)[]),
        ...aoa.map((m) => [m.path_id, "AoA", m.az, m.el, m.power] as (string | number)[]),
      ],
    );

  const rings = [0.25, 0.5, 0.75, 1];
  const spokes = [0, 45, 90, 135, 180, 225, 270, 315];

  const marker = (m: Marker, filled: boolean, color: string) => {
    const { x, y } = toXY(m.az, m.power);
    return (
      <circle
        key={`${filled ? "aod" : "aoa"}_${m.path_id}`}
        cx={x}
        cy={y}
        r={3.2}
        fill={filled ? color : "#ffffff"}
        stroke={color}
        strokeWidth={filled ? 0 : 1.3}
      >
        <title>
          {m.path_id} · {filled ? "AoD" : "AoA"} · az {m.az.toFixed(1)}° · el {m.el.toFixed(1)}° ·{" "}
          {m.power.toFixed(1)} dBm
        </title>
      </circle>
    );
  };

  const hasData = aod.length > 0 || aoa.length > 0;

  return (
    <ChartFrame title="AoA / AoD (azimuth)" name="angular_aod_aoa" svgRef={ref} onCsv={onCsv}>
      <svg ref={ref} viewBox={`0 0 ${size} ${size}`} className="chart-svg">
        <g fontFamily={CHART_FONT} fontSize={10} fill="#000">
          {/* radial rings */}
          {rings.map((f) => (
            <circle key={`ring${f}`} cx={cx} cy={cy} r={f * rMax} fill="none" stroke="#dddddd" strokeWidth={0.6} />
          ))}
          {/* azimuth spokes + outer labels */}
          {spokes.map((deg) => {
            const a = (deg * Math.PI) / 180;
            const x2 = cx + rMax * Math.cos(a);
            const y2 = cy - rMax * Math.sin(a);
            const lx = cx + (rMax + 12) * Math.cos(a);
            const ly = cy - (rMax + 12) * Math.sin(a);
            return (
              <g key={`spoke${deg}`}>
                <line x1={cx} y1={cy} x2={x2} y2={y2} stroke="#eeeeee" strokeWidth={0.6} />
                <text x={lx} y={ly + 3} textAnchor="middle">
                  {deg}°
                </text>
              </g>
            );
          })}
          {/* radial power annotation: inner = pMin, outer = pMax */}
          <text x={cx + 3} y={cy - 2}>
            {pMin.toFixed(0)} dBm
          </text>
          <text x={cx + 3} y={cy - rMax + 10}>
            {pMax.toFixed(0)} dBm
          </text>

          {hasData ? (
            <>
              {aoa.map((m) => marker(m, false, AOA_COLOR))}
              {aod.map((m) => marker(m, true, AOD_COLOR))}
            </>
          ) : (
            <text x={cx} y={cy} textAnchor="middle" fill="#666">
              no angular data
            </text>
          )}

          {/* legend */}
          <g transform={`translate(8, ${size - 20})`}>
            <circle cx={4} cy={-3} r={3.2} fill={AOD_COLOR} />
            <text x={12} y={0}>AoD ({aod.length})</text>
            <circle cx={74} cy={-3} r={3.2} fill="#ffffff" stroke={AOA_COLOR} strokeWidth={1.3} />
            <text x={82} y={0}>AoA ({aoa.length})</text>
          </g>
        </g>
      </svg>
    </ChartFrame>
  );
}
