/**
 * ISAC trade-off (B1) and sensing coverage (B2) sections of the Results panel.
 *
 * Both run a backend solve through the store (runIsac / runSensingCoverage)
 * and read the stored result back from it; the viewport overlays (beam lobes,
 * coverage plane) are drawn by Viewer3D from the same store slices.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { InputHTMLAttributes } from "react";
import { useAppStore } from "../store/appStore";
import { Collapsible, EpochStaleChip } from "./common";
import { Axes, CHART_COLORS, CHART_FONT, ChartFrame, exportCsv, xScale, yScale } from "../charts";
import type { ChartGeom } from "../charts";
import { ISAC_COMM_COLOR, ISAC_SENSING_COLOR } from "./IsacOverlay";
import type {
  ISACPoint,
  ISACTxResult,
  SensingCoverageMetric,
  SensingCoverageResultSet,
  SharingMode,
} from "../types/api";

const DEFAULT_SLOT_RATIOS = "0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1";
const MAX_SLOT_RATIOS = 32;
const MAX_CODEBOOK = 361;
// Beams × nonzero slot ratios per TX (schemas/sensing.py MAX_ISAC_POINTS).
const MAX_ISAC_POINTS = 4096;
// TR 38.901 uav-small-size nominal RCS (schemas/sensing.py TR38901_NOMINAL_RCS_DBSM).
const NOMINAL_UAV_RCS_DBSM = -12.81;

function fmtOr(v: number | null | undefined, digits: number, unit = ""): string {
  return v == null || !Number.isFinite(v) ? "—" : `${v.toFixed(digits)}${unit}`;
}

/** Pd: two decimals, but Pfa-scale values (no sensing) stay readable as 1e-6. */
function fmtPd(v: number): string {
  return v > 0 && v < 0.005 ? v.toExponential(0) : v.toFixed(2);
}

function fmtDeg(v: number | null | undefined): string {
  return v == null || !Number.isFinite(v) ? "—" : `${+v.toFixed(1)}°`;
}

/** Clamp a number input on commit (live clamping fights partial typing). */
function clampNum(v: number, lo: number, hi: number, fallback: number): number {
  return Number.isFinite(v) ? Math.max(lo, Math.min(hi, v)) : fallback;
}

/** "1e-6" → 1e-6; null unless strictly inside (0, 1). */
function parseProbability(s: string): number | null {
  const v = Number(s.trim());
  return s.trim() !== "" && Number.isFinite(v) && v > 0 && v < 1 ? v : null;
}

/** Comma/space separated ratios in [0, 1], sorted and deduped; `bad` holds
 *  every token that is not a number in [0, 1] (reported, never dropped). */
function parseSlotRatios(s: string): { values: number[]; bad: string[] } {
  const values: number[] = [];
  const bad: string[] = [];
  for (const t of s.split(/[\s,;]+/)) {
    if (t === "") continue;
    const v = Number(t);
    if (Number.isFinite(v) && v >= 0 && v <= 1) values.push(v);
    else bad.push(t);
  }
  return { values: [...new Set(values)].sort((a, b) => a - b), bad };
}

function codebookSize(start: number, stop: number, step: number): number {
  if (!(step > 0) || start > stop) return 0;
  return Math.floor((stop - start) / step + 1e-9) + 1;
}

/** type="number" input that keeps unparseable partial text ("-", "1e") to
 *  itself: the parent only ever sees finite numbers. Mapping "-" to 0 would
 *  make React write "0" back, so editing -60 to -45 ended at +45. */
function NumericInput({
  value,
  onChange,
  onBlur,
  ...rest
}: {
  value: number;
  onChange: (v: number) => void;
  onBlur?: () => void;
} & Omit<InputHTMLAttributes<HTMLInputElement>, "value" | "onChange" | "onBlur" | "type">) {
  // The raw text; a partial "-" reads back as "" and stays "" here, so React
  // leaves the browser's own "-" alone.
  const [text, setText] = useState(String(value));
  useEffect(() => {
    setText((t) => (t.trim() !== "" && Number(t) === value ? t : String(value)));
  }, [value]);
  return (
    <input
      {...rest}
      type="number"
      value={text}
      onChange={(e) => {
        const t = e.target.value;
        setText(t);
        const v = Number(t);
        if (t.trim() !== "" && Number.isFinite(v)) onChange(v);
      }}
      onBlur={() => {
        setText(String(value));
        onBlur?.();
      }}
    />
  );
}

function NumberField({
  label,
  unit,
  value,
  onChange,
  onCommit,
  disabled,
  step = 1,
  title,
}: {
  label: string;
  unit?: string;
  value: number;
  onChange: (v: number) => void;
  onCommit?: () => void;
  disabled: boolean;
  step?: number;
  title?: string;
}) {
  return (
    <label className="solver-field" title={title}>
      <span className="solver-field-label">{label}</span>
      <span className="solver-field-input">
        <NumericInput
          step={step}
          value={value}
          disabled={disabled}
          onChange={onChange}
          onBlur={onCommit}
        />
        {unit && <span className="solver-unit">{unit}</span>}
      </span>
    </label>
  );
}

function ArrayField({
  label,
  rows,
  cols,
  setRows,
  setCols,
  disabled,
}: {
  label: string;
  rows: number;
  cols: number;
  setRows: (v: number) => void;
  setCols: (v: number) => void;
  disabled: boolean;
}) {
  return (
    <label className="solver-field" title="Planar array rows × cols (1..16); the codebook steers in azimuth only">
      <span className="solver-field-label">{label}</span>
      <span className="solver-field-input">
        <NumericInput
          min={1}
          max={16}
          step={1}
          value={rows}
          disabled={disabled}
          style={{ width: 52 }}
          onChange={setRows}
          onBlur={() => setRows(Math.round(clampNum(rows, 1, 16, 4)))}
        />
        <span className="solver-unit">×</span>
        <NumericInput
          min={1}
          max={16}
          step={1}
          value={cols}
          disabled={disabled}
          style={{ width: 52 }}
          onChange={setCols}
          onBlur={() => setCols(Math.round(clampNum(cols, 1, 16, 4)))}
        />
      </span>
    </label>
  );
}

function Warnings({ warnings }: { warnings: string[] }) {
  if (warnings.length === 0) return null;
  return (
    <div className="ai-note">
      {warnings.map((w, i) => (
        <div key={i}>{w}</div>
      ))}
    </div>
  );
}

// ------------------------------------------------------------ ISAC trade-off

/** Native-tooltip text of one trade-off point. */
function pointLabel(tx: ISACTxResult, p: ISACPoint): string {
  const rate = `${p.sum_rate_bps_hz.toFixed(2)} bit/s/Hz`;
  if (p.beam_idx === null) {
    return `ρ=${+p.rho.toFixed(2)} · comm only · ${rate} · Pd ${fmtPd(p.pd)}`;
  }
  return (
    `ρ=${p.rho.toFixed(2)} · beam ${fmtDeg(tx.angles_deg[p.beam_idx])} · ${rate} · ` +
    `Pd ${fmtPd(p.pd)} · SNR ${fmtOr(p.sensing_snr_db, 1, " dB")}`
  );
}

/** Pd (x) vs sum rate (y) of every slot-sharing point of one TX; the Pareto
 *  front is joined, the comm-only point (rho = 0) is a square. */
export function IsacParetoChart({ tx }: { tx: ISACTxResult }) {
  const ref = useRef<SVGSVGElement>(null);
  const W = 420;
  const H = 240;
  const maxRate = tx.points.reduce((m, p) => Math.max(m, p.sum_rate_bps_hz), 0);
  const g: ChartGeom = {
    W,
    H,
    L: 52,
    R: 12,
    T: 10,
    B: 34,
    xMin: 0,
    xMax: 1,
    yMin: 0,
    yMax: maxRate > 0 ? maxRate * 1.08 : 1,
  };
  const sx = xScale(g);
  const sy = yScale(g);
  const front = useMemo(
    () =>
      tx.points
        .filter((p) => p.pareto)
        .sort((a, b) => a.pd - b.pd || b.sum_rate_bps_hz - a.sum_rate_bps_hz),
    [tx],
  );
  const commOnly = tx.points.find((p) => p.beam_idx === null) ?? null;
  const name = `isac_${tx.tx_id}`;
  const onCsv = () =>
    exportCsv(
      name,
      ["rho", "beam_deg", "sum_rate_bps_hz", "sensing_snr_db", "pd", "pareto"],
      tx.points.map((p) => [
        p.rho,
        p.beam_idx === null ? null : (tx.angles_deg[p.beam_idx] ?? null),
        p.sum_rate_bps_hz,
        p.sensing_snr_db,
        p.pd,
        p.pareto ? 1 : 0,
      ]),
    );
  const pdTarget = tx.pareto.pd_target;
  const sq = 3.5;

  return (
    <ChartFrame title={`Pd–rate trade-off · ${tx.tx_id}`} name={name} svgRef={ref} onCsv={onCsv}>
      <svg ref={ref} viewBox={`0 0 ${W} ${H}`} className="chart-svg">
        <Axes
          g={g}
          xLabel="Pd (weakest target)"
          yLabel="Sum rate (bit/s/Hz)"
          xTicks={[0, 0.2, 0.4, 0.6, 0.8, 1]}
        />
        {pdTarget > 0 && pdTarget < 1 && (
          <g fontFamily={CHART_FONT} fontSize={10}>
            <line
              x1={sx(pdTarget)}
              y1={g.T}
              x2={sx(pdTarget)}
              y2={g.H - g.B}
              stroke="#000"
              strokeWidth={0.8}
              strokeDasharray="4 3"
            />
            <text x={sx(pdTarget) - 3} y={g.T + 11} textAnchor="end" fill="#000">
              Pd target {pdTarget}
            </text>
          </g>
        )}
        {tx.points
          .filter((p) => !p.pareto && p.beam_idx !== null)
          .map((p, i) => (
            <circle
              key={`o${i}`}
              cx={sx(p.pd)}
              cy={sy(p.sum_rate_bps_hz)}
              r={2.5}
              fill="#9e9e9e"
              fillOpacity={0.75}
            >
              <title>{pointLabel(tx, p)}</title>
            </circle>
          ))}
        {front.length > 1 && (
          <polyline
            points={front.map((p) => `${sx(p.pd).toFixed(1)},${sy(p.sum_rate_bps_hz).toFixed(1)}`).join(" ")}
            fill="none"
            stroke={CHART_COLORS[1]}
            strokeWidth={1.4}
          />
        )}
        {front
          .filter((p) => p.beam_idx !== null)
          .map((p, i) => (
            <circle
              key={`p${i}`}
              cx={sx(p.pd)}
              cy={sy(p.sum_rate_bps_hz)}
              r={3.5}
              fill={CHART_COLORS[1]}
            >
              <title>{pointLabel(tx, p)}</title>
            </circle>
          ))}
        {commOnly && (
          <rect
            x={sx(commOnly.pd) - sq}
            y={sy(commOnly.sum_rate_bps_hz) - sq}
            width={2 * sq}
            height={2 * sq}
            fill={commOnly.pareto ? CHART_COLORS[1] : CHART_COLORS[0]}
            stroke="#000"
            strokeWidth={0.8}
          >
            <title>{pointLabel(tx, commOnly)}</title>
          </rect>
        )}
      </svg>
      <div className="results-meta">
        ■ comm only (ρ = 0) · <span style={{ color: CHART_COLORS[1] }}>●</span> Pareto front (
        {tx.pareto.num_pareto_points}) · <span style={{ color: "#9e9e9e" }}>●</span> other (ρ, beam)
        points · hover a point for ρ / beam / SNR
      </div>
    </ChartFrame>
  );
}

/** Summary + beam table + Pareto chart of one TX of an ISAC result. */
function IsacTxView({ tx }: { tx: ISACTxResult }) {
  const pareto = tx.pareto;
  const multiTarget = tx.target_ids.length > 1;
  const atBeam =
    pareto.beam_idx_at_pd_target !== null ? tx.angles_deg[pareto.beam_idx_at_pd_target] : null;
  const anchors = [
    ...Object.entries(tx.ue_single_element_rss_dbm).map(
      ([u, v]) => `${u} single-element RSS ${fmtOr(v, 1, " dBm")}`,
    ),
    ...Object.entries(tx.target_single_element_snr_db).map(
      ([q, v]) => `${q} single-element echo SNR ${fmtOr(v, 1, " dB")}`,
    ),
  ];
  return (
    <>
      <div className="results-meta" title={anchors.join("\n") || undefined}>
        <span className="mono">
          {tx.tx_array[0]}×{tx.tx_array[1]}
        </span>{" "}
        TX · sensing rx <span className="mono">{tx.sensing_rx_id}</span> (
        {tx.sensing_baseline_m < 1
          ? "monostatic"
          : `bistatic ${tx.sensing_baseline_m.toFixed(0)} m`}
        ,{" "}
        <span className="mono">
          {tx.rx_array[0]}×{tx.rx_array[1]}
        </span>
        ) · UEs <span className="mono">{tx.ue_ids.length > 0 ? tx.ue_ids.join(", ") : "none"}</span>{" "}
        · targets <span className="mono">{tx.target_ids.join(", ") || "none"}</span>
      </div>
      <div className="results-meta">
        <span className="isac-swatch" style={{ background: ISAC_COMM_COLOR }} />
        comm <span className="mono">{fmtDeg(tx.comm_beam_angle_deg)}</span> ·{" "}
        <span className="isac-swatch" style={{ background: ISAC_SENSING_COLOR }} />
        sensing <span className="mono">{fmtDeg(tx.sensing_beam_angle_deg)}</span> · gap{" "}
        <span className="mono">{fmtDeg(tx.angle_gap_deg)}</span>
      </div>
      <div className="results-meta">
        rate @ Pd ≥ {pareto.pd_target}{" "}
        <span className="mono">{fmtOr(pareto.rate_at_pd_target_bps_hz, 2, " bit/s/Hz")}</span>
        {pareto.rho_at_pd_target !== null && (
          <>
            {" "}
            (ρ <span className="mono">{pareto.rho_at_pd_target}</span>, beam{" "}
            <span className="mono">{fmtDeg(atBeam)}</span>)
          </>
        )}{" "}
        · loss vs comm-only{" "}
        <span className="mono">{fmtOr(pareto.rate_loss_at_pd_target_bps_hz, 2)}</span> of{" "}
        <span className="mono">{pareto.comm_only_rate_bps_hz.toFixed(2)}</span> · Pd @ 95 % rate{" "}
        <span className="mono">{fmtPd(pareto.pd_at_95pct_rate)}</span> · max Pd{" "}
        <span className="mono">{fmtPd(pareto.max_pd)}</span>
      </div>
      <div className="isac-table-wrap">
        <table className="results-table">
          <thead>
            <tr>
              <th title="Codebook beam, local azimuth of the TX panel">beam</th>
              {tx.ue_ids.map((u) => (
                <th key={u} title={`SINR (= SNR, no inter-TX interference) of ${u} with this beam`}>
                  {u} dB
                </th>
              ))}
              <th title="Sum over this TX's UEs of log2(1 + SINR)">rate</th>
              {tx.target_ids.map((q) => (
                <th key={q} title={`Echo SNR of ${q}: this TX beam, best RX beam, full CPI`}>
                  {multiTarget ? `${q} dB` : "echo dB"}
                </th>
              ))}
              {tx.target_ids.map((q) => (
                <th key={`rx_${q}`} title={`Best sensing-RX beam for ${q}`}>
                  {multiTarget ? `RX° ${q}` : "best RX°"}
                </th>
              ))}
              <th title="Swerling-1 Pd of the weakest target with this beam full time">Pd</th>
            </tr>
          </thead>
          <tbody>
            {tx.beams.map((b, k) => {
              const cls =
                (k === tx.comm_beam_idx ? "isac-comm-row" : "") +
                (k === tx.sensing_beam_idx ? " isac-sensing-row" : "");
              return (
                <tr key={k} className={cls.trim()}>
                  <td className="mono">{fmtDeg(b.angle_deg)}</td>
                  {tx.ue_ids.map((u) => (
                    <td key={u} className="mono">
                      {fmtOr(b.ue_sinr_db[u], 1)}
                    </td>
                  ))}
                  <td className="mono">{b.sum_rate_bps_hz.toFixed(2)}</td>
                  {tx.target_ids.map((q) => (
                    <td key={q} className="mono">
                      {fmtOr(b.target_snr_db[q], 1)}
                    </td>
                  ))}
                  {tx.target_ids.map((q) => (
                    <td key={`rx_${q}`} className="mono">
                      {fmtDeg(b.target_best_rx_angle_deg[q])}
                    </td>
                  ))}
                  <td className={"mono " + (b.detected ? "sensing-ok" : "")}>{fmtPd(b.pd)}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {tx.points.length > 0 && <IsacParetoChart tx={tx} />}
      <Warnings warnings={tx.warnings} />
    </>
  );
}

export function IsacTradeoffSection() {
  const scene = useAppStore((s) => s.scene);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);
  const isac = useAppStore((s) => s.isac);
  const showIsac = useAppStore((s) => s.showIsac);
  const runIsac = useAppStore((s) => s.runIsac);
  const toggleOverlay = useAppStore((s) => s.toggleOverlay);
  const disabled = busy !== null;

  const txDevices = useMemo(
    () => (scene?.devices ?? []).filter((d) => d.kind === "tx"),
    [scene],
  );
  const hasTarget = (scene?.actors ?? []).some(
    (a) => a.sensing != null && a.sensing.enabled !== false,
  );

  const [txSel, setTxSel] = useState<string[]>([]);
  const [txRows, setTxRows] = useState(4);
  const [txCols, setTxCols] = useState(4);
  const [sweepStart, setSweepStart] = useState(-60);
  const [sweepStop, setSweepStop] = useState(60);
  const [sweepStep, setSweepStep] = useState(5);
  const [cpiPulses, setCpiPulses] = useState(4096);
  const [pfaStr, setPfaStr] = useState("1e-6");
  const [ratiosStr, setRatiosStr] = useState(DEFAULT_SLOT_RATIOS);
  const [sharingMode, setSharingMode] = useState<SharingMode>("dual_function");
  const [viewTx, setViewTx] = useState<string | null>(null);

  const { values: ratios, bad: badRatios } = useMemo(
    () => parseSlotRatios(ratiosStr),
    [ratiosStr],
  );
  const pfa = parseProbability(pfaStr);
  const nBeams = codebookSize(sweepStart, sweepStop, sweepStep);
  const nPoints = nBeams * ratios.filter((r) => r > 0).length;
  const selected = txSel.filter((id) => txDevices.some((d) => d.id === id));
  const problem =
    badRatios.length > 0
      ? `slot ratios: ${badRatios.map((t) => `"${t}"`).join(", ")} not a number in [0, 1]`
      : ratios.length === 0
        ? "slot ratios: enter at least one value in [0, 1]"
        : ratios.length > MAX_SLOT_RATIOS
          ? `slot ratios: at most ${MAX_SLOT_RATIOS} values`
          : pfa === null
            ? "Pfa must be strictly between 0 and 1"
            : nBeams === 0
              ? "sweep: start ≤ stop and step > 0"
              : nBeams > MAX_CODEBOOK
                ? `sweep: ${nBeams} beams (at most ${MAX_CODEBOOK})`
                : sweepStart < -90 || sweepStop > 90
                  ? "sweep angles must stay within ±90°"
                  : nPoints > MAX_ISAC_POINTS
                    ? `${nBeams} beams × ${nPoints / nBeams} nonzero ρ = ${nPoints} ` +
                      `trade-off points (at most ${MAX_ISAC_POINTS})`
                    : null;
  const canRun = !!projectId && !disabled && problem === null && txDevices.length > 0;

  const run = () => {
    if (!canRun || pfa === null) return;
    void runIsac({
      tx_ids: selected.length > 0 ? selected : null,
      tx_rows: Math.round(clampNum(txRows, 1, 16, 4)),
      tx_cols: Math.round(clampNum(txCols, 1, 16, 4)),
      sweep_start_deg: sweepStart,
      sweep_stop_deg: sweepStop,
      sweep_step_deg: sweepStep,
      cpi_pulses: Math.round(clampNum(cpiPulses, 1, 1e8, 4096)),
      pfa,
      slot_ratios: ratios,
      sharing_mode: sharingMode,
    });
  };

  const tx = isac ? (isac.txs.find((t) => t.tx_id === viewTx) ?? isac.txs[0] ?? null) : null;
  const serving = isac
    ? Object.entries(isac.ue_serving_tx)
        .map(([u, t]) => `${u}→${t ?? "none"}`)
        .join(", ")
    : "";

  return (
    <Collapsible title="ISAC trade-off">
      <p className="hint">
        Per TX: the best communication beam vs the best sensing beam of one azimuth codebook, and
        the Pd–rate Pareto over the share ρ of slots spent sensing. An rx within 1 m of a TX is its
        sensing receiver; every rx not within 1 m of any TX is a UE; needs ≥1 actor bound as a
        sensing target.
      </p>
      {txDevices.length > 0 && (
        <div className="solver-field" title="None checked = every TX">
          <span className="solver-field-label">TX</span>
          <span className="solver-field-input" style={{ flexWrap: "wrap", gap: 8 }}>
            {txDevices.map((d) => (
              <label key={d.id} className="solver-check" style={{ margin: 0 }}>
                <input
                  type="checkbox"
                  checked={selected.includes(d.id)}
                  disabled={disabled}
                  onChange={(e) =>
                    setTxSel(
                      e.target.checked
                        ? [...selected, d.id]
                        : selected.filter((id) => id !== d.id),
                    )
                  }
                />
                {d.id}
              </label>
            ))}
            {selected.length === 0 && <span className="solver-unit">all</span>}
          </span>
        </div>
      )}
      <ArrayField
        label="TX array"
        rows={txRows}
        cols={txCols}
        setRows={setTxRows}
        setCols={setTxCols}
        disabled={disabled}
      />
      <NumberField
        label="Sweep start"
        unit="°"
        value={sweepStart}
        onChange={setSweepStart}
        onCommit={() => setSweepStart(clampNum(sweepStart, -90, 90, -60))}
        disabled={disabled}
      />
      <NumberField
        label="Sweep stop"
        unit="°"
        value={sweepStop}
        onChange={setSweepStop}
        onCommit={() => setSweepStop(clampNum(sweepStop, -90, 90, 60))}
        disabled={disabled}
      />
      <NumberField
        label="Sweep step"
        unit="°"
        value={sweepStep}
        step={0.5}
        onChange={setSweepStep}
        onCommit={() => setSweepStep(sweepStep > 0 ? sweepStep : 5)}
        disabled={disabled}
        title={`${nBeams} beam(s)`}
      />
      <NumberField
        label="CPI pulses"
        value={cpiPulses}
        onChange={setCpiPulses}
        onCommit={() => setCpiPulses(Math.round(clampNum(cpiPulses, 1, 1e8, 4096)))}
        disabled={disabled}
        title="Coherently integrated pulses at ρ = 1 (gain 10·log10(ρ·pulses))"
      />
      <label className="solver-field" title="False-alarm probability of the Swerling-1 detector">
        <span className="solver-field-label">Pfa</span>
        <span className="solver-field-input">
          <input
            type="text"
            value={pfaStr}
            disabled={disabled}
            onChange={(e) => setPfaStr(e.target.value)}
          />
        </span>
      </label>
      <label className="solver-field" title="Fractions ρ of slots / pulses spent sensing, comma separated, in [0, 1]">
        <span className="solver-field-label">Slot ratios ρ</span>
        <span className="solver-field-input">
          <input
            type="text"
            value={ratiosStr}
            disabled={disabled}
            onChange={(e) => setRatiosStr(e.target.value)}
          />
        </span>
      </label>
      <label
        className="solver-field"
        title="dual_function: the sensing slots also carry data on the sensing beam; time_sharing: they carry none"
      >
        <span className="solver-field-label">Sharing</span>
        <span className="solver-field-input">
          <select
            value={sharingMode}
            disabled={disabled}
            onChange={(e) => setSharingMode(e.target.value as SharingMode)}
          >
            <option value="dual_function">dual_function</option>
            <option value="time_sharing">time_sharing</option>
          </select>
        </span>
      </label>
      {problem && <p className="hint sensing-miss">{problem}</p>}
      {!hasTarget && <p className="hint">No actor is bound as a sensing target yet.</p>}
      <div className="panel-actions">
        <button className="primary" disabled={!canRun} onClick={run}>
          Run ISAC trade-off
        </button>
      </div>

      {isac && (
        <>
          <div className="panel-actions" style={{ alignItems: "center" }}>
            <label className="solver-check" title="Comm (cyan) and sensing (magenta) beam lobe per TX">
              <input type="checkbox" checked={showIsac} onChange={() => toggleOverlay("isac")} />
              ISAC lobes
            </label>
            <EpochStaleChip kind="isac" />
          </div>
          <div className="results-meta">
            <span className="mono">{isac.result_id}</span> ·{" "}
            <span className="mono">{isac.backend}</span> ·{" "}
            <span className="mono">{(isac.frequency_hz / 1e9).toFixed(2)} GHz</span> ·{" "}
            <span className="mono">{isac.sharing_mode}</span>
            {serving && (
              <>
                {" "}
                · <span className="mono">{serving}</span>
              </>
            )}
          </div>
          {isac.txs.length === 0 ? (
            <p className="hint">The result holds no TX.</p>
          ) : (
            <>
              <label className="solver-field">
                <span className="solver-field-label">Show TX</span>
                <span className="solver-field-input">
                  <select value={tx?.tx_id ?? ""} onChange={(e) => setViewTx(e.target.value)}>
                    {isac.txs.map((t) => (
                      <option key={t.tx_id} value={t.tx_id}>
                        {t.tx_id}
                        {t.ue_ids.length === 0 ? " (sensing only)" : ""}
                      </option>
                    ))}
                  </select>
                </span>
              </label>
              {tx && <IsacTxView tx={tx} />}
            </>
          )}
          <Warnings warnings={isac.warnings} />
        </>
      )}
    </Collapsible>
  );
}

// ---------------------------------------------------------- sensing coverage

const COVERAGE_METRICS: { value: SensingCoverageMetric; label: string }[] = [
  { value: "best_snr_db", label: "Best SNR" },
  { value: "n_links_detected", label: "Links detected" },
  { value: "pd_best", label: "Pd" },
  { value: "fusion_feasible", label: "Fusion feasible" },
];

function CoverageSummary({ result }: { result: SensingCoverageResultSet }) {
  const s = result.summary;
  const los = typeof result.metadata?.los_model === "string" ? result.metadata.los_model : "";
  return (
    <>
      <div className="results-meta" title={los ? `LOS model: ${los}` : undefined}>
        <span className="mono">{result.result_id}</span> · <span className="mono">{s.num_cells}</span>{" "}
        cells · <span className="mono">{s.num_links}</span> links (
        <span className="mono">{s.num_geometries}</span> geometries) ·{" "}
        <span className="mono">{s.pct_cells_detected.toFixed(1)} %</span> ≥1 detection ·{" "}
        <span className="mono">{s.pct_cells_fusion_feasible.toFixed(1)} %</span> fusion-feasible ·
        median best SNR <span className="mono">{fmtOr(s.median_best_snr_db, 1, " dB")}</span> ·{" "}
        <span className="mono">{result.backend}</span>
      </div>
      <div className="results-meta">
        LOS <span className="mono">{s.pct_cells_los.toFixed(1)} %</span> · σ{" "}
        <span className="mono">{result.rcs_dbsm.toFixed(2)} dBsm</span> · array gain{" "}
        <span className="mono">+{result.array_gain_db.toFixed(1)} dB</span> · h{" "}
        <span className="mono">{result.grid.height_m} m</span> ·{" "}
        <span className="mono">{(result.frequency_hz / 1e9).toFixed(2)} GHz</span>
      </div>
      {result.links.length > 0 && (
        <div className="isac-table-wrap">
          <table className="results-table">
            <thead>
              <tr>
                <th>tx→rx</th>
                <th title="Links with the same foci (either order) share a geometry group">group</th>
                <th>baseline</th>
                <th title="Cells with both legs line-of-sight">LOS</th>
                <th title="Cells with SNR ≥ threshold">detected</th>
              </tr>
            </thead>
            <tbody>
              {result.links.map((l) => (
                <tr key={`${l.tx_id}>${l.rx_id}`}>
                  <td className="mono">
                    {l.tx_id}→{l.rx_id}
                  </td>
                  <td className="mono">{l.geometry_group}</td>
                  <td className="mono">{l.baseline_m.toFixed(0)} m</td>
                  <td className="mono">{(l.los_fraction * 100).toFixed(1)} %</td>
                  <td className="mono">{(l.detected_fraction * 100).toFixed(1)} %</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Warnings warnings={result.warnings} />
    </>
  );
}

export function SensingCoverageSection() {
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);
  const coverage = useAppStore((s) => s.sensingCoverage);
  const showCoverage = useAppStore((s) => s.showSensingCoverage);
  const metric = useAppStore((s) => s.coverageMetric);
  const setCoverageMetric = useAppStore((s) => s.setCoverageMetric);
  const runSensingCoverage = useAppStore((s) => s.runSensingCoverage);
  const toggleOverlay = useAppStore((s) => s.toggleOverlay);
  const disabled = busy !== null;

  const [heightM, setHeightM] = useState(60);
  const [cellM, setCellM] = useState(10);
  const [rcsStr, setRcsStr] = useState("");
  const [thresholdDb, setThresholdDb] = useState(13);
  const [pulses, setPulses] = useState(4096);
  const [pfaStr, setPfaStr] = useState("1e-6");
  const [arrayGain, setArrayGain] = useState<"none" | "steered">("none");
  const [txRows, setTxRows] = useState(4);
  const [txCols, setTxCols] = useState(4);

  const pfa = parseProbability(pfaStr);
  const rcs = rcsStr.trim() === "" ? null : Number(rcsStr);
  const problem =
    rcs !== null && !(Number.isFinite(rcs) && rcs >= -80 && rcs <= 80)
      ? "RCS must be within −80..80 dBsm (empty = TR 38.901 uav-small-size)"
      : pfa === null
        ? "Pfa must be strictly between 0 and 1"
        : !(cellM > 0 && cellM <= 1e4)
          ? "cell size must be in (0, 10 000] m"
          : !(Math.abs(heightM) <= 1e5)
            ? "height must be within ±100 000 m"
            : null;
  const canRun = !!projectId && !disabled && problem === null;

  const run = () => {
    if (!canRun || pfa === null) return;
    void runSensingCoverage({
      height_m: heightM,
      cell_size_m: cellM,
      rcs_dbsm: rcs,
      threshold_db: clampNum(thresholdDb, -30, 60, 13),
      cpi_pulses: Math.round(clampNum(pulses, 1, 1e8, 4096)),
      pfa,
      array_gain: arrayGain,
      ...(arrayGain === "steered"
        ? {
            tx_rows: Math.round(clampNum(txRows, 1, 16, 4)),
            tx_cols: Math.round(clampNum(txCols, 1, 16, 4)),
          }
        : {}),
    });
  };

  return (
    <Collapsible title="Sensing coverage">
      <p className="hint">
        Detection map of a virtual point target on a plane at the given height: bistatic radar
        equation on every TX × co-located sensing RX link, both legs line-of-sight (sionna) and
        multistatic fusion feasibility (≥ 3 distinct geometries).
      </p>
      <NumberField
        label="Height"
        unit="m"
        value={heightM}
        onChange={setHeightM}
        disabled={disabled}
      />
      <NumberField
        label="Cell size"
        unit="m"
        value={cellM}
        onChange={setCellM}
        onCommit={() => setCellM(cellM > 0 ? cellM : 10)}
        disabled={disabled}
      />
      <label className="solver-field" title="Constant point RCS; empty = TR 38.901 uav-small-size nominal">
        <span className="solver-field-label">RCS</span>
        <span className="solver-field-input">
          <input
            type="text"
            value={rcsStr}
            placeholder={`${NOMINAL_UAV_RCS_DBSM} (uav-small)`}
            disabled={disabled}
            onChange={(e) => setRcsStr(e.target.value)}
          />
          <span className="solver-unit">dBsm</span>
        </span>
      </label>
      <NumberField
        label="Threshold"
        unit="dB"
        value={thresholdDb}
        onChange={setThresholdDb}
        onCommit={() => setThresholdDb(clampNum(thresholdDb, -30, 60, 13))}
        disabled={disabled}
      />
      <NumberField
        label="CPI pulses"
        value={pulses}
        onChange={setPulses}
        onCommit={() => setPulses(Math.round(clampNum(pulses, 1, 1e8, 4096)))}
        disabled={disabled}
        title="Coherent integration gain = 10·log10(pulses)"
      />
      <label className="solver-field" title="False-alarm probability (Pd map)">
        <span className="solver-field-label">Pfa</span>
        <span className="solver-field-input">
          <input
            type="text"
            value={pfaStr}
            disabled={disabled}
            onChange={(e) => setPfaStr(e.target.value)}
          />
        </span>
      </label>
      <label
        className="solver-field"
        title="steered: ideal full array gain at both ends (10·log10 N_tx + 10·log10 N_rx)"
      >
        <span className="solver-field-label">Array gain</span>
        <span className="solver-field-input">
          <select
            value={arrayGain}
            disabled={disabled}
            onChange={(e) => setArrayGain(e.target.value as "none" | "steered")}
          >
            <option value="none">none (single element)</option>
            <option value="steered">steered</option>
          </select>
        </span>
      </label>
      {arrayGain === "steered" && (
        <ArrayField
          label="TX array"
          rows={txRows}
          cols={txCols}
          setRows={setTxRows}
          setCols={setTxCols}
          disabled={disabled}
        />
      )}
      <label className="solver-field" title="Metric drawn on the coverage plane">
        <span className="solver-field-label">Map metric</span>
        <span className="solver-field-input">
          <select
            value={metric}
            onChange={(e) => setCoverageMetric(e.target.value as SensingCoverageMetric)}
          >
            {COVERAGE_METRICS.map((m) => (
              <option key={m.value} value={m.value}>
                {m.label}
              </option>
            ))}
          </select>
        </span>
      </label>
      {problem && <p className="hint sensing-miss">{problem}</p>}
      <div className="panel-actions">
        <button className="primary" disabled={!canRun} onClick={run}>
          Run sensing coverage
        </button>
      </div>

      {coverage && (
        <>
          <div className="panel-actions" style={{ alignItems: "center" }}>
            <label className="solver-check" title="Coverage heatmap plane at the target height">
              <input
                type="checkbox"
                checked={showCoverage}
                onChange={() => toggleOverlay("sensingCoverage")}
              />
              Coverage map
            </label>
            <EpochStaleChip kind="sensing_coverage" />
          </div>
          <CoverageSummary result={coverage} />
        </>
      )}
    </Collapsible>
  );
}
