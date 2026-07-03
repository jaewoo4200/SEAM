/**
 * Floating viewport lighting/display panel (Unity/Blender style).
 *
 * Toggled by the gear button overlaid top-left of the 3D canvas. Edits the
 * per-project viewport settings slice in the app store (which persists them to
 * localStorage under 'stw.viewport.<pid>'). All controls are live — changes
 * apply to the viewer immediately.
 */

import { useAppStore } from "../store/appStore";
import type { ViewportSettings } from "../viewportSettings";

/** Labeled slider bound to a numeric viewport field. */
function Slider({
  label,
  value,
  min,
  max,
  step,
  onChange,
}: {
  label: string;
  value: number;
  min: number;
  max: number;
  step: number;
  onChange: (v: number) => void;
}) {
  return (
    <label className="vp-slider">
      <span className="vp-slider-head">
        <span>{label}</span>
        <span className="mono vp-slider-value">{value.toFixed(2)}</span>
      </span>
      <input
        type="range"
        min={min}
        max={max}
        step={step}
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
      />
    </label>
  );
}

/** Labeled color picker bound to a color viewport field. */
function ColorField({
  label,
  value,
  onChange,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <label className="vp-color">
      <span>{label}</span>
      <input type="color" value={value} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}

/** Labeled checkbox bound to a boolean viewport field. */
function Toggle({
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
    <label className={"vp-toggle" + (disabled ? " disabled" : "")}>
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

export default function ViewportPanel({ onClose }: { onClose: () => void }) {
  const viewport = useAppStore((s) => s.viewport);
  const setViewport = useAppStore((s) => s.setViewport);
  const resetViewport = useAppStore((s) => s.resetViewport);
  const scene = useAppStore((s) => s.scene);

  const patch = (p: Partial<ViewportSettings>) => setViewport(p);
  const hasOverlay = (scene?.assets.visual_overlay_uri ?? null) !== null;

  return (
    <div className="viewport-panel">
      <div className="viewport-panel-head">
        <span>Viewport</span>
        <button className="vp-close" title="Close" onClick={onClose}>
          ×
        </button>
      </div>

      <div className="viewport-panel-body">
        <div className="vp-section-title">Lighting</div>
        <Slider
          label="Ambient"
          value={viewport.ambientIntensity}
          min={0}
          max={2}
          step={0.05}
          onChange={(v) => patch({ ambientIntensity: v })}
        />
        <Slider
          label="Hemisphere"
          value={viewport.hemisphereIntensity}
          min={0}
          max={2}
          step={0.05}
          onChange={(v) => patch({ hemisphereIntensity: v })}
        />
        <Slider
          label="Directional"
          value={viewport.directionalIntensity}
          min={0}
          max={3}
          step={0.05}
          onChange={(v) => patch({ directionalIntensity: v })}
        />
        <Slider
          label="Sun azimuth"
          value={viewport.directionalAzimuthDeg}
          min={-180}
          max={180}
          step={1}
          onChange={(v) => patch({ directionalAzimuthDeg: v })}
        />
        <Slider
          label="Sun elevation"
          value={viewport.directionalElevationDeg}
          min={0}
          max={90}
          step={1}
          onChange={(v) => patch({ directionalElevationDeg: v })}
        />
        <ColorField
          label="Directional color"
          value={viewport.directionalColor}
          onChange={(v) => patch({ directionalColor: v })}
        />

        <div className="vp-section-title">Scene</div>
        <ColorField
          label="Background"
          value={viewport.backgroundColor}
          onChange={(v) => patch({ backgroundColor: v })}
        />
        <Toggle
          label="Grid"
          checked={viewport.showGrid}
          onChange={(v) => patch({ showGrid: v })}
        />
        <Toggle
          label="Axes"
          checked={viewport.showAxes}
          onChange={(v) => patch({ showAxes: v })}
        />
        <Toggle
          label={hasOverlay ? "Textured overlay" : "Textured overlay (none)"}
          checked={viewport.showOverlay}
          disabled={!hasOverlay}
          onChange={(v) => patch({ showOverlay: v })}
        />

        <div className="viewport-panel-actions">
          <button onClick={() => resetViewport()} title="Restore default lighting & display">
            Reset defaults
          </button>
        </div>
      </div>
    </div>
  );
}
