import { useState } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { SEVERITY_COLORS } from "./common";
import type { Severity, ValidationIssue } from "../types/api";

const SEVERITY_ORDER: Severity[] = ["error", "warning", "info"];

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
      const data = await api.aiExplainValidation(projectId);
      setExplanation(data.explanation);
    } catch (err) {
      const status = (err as { status?: number }).status;
      setError(
        status === 409
          ? "No local LLM available — start a provider (e.g. Ollama) to explain validation."
          : err instanceof Error
            ? err.message
            : String(err),
      );
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

/** Collapsible message list: shows the first `limit` items, then a toggle to
 *  reveal the rest (mirrors the pattern the audit asked for — collapsed beyond
 *  ~5). Used for the compile warnings/errors. */
function MessageList({
  messages,
  color,
  limit = 5,
}: {
  messages: string[];
  color: string;
  limit?: number;
}) {
  const [expanded, setExpanded] = useState(false);
  if (messages.length === 0) return null;
  const shown = expanded ? messages : messages.slice(0, limit);
  const hidden = messages.length - shown.length;
  return (
    <>
      <ul className="compile-msg-list" style={{ borderLeft: `2px solid ${color}` }}>
        {shown.map((m, i) => (
          <li key={i}>{m}</li>
        ))}
      </ul>
      {messages.length > limit && (
        <button
          className="compile-more"
          onClick={() => setExpanded((v) => !v)}
        >
          {expanded ? "Show fewer" : `Show ${hidden} more`}
        </button>
      )}
    </>
  );
}

/** "Last RF compile" summary: the outcome of the most recent Compile RF run
 *  (store.compileResult). Persists until the next compile; the dismiss button
 *  clears it. */
function LastCompileResult() {
  const compileResult = useAppStore((s) => s.compileResult);
  const clearCompileResult = useAppStore((s) => s.clearCompileResult);
  if (!compileResult) return null;

  const { ok, generated_files, material_groups, warnings, errors } = compileResult;
  return (
    <div className="compile-summary">
      <div className="compile-summary-head">
        <h4 className="compile-summary-title">Last RF compile</h4>
        <button
          className="compile-dismiss"
          title="Dismiss this compile summary"
          onClick={() => clearCompileResult()}
        >
          ×
        </button>
      </div>
      <div className="chips">
        <span className="chip" style={{ color: ok ? "var(--ok)" : "var(--err)" }}>
          {ok ? "compiled" : `${errors.length} error${errors.length === 1 ? "" : "s"}`}
        </span>
        <span className="chip">
          {material_groups.length} material group{material_groups.length === 1 ? "" : "s"}
        </span>
        {warnings.length > 0 && (
          <span className="chip" style={{ color: SEVERITY_COLORS.warning }}>
            {warnings.length} warning{warnings.length === 1 ? "" : "s"}
          </span>
        )}
      </div>

      {generated_files.length > 0 ? (
        <ul className="compile-files">
          {generated_files.map((f) => (
            <li key={f} className="mono">
              {f}
            </li>
          ))}
        </ul>
      ) : (
        <div className="hint">No files generated.</div>
      )}

      {errors.length > 0 && (
        <div className="compile-msg-group">
          <span className="compile-msg-label" style={{ color: SEVERITY_COLORS.error }}>
            errors
          </span>
          <MessageList messages={errors} color={SEVERITY_COLORS.error} />
        </div>
      )}
      {warnings.length > 0 && (
        <div className="compile-msg-group">
          <span className="compile-msg-label" style={{ color: SEVERITY_COLORS.warning }}>
            warnings
          </span>
          <MessageList messages={warnings} color={SEVERITY_COLORS.warning} />
        </div>
      )}
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

      <LastCompileResult />
      <button
        className="primary"
        disabled={!projectId || busy !== null}
        onClick={() => void runValidation()}
      >
        Run validation
      </button>

      {validation === null ? (
        <div className="empty-state">Run validation to check RF assignments and mesh references.</div>
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
            <span className="chip" style={{ color: validation.ok ? "var(--ok)" : "var(--err)" }}>
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
