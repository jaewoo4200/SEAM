"""RF material assignment onto canonical scene prims.

Assignment mutates the scene in place; persistence is the caller's job (the
API route saves the scene and appends a provenance event). Group prims are
assignable too - the RF compiler decides how bindings propagate to geometry.
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
