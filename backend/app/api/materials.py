"""RF material library and assignment endpoints."""

from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from app.api import deps
from app.schemas.common import StrictModel
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
    prims_using_material,
    unassign_materials,
)
from app.services.project_store import ProjectNotFoundError, ProjectStore

router = APIRouter(tags=["materials"])


class UnassignRequest(StrictModel):
    """Clear the RF material binding on the given prims (back to 'unassigned').

    Defined here (not in schemas/materials.py) so the unassign flow is fully
    contained in this router.
    """

    prim_ids: list[str] = Field(min_length=1)


class MaterialImportRequest(StrictModel):
    """A material pack, as produced by the export endpoint.

    Defined here (not in schemas/materials.py) so the export/import flow is
    fully contained in this router.
    """

    materials: list[RFMaterial] = Field(min_length=1)


class MaterialImportResponse(StrictModel):
    imported: list[str] = Field(default_factory=list)
    # Incoming id -> id actually stored, for imports renamed on collision.
    renamed: dict[str, str] = Field(default_factory=dict)
    skipped: list[str] = Field(default_factory=list)


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


# NOTE: the static export/import routes below must stay declared before the
# /rf/materials/{material_id} routes so "export"/"import" are never captured
# as a material id.


@router.get(
    "/projects/{project_id}/rf/materials/export",
    response_model=RFMaterialLibrary,
)
def export_materials(
    project_id: str, ids: Optional[str] = Query(default=None)
) -> RFMaterialLibrary:
    """Export custom materials as a pack for import into another project.

    ``ids`` is a comma-separated selection; omitted means every non-builtin
    material in the library. Builtin materials are never exported - they ship
    with every install - but explicitly requesting an unknown id is a 404.
    """
    store = deps.get_store()
    _resolve_or_404(store, project_id)
    library = store.load_materials(project_id)
    if ids is None:
        selected = list(library.materials)
    else:
        requested = list(
            dict.fromkeys(token.strip() for token in ids.split(",") if token.strip())
        )
        missing = [mid for mid in requested if library.get(mid) is None]
        if missing:
            raise HTTPException(
                status_code=404,
                detail=f"material not found: {', '.join(missing)}",
            )
        selected = [library.get(mid) for mid in requested]
    return RFMaterialLibrary(materials=[m for m in selected if not m.builtin])


def _next_free_id(base_id: str, taken: set[str]) -> str:
    """First ``{base_id}_{n}`` (n >= 2) not in ``taken``.

    ``base_id`` already matches ``^[a-z0-9_]+$``, so a numeric suffix keeps
    the id pattern valid.
    """
    n = 2
    while f"{base_id}_{n}" in taken:
        n += 1
    return f"{base_id}_{n}"


@router.post(
    "/projects/{project_id}/rf/materials/import",
    response_model=MaterialImportResponse,
)
def import_materials(
    project_id: str, body: MaterialImportRequest
) -> MaterialImportResponse:
    """Merge an exported material pack into this project's library.

    Imports are user-space copies, so builtin is forced off (mirrors
    put_material). An incoming material whose values match the stored one
    (builtin flag aside) is skipped; a colliding id with different values is
    imported under a numeric-suffix rename (glass -> glass_2).
    """
    store = deps.get_store()
    _resolve_or_404(store, project_id)
    library = store.load_materials(project_id)
    imported: list[str] = []
    renamed: dict[str, str] = {}
    skipped: list[str] = []
    for incoming in body.materials:
        material = incoming.model_copy(update={"builtin": False})
        existing = library.get(material.id)
        if existing is not None:
            # Compare with builtin masked off: re-importing an unmodified
            # builtin (or an already-imported copy) is a no-op, not a dupe.
            if existing.model_copy(update={"builtin": False}) == material:
                skipped.append(material.id)
                continue
            new_id = _next_free_id(material.id, library.ids())
            renamed[material.id] = new_id
            material = material.model_copy(update={"id": new_id})
        library.materials.append(material)
        imported.append(material.id)
    if imported:
        store.save_materials(project_id, library)
    return MaterialImportResponse(
        imported=imported, renamed=renamed, skipped=skipped
    )


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


@router.post("/projects/{project_id}/rf/unassign", response_model=AssignResponse)
def unassign(project_id: str, body: UnassignRequest) -> AssignResponse:
    """Clear the RF material on the given prims (status -> 'unassigned')."""
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    result = unassign_materials(scene, body.prim_ids)
    store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "rf_unassign",
            "updated_prim_ids": result.updated_prim_ids,
            "skipped_prim_ids": result.skipped_prim_ids,
        },
    )
    return result


@router.delete(
    "/projects/{project_id}/rf/materials/{material_id}",
    response_model=RFMaterialLibrary,
)
def delete_material(project_id: str, material_id: str) -> RFMaterialLibrary:
    """Remove a custom, unassigned RF material from the project library.

    Refuses builtin materials (400) and materials still assigned to prims
    (409, listing the prim ids). Only custom materials with no remaining
    assignment can be deleted.
    """
    store = deps.get_store()
    _resolve_or_404(store, project_id)
    library = store.load_materials(project_id)
    material = library.get(material_id)
    if material is None:
        raise HTTPException(
            status_code=404, detail=f"material not found: {material_id}"
        )
    if material.builtin:
        raise HTTPException(
            status_code=400,
            detail=f"cannot delete builtin material: {material_id}",
        )
    # Guard against dangling references: a prim pointing at a deleted material
    # would fail RF compilation. Load the scene when present (a freshly created
    # project always has one) and block if the material is still in use.
    try:
        scene = store.load_scene(project_id)
    except ProjectNotFoundError:
        scene = None
    if scene is not None:
        in_use = prims_using_material(scene, material_id)
        if in_use:
            raise HTTPException(
                status_code=409,
                detail={
                    "message": (
                        f"material {material_id} is still assigned to "
                        f"{len(in_use)} prim(s); unassign them first"
                    ),
                    "prim_ids": in_use,
                },
            )
    library.materials = [m for m in library.materials if m.id != material_id]
    store.save_materials(project_id, library)
    store.append_provenance(
        project_id, {"type": "rf_material_delete", "material_id": material_id}
    )
    return library


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
