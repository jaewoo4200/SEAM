import { useEffect, useMemo, useRef, useState } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { materialById } from "./common";
import type { AgentEvidence, AgentSegment, Prim } from "../types/api";

// SEAM-Agent: agentic RF-material authoring for one prim. The panel captures
// multi-view RGB + triangle-id buffers of the prim's mesh (from the live 3D
// viewer), POSTs them to the backend agent, and streams a LIVE ACTIVITY TRACE
// (steps + evidence + proposed segments) while the agent segments the surface,
// gathers web/image evidence, and proposes a material per segment. Accepting
// segments bakes them into the visual GLB as per-material sub-prims (same
// backup/undo as the "Split by material" flow). Lives on the selected prim's
// inspector card, gated on prim.mesh_ref.

/** Default-check a segment when the agent is at least moderately confident. */
const APPLY_CONFIDENCE_THRESHOLD = 0.55;

/** Step-status glyphs for the activity trace. */
const STEP_ICON: Record<"running" | "done" | "error", string> = {
  running: "◐",
  done: "✓",
  error: "✕",
};

/** Human labels for the pipeline stages reported in trace.progress. */
const STAGE_LABEL: Record<string, string> = {
  inspect: "inspecting mesh",
  decode: "decoding views",
  retrieve: "gathering web evidence",
  views: "analyzing views",
  refine: "refining low-confidence regions",
  aggregate: "aggregating votes",
  segments: "building segments",
};

function StepIcon({ status }: { status: "running" | "done" | "error" }) {
  return (
    <span className={"agent-step-icon agent-step-" + status} aria-hidden>
      {STEP_ICON[status]}
    </span>
  );
}

/** One evidence card: thumbnail (if any) + claim + source link. */
function EvidenceCard({ projectId, ev }: { projectId: string; ev: AgentEvidence }) {
  const link = ev.source_url ?? ev.page_url ?? null;
  return (
    <div className="agent-evidence-card">
      {ev.thumb_asset_path && (
        <img
          className="agent-evidence-thumb"
          src={api.assetUrl(projectId, ev.thumb_asset_path)}
          alt={ev.type}
          loading="lazy"
        />
      )}
      <div className="agent-evidence-body">
        <div className="agent-evidence-type">{ev.type}</div>
        <div className="agent-evidence-claim">{ev.claim}</div>
        {link && (
          <a
            className="agent-evidence-link"
            href={link}
            target="_blank"
            rel="noreferrer"
            title={link}
          >
            source ↗
          </a>
        )}
      </div>
    </div>
  );
}

export default function SeamAgentPanel({ prim }: { prim: Prim }) {
  const projectId = useAppStore((s) => s.projectId);
  const materials = useAppStore((s) => s.materials);
  const busy = useAppStore((s) => s.busy);
  const agentJob = useAppStore((s) => s.agentJob);
  const agentTrace = useAppStore((s) => s.agentTrace);
  const lastSegApply = useAppStore((s) => s.lastSegApply);
  const startAgentJob = useAppStore((s) => s.startAgentJob);
  const applyAgentSegments = useAppStore((s) => s.applyAgentSegments);
  const clearAgentJob = useAppStore((s) => s.clearAgentJob);
  const cancelAgentJob = useAppStore((s) => s.cancelAgentJob);
  const undoSeg = useAppStore((s) => s.undoSegmentation);

  const [open, setOpen] = useState(false);
  const [userHint, setUserHint] = useState("");
  const [allowWeb, setAllowWeb] = useState(true);
  const [model, setModel] = useState("");
  // Per-segment accept toggles (segment_id → checked). Seeded when segments
  // first arrive (confidence >= threshold), user can override.
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [seededFor, setSeededFor] = useState<string | null>(null);
  // Wall-clock start of the active job for the elapsed-time readout.
  const [startedAt, setStartedAt] = useState<number | null>(null);
  const [elapsed, setElapsed] = useState(0);

  const traceRef = useRef<HTMLDivElement>(null);
  // Stick-to-bottom auto-scroll: follow new steps unless the user scrolled up
  // to read something (resumes once they return near the bottom).
  const stickRef = useRef(true);

  const disabled = busy !== null;
  // A job/trace belongs to a specific prim; only surface it on that prim's card.
  const jobHere = agentJob !== null && agentJob.primId === prim.id;
  // A live/settled job for THIS prim auto-opens the card: batch "Review"
  // (which selects the prim from another tab) and mid-run tab switches land
  // on a visible trace instead of a collapsed expander.
  useEffect(() => {
    if (jobHere) setOpen(true);
  }, [jobHere]);
  const traceHere = jobHere ? agentTrace : null;
  const meshName = prim.mesh_ref?.mesh_name ?? null;
  const status = traceHere?.status ?? (jobHere ? "running" : null);
  const running = status === "running";
  const settled = status === "done" || status === "needs_review";
  const applyHere = lastSegApply !== null && lastSegApply.primId === prim.id;

  // Elapsed-time ticker. The backend's progress.elapsed_sec is authoritative:
  // reseeding startedAt from it keeps the counter correct across tab switches
  // (this component unmounts with the mode panel, so a purely local start
  // time would reset to zero on every return — reported bug).
  const backendElapsed = traceHere?.progress?.elapsed_sec ?? null;
  useEffect(() => {
    if (jobHere && running) {
      if (backendElapsed !== null) {
        setStartedAt(Date.now() - backendElapsed * 1000);
      } else if (startedAt === null) {
        setStartedAt(Date.now());
      }
    } else if (!jobHere) {
      setStartedAt(null);
      setElapsed(0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- startedAt is
    // intentionally omitted: the backend reseed must win over the local seed.
  }, [jobHere, running, backendElapsed]);

  useEffect(() => {
    if (startedAt === null || !running) return;
    const t = setInterval(() => setElapsed(Date.now() - startedAt), 500);
    return () => clearInterval(t);
  }, [startedAt, running]);

  // Auto-scroll the activity trace to the latest step as steps stream in
  // (only while the user hasn't scrolled up to read earlier steps).
  const stepCount = traceHere?.steps.length ?? 0;
  useEffect(() => {
    const el = traceRef.current;
    if (el && stickRef.current) el.scrollTop = el.scrollHeight;
  }, [stepCount, traceHere?.steps]);

  // Seed the accept checkboxes once per job's segment set (default-check by
  // confidence). Keyed on the job id so a new job re-seeds.
  const segments: AgentSegment[] = useMemo(() => traceHere?.segments ?? [], [traceHere]);
  useEffect(() => {
    if (!jobHere || !agentJob || segments.length === 0) return;
    if (seededFor === agentJob.jobId) return;
    const next: Record<string, boolean> = {};
    for (const seg of segments) next[seg.segment_id] = seg.confidence >= APPLY_CONFIDENCE_THRESHOLD;
    setChecked(next);
    setSeededFor(agentJob.jobId);
  }, [jobHere, agentJob, segments, seededFor]);

  if (!meshName) return null;

  const evidenceById = new Map((traceHere?.evidence ?? []).map((e) => [e.evidence_id, e]));

  const runDisabled = disabled || !projectId || running;

  const doRun = () => {
    if (!meshName) return;
    void startAgentJob(prim.id, {
      meshName,
      userHint: userHint.trim() || null,
      allowWeb,
      model: model.trim() || null,
    });
    // A fresh run re-seeds the accept checkboxes.
    setSeededFor(null);
    setChecked({});
  };

  const selectedIds = segments.filter((s) => checked[s.segment_id]).map((s) => s.segment_id);

  return (
    <div className="seg-section agent-section" style={{ marginTop: 12 }}>
      <button
        className={"seg-expander" + (open ? " open" : "")}
        onClick={() => setOpen((o) => !o)}
        title="AI agent that segments this mesh and proposes an RF material per region with web evidence"
      >
        {open ? "▾" : "▸"} SEAM-Agent (AI material authoring)…
      </button>

      {open && (
        <div className="seg-body">
          {/* Inputs */}
          <label className="solver-field">
            <span className="solver-field-label">Hint</span>
            <input
              type="text"
              value={userHint}
              disabled={disabled || running}
              placeholder="e.g. 한양대학교 퓨전테크센터 (FTC) building"
              onChange={(e) => setUserHint(e.target.value)}
            />
          </label>
          <label className="solver-check" title="Let the agent search the web for datasheets / imagery as evidence">
            <input
              type="checkbox"
              checked={allowWeb}
              disabled={disabled || running}
              onChange={(e) => setAllowWeb(e.target.checked)}
            />
            Allow web evidence
          </label>
          <label className="solver-field">
            <span className="solver-field-label">Model</span>
            <input
              type="text"
              value={model}
              disabled={disabled || running}
              placeholder="provider default"
              onChange={(e) => setModel(e.target.value)}
            />
          </label>

          <div className="panel-actions">
            <button className="primary" disabled={runDisabled} onClick={doRun}>
              {running ? "Running…" : "Run agent"}
            </button>
            {jobHere && running && (
              <button
                className="on-reject"
                disabled={disabled}
                onClick={() => void cancelAgentJob()}
                title="Ask the agent to stop at the next step (the trace settles as cancelled)"
              >
                ■ Stop
              </button>
            )}
            {jobHere && !running && (
              <button disabled={disabled} onClick={() => clearAgentJob()}>
                Clear
              </button>
            )}
          </div>
          <p className="hint">
            Captures 6 views (RGB + triangle-id) of{" "}
            <span className="mono">{meshName}</span> and sends them to the agent. Applying
            bakes the accepted segments into the visual GLB (backup kept for undo).
          </p>

          {/* Live activity trace */}
          {jobHere && (
            <div className="agent-trace-wrap">
              <div className="agent-trace-head">
                <span className={"agent-status agent-status-" + (status ?? "running")}>
                  {status === "needs_review"
                    ? "needs review"
                    : status ?? "running"}
                </span>
                {running && (
                  <span className="agent-elapsed">{(elapsed / 1000).toFixed(1)}s</span>
                )}
              </div>

              {/* Live progress: stage + measured ETA from the backend. */}
              {running && traceHere?.progress && (
                <div className="agent-progress">
                  <div className="agent-progress-bar">
                    <span
                      style={{
                        width: `${(() => {
                          const p = traceHere.progress!;
                          const total = p.total_stages || 7;
                          const idx = Math.max((p.stage_index || 1) - 1, 0);
                          const inStage =
                            p.stage === "views" && (p.views_total ?? 0) > 0
                              ? (p.views_done ?? 0) / (p.views_total ?? 1)
                              : 0;
                          return Math.min(100, Math.round(((idx + inStage) / total) * 100));
                        })()}%`,
                      }}
                    />
                  </div>
                  <span className="agent-progress-label">
                    {STAGE_LABEL[traceHere.progress.stage] ?? traceHere.progress.stage}
                    {traceHere.progress.stage === "views" &&
                      (traceHere.progress.views_total ?? 0) > 0 &&
                      ` ${traceHere.progress.views_done ?? 0}/${traceHere.progress.views_total}`}
                    {traceHere.progress.eta_sec != null &&
                      traceHere.progress.eta_sec > 0 &&
                      ` · ~${Math.ceil(traceHere.progress.eta_sec)}s left`}
                  </span>
                </div>
              )}

              {traceHere && traceHere.steps.length > 0 && (
                <div
                  className="agent-trace"
                  ref={traceRef}
                  onScroll={(e) => {
                    const el = e.currentTarget;
                    stickRef.current =
                      el.scrollHeight - el.scrollTop - el.clientHeight < 24;
                  }}
                >
                  {traceHere.steps.map((step) => (
                    <div key={step.step_id} className="agent-step">
                      <StepIcon status={step.status} />
                      <div className="agent-step-body">
                        <div className="agent-step-summary">{step.summary}</div>
                        {step.queries && step.queries.length > 0 && (
                          <div className="agent-chips">
                            {step.queries.map((q, i) => (
                              <span key={i} className="agent-chip" title={q}>
                                {q}
                              </span>
                            ))}
                          </div>
                        )}
                      </div>
                    </div>
                  ))}
                </div>
              )}

              {running && (!traceHere || traceHere.steps.length === 0) && (
                <p className="hint">Waiting for the agent to report activity…</p>
              )}

              {/* The captured renders the VLM reasoned over (click to open). */}
              {projectId && traceHere && (traceHere.views?.length ?? 0) > 0 && (
                <div className="agent-evidence">
                  <div className="agent-subhead">Views the agent saw</div>
                  <div className="agent-views-strip">
                    {traceHere.views!.map((v) => (
                      <img
                        key={v.view_id}
                        className="agent-view-thumb"
                        src={api.assetUrl(projectId, v.asset_path)}
                        alt={`captured view ${v.view_id}`}
                        loading="lazy"
                        onClick={() =>
                          window.open(
                            api.assetUrl(projectId, v.asset_path),
                            "_blank",
                            "noreferrer",
                          )
                        }
                      />
                    ))}
                  </div>
                </div>
              )}

              {/* Evidence cards */}
              {projectId && traceHere && traceHere.evidence.length > 0 && (
                <div className="agent-evidence">
                  <div className="agent-subhead">Evidence</div>
                  <div className="agent-evidence-list">
                    {traceHere.evidence.map((ev) => (
                      <EvidenceCard key={ev.evidence_id} projectId={projectId} ev={ev} />
                    ))}
                  </div>
                </div>
              )}

              {/* Error */}
              {status === "error" && (
                <div className="agent-error">
                  <div className="field-error">
                    {traceHere?.detail || "The agent job failed."}
                  </div>
                  <button className="primary" disabled={runDisabled} onClick={doRun}>
                    Retry
                  </button>
                </div>
              )}

              {/* Segments table (done / needs_review) */}
              {settled && segments.length > 0 && (
                <div className="agent-segments">
                  <div className="agent-subhead">
                    Proposed materials ({segments.length})
                    {status === "needs_review" && (
                      <span className="agent-review-tag"> · review before applying</span>
                    )}
                  </div>
                  <table className="seg-table agent-seg-table">
                    <thead>
                      <tr>
                        <th />
                        <th>segment</th>
                        <th>faces</th>
                        <th>RF material</th>
                        <th>conf.</th>
                      </tr>
                    </thead>
                    <tbody>
                      {segments.map((seg) => {
                        const rfMat = materialById(materials, seg.rf_material_id);
                        const altTitle =
                          seg.alternatives && seg.alternatives.length > 0
                            ? "alternatives: " +
                              seg.alternatives
                                .map(
                                  (a) =>
                                    `${a.rf_material_id} (${(a.confidence * 100).toFixed(0)}%)`,
                                )
                                .join(", ")
                            : undefined;
                        const evThumbs = seg.evidence_ids
                          .map((id) => evidenceById.get(id))
                          .filter((e): e is AgentEvidence => Boolean(e && e.thumb_asset_path));
                        return (
                          <tr key={seg.segment_id}>
                            <td>
                              <input
                                type="checkbox"
                                checked={checked[seg.segment_id] ?? false}
                                disabled={disabled}
                                onChange={(e) =>
                                  setChecked((c) => ({
                                    ...c,
                                    [seg.segment_id]: e.target.checked,
                                  }))
                                }
                              />
                            </td>
                            <td title={altTitle}>
                              <span className="agent-seg-label">
                                {/* "Show me the faces you mean": tinted render
                                    crop of exactly this segment's coverage. */}
                                {seg.preview_asset_path && projectId && (
                                  <img
                                    className="agent-seg-preview"
                                    src={api.assetUrl(projectId, seg.preview_asset_path)}
                                    alt={`${seg.semantic_label} coverage preview`}
                                    loading="lazy"
                                    title="Click to open the full preview"
                                    onClick={() =>
                                      window.open(
                                        api.assetUrl(projectId, seg.preview_asset_path!),
                                        "_blank",
                                        "noreferrer",
                                      )
                                    }
                                  />
                                )}
                                {seg.semantic_label}
                              </span>
                              {evThumbs.length > 0 && projectId && (
                                <span className="agent-seg-thumbs">
                                  {evThumbs.slice(0, 3).map((e) => (
                                    <img
                                      key={e.evidence_id}
                                      className="agent-seg-thumb"
                                      src={api.assetUrl(projectId, e.thumb_asset_path!)}
                                      alt=""
                                      loading="lazy"
                                    />
                                  ))}
                                </span>
                              )}
                            </td>
                            <td>{seg.face_count.toLocaleString()}</td>
                            <td>
                              <span
                                className="mono"
                                title={rfMat?.display_name ?? seg.rf_material_id}
                              >
                                {seg.rf_material_id}
                              </span>
                            </td>
                            <td>
                              <span className="agent-conf">
                                <span className="agent-conf-bar">
                                  <span
                                    style={{
                                      width: `${Math.round(seg.confidence * 100)}%`,
                                    }}
                                  />
                                </span>
                                <span className="agent-conf-num">
                                  {(seg.confidence * 100).toFixed(0)}%
                                </span>
                              </span>
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                  <div className="panel-actions">
                    <button
                      className="primary"
                      disabled={disabled || selectedIds.length === 0}
                      onClick={() => void applyAgentSegments(selectedIds)}
                    >
                      Apply selected ({selectedIds.length})
                    </button>
                    <button disabled={disabled} onClick={() => clearAgentJob()}>
                      Discard
                    </button>
                  </div>
                </div>
              )}

              {settled && segments.length === 0 && (
                <p className="hint">The agent returned no material segments.</p>
              )}

              {status === "cancelled" && (
                <p className="hint">
                  Stopped by request — nothing was applied. Run again anytime.
                </p>
              )}
            </div>
          )}

          {/* Applied result + undo (survives re-render via lastSegApply). */}
          {applyHere && lastSegApply && !jobHere && (
            <div className="seg-applied">
              <div className="seg-applied-note">
                ✓ Applied — split into {lastSegApply.addedPrimIds.length} prim(s):{" "}
                {lastSegApply.addedPrimIds.map((id) => (
                  <span key={id} className="mono seg-applied-prim">
                    {id}
                  </span>
                ))}
              </div>
              <button
                className="on-reject"
                disabled={disabled}
                onClick={() => void undoSeg(lastSegApply.batchId)}
              >
                Undo
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
