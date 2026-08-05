"""Frame-indexed sensor data (GT side of the playback dashboard, issue #3).

    GET /projects/{project_id}/sensors -> SensorManifestResponse

Frame files themselves are served by the existing asset route, so this only
hands out the manifest, the detected drive segments, and load warnings. 404
when the project carries no ``sensor_data/manifest.json`` (most don't); 422
when it carries one that cannot be parsed.
"""

from fastapi import APIRouter, HTTPException

from seam_studio.api import deps
from seam_studio.schemas.sensors import SensorManifestResponse
from seam_studio.services.project_store import ProjectNotFoundError
from seam_studio.services.sensor_store import (
    MANIFEST_REL,
    load_manifest,
    manifest_response,
)

router = APIRouter(tags=["sensors"])


@router.get("/projects/{project_id}/sensors", response_model=SensorManifestResponse)
def get_sensors(project_id: str) -> SensorManifestResponse:
    store = deps.get_store()
    try:
        project_dir = store.resolve(project_id)
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"project not found: {project_id}"
        )
    try:
        manifest = load_manifest(project_dir)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc))
    if manifest is None:
        raise HTTPException(
            status_code=404, detail=f"no sensor data: {MANIFEST_REL} not found"
        )
    return manifest_response(project_dir, manifest)
