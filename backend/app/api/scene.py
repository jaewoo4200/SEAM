"""Scene read/write, validation, and project asset serving endpoints."""

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.api import deps
from app.schemas.scene import Scene
from app.schemas.validation import ValidationReport
from app.services.project_store import InvalidAssetPathError, ProjectNotFoundError
from app.services.scene_validator import validate_scene

router = APIRouter(tags=["scene"])

_MEDIA_TYPES = {
    ".glb": "model/gltf-binary",
    ".gltf": "model/gltf+json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".json": "application/json",
    ".xml": "application/xml",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".obj": "text/plain",
    ".ply": "application/octet-stream",
}


@router.get("/projects/{project_id}/scene", response_model=Scene)
def get_scene(project_id: str) -> Scene:
    return deps.load_scene_or_404(deps.get_store(), project_id)


@router.put("/projects/{project_id}/scene", response_model=Scene)
def put_scene(project_id: str, body: Scene) -> Scene:
    store = deps.get_store()
    existing = deps.load_scene_or_404(store, project_id)
    if body.scene_id != existing.scene_id:
        raise HTTPException(
            status_code=400,
            detail=(
                f"scene_id mismatch: project scene is {existing.scene_id!r}, "
                f"body has {body.scene_id!r}"
            ),
        )
    store.save_scene(project_id, body)
    return body


@router.post("/projects/{project_id}/scene/validate", response_model=ValidationReport)
def validate_project_scene(project_id: str) -> ValidationReport:
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    library = store.load_materials(project_id)
    return validate_scene(scene, library, project_dir=project_dir)


@router.get("/projects/{project_id}/assets/{asset_path:path}")
def get_asset(project_id: str, asset_path: str) -> FileResponse:
    store = deps.get_store()
    try:
        path = store.asset_path(project_id, asset_path)
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"project not found: {project_id}"
        )
    except InvalidAssetPathError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail=f"asset not found: {asset_path}"
        )
    media_type = (
        _MEDIA_TYPES.get(path.suffix.lower())
        or mimetypes.guess_type(path.name)[0]
        or "application/octet-stream"
    )
    return FileResponse(path, media_type=media_type)
