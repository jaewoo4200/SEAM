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
    AIProviderStatus,
    ApplySuggestionsRequest,
    MaterialSuggestionResponse,
    SuggestMaterialsRequest,
)
from app.schemas.materials import AssignRequest, AssignResponse
from app.services import ai_provider
from app.services.material_assignment import UnknownMaterialError, assign_materials

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
    try:
        response = ai_provider.suggest_materials(scene, library, request)
    except ValueError as exc:  # unknown forced provider
        raise HTTPException(status_code=400, detail=str(exc))
    store.append_jsonl(
        project_id,
        SUGGESTIONS_LOG,
        {
            "timestamp": _utcnow(),
            "event": "suggested",
            "provider": response.provider,
            "model": response.model,
            "prompt_version": response.prompt_version,
            "input_prim_ids": ai_provider.resolve_target_prim_ids(scene, request),
            # Provenance only: whether a viewport image was attached. The image
            # itself is EVIDENCE, transient, and never persisted here.
            "screenshot_attached": bool(request.screenshot_data_url),
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

    for decision in request.decisions:
        suggestion = suggestion_by_prim.get(decision.prim_id)
        final_material: Optional[str] = None
        if decision.action != "reject":
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

    if updated:  # persist the mutated scene exactly once
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
        },
    )
    return AssignResponse(
        updated_prim_ids=updated, skipped_prim_ids=skipped, warnings=warnings
    )
