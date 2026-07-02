"""Shared FastAPI dependencies."""

from functools import lru_cache

from fastapi import HTTPException

from app.schemas.scene import Scene
from app.services.project_store import ProjectNotFoundError, ProjectStore


@lru_cache(maxsize=1)
def get_store() -> ProjectStore:
    return ProjectStore()


def load_scene_or_404(store: ProjectStore, project_id: str) -> Scene:
    try:
        return store.load_scene(project_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
