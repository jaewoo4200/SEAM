# AI assistant

> 🌐 **English** · [한국어](ai_assistant.ko.md)

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
| `local_openai` | reachable OpenAI-compatible server (LM Studio 등, `SIONNATWIN_OPENAI_URL`) | prompts a local LLM with prim evidence, strict JSON back. **Vision supported**: with a screenshot the request becomes multimodal (`image_url`); if the loaded model rejects images it retries text-only before falling back |
| `ollama_text` | reachable Ollama server + text model | same contract via Ollama chat API. **Vision supported**: a screenshot attaches as base64 `images` and the call switches to `SIONNATWIN_AI_VISION_MODEL` (a warning notes the model swap); image rejection retries text-only |
| `disabled` | — | returns no suggestions (AI turned off) |

Selection: `SuggestMaterialsRequest.provider` forces a specific provider;
otherwise the best available one is used (`local_openai` → `ollama_text` →
`rule_based`). If a server is unreachable, times out, or returns JSON that
fails schema validation, the response falls back down the chain and records
what happened in `warnings`. The `provider`/`model` fields of the response
always name what *actually* produced the result.

**Live-verified**: LM Studio + `google/gemma-4-31b` end-to-end (text and
multimodal), including library-id validation of every suggestion — the model
cannot introduce a material id that is not in the project's RF library.

All Ollama access is lazy (imported/probed inside functions): no AI server,
no GPU, and no compatible model are required for anything else to work, and
`/api/health` reports each provider's availability via
`get_provider_statuses()`.

## Configuration

Environment variables (read once by `app.core.config.get_settings()`).
Names use the canonical `SEAM_*` prefix; the legacy `SIONNATWIN_*` name is
still accepted for every variable (`SEAM_*` wins when both are set).

| variable | default | meaning |
|---|---|---|
| `SEAM_AI_ENABLED` | `auto` | `auto` (use if reachable) \| `on` \| `off` |
| `SEAM_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `SEAM_AI_TEXT_MODEL` | `qwen3:8b` | Ollama text model |
| `SEAM_AI_VISION_MODEL` | `qwen2.5vl:3b` | Ollama vision model used when a screenshot is attached (e.g. set to `llava` to use LLaVA) |
| `SEAM_OPENAI_URL` | `http://localhost:1234/v1` | OpenAI-compatible endpoint (LM Studio 기본 포트) |
| `SEAM_OPENAI_MODEL` | `google/gemma-4-31b` | model id served by the OpenAI-compatible server |
| `SEAM_AI_TIMEOUT_S` | `60` | request timeout (seconds) for text-only calls |
| `SEAM_AI_VISION_TIMEOUT_S` | `300` | request timeout (seconds) for multimodal (image-carrying) calls — a local VLM needs model load + multi-image prefill, so it gets a higher ceiling than text |
| `SEAM_AI_AUTO_APPLY` | `false` | reserved for a future auto-apply gate; parsed into settings but **no code acts on it in the MVP** |

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
  "prompt_version": "v2",
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
 "prompt_version": "v2",
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

## Natural-language rule generation

Per-prim suggestion is one axis; the other is *bulk* assignment by intent.
`POST /projects/{id}/ai/generate-rules` turns a plain-language instruction
("glass windows are `itu_glass`, everything with 'concrete' in the name is
`itu_concrete`") into a list of deterministic, inspectable **assignment rules**:

```json
POST /projects/{id}/ai/generate-rules
{ "instruction": "windows are glass, walls with 'concrete' → itu_concrete" }
->
{ "rules": [
    { "id": "r1", "match_name_contains": ["window", "glass"],
      "rf_material_id": "itu_glass", "note": "glazing" },
    { "id": "r2", "match_name_contains": ["concrete"],
      "rf_material_id": "itu_concrete", "note": null } ],
  "provider": "local_openai", "model": "…", "warnings": [] }
```

An `AssignmentRule` is `{id, match_name_contains: string[] (≥1),
rf_material_id, note?}` — a case-insensitive substring OR-match over prim
names. The same library-id guard as suggestions applies: a rule naming an
`rf_material_id` outside the project library is dropped with a warning, so the
model can never invent a material. Rules are **proposals**, not an assignment.

Applying them is the explicit second step: `POST /projects/{id}/ai/apply-rules`
takes the (possibly user-edited) rule list, matches it against the current
scene, and returns a `MaterialSuggestionResponse` — the exact same shape the
per-prim suggester returns, so the review-and-apply UI is identical. Matched
prims come back as suggestions with `assignment_status: "rule_assigned"`
evidence; nothing touches the scene until the user approves them through the
normal apply-suggestions path. Prims a user has already rejected stay
`rejected` (material id null) and are not re-proposed.

## Validation explanation

`POST /projects/{id}/ai/explain-validation` runs the scene validator and asks
the provider to explain the resulting issues in plain language — what each
`ValidationIssue` means for accuracy and what to do about it:

```json
POST /projects/{id}/ai/explain-validation
->
{ "explanation": "3 prims are unassigned … an ITU ground material is used at
   28 GHz, which is out of band; switch it to `ground_28ghz` …",
  "provider": "ollama_text", "model": "qwen3:8b", "warnings": [] }
```

It is read-only: it never mutates the scene or the assignments, only narrates
the checklist. Each `ValidationIssue` also now carries
`suggested_actions: string[]` (the concrete next steps the UI shows as
one-click fixes), so the natural-language explanation and the structured
actions stay in sync. As always this degrades gracefully — with no reachable
AI server the `rule_based` provider returns a templated explanation built from
the issue codes.

## RF disambiguation

Vision alone cannot tell two RF-different materials apart when they *look*
the same. Glass is the canonical case: Dai et al. (Qualcomm, JSTEAP 2025)
report that visually indistinguishable glass panes span roughly **2.5–23.6 dB**
of penetration loss at mmWave — a difference that dominates the link budget yet
leaves no visual trace a camera or a texture name could pick up. A rule- or
vision-based suggester will happily label them all `itu_glass`.

RF disambiguation resolves the tie with *measurements* instead of pixels. Given
a prim, a shortlist of candidate materials (the suggestion plus its
alternatives), and a few measured per-link path gains, the service binds each
candidate to the prim in turn, recompiles the scene, re-simulates the measured
links, and scores the level-aligned RMSE — the same metric as parameter
calibration (`services/calibration.py::disambiguate_materials`). The lowest-RMSE
candidate wins.

```
POST /projects/{id}/calibrate/disambiguate
{ "config": {"backend": "sionna"},
  "prim_ids": ["/buildings/b01/window_12"],
  "candidate_material_ids": ["itu_glass", "itu_glass_thick", "metal"],
  "measurements": [ {"rx_position": [10,5,1.5], "measured_path_gain_db": -92.0}, ... ] }
->
{ "prim_ids": [...],
  "candidates": [ {"material_id": "itu_glass", "rmse_db": 1.8, "n_links": 6}, ... ],
  "best_material_id": "itu_glass_thick",
  "backend": "sionna", "warnings": [] }
```

**Indistinguishable warning.** When the candidates' RMSE spread is below
0.05 dB the measurements cannot separate them at those positions — the service
returns `best_material_id: null` and warns
`candidates are indistinguishable at these positions (RMSE spread … dB); add
measurements nearer the prims` rather than picking noise. This is exactly what
the deterministic mock backend does for any two ITU frequency-dependent
candidates (its reflection loss only carries the scattering term, so equal-`S`
materials predict identically) — disambiguation is a **Sionna-backend accuracy
feature**; the mock exists to make the flow testable, not to separate materials.

## Assignment impact evaluation

Once materials are assigned, *how much do they actually matter for this link?*
The impact evaluation implements the CFR framework of Lee et al. (KICS 2026):
solve each TX→RX position twice — once with the scene's assigned materials, once
with **every** prim rebound to a single baseline material (default
`itu_concrete`) — and compare the two channel frequency responses
(`services/material_impact.py`). Per position it reports:

- **NMSE (dB)** — `Σ|H_mat − H_base|² / Σ|H_mat|²`. How far the baseline channel
  is from the material-aware one. More negative = the materials barely move the
  channel; near 0 dB = they dominate it. A position above `sensitive_nmse_db`
  (KICS uses −60 dB) is flagged **material-sensitive**.
- **cosine similarity** — `|H_matᴴ H_base| / (‖H_mat‖‖H_base‖)`, in [0, 1]. Shape
  agreement of the two CFRs; 1.0 means identical up to scale.
- **dRSS (dB)** — signed `RSS_mat − RSS_base`. Whether the assigned materials
  raise or lower received power vs the baseline.
- **capacity proxy (Mbps)** — a Shannon `B·mean_f log₂(1+SNR(f))` throughput for
  each variant, so the material effect lands in an end-to-end KPI.

Read it as: **near-zero NMSE + cos-sim ≈ 1 + dRSS ≈ 0 means "materials don't
matter here"** (the geometry/LoS carries the link); a high per-position NMSE
with a large dRSS is a location where getting the material right is essential.
Live on Sionna (`lab_room`) the per-position NMSE runs −6 to −17 dB. On the
material-blind mock, binding the baseline to the same material already on the
reflecting prims collapses every metric to its identity (cos-sim 1, dRSS 0,
global NMSE undefined) — again a **Sionna accuracy feature** with a testable
mock stub. Endpoint: `POST /projects/{id}/analyze/material-impact`.

## Multi-view capture & texture crops

**Multi-view capture** *(accuracy feature).* A single screenshot sees each
surface from one angle, under one glare/occlusion condition. `SuggestMaterialsRequest`
therefore accepts `screenshot_data_urls` (up to 6 views; the legacy single
`screenshot_data_url` is still honoured as a one-item list). Following Dai et
al.'s multi-view majority merge, a vision provider aggregates a per-category
prompt variant across the views and keeps the majority label, so a window that
reads as "glass" in four of six views is not derailed by the two frames where a
reflection made it look like metal.

**Texture crops** *(accuracy feature).* Whole-viewport screenshots hand the model
mostly empty space and force it to guess which pixels belong to the prim under
question. Passing tight per-prim texture crops (the KICS SAM2.1 + DINOv2
per-triangle route) instead gives the model the surface's own appearance at full
resolution, which is what per-triangle voting and the CFR/NMSE eval downstream
actually consume.
