"""World-space AABB of a project's visual scene.

The single source of truth for "how big is this scene" — used to seed
sensible defaults for dataset sampling regions, trajectory endpoints, and
radio-map extents instead of hardcoded ±50 m guesses that silently fall
outside small indoor scenes (audit: all-zero datasets).

Bounds come from the visual GLB (same asset the viewer renders, already
Z-up world coordinates). Cached per (path, mtime) so repeated UI queries
don't re-parse the mesh.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..schemas.scene import Scene, SceneBounds
from .mesh_tools import load_visual_scene

_cache: dict[str, tuple[float, SceneBounds]] = {}


def compute_scene_bounds(project_dir: Path, scene: Scene) -> Optional[SceneBounds]:
    """AABB of the visual asset (plus devices/actors so nothing sits outside).

    Returns None when the project has no loadable visual mesh — callers fall
    back to device positions or tell the user to import geometry first.
    """
    uri = (scene.assets.visual_scene_uri if scene.assets else None) or "visual/scene.glb"
    path = project_dir / uri
    key = str(path)
    mtime = path.stat().st_mtime if path.is_file() else None

    bounds: Optional[SceneBounds] = None
    if mtime is not None:
        hit = _cache.get(key)
        if hit is not None and hit[0] == mtime:
            bounds = hit[1]
        else:
            tm_scene = load_visual_scene(project_dir, uri)
            if tm_scene is not None and len(tm_scene.geometry) > 0:
                lo, hi = tm_scene.bounds  # world-space, transforms baked by trimesh
                bounds = SceneBounds(
                    min=[float(v) for v in lo],
                    max=[float(v) for v in hi],
                )
                _cache[key] = (mtime, bounds)

    # Merge device/actor positions so bounds cover everything interactable
    # even when the mesh is missing (mock-only projects).
    pts = [d.position for d in scene.devices] + [a.position for a in scene.actors]
    if bounds is None:
        if not pts:
            return None
        lo = [min(p[i] for p in pts) for i in range(3)]
        hi = [max(p[i] for p in pts) for i in range(3)]
        bounds = SceneBounds(min=lo, max=hi)
    elif pts:
        bounds = SceneBounds(
            min=[min(bounds.min[i], min(p[i] for p in pts)) for i in range(3)],
            max=[max(bounds.max[i], max(p[i] for p in pts)) for i in range(3)],
        )
    return bounds
