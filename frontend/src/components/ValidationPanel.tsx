import { useAppStore } from "../store/appStore";
import { SEVERITY_COLORS } from "./common";
import type { Severity, ValidationIssue } from "../types/api";

const SEVERITY_ORDER: Severity[] = ["error", "warning", "info"];

function IssueRow({ issue }: { issue: ValidationIssue }) {
  const selectPrim = useAppStore((s) => s.selectPrim);
  const selectDevice = useAppStore((s) => s.selectDevice);
  return (
    <div
      className="issue-row"
      style={{ borderLeft: `2px solid ${SEVERITY_COLORS[issue.severity]}` }}
      onClick={() => {
        if (issue.prim_id) selectPrim(issue.prim_id);
        else if (issue.device_id) selectDevice(issue.device_id);
      }}
      title={issue.prim_id ?? issue.device_id ?? undefined}
    >
      <span className="issue-code" style={{ color: SEVERITY_COLORS[issue.severity] }}>
        {issue.code}
      </span>
      <span>
        <span className="issue-msg">{issue.message}</span>
        {issue.prim_id && (
          <>
            {" "}
            <span className="issue-prim">{issue.prim_id}</span>
          </>
        )}
        {issue.device_id && (
          <>
            {" "}
            <span className="issue-prim">{issue.device_id}</span>
          </>
        )}
      </span>
    </div>
  );
}

export default function ValidationPanel() {
  const validation = useAppStore((s) => s.validation);
  const runValidation = useAppStore((s) => s.runValidation);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);

  return (
    <div className="panel">
      <h3 className="panel-title">Scene validation</h3>
      <button
        className="primary"
        disabled={!projectId || busy !== null}
        onClick={() => void runValidation()}
      >
        Run validation
      </button>

      {validation === null ? (
        <div className="empty-state">Run validation to check RF assignments and mesh refs.</div>
      ) : (
        <>
          <div className="chips">
            <span className="chip" style={{ borderColor: SEVERITY_COLORS.error, color: SEVERITY_COLORS.error }}>
              {validation.error_count} errors
            </span>
            <span className="chip" style={{ borderColor: SEVERITY_COLORS.warning, color: SEVERITY_COLORS.warning }}>
              {validation.warning_count} warnings
            </span>
            <span className="chip" style={{ borderColor: SEVERITY_COLORS.info, color: SEVERITY_COLORS.info }}>
              {validation.info_count} info
            </span>
            <span className="chip" style={{ color: validation.ok ? "#66bb6a" : "#ef5350" }}>
              {validation.ok ? "ok" : "blocked"}
            </span>
          </div>

          {validation.issues.length === 0 ? (
            <div className="empty-state">No issues — the scene is ready to compile.</div>
          ) : (
            SEVERITY_ORDER.map((severity) => {
              const group = validation.issues.filter((i) => i.severity === severity);
              if (group.length === 0) return null;
              return (
                <div key={severity} className="severity-group">
                  <h4 style={{ color: SEVERITY_COLORS[severity] }}>
                    {severity} ({group.length})
                  </h4>
                  {group.map((issue, i) => (
                    <IssueRow key={`${issue.code}_${i}`} issue={issue} />
                  ))}
                </div>
              );
            })
          )}
        </>
      )}
    </div>
  );
}
