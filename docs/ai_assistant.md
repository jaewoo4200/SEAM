# AI assistant

The AI assistant suggests RF materials for prims from visual evidence:
object names, GLB material names, semantic tags, texture names. It is
strictly optional — the app is fully usable manual-only — and it never
mutates the scene by itself.

## Provider abstraction

`app.services.ai_provider` exposes two entry points used by the API layer:

```python
get_provider_statuses() -> list[AIProviderStatus]
suggest_materials(scene, library, request: SuggestMaterialsRequest) -> MaterialSuggestionResponse
```

Behind them sits a provider chain:

| provider | needs | behavior |
|---|---|---|
| `rule_based` | nothing | deterministic keyword rules over name / visual material name / tags (window→`itu_glass`, brick→`itu_brick`, road→`asphalt_custom`, ...) |
| `ollama_text` | reachable Ollama server + text model | prompts a local LLM with prim evidence, expects strict JSON back |
| `ollama_vision` | *planned, not implemented* | same contract with image evidence; not selectable yet — `SIONNATWIN_AI_VISION_MODEL` is reserved config until then |
| `disabled` | — | returns no suggestions (AI turned off) |

Selection: `SuggestMaterialsRequest.provider` forces a specific provider;
otherwise the best available one is used. **Fallback chain: ollama →
rule_based.** If the Ollama server is unreachable, times out, or returns
JSON that fails schema validation, the response falls back to the rule-based
provider and records what happened in `warnings`. The `provider` field of
the response always names the provider that *actually* produced the result.

All Ollama access is lazy (imported/probed inside functions): no AI server,
no GPU, and no compatible model are required for anything else to work, and
`/api/health` reports each provider's availability via
`get_provider_statuses()`.

## Configuration

Environment variables (read once by `app.core.config.get_settings()`):

| variable | default | meaning |
|---|---|---|
| `SIONNATWIN_AI_ENABLED` | `auto` | `auto` (use if reachable) \| `on` \| `off` |
| `SIONNATWIN_OLLAMA_URL` | `http://localhost:11434` | Ollama-compatible endpoint |
| `SIONNATWIN_AI_TEXT_MODEL` | `qwen3:8b` | text model name (configuration, not hardcoded) |
| `SIONNATWIN_AI_VISION_MODEL` | `qwen2.5vl:3b` | reserved for the future vision provider; unused in the MVP |
| `SIONNATWIN_AI_TIMEOUT_S` | `60` | request timeout |
| `SIONNATWIN_AI_AUTO_APPLY` | `false` | reserved for a future auto-apply gate; parsed into settings but **no code acts on it in the MVP** |

## Strict JSON contract

Model output must validate against `MaterialSuggestionResponse`
(`backend/app/schemas/ai.py`). Free-form AI text never reaches the scene.

```json
{
  "suggestions": [
    {
      "prim_id": "/buildings/b01/window_01",
      "recommended_rf_material_id": "itu_glass",
      "confidence": 0.86,
      "evidence": [
        "object name contains 'window'",
        "visual material name contains 'glass'"
      ],
      "alternatives": [{"rf_material_id": "metal", "confidence": 0.11}],
      "needs_user_confirmation": true
    }
  ],
  "provider": "ollama_text",
  "model": "qwen3:8b",
  "prompt_version": "v1",
  "warnings": []
}
```

Constraints enforced at parse time: `confidence` in [0, 1], unknown keys
rejected. A `recommended_rf_material_id` that is not in the project library
is discarded with a warning rather than passed through.

## Applying suggestions

Suggestions are proposals. Applying them is an explicit user decision sent
to the apply endpoint (`ApplySuggestionsRequest`): each decision is
`approve` (use the suggested material), `edit` (user picked a different
material), or `reject`. Approved/edited decisions go through the same
`assign_materials` path as manual assignment, producing an RF binding with
`assignment_status: "ai_suggested"` promoted to `"user_confirmed"` and
`assignment_sources` recording the chain (e.g.
`["ai:ollama/qwen3:8b", "user"]`).

**Never-auto-apply rule:** no suggestion mutates the scene unless the user
acts on it. The MVP has no auto-apply code path at all;
`SIONNATWIN_AI_AUTO_APPLY` is a reserved flag for a future opt-in, and even
then provenance would still record that the assignment came from AI.

## Provenance log

Every suggestion batch and every user decision is appended to
`<project>/ai/suggestions.jsonl`, one JSON object per line:

```json
{"timestamp": "2026-07-02T09:14:03+00:00",
 "event": "suggest",
 "provider": "ollama_text",
 "model": "qwen3:8b",
 "prompt_version": "v1",
 "input_prim_ids": ["/buildings/b01/window_01"],
 "suggestions": [ ...MaterialSuggestion objects... ],
 "warnings": []}

{"timestamp": "2026-07-02T09:15:40+00:00",
 "event": "decision",
 "provider": "ollama_text",
 "model": "qwen3:8b",
 "prim_id": "/buildings/b01/window_01",
 "action": "approve",
 "final_rf_material_id": "itu_glass"}
```

The log is append-only (`ProjectStore.append_jsonl`) and ships with the
project folder, so the full history of who/what suggested each material —
and what the user did about it — survives sharing and re-opening.
