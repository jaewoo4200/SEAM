import { useState } from "react";
import { useAppStore } from "../store/appStore";
import { api } from "../api/client";
import { MaterialSelect, Swatch, materialById } from "./common";
import { LineChart } from "../charts";
import type {
  DisambiguationReport,
  MaterialImpactReport,
  MaterialSuggestion,
  MaterialSuggestionResponse,
  MeasurementSample,
  Vec3,
} from "../types/api";

// ------------------------------------------------------- ai fetch helper
//
// The three AI-rule endpoints are not (yet) on the shared `api` client
// (api/client.ts is owned by a sibling). Until they land there we call them
// directly, mirroring the client's base-URL resolution: every URL is the
// relative "/api/..." path so the Vite dev proxy / prod reverse proxy routes
// it to FastAPI without CORS (see api/client.ts `const BASE = "/api"`).

const API_BASE = "/api";

/** POST JSON and parse JSON, surfacing the backend `detail` on non-2xx.
 *  Kept local (not on the shared client) — see note above. */
async function postJson<T>(path: string, body: unknown): Promise<T> {
  let res: Response;
  try {
    res = await fetch(API_BASE + path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
  } catch (err) {
    const e = new Error(
      `backend unreachable: ${err instanceof Error ? err.message : String(err)}`,
    ) as Error & { status: number };
    e.status = 0;
    throw e;
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const data: unknown = await res.json();
      if (data !== null && typeof data === "object" && "detail" in data) {
        const d = (data as { detail: unknown }).detail;
        if (typeof d === "string") detail = d;
        else if (d !== null && typeof d === "object" && "message" in d) {
          detail = String((d as { message: unknown }).message);
        } else detail = JSON.stringify(d);
      }
    } catch {
      /* non-JSON error body; keep the status text */
    }
    const e = new Error(detail) as Error & { status: number };
    e.status = res.status;
    throw e;
  }
  return (await res.json()) as T;
}

// ---------------------------------------------- assignment-rule generation
//
// Local mirror of the sibling contract (backend AssignmentRule + the two AI
// rule routes). Defined here rather than in types/api.ts because that file is
// owned by another agent this wave; keep in sync with their Pydantic model.

/** One name-match rule: any prim whose name contains one of `match_name_contains`
 *  gets `rf_material_id`. */
interface AssignmentRule {
  id: string;
  match_name_contains: string[];
  rf_material_id: string;
  note?: string | null;
}

interface GenerateRulesResponse {
  rules: AssignmentRule[];
  provider: string;
  model?: string | null;
  warnings: string[];
}

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

function SuggestionCard({ suggestion }: { suggestion: MaterialSuggestion }) {
  const materials = useAppStore((s) => s.materials);
  const decisions = useAppStore((s) => s.decisions);
  const setDecision = useAppStore((s) => s.setDecision);
  const selectPrim = useAppStore((s) => s.selectPrim);

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
      <span className="prim-link" onClick={() => selectPrim(suggestion.prim_id)}>
        {suggestion.prim_id}
      </span>

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

// ---------------------------------------------------- assignment impact

/** 8 straight-line positions through the scene at 1.5 m, derived from the
 *  scene-bounds diagonal (mirrors the trajectory seeding in ResultExplorer). */
function impactWaypoints(): Vec3[] {
  const b = useAppStore.getState().sceneBounds;
  const n = 8;
  if (!b) {
    // No bounds: a plain X sweep at head height so the request still runs.
    return Array.from({ length: n }, (_, i) => {
      const t = i / (n - 1);
      return [-40 + t * 80, 0, 1.5] as Vec3;
    });
  }
  // UE height: 1.5 m if it fits inside the scene's Z range, else mid-height.
  const h = b.min[2] + 1.5 < b.max[2] ? b.min[2] + 1.5 : (b.min[2] + b.max[2]) / 2;
  // Diagonal across the XY footprint, inset 10% off each corner so endpoints
  // stay inside the geometry.
  const x0 = b.min[0] + (b.max[0] - b.min[0]) * 0.1;
  const y0 = b.min[1] + (b.max[1] - b.min[1]) * 0.1;
  const x1 = b.min[0] + (b.max[0] - b.min[0]) * 0.9;
  const y1 = b.min[1] + (b.max[1] - b.min[1]) * 0.9;
  return Array.from({ length: n }, (_, i) => {
    const t = i / (n - 1);
    return [x0 + (x1 - x0) * t, y0 + (y1 - y0) * t, h] as Vec3;
  });
}

const fmtDb = (v: number | null | undefined, digits = 1) =>
  v === null || v === undefined ? "—" : `${v.toFixed(digits)} dB`;

function ImpactSection() {
  const projectId = useAppStore((s) => s.projectId);
  const materials = useAppStore((s) => s.materials);
  const sceneBounds = useAppStore((s) => s.sceneBounds);

  const [baseline, setBaseline] = useState("itu_concrete");
  const [report, setReport] = useState<MaterialImpactReport | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const evaluate = async () => {
    if (!projectId) return;
    setBusy(true);
    setError(null);
    try {
      const rep = await api.materialImpact(projectId, {
        baseline_material_id: baseline,
        waypoints: impactWaypoints(),
      });
      setReport(rep);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const nmseSeries = report
    ? [
        {
          label: "NMSE",
          x: report.positions.map((_, i) => i),
          y: report.positions.map((p) => p.nmse_db ?? null),
        },
      ]
    : [];

  return (
    <div className="impact-section">
      <h4>Assignment impact (vs single-material baseline)</h4>
      <p className="hint">
        Re-solves the channel along {impactWaypoints().length} positions through the scene with the
        assigned materials vs a uniform baseline (Lee et al., KICS 2026).
      </p>

      <div className="impact-controls">
        <label>
          Baseline
          <select value={baseline} onChange={(e) => setBaseline(e.target.value)}>
            {(materials?.materials ?? []).map((m) => (
              <option key={m.id} value={m.id}>
                {m.display_name} ({m.id})
              </option>
            ))}
          </select>
        </label>
        <button
          className="primary"
          disabled={!projectId || busy}
          onClick={() => void evaluate()}
          title={sceneBounds ? "" : "No scene bounds yet — using a default X sweep"}
        >
          {busy ? "Evaluating…" : "Evaluate"}
        </button>
      </div>

      {error && <div className="disambig-error">{error}</div>}

      {report && (
        <>
          <div className="results-meta">
            {report.tx_id} → {report.rx_id} · baseline{" "}
            <span className="mono">{report.baseline_material_id}</span> · backend{" "}
            <span className="mono">{report.backend}</span>
          </div>

          <div className="impact-kpis">
            <div className="kpi">
              <span className="kpi-label">global NMSE</span>
              <span className="kpi-val">{fmtDb(report.global_nmse_db)}</span>
            </div>
            <div className="kpi">
              <span className="kpi-label">mean cos-sim</span>
              <span className="kpi-val">
                {report.mean_cosine_similarity === null || report.mean_cosine_similarity === undefined
                  ? "—"
                  : report.mean_cosine_similarity.toFixed(3)}
              </span>
            </div>
            <div className="kpi">
              <span className="kpi-label">mean ΔRSS</span>
              <span className="kpi-val">{fmtDb(report.mean_delta_rss_db)}</span>
            </div>
            <div className="kpi">
              <span className="kpi-label">capacity mat / base</span>
              <span className="kpi-val">
                {report.mean_capacity_material_mbps === null ||
                report.mean_capacity_material_mbps === undefined
                  ? "—"
                  : `${report.mean_capacity_material_mbps.toFixed(0)}`}
                {" / "}
                {report.mean_capacity_baseline_mbps === null ||
                report.mean_capacity_baseline_mbps === undefined
                  ? "—"
                  : `${report.mean_capacity_baseline_mbps.toFixed(0)} Mbps`}
              </span>
            </div>
            <div className="kpi">
              <span className="kpi-label">sensitive</span>
              <span className="kpi-val">
                {report.material_sensitive_count} / {report.positions.length}
              </span>
            </div>
          </div>

          {report.warnings.length > 0 && (
            <ul className="disambig-warnings">
              {report.warnings.map((w, i) => (
                <li key={i}>{w}</li>
              ))}
            </ul>
          )}

          {report.positions.length > 0 && (
            <LineChart
              title="Per-position NMSE (material vs baseline)"
              name="material_impact_nmse"
              xLabel="position index"
              yLabel="NMSE (dB)"
              series={nmseSeries}
              legend={false}
            />
          )}
        </>
      )}
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
      const resp = await postJson<GenerateRulesResponse>(
        `/projects/${projectId}/ai/generate-rules`,
        { instruction: instruction.trim(), provider: aiProvider },
      );
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
      const resp = await postJson<MaterialSuggestionResponse>(
        `/projects/${projectId}/ai/apply-rules`,
        { rules },
      );
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

/** Message from a thrown error (postJson attaches a numeric `status`). */
function errMessage(err: unknown): string {
  return err instanceof Error ? err.message : String(err);
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
  const projectId = useAppStore((s) => s.projectId);
  const busy = useAppStore((s) => s.busy);

  const providers = aiStatuses.length > 0 ? aiStatuses : (health?.ai_providers ?? []);
  const decisionCount = Object.keys(decisions).length;

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
          <span className="dot" style={{ background: p.available ? "#66bb6a" : "#78909c" }} />
          <span className="provider-name">{p.name}</span>
          {p.model && <span className="mono">{p.model}</span>}
          {p.detail && <span title={p.detail}>· {p.detail}</span>}
        </label>
      ))}

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
              <SuggestionCard key={s.prim_id} suggestion={s} />
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

      <ImpactSection />
    </div>
  );
}
