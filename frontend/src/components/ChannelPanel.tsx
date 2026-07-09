/**
 * Channel analysis panel (Results mode, below SolverControls). This is the
 * InSite / operator-style RT-vs-38.901 view: a link budget for one TX->RX
 * link, the ray CIR as a stem plot, the CFR magnitude, and a path-loss model
 * comparison table where the ray-traced (RT) reference row is highlighted and
 * every 3GPP TR 38.901 / CI model shows its delta versus RT.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import { StaleChip } from "./ResultExplorer";
import { PATH_COLORS } from "./common";
import type { CirTap, PathType } from "../types/api";

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
            onClick={() => void analyzeChannel(txId, rxId, numCfrPoints, live.scsKhz)}
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
                {r.pl_models.map((m) => (
                  <tr key={m.model} className={m.valid ? "" : "channel-pl-invalid"}>
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
                ))}
              </tbody>
            </table>
          </>
        )}
      </Section>
    </div>
  );
}
