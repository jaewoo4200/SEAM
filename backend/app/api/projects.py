"""Project listing, creation, and lookup endpoints."""

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
