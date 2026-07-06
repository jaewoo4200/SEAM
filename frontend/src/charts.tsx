/** Paper-ready SVG chart kit for the communication-metrics dashboard.
 *
 * Design goals (owner requirements):
 * - Figures render exactly as they would sit in a paper: white background,
 *   black serif ("Times New Roman") axes/labels — copy-paste ready.
 * - Every chart exports PNG (3x supersampled), SVG (standalone, styles
 *   inlined), and CSV (the underlying series data).
 * - No chart library: plain SVG, same approach as the existing viewer plots.
 */

import { useRef } from "react";
import type { ReactNode } from "react";

export const CHART_FONT = '"Times New Roman", "Nimbus Roman", Times, serif';
export const CHART_COLORS = [
  "#1f4e9c", // blue
  "#c23b22", // red
  "#2e7d32", // green
  "#7b1fa2", // purple
  "#e6890f", // orange
  "#00838f", // teal
];

export interface Series {
  label: string;
  x: number[];
  y: (number | null)[];
  color?: string;
}

// ------------------------------------------------------------------ export

function downloadBlob(blob: Blob, filename: string): void {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 5000);
}

function svgMarkup(svg: SVGSVGElement): string {
  const clone = svg.cloneNode(true) as SVGSVGElement;
  clone.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  // Standalone white background so the figure is identical outside the app.
  const bg = document.createElementNS("http://www.w3.org/2000/svg", "rect");
  bg.setAttribute("width", "100%");
  bg.setAttribute("height", "100%");
  bg.setAttribute("fill", "#ffffff");
  clone.insertBefore(bg, clone.firstChild);
  return new XMLSerializer().serializeToString(clone);
}

export function exportSvg(svg: SVGSVGElement | null, name: string): void {
  if (!svg) return;
  downloadBlob(new Blob([svgMarkup(svg)], { type: "image/svg+xml" }), `${name}.svg`);
}

export function exportPng(svg: SVGSVGElement | null, name: string, scale = 3): void {
  if (!svg) return;
  const w = svg.viewBox.baseVal?.width || svg.clientWidth;
  const h = svg.viewBox.baseVal?.height || svg.clientHeight;
  const img = new Image();
  const url = URL.createObjectURL(
    new Blob([svgMarkup(svg)], { type: "image/svg+xml" }),
  );
  img.onload = () => {
    const canvas = document.createElement("canvas");
    canvas.width = Math.round(w * scale);
    canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext("2d");
    if (ctx) {
      ctx.fillStyle = "#ffffff";
      ctx.fillRect(0, 0, canvas.width, canvas.height);
      ctx.drawImage(img, 0, 0, canvas.width, canvas.height);
      canvas.toBlob((blob) => {
        if (blob) downloadBlob(blob, `${name}.png`);
      }, "image/png");
    }
    URL.revokeObjectURL(url);
  };
  img.src = url;
}

export function exportCsv(
  name: string,
  header: string[],
  rows: (number | string | null)[][],
): void {
  const esc = (v: number | string | null) =>
    v === null ? "" : typeof v === "string" && /[",\n]/.test(v) ? `"${v.replace(/"/g, '""')}"` : String(v);
  const text = [header.join(","), ...rows.map((r) => r.map(esc).join(","))].join("\n");
  downloadBlob(new Blob([text], { type: "text/csv" }), `${name}.csv`);
}

/** CSV rows for one-or-more series sharing (or not sharing) an x axis. */
export function seriesCsv(name: string, series: Series[]): void {
  if (series.length === 0) return;
  const shared = series.every(
    (s) => s.x.length === series[0].x.length && s.x.every((v, i) => v === series[0].x[i]),
  );
  if (shared) {
    exportCsv(
      name,
      ["x", ...series.map((s) => s.label)],
      series[0].x.map((x, i) => [x, ...series.map((s) => s.y[i] ?? null)]),
    );
  } else {
    exportCsv(
      name,
      ["series", "x", "y"],
      series.flatMap((s) => s.x.map((x, i) => [s.label, x, s.y[i] ?? null])),
    );
  }
}

// ------------------------------------------------------------------- frame

/** Titled figure card with PNG / SVG / CSV export buttons. Children render
 *  the <svg>; the ref is shared so exports serialize exactly what is shown. */
export function ChartFrame({
  title,
  name,
  onCsv,
  svgRef,
  children,
}: {
  title: string;
  /** File basename for exports (snake_case). */
  name: string;
  onCsv?: () => void;
  svgRef: React.RefObject<SVGSVGElement>;
  children: ReactNode;
}) {
  return (
    <div className="chart-frame">
      <div className="chart-frame-head">
        <span className="chart-frame-title">{title}</span>
        <span className="chart-frame-actions">
          <button title="Download as PNG (3x)" onClick={() => exportPng(svgRef.current, name)}>
            PNG
          </button>
          <button title="Download as SVG (vector)" onClick={() => exportSvg(svgRef.current, name)}>
            SVG
          </button>
          {onCsv && (
            <button title="Download the data as CSV" onClick={onCsv}>
              CSV
            </button>
          )}
        </span>
      </div>
      {children}
    </div>
  );
}

// -------------------------------------------------------------------- axes

export interface ChartGeom {
  W: number;
  H: number;
  L: number; // left margin (y labels)
  R: number; // right margin
  T: number; // top margin
  B: number; // bottom margin (x labels)
  xMin: number;
  xMax: number;
  yMin: number;
  yMax: number;
}

export function xScale(g: ChartGeom): (v: number) => number {
  const span = g.xMax - g.xMin || 1;
  return (v) => g.L + ((v - g.xMin) / span) * (g.W - g.L - g.R);
}

export function yScale(g: ChartGeom): (v: number) => number {
  const span = g.yMax - g.yMin || 1;
  return (v) => g.T + (1 - (v - g.yMin) / span) * (g.H - g.T - g.B);
}

/** ~n "nice" tick values across [min, max]. */
export function ticks(min: number, max: number, n = 5): number[] {
  const span = max - min || 1;
  const step0 = span / Math.max(1, n);
  const mag = 10 ** Math.floor(Math.log10(step0));
  const step = [1, 2, 5, 10].map((m) => m * mag).find((s) => span / s <= n) ?? mag * 10;
  const first = Math.ceil(min / step) * step;
  const out: number[] = [];
  for (let v = first; v <= max + 1e-9; v += step) out.push(Math.round(v * 1e9) / 1e9);
  return out;
}

export function fmtTick(v: number): string {
  const a = Math.abs(v);
  if (a >= 1e6) return `${v / 1e6}M`;
  if (a >= 1e4) return `${v / 1e3}k`;
  if (a > 0 && a < 0.01) return v.toExponential(0);
  return String(Math.round(v * 100) / 100);
}

/** Axes + grid + labels in paper style (black on white, serif). */
export function Axes({
  g,
  xLabel,
  yLabel,
  xTicks,
  yTicks,
}: {
  g: ChartGeom;
  xLabel: string;
  yLabel: string;
  xTicks?: number[];
  yTicks?: number[];
}) {
  const sx = xScale(g);
  const sy = yScale(g);
  const xs = xTicks ?? ticks(g.xMin, g.xMax);
  const ys = yTicks ?? ticks(g.yMin, g.yMax);
  return (
    <g fontFamily={CHART_FONT} fontSize={11} fill="#000">
      {/* grid */}
      {xs.map((v) => (
        <line key={`gx${v}`} x1={sx(v)} y1={g.T} x2={sx(v)} y2={g.H - g.B} stroke="#dddddd" strokeWidth={0.5} />
      ))}
      {ys.map((v) => (
        <line key={`gy${v}`} x1={g.L} y1={sy(v)} x2={g.W - g.R} y2={sy(v)} stroke="#dddddd" strokeWidth={0.5} />
      ))}
      {/* frame */}
      <rect x={g.L} y={g.T} width={g.W - g.L - g.R} height={g.H - g.T - g.B} fill="none" stroke="#000" strokeWidth={1} />
      {/* tick labels */}
      {xs.map((v) => (
        <text key={`tx${v}`} x={sx(v)} y={g.H - g.B + 14} textAnchor="middle">
          {fmtTick(v)}
        </text>
      ))}
      {ys.map((v) => (
        <text key={`ty${v}`} x={g.L - 5} y={sy(v) + 3.5} textAnchor="end">
          {fmtTick(v)}
        </text>
      ))}
      {/* axis labels */}
      <text x={(g.L + g.W - g.R) / 2} y={g.H - 4} textAnchor="middle" fontSize={12}>
        {xLabel}
      </text>
      <text
        x={12}
        y={(g.T + g.H - g.B) / 2}
        textAnchor="middle"
        fontSize={12}
        transform={`rotate(-90 12 ${(g.T + g.H - g.B) / 2})`}
      >
        {yLabel}
      </text>
    </g>
  );
}

function geomFor(
  series: Series[],
  W: number,
  H: number,
  pad = 0.05,
): ChartGeom {
  let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
  for (const s of series) {
    for (let i = 0; i < s.x.length; i++) {
      const y = s.y[i];
      if (y === null || !Number.isFinite(y)) continue;
      xMin = Math.min(xMin, s.x[i]);
      xMax = Math.max(xMax, s.x[i]);
      yMin = Math.min(yMin, y);
      yMax = Math.max(yMax, y);
    }
  }
  if (!Number.isFinite(xMin)) {
    xMin = 0; xMax = 1; yMin = 0; yMax = 1;
  }
  const ySpan = (yMax - yMin) || 1;
  return {
    W, H, L: 52, R: 12, T: 10, B: 34,
    xMin, xMax, yMin: yMin - ySpan * pad, yMax: yMax + ySpan * pad,
  };
}

// ------------------------------------------------------------------- kinds

/** Multi-series line chart (CFR, fading envelope, trajectory time series). */
export function LineChart({
  title,
  name,
  xLabel,
  yLabel,
  series,
  width = 460,
  height = 240,
  legend = true,
}: {
  title: string;
  name: string;
  xLabel: string;
  yLabel: string;
  series: Series[];
  width?: number;
  height?: number;
  legend?: boolean;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const g = geomFor(series, width, height);
  const sx = xScale(g);
  const sy = yScale(g);
  const pathFor = (s: Series) => {
    let d = "";
    let pen = false;
    for (let i = 0; i < s.x.length; i++) {
      const y = s.y[i];
      if (y === null || !Number.isFinite(y)) {
        pen = false;
        continue;
      }
      d += `${pen ? "L" : "M"}${sx(s.x[i]).toFixed(1)},${sy(y).toFixed(1)}`;
      pen = true;
    }
    return d;
  };
  return (
    <ChartFrame title={title} name={name} svgRef={ref} onCsv={() => seriesCsv(name, series)}>
      <svg ref={ref} viewBox={`0 0 ${width} ${height}`} className="chart-svg">
        <Axes g={g} xLabel={xLabel} yLabel={yLabel} />
        {series.map((s, i) => (
          <path
            key={s.label}
            d={pathFor(s)}
            fill="none"
            stroke={s.color ?? CHART_COLORS[i % CHART_COLORS.length]}
            strokeWidth={1.6}
          />
        ))}
        {legend && series.length > 1 && (
          <g fontFamily={CHART_FONT} fontSize={11}>
            {series.map((s, i) => (
              <g key={s.label} transform={`translate(${g.L + 10}, ${g.T + 14 + i * 15})`}>
                <line x1={0} y1={-4} x2={18} y2={-4} stroke={s.color ?? CHART_COLORS[i % CHART_COLORS.length]} strokeWidth={2} />
                <text x={23} y={0} fill="#000">{s.label}</text>
              </g>
            ))}
          </g>
        )}
      </svg>
    </ChartFrame>
  );
}

/** Stem (lollipop) chart — power-delay profile. */
export function StemChart({
  title,
  name,
  xLabel,
  yLabel,
  points,
  width = 460,
  height = 240,
}: {
  title: string;
  name: string;
  xLabel: string;
  yLabel: string;
  points: { x: number; y: number; color?: string; label?: string }[];
  width?: number;
  height?: number;
}) {
  const ref = useRef<SVGSVGElement>(null);
  const series: Series[] = [{ label: yLabel, x: points.map((p) => p.x), y: points.map((p) => p.y) }];
  const g = geomFor(series, width, height, 0.08);
  const sx = xScale(g);
  const sy = yScale(g);
  const y0 = g.H - g.B;
  return (
    <ChartFrame
      title={title}
      name={name}
      svgRef={ref}
      onCsv={() =>
        exportCsv(name, [xLabel, yLabel, "type"], points.map((p) => [p.x, p.y, p.label ?? ""]))
      }
    >
      <svg ref={ref} viewBox={`0 0 ${width} ${height}`} className="chart-svg">
        <Axes g={g} xLabel={xLabel} yLabel={yLabel} />
        {points.map((p, i) => (
          <g key={i}>
            <line x1={sx(p.x)} y1={y0} x2={sx(p.x)} y2={sy(p.y)} stroke={p.color ?? CHART_COLORS[0]} strokeWidth={1.4} />
            <circle cx={sx(p.x)} cy={sy(p.y)} r={2.8} fill={p.color ?? CHART_COLORS[0]}>
              {p.label && <title>{p.label}</title>}
            </circle>
          </g>
        ))}
      </svg>
    </ChartFrame>
  );
}

/** Horizontal bar chart — e.g. path-loss model comparison. */
export function BarChart({
  title,
  name,
  valueLabel,
  items,
  width = 460,
  refLine,
}: {
  title: string;
  name: string;
  valueLabel: string;
  items: { label: string; value: number | null; color?: string }[];
  width?: number;
  /** Reference value drawn as a dashed vertical line (e.g. ray-traced PL). */
  refLine?: { value: number; label: string };
}) {
  const ref = useRef<SVGSVGElement>(null);
  const rowH = 22;
  const L = 150, R = 16, T = 8, B = 34;
  const height = T + B + items.length * rowH;
  const vals = items.map((i) => i.value).filter((v): v is number => v !== null && Number.isFinite(v));
  if (refLine) vals.push(refLine.value);
  let vMin = Math.min(...(vals.length ? vals : [0]));
  let vMax = Math.max(...(vals.length ? vals : [1]));
  if (vMax - vMin < 1e-9) vMax = vMin + 1;
  const span = vMax - vMin;
  vMin -= span * 0.05;
  vMax += span * 0.05;
  const sx = (v: number) => L + ((v - vMin) / (vMax - vMin)) * (width - L - R);
  return (
    <ChartFrame
      title={title}
      name={name}
      svgRef={ref}
      onCsv={() => exportCsv(name, ["model", valueLabel], items.map((i) => [i.label, i.value]))}
    >
      <svg ref={ref} viewBox={`0 0 ${width} ${height}`} className="chart-svg">
        <g fontFamily={CHART_FONT} fontSize={11} fill="#000">
          {items.map((it, i) => {
            const y = T + i * rowH;
            return (
              <g key={it.label}>
                <text x={L - 6} y={y + rowH / 2 + 3.5} textAnchor="end">{it.label}</text>
                {it.value !== null && Number.isFinite(it.value) && (
                  <>
                    <rect
                      x={sx(Math.min(it.value, vMin < 0 ? 0 : vMin))}
                      y={y + 4}
                      width={Math.max(1, Math.abs(sx(it.value) - sx(vMin < 0 ? 0 : vMin)))}
                      height={rowH - 8}
                      fill={it.color ?? CHART_COLORS[i % CHART_COLORS.length]}
                      fillOpacity={0.85}
                    />
                    <text x={sx(it.value) + 4} y={y + rowH / 2 + 3.5} fontSize={10}>
                      {fmtTick(it.value)}
                    </text>
                  </>
                )}
              </g>
            );
          })}
          {refLine && (
            <>
              <line
                x1={sx(refLine.value)}
                y1={T}
                x2={sx(refLine.value)}
                y2={height - B}
                stroke="#000"
                strokeWidth={1}
                strokeDasharray="4 3"
              />
              <text x={sx(refLine.value)} y={height - B + 14} textAnchor="middle" fontSize={10}>
                {refLine.label}
              </text>
            </>
          )}
          <text x={(L + width - R) / 2} y={height - 4} textAnchor="middle" fontSize={12}>
            {valueLabel}
          </text>
        </g>
      </svg>
    </ChartFrame>
  );
}
