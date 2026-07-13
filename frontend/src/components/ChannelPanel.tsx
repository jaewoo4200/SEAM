/**
 * Channel analysis panel (Results mode, below SolverControls). This is the
 * InSite / operator-style RT-vs-38.901 view: a link budget for one TX->RX
 * link, the ray CIR as a stem plot, the CFR magnitude, and a path-loss model
 * comparison table where the ray-traced (RT) reference row is highlighted and
 * every 3GPP TR 38.901 / CI model shows its delta versus RT.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { LineChart } from "../charts";
import { StaleChip } from "./ResultExplorer";
import { PATH_COLORS } from "./common";
import type { Series } from "../charts";
import type {
  CirTap,
  PathType,
  ChannelSweepResult,
  SpectrogramResult,
  MeasurementSample,
  TrajectoryValidationReport,
} from "../types/api";

/** SCS options for the OFDM grid (kHz). 15 = LTE, 30 = 5G FR1 default. */
const SCS_OPTIONS = [15, 30, 60, 120] as const;

/** Live, staged channel parameters editable in the panel (human-facing units). */
interface LiveParams {
  freqGhz: number;
  bandwidthMhz: number;
  txPowerDbm: number;
  noiseFigureDb: number;
  scsKhz: number;
}

/** Collapsible section shell (matches SolverControls' look). */
function Section({
  title,
  open,
  onToggle,
  children,
}: {
  title: string;
  open: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className="solver-section">
      <div className="solver-section-head">
        <button className="solver-caret" onClick={onToggle}>
          <span className="caret">{open ? "▾" : "▸"}</span>
          {title}
        </button>
      </div>
      {open && <div className="solver-section-body">{children}</div>}
    </div>
  );
}

const KNOWN_PATH_TYPES = new Set<string>(Object.keys(PATH_COLORS));

/** Color a CIR tap by its path_type, falling back to the "mixed" gray. */
function tapColor(pathType: string): string {
  return KNOWN_PATH_TYPES.has(pathType)
    ? PATH_COLORS[pathType as PathType]
    : PATH_COLORS.mixed;
}

/** CIR stem plot: delay (x) vs power (y), one stem per tap colored by type. */
function CirStemPlot({ cir }: { cir: CirTap[] }) {
  const W = 300;
  const H = 150;
  const L = 36;
  const B = 22;
  const T = 8;

  const { dMin, dMax, pMin, pMax } = useMemo(() => {
    let dMin = Infinity, dMax = -Infinity, pMin = Infinity, pMax = -Infinity;
    for (const t of cir) {
      dMin = Math.min(dMin, t.delay_ns);
      dMax = Math.max(dMax, t.delay_ns);
      pMin = Math.min(pMin, t.power_dbm);
      pMax = Math.max(pMax, t.power_dbm);
    }
    if (!Number.isFinite(dMin)) {
      dMin = 0; dMax = 1; pMin = -120; pMax = 0;
    }
    if (dMax - dMin < 1e-9) dMax = dMin + 1;
    if (pMax - pMin < 1e-9) pMin = pMax - 1;
    return { dMin, dMax, pMin, pMax };
  }, [cir]);

  const x = (delay: number) => L + ((delay - dMin) / (dMax - dMin)) * (W - L - 8);
  const y = (power: number) => T + (1 - (power - pMin) / (pMax - pMin)) * (H - B - T);
  const yBase = y(pMin);

  if (cir.length === 0) return <p className="hint">No CIR taps.</p>;

  return (
    <div className="scatter-wrap">
      <h4>CIR (power delay profile)</h4>
      <svg width={W} height={H}>
        <line className="scatter-axis" x1={L} y1={H - B} x2={W - 4} y2={H - B} />
        <line className="scatter-axis" x1={L} y1={T} x2={L} y2={H - B} />
        <text className="scatter-label" x={L} y={H - 8}>
          {dMin.toFixed(1)} ns
        </text>
        <text className="scatter-label" x={W - 8} y={H - 8} textAnchor="end">
          {dMax.toFixed(1)} ns
        </text>
        <text className="scatter-label" x={L - 4} y={T + 6} textAnchor="end">
          {pMax.toFixed(0)}
        </text>
        <text className="scatter-label" x={L - 4} y={H - B} textAnchor="end">
          {pMin.toFixed(0)}
        </text>
        <text className="scatter-label" x={L - 4} y={(H - B + T) / 2} textAnchor="end">
          dBm
        </text>
        {cir.map((t, i) => {
          const cx = x(t.delay_ns);
          const cy = y(t.power_dbm);
          const color = tapColor(t.path_type);
          return (
            <g key={i}>
              <line x1={cx} y1={yBase} x2={cx} y2={cy} stroke={color} strokeWidth={1.5} />
              <circle cx={cx} cy={cy} r={2.6} fill={color}>
                <title>
                  {t.path_type}: {t.delay_ns.toFixed(1)} ns, {t.power_dbm.toFixed(1)} dBm
                </title>
              </circle>
            </g>
          );
        })}
      </svg>
    </div>
  );
}

/** CFR magnitude line plot: frequency offset (x) vs |H| dB (y). */
function CfrPlot({ freq, mag }: { freq: number[]; mag: number[] }) {
  const W = 300;
  const H = 130;
  const L = 36;
  const B = 22;
  const T = 8;

  const n = Math.min(freq.length, mag.length);
  const { fMin, fMax, mMin, mMax } = useMemo(() => {
    let fMin = Infinity, fMax = -Infinity, mMin = Infinity, mMax = -Infinity;
    for (let i = 0; i < n; i++) {
      fMin = Math.min(fMin, freq[i]);
      fMax = Math.max(fMax, freq[i]);
      mMin = Math.min(mMin, mag[i]);
      mMax = Math.max(mMax, mag[i]);
    }
    if (!Number.isFinite(fMin)) {
      fMin = 0; fMax = 1; mMin = -1; mMax = 1;
    }
    if (fMax - fMin < 1e-9) fMax = fMin + 1;
    if (mMax - mMin < 1e-9) { mMin -= 1; mMax += 1; }
    return { fMin, fMax, mMin, mMax };
  }, [freq, mag, n]);

  const x = (f: number) => L + ((f - fMin) / (fMax - fMin)) * (W - L - 8);
  const y = (m: number) => T + (1 - (m - mMin) / (mMax - mMin)) * (H - B - T);

  const points = useMemo(() => {
    const pts: string[] = [];
    for (let i = 0; i < n; i++) pts.push(`${x(freq[i]).toFixed(1)},${y(mag[i]).toFixed(1)}`);
    return pts.join(" ");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [freq, mag, n, fMin, fMax, mMin, mMax]);

  if (n === 0) return <p className="hint">No CFR data.</p>;

  const fMhz = (hz: number) => (hz / 1e6).toFixed(1);

  return (
    <div className="scatter-wrap">
      <h4>CFR magnitude</h4>
      <svg width={W} height={H}>
        <line className="scatter-axis" x1={L} y1={H - B} x2={W - 4} y2={H - B} />
        <line className="scatter-axis" x1={L} y1={T} x2={L} y2={H - B} />
        <text className="scatter-label" x={L} y={H - 8}>
          {fMhz(fMin)} MHz
        </text>
        <text className="scatter-label" x={W - 8} y={H - 8} textAnchor="end">
          {fMhz(fMax)} MHz
        </text>
        <text className="scatter-label" x={L - 4} y={T + 6} textAnchor="end">
          {mMax.toFixed(0)}
        </text>
        <text className="scatter-label" x={L - 4} y={H - B} textAnchor="end">
          {mMin.toFixed(0)}
        </text>
        <text className="scatter-label" x={L - 4} y={(H - B + T) / 2} textAnchor="end">
          dB
        </text>
        <polyline points={points} fill="none" stroke="var(--accent)" strokeWidth={1.5} />
      </svg>
    </div>
  );
}

// ----------------------------------------------------- sweep + ISAC helpers
// Independent, direct-api subsections rendered below the main link-budget
// view: a config-field sweep, a Doppler-time spectrogram (first ISAC output),
// and measured-vs-predicted flight-log validation. Each reuses the panel's
// TX/RX selection, the paper-style chart kit, and the shared notice/error bus.

const SWEEP_FIELDS = [
  { key: "frequency_hz", label: "Frequency (Hz)" },
  { key: "tx_power_dbm", label: "TX power (dBm)" },
  { key: "bandwidth_hz", label: "Bandwidth (Hz)" },
  { key: "noise_figure_db", label: "Noise figure (dB)" },
] as const;

type SweepField = (typeof SWEEP_FIELDS)[number]["key"];

type SweepMetric =
  | "rss_dbm"
  | "snr_db"
  | "sinr_db"
  | "path_loss_db"
  | "rms_delay_spread_ns"
  | "k_factor_db";

const SWEEP_METRICS: { key: SweepMetric; label: string }[] = [
  { key: "rss_dbm", label: "RSS (dBm)" },
  { key: "snr_db", label: "SNR (dB)" },
  { key: "sinr_db", label: "SINR (dB)" },
  { key: "path_loss_db", label: "Path loss (dB)" },
  { key: "rms_delay_spread_ns", label: "RMS delay spread (ns)" },
  { key: "k_factor_db", label: "K-factor (dB)" },
];

/** Parse a comma / whitespace / newline separated list of numbers, tolerating
 *  scientific notation ("3.5e9") and stray blanks. Non-numeric tokens drop. */
function parseSweepValues(text: string): number[] {
  return text
    .split(/[\s,]+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0)
    .map((s) => Number(s))
    .filter((v) => Number.isFinite(v));
}

/** Jet colormap: t in 0..1 -> [r,g,b] (0..255). Standard 4-segment approx. */
function jet(t: number): [number, number, number] {
  const x = Math.min(1, Math.max(0, t));
  const c = (v: number) => Math.round(255 * Math.min(1, Math.max(0, v)));
  return [c(1.5 - Math.abs(4 * x - 3)), c(1.5 - Math.abs(4 * x - 2)), c(1.5 - Math.abs(4 * x - 1))];
}

/** Parse pasted flight-log measurements: a JSON array of MeasurementSample, or
 *  CSV with header `time_s,x,y,z,measured_path_gain_db` (time_s optional; the
 *  header itself optional — headerless rows are read positionally). */
function parseMeasurements(text: string): MeasurementSample[] {
  const trimmed = text.trim();
  if (!trimmed) return [];
  if (trimmed.startsWith("[") || trimmed.startsWith("{")) {
    const parsed: unknown = JSON.parse(trimmed);
    const arr = Array.isArray(parsed) ? parsed : [parsed];
    const out: MeasurementSample[] = [];
    for (const item of arr) {
      const o = item as Record<string, unknown>;
      const pos = Array.isArray(o.rx_position)
        ? (o.rx_position as unknown[]).map((v) => Number(v))
        : [Number(o.x), Number(o.y), Number(o.z)];
      const gain = Number(o.measured_path_gain_db);
      if (!Number.isFinite(gain) || pos.length < 3 || pos.some((v) => !Number.isFinite(v))) continue;
      out.push({
        rx_position: [pos[0], pos[1], pos[2]],
        measured_path_gain_db: gain,
        time_s: o.time_s == null ? null : Number(o.time_s),
        tx_id: typeof o.tx_id === "string" ? o.tx_id : undefined,
      });
    }
    return out;
  }
  return parseMeasurementCsv(trimmed);
}

function parseMeasurementCsv(text: string): MeasurementSample[] {
  const lines = text
    .split(/\r?\n/)
    .map((l) => l.trim())
    .filter((l) => l.length > 0);
  if (lines.length === 0) return [];
  const firstCols = lines[0].split(",").map((s) => s.trim());
  const hasHeader = firstCols.some((tok) => Number.isNaN(Number(tok)));
  let idx = { time: -1, x: 0, y: 1, z: 2, gain: 3 };
  let dataLines = lines;
  if (hasHeader) {
    const cols = firstCols.map((s) => s.toLowerCase());
    const find = (names: string[]) => cols.findIndex((c) => names.includes(c));
    idx = {
      time: find(["time_s", "time", "t"]),
      x: find(["x", "rx_x"]),
      y: find(["y", "rx_y"]),
      z: find(["z", "rx_z"]),
      gain: find(["measured_path_gain_db", "path_gain_db", "gain_db", "measured_db"]),
    };
    if (idx.x < 0 || idx.y < 0 || idx.z < 0 || idx.gain < 0) {
      throw new Error("CSV header must include x, y, z and measured_path_gain_db.");
    }
    dataLines = lines.slice(1);
  } else if (firstCols.length >= 5) {
    idx = { time: 0, x: 1, y: 2, z: 3, gain: 4 };
  }
  const out: MeasurementSample[] = [];
  for (const line of dataLines) {
    const c = line.split(",").map((s) => Number(s.trim()));
    const gain = c[idx.gain];
    const x = c[idx.x];
    const y = c[idx.y];
    const z = c[idx.z];
    if (![gain, x, y, z].every((v) => Number.isFinite(v))) continue;
    out.push({
      rx_position: [x, y, z],
      measured_path_gain_db: gain,
      time_s: idx.time >= 0 && Number.isFinite(c[idx.time]) ? c[idx.time] : null,
    });
  }
  return out;
}

/** 1. Channel sweep: link metrics vs one swept config field, charted. */
function ChannelSweepSection({
  txId,
  rxId,
  disabled,
}: {
  txId: string;
  rxId: string;
  disabled: boolean;
}) {
  const projectId = useAppStore((s) => s.projectId);
  const notify = useAppStore((s) => s.notify);
  const notifyError = useAppStore((s) => s.notifyError);

  const [open, setOpen] = useState(false);
  const [field, setField] = useState<SweepField>("frequency_hz");
  const [valuesText, setValuesText] = useState("3.5e9, 28e9");
  const [metric, setMetric] = useState<SweepMetric>("rss_dbm");
  const [result, setResult] = useState<ChannelSweepResult | null>(null);
  const [loading, setLoading] = useState(false);
  const [inlineError, setInlineError] = useState<string | null>(null);

  const fieldLabel = SWEEP_FIELDS.find((f) => f.key === field)?.label ?? field;
  const metricLabel = SWEEP_METRICS.find((m) => m.key === metric)?.label ?? metric;

  const series: Series[] = useMemo(() => {
    if (!result) return [];
    return [
      {
        label: metricLabel,
        x: result.rows.map((row) => row.value),
        y: result.rows.map((row) => row[metric]),
      },
    ];
  }, [result, metric, metricLabel]);

  async function run() {
    if (!projectId) return;
    const vals = parseSweepValues(valuesText);
    if (vals.length < 2) {
      setInlineError("Enter at least two numeric sweep values (comma-separated).");
      return;
    }
    setInlineError(null);
    setLoading(true);
    try {
      const res = await api.analyzeChannelSweep(projectId, {
        sweep_field: field,
        sweep_values: vals,
        tx_id: txId || undefined,
        rx_id: rxId || undefined,
      });
      setResult(res);
      notify(`Swept ${res.rows.length} point(s) over ${fieldLabel}.`);
    } catch (err) {
      notifyError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  const busy = disabled || loading;

  return (
    <Section title="Channel sweep" open={open} onToggle={() => setOpen((o) => !o)}>
      <label className="solver-field">
        <span className="solver-field-label">Sweep field</span>
        <select value={field} disabled={busy} onChange={(e) => setField(e.target.value as SweepField)}>
          {SWEEP_FIELDS.map((f) => (
            <option key={f.key} value={f.key}>
              {f.label}
            </option>
          ))}
        </select>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Values</span>
        <span className="solver-field-input">
          <input
            type="text"
            value={valuesText}
            disabled={busy}
            placeholder="e.g. 3.5e9, 28e9 or 10, 20, 30"
            onChange={(e) => setValuesText(e.target.value)}
          />
        </span>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Y metric</span>
        <select value={metric} disabled={busy} onChange={(e) => setMetric(e.target.value as SweepMetric)}>
          {SWEEP_METRICS.map((m) => (
            <option key={m.key} value={m.key}>
              {m.label}
            </option>
          ))}
        </select>
      </label>
      {inlineError && (
        <p className="hint" style={{ color: "#ef5350" }}>
          {inlineError}
        </p>
      )}
      <div className="panel-actions">
        <button className="primary" disabled={busy || !projectId || !txId || !rxId} onClick={() => void run()}>
          {loading ? "Running…" : "Run sweep"}
        </button>
      </div>
      {result && (
        <>
          {result.warnings.length > 0 && (
            <div className="ai-note">
              {result.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}
          <LineChart
            title={`${metricLabel} vs ${fieldLabel}`}
            name={`channel_sweep_${field}_${metric}`}
            xLabel={fieldLabel}
            yLabel={metricLabel}
            series={series}
            legend={false}
          />
        </>
      )}
    </Section>
  );
}

/** 2. Doppler-time spectrogram: STFT of h(t) drawn as a jet heatmap. */
function SpectrogramSection({
  txId,
  rxId,
  disabled,
}: {
  txId: string;
  rxId: string;
  disabled: boolean;
}) {
  const projectId = useAppStore((s) => s.projectId);
  const notify = useAppStore((s) => s.notify);
  const notifyError = useAppStore((s) => s.notifyError);

  const [open, setOpen] = useState(false);
  const [durationS, setDurationS] = useState(1);
  const [fs, setFs] = useState(500);
  const [windowLen, setWindowLen] = useState(128);
  const [result, setResult] = useState<SpectrogramResult | null>(null);
  const [loading, setLoading] = useState(false);
  const canvasRef = useRef<HTMLCanvasElement>(null);

  async function run() {
    if (!projectId) return;
    setLoading(true);
    try {
      const res = await api.analyzeSpectrogram(projectId, {
        tx_id: txId || undefined,
        rx_id: rxId || undefined,
        duration_s: durationS,
        sampling_frequency_hz: fs,
        window: windowLen,
      });
      setResult(res);
      notify(`Spectrogram: ${res.times_s.length} frame(s), ${res.num_paths} path(s).`);
    } catch (err) {
      notifyError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  // Repaint the magnitude_db matrix as a jet heatmap whenever the result changes.
  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas || !result) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const mag = result.magnitude_db;
    const nF = mag.length;
    const nB = nF > 0 ? mag[0].length : 0;
    if (nF === 0 || nB === 0) return;
    let lo = Infinity;
    let hi = -Infinity;
    for (const frame of mag) {
      for (const v of frame) {
        if (!Number.isFinite(v)) continue;
        if (v < lo) lo = v;
        if (v > hi) hi = v;
      }
    }
    if (!Number.isFinite(lo)) {
      lo = 0;
      hi = 1;
    }
    const span = hi - lo || 1;
    const img = new ImageData(nF, nB);
    for (let f = 0; f < nF; f++) {
      const frame = mag[f];
      for (let b = 0; b < nB; b++) {
        const v = frame[b];
        const t = Number.isFinite(v) ? (v - lo) / span : 0;
        const [rr, gg, bb] = jet(t);
        const row = nB - 1 - b; // highest Doppler at the top
        const p = (row * nF + f) * 4;
        img.data[p] = rr;
        img.data[p + 1] = gg;
        img.data[p + 2] = bb;
        img.data[p + 3] = 255;
      }
    }
    const off = document.createElement("canvas");
    off.width = nF;
    off.height = nB;
    off.getContext("2d")?.putImageData(img, 0, 0);
    ctx.imageSmoothingEnabled = false;
    ctx.drawImage(off, 0, 0, canvas.width, canvas.height);
  }, [result]);

  const busy = disabled || loading;
  const empty = result != null && result.magnitude_db.length === 0;
  const tMin = result && result.times_s.length ? result.times_s[0] : 0;
  const tMax = result && result.times_s.length ? result.times_s[result.times_s.length - 1] : 0;
  const dMin = result && result.doppler_hz.length ? Math.min(...result.doppler_hz) : 0;
  const dMax = result && result.doppler_hz.length ? Math.max(...result.doppler_hz) : 0;

  return (
    <Section title="Doppler-time spectrogram" open={open} onToggle={() => setOpen((o) => !o)}>
      <label className="solver-field">
        <span className="solver-field-label">Duration (s)</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={0.05}
            max={10}
            step={0.05}
            value={durationS}
            disabled={busy}
            onChange={(e) => setDurationS(Number(e.target.value))}
          />
        </span>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Sampling freq (Hz)</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={1}
            max={100000}
            step={1}
            value={fs}
            disabled={busy}
            onChange={(e) => setFs(Number(e.target.value))}
          />
        </span>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Window</span>
        <span className="solver-field-input">
          <input
            type="number"
            min={8}
            max={2048}
            step={1}
            value={windowLen}
            disabled={busy}
            onChange={(e) => setWindowLen(Math.round(Number(e.target.value)))}
          />
        </span>
      </label>
      <div className="panel-actions">
        <button className="primary" disabled={busy || !projectId || !txId || !rxId} onClick={() => void run()}>
          {loading ? "Computing…" : "Compute spectrogram"}
        </button>
      </div>
      {result && (
        <>
          <div className="results-meta">
            backend <span className="mono">{result.backend}</span> · {result.num_paths} path(s) ·{" "}
            {result.times_s.length}×{result.doppler_hz.length} (time×Doppler)
          </div>
          {result.warnings.length > 0 && (
            <div className="ai-note">
              {result.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}
          {empty ? (
            <p className="hint">No spectrogram data (the channel has no time variation).</p>
          ) : (
            <div className="scatter-wrap">
              <h4>Doppler-time magnitude (jet, dB)</h4>
              <div style={{ display: "flex", gap: 6 }}>
                <div
                  style={{
                    display: "flex",
                    flexDirection: "column",
                    justifyContent: "space-between",
                    fontSize: 10,
                    color: "var(--muted)",
                    textAlign: "right",
                  }}
                >
                  <span>{dMax.toFixed(0)} Hz</span>
                  <span>0</span>
                  <span>{dMin.toFixed(0)} Hz</span>
                </div>
                <div>
                  <canvas
                    ref={canvasRef}
                    width={340}
                    height={200}
                    style={{
                      width: 340,
                      height: 200,
                      border: "1px solid var(--border, #333)",
                      imageRendering: "pixelated",
                      display: "block",
                    }}
                  />
                  <div
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      fontSize: 10,
                      color: "var(--muted)",
                    }}
                  >
                    <span>{tMin.toFixed(2)} s</span>
                    <span>time →</span>
                    <span>{tMax.toFixed(2)} s</span>
                  </div>
                </div>
              </div>
              <p className="hint" style={{ marginTop: 4 }}>
                Color = STFT magnitude in dB (jet, normalized over min–max). Y = Doppler, 0 Hz centered.
              </p>
            </div>
          )}
        </>
      )}
    </Section>
  );
}

/** 3. Flight-log validation: measured vs predicted path gain along the route. */
function FlightLogValidationSection({ txId, disabled }: { txId: string; disabled: boolean }) {
  const projectId = useAppStore((s) => s.projectId);
  const notify = useAppStore((s) => s.notify);
  const notifyError = useAppStore((s) => s.notifyError);

  const [open, setOpen] = useState(false);
  const [text, setText] = useState("");
  const [report, setReport] = useState<TrajectoryValidationReport | null>(null);
  const [loading, setLoading] = useState(false);
  const [inlineError, setInlineError] = useState<string | null>(null);

  const hasTime = report != null && report.points.length > 0 && report.points.every((p) => p.time_s != null);

  const series: Series[] = useMemo(() => {
    if (!report || report.points.length === 0) return [];
    const useTime = report.points.every((p) => p.time_s != null);
    const xs = report.points.map((p) => (useTime ? (p.time_s as number) : p.index));
    return [
      { label: "Measured", x: xs, y: report.points.map((p) => p.measured_db) },
      { label: "Predicted (aligned)", x: xs, y: report.points.map((p) => p.aligned_predicted_db) },
    ];
  }, [report]);

  async function run() {
    if (!projectId) return;
    let measurements: MeasurementSample[] | undefined;
    const trimmed = text.trim();
    if (trimmed) {
      try {
        measurements = parseMeasurements(trimmed);
      } catch (e) {
        setInlineError(`Could not parse measurements: ${e instanceof Error ? e.message : String(e)}`);
        return;
      }
      if (measurements.length === 0) {
        setInlineError("No valid rows found. Expected: time_s,x,y,z,measured_path_gain_db.");
        return;
      }
    }
    setInlineError(null);
    setLoading(true);
    try {
      const rep = await api.validateTrajectory(projectId, {
        tx_id: txId || undefined,
        measurements: measurements ?? null,
      });
      setReport(rep);
      notify(`Validated ${rep.stats.n} point(s); RMSE ${rep.stats.rmse_db.toFixed(2)} dB.`);
    } catch (err) {
      // 400 = no measurements supplied and none stored; surface backend detail.
      notifyError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }

  const busy = disabled || loading;

  return (
    <Section title="Flight-log validation" open={open} onToggle={() => setOpen((o) => !o)}>
      <p className="hint">
        Paste measurements as CSV (header <span className="mono">time_s,x,y,z,measured_path_gain_db</span>, or
        without <span className="mono">time_s</span>) or a JSON array. Leave blank to use the project's stored
        measurements.
      </p>
      <textarea
        className="mono"
        rows={5}
        value={text}
        disabled={busy}
        placeholder={"time_s,x,y,z,measured_path_gain_db\n0.0,10,5,1.5,-82.3\n0.5,12,5,1.5,-84.1"}
        style={{ width: "100%", resize: "vertical", fontSize: 11, boxSizing: "border-box" }}
        onChange={(e) => setText(e.target.value)}
      />
      {inlineError && (
        <p className="hint" style={{ color: "#ef5350" }}>
          {inlineError}
        </p>
      )}
      <div className="panel-actions">
        <button className="primary" disabled={busy || !projectId} onClick={() => void run()}>
          {loading ? "Validating…" : "Validate"}
        </button>
      </div>
      {report && (
        <>
          <div className="results-meta">
            <span className="mono">{report.tx_id}</span> · backend{" "}
            <span className="mono">{report.backend}</span> · {report.stats.n} point(s)
          </div>
          {report.warnings.length > 0 && (
            <div className="ai-note">
              {report.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}
          <div className="traj-kpis">
            <div className="traj-kpi">
              <span className="traj-kpi-label">Level offset</span>
              <span className="traj-kpi-value mono">{report.stats.level_offset_db.toFixed(2)} dB</span>
            </div>
            <div className="traj-kpi">
              <span className="traj-kpi-label">RMSE</span>
              <span className="traj-kpi-value mono">{report.stats.rmse_db.toFixed(2)} dB</span>
            </div>
            <div className="traj-kpi">
              <span className="traj-kpi-label">Mean abs err</span>
              <span className="traj-kpi-value mono">{report.stats.mean_abs_error_db.toFixed(2)} dB</span>
            </div>
            <div className="traj-kpi">
              <span className="traj-kpi-label">N</span>
              <span className="traj-kpi-value mono">{report.stats.n}</span>
            </div>
          </div>
          {series.length > 0 && (
            <LineChart
              title="Measured vs predicted path gain"
              name="flight_log_validation"
              xLabel={hasTime ? "time (s)" : "index"}
              yLabel="path gain (dB)"
              series={series}
            />
          )}
        </>
      )}
    </Section>
  );
}

export default function ChannelPanel() {
  const scene = useAppStore((s) => s.scene);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);
  const channelResult = useAppStore((s) => s.channelResult);
  const analyzeChannel = useAppStore((s) => s.analyzeChannel);
  const clearChannel = useAppStore((s) => s.clearChannel);
  const autoChannel = useAppStore((s) => s.autoChannel);
  const setAuto = useAppStore((s) => s.setAuto);
  const pathsConfig = useAppStore((s) => s.pathsConfig);
  const setPathsConfig = useAppStore((s) => s.setPathsConfig);
  const updateDevice = useAppStore((s) => s.updateDevice);

  const [open, setOpen] = useState(false);
  const txs = useMemo(() => scene?.devices.filter((d) => d.kind === "tx") ?? [], [scene]);
  const rxs = useMemo(() => scene?.devices.filter((d) => d.kind === "rx") ?? [], [scene]);

  const [txId, setTxId] = useState<string>("");
  const [rxId, setRxId] = useState<string>("");
  // CFR resolution (number of frequency samples across the band). Defaults to
  // the backend default (128) until the user overrides it.
  const [numCfrPoints, setNumCfrPoints] = useState(128);

  // Default the selects to the first available tx/rx when the scene changes.
  useEffect(() => {
    if (!txId && txs.length > 0) setTxId(txs[0].id);
    if (!rxId && rxs.length > 0) setRxId(rxs[0].id);
    // If the selected id no longer exists (device deleted), reset it.
    if (txId && !txs.some((d) => d.id === txId)) setTxId(txs[0]?.id ?? "");
    if (rxId && !rxs.some((d) => d.id === rxId)) setRxId(rxs[0]?.id ?? "");
  }, [txs, rxs, txId, rxId]);

  // --- Live parameters (staged, edited in the panel) -------------------
  const selectedTx = useMemo(() => txs.find((d) => d.id === txId) ?? null, [txs, txId]);

  /** Read the current config + selected TX into human-facing units. */
  const seedFromConfig = useMemo<() => LiveParams>(
    () => () => ({
      freqGhz: pathsConfig.frequency_hz / 1e9,
      bandwidthMhz: pathsConfig.bandwidth_hz / 1e6,
      txPowerDbm: selectedTx?.power_dbm ?? 30,
      noiseFigureDb: pathsConfig.noise_figure_db,
      scsKhz: 30,
    }),
    [pathsConfig.frequency_hz, pathsConfig.bandwidth_hz, pathsConfig.noise_figure_db, selectedTx],
  );

  const [live, setLive] = useState<LiveParams>(seedFromConfig);

  // Re-seed the staged values whenever the selected TX changes (so the power
  // field tracks the device you're analyzing). The SCS is preserved across a
  // TX switch by keeping the previous value.
  useEffect(() => {
    setLive((prev) => ({ ...seedFromConfig(), scsKhz: prev.scsKhz }));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [txId]);

  // Derived N_RB shown next to the SCS select: floor(BW / (12 * SCS)).
  const numResourceBlocks = useMemo(() => {
    const bwHz = live.bandwidthMhz * 1e6;
    const scsHz = live.scsKhz * 1e3;
    if (!(bwHz > 0) || !(scsHz > 0)) return 0;
    return Math.floor(bwHz / (12 * scsHz));
  }, [live.bandwidthMhz, live.scsKhz]);

  // Only auto-re-run when a result already exists for the *currently selected*
  // pair (which also covers "the user pressed Analyze once for this pair").
  const hasResultForPair =
    channelResult != null && channelResult.tx_id === txId && channelResult.rx_id === rxId;

  // Debounced apply: patch pathsConfig, persist TX power if it changed, and
  // re-run analysis when a result already exists for the pair.
  const applyTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Latest gating inputs read inside the debounced callback without re-arming
  // the timer on every keystroke of unrelated state.
  const applyCtx = useRef({ txId, rxId, numCfrPoints, hasResultForPair, prevPowerDbm: 0 });
  applyCtx.current = {
    txId,
    rxId,
    numCfrPoints,
    hasResultForPair,
    prevPowerDbm: selectedTx?.power_dbm ?? 30,
  };

  /** Stage a change and schedule the 500 ms debounced apply. The side effect is
   *  kept out of the state updater (StrictMode double-invokes updaters). */
  function patchLive(patch: Partial<LiveParams>): void {
    const next = { ...live, ...patch };
    setLive(next);
    if (applyTimer.current) clearTimeout(applyTimer.current);
    const armedPid = useAppStore.getState().projectId;
    applyTimer.current = setTimeout(() => {
      applyTimer.current = null;
      // Bail if the project changed while the debounce was pending: the
      // staged values belong to the old scene (audit B4).
      if (useAppStore.getState().projectId !== armedPid) return;
      const ctx = applyCtx.current;
      // (a) push frequency/bandwidth/noise-figure into the solver config.
      setPathsConfig({
        frequency_hz: Math.round(next.freqGhz * 1e9),
        bandwidth_hz: Math.round(next.bandwidthMhz * 1e6),
        noise_figure_db: next.noiseFigureDb,
      });
      // (b) persist TX power only when it actually changed (writes the scene).
      if (ctx.txId && Math.abs(next.txPowerDbm - ctx.prevPowerDbm) > 1e-9) {
        void updateDevice(ctx.txId, { power_dbm: next.txPowerDbm });
      }
      // (c) re-run the analysis for the selected pair, but only if one already
      // exists for it (mirrors lastChannelArgs / Analyze-first semantics).
      if (ctx.hasResultForPair && ctx.txId && ctx.rxId) {
        void analyzeChannel(ctx.txId, ctx.rxId, ctx.numCfrPoints, next.scsKhz);
      }
    }, 500);
  }

  // Cancel any pending debounced apply on unmount.
  useEffect(
    () => () => {
      if (applyTimer.current) clearTimeout(applyTimer.current);
    },
    [],
  );

  /** Restore the staged values from the current config + device. */
  function resetLive(): void {
    if (applyTimer.current) {
      clearTimeout(applyTimer.current);
      applyTimer.current = null;
    }
    setLive(seedFromConfig());
  }

  const disabled = busy !== null;
  const r = channelResult;

  const fmt = (v: number | null, unit: string, digits = 1) =>
    v === null ? "—" : `${v.toFixed(digits)} ${unit}`;

  const budget = (label: string, value: string) => (
    <div className="traj-kpi">
      <span className="traj-kpi-label">{label}</span>
      <span className="traj-kpi-value mono">{value}</span>
    </div>
  );

  return (
    <div className="panel channel-panel">
      <Section title="Channel analysis" open={open} onToggle={() => setOpen((o) => !o)}>
        <label className="solver-field">
          <span className="solver-field-label">TX</span>
          <select value={txId} disabled={disabled || txs.length === 0} onChange={(e) => setTxId(e.target.value)}>
            {txs.length === 0 && <option value="">no transmitters</option>}
            {txs.map((d) => (
              <option key={d.id} value={d.id}>
                {d.id}
              </option>
            ))}
          </select>
        </label>
        <label className="solver-field">
          <span className="solver-field-label">RX</span>
          <select value={rxId} disabled={disabled || rxs.length === 0} onChange={(e) => setRxId(e.target.value)}>
            {rxs.length === 0 && <option value="">no receivers</option>}
            {rxs.map((d) => (
              <option key={d.id} value={d.id}>
                {d.id}
              </option>
            ))}
          </select>
        </label>
        <label className="solver-field">
          <span className="solver-field-label">CFR points</span>
          <span className="solver-field-input">
            <input
              type="number"
              min={2}
              max={4096}
              step={1}
              value={numCfrPoints}
              disabled={disabled}
              onChange={(e) =>
                setNumCfrPoints(Math.max(2, Math.min(4096, Math.round(Number(e.target.value)))))
              }
            />
          </span>
        </label>
        <div
          className="channel-live"
          style={{
            marginTop: 8,
            paddingTop: 8,
            borderTop: "1px solid var(--border, #333)",
          }}
        >
          <div
            className="channel-live-head"
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 6,
            }}
          >
            <span className="channel-live-title" style={{ fontWeight: 600, fontSize: 12 }}>
              Live parameters
            </span>
            <button
              className="channel-live-reset"
              disabled={disabled}
              onClick={resetLive}
              title="Restore these values from the current solver config and TX device"
            >
              Reset to config
            </button>
          </div>
          <label className="solver-field">
            <span className="solver-field-label">Frequency (GHz)</span>
            <span className="solver-field-input">
              <input
                type="number"
                min={0.1}
                max={100}
                step={0.1}
                value={live.freqGhz}
                disabled={disabled}
                onChange={(e) => patchLive({ freqGhz: Number(e.target.value) })}
              />
            </span>
          </label>
          <label className="solver-field">
            <span className="solver-field-label">Bandwidth (MHz)</span>
            <span className="solver-field-input">
              <input
                type="number"
                min={0.1}
                max={2000}
                step={1}
                value={live.bandwidthMhz}
                disabled={disabled}
                onChange={(e) => patchLive({ bandwidthMhz: Number(e.target.value) })}
              />
            </span>
          </label>
          <label className="solver-field">
            <span className="solver-field-label">TX power (dBm)</span>
            <span className="solver-field-input">
              <input
                type="number"
                min={-30}
                max={60}
                step={1}
                value={live.txPowerDbm}
                disabled={disabled || !selectedTx}
                onChange={(e) => patchLive({ txPowerDbm: Number(e.target.value) })}
              />
            </span>
          </label>
          <label className="solver-field">
            <span className="solver-field-label">Noise figure (dB)</span>
            <span className="solver-field-input">
              <input
                type="number"
                min={0}
                max={30}
                step={0.5}
                value={live.noiseFigureDb}
                disabled={disabled}
                onChange={(e) => patchLive({ noiseFigureDb: Number(e.target.value) })}
              />
            </span>
          </label>
          <label className="solver-field">
            <span className="solver-field-label">SCS (kHz)</span>
            <span className="solver-field-input">
              <select
                value={live.scsKhz}
                disabled={disabled}
                onChange={(e) => patchLive({ scsKhz: Number(e.target.value) })}
              >
                {SCS_OPTIONS.map((s) => (
                  <option key={s} value={s}>
                    {s}
                  </option>
                ))}
              </select>
              <span
                className="channel-nrb mono"
                style={{ marginLeft: 8, opacity: 0.8, whiteSpace: "nowrap" }}
                title="Resource blocks = floor(BW / (12 × SCS))"
              >
                N_RB {numResourceBlocks}
              </span>
            </span>
          </label>
        </div>

        <div className="panel-actions">
          <button
            className="primary"
            disabled={!projectId || disabled || !txId || !rxId}
            onClick={() =>
              // Manual analyses persist (kind "channel") so the dashboard
              // survives reload; live/auto re-runs stay interactive-only.
              void analyzeChannel(txId, rxId, numCfrPoints, live.scsKhz, { persist: true })
            }
          >
            Analyze
          </button>
          {r && (
            <button disabled={disabled} onClick={() => clearChannel()} title="Clear channel result">
              Clear
            </button>
          )}
          <label
            className="solver-auto"
            title="Re-run the last analysis whenever the scene changes (needs one manual run first)"
          >
            <input
              type="checkbox"
              checked={autoChannel}
              disabled={disabled}
              onChange={(e) => setAuto("channel", e.target.checked)}
            />
            Auto update
          </label>
        </div>

        {r && (
          <>
            <div className="results-meta">
              <span className="mono">{r.tx_id}</span> → <span className="mono">{r.rx_id}{" "}
            <span
              className="traj-kind"
              title="Snapshot of the fixed TX/RX positions at compute time — a moving-UE sweep lives in the Trajectory panel"
            >
              fixed link
            </span>{" "}
            <StaleChip kind="channel" /></span> ·{" "}
              backend <span className="mono">{r.backend}</span> · {(r.frequency_hz / 1e9).toFixed(2)} GHz ·{" "}
              {r.distance_3d_m.toFixed(1)} m · {r.num_paths} path(s)
            </div>
            {r.warnings.length > 0 && (
              <div className="ai-note">
                {r.warnings.map((w, i) => (
                  <div key={i}>{w}</div>
                ))}
              </div>
            )}

            <h4>Link budget</h4>
            <div className="traj-kpis">
              {budget("RSS", fmt(r.rss_dbm, "dBm"))}
              {budget("SNR", fmt(r.snr_db, "dB"))}
              {budget("SINR", fmt(r.sinr_db ?? null, "dB"))}
              {budget(
                (r.num_interferers ?? 0) > 0 ? `Interference (${r.num_interferers} TX)` : "Interference",
                fmt(r.interference_dbm ?? null, "dBm"),
              )}
              {budget("Shannon", fmt(r.shannon_capacity_mbps, "Mbps"))}
              {budget("K-factor", fmt(r.k_factor_db, "dB"))}
              {budget("RMS DS", fmt(r.rms_delay_spread_ns, "ns", 2))}
              {budget("Coh. BW", fmt(r.coherence_bandwidth_mhz, "MHz", 2))}
              {r.doppler_spread_hz != null && budget("Doppler spread", fmt(r.doppler_spread_hz, "Hz", 1))}
              {r.coherence_time_ms != null && budget("Coh. time", fmt(r.coherence_time_ms, "ms", 2))}
            </div>

            <CirStemPlot cir={r.cir} />
            <CfrPlot freq={r.cfr_freq_offset_hz} mag={r.cfr_mag_db} />

            <h4 style={{ marginTop: 10 }}>Path-loss models vs RT</h4>
            <table className="results-table channel-pl-table">
              <thead>
                <tr>
                  <th>model</th>
                  <th>PL (dB)</th>
                  <th>Δ vs RT (dB)</th>
                </tr>
              </thead>
              <tbody>
                <tr className="channel-rt-row">
                  <td className="mono">RT (ray-traced)</td>
                  <td className="mono">{r.rt_path_loss_db === null ? "n/a" : r.rt_path_loss_db.toFixed(1)}</td>
                  <td className="mono">ref</td>
                </tr>
                {r.pl_models.map((m) =>
                  m.valid ? (
                    <tr key={m.model}>
                      <td className="mono" title={m.notes}>
                        {m.model}
                      </td>
                      <td className="mono">{m.path_loss_db === null ? "n/a" : m.path_loss_db.toFixed(1)}</td>
                      <td className="mono">
                        {m.delta_vs_rt_db === null
                          ? "n/a"
                          : `${m.delta_vs_rt_db > 0 ? "+" : ""}${m.delta_vs_rt_db.toFixed(1)}`}
                      </td>
                    </tr>
                  ) : (
                    // Invalid model (e.g. TR 36.777 aerial rows on a terrestrial
                    // link): muted, no bare number — show the reason from `notes`.
                    <tr key={m.model} className="channel-pl-invalid">
                      <td className="mono" title={m.notes}>
                        {m.model}
                      </td>
                      <td className="mono" colSpan={2} title={m.notes}>
                        {m.notes ? `N/A — ${m.notes}` : "N/A"}
                      </td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          </>
        )}
      </Section>

      <ChannelSweepSection txId={txId} rxId={rxId} disabled={disabled} />
      <SpectrogramSection txId={txId} rxId={rxId} disabled={disabled} />
      <FlightLogValidationSection txId={txId} disabled={disabled} />
    </div>
  );
}
