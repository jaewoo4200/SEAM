"""AI material-suggestion endpoints (HANDOFF 6 + 9).

Every suggestion batch and every user decision is logged to
``ai/suggestions.jsonl`` for provenance. Auto-apply exists only as a config
gate (``settings.ai.auto_apply``) and is deliberately NOT implemented by
these MVP endpoints: no AI output mutates the scene without an explicit user
decision posted to /ai/apply-suggestions.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.api.deps import get_store, load_scene_or_404
from app.schemas.ai import (
    AIModelsResponse,
    AIProviderStatus,
    ApplyRulesRequest,
    ApplySuggestionsRequest,
    ExplainValidationResponse,
    MaterialSuggestionResponse,
    RuleGenerationRequest,
    RuleGenerationResponse,
    SuggestMaterialsRequest,
)
from app.schemas.materials import AssignRequest, AssignResponse
from app.schemas.scene import RFBinding
from app.services import ai_provider
from app.services.ai_provider import AIParseError, NoTextProviderError
from app.services.material_assignment import UnknownMaterialError, assign_materials
from app.services.scene_validator import validate_scene

router = APIRouter(tags=["ai"])

SUGGESTIONS_LOG = "ai/suggestions.jsonl"


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get(
    "/projects/{project_id}/ai/status",
    response_model=list[AIProviderStatus],
)
def ai_status(project_id: str) -> list[AIProviderStatus]:
    store = get_store()
    load_scene_or_404(store, project_id)  # 404 for unknown project
    return ai_provider.get_provider_statuses()


@router.get(
    "/projects/{project_id}/ai/models",
    response_model=AIModelsResponse,
)
def ai_models(project_id: str) -> AIModelsResponse:
    """Selectable models per model-bearing provider for the picker.

    Covers local_openai + ollama_text (rule_based/disabled omitted); each entry
    mirrors the provider probe state and lists the models discovered on its
    server.
    """
    store = get_store()
    load_scene_or_404(store, project_id)  # 404 for unknown project
    return ai_provider.get_provider_models()


@router.post(
    "/projects/{project_id}/ai/suggest-materials",
    response_model=MaterialSuggestionResponse,
)
def suggest_materials(
    project_id: str, request: SuggestMaterialsRequest
) -> MaterialSuggestionResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    project_dir = store.resolve(project_id)
    try:
        response = ai_provider.suggest_materials(
            scene, library, request, project_dir=project_dir
        )
    except ValueError as exc:  # unknown forced provider
        raise HTTPException(status_code=400, detail=str(exc))
    screenshot_count = len(
        ai_provider._effective_images(
            request.screenshot_data_urls, request.screenshot_data_url
        )
    )
    # Model provenance: "user" when the caller's model override was honored,
    # "fallback" when the guardrail replaced an unknown model with the default
    # (detected via the warning the provider appended), else "env_default".
    if request.model is None:
        model_source = "env_default"
    elif any(
        f"requested model '{request.model}' is not loaded" in w
        for w in response.warnings
    ):
        model_source = "fallback"
    else:
        model_source = "user"
    store.append_jsonl(
        project_id,
        SUGGESTIONS_LOG,
        {
            "timestamp": _utcnow(),
            "event": "suggested",
            "provider": response.provider,
            "model": response.model,
            "model_source": model_source,
            "prompt_version": response.prompt_version,
            "input_prim_ids": ai_provider.resolve_target_prim_ids(scene, request),
            # Image provenance: viewport screenshots are transient (count
            # only), but texture crops the provider saw are persisted under
            # ai/evidence/ and referenced here so a batch is reproducible.
            "screenshot_attached": screenshot_count > 0,
            "screenshot_count": screenshot_count,
            "texture_crops_requested": bool(request.attach_texture_crops),
            "evidence_images": [
                e.asset_path for e in (response.evidence_images or [])
            ],
            "suggestions": [s.model_dump(mode="json") for s in response.suggestions],
        },
    )
    return response


@router.post(
    "/projects/{project_id}/ai/apply-suggestions",
    response_model=AssignResponse,
)
def apply_suggestions(
    project_id: str, request: ApplySuggestionsRequest
) -> AssignResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    suggestion_by_prim = {s.prim_id: s for s in request.suggestions}

    updated: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    records: list[dict] = []
    counts = {"approve": 0, "reject": 0, "edit": 0}
    # A reject stamps assignment_status="rejected" (material stays None), which
    # mutates the scene without adding to ``updated``; track it so the scene is
    # still persisted.
    mutated = False

    for decision in request.decisions:
        suggestion = suggestion_by_prim.get(decision.prim_id)
        final_material: Optional[str] = None
        if decision.action == "reject":
            prim = scene.prim_by_id(decision.prim_id)
            if prim is None:
                skipped.append(decision.prim_id)
                warnings.append(f"prim not found: {decision.prim_id}")
            else:
                # Explicit no-material decision: record the rejection on the
                # binding so validation and re-suggestion can skip it. Material
                # stays None (NO_MATERIAL_STATUSES invariant).
                prim.rf = RFBinding(
                    material_id=None,
                    assignment_status="rejected",
                    assignment_sources=[f"ai:{request.provider}", "user"],
                )
                mutated = True
        else:
            if decision.action == "approve":
                material_id = decision.rf_material_id or (
                    suggestion.recommended_rf_material_id if suggestion else None
                )
                if material_id is None:
                    raise HTTPException(
                        status_code=400,
                        detail=(
                            f"approve for {decision.prim_id}: no rf_material_id "
                            "given and no matching suggestion in the batch"
                        ),
                    )
                sources = [f"ai:{request.provider}", "user"]
            else:  # edit
                if decision.rf_material_id is None:
                    raise HTTPException(
                        status_code=400,
                        detail=f"edit for {decision.prim_id}: rf_material_id is required",
                    )
                material_id = decision.rf_material_id
                sources = [f"ai:{request.provider}", "user_edit"]
            confidence = (
                suggestion.confidence
                if suggestion is not None
                and material_id == suggestion.recommended_rf_material_id
                else None
            )
            assign_request = AssignRequest(
                prim_ids=[decision.prim_id],
                rf_material_id=material_id,
                assignment_status="user_confirmed",
                sources=sources,
                confidence=confidence,
            )
            try:
                result = assign_materials(scene, assign_request, library)
            except UnknownMaterialError as exc:
                raise HTTPException(status_code=404, detail=str(exc))
            updated.extend(result.updated_prim_ids)
            skipped.extend(result.skipped_prim_ids)
            warnings.extend(result.warnings)
            if result.updated_prim_ids:
                final_material = material_id
        counts[decision.action] += 1
        records.append(
            {
                "timestamp": _utcnow(),
                "event": "decision",
                "action": decision.action,
                "prim_id": decision.prim_id,
                "provider": request.provider,
                "model": request.model,
                "final_rf_material_id": final_material,
            }
        )

    if updated or mutated:  # persist the mutated scene exactly once
        store.save_scene(project_id, scene)
    for record in records:
        store.append_jsonl(project_id, SUGGESTIONS_LOG, record)
    store.append_provenance(
        project_id,
        {
            "type": "ai_apply",
            "approved": counts["approve"],
            "rejected": counts["reject"],
            "edited": counts["edit"],
            # Which provider/model produced the batch these decisions apply to.
            "provider": request.provider,
            "model": request.model,
        },
    )
    return AssignResponse(
        updated_prim_ids=updated, skipped_prim_ids=skipped, warnings=warnings
    )


@router.post(
    "/projects/{project_id}/ai/generate-rules",
    response_model=RuleGenerationResponse,
)
def generate_rules(
    project_id: str, request: RuleGenerationRequest
) -> RuleGenerationResponse:
    store = get_store()
    load_scene_or_404(store, project_id)  # 404 for unknown project
    library = store.load_materials(project_id)
    try:
        rules, provider, model, warnings = ai_provider.generate_assignment_rules(
            request.instruction, library, request.provider, request.model
        )
    except NoTextProviderError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except AIParseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return RuleGenerationResponse(
        rules=rules, provider=provider, model=model, warnings=warnings
    )


@router.post(
    "/projects/{project_id}/ai/apply-rules",
    response_model=MaterialSuggestionResponse,
)
def apply_rules(
    project_id: str, request: ApplyRulesRequest
) -> MaterialSuggestionResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    response = ai_provider.apply_rules(scene, library, request.rules)
    store.append_jsonl(
        project_id,
        SUGGESTIONS_LOG,
        {
            "timestamp": _utcnow(),
            "event": "suggested",
            "provider": response.provider,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "input_prim_ids": [s.prim_id for s in response.suggestions],
            "rule_ids": [rule.id for rule in request.rules],
            "screenshot_attached": False,
            "screenshot_count": 0,
            "texture_crops_requested": False,
            "suggestions": [s.model_dump(mode="json") for s in response.suggestions],
        },
    )
    return response


@router.post(
    "/projects/{project_id}/ai/explain-validation",
    response_model=ExplainValidationResponse,
)
def explain_validation(project_id: str) -> ExplainValidationResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    library = store.load_materials(project_id)
    report = validate_scene(scene, library, project_dir=project_dir)
    try:
        explanation, provider, model, warnings = (
            ai_provider.explain_validation_warnings(report.issues, library)
        )
    except NoTextProviderError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except AIParseError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    return ExplainValidationResponse(
        explanation=explanation, provider=provider, model=model, warnings=warnings
    )
