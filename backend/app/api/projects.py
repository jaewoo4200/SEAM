"""Project listing, creation, lookup, and deletion endpoints."""

import shutil

from fastapi import APIRouter, HTTPException

from app.api import deps
from app.schemas.projects import ProjectCreateRequest, ProjectInfo
from app.services.project_store import ProjectNotFoundError

router = APIRouter(tags=["projects"])


@router.get("/projects", response_model=list[ProjectInfo])
def list_projects() -> list[ProjectInfo]:
    return deps.get_store().list_projects()


@router.post("/projects", response_model=ProjectInfo, status_code=201)
def create_project(body: ProjectCreateRequest) -> ProjectInfo:
    """Create a new project folder under the first configured project root.

    ``template`` is accepted for forward compatibility, but only "empty" is
    materialized today: "demo" also yields an empty project (the demo content
    generator lives in examples/scripts and is not wired into the API yet).
    """
    store = deps.get_store()
    try:
        return store.create_project(name=body.name, project_id=body.project_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/projects/{project_id}", response_model=ProjectInfo)
def get_project(project_id: str) -> ProjectInfo:
    store = deps.get_store()
    try:
        project_dir = store.resolve(project_id)
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"project not found: {project_id}"
        )
    return store.info(project_dir)


@router.delete("/projects/{project_id}")
def delete_project(project_id: str) -> dict:
    """Permanently remove a project folder from disk.

    The project id is resolved through the store (which only ever yields
    directories that live under a configured project root), so a caller can
    never point deletion at an arbitrary path via id traversal. The sionna
    backend's scene caches are keyed by generated-XML path + mtime and
    self-heal, so nothing here needs to touch them; the store keeps no
    in-memory project listing cache (``list_projects`` rescans the roots each
    call), so there is nothing to invalidate beyond the folder itself.
    """
    store = deps.get_store()
    try:
        project_dir = store.resolve(project_id)
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"project not found: {project_id}"
        )
    shutil.rmtree(project_dir)
    return {"deleted": True, "project_id": project_id}
