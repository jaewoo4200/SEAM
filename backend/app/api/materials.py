"""RF material library and assignment endpoints."""

from pathlib import Path

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.schemas.materials import (
    AssignRequest,
    AssignResponse,
    BatchAssignRequest,
    RFMaterial,
    RFMaterialLibrary,
)
from app.services.material_assignment import (
    UnknownMaterialError,
    apply_batch,
    assign_materials,
)
from app.services.project_store import ProjectNotFoundError, ProjectStore

router = APIRouter(tags=["materials"])


def _resolve_or_404(store: ProjectStore, project_id: str) -> Path:
    try:
        return store.resolve(project_id)
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"project not found: {project_id}"
        )


@router.get("/projects/{project_id}/rf/materials", response_model=RFMaterialLibrary)
def get_materials(project_id: str) -> RFMaterialLibrary:
    store = deps.get_store()
    _resolve_or_404(store, project_id)
    return store.load_materials(project_id)


@router.put(
    "/projects/{project_id}/rf/materials/{material_id}",
    response_model=RFMaterialLibrary,
)
def put_material(
    project_id: str, material_id: str, body: RFMaterial
) -> RFMaterialLibrary:
    if body.id != material_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"material id mismatch: path has {material_id!r}, "
                f"body has {body.id!r}"
            ),
        )
    store = deps.get_store()
    _resolve_or_404(store, project_id)
    library = store.load_materials(project_id)
    # A PUT is a user edit: the stored copy is never marked builtin, even
    # when it shadows a material shipped with the app.
    stored = body.model_copy(update={"builtin": False})
    for i, mat in enumerate(library.materials):
        if mat.id == material_id:
            library.materials[i] = stored
            break
    else:
        library.materials.append(stored)
    store.save_materials(project_id, library)
    return library


@router.post("/projects/{project_id}/rf/assign", response_model=AssignResponse)
def assign(project_id: str, body: AssignRequest) -> AssignResponse:
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    try:
        result = assign_materials(scene, body, library)
    except UnknownMaterialError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:  # e.g. material set with status "unassigned"
        raise HTTPException(status_code=400, detail=str(exc))
    store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "rf_assign",
            "rf_material_id": body.rf_material_id,
            "assignment_status": body.assignment_status,
            "sources": list(body.sources),
            "updated_prim_ids": result.updated_prim_ids,
            "skipped_prim_ids": result.skipped_prim_ids,
        },
    )
    return result


@router.post("/projects/{project_id}/rf/batch-assign", response_model=AssignResponse)
def batch_assign(project_id: str, body: BatchAssignRequest) -> AssignResponse:
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    try:
        result = apply_batch(scene, body, library)
    except UnknownMaterialError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "rf_assign",
            "batch": True,
            "items": [
                {
                    "rf_material_id": item.rf_material_id,
                    "assignment_status": item.assignment_status,
                    "prim_ids": item.prim_ids,
                }
                for item in body.assignments
            ],
            "updated_prim_ids": result.updated_prim_ids,
            "skipped_prim_ids": result.skipped_prim_ids,
        },
    )
    return result
