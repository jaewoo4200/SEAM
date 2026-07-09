"""Shared FastAPI dependencies."""

from functools import lru_cache

from fastapi import HTTPException

from app.schemas.scene import Scene
from app.services.project_store import ProjectNotFoundError, ProjectStore


@lru_cache(maxsize=1)
def get_store() -> ProjectStore:
    return ProjectStore()


def load_scene_or_404(store: ProjectStore, project_id: str) -> Scene:
    """Load the scene AS STORED (no live overlay).

    Write-path endpoints (device edits, material assigns, /live/state itself)
    must start from disk truth so they never accidentally persist an ephemeral
    live-state delta. Read paths that should follow live positions use
    :func:`load_scene_live` instead.
    """
    try:
        return store.load_scene(project_id)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")


def load_scene_live(store: ProjectStore, project_id: str) -> Scene:
    """Load the scene WITH the ephemeral live-state overlay applied.

    Used by read-only consumers — GET /scene (viewer Live sync polling) and the
    simulate endpoints (so periodic re-solves follow externally-pushed
    positions) — that never write the scene back. The overlay holds only
    non-persisted ``POST /live/state`` deltas and is cleared on any
    authoritative save (see services/live_state.py).
    """
    from app.services import live_state

    return live_state.apply_overlay(project_id, load_scene_or_404(store, project_id))
