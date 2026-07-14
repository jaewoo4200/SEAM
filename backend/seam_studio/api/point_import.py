"""Import devices (TX/RX/UE) and UE trajectories from a standardized JSON.

Three endpoints, all thin request/response + error-mapping shells over
``app.services.point_import``:

- POST /projects/{id}/import/devices     upsert/add devices into the scene;
- POST /projects/{id}/import/trajectory  resolve waypoints (no scene mutation);
- GET  /import/templates                 self-describing starter payloads.

Both cartesian (local ENU Z-up meters) and geographic (WGS84 lat/lon) points
are accepted and auto-detected per point. Geographic points require the scene's
geodetic anchor (coordinate_system.origin_lat_lon_alt).

Error contract: 400 malformed point / geographic point without a geodetic
anchor; 409 mode='add' id collision; 404 unknown project.
"""

from typing import Any

from fastapi import APIRouter, HTTPException

from seam_studio.api import deps
from seam_studio.core.config import APP_VERSION
from seam_studio.schemas.point_import import (
    DeviceImportRequest,
    DeviceImportResponse,
    TrajectoryImportRequest,
    TrajectoryImportResponse,
)
from seam_studio.services.point_import import (
    IMPORT_TEMPLATES,
    DeviceIdCollisionError,
    GeoAnchorMissingError,
    import_devices,
    resolve_trajectory,
)

router = APIRouter(tags=["import"])


@router.post(
    "/projects/{project_id}/import/devices", response_model=DeviceImportResponse
)
def import_devices_route(
    project_id: str, request: DeviceImportRequest
) -> DeviceImportResponse:
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    try:
        added_ids, updated_ids, warnings = import_devices(project_dir, scene, request)
    except DeviceIdCollisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (GeoAnchorMissingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "device_import",
            "created_by": f"seam-studio/{APP_VERSION} (point_import)",
            "count": len(added_ids) + len(updated_ids),
            "mode": request.mode,
            "added_ids": added_ids,
            "updated_ids": updated_ids,
            "warnings": warnings,
        },
    )
    return DeviceImportResponse(
        added_ids=added_ids, updated_ids=updated_ids, warnings=warnings
    )


@router.post(
    "/projects/{project_id}/import/trajectory",
    response_model=TrajectoryImportResponse,
)
def import_trajectory_route(
    project_id: str, request: TrajectoryImportRequest
) -> TrajectoryImportResponse:
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    try:
        waypoints, orientations, warnings = resolve_trajectory(
            project_dir,
            scene,
            request.points,
            default_agl=request.agl_m,
            ue_id=request.ue_id,
        )
    except (GeoAnchorMissingError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return TrajectoryImportResponse(
        ue_id=request.ue_id,
        waypoints=waypoints,
        # Only surface orientations when at least one waypoint carried one.
        orientations_deg=(orientations if any(o is not None for o in orientations) else None),
        warnings=warnings,
    )


@router.get("/import/templates")
def import_templates() -> dict[str, Any]:
    """Example payloads for both import endpoints plus a field reference, so the
    frontend can offer a downloadable, self-describing starter file."""
    return IMPORT_TEMPLATES
