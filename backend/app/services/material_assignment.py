"""RF material assignment onto canonical scene prims.

Assignment mutates the scene in place; persistence is the caller's job (the
API route saves the scene and appends a provenance event). Only
mesh_primitive prims are assignable; group prims are skipped with a warning
because the RF compiler only compiles mesh geometry, so a binding on a group
would silently have no RF effect.
"""

from app.schemas.materials import (
    AssignRequest,
    AssignResponse,
    BatchAssignRequest,
    RFMaterialLibrary,
)
from app.schemas.scene import RFBinding, Scene


class UnknownMaterialError(ValueError):
    def __init__(self, material_id: str):
        super().__init__(f"unknown RF material: {material_id}")
        self.material_id = material_id


def assign_materials(
    scene: Scene,
    request: AssignRequest,
    library: RFMaterialLibrary,
) -> AssignResponse:
    if library.get(request.rf_material_id) is None:
        raise UnknownMaterialError(request.rf_material_id)

    updated: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    overrides = request.overrides
    for prim_id in request.prim_ids:
        prim = scene.prim_by_id(prim_id)
        if prim is None:
            skipped.append(prim_id)
            warnings.append(f"prim not found: {prim_id}")
            continue
        if prim.type != "mesh_primitive":
            # The RF compiler only compiles mesh_primitive prims, so a binding
            # on a group prim would silently have no RF effect. Skip it and
            # steer the caller to the child mesh prims instead.
            skipped.append(prim.id)
            warnings.append(
                f"{prim.id}: group prims are not compiled to RF; "
                "assign the child mesh prims instead"
            )
            continue
        # Build a fresh binding instead of mutating field-by-field: RFBinding
        # validates material/status consistency as a whole, so an incremental
        # update could trip validate_assignment mid-way. This also clears any
        # stale per-prim overrides when the request carries none.
        prim.rf = RFBinding(
            material_id=request.rf_material_id,
            thickness_m=overrides.thickness_m if overrides else None,
            scattering_coefficient=(
                overrides.scattering_coefficient if overrides else None
            ),
            xpd_coefficient=overrides.xpd_coefficient if overrides else None,
            assignment_status=request.assignment_status,
            assignment_sources=list(request.sources),
            confidence=request.confidence,
        )
        updated.append(prim_id)

    return AssignResponse(
        updated_prim_ids=updated,
        skipped_prim_ids=skipped,
        warnings=warnings,
    )


def unassign_materials(scene: Scene, prim_ids: list[str]) -> AssignResponse:
    """Clear the RF material binding on each prim, returning it to 'unassigned'.

    Like :func:`assign_materials`, this mutates the scene in place and leaves
    persistence to the caller. A fresh default ``RFBinding`` is written so the
    material id, per-prim overrides, sources and confidence are all cleared and
    the status/material invariant (unassigned <=> material_id is None) holds.
    Unknown prim ids are skipped (never an error) with a warning.
    """
    updated: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    for prim_id in prim_ids:
        prim = scene.prim_by_id(prim_id)
        if prim is None:
            skipped.append(prim_id)
            warnings.append(f"prim not found: {prim_id}")
            continue
        prim.rf = RFBinding(
            material_id=None,
            assignment_status="unassigned",
            assignment_sources=["user"],
        )
        updated.append(prim_id)
    return AssignResponse(
        updated_prim_ids=updated,
        skipped_prim_ids=skipped,
        warnings=warnings,
    )


def prims_using_material(scene: Scene, material_id: str) -> list[str]:
    """Ids of every prim whose RF binding references ``material_id``."""
    return [p.id for p in scene.prims if p.rf.material_id == material_id]


def apply_batch(
    scene: Scene,
    batch: BatchAssignRequest,
    library: RFMaterialLibrary,
) -> AssignResponse:
    """Apply every assignment in the batch, aggregating per-item results."""
    # Validate all material ids up front so a bad item cannot leave the scene
    # half-mutated.
    for item in batch.assignments:
        if library.get(item.rf_material_id) is None:
            raise UnknownMaterialError(item.rf_material_id)

    updated: list[str] = []
    skipped: list[str] = []
    warnings: list[str] = []
    for item in batch.assignments:
        result = assign_materials(scene, item, library)
        for pid in result.updated_prim_ids:
            if pid not in updated:
                updated.append(pid)
        for pid in result.skipped_prim_ids:
            if pid not in skipped:
                skipped.append(pid)
        warnings.extend(result.warnings)

    return AssignResponse(
        updated_prim_ids=updated,
        skipped_prim_ids=skipped,
        warnings=warnings,
    )
