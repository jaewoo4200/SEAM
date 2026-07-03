import { useState } from "react";
import type { ReactNode } from "react";
import { useAppStore } from "../store/appStore";
import type { BeamformingMode, SimulationConfig } from "../types/api";
import { PRESETS, detectPreset } from "../configPresets";

// ------------------------------------------------------------ primitives

function Section({
  title,
  actions,
  defaultOpen = true,
  children,
}: {
  title: string;
  actions?: ReactNode;
  defaultOpen?: boolean;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  return (
    <div className="solver-section">
      <div className="solver-section-head">
        <button className="solver-caret" onClick={() => setOpen((o) => !o)}>
          <span className="caret">{open ? "▾" : "▸"}</span>
          {title}
        </button>
        {actions && <span className="solver-section-actions">{actions}</span>}
      </div>
      {open && <div className="solver-section-body">{children}</div>}
    </div>
  );
}

/** Number field bound to a config value with a unit suffix and optional scale. */
function NumField({
  label,
  value,
  unit,
  step,
  min,
  onChange,
  disabled,
}: {
  label: string;
  value: number;
  unit?: string;
  step?: number;
  min?: number;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <label className="solver-field">
      <span className="solver-field-label">{label}</span>
      <span className="solver-field-input">
        <input
          type="number"
          value={Number.isFinite(value) ? value : ""}
          step={step}
          min={min}
          disabled={disabled}
          onChange={(e) => {
            const n = Number(e.target.value);
            if (!Number.isNaN(n)) onChange(n);
          }}
        />
        {unit && <span className="solver-unit">{unit}</span>}
      </span>
    </label>
  );
}

function Slider({
  label,
  value,
  min,
  max,
  step,
  display,
  onChange,
  disabled,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  display: string;
  onChange: (v: number) => void;
  disabled?: boolean;
}) {
  return (
    <label className="solver-slider">
      <span className="solver-slider-head">
        <span>{label}</span>
        <span className="mono solver-slider-value">{display}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

function Check({
  label,
  checked,
  onChange,
  disabled,
}: {
  label: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  disabled?: boolean;
}) {
  return (
    <label className="solver-check">
      <input
        type="checkbox"
        checked={checked}
        disabled={disabled}
        onChange={(e) => onChange(e.target.checked)}
      />
      {label}
    </label>
  );
}

// The five (+edge) interaction mechanisms, matching the Polyscope panel order.
const MECHANISMS: { key: keyof SimulationConfig; label: string }[] = [
  { key: "los", label: "Line of sight" },
  { key: "reflection", label: "Specular reflection" },
  { key: "scattering", label: "Diffuse reflection" },
  { key: "refraction", label: "Refraction" },
  { key: "diffraction", label: "Diffraction" },
  { key: "edge_diffraction", label: "Edge diffraction" },
];

/** log10 of num_samples clamped to the 10^4..10^7 slider range. */
function samplesLog10(config: SimulationConfig): number {
  const x = Math.log10(Math.max(1, config.num_samples));
  return Math.min(7, Math.max(4, Math.round(x)));
}

function Mechanisms({
  config,
  patch,
  disabled,
}: {
  config: SimulationConfig;
  patch: (p: Partial<SimulationConfig>) => void;
  disabled: boolean;
}) {
  return (
    <div className="solver-mechanisms">
      {MECHANISMS.map((m) => (
        <Check
          key={m.key}
          label={m.label}
          checked={config[m.key] as boolean}
          disabled={disabled}
          onChange={(v) => patch({ [m.key]: v } as Partial<SimulationConfig>)}
        />
      ))}
    </div>
  );
}

/** Max depth + samples-log10 sliders shared by both solver sections. */
function DepthAndSamples({
  config,
  patch,
  disabled,
}: {
  config: SimulationConfig;
  patch: (p: Partial<SimulationConfig>) => void;
  disabled: boolean;
}) {
  const log = samplesLog10(config);
  return (
    <>
      <Slider
        label="Max depth"
        value={config.max_depth}
        min={0}
        max={12}
        step={1}
        display={String(config.max_depth)}
        disabled={disabled}
        onChange={(v) => patch({ max_depth: v })}
      />
      <Slider
        label="Samples / it (log 10)"
        value={log}
        min={4}
        max={7}
        step={1}
        display={`10^${log} = ${(10 ** log).toLocaleString()}`}
        disabled={disabled}
        onChange={(v) => patch({ num_samples: 10 ** v })}
      />
    </>
  );
}

// -------------------------------------------------------------- sections

function GlobalSection() {
  const pathsConfig = useAppStore((s) => s.pathsConfig);
  const setPathsConfig = useAppStore((s) => s.setPathsConfig);
  const setRadioMapConfig = useAppStore((s) => s.setRadioMapConfig);
  const busy = useAppStore((s) => s.busy);
  const runBeamforming = useAppStore((s) => s.runBeamforming);
  const projectId = useAppStore((s) => s.projectId);
  const bfTxRows = useAppStore((s) => s.bfTxRows);
  const bfTxCols = useAppStore((s) => s.bfTxCols);
  const bfRxRows = useAppStore((s) => s.bfRxRows);
  const bfRxCols = useAppStore((s) => s.bfRxCols);
  const setBeamArray = useAppStore((s) => s.setBeamArray);
  const bfMode = useAppStore((s) => s.bfMode);
  const bfSweepStartDeg = useAppStore((s) => s.bfSweepStartDeg);
  const bfSweepStopDeg = useAppStore((s) => s.bfSweepStopDeg);
  const bfSweepStepDeg = useAppStore((s) => s.bfSweepStepDeg);
  const setBeamforming = useAppStore((s) => s.setBeamforming);
  const autoBeamforming = useAppStore((s) => s.autoBeamforming);
  const setAuto = useAppStore((s) => s.setAuto);
  const liveMode = useAppStore((s) => s.liveMode);
  const setLiveMode = useAppStore((s) => s.setLiveMode);
  const sendScreenshot = useAppStore((s) => s.sendScreenshot);
  const setSendScreenshot = useAppStore((s) => s.setSendScreenshot);
  const disabled = busy !== null;

  // Global fields live on both configs so backend/frequency/etc. stay in sync.
  const patchBoth = (p: Partial<SimulationConfig>) => {
    setPathsConfig(p);
    setRadioMapConfig(p);
  };

  const arraySizes = [1, 2, 4, 8];
  const arraySelect = (
    value: number,
    onChange: (v: number) => void,
  ) => (
    <select value={value} disabled={disabled} onChange={(e) => onChange(Number(e.target.value))}>
      {arraySizes.map((n) => (
        <option key={n} value={n}>
          {n}
        </option>
      ))}
    </select>
  );

  const applyConfigPreset = useAppStore((s) => s.applyConfigPreset);
  // Which named preset the live paths config currently matches ("custom" if
  // none). The select reflects it and re-derives on every config edit.
  const activePreset = detectPreset(pathsConfig);

  return (
    <Section title="Global">
      <label className="solver-field">
        <span className="solver-field-label">Preset</span>
        <select
          value={activePreset}
          disabled={disabled}
          onChange={(e) => applyConfigPreset(e.target.value as typeof activePreset)}
          title="Apply a canonical solver configuration to both Paths and Radio map"
        >
          {PRESETS.map((p) => (
            <option key={p.id} value={p.id}>
              {p.label}
            </option>
          ))}
          <option value="custom">Custom</option>
        </select>
      </label>
      <label className="solver-field">
        <span className="solver-field-label">Backend</span>
        <select
          value={pathsConfig.backend}
          disabled={disabled}
          onChange={(e) => patchBoth({ backend: e.target.value as SimulationConfig["backend"] })}
        >
          <option value="auto">auto</option>
          <option value="mock">mock</option>
          <option value="sionna">sionna</option>
        </select>
      </label>
      <NumField
        label="Frequency"
        unit="GHz"
        step={0.1}
        min={0}
        value={pathsConfig.frequency_hz / 1e9}
        disabled={disabled}
        onChange={(v) => patchBoth({ frequency_hz: v * 1e9 })}
      />
      <NumField
        label="Bandwidth"
        unit="MHz"
        step={1}
        min={0}
        value={pathsConfig.bandwidth_hz / 1e6}
        disabled={disabled}
        onChange={(v) => patchBoth({ bandwidth_hz: v * 1e6 })}
      />
      <NumField
        label="Noise figure"
        unit="dB"
        step={0.5}
        min={0}
        value={pathsConfig.noise_figure_db}
        disabled={disabled}
        onChange={(v) => patchBoth({ noise_figure_db: v })}
      />
      <NumField
        label="Seed"
        step={1}
        min={0}
        value={pathsConfig.seed}
        disabled={disabled}
        onChange={(v) => patchBoth({ seed: Math.max(0, Math.round(v)) })}
      />

      <div className="solver-subhead">Beamforming array</div>
      <div className="solver-array-grid">
        <span className="solver-array-label">TX rows × cols</span>
        <span className="solver-array-selects">
          {arraySelect(bfTxRows, (v) => setBeamArray({ bfTxRows: v }))}
          <span className="solver-times">×</span>
          {arraySelect(bfTxCols, (v) => setBeamArray({ bfTxCols: v }))}
        </span>
        <span className="solver-array-label">RX rows × cols</span>
        <span className="solver-array-selects">
          {arraySelect(bfRxRows, (v) => setBeamArray({ bfRxRows: v }))}
          <span className="solver-times">×</span>
          {arraySelect(bfRxCols, (v) => setBeamArray({ bfRxCols: v }))}
        </span>
      </div>
      <label className="solver-field">
        <span className="solver-field-label">Mode</span>
        <select
          value={bfMode}
          disabled={disabled}
          onChange={(e) => setBeamforming({ bfMode: e.target.value as BeamformingMode })}
        >
          <option value="codebook_sweep">codebook sweep</option>
          <option value="tx_mrt">TX-MRT</option>
          <option value="svd">SVD</option>
        </select>
      </label>
      {bfMode === "codebook_sweep" && (
        <div className="solver-sweep-grid">
          <NumField
            label="Sweep start"
            unit="°"
            step={5}
            value={bfSweepStartDeg}
            disabled={disabled}
            onChange={(v) => setBeamforming({ bfSweepStartDeg: v })}
          />
          <NumField
            label="Sweep stop"
            unit="°"
            step={5}
            value={bfSweepStopDeg}
            disabled={disabled}
            onChange={(v) => setBeamforming({ bfSweepStopDeg: v })}
          />
          <NumField
            label="Sweep step"
            unit="°"
            step={1}
            min={0.5}
            value={bfSweepStepDeg}
            disabled={disabled}
            onChange={(v) => setBeamforming({ bfSweepStepDeg: Math.max(0.5, v) })}
          />
        </div>
      )}
      <div className="panel-actions">
        <button
          disabled={!projectId || disabled}
          onClick={() => void runBeamforming()}
          title="MIMO beamforming gain (codebook sweep, TX-MRT, or both-ends SVD) over the first TX→RX link"
        >
          Beamforming
        </button>
        <label className="solver-auto">
          <input
            type="checkbox"
            checked={autoBeamforming}
            disabled={disabled}
            onChange={(e) => setAuto("beamforming", e.target.checked)}
          />
          Auto update
        </label>
      </div>

      <div className="solver-subhead">Live &amp; AI</div>
      <label className="solver-check">
        <input
          type="checkbox"
          checked={liveMode}
          disabled={!projectId}
          onChange={(e) => setLiveMode(e.target.checked)}
        />
        Live sync
        {liveMode && <span className="live-badge">LIVE</span>}
      </label>
      <p className="hint" style={{ margin: "0 0 4px" }}>
        Polls the scene every 2&nbsp;s and refreshes device/actor positions in the viewer.
      </p>
      <label className="solver-check">
        <input
          type="checkbox"
          checked={sendScreenshot}
          onChange={(e) => setSendScreenshot(e.target.checked)}
        />
        Attach viewport to AI
      </label>
    </Section>
  );
}

function PathsSection() {
  const config = useAppStore((s) => s.pathsConfig);
  const patch = useAppStore((s) => s.setPathsConfig);
  const simulatePaths = useAppStore((s) => s.simulatePaths);
  const removePaths = useAppStore((s) => s.removePaths);
  const autoPaths = useAppStore((s) => s.autoPaths);
  const setAuto = useAppStore((s) => s.setAuto);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);
  const disabled = busy !== null;

  return (
    <Section
      title="Paths"
      actions={
        <label className="solver-auto">
          <input
            type="checkbox"
            checked={autoPaths}
            disabled={disabled}
            onChange={(e) => setAuto("paths", e.target.checked)}
          />
          Auto update
        </label>
      }
    >
      <div className="panel-actions">
        <button className="primary" disabled={!projectId || disabled} onClick={() => void simulatePaths()}>
          Compute paths
        </button>
        <button disabled={disabled} onClick={() => removePaths()} title="Clear the ray overlay">
          Remove
        </button>
      </div>
      <DepthAndSamples config={config} patch={patch} disabled={disabled} />
      <Check
        label="Synthetic array"
        checked={config.synthetic_array}
        disabled={disabled}
        onChange={(v) => patch({ synthetic_array: v })}
      />
      <div className="solver-subhead">Mechanisms</div>
      <Mechanisms config={config} patch={patch} disabled={disabled} />
    </Section>
  );
}

function RadioMapSection() {
  const config = useAppStore((s) => s.radioMapConfig);
  const patch = useAppStore((s) => s.setRadioMapConfig);
  const simulateRadioMap = useAppStore((s) => s.simulateRadioMap);
  const removeRadioMap = useAppStore((s) => s.removeRadioMap);
  const autoRadioMap = useAppStore((s) => s.autoRadioMap);
  const setAuto = useAppStore((s) => s.setAuto);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);
  const disabled = busy !== null;

  const patchGrid = (p: Partial<SimulationConfig["radio_map"]>) =>
    patch({ radio_map: { ...config.radio_map, ...p } });

  return (
    <Section
      title="Radio map"
      actions={
        <label className="solver-auto">
          <input
            type="checkbox"
            checked={autoRadioMap}
            disabled={disabled}
            onChange={(e) => setAuto("radioMap", e.target.checked)}
          />
          Auto update
        </label>
      }
    >
      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || disabled}
          onClick={() => void simulateRadioMap()}
        >
          Compute radio map
        </button>
        <button disabled={disabled} onClick={() => removeRadioMap()} title="Clear the radio-map overlay">
          Remove
        </button>
      </div>
      <NumField
        label="Cell size"
        unit="m"
        step={0.5}
        min={0}
        value={config.radio_map.cell_size_m}
        disabled={disabled}
        onChange={(v) => patchGrid({ cell_size_m: v })}
      />
      <NumField
        label="Height"
        unit="m"
        step={0.5}
        value={config.radio_map.height_m}
        disabled={disabled}
        onChange={(v) => patchGrid({ height_m: v })}
      />
      <label className="solver-field">
        <span className="solver-field-label">Metric</span>
        <select
          value={config.radio_map.metric}
          disabled={disabled}
          onChange={(e) =>
            patchGrid({ metric: e.target.value as SimulationConfig["radio_map"]["metric"] })
          }
        >
          <option value="path_gain_db">path_gain_db</option>
          <option value="rss_dbm">rss_dbm</option>
        </select>
      </label>
      <DepthAndSamples config={config} patch={patch} disabled={disabled} />
      <div className="solver-subhead">Mechanisms</div>
      <Mechanisms config={config} patch={patch} disabled={disabled} />
    </Section>
  );
}

export default function SolverControls() {
  const saveProjectDefault = useAppStore((s) => s.saveProjectDefault);
  const busy = useAppStore((s) => s.busy);
  const projectId = useAppStore((s) => s.projectId);

  return (
    <div className="panel solver-controls">
      <h3 className="panel-title">Simulation</h3>
      <GlobalSection />
      <PathsSection />
      <RadioMapSection />
      <div className="panel-actions">
        <button
          disabled={!projectId || busy !== null}
          onClick={() => void saveProjectDefault()}
          title="Write the Paths config into the scene's default simulation config and save"
        >
          Save as project default
        </button>
      </div>
    </div>
  );
}
