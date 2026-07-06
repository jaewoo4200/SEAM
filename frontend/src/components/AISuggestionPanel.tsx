import { useAppStore } from "../store/appStore";
import { MaterialSelect, Swatch, materialById } from "./common";
import type { MaterialSuggestion } from "../types/api";

function SuggestionCard({ suggestion }: { suggestion: MaterialSuggestion }) {
  const materials = useAppStore((s) => s.materials);
  const decisions = useAppStore((s) => s.decisions);
  const setDecision = useAppStore((s) => s.setDecision);
  const selectPrim = useAppStore((s) => s.selectPrim);

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
    </div>
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
          vision-capable providers see the 3D view (may switch to the vision model)
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
    </div>
  );
}
