import type { ReactNode } from "react";
import { useAppStore } from "../store/appStore";
import type {
  AssignmentStatus,
  PathType,
  RFMaterial,
  RFMaterialLibrary,
  Severity,
  Vec4,
} from "../types/api";

/** All result kinds that carry a computed-at epoch (see appStore.resultEpochs).
 *  Superset of the union in ResultExplorer's local StaleChip; kept here so
 *  panels outside ResultExplorer (SolverControls, ...) can flag staleness for
 *  the radio-map / scenario results too. */
export type ResultKind =
  | "paths"
  | "channel"
  | "trajectory"
  | "beamforming"
  | "mesh_radio_map"
  | "radio_map"
  | "scenario";

/** "Scene changed since this was computed" badge. Mirrors ResultExplorer's
 *  StaleChip (same class/markup) but covers every result kind, so it can mount
 *  next to the radio-map and scenario controls that live outside that file. */
export function EpochStaleChip({ kind }: { kind: ResultKind }) {
  const sceneEpoch = useAppStore((st) => st.sceneEpoch);
  const at = useAppStore((st) => st.resultEpochs[kind]);
  if (at === undefined || at === sceneEpoch) return null;
  return (
    <span
      className="stale-chip"
      title="The scene was edited after this result was computed - re-run to refresh"
    >
      ⚠ stale
    </span>
  );
}

export const ACCENT = "#4fc3f7";

// AODT-like viewer path palette (guide section 17): LOS cyan, reflection
// magenta, diffraction orange. Single source of truth for the 3D viewer,
// the results table, and the scatter plot.
export const PATH_COLORS: Record<PathType, string> = {
  los: "#00e5ff",
  reflection: "#ff00ff",
  diffraction: "#ff9800",
  scattering: "#00e676",
  transmission: "#ff80ab",
  mixed: "#b0bec5",
};

export const SELECTED_PATH_COLOR = "#ffee58";

export const STATUS_COLORS: Record<AssignmentStatus, string> = {
  unassigned: "#ff9800",
  rule_suggested: "#ffd54f",
  rule_assigned: "#9ccc65",
  ai_suggested: "#ffd54f",
  user_confirmed: "#66bb6a",
  measurement_calibrated: "#29b6f6",
  rejected: "#e57373",
};

export const SEVERITY_COLORS: Record<Severity, string> = {
  error: "#ef5350",
  warning: "#ffb74d",
  info: "#4fc3f7",
};

export function Swatch({ color, size = 12 }: { color: string; size?: number }) {
  return (
    <span
      className="swatch"
      style={{ background: color, width: size, height: size }}
      title={color}
    />
  );
}

export function StatusDot({ status }: { status: AssignmentStatus }) {
  return <span className="dot" title={status} style={{ background: STATUS_COLORS[status] }} />;
}

export function StatusBadge({ status }: { status: AssignmentStatus }) {
  return (
    <span className="badge" style={{ borderColor: STATUS_COLORS[status], color: STATUS_COLORS[status] }}>
      {status.replace(/_/g, " ")}
    </span>
  );
}

export function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="kv-row">
      <span className="kv-label">{label}</span>
      <span className="kv-value">{children}</span>
    </div>
  );
}

export function materialById(
  library: RFMaterialLibrary | null,
  id: string | null | undefined,
): RFMaterial | null {
  if (!library || !id) return null;
  return library.materials.find((m) => m.id === id) ?? null;
}

export function rgbaToCss(rgba: Vec4 | null | undefined): string | null {
  if (!rgba) return null;
  const [r, g, b, a] = rgba;
  return `rgba(${Math.round(r * 255)}, ${Math.round(g * 255)}, ${Math.round(b * 255)}, ${a})`;
}

export function formatVec(v: readonly number[], digits = 2): string {
  return `(${v.map((x) => x.toFixed(digits)).join(", ")})`;
}

/** Material dropdown shared by the inspector, AI edit action, and editors. */
export function MaterialSelect({
  library,
  value,
  placeholder,
  onSelect,
  disabled = false,
}: {
  library: RFMaterialLibrary | null;
  value: string | null;
  placeholder: string;
  onSelect: (materialId: string) => void;
  disabled?: boolean;
}) {
  const current = materialById(library, value);
  return (
    <span className="mat-select">
      <Swatch color={current?.preview_color ?? "#3a4450"} />
      <select
        value={value ?? ""}
        disabled={disabled || !library}
        onChange={(e) => {
          if (e.target.value) onSelect(e.target.value);
        }}
      >
        <option value="" disabled>
          {placeholder}
        </option>
        {(library?.materials ?? []).map((m) => (
          <option key={m.id} value={m.id}>
            {m.display_name} ({m.id})
          </option>
        ))}
      </select>
    </span>
  );
}
