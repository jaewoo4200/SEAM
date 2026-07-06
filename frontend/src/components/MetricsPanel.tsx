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

import { useMemo } from "react";
import { useAppStore } from "../store/appStore";
import { StaleChip } from "./ResultExplorer";
import { PATH_COLORS } from "./common";
import {
  BarChart,
  LineChart,
  StemChart,
  exportCsv,
  CHART_COLORS,
  type Series,
} from "../charts";
import type { ChannelAnalysisResult, PathType, TrajectorySample } from "../types/api";

const KNOWN_PATH_TYPES = new Set<string>(Object.keys(PATH_COLORS));
function tapColor(pathType: string): string {
  return KNOWN_PATH_TYPES.has(pathType)
    ? PATH_COLORS[pathType as PathType]
    : PATH_COLORS.mixed;
}

/** Formats a nullable number as "value" (or "–" when absent). */
function num(v: number | null | undefined, digits = 1): string {
  return v === null || v === undefined || !Number.isFinite(v)
    ? "–"
    : (Math.round(v * 10 ** digits) / 10 ** digits).toString();
}

interface Kpi {
  label: string;
  /** Rendered value including unit (or "–"). */
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
      ? "–"
      : `${nrb === null ? "–" : nrb} @ ${scs === null ? "–" : scs} kHz`;
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
      title: "Shannon capacity B·log2(1+SNR) for the analysis bandwidth." },
    { label: "K-factor", unit: "dB", csv: r.k_factor_db ?? null,
      title: "Rician K-factor — ratio of dominant (LOS) path power to the scattered path power." },
    { label: "Mean delay", unit: "ns", csv: r.mean_delay_ns ?? null,
      title: "Power-weighted mean excess delay of the channel impulse response." },
    { label: "RMS delay spread", unit: "ns", csv: r.rms_delay_spread_ns ?? null,
      title: "Root-mean-square delay spread — square root of the second central moment of the power-delay profile." },
    { label: "Coherence BW", unit: "MHz", csv: r.coherence_bandwidth_mhz ?? null,
      title: "Coherence bandwidth — frequency span over which the channel is approximately flat (≈ 1 / (2π · RMS delay spread))." },
    { label: "Doppler spread", unit: "Hz", csv: r.doppler_spread_hz ?? null,
      title: "Doppler spread — RMS spread of the Doppler power spectrum from device/actor motion." },
    { label: "Coherence time", unit: "ms", csv: r.coherence_time_ms ?? null,
      title: "Coherence time — duration over which the channel is approximately static (≈ 1 / Doppler spread)." },
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
          ? "–"
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
    const samples: TrajectorySample[] = trajectory?.samples ?? [];
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
        Static link (fixed RX) - {r.tx_id} → {r.rx_id} <StaleChip kind="channel" />
      </h4>
      {Array.isArray(r.metadata?.rx_position_m) && (
        <p className="hint">
          Computed with RX at [
          {(r.metadata.rx_position_m as number[]).map((v) => v.toFixed(1)).join(", ")}
          ] - the trajectory section below is a separate moving-UE sweep.
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
    </div>
  );
}
