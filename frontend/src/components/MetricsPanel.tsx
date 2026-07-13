/**
 * Communication-metrics dashboard (dockable "Metrics dashboard" panel).
 *
 * One place to see every link-level metric at a glance and export it
 * paper-ready. The KPI grid summarizes store.channelResult; the figures use
 * charts.tsx exclusively (white background, Times New Roman, per-figure
 * PNG/SVG/CSV export). Trajectory time-series charts appear once a trajectory
 * has been simulated. Every figure only renders when its data exists and
 * otherwise shows its own empty hint.
 */

import { useMemo, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import { StaleChip } from "./ResultExplorer";
import { PATH_COLORS } from "./common";
import {
  BarChart,
  LineChart,
  StemChart,
  ChartFrame,
  exportCsv,
  ticks,
  fmtTick,
  CHART_COLORS,
  CHART_FONT,
  type Series,
} from "../charts";
import type {
  ChannelAnalysisResult,
  HandoverEvent,
  HandoverSummary,
  PathType,
  TrajectorySample,
} from "../types/api";

const KNOWN_PATH_TYPES = new Set<string>(Object.keys(PATH_COLORS));
function tapColor(pathType: string): string {
  return KNOWN_PATH_TYPES.has(pathType)
    ? PATH_COLORS[pathType as PathType]
    : PATH_COLORS.mixed;
}

/** Formats a nullable number as "value" (or "—" when absent). */
function num(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined || !Number.isFinite(v)
    ? "—"
    : (Math.round(v * 10 ** digits) / 10 ** digits).toString();
}

/** TX with the strongest per-step RSS at `step` (fallback serving-cell guess
 *  when the samples predate the serving_tx_id field). */
function argmaxTxAtStep(
  byTx: Record<string, (number | null)[]>,
  step: number,
): string | null {
  let best: string | null = null;
  let bestV = -Infinity;
  for (const [tx, arr] of Object.entries(byTx)) {
    const v = Array.isArray(arr) ? arr[step] : null;
    if (typeof v === "number" && Number.isFinite(v) && v > bestV) {
      bestV = v;
      best = tx;
    }
  }
  return best;
}

/** Reconstruct the per-step serving TX purely from A3 events (+ an optional
 *  per-TX RSS table for the pre-first-event cell), for results whose samples
 *  don't carry serving_tx_id. */
function reconstructServing(
  events: HandoverEvent[],
  numSteps: number,
  byTx: Record<string, (number | null)[]> | undefined,
): (string | null)[] {
  const sorted = [...events].sort((a, b) => a.step - b.step);
  let cur: string | null =
    sorted.length > 0 ? sorted[0].from_tx : byTx ? argmaxTxAtStep(byTx, 0) : null;
  const out: (string | null)[] = new Array(numSteps).fill(null);
  let ei = 0;
  for (let s = 0; s < numSteps; s++) {
    while (ei < sorted.length && sorted[ei].step <= s) {
      cur = sorted[ei].to_tx;
      ei++;
    }
    out[s] = cur;
  }
  return out;
}

/** Stepwise serving-cell chart: x = trajectory step, y = a categorical lane per
 *  distinct serving TX (labeled with the tx_id), with dashed markers at each A3
 *  handover step. Self-contained paper-styled SVG (charts.tsx has no step
 *  chart) wrapped in ChartFrame so it exports PNG / SVG / CSV like the rest. */
function ServingTxChart({
  title,
  name,
  steps,
  serving,
  lanes,
  laneOf,
  events,
}: {
  title: string;
  name: string;
  steps: number[];
  serving: (string | null)[];
  lanes: string[];
  laneOf: Map<string, number>;
  events: HandoverEvent[];
}) {
  const ref = useRef<SVGSVGElement>(null);
  const width = 460;
  const rowH = 26;
  const T = 10;
  const B = 34;
  const L = 96;
  const R = 14;
  const K = Math.max(1, lanes.length);
  const height = Math.max(150, T + B + K * rowH);
  const xMin = steps.length ? steps[0] : 0;
  const xMaxRaw = steps.length ? steps[steps.length - 1] : 1;
  const xMax = xMaxRaw > xMin ? xMaxRaw : xMin + 1;
  const sx = (v: number) => L + ((v - xMin) / (xMax - xMin)) * (width - L - R);
  const yc = (lane: number) =>
    height - B - ((lane + 0.5) / K) * (height - T - B);
  const xs = ticks(xMin, xMax);

  // Post-step staircase: hold each serving lane until the next step boundary.
  let d = "";
  let pen = false;
  for (let i = 0; i < steps.length; i++) {
    const v = serving[i];
    const lane = v == null ? undefined : laneOf.get(v);
    if (lane === undefined) {
      pen = false;
      continue;
    }
    const x = sx(steps[i]);
    const y = yc(lane);
    d += `${pen ? "L" : "M"}${x.toFixed(1)},${y.toFixed(1)}`;
    pen = true;
    const xNext = i + 1 < steps.length ? sx(steps[i + 1]) : x;
    d += `L${xNext.toFixed(1)},${y.toFixed(1)}`;
  }

  return (
    <ChartFrame
      title={title}
      name={name}
      svgRef={ref}
      onCsv={() =>
        exportCsv(name, ["step", "serving_tx"], steps.map((s, i) => [s, serving[i] ?? ""]))
      }
    >
      <svg ref={ref} viewBox={`0 0 ${width} ${height}`} className="chart-svg">
        <g fontFamily={CHART_FONT} fontSize={11} fill="#000">
          {/* lane gridlines + tx_id y labels */}
          {lanes.map((tx, l) => (
            <g key={tx}>
              <line x1={L} y1={yc(l)} x2={width - R} y2={yc(l)} stroke="#eeeeee" strokeWidth={0.5} />
              <line x1={L - 4} y1={yc(l)} x2={L} y2={yc(l)} stroke="#000" strokeWidth={1} />
              <text x={L - 7} y={yc(l) + 3.5} textAnchor="end">{tx}</text>
            </g>
          ))}
          {/* x gridlines + tick labels */}
          {xs.map((v) => (
            <g key={`x${v}`}>
              <line x1={sx(v)} y1={T} x2={sx(v)} y2={height - B} stroke="#dddddd" strokeWidth={0.5} />
              <text x={sx(v)} y={height - B + 14} textAnchor="middle">{fmtTick(v)}</text>
            </g>
          ))}
          {/* plot frame */}
          <rect x={L} y={T} width={width - L - R} height={height - T - B} fill="none" stroke="#000" strokeWidth={1} />
          {/* A3 handover markers */}
          {events.map((e, i) => (
            <line
              key={`ev${i}`}
              x1={sx(e.step)}
              y1={T}
              x2={sx(e.step)}
              y2={height - B}
              stroke="#c23b22"
              strokeWidth={1}
              strokeDasharray="4 3"
            />
          ))}
          {/* serving-cell staircase */}
          <path d={d} fill="none" stroke={CHART_COLORS[0]} strokeWidth={1.8} />
          {/* per-sample dots, colored by serving lane */}
          {steps.map((s, i) => {
            const v = serving[i];
            const lane = v == null ? undefined : laneOf.get(v);
            if (lane === undefined) return null;
            return (
              <circle
                key={`d${i}`}
                cx={sx(s)}
                cy={yc(lane)}
                r={2.4}
                fill={CHART_COLORS[lane % CHART_COLORS.length]}
              />
            );
          })}
          {/* x axis label */}
          <text x={(L + width - R) / 2} y={height - 4} textAnchor="middle" fontSize={12}>
            step
          </text>
        </g>
      </svg>
    </ChartFrame>
  );
}

interface Kpi {
  label: string;
  /** Rendered value including unit (or "—"). */
  display: string;
  /** Full definition, shown on hover. */
  title: string;
  /** Bare numeric value for the CSV export ("" when absent). */
  csv: number | null;
  unit: string;
}

/** The KPI table: labels, definitions, values and units for one link. */
function kpiRows(r: ChannelAnalysisResult): Kpi[] {
  const nrb = r.num_resource_blocks ?? null;
  const scs = r.subcarrier_spacing_khz ?? null;
  const numInterferers = r.num_interferers ?? 0;
  const rbDisplay =
    nrb === null && scs === null
      ? "—"
      : `${nrb === null ? "—" : nrb} @ ${scs === null ? "—" : scs} kHz`;
  return [
    { label: "RSS", unit: "dBm", csv: r.rss_dbm ?? null,
      title: "Received signal strength — total received power summed over all ray paths." },
    { label: "RSRP", unit: "dBm", csv: r.rsrp_dbm ?? null,
      title: "Reference Signal Received Power — mean power of the resource elements carrying reference signals (TS 38.215)." },
    { label: "RSSI", unit: "dBm", csv: r.rssi_dbm ?? null,
      title: "Received Signal Strength Indicator — total wideband received power over the measurement bandwidth." },
    { label: "RSRQ", unit: "dB", csv: r.rsrq_db ?? null,
      title: "Reference Signal Received Quality = N_RB · RSRP / RSSI (TS 38.215)." },
    { label: "Path loss", unit: "dB", csv: r.rt_path_loss_db ?? null,
      title: "Ray-traced path loss — free-space-referenced attenuation from the solved paths." },
    { label: "SNR", unit: "dB", csv: r.snr_db ?? null,
      title: "Signal-to-noise ratio — received power over thermal noise in the analysis bandwidth." },
    { label: "SINR", unit: "dB", csv: r.sinr_db ?? null,
      title: "S/(I+N) — equals SNR when no other TX transmits." },
    { label: numInterferers > 0 ? `INTERFERENCE (${numInterferers} TX)` : "Interference", unit: "dBm",
      csv: r.interference_dbm ?? null,
      title: "Co-channel power from all other TXs, full-buffer." },
    { label: "Shannon capacity", unit: "Mbps", csv: r.shannon_capacity_mbps ?? null,
      title: "Shannon capacity B·log2(1+SINR) for the analysis bandwidth (SINR equals SNR when no interfering TX)." },
    { label: "K-factor", unit: "dB", csv: r.k_factor_db ?? null,
      title: "Rician K-factor — ratio of dominant (LOS) path power to the scattered path power." },
    { label: "Mean delay", unit: "ns", csv: r.mean_delay_ns ?? null,
      title: "Power-weighted mean delay of the channel impulse response." },
    { label: "RMS delay spread", unit: "ns", csv: r.rms_delay_spread_ns ?? null,
      title: "Root-mean-square delay spread — square root of the second central moment of the power-delay profile." },
    { label: "Coherence BW", unit: "MHz", csv: r.coherence_bandwidth_mhz ?? null,
      title: "Coherence bandwidth — frequency span over which the channel is approximately flat (≈ 1 / (2π · RMS delay spread))." },
    { label: "Doppler spread", unit: "Hz", csv: r.doppler_spread_hz ?? null,
      title: "Doppler spread — RMS spread of the Doppler power spectrum from device/actor motion." },
    { label: "Coherence time", unit: "ms", csv: r.coherence_time_ms ?? null,
      title: "Coherence time ≈ 0.42 / max Doppler shift (Clarke/Jakes)." },
    { label: "Num paths", unit: "", csv: r.num_paths ?? null,
      title: "Number of ray paths in the solved channel impulse response." },
    { label: "N_RB @ SCS", unit: "", csv: null,
      title: "Number of OFDM resource blocks at the configured subcarrier spacing (used for RSRP/RSSI/RSRQ)." },
  ].map((k) => ({
    ...k,
    display:
      k.label === "N_RB @ SCS"
        ? rbDisplay
        : k.csv === null || k.csv === undefined
          ? "—"
          : `${num(k.csv, k.unit === "ns" || k.unit === "MHz" || k.unit === "ms" ? 2 : 1)}${k.unit ? " " + k.unit : ""}`,
  }));
}

function EmptyFigure({ title, hint }: { title: string; hint: string }) {
  return (
    <div className="chart-frame">
      <div className="chart-frame-head">
        <span className="chart-frame-title">{title}</span>
      </div>
      <p className="hint metrics-empty-fig">{hint}</p>
    </div>
  );
}

export default function MetricsPanel() {
  const r = useAppStore((s) => s.channelResult);
  const trajectory = useAppStore((s) => s.trajectory);
  const scenario = useAppStore((s) => s.scenario);
  // Which UE's handover to show (multi-UE runs); null = the primary UE.
  const [hoUe, setHoUe] = useState<string | null>(null);

  const kpis = useMemo(() => (r ? kpiRows(r) : []), [r]);

  // --- power-delay profile (stem) ---
  const pdpPoints = useMemo(
    () =>
      (r?.cir ?? []).map((t) => ({
        x: t.delay_ns,
        y: t.power_dbm,
        color: tapColor(t.path_type),
        label: t.path_type,
      })),
    [r],
  );

  // --- CFR magnitude (freq offset -> MHz) ---
  const cfrSeries = useMemo<Series[]>(() => {
    if (!r) return [];
    const n = Math.min(r.cfr_freq_offset_hz.length, r.cfr_mag_db.length);
    if (n === 0) return [];
    return [
      {
        label: "|H(f)|",
        x: r.cfr_freq_offset_hz.slice(0, n).map((hz) => hz / 1e6),
        y: r.cfr_mag_db.slice(0, n),
      },
    ];
  }, [r]);

  // --- Doppler fading envelope |h(t)| (time -> ms) ---
  const envSeries = useMemo<Series[]>(() => {
    const t = r?.cir_time_s ?? [];
    const e = r?.cir_time_envelope_db ?? [];
    const n = Math.min(t.length, e.length);
    if (n === 0) return [];
    return [
      { label: "|h(t)|", x: t.slice(0, n).map((s) => s * 1e3), y: e.slice(0, n) },
    ];
  }, [r]);

  // --- path-loss model comparison (bar) ---
  const plItems = useMemo(
    () =>
      (r?.pl_models ?? [])
        .filter((m) => m.valid && m.path_loss_db !== null)
        .map((m) => ({ label: m.model, value: m.path_loss_db })),
    [r],
  );

  // --- trajectory time series ---
  const traj = useMemo(() => {
    // Multi-UE results interleave samples step-major; the charts follow the
    // FIRST routed UE (the playback panel's KPI card has a per-UE selector).
    const all: TrajectorySample[] = trajectory?.samples ?? [];
    const firstUe = all[0]?.ue_id;
    const samples = all.filter((s) => s.ue_id === firstUe);
    if (samples.length === 0) return null;
    const t = samples.map((s) => s.time_s);
    const rss = samples.map((s) => s.rss_dbm);
    const sinr = samples.map((s) => s.sinr_db);
    const rms = samples.map((s) => s.rms_delay_spread_ns);
    const pc = samples.map((s) => s.path_count);
    // "SINR" once any sample carries interference from another TX, else "SNR".
    const hasInterference = samples.some((s) => s.interference_dbm != null);
    // Derived RSRP = RSS - 10·log10(12·N_RB) when N_RB is known from the
    // last channel analysis (12 subcarriers per resource block).
    const nrb = r?.num_resource_blocks ?? null;
    const rsrp =
      nrb && nrb > 0
        ? rss.map((v) => (v === null ? null : v - 10 * Math.log10(12 * nrb)))
        : null;
    return { t, rss, sinr, rms, pc, rsrp, hasInterference };
  }, [trajectory, r]);

  const trajPower = useMemo<Series[]>(() => {
    if (!traj) return [];
    const out: Series[] = [
      { label: "RSS (dBm)", x: traj.t, y: traj.rss, color: CHART_COLORS[0] },
      { label: (traj.hasInterference ? "SINR" : "SNR") + " (dB)", x: traj.t, y: traj.sinr, color: CHART_COLORS[1] },
    ];
    if (traj.rsrp) out.push({ label: "RSRP (derived)", x: traj.t, y: traj.rsrp, color: CHART_COLORS[3] });
    return out;
  }, [traj]);

  // RMS delay spread (ns) and path count live on very different scales, so
  // each gets its own single-series chart rather than a shared axis.
  const trajRms = useMemo<Series[]>(
    () => (traj ? [{ label: "RMS delay spread (ns)", x: traj.t, y: traj.rms, color: CHART_COLORS[4] }] : []),
    [traj],
  );
  const trajPaths = useMemo<Series[]>(
    () => (traj ? [{ label: "path count", x: traj.t, y: traj.pc, color: CHART_COLORS[2] }] : []),
    [traj],
  );

  // --- Doppler spread over time (idea #15: Doppler is computed, keep it) ---
  // metadata.doppler_spread_hz is parallel to samples; follow the FIRST routed
  // UE (like the other trajectory charts). Scenario runs may expose an
  // analogous per-frame array — plotted as a second series when present.
  const dopplerSeries = useMemo<Series[]>(() => {
    const out: Series[] = [];
    if (trajectory) {
      const dop = trajectory.metadata.doppler_spread_hz as (number | null)[] | undefined;
      const all = trajectory.samples;
      if (Array.isArray(dop) && all.length > 0) {
        const firstUe = all[0]?.ue_id;
        const n = Math.min(all.length, dop.length);
        const x: number[] = [];
        const y: (number | null)[] = [];
        for (let i = 0; i < n; i++) {
          if (all[i].ue_id !== firstUe) continue;
          x.push(all[i].time_s);
          const v = dop[i];
          y.push(typeof v === "number" && Number.isFinite(v) ? v : null);
        }
        if (y.some((v) => v != null)) {
          out.push({ label: `Doppler (${firstUe ?? "UE"})`, x, y, color: CHART_COLORS[4] });
        }
      }
    }
    if (scenario) {
      const dop = scenario.metadata.doppler_spread_hz as (number | null)[] | undefined;
      const frames = scenario.frames;
      if (Array.isArray(dop) && Array.isArray(frames) && frames.length > 0) {
        const n = Math.min(frames.length, dop.length);
        const x: number[] = [];
        const y: (number | null)[] = [];
        for (let i = 0; i < n; i++) {
          x.push(frames[i].time_s);
          const v = dop[i];
          y.push(typeof v === "number" && Number.isFinite(v) ? v : null);
        }
        if (y.some((v) => v != null)) {
          out.push({ label: "Doppler (scenario)", x, y, color: CHART_COLORS[5] });
        }
      }
    }
    return out;
  }, [trajectory, scenario]);

  // --- handover (idea #13: per-step serving TX + A3 events) ---
  // metadata.handover is present only on A3 runs; guard every read.
  const handover = useMemo(() => {
    const ho = trajectory?.metadata.handover as Record<string, HandoverSummary> | undefined;
    if (!ho || typeof ho !== "object") return null;
    const ueKeys = Object.keys(ho);
    if (ueKeys.length === 0) return null;
    return { ho, ueKeys };
  }, [trajectory]);

  // Resolve the shown UE: explicit pick if still valid, else the primary UE
  // (trajectory.ue_id) if it has handover data, else the first key.
  const hoUeSel = useMemo(() => {
    if (!handover) return null;
    const { ho, ueKeys } = handover;
    const primary = trajectory?.ue_id && ho[trajectory.ue_id] ? trajectory.ue_id : ueKeys[0];
    return hoUe && ho[hoUe] ? hoUe : primary;
  }, [handover, hoUe, trajectory]);

  const hoSummary = handover && hoUeSel ? handover.ho[hoUeSel] : null;

  // Serving-cell staircase + per-TX RSS series for the shown UE.
  const hoChart = useMemo(() => {
    if (!handover || !hoUeSel || !trajectory || !hoSummary) return null;
    const events = Array.isArray(hoSummary.events) ? hoSummary.events : [];

    const ueSamples = trajectory.samples.filter((s) => s.ue_id === hoUeSel);
    const txRssAll = trajectory.metadata.tx_rss_dbm as
      | Record<string, Record<string, (number | null)[]>>
      | undefined;
    const byTx =
      txRssAll && typeof txRssAll === "object" ? txRssAll[hoUeSel] : undefined;

    // Step count: the largest evidence we have (samples / per-TX RSS / num_steps).
    let numSteps = ueSamples.length;
    if (byTx) {
      for (const arr of Object.values(byTx)) {
        if (Array.isArray(arr)) numSteps = Math.max(numSteps, arr.length);
      }
    }
    const metaSteps = trajectory.metadata.num_steps;
    if (typeof metaSteps === "number") numSteps = Math.max(numSteps, metaSteps);
    if (numSteps <= 0) return null;

    // Prefer per-sample serving_tx_id; fall back to reconstructing from events.
    const fromSamples = ueSamples.map((s) => s.serving_tx_id ?? null);
    let serving: (string | null)[] = fromSamples.some((v) => v != null)
      ? fromSamples.slice(0, numSteps)
      : reconstructServing(events, numSteps, byTx);
    // Clamp / pad to exactly numSteps (hold the last known cell).
    if (serving.length > numSteps) serving = serving.slice(0, numSteps);
    while (serving.length < numSteps) {
      serving.push(serving.length ? serving[serving.length - 1] : null);
    }
    const steps = Array.from({ length: numSteps }, (_, i) => i);

    // Distinct serving cells (+ event endpoints) → integer lanes, in first-seen
    // order so the y axis reads chronologically.
    const lanes: string[] = [];
    const laneOf = new Map<string, number>();
    const see = (tx: string) => {
      if (!laneOf.has(tx)) {
        laneOf.set(tx, lanes.length);
        lanes.push(tx);
      }
    };
    for (const v of serving) if (v != null) see(v);
    for (const e of events) {
      see(e.from_tx);
      see(e.to_tx);
    }
    if (lanes.length === 0) return null;

    // Per-TX RSS overlay series (why the handover fired), colored to match lanes.
    let rssSeries: Series[] = [];
    if (byTx) {
      rssSeries = Object.keys(byTx).map((tx, i) => {
        const arr = Array.isArray(byTx[tx]) ? byTx[tx] : [];
        const n = Math.min(arr.length, numSteps);
        const lane = laneOf.get(tx);
        return {
          label: tx,
          x: steps.slice(0, n),
          y: arr.slice(0, n).map((v) => (typeof v === "number" && Number.isFinite(v) ? v : null)),
          color: CHART_COLORS[(lane ?? lanes.length + i) % CHART_COLORS.length],
        };
      });
    }

    return { events, steps, serving, lanes, laneOf, rssSeries };
  }, [handover, hoUeSel, hoSummary, trajectory]);

  const exportAll = () => {
    if (!r) return;
    exportCsv(
      "metrics_kpis",
      ["metric", "value", "unit"],
      kpis.map((k) => [k.label, k.label === "N_RB @ SCS" ? k.display : k.csv, k.unit]),
    );
  };

  if (!r) {
    return (
      <div className="panel metrics-panel">
        <p className="hint">
          Run a channel analysis (Channel panel → Analyze) to populate metrics;
          trajectory charts appear after Simulate trajectory.
        </p>
      </div>
    );
  }

  return (
    <div className="panel metrics-panel">
      <div className="metrics-header">
        <button className="metrics-export-all" onClick={exportAll} title="Download the KPI table as metric,value,unit CSV">
          Export all (CSV)
        </button>
        <span className="hint metrics-header-hint">
          Figures export as shown — white background, Times New Roman.
        </span>
      </div>

      <div className="results-meta">
        <span className="mono">{r.tx_id}</span> → <span className="mono">{r.rx_id}</span> ·{" "}
        backend <span className="mono">{r.backend}</span> · {(r.frequency_hz / 1e9).toFixed(2)} GHz ·{" "}
        {r.distance_3d_m.toFixed(1)} m · {r.num_paths} path(s)
      </div>

      <h4 className="metrics-section-head">
        Static link (fixed RX) — {r.tx_id} → {r.rx_id} <StaleChip kind="channel" />
      </h4>
      {Array.isArray(r.metadata?.rx_position_m) && (
        <p className="hint">
          Computed with RX at [
          {(r.metadata.rx_position_m as number[]).map((v) => v.toFixed(1)).join(", ")}
          ] — the trajectory section below is a separate moving-UE sweep.
        </p>
      )}
      <div className="metric-grid">
        {kpis.map((k) => (
          <div className="metric-cell" key={k.label} title={k.title}>
            <div className="metric-cell-label">{k.label}</div>
            <div className="metric-cell-value mono">{k.display}</div>
          </div>
        ))}
      </div>

      {/* a. power-delay profile */}
      {pdpPoints.length > 0 ? (
        <StemChart
          title="Power-delay profile"
          name="power_delay_profile"
          xLabel="delay (ns)"
          yLabel="power (dBm)"
          points={pdpPoints}
        />
      ) : (
        <EmptyFigure title="Power-delay profile" hint="No CIR taps in this result." />
      )}

      {/* b. CFR magnitude */}
      {cfrSeries.length > 0 ? (
        <LineChart
          title="CFR magnitude"
          name="cfr_magnitude"
          xLabel="frequency offset (MHz)"
          yLabel="|H(f)| (dB)"
          series={cfrSeries}
          legend={false}
        />
      ) : (
        <EmptyFigure title="CFR magnitude" hint="No channel frequency response in this result." />
      )}

      {/* c. Doppler fading envelope */}
      {envSeries.length > 0 ? (
        <LineChart
          title="Doppler fading envelope |h(t)|"
          name="fading_envelope"
          xLabel="time (ms)"
          yLabel="|h(t)| (dB)"
          series={envSeries}
          legend={false}
        />
      ) : (
        <EmptyFigure
          title="Doppler fading envelope |h(t)|"
          hint="No time-varying envelope — set device/actor velocities and re-analyze."
        />
      )}

      {/* d. path-loss model comparison */}
      {plItems.length > 0 ? (
        <BarChart
          title="Path-loss model comparison"
          name="path_loss_models"
          valueLabel="path loss (dB)"
          items={plItems}
          refLine={
            r.rt_path_loss_db !== null ? { value: r.rt_path_loss_db, label: "ray-traced" } : undefined
          }
        />
      ) : (
        <EmptyFigure title="Path-loss model comparison" hint="No valid path-loss models for this link." />
      )}

      {/* e. trajectory time series */}
      {traj ? (
        <>
      <h4 className="metrics-section-head">
        Moving UE (trajectory sweep) <StaleChip kind="trajectory" />
      </h4>
          <LineChart
            title="Trajectory: power vs time"
            name="trajectory_power"
            xLabel="time (s)"
            yLabel="dBm / dB"
            series={trajPower}
          />
          <LineChart
            title="Trajectory: RMS delay spread vs time"
            name="trajectory_rms_delay_spread"
            xLabel="time (s)"
            yLabel="RMS delay spread (ns)"
            series={trajRms}
            legend={false}
          />
          <LineChart
            title="Trajectory: path count vs time"
            name="trajectory_path_count"
            xLabel="time (s)"
            yLabel="path count"
            series={trajPaths}
            legend={false}
          />
        </>
      ) : (
        <EmptyFigure
          title="Trajectory time series"
          hint="Simulate a trajectory (Trajectory panel) to plot RSS / SNR / delay spread over time."
        />
      )}

      {/* f. Doppler spread over time (only when the run recorded it) */}
      {dopplerSeries.length > 0 && (
        <LineChart
          title="Doppler spread over trajectory"
          name="doppler_spread"
          xLabel="time (s)"
          yLabel="Doppler spread (Hz)"
          series={dopplerSeries}
          legend={dopplerSeries.length > 1}
        />
      )}

      {/* g. handover (A3 runs only) */}
      {handover && hoUeSel && hoSummary && (
        <>
          <h4 className="metrics-section-head">
            Handover <StaleChip kind="trajectory" />
          </h4>
          {handover.ueKeys.length > 1 && (
            <div className="results-meta">
              UE{" "}
              <select value={hoUeSel} onChange={(e) => setHoUe(e.target.value)}>
                {handover.ueKeys.map((u) => (
                  <option key={u} value={u}>
                    {u}
                  </option>
                ))}
              </select>
            </div>
          )}
          <div className="results-meta">
            <span className="mono">{hoUeSel}</span>: {hoSummary.count} handover
            {hoSummary.count === 1 ? "" : "s"},{" "}
            {hoSummary.ping_pongs > 0 ? (
              <span className="badge" style={{ borderColor: "var(--warn)", color: "var(--warn)" }}>
                {hoSummary.ping_pongs} ping-pong{hoSummary.ping_pongs === 1 ? "" : "s"}
              </span>
            ) : (
              <span>0 ping-pongs</span>
            )}
          </div>

          {hoChart && (
            <ServingTxChart
              title="Serving cell over trajectory"
              name="handover_serving_tx"
              steps={hoChart.steps}
              serving={hoChart.serving}
              lanes={hoChart.lanes}
              laneOf={hoChart.laneOf}
              events={hoChart.events}
            />
          )}

          {hoChart && hoChart.rssSeries.length > 0 && (
            <LineChart
              title="Per-TX RSS over trajectory (serving-cell selection)"
              name="handover_tx_rss"
              xLabel="step"
              yLabel="RSS (dBm)"
              series={hoChart.rssSeries}
            />
          )}

          <table className="results-table">
            <thead>
              <tr>
                <th>step</th>
                <th>time (s)</th>
                <th>from → to</th>
              </tr>
            </thead>
            <tbody>
              {hoSummary.events.length === 0 ? (
                <tr>
                  <td colSpan={3} className="hint">
                    No handover events on this UE.
                  </td>
                </tr>
              ) : (
                hoSummary.events.map((e, i) => (
                  <tr key={i}>
                    <td className="mono">{e.step}</td>
                    <td className="mono">{num(e.time_s, 2)}</td>
                    <td>
                      <span className="mono">{e.from_tx}</span> →{" "}
                      <span className="mono">{e.to_tx}</span>
                    </td>
                  </tr>
                ))
              )}
            </tbody>
          </table>
        </>
      )}
    </div>
  );
}
