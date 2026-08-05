/**
 * GT-vs-DT playback dashboard (issue #3).
 *
 * Shown in Results mode whenever the project carries sensor_data/. One frame
 * cursor drives four cells: the merged beam chart (measured GT curve vs the
 * SEAM sweep), the camera frame, a top-down lidar view and the KPI table.
 *
 * Frame cursor semantics: store.playbackFrame is a position into the PACK's
 * frames array, while sensor segments index the MANIFEST's frames array. The
 * two differ whenever frame_stride > 1 or a build was capped, so the segment
 * scoping maps manifest positions -> pack positions through the frame `index`
 * field rather than assuming they line up. Playing across a segment boundary
 * is what makes a drive dataset look shuffled (verification-session lesson),
 * hence the transport scrubs inside one segment at a time.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { parsePcd } from "../utils/pcd";
import type { ParsedPcd } from "../utils/pcd";
import type {
  BeamProfile,
  PlaybackFrame,
  SensorFrame,
  SensorGtMetrics,
  SensorManifestResponse,
  Vec3,
} from "../types/api";

const GT_COLOR = "#0d9488";
const DT_COLOR = "#d97706";
const GRID_COLOR = "#232b36";
const AXIS_TEXT = "#8494a7";
const CELL_BG = "#0b0f14";

/** Fixed 2x canvas backing store: the beam/lidar cells are small and static
 *  between frames, so oversampling costs nothing and keeps 1px strokes and
 *  point splats crisp on both 1x and HiDPI displays. */
const DPR = 2;

/** Speeds offered by the transport (frames per second). */
const SPEEDS = [2, 4, 6, 10];

/** Zoom framing half-extent (m) around the RX for the lidar cell. */
const ZOOM_HALF_M = 60;

/** Point clouds kept in memory while scrubbing (one ~200k-point frame is a
 *  few MB, so the cache is small on purpose). */
const CLOUD_CACHE = 8;

function clamp01(v: number): number {
  return v < 0 ? 0 : v > 1 ? 1 : v;
}

function fmt(v: number | null | undefined, digits: number): string {
  return v === null || v === undefined || !Number.isFinite(v) ? "—" : v.toFixed(digits);
}

/** Container width tracked for canvas sizing (the Results panel is drag-resizable). */
function useBoxWidth(ref: React.RefObject<HTMLElement>): number {
  const [w, setW] = useState(300);
  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const ro = new ResizeObserver((entries) => {
      const next = Math.round(entries[0]?.contentRect.width ?? 0);
      if (next > 0) setW(next);
    });
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}

// ------------------------------------------------------------- beam chart

interface Curve {
  az: number[];
  p: (number | null)[];
}

interface HoverInfo {
  px: number;
  az: number;
  gt: number | null;
  dt: number | null;
}

/** Fold azimuth `a` by whole turns to land nearest `ref` — both beam curves
 *  are circular quantities, and the pack wraps to atan2's (-180, 180] while a
 *  GT profile may be authored in any equivalent window. Without this, a west
 *  bearing plots the two peaks 360° apart and the merged chart falls apart. */
function foldNear(a: number, ref: number): number {
  if (!Number.isFinite(a) || !Number.isFinite(ref)) return a;
  return a + 360 * Math.round((ref - a) / 360);
}

/** Index of the strongest sample of a curve (null when it is all-null). */
function argMax(c: Curve | null): number | null {
  if (!c || !Array.isArray(c.p) || !Array.isArray(c.az)) return null;
  let best: number | null = null;
  let bestV = -Infinity;
  for (let i = 0; i < c.p.length; i++) {
    const v = c.p[i];
    if (v !== null && Number.isFinite(v) && v > bestV) {
      bestV = v;
      best = i;
    }
  }
  return best;
}

/** Nearest sample of `c` to azimuth `az`, or null when out of reach. The
 *  reach is one-and-a-half sample spacings: the GT sweep usually covers a
 *  narrower FOV than the SEAM sweep, so the x axis extends past the end of the
 *  GT curve and a plain nearest-neighbour would report its last value all the
 *  way out there. */
function sampleAt(c: Curve | null, az: number): number | null {
  if (!c || !Array.isArray(c.az) || !Array.isArray(c.p) || c.az.length === 0)
    return null;
  let best = 0;
  let bestD = Infinity;
  for (let i = 0; i < c.az.length; i++) {
    const d = Math.abs(c.az[i] - az);
    if (d < bestD) {
      bestD = d;
      best = i;
    }
  }
  const span = Math.abs(c.az[c.az.length - 1] - c.az[0]);
  const reach = c.az.length > 1 ? (span / (c.az.length - 1)) * 1.5 : Infinity;
  if (bestD > reach) return null;
  const v = c.p[best];
  return v !== null && Number.isFinite(v) ? v : null;
}

/** One merged azimuth chart: measured GT against the SEAM sweep, both on the
 *  same WORLD azimuth axis (the pack reports world angles, not broadside
 *  offsets, so the two curves are directly comparable). */
function BeamChart({
  gt,
  dt,
  gtBest,
  dtBest,
}: {
  gt: Curve | null;
  dt: Curve | null;
  gtBest: number | null;
  dtBest: number | null;
}) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const width = useBoxWidth(wrapRef);
  const [hover, setHover] = useState<HoverInfo | null>(null);
  const H = 190;

  const range = useMemo(() => {
    let x0 = Infinity;
    let x1 = -Infinity;
    // Fixed dBm window unless the data falls outside it - a per-frame autoscale
    // would make the curve height meaningless while scrubbing.
    let y0 = -132;
    let y1 = -84;
    for (const c of [gt, dt]) {
      if (!c) continue;
      for (let i = 0; i < c.az.length; i++) {
        const a = c.az[i];
        if (Number.isFinite(a)) {
          x0 = Math.min(x0, a);
          x1 = Math.max(x1, a);
        }
        const v = c.p[i];
        if (v !== null && Number.isFinite(v)) {
          y0 = Math.min(y0, v);
          y1 = Math.max(y1, v);
        }
      }
    }
    if (!Number.isFinite(x0)) {
      x0 = -180;
      x1 = 180;
    }
    if (x1 - x0 < 1e-6) {
      x0 -= 1;
      x1 += 1;
    }
    return {
      x0,
      x1,
      y0: Math.floor(y0 / 6) * 6,
      y1: Math.ceil(y1 / 6) * 6,
    };
  }, [gt, dt]);

  const L = 40;
  const R = 10;
  const T = 12;
  const B = 24;
  const plotW = Math.max(10, width - L - R);
  const plotH = H - T - B;
  const xOf = (a: number) => L + ((a - range.x0) / (range.x1 - range.x0)) * plotW;
  const azOf = (px: number) => range.x0 + ((px - L) / plotW) * (range.x1 - range.x0);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    canvas.width = width * DPR;
    canvas.height = H * DPR;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    ctx.clearRect(0, 0, width, H);
    ctx.fillStyle = CELL_BG;
    ctx.fillRect(0, 0, width, H);

    const yOf = (v: number) => T + (1 - (v - range.y0) / (range.y1 - range.y0)) * plotH;

    // dBm gridlines every 6 dB.
    ctx.font = "10px Consolas, monospace";
    ctx.textBaseline = "middle";
    for (let v = range.y0; v <= range.y1 + 1e-6; v += 6) {
      const y = Math.round(yOf(v)) + 0.5;
      ctx.strokeStyle = GRID_COLOR;
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(L, y);
      ctx.lineTo(L + plotW, y);
      ctx.stroke();
      ctx.fillStyle = AXIS_TEXT;
      ctx.textAlign = "right";
      ctx.fillText(String(v), L - 5, y);
    }
    // Azimuth ticks (5 across the union range).
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    for (let i = 0; i < 5; i++) {
      const a = range.x0 + ((range.x1 - range.x0) * i) / 4;
      const x = Math.round(xOf(a)) + 0.5;
      ctx.strokeStyle = GRID_COLOR;
      ctx.beginPath();
      ctx.moveTo(x, T);
      ctx.lineTo(x, T + plotH);
      ctx.stroke();
      ctx.fillStyle = AXIS_TEXT;
      ctx.fillText(a.toFixed(0) + "°", x, T + plotH + 4);
    }

    const drawCurve = (c: Curve | null, color: string) => {
      if (!c) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1.6;
      ctx.beginPath();
      let open = false;
      for (let i = 0; i < c.az.length; i++) {
        const v = c.p[i];
        if (v === null || !Number.isFinite(v)) {
          // Null samples are gaps (no paths at that beam), not zeros.
          open = false;
          continue;
        }
        const x = xOf(c.az[i]);
        const y = yOf(Math.max(range.y0, Math.min(range.y1, v)));
        if (open) ctx.lineTo(x, y);
        else ctx.moveTo(x, y);
        open = true;
      }
      ctx.stroke();
    };
    drawCurve(gt, GT_COLOR);
    drawCurve(dt, DT_COLOR);

    // Best-beam markers, azimuth printed next to each.
    const marker = (deg: number | null, color: string, labelTop: boolean) => {
      if (deg === null || !Number.isFinite(deg)) return;
      const x = Math.round(xOf(deg)) + 0.5;
      if (x < L || x > L + plotW) return;
      ctx.strokeStyle = color;
      ctx.lineWidth = 1;
      ctx.setLineDash([4, 3]);
      ctx.beginPath();
      ctx.moveTo(x, T);
      ctx.lineTo(x, T + plotH);
      ctx.stroke();
      ctx.setLineDash([]);
      ctx.fillStyle = color;
      ctx.textAlign = x > L + plotW - 40 ? "right" : "left";
      ctx.textBaseline = "top";
      ctx.fillText(`${deg.toFixed(1)}°`, x + (x > L + plotW - 40 ? -3 : 3), labelTop ? T + 1 : T + 13);
    };
    marker(gtBest, GT_COLOR, true);
    marker(dtBest, DT_COLOR, false);

    if (hover) {
      const x = Math.round(hover.px) + 0.5;
      ctx.strokeStyle = "#4fc3f7";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(x, T);
      ctx.lineTo(x, T + plotH);
      ctx.stroke();
    }
  }, [gt, dt, gtBest, dtBest, hover, range, width, plotW, plotH]);

  const onMove = (e: React.MouseEvent<HTMLCanvasElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const px = e.clientX - rect.left;
    if (px < L || px > L + plotW) {
      setHover(null);
      return;
    }
    const az = azOf(px);
    setHover({ px, az, gt: sampleAt(gt, az), dt: sampleAt(dt, az) });
  };

  return (
    <div className="pb-cell">
      <div className="pb-cell-head">
        <span>Beam power vs world azimuth</span>
        <span className="pb-legend">
          <i style={{ background: GT_COLOR }} /> GT
          <i style={{ background: DT_COLOR }} /> SEAM
        </span>
      </div>
      <div className="pb-canvas-wrap" ref={wrapRef}>
        <canvas
          ref={canvasRef}
          style={{ width: "100%", height: H }}
          onMouseMove={onMove}
          onMouseLeave={() => setHover(null)}
        />
        {hover && (
          <div
            className="pb-tip mono"
            style={{
              left: Math.min(Math.max(hover.px + 10, 4), Math.max(4, width - 118)),
            }}
          >
            az {hover.az.toFixed(1)}°
            <br />
            <span style={{ color: GT_COLOR }}>GT {fmt(hover.gt, 1)}</span>
            <br />
            <span style={{ color: DT_COLOR }}>SEAM {fmt(hover.dt, 1)}</span>
          </div>
        )}
      </div>
    </div>
  );
}

// ------------------------------------------------------------ lidar cell

/** Teal height ramp. The 0.45 exponent WIDENS the low band on purpose: with a
 *  linear ramp a 0.5 m pedestrian sits within one shade of the road and
 *  disappears against buildings 20 m tall (verification-session lesson). */
function heightRgb(t: number, out: Uint8ClampedArray, at: number): void {
  const s = Math.pow(clamp01(t), 0.45);
  out[at] = 8 + s * 86;
  out[at + 1] = 58 + s * 176;
  out[at + 2] = 72 + s * 140;
  out[at + 3] = 255;
}

/** Semantic coloring only makes sense for a palette (a per-point rgb capture
 *  has thousands of shades and reads as noise at cell size). */
function looksSemantic(cloud: ParsedPcd | null): boolean {
  if (!cloud || !cloud.colors) return false;
  const n = Math.min(4096, cloud.colors.length / 3);
  const seen = new Set<number>();
  for (let i = 0; i < n; i++) {
    const c = cloud.colors;
    seen.add((c[i * 3] << 16) | (c[i * 3 + 1] << 8) | c[i * 3 + 2]);
    if (seen.size >= 32) return false;
  }
  return seen.size > 1;
}

function LidarCell({ url, rx }: { url: string | null; rx: Vec3 | null }) {
  const wrapRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const width = useBoxWidth(wrapRef);
  const [framing, setFraming] = useState<"full" | "zoom">("full");
  const [colorMode, setColorMode] = useState<"height" | "semantic">("height");
  const [cloud, setCloud] = useState<ParsedPcd | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cache = useRef(new Map<string, ParsedPcd>());
  const H = 180;

  useEffect(() => {
    if (!url) {
      setCloud(null);
      setError(null);
      return;
    }
    const hit = cache.current.get(url);
    if (hit) {
      setCloud(hit);
      setError(null);
      return;
    }
    let stale = false;
    void (async () => {
      try {
        const res = await fetch(url);
        if (!res.ok) throw new Error(`${res.status} ${res.statusText}`);
        const parsed = parsePcd(await res.arrayBuffer());
        cache.current.set(url, parsed);
        // Ring-drop the oldest entry (Map preserves insertion order).
        while (cache.current.size > CLOUD_CACHE) {
          const oldest = cache.current.keys().next().value;
          if (oldest === undefined) break;
          cache.current.delete(oldest);
        }
        if (!stale) {
          setCloud(parsed);
          setError(null);
        }
      } catch (err) {
        if (!stale) {
          setCloud(null);
          setError(err instanceof Error ? err.message : String(err));
        }
      }
    })();
    return () => {
      stale = true;
    };
  }, [url]);

  const semanticAvailable = useMemo(() => looksSemantic(cloud), [cloud]);
  const mode = colorMode === "semantic" && semanticAvailable ? "semantic" : "height";

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const cw = Math.max(1, Math.round(width * DPR));
    const ch = H * DPR;
    canvas.width = cw;
    canvas.height = ch;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = CELL_BG;
    ctx.fillRect(0, 0, cw, ch);
    if (!cloud || cloud.points.length === 0) return;

    const pts = cloud.points;
    const n = pts.length / 3;
    let xMin = Infinity, xMax = -Infinity, yMin = Infinity, yMax = -Infinity;
    let zMin = Infinity, zMax = -Infinity, sx = 0, sy = 0;
    for (let i = 0; i < n; i++) {
      const x = pts[i * 3];
      const y = pts[i * 3 + 1];
      const z = pts[i * 3 + 2];
      if (x < xMin) xMin = x;
      if (x > xMax) xMax = x;
      if (y < yMin) yMin = y;
      if (y > yMax) yMax = y;
      if (z < zMin) zMin = z;
      if (z > zMax) zMax = z;
      sx += x;
      sy += y;
    }
    let cx: number;
    let cy: number;
    let half: number;
    if (framing === "zoom") {
      cx = rx ? rx[0] : sx / n;
      cy = rx ? rx[1] : sy / n;
      half = ZOOM_HALF_M;
    } else {
      cx = (xMin + xMax) / 2;
      cy = (yMin + yMax) / 2;
      half = Math.max(1, Math.max(xMax - xMin, yMax - yMin) / 2) * 1.02;
    }
    const scale = Math.min(cw, ch) / (2 * half);
    const zSpan = zMax - zMin > 1e-6 ? zMax - zMin : 1;

    // Rasterize into an ImageData buffer: 200k per-point canvas fill calls
    // would stall the scrub, direct pixel writes do not.
    const img = ctx.createImageData(cw, ch);
    const data = img.data;
    for (let p = 0; p < cw * ch; p++) {
      data[p * 4] = 11;
      data[p * 4 + 1] = 15;
      data[p * 4 + 2] = 20;
      data[p * 4 + 3] = 255;
    }
    const rgb = new Uint8ClampedArray(4);
    for (let i = 0; i < n; i++) {
      const px = Math.round(cw / 2 + (pts[i * 3] - cx) * scale);
      // North (+y) is up on screen.
      const py = Math.round(ch / 2 - (pts[i * 3 + 1] - cy) * scale);
      if (px < 0 || py < 0 || px >= cw - 1 || py >= ch - 1) continue;
      if (mode === "semantic" && cloud.colors) {
        rgb[0] = cloud.colors[i * 3];
        rgb[1] = cloud.colors[i * 3 + 1];
        rgb[2] = cloud.colors[i * 3 + 2];
        rgb[3] = 255;
      } else {
        heightRgb((pts[i * 3 + 2] - zMin) / zSpan, rgb, 0);
      }
      // 2x2 splat = 1 CSS pixel at DPR 2.
      for (let dy = 0; dy < 2; dy++) {
        for (let dx = 0; dx < 2; dx++) {
          const o = ((py + dy) * cw + px + dx) * 4;
          data[o] = rgb[0];
          data[o + 1] = rgb[1];
          data[o + 2] = rgb[2];
          data[o + 3] = 255;
        }
      }
    }
    ctx.putImageData(img, 0, 0);

    if (rx) {
      const mx = cw / 2 + (rx[0] - cx) * scale;
      const my = ch / 2 - (rx[1] - cy) * scale;
      ctx.strokeStyle = DT_COLOR;
      ctx.lineWidth = 2;
      ctx.beginPath();
      ctx.arc(mx, my, 7, 0, Math.PI * 2);
      ctx.stroke();
      ctx.beginPath();
      ctx.moveTo(mx - 11, my);
      ctx.lineTo(mx + 11, my);
      ctx.moveTo(mx, my - 11);
      ctx.lineTo(mx, my + 11);
      ctx.stroke();
    }
    ctx.fillStyle = AXIS_TEXT;
    ctx.font = `${10 * DPR}px Consolas, monospace`;
    ctx.textAlign = "left";
    ctx.textBaseline = "bottom";
    ctx.fillText(`±${half.toFixed(0)} m · ${n} pts`, 6, ch - 5);
  }, [cloud, framing, mode, rx, width]);

  return (
    <div className="pb-cell">
      <div className="pb-cell-head">
        <span>Lidar (top-down)</span>
        <span className="pb-cell-toggles">
          <button
            className={framing === "full" ? "active" : ""}
            title="Fit the whole sweep"
            onClick={() => setFraming("full")}
          >
            Full
          </button>
          <button
            className={framing === "zoom" ? "active" : ""}
            title={`±${ZOOM_HALF_M} m around the RX`}
            onClick={() => setFraming("zoom")}
          >
            Zoom
          </button>
          <button
            className={mode === "height" ? "active" : ""}
            title="Shade by height (low band widened so short objects stay visible)"
            onClick={() => setColorMode("height")}
          >
            Height
          </button>
          {semanticAvailable && (
            <button
              className={mode === "semantic" ? "active" : ""}
              title="Use the cloud's own rgb palette"
              onClick={() => setColorMode("semantic")}
            >
              Semantic
            </button>
          )}
        </span>
      </div>
      <div className="pb-canvas-wrap" ref={wrapRef}>
        <canvas ref={canvasRef} style={{ width: "100%", height: H }} />
      </div>
      {error && <p className="hint pb-err">{error}</p>}
    </div>
  );
}

// -------------------------------------------------------------- KPI table

interface KpiRow {
  label: string;
  unit: string;
  digits: number;
  title: string;
  gt: number | null;
  dt: number | null;
  /** Circular quantity (azimuth): the delta wraps to (-180, 180]. */
  circular?: boolean;
}

function KpiTable({ gt, frame }: { gt: SensorGtMetrics | null; frame: PlaybackFrame }) {
  const rows: KpiRow[] = [
    {
      label: "RSS",
      unit: "dBm",
      digits: 1,
      title: "Link gain as received power, summed over the frame's paths.",
      gt: gt?.rss_dbm ?? null,
      dt: frame.rss_dbm,
    },
    {
      label: "RSS coherent",
      unit: "dBm",
      digits: 1,
      title: "Phase-coherent path sum (fading nulls included).",
      gt: gt?.rss_coherent_dbm ?? null,
      dt: frame.rss_coherent_dbm,
    },
    {
      label: "RMS delay",
      unit: "ns",
      digits: 2,
      title: "RMS delay spread of the power-delay profile.",
      gt: gt?.tau_rms_ns ?? null,
      dt: frame.tau_rms_ns,
    },
    {
      label: "Paths",
      unit: "",
      digits: 0,
      title: "Number of ray paths at this frame.",
      gt: gt?.n_paths ?? null,
      dt: frame.n_paths,
    },
    {
      label: "Best beam",
      unit: "°",
      digits: 1,
      title: "World azimuth of the strongest beam.",
      gt: gt?.best_beam_deg ?? null,
      dt: frame.best_beam_deg,
      // Azimuths are circular: -155 vs 205 is the same bearing, and a plain
      // subtraction would print +360 for a perfect match.
      circular: true,
    },
  ];

  return (
    <div className="pb-cell">
      <div className="pb-cell-head">
        <span>KPIs</span>
        {!gt && <span className="pb-legend">no GT for this frame</span>}
      </div>
      <table className="results-table pb-kpi">
        <thead>
          <tr>
            <th>KPI</th>
            <th>GT</th>
            <th>SEAM</th>
            <th>Δ</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((r) => {
            const both =
              r.gt !== null &&
              r.dt !== null &&
              Number.isFinite(r.gt) &&
              Number.isFinite(r.dt);
            // The doubled modulo guards JS's negative-operand %: a plain
            // ((dt-gt+180)%360)-180 returns -360 for dt=-155, gt=205.
            const raw = both ? (r.dt as number) - (r.gt as number) : null;
            const d =
              raw === null
                ? null
                : r.circular
                  ? ((((raw + 180) % 360) + 360) % 360) - 180
                  : raw;
            return (
              <tr key={r.label}>
                <td title={r.title}>
                  {r.label}
                  {r.unit ? ` (${r.unit})` : ""}
                </td>
                <td className="mono">{fmt(r.gt, r.digits)}</td>
                <td className="mono">{fmt(r.dt, r.digits)}</td>
                <td className="mono">
                  {d === null ? "—" : `${d > 0 ? "+" : ""}${d.toFixed(r.digits)}`}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

// ------------------------------------------------------------------ panel

function PlaybackBody({ sensors }: { sensors: SensorManifestResponse }) {
  const playback = useAppStore((s) => s.playback);
  const playbackFrame = useAppStore((s) => s.playbackFrame);
  const playing = useAppStore((s) => s.playbackPlaying);
  const speed = useAppStore((s) => s.playbackSpeed);
  const segment = useAppStore((s) => s.playbackSegment);
  const setPlaybackFrame = useAppStore((s) => s.setPlaybackFrame);
  const setPlaying = useAppStore((s) => s.setPlaybackPlaying);
  const setSpeed = useAppStore((s) => s.setPlaybackSpeed);
  const setSegment = useAppStore((s) => s.setPlaybackSegment);
  const runPlaybackBuild = useAppStore((s) => s.runPlaybackBuild);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);

  const manifest = sensors.manifest;
  const segments = sensors.segments;

  const channelKeys = useMemo(() => {
    const of = (kind: string) =>
      manifest.channels.find((c) => c.kind === kind)?.key ?? null;
    return { gt: of("beam_profile"), camera: of("image"), lidar: of("pointcloud") };
  }, [manifest]);

  const frameByIndex = useMemo(() => {
    const m = new Map<number, SensorFrame>();
    for (const f of manifest.frames) m.set(f.index, f);
    return m;
  }, [manifest]);

  // Pack positions belonging to the selected segment, in play order.
  const scoped = useMemo(() => {
    if (!playback) return [] as number[];
    const all = playback.frames.map((_, i) => i);
    const seg = segment >= 0 ? segments[segment] : undefined;
    if (!seg) return all;
    const posByIndex = new Map<number, number>();
    playback.frames.forEach((f, i) => {
      if (!posByIndex.has(f.index)) posByIndex.set(f.index, i);
    });
    const out: number[] = [];
    for (let p = seg.start; p <= seg.end && p < manifest.frames.length; p++) {
      const pos = posByIndex.get(manifest.frames[p].index);
      if (pos !== undefined) out.push(pos);
    }
    return out;
  }, [playback, segment, segments, manifest]);

  // A segment switch can leave the cursor outside the new scope.
  useEffect(() => {
    if (scoped.length > 0 && !scoped.includes(playbackFrame)) setPlaybackFrame(scoped[0]);
  }, [scoped, playbackFrame, setPlaybackFrame]);

  // Playback clock (mirrors the trajectory transport): the tick reads fresh
  // store state so the interval never closes over a stale frame.
  useEffect(() => {
    if (!playing || scoped.length === 0) return;
    const period = Math.max(30, 1000 / speed);
    const timer = setInterval(() => {
      const st = useAppStore.getState();
      const at = scoped.indexOf(st.playbackFrame);
      const next = at + 1;
      if (at < 0 || next >= scoped.length) {
        st.setPlaybackPlaying(false);
        return;
      }
      st.setPlaybackFrame(scoped[next]);
    }, period);
    return () => clearInterval(timer);
  }, [playing, speed, scoped]);

  const cursor = Math.max(0, scoped.indexOf(playbackFrame));
  const frame = playback?.frames[playbackFrame] ?? null;
  const sensorFrame = frame ? (frameByIndex.get(frame.index) ?? null) : null;

  // GT beam curves are per-frame files; keep the fetched ones so scrubbing
  // back and forth does not refetch, and drop late responses on frame change.
  // The state carries the frame index it belongs to: on a cache miss the
  // fetch resolves AFTER the frame advanced, and drawing frame A's GT against
  // frame B's SEAM curve is a silent lie the key match makes impossible.
  const profileCache = useRef(new Map<number, BeamProfile | null>());
  const [gtProfile, setGtProfile] = useState<{
    key: number;
    data: BeamProfile | null;
  } | null>(null);
  useEffect(() => {
    profileCache.current.clear();
  }, [projectId]);
  useEffect(() => {
    const key = frame?.index;
    const path = channelKeys.gt && sensorFrame ? sensorFrame.files[channelKeys.gt] : undefined;
    if (!projectId || key === undefined || !path) {
      setGtProfile(null);
      return;
    }
    const cached = profileCache.current.get(key);
    if (cached !== undefined) {
      setGtProfile({ key, data: cached });
      return;
    }
    let stale = false;
    void (async () => {
      let data: BeamProfile | null = null;
      try {
        const res = await fetch(api.assetUrl(projectId, path));
        if (res.ok) {
          // Validate the shape before it is cached or drawn: a malformed
          // profile file must degrade to "no GT curve", not crash the tree.
          const json = (await res.json()) as Partial<BeamProfile> | null;
          if (json && Array.isArray(json.azimuth_deg) && Array.isArray(json.power_dbm)) {
            data = { azimuth_deg: json.azimuth_deg, power_dbm: json.power_dbm };
          }
        }
      } catch {
        // missing/malformed GT curve: the chart falls back to SEAM only
      }
      profileCache.current.set(key, data);
      if (!stale) setGtProfile({ key, data });
    })();
    return () => {
      stale = true;
    };
  }, [projectId, channelKeys.gt, sensorFrame, frame]);

  const dtCurve: Curve | null = frame
    ? { az: frame.beam_azimuth_deg, p: frame.beam_power_dbm }
    : null;
  // Circular fold: land every GT azimuth in the DT curve's window (see
  // foldNear). Per-sample folding also re-glues a GT profile that itself
  // straddles the +/-180 seam into one monotone run.
  const foldRef =
    dtCurve && dtCurve.az.length
      ? (dtCurve.az[0] + dtCurve.az[dtCurve.az.length - 1]) / 2
      : 0;
  const gtRaw = gtProfile && gtProfile.key === frame?.index ? gtProfile.data : null;
  const gtCurve: Curve | null = gtRaw
    ? { az: gtRaw.azimuth_deg.map((a) => foldNear(a, foldRef)), p: gtRaw.power_dbm }
    : null;
  const gtBestIdx = argMax(gtCurve);
  const gtBestRaw =
    sensorFrame?.gt?.best_beam_deg ??
    (gtBestIdx !== null && gtCurve ? gtCurve.az[gtBestIdx] : null);
  const gtBest = gtBestRaw !== null ? foldNear(gtBestRaw, foldRef) : null;
  const dtBestIdx = argMax(dtCurve);
  const dtBest =
    frame?.best_beam_deg ?? (dtBestIdx !== null && dtCurve ? dtCurve.az[dtBestIdx] : null);

  const cameraUrl =
    projectId && channelKeys.camera && sensorFrame?.files[channelKeys.camera]
      ? api.assetUrl(projectId, sensorFrame.files[channelKeys.camera])
      : null;
  const lidarUrl =
    projectId && channelKeys.lidar && sensorFrame?.files[channelKeys.lidar]
      ? api.assetUrl(projectId, sensorFrame.files[channelKeys.lidar])
      : null;

  const disabled = busy !== null || !projectId;
  const build = () =>
    void runPlaybackBuild({
      tx_rows: 1,
      tx_cols: 16,
      rx_rows: 1,
      rx_cols: 1,
      use_device_orientation: true,
      sweep_step_deg: 2.5,
      frame_stride: 1,
    });

  const warnings = playback?.warnings ?? [];

  return (
    <div className="pb-panel">
      <div className="pb-head">
        <span className="pb-title">GT vs DT playback</span>
        <span className="pb-sub">
          {manifest.frames.length} frame(s) · {segments.length} segment(s) ·{" "}
          {manifest.channels.map((c) => c.key).join(", ") || "no channels"}
          {manifest.entity_id && (
            <>
              {" · "}
              <span className="mono">{manifest.entity_id}</span>
            </>
          )}
        </span>
      </div>

      {sensors.warnings.length > 0 && (
        <p className="hint pb-err">{sensors.warnings.slice(0, 3).join(" · ")}</p>
      )}

      <div className="pb-actions">
        <button className="primary" disabled={disabled} onClick={build}>
          Build playback pack
        </button>
        {playback && (
          <span className="pb-sub">
            <span className="mono">{playback.result_id}</span> · {playback.frames.length}{" "}
            frame(s) · <span className="mono">{playback.backend}</span>
          </span>
        )}
      </div>

      {warnings.length > 0 && (
        <p className="hint pb-err">
          {warnings.slice(0, 3).join(" · ")}
          {warnings.length > 3 && ` (+${warnings.length - 3} more)`}
        </p>
      )}

      {!playback ? (
        <p className="hint">
          No playback pack yet. Building solves paths + a beam sweep at every
          recorded pose, then this dashboard compares them against the measured
          frames.
        </p>
      ) : (
        <>
          <div className="pb-transport">
            <select
              value={segment}
              title="Scrub inside one drive segment (playing across a boundary teleports the RX)"
              onChange={(e) => setSegment(Number(e.target.value))}
            >
              <option value={-1}>All frames</option>
              {segments.map((s, i) => (
                <option key={`${s.label}_${i}`} value={i}>
                  {s.label}
                </option>
              ))}
            </select>
            <button
              disabled={scoped.length === 0}
              title={playing ? "Pause" : "Play"}
              onClick={() => {
                if (!playing && cursor >= scoped.length - 1 && scoped.length > 0)
                  setPlaybackFrame(scoped[0]);
                setPlaying(!playing);
              }}
            >
              {playing ? "⏸" : "▶"}
            </button>
            <select
              value={speed}
              title="Playback rate"
              onChange={(e) => setSpeed(Number(e.target.value))}
            >
              {SPEEDS.map((s) => (
                <option key={s} value={s}>
                  {s} fps
                </option>
              ))}
            </select>
            <input
              type="range"
              min={0}
              max={Math.max(0, scoped.length - 1)}
              step={1}
              value={cursor}
              disabled={scoped.length === 0}
              onChange={(e) => {
                const at = Number(e.target.value);
                if (scoped[at] !== undefined) setPlaybackFrame(scoped[at]);
              }}
            />
            <span className="mono pb-frame-num">
              {frame ? `index ${frame.index}` : "—"}
              {frame?.time_s != null && ` · t=${frame.time_s.toFixed(2)}s`}
              {` · ${cursor + 1}/${scoped.length}`}
            </span>
          </div>

          {scoped.length === 0 ? (
            <p className="hint">
              This segment has no built frames — the pack was built from a
              subset of the recording (stride/cap); rebuild the pack to cover
              it.
            </p>
          ) : (
            <div className="pb-grid">
              <BeamChart gt={gtCurve} dt={dtCurve} gtBest={gtBest} dtBest={dtBest} />
              {cameraUrl && (
                <div className="pb-cell">
                  <div className="pb-cell-head">
                    <span>Camera</span>
                  </div>
                  <img className="pb-img" src={cameraUrl} alt={`frame ${frame?.index ?? ""}`} />
                </div>
              )}
              {lidarUrl && (
                <LidarCell url={lidarUrl} rx={frame ? frame.rx_position : null} />
              )}
              {frame && <KpiTable gt={sensorFrame?.gt ?? null} frame={frame} />}
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** Hidden entirely for projects without sensor_data/ (the common case). */
export default function PlaybackPanel() {
  const sensors = useAppStore((s) => s.sensors);
  if (!sensors) return null;
  return <PlaybackBody sensors={sensors} />;
}
