"""Scene read/write, validation, and project asset serving endpoints."""

import mimetypes

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.api import deps
from app.services.events import publish_event
from app.schemas.scene import Scene, SceneBounds
from app.schemas.validation import ValidationReport
from app.services.project_store import InvalidAssetPathError, ProjectNotFoundError
from app.services.scene_bounds import compute_scene_bounds
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
    # Live overlay applied so the viewer's Live sync polling follows external
    # (persist=false) /live/state pushes without the scene file changing.
    return deps.load_scene_live(deps.get_store(), project_id)


@router.get("/projects/{project_id}/scene/bounds", response_model=SceneBounds)
def get_scene_bounds(project_id: str) -> SceneBounds:
    """World-space AABB of the visual scene (devices/actors merged in).

    Lets the UI seed dataset sampling regions, trajectory endpoints, and
    placement defaults from real geometry. 404s when the project has neither
    a visual mesh nor any devices to bound.
    """
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    bounds = compute_scene_bounds(store.resolve(project_id), scene)
    if bounds is None:
        raise HTTPException(
            status_code=404,
            detail="scene has no visual mesh or devices to compute bounds from",
        )
    return bounds


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
    # Optimistic concurrency: a client that round-trips the scene carries the
    # revision it fetched; a stale write (another tab / external script saved
    # meanwhile) gets 409 instead of silently clobbering the newer state.
    # Clients that send no revision (older tools, tests) skip the check.
    if (
        body.revision is not None
        and existing.revision is not None
        and body.revision != existing.revision
    ):
        raise HTTPException(
            status_code=409,
            detail=(
                f"scene changed on disk (revision {existing.revision}, "
                f"you had {body.revision}) — refresh and retry"
            ),
        )
    saved = store.save_scene(project_id, body)
    publish_event(project_id, {"type": "scene_saved", "revision": saved.revision})
    return saved


@router.post("/projects/{project_id}/scene/restore", response_model=Scene)
def restore_scene(project_id: str, steps: int = 1) -> Scene:
    """Undo: make the steps-th newest history snapshot the current scene."""
    store = deps.get_store()
    deps.load_scene_or_404(store, project_id)  # 404 for unknown projects
    try:
        scene = store.restore_scene(project_id, steps=steps)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    store.append_provenance(
        project_id, {"type": "scene_restore", "steps": steps}
    )
    publish_event(project_id, {"type": "scene_saved", "revision": scene.revision})
    return scene


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
