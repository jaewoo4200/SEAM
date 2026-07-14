"""Project listing, creation, lookup, duplication, rename, and deletion endpoints."""

import shutil
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import Field

from seam_studio.api import deps
from seam_studio.schemas.common import StrictModel
from seam_studio.schemas.projects import ProjectCreateRequest, ProjectInfo
from seam_studio.services.project_store import (
    PROJECT_SUFFIX,
    PROJECT_SUFFIXES,
    ProjectNotFoundError,
    ProjectStore,
    slugify,
)

router = APIRouter(tags=["projects"])


class ProjectDuplicateRequest(StrictModel):
    """Body for POST /projects/{id}/duplicate. Both fields optional.

    Defined here rather than in app.schemas.projects because the model is
    purely a transport detail of this route (nothing else consumes it).
    """

    # Same id alphabet create_project enforces; derived from the source id
    # ("<id>_copy", "<id>_copy2", ...) when omitted.
    new_id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_\-]+$")
    # New display name for the copied scene; source name kept when omitted.
    name: Optional[str] = None


class ProjectRenameRequest(StrictModel):
    """Body for PATCH /projects/{id}."""

    name: str = Field(min_length=1)


@router.get("/projects", response_model=list[ProjectInfo])
def list_projects() -> list[ProjectInfo]:
    return deps.get_store().list_projects()


@router.post("/projects", response_model=ProjectInfo, status_code=201)
def create_project(body: ProjectCreateRequest) -> ProjectInfo:
    """Create a new project folder under the first configured project root.

    ``template="demo"`` generates the Sample Demo content (toy urban scene
    GLB, TX/RX pair, car + pedestrian actors) programmatically — this is how
    a pip install gets its first project without shipping binary assets.
    """
    store = deps.get_store()
    try:
        if body.template == "demo":
            # Lazy import: the generator pulls in trimesh mesh building, which
            # plain project CRUD never needs.
            from seam_studio.services.demo_project import create_demo_project

            project_dir = create_demo_project(
                store, project_id=body.project_id or "sample_demo", name=body.name
            )
            return store.info(project_dir)
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


def _id_in_use(store: ProjectStore, dest_parent: Path, candidate: str) -> bool:
    """True when ``candidate`` cannot become a new project id.

    Checks both the store's id space (any root, either suffix) and the raw
    destination folder names: a stray ``<id>.seam``/``<id>.sionnatwin`` dir
    without a scene file is invisible to resolve() but would still make
    copytree fail, so it counts as taken too.
    """
    try:
        store.resolve(candidate)
        return True
    except ProjectNotFoundError:
        pass
    return any(
        (dest_parent / f"{candidate}{suffix}").exists() for suffix in PROJECT_SUFFIXES
    )


@router.post(
    "/projects/{project_id}/duplicate", response_model=ProjectInfo, status_code=201
)
def duplicate_project(
    project_id: str, body: Optional[ProjectDuplicateRequest] = None
) -> ProjectInfo:
    """Fork a project: copy its whole folder to a sibling under the same root.

    EVERYTHING is copied - visual assets, rf/ artifacts, results/, ai/ logs,
    provenance - so the duplicate is a true fork of the project's full state.
    The copy always gets a modern ``<new_id>.seam`` folder name (even when the
    source is a legacy ``.sionnatwin``); the scene file inside keeps whatever
    filename was copied, which the store loads and saves back transparently.
    After the copy, the scene's scene_id (and name, if provided) is rewritten
    via the store so the fork is self-consistent (PUT /scene id checks pass).
    No in-memory store state needs cloning: the store rescans roots per call,
    and per-project locks/live overlays are keyed by id, which the fork does
    not share.
    """
    store = deps.get_store()
    try:
        src_dir = store.resolve(project_id)
    except ProjectNotFoundError:
        raise HTTPException(
            status_code=404, detail=f"project not found: {project_id}"
        )
    # The copy lands next to the source. When the source folder IS a configured
    # root, a sibling would fall outside every root and never be discovered.
    if any(src_dir.resolve() == Path(r).resolve() for r in store.roots):
        raise HTTPException(
            status_code=400,
            detail="cannot duplicate a project that is itself a project root",
        )
    dest_parent = src_dir.parent

    if body is not None and body.new_id is not None:
        new_id = body.new_id
        if _id_in_use(store, dest_parent, new_id):
            raise HTTPException(
                status_code=409, detail=f"project already exists: {new_id}"
            )
    else:
        # "<old>_copy", then "_copy2", "_copy3", ... slugified so a legacy id
        # with characters outside the create_project alphabet still yields a
        # conforming new id.
        base = slugify(f"{project_id}_copy")
        new_id, n = base, 2
        while _id_in_use(store, dest_parent, new_id):
            new_id = f"{base}{n}"
            n += 1

    new_name = None
    if body is not None and body.name is not None:
        new_name = body.name.strip()
        if not new_name:
            raise HTTPException(
                status_code=422, detail="name must be non-empty"
            )

    dst_dir = dest_parent / f"{new_id}{PROJECT_SUFFIX}"
    try:
        shutil.copytree(src_dir, dst_dir)
    except FileExistsError:  # lost a race with a concurrent duplicate/create
        raise HTTPException(
            status_code=409, detail=f"project already exists: {new_id}"
        )

    scene = store.load_scene(new_id)
    scene.scene_id = new_id
    if new_name is not None:
        scene.name = new_name
    store.save_scene(new_id, scene)
    store.append_provenance(
        new_id, {"type": "project_duplicated", "source_project_id": project_id}
    )
    return store.info(dst_dir)


@router.patch("/projects/{project_id}", response_model=ProjectInfo)
def rename_project(project_id: str, body: ProjectRenameRequest) -> ProjectInfo:
    """Rename a project's scene (display name only - the folder/id is stable)."""
    name = body.name.strip()
    if not name:
        raise HTTPException(status_code=422, detail="name must be non-empty")
    store = deps.get_store()
    scene = deps.load_scene_or_404(store, project_id)
    scene.name = name
    store.save_scene(project_id, scene)
    return store.info(store.resolve(project_id))


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
