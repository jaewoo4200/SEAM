import type { ReactNode } from "react";
import type {
  AssignmentStatus,
  RFMaterial,
  RFMaterialLibrary,
  Severity,
  Vec4,
} from "../types/api";

export const ACCENT = "#4fc3f7";

export const STATUS_COLORS: Record<AssignmentStatus, string> = {
  unassigned: "#ff9800",
  rule_suggested: "#ffd54f",
  ai_suggested: "#ffd54f",
  user_confirmed: "#66bb6a",
  measurement_calibrated: "#29b6f6",
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
