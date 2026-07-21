import { useEffect, useState } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { MaterialSelect, Swatch, materialById } from "./common";
import type {
  AssignmentRule,
  DisambiguationReport,
  GenerateRulesRequest,
  MaterialSuggestion,
  MeasurementSample,
  Vec3,
} from "../types/api";

// ------------------------------------------------------- RF disambiguation

/** One editable measurement row (RX x/y/z + measured path gain). */
interface MeasRow {
  x: string;
  y: string;
  z: string;
  gain: string;
}

const emptyRow = (): MeasRow => ({ x: "0", y: "0", z: "1.5", gain: "-80" });

/** Inline RF-sensing disambiguation form (Dai et al., JSTEAP 2025): re-simulate
 *  the measured links with each candidate material and rank the fit. Lives on a
 *  suggestion card and writes the winner back through the normal decision flow. */
function DisambiguateForm({ suggestion }: { suggestion: MaterialSuggestion }) {
  const projectId = useAppStore((s) => s.projectId);
  const materials = useAppStore((s) => s.materials);
  const setDecision = useAppStore((s) => s.setDecision);

  const [rows, setRows] = useState<MeasRow[]>([emptyRow()]);
  const [report, setReport] = useState<DisambiguationReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Extra candidates the user adds by hand (a suggestion with no alternatives
  // would otherwise leave a 1-item pool, and the API needs at least 2).
  const [extraIds, setExtraIds] = useState<string[]>([]);

  // Candidate pool: recommended first, then the alternatives, then any
  // user-added materials (deduped).
  const candidateIds = Array.from(
    new Set([
      suggestion.recommended_rf_material_id,
      ...suggestion.alternatives.map((a) => a.rf_material_id),
      ...extraIds,
    ]),
  );

  const setRow = (i: number, patch: Partial<MeasRow>) =>
    setRows((rs) => rs.map((r, j) => (j === i ? { ...r, ...patch } : r)));
  const addRow = () => setRows((rs) => (rs.length >= 5 ? rs : [...rs, emptyRow()]));
  const removeRow = (i: number) =>
    setRows((rs) => (rs.length <= 1 ? rs : rs.filter((_, j) => j !== i)));

  const run = async () => {
    if (!projectId || candidateIds.length < 2) return;
    const measurements: MeasurementSample[] = rows.map((r) => ({
      rx_position: [Number(r.x), Number(r.y), Number(r.z)] as Vec3,
      measured_path_gain_db: Number(r.gain),
    }));
    setBusy(true);
    setError(null);
    try {
      const rep = await api.disambiguate(projectId, {
        prim_ids: [suggestion.prim_id],
        candidate_material_ids: candidateIds,
        measurements,
      });
      setReport(rep);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const useBest = () => {
    const best = report?.best_material_id;
    if (!best) return;
    // Route through the same decision flow as a manual edit/approve so Apply
    // picks it up (recommended → approve, otherwise → edit).
    if (best === suggestion.recommended_rf_material_id) {
      setDecision(suggestion.prim_id, { prim_id: suggestion.prim_id, action: "approve" });
    } else {
      setDecision(suggestion.prim_id, {
        prim_id: suggestion.prim_id,
        action: "edit",
        rf_material_id: best,
      });
    }
  };

  return (
    <div className="disambig">
      <div className="disambig-head">
        RF disambiguation · {candidateIds.length} candidate(s)
      </div>
      <div className="disambig-cands">
        {candidateIds.map((id) => (
          <span key={id} className="cand-chip mono">
            {id}
            {extraIds.includes(id) && (
              <button
                className="row-del"
                title="Remove candidate"
                onClick={() => setExtraIds((xs) => xs.filter((x) => x !== id))}
              >
                ×
              </button>
            )}
          </span>
        ))}
        <select
          value=""
          onChange={(e) => {
            const id = e.target.value;
            if (id && !candidateIds.includes(id)) setExtraIds((xs) => [...xs, id]);
          }}
        >
          <option value="">+ candidate…</option>
          {(materials?.materials ?? [])
            .filter((m) => !candidateIds.includes(m.id))
            .map((m) => (
              <option key={m.id} value={m.id}>
                {m.display_name} ({m.id})
              </option>
            ))}
        </select>
      </div>
      {candidateIds.length < 2 && (
        <div className="hint">
          Add at least one more candidate to compare (this suggestion has no
          alternatives).
        </div>
      )}
      <table className="meas-table">
        <thead>
          <tr>
            <th>x</th>
            <th>y</th>
            <th>z</th>
            <th>gain dB</th>
            <th />
          </tr>
        </thead>
        <tbody>
          {rows.map((r, i) => (
            <tr key={i}>
              <td>
                <input value={r.x} onChange={(e) => setRow(i, { x: e.target.value })} />
              </td>
              <td>
                <input value={r.y} onChange={(e) => setRow(i, { y: e.target.value })} />
              </td>
              <td>
                <input value={r.z} onChange={(e) => setRow(i, { z: e.target.value })} />
              </td>
              <td>
                <input value={r.gain} onChange={(e) => setRow(i, { gain: e.target.value })} />
              </td>
              <td>
                <button
                  className="row-del"
                  title="Remove row"
                  disabled={rows.length <= 1}
                  onClick={() => removeRow(i)}
                >
                  ×
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <div className="disambig-actions">
        <button disabled={rows.length >= 5} onClick={addRow}>
          + row
        </button>
        <button
          className="primary"
          disabled={busy || candidateIds.length < 2}
          title={candidateIds.length < 2 ? "Needs at least 2 candidate materials" : ""}
          onClick={() => void run()}
        >
          {busy ? "Running…" : "Run RF disambiguation"}
        </button>
      </div>

      {error && <div className="disambig-error">{error}</div>}

      {report && (
        <div className="disambig-report">
          <div className="disambig-backend">
            backend <span className="mono">{report.backend}</span>
          </div>
          <table className="cand-table">
            <thead>
              <tr>
                <th>material</th>
                <th>RMSE dB</th>
                <th>links</th>
              </tr>
            </thead>
            <tbody>
              {report.candidates.map((c) => {
                const mat = materialById(materials, c.material_id);
                const isBest = c.material_id === report.best_material_id;
                return (
                  <tr key={c.material_id} className={isBest ? "cand-best" : ""}>
                    <td>
                      <Swatch color={mat?.preview_color ?? "#3a4450"} />
                      <span className="mono">{c.material_id}</span>
                      {isBest && <span className="best-tag">best</span>}
                    </td>
                    <td>{c.rmse_db === null || c.rmse_db === undefined ? "—" : c.rmse_db.toFixed(2)}</td>
                    <td>{c.n_links}</td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {report.warnings.length > 0 && (
            <ul className="disambig-warnings">
              {report.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}
          {report.best_material_id && (
            <button className="primary" onClick={useBest}>
              Use best ({report.best_material_id})
            </button>
          )}
        </div>
      )}
    </div>
  );
}

function SuggestionCard({
  suggestion,
  evidenceImage,
}: {
  suggestion: MaterialSuggestion;
  /** Project-relative path of the texture crop the VLM saw for this prim, if
   *  the last suggest response carried one. */
  evidenceImage: string | null;
}) {
  const materials = useAppStore((s) => s.materials);
  const decisions = useAppStore((s) => s.decisions);
  const setDecision = useAppStore((s) => s.setDecision);
  const selectPrim = useAppStore((s) => s.selectPrim);
  const projectId = useAppStore((s) => s.projectId);

  const [disambigOpen, setDisambigOpen] = useState(false);

  const decision = decisions[suggestion.prim_id];
  const recommended = materialById(materials, suggestion.recommended_rf_material_id);

  const toggle = (action: "approve" | "reject") => {
    if (decision?.action === action) {
      setDecision(suggestion.prim_id, null);
    } else {
      setDecision(suggestion.prim_id, { prim_id: suggestion.prim_id, action });
    }
  };

  const setEdit = (materialId: string) => {
    if (materialId === suggestion.recommended_rf_material_id) {
      setDecision(suggestion.prim_id, { prim_id: suggestion.prim_id, action: "approve" });
    } else {
      setDecision(suggestion.prim_id, {
        prim_id: suggestion.prim_id,
        action: "edit",
        rf_material_id: materialId,
      });
    }
  };

  return (
    <div className={"ai-card" + (decision ? ` decided-${decision.action}` : "")}>
      <div className="ai-card-head">
        <span className="prim-link" onClick={() => selectPrim(suggestion.prim_id)}>
          {suggestion.prim_id}
        </span>
        {/* The texture crop the VLM actually looked at (graceful no-op when the
            response carried none / the asset 404s). */}
        {projectId && evidenceImage && (
          <img
            className="evidence-thumb"
            src={api.assetUrl(projectId, evidenceImage)}
            alt=""
            title="Texture evidence the AI saw"
            onError={(e) => {
              (e.currentTarget as HTMLImageElement).style.display = "none";
            }}
          />
        )}
      </div>

      <div className="recommended">
        <Swatch color={recommended?.preview_color ?? "#3a4450"} />
        <strong>{recommended?.display_name ?? suggestion.recommended_rf_material_id}</strong>
        <span className="mono" style={{ color: "var(--muted)" }}>
          {suggestion.recommended_rf_material_id}
        </span>
      </div>

      <div className="conf-row">
        <span>confidence</span>
        <div className="conf-bar">
          <div style={{ width: `${Math.round(suggestion.confidence * 100)}%` }} />
        </div>
        <span>{(suggestion.confidence * 100).toFixed(0)}%</span>
      </div>

      {suggestion.evidence.length > 0 && (
        <ul className="evidence">
          {suggestion.evidence.map((e, i) => (
            <li key={i}>{e}</li>
          ))}
        </ul>
      )}

      {suggestion.alternatives.length > 0 && (
        <div className="alt-chips">
          {suggestion.alternatives.map((alt) => (
            <span
              key={alt.rf_material_id}
              className="alt-chip"
              title={`Choose ${alt.rf_material_id} instead (edit)`}
              onClick={() => setEdit(alt.rf_material_id)}
            >
              {alt.rf_material_id} · {(alt.confidence * 100).toFixed(0)}%
            </span>
          ))}
        </div>
      )}

      <div className="ai-card-actions">
        <button
          className={decision?.action === "approve" ? "on-approve" : ""}
          onClick={() => toggle("approve")}
        >
          {decision?.action === "approve" ? "✓ Approved" : "Approve"}
        </button>
        <button
          className={decision?.action === "reject" ? "on-reject" : ""}
          onClick={() => toggle("reject")}
        >
          {decision?.action === "reject" ? "✗ Rejected" : "Reject"}
        </button>
        <MaterialSelect
          library={materials}
          value={decision?.action === "edit" ? (decision.rf_material_id ?? null) : null}
          placeholder="Edit: pick other…"
          onSelect={setEdit}
        />
      </div>

      <button
        className={"disambig-toggle" + (disambigOpen ? " open" : "")}
        onClick={() => setDisambigOpen((o) => !o)}
      >
        {disambigOpen ? "▾ RF disambiguate" : "▸ RF disambiguate"}
      </button>
      {disambigOpen && <DisambiguateForm suggestion={suggestion} />}
    </div>
  );
}

// -------------------------------------------------- assignment-rule section

/** One editable rule row. Match terms are edited as a comma-separated string
 *  and split on read so the row stays a plain text field. */
function RuleRow({
  rule,
  onChange,
  onDelete,
}: {
  rule: AssignmentRule;
  onChange: (patch: Partial<AssignmentRule>) => void;
  onDelete: () => void;
}) {
  const materials = useAppStore((s) => s.materials);
  return (
    <div className="rule-row">
      <span className="rule-id mono">{rule.id}</span>
      <input
        className="rule-terms"
        value={rule.match_name_contains.join(", ")}
        placeholder="match terms (comma-separated)"
        title="Prim names containing any of these substrings match this rule"
        onChange={(e) =>
          onChange({
            match_name_contains: e.target.value
              .split(",")
              .map((t) => t.trim())
              .filter((t) => t.length > 0),
          })
        }
      />
      <MaterialSelect
        library={materials}
        value={rule.rf_material_id || null}
        placeholder="material…"
        onSelect={(id) => onChange({ rf_material_id: id })}
      />
      <button className="row-del" title="Delete rule" onClick={onDelete}>
        ×
      </button>
    </div>
  );
}

/** Natural-language → assignment-rules flow: describe the mapping, generate
 *  rules with the LLM, tweak them, then apply (which only *suggests* — the
 *  result flows into the normal review/Apply-decisions loop). */
function RuleGenerationSection() {
  const projectId = useAppStore((s) => s.projectId);
  const aiProvider = useAppStore((s) => s.aiProvider);
  const aiModel = useAppStore((s) => s.aiModel);

  const [instruction, setInstruction] = useState("");
  const [rules, setRules] = useState<AssignmentRule[] | null>(null);
  const [meta, setMeta] = useState<{ provider: string; model?: string | null } | null>(null);
  const [warnings, setWarnings] = useState<string[]>([]);
  const [genBusy, setGenBusy] = useState(false);
  const [applyBusy, setApplyBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const generate = async () => {
    if (!projectId || !instruction.trim()) return;
    setGenBusy(true);
    setError(null);
    try {
      const body: GenerateRulesRequest = {
        instruction: instruction.trim(),
        provider: aiProvider,
        // null = the provider default model (same convention as suggest).
        model: aiModel,
      };
      const resp = await api.aiGenerateRules(projectId, body);
      setRules(resp.rules);
      setMeta({ provider: resp.provider, model: resp.model });
      setWarnings(resp.warnings ?? []);
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setGenBusy(false);
    }
  };

  const apply = async () => {
    if (!projectId || !rules || rules.length === 0) return;
    setApplyBusy(true);
    setError(null);
    try {
      const resp = await api.aiApplyRules(projectId, { rules });
      // Hand off to the normal suggestion-review flow (resets any decisions);
      // nothing is committed to the scene until the user clicks Apply decisions.
      useAppStore.getState().setSuggestions(resp);
    } catch (err) {
      setError(errMessage(err));
    } finally {
      setApplyBusy(false);
    }
  };

  const patchRule = (i: number, patch: Partial<AssignmentRule>) =>
    setRules((rs) => (rs ? rs.map((r, j) => (j === i ? { ...r, ...patch } : r)) : rs));
  const deleteRule = (i: number) =>
    setRules((rs) => (rs ? rs.filter((_, j) => j !== i) : rs));

  const applicable = (rules ?? []).filter(
    (r) => r.match_name_contains.length > 0 && r.rf_material_id,
  ).length;

  return (
    <div className="rules-section">
      <h4>Assignment rules (natural language)</h4>
      <p className="hint">
        Describe how materials should map to prims by name; the LLM drafts
        rules you can edit before applying.
      </p>
      <textarea
        className="rules-instruction"
        rows={3}
        value={instruction}
        placeholder={
          "예: 벽(wall)은 콘크리트, 창문(window)은 유리로 배정해줘\n" +
          "e.g. assign concrete to walls and glass to windows/glazing"
        }
        onChange={(e) => setInstruction(e.target.value)}
      />
      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || genBusy || !instruction.trim()}
          onClick={() => void generate()}
        >
          {genBusy ? "Generating…" : "Generate rules"}
        </button>
      </div>

      {error && <div className="disambig-error">{error}</div>}

      {warnings.length > 0 && (
        <ul className="disambig-warnings">
          {warnings.map((w, i) => (
            <li key={i}>{w}</li>
          ))}
        </ul>
      )}

      {rules && (
        <>
          {meta && (
            <div className="results-meta">
              Provider: <span className="mono">{meta.provider}</span>
              {meta.model && (
                <>
                  {" "}
                  · model <span className="mono">{meta.model}</span>
                </>
              )}
            </div>
          )}

          {rules.length === 0 ? (
            <div className="empty-state">No rules generated for that instruction.</div>
          ) : (
            <div className="rule-list">
              {rules.map((r, i) => (
                <RuleRow
                  key={r.id}
                  rule={r}
                  onChange={(patch) => patchRule(i, patch)}
                  onDelete={() => deleteRule(i)}
                />
              ))}
            </div>
          )}

          <div className="panel-actions">
            <button
              className="primary"
              disabled={applyBusy || applicable === 0}
              title={
                applicable === 0
                  ? "Each rule needs at least one match term and a material"
                  : ""
              }
              onClick={() => void apply()}
            >
              {applyBusy ? "Applying…" : `Apply rules (${applicable})`}
            </button>
          </div>
          <p className="hint">
            Applying only <em>suggests</em> — the matches populate the review
            list above; nothing is committed until you click Apply decisions.
          </p>
        </>
      )}
    </div>
  );
}

/** Message from a thrown error (the shared client's ApiError carries a
 *  numeric `status` when callers need it). */
function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
}

// ------------------------------------------------- SEAM-Agent batch (multi)
//
// Run the agent over SEVERAL buildings sequentially (one live job per project
// is enforced server-side). Each settled building stays reviewable: "Review"
// re-opens its persisted trace on the prim's inspector card.

function AgentBatchSection() {
  const scene = useAppStore((s) => s.scene);
  const busy = useAppStore((s) => s.busy);
  const agentBatch = useAppStore((s) => s.agentBatch);
  const runAgentBatch = useAppStore((s) => s.runAgentBatch);
  const stopAgentBatch = useAppStore((s) => s.stopAgentBatch);
  const reviewAgentBatchItem = useAppStore((s) => s.reviewAgentBatchItem);
  const aiModel = useAppStore((s) => s.aiModel);

  const [open, setOpen] = useState(false);
  const [checked, setChecked] = useState<Record<string, boolean>>({});
  const [hint, setHint] = useState("");
  const [allowWeb, setAllowWeb] = useState(false);

  // Buildings = mesh-backed prims (same gate as the per-prim agent card).
  const candidates = (scene?.prims ?? []).filter((p) => p.mesh_ref?.mesh_name);
  const selectedIds = candidates.filter((p) => checked[p.id]).map((p) => p.id);
  const running = agentBatch?.running ?? false;

  return (
    <div className="rules-section">
      <button
        className={"seg-expander" + (open ? " open" : "")}
        onClick={() => setOpen((o) => !o)}
        title="Run SEAM-Agent over several buildings, one after another"
      >
        {open ? "▾" : "▸"} SEAM-Agent batch (multi-building)…
      </button>
      {open && (
        <>
          <p className="hint">
            Pick buildings, then the agent runs them <b>sequentially</b> (capture →
            analyze → propose). Review each result on its prim card afterwards.
          </p>
          <div className="agent-batch-picker">
            {candidates.length === 0 && (
              <div className="hint">No mesh-backed prims in this scene.</div>
            )}
            {candidates.map((p) => (
              <label key={p.id} className="solver-check">
                <input
                  type="checkbox"
                  checked={checked[p.id] ?? false}
                  disabled={running}
                  onChange={(e) =>
                    setChecked((c) => ({ ...c, [p.id]: e.target.checked }))
                  }
                />
                <span className="mono">{p.name}</span>
              </label>
            ))}
          </div>
          <label className="solver-field">
            <span className="solver-field-label">Hint</span>
            <input
              type="text"
              value={hint}
              disabled={running}
              placeholder="shared site hint, e.g. Hanyang University Seoul campus"
              onChange={(e) => setHint(e.target.value)}
            />
          </label>
          <label
            className="solver-check"
            title="Let the agent search the web for exterior photos as evidence"
          >
            <input
              type="checkbox"
              checked={allowWeb}
              disabled={running}
              onChange={(e) => setAllowWeb(e.target.checked)}
            />
            Allow web evidence
          </label>
          <div className="panel-actions">
            <button
              className="primary"
              disabled={busy !== null || running || selectedIds.length === 0}
              onClick={() =>
                void runAgentBatch(selectedIds, {
                  userHint: hint.trim() || null,
                  allowWeb,
                  model: aiModel,
                })
              }
            >
              Run {selectedIds.length} building(s) sequentially
            </button>
            {running && (
              <button className="on-reject" onClick={() => stopAgentBatch()}>
                ■ Stop batch
              </button>
            )}
          </div>
          {agentBatch && agentBatch.items.length > 0 && (
            <div className="agent-batch-list">
              {agentBatch.items.map((it) => (
                <div key={it.primId} className="agent-batch-item">
                  <span className={`agent-batch-status st-${it.status}`}>
                    {it.status === "needs_review" ? "needs review" : it.status}
                  </span>
                  <span className="mono" style={{ flex: 1 }} title={it.detail}>
                    {it.primId}
                  </span>
                  {typeof it.segments === "number" && (
                    <span className="hint">{it.segments} seg</span>
                  )}
                  {it.jobId && it.status !== "queued" && it.status !== "running" && (
                    <button
                      disabled={busy !== null}
                      onClick={() => void reviewAgentBatchItem(it.primId)}
                      title="Open this building's proposals on its prim card"
                    >
                      Review
                    </button>
                  )}
                </div>
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------- model picker
//
// Sits under the provider radios. It lets the user pin a specific model for the
// currently selected provider; "" = the provider default. Only rendered for a
// concrete, non-rule-based provider (rule_based / "auto" have nothing to pick).

function ModelPicker() {
  const aiProvider = useAppStore((s) => s.aiProvider);
  const aiModel = useAppStore((s) => s.aiModel);
  const setAiModel = useAppStore((s) => s.setAiModel);
  const aiModels = useAppStore((s) => s.aiModels);
  const aiModelsLoading = useAppStore((s) => s.aiModelsLoading);
  const projectId = useAppStore((s) => s.projectId);
  const loadAiModels = useAppStore((s) => s.loadAiModels);

  // (Re)load the model list on mount and whenever the provider changes: the
  // available set (and the stale-selection reset) depends on the provider.
  useEffect(() => {
    if (projectId) void loadAiModels(projectId);
  }, [projectId, aiProvider, loadAiModels]);

  // "auto" (null) and the rule-based provider have no model to choose.
  if (aiProvider === null || aiProvider === "rule_based") return null;

  const pm = aiModels.find((p) => p.provider === aiProvider) ?? null;
  const models = pm?.models ?? [];
  // Disabled when: loading, the provider is unreachable, or it lists no models.
  const empty = !aiModelsLoading && (pm === null || !pm.available || models.length === 0);
  const disabled = aiModelsLoading || empty;
  // The forced model no longer exists in the refreshed list (loadAiModels also
  // resets aiModel to null in the store; this note explains the reset to the
  // user for the window before/if it lingers).
  const stale = aiModel !== null && !models.some((m) => m.id === aiModel);
  const defaultLabel = pm?.default_model
    ? `(provider default: ${pm.default_model})`
    : "(provider default)";

  return (
    <label className="ai-model-row" title="Pick a specific model for the selected provider">
      <span className="ai-model-label">Model</span>
      <select
        className="ai-model-select"
        value={aiModel ?? ""}
        disabled={disabled}
        title={
          empty
            ? "start LM Studio / Ollama to pick a model"
            : "Pick a specific model for the selected provider"
        }
        onChange={(e) => setAiModel(e.target.value === "" ? null : e.target.value)}
      >
        <option value="">{aiModelsLoading ? "Loading models…" : defaultLabel}</option>
        {models.map((m) => (
          <option key={m.id} value={m.id}>
            {m.label}
            {m.is_default ? " · default" : ""}
          </option>
        ))}
      </select>
      {stale && (
        <span className="hint ai-model-stale">
          Previous model is no longer available — reset to the provider default.
        </span>
      )}
    </label>
  );
}

export default function AISuggestionPanel() {
  const health = useAppStore((s) => s.health);
  const aiStatuses = useAppStore((s) => s.aiStatuses);
  const selection = useAppStore((s) => s.selection);
  const suggestions = useAppStore((s) => s.suggestions);
  const decisions = useAppStore((s) => s.decisions);
  const suggestMaterials = useAppStore((s) => s.suggestMaterials);
  const sendScreenshot = useAppStore((s) => s.sendScreenshot);
  const setSendScreenshot = useAppStore((s) => s.setSendScreenshot);
  const sendTextureCrops = useAppStore((s) => s.sendTextureCrops);
  const setSendTextureCrops = useAppStore((s) => s.setSendTextureCrops);
  const aiProvider = useAppStore((s) => s.aiProvider);
  const setAiProvider = useAppStore((s) => s.setAiProvider);
  const applyDecisions = useAppStore((s) => s.applyDecisions);
  const lastDecisionApply = useAppStore((s) => s.lastDecisionApply);
  const revertDecisions = useAppStore((s) => s.revertDecisions);
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);

  const providers = aiStatuses.length > 0 ? aiStatuses : (health?.ai_providers ?? []);
  const decisionCount = Object.keys(decisions).length;

  // prim_id -> texture-crop asset path the VLM saw (empty when the response
  // carried no evidence images / a non-vision provider answered).
  const evidenceByPrim = new Map(
    (suggestions?.evidence_images ?? []).map((ev) => [ev.prim_id, ev.asset_path]),
  );

  return (
    <div className="panel">
      <h3 className="panel-title">AI material assist</h3>

      <h4>Providers</h4>
      {providers.length === 0 && <div className="hint">No provider status available.</div>}
      <label className="provider-row provider-pickable">
        <input
          type="radio"
          name="ai-provider"
          checked={aiProvider === null}
          onChange={() => setAiProvider(null)}
        />
        <span className="provider-name">auto (best available)</span>
      </label>
      {providers.map((p) => (
        <label
          key={p.name}
          className={"provider-row provider-pickable" + (p.available ? "" : " provider-off")}
          title={p.available ? "Use this provider for suggestions" : p.detail || "unavailable"}
        >
          <input
            type="radio"
            name="ai-provider"
            disabled={!p.available}
            checked={aiProvider === p.name}
            onChange={() => setAiProvider(p.name)}
          />
          <span className="dot" style={{ background: p.available ? "var(--ok)" : "var(--off)" }} />
          <span className="provider-name">{p.name}</span>
          {p.model && <span className="mono">{p.model}</span>}
          {p.detail && <span title={p.detail}>· {p.detail}</span>}
        </label>
      ))}

      {/* Model picker for the selected provider (hidden for auto / rule_based). */}
      <ModelPicker />

      {/* The vision opt-in lives HERE, next to the button that uses it (it
          also exists in Simulation > LIVE & AI; same store flag). */}
      <label className="solver-check">
        <input
          type="checkbox"
          checked={sendScreenshot}
          onChange={(e) => setSendScreenshot(e.target.checked)}
        />
        Attach viewport screenshot
        <span className="hint" style={{ marginLeft: 6 }}>
          vision-capable providers see the 3D view from 4 sides (may switch to the vision model)
        </span>
      </label>
      <label className="solver-check">
        <input
          type="checkbox"
          checked={sendTextureCrops}
          onChange={(e) => setSendTextureCrops(e.target.checked)}
        />
        Attach per-prim texture crops
        <span className="hint" style={{ marginLeft: 6 }}>
          close-ups of each prim's baseColor texture from the GLB (textured scenes only)
        </span>
      </label>
      <div className="panel-actions">
        <button
          className="primary"
          disabled={!projectId || busy !== null}
          onClick={() => void suggestMaterials()}
        >
          Suggest RF materials{" "}
          {selection.length > 0 ? `(${selection.length} selected)` : "(all unassigned)"}
        </button>
      </div>
      <p className="hint">
        Suggestions are evidence only — nothing is applied until you approve and click Apply.
      </p>

      <RuleGenerationSection />

      <AgentBatchSection />

      {suggestions && (
        <>
          <div className="results-meta">
            Provider: <span className="mono">{suggestions.provider}</span>
            {suggestions.model && (
              <>
                {" "}
                · model <span className="mono">{suggestions.model}</span>
              </>
            )}
            {suggestions.prompt_version && (
              <>
                {" "}
                · prompt <span className="mono">{suggestions.prompt_version}</span>
              </>
            )}
          </div>

          {suggestions.warnings.length > 0 && (
            <div className="ai-note">
              {suggestions.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}

          {suggestions.suggestions.length === 0 ? (
            <div className="empty-state">No suggestions returned for the requested prims.</div>
          ) : (
            suggestions.suggestions.map((s) => (
              <SuggestionCard
                key={s.prim_id}
                suggestion={s}
                evidenceImage={evidenceByPrim.get(s.prim_id) ?? null}
              />
            ))
          )}

          <div className="ai-footer">
            <button
              className="primary"
              disabled={decisionCount === 0 || busy !== null}
              onClick={() => void applyDecisions()}
            >
              Apply decisions ({decisionCount})
            </button>
          </div>
        </>
      )}

      {/* One-click revert of the last applied decisions (previous RF bindings
          were snapshotted at apply time). */}
      {lastDecisionApply && !suggestions && (
        <div className="seg-applied">
          <div className="seg-applied-note">
            ✓ Applied {lastDecisionApply.items.length} suggestion decision(s).
          </div>
          <button
            className="on-reject"
            disabled={busy !== null}
            onClick={() => void revertDecisions()}
            title="Restore every affected prim to the RF binding it had before the apply"
          >
            ↩ Revert
          </button>
        </div>
      )}
      {/* Assignment-impact (NMSE vs single-material baseline) UI removed after
          verification feedback — the research API stays at
          POST /rf/materials/assignment-impact; ImpactSection is unmounted. */}
    </div>
  );
}
