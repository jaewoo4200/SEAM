import { useState } from "react";
import { useAppStore } from "../store/appStore";
import { SEVERITY_COLORS } from "./common";
import type { Severity, ValidationIssue } from "../types/api";

const SEVERITY_ORDER: Severity[] = ["error", "warning", "info"];

// The AI explain-validation route is not (yet) on the shared `api` client
// (api/client.ts is owned by a sibling). Call it directly with the same
// relative-"/api" base the client uses so the dev/prod proxy routes it.
const API_BASE = "/api";

interface ExplainValidationResponse {
  explanation: string;
  provider: string;
  model?: string | null;
  warnings: string[];
}

/** Suggested next actions the backend attaches to an issue. Read defensively:
 *  `ValidationIssue.suggested_actions` lands via a sibling contract this wave
 *  and may not be on the pinned type yet. */
function issueActions(issue: ValidationIssue): string[] {
  const a = (issue as { suggested_actions?: string[] | null }).suggested_actions;
  return Array.isArray(a) ? a : [];
}

function IssueRow({ issue }: { issue: ValidationIssue }) {
  const selectPrim = useAppStore((s) => s.selectPrim);
  const selectDevice = useAppStore((s) => s.selectDevice);
  const actions = issueActions(issue);
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
        {actions.length > 0 && (
          <ul className="issue-actions">
            {actions.map((a, i) => (
              <li key={i}>{a}</li>
            ))}
          </ul>
        )}
      </span>
    </div>
  );
}

/** Panel-level "explain the whole report in prose" affordance. */
function ExplainWithAI() {
  const projectId = useAppStore((s) => s.projectId);
  const [explanation, setExplanation] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const explain = async () => {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    try {
      const res = await fetch(`${API_BASE}/projects/${projectId}/ai/explain-validation`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (!res.ok) {
        if (res.status === 409) {
          throw new Error(
            "No local LLM available — start a provider (e.g. Ollama) to explain validation.",
          );
        }
        let detail = `${res.status} ${res.statusText}`;
        try {
          const data = (await res.json()) as { detail?: unknown };
          if (typeof data.detail === "string") detail = data.detail;
        } catch {
          /* non-JSON */
        }
        throw new Error(detail);
      }
      const data = (await res.json()) as ExplainValidationResponse;
      setExplanation(data.explanation);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="validation-explain">
      <button disabled={!projectId || busy} onClick={() => void explain()}>
        {busy ? "Explaining…" : "Explain with AI"}
      </button>
      {error && <div className="disambig-error">{error}</div>}
      {explanation && <div className="explain-block">{explanation}</div>}
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

          <ExplainWithAI />

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
