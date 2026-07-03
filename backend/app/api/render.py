"""Scene render endpoint.

POST /projects/{project_id}/render -> a Mitsuba path-traced PNG (FileResponse).

This is the file-export counterpart to Sionna RT's interactive scene preview:
the compiled RF projection is rendered in-process by Mitsuba from a caller-
supplied camera and returned as an ``image/png`` download. The absolute path of
the written file is echoed in the ``X-Render-Path`` response header.

Error mapping mirrors the other domain endpoints:
  * Mitsuba/Sionna unavailable        -> 409 (RenderUnavailableError)
  * unknown project                   -> 404 (load_scene_or_404)
  * scene won't compile/load/render   -> 400 (RenderSceneError)
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from app.api.deps import get_store, load_scene_or_404
from app.schemas.render import RenderRequest
from app.services.scene_render import (
    RenderSceneError,
    RenderUnavailableError,
    render_scene,
)

router = APIRouter(tags=["render"])


@router.post("/projects/{project_id}/render")
def render_project(project_id: str, request: RenderRequest) -> FileResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    project_dir = store.resolve(project_id)

    try:
        png_path = render_scene(project_dir, scene, library, request)
    except RenderUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except RenderSceneError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    store.append_provenance(
        project_id,
        {"type": "render", "file": str(png_path), "spp": request.spp},
    )
    return FileResponse(
        png_path,
        media_type="image/png",
        filename=png_path.name,
        headers={"X-Render-Path": str(png_path)},
    )
