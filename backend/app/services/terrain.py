"""Terrain-following for UE trajectories and dataset sweeps.

Given waypoints in the XY plane, snap each point's z to the scene surface
underneath it (raycast straight down onto the visual mesh) plus a height
offset — so a trajectory over sloped ground (e.g. the FTC outdoor terrain)
keeps a constant antenna height instead of running under or over the mesh.

Uses trimesh's pure-python ray casting (no embree needed); fine for the
few-hundred-waypoint scale of trajectories/datasets. The concatenated scene
mesh is cached per (glb path, mtime).
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..schemas.scene import Scene
from .mesh_tools import load_visual_scene

_cache: dict[str, tuple[float, object]] = {}


def _scene_mesh(project_dir: Path, scene: Scene):
    """Concatenated world-space trimesh of the visual asset, or None."""
    uri = (scene.assets.visual_scene_uri if scene.assets else None) or "visual/scene.glb"
    path = project_dir / uri
    if not path.is_file():
        return None
    key = str(path)
    mtime = path.stat().st_mtime
    hit = _cache.get(key)
    if hit is not None and hit[0] == mtime:
        return hit[1]
    tm_scene = load_visual_scene(project_dir, uri)
    if tm_scene is None or len(tm_scene.geometry) == 0:
        return None
    mesh = (
        tm_scene.to_geometry()
        if hasattr(tm_scene, "to_geometry")
        else tm_scene.dump(concatenate=True)
    )
    _cache[key] = (mtime, mesh)
    return mesh


def snap_to_terrain(
    project_dir: Path,
    scene: Scene,
    points: list[list[float]],
    height_m: float,
    warnings: Optional[list[str]] = None,
) -> list[list[float]]:
    """Return points with z = (highest surface under each XY) + height_m.

    Points with no surface underneath keep their original z (and one summary
    warning is appended). Rays start above the mesh top so roofs/terrain are
    hit from outside the geometry.
    """
    import numpy as np

    mesh = _scene_mesh(project_dir, scene)
    if mesh is None:
        if warnings is not None:
            warnings.append("follow_terrain requested but the scene has no visual mesh; z kept")
        return points

    top = float(mesh.bounds[1][2]) + 10.0
    origins = np.array([[p[0], p[1], top] for p in points], dtype=np.float64)
    directions = np.tile([0.0, 0.0, -1.0], (len(points), 1))
    try:
        locations, index_ray, index_tri = mesh.ray.intersects_location(
            ray_origins=origins, ray_directions=directions
        )
    except ImportError as exc:
        # trimesh's ray casting lazily imports rtree's native spatial index;
        # a broken install must degrade to "keep z + warn", not a 500.
        if warnings is not None:
            warnings.append(
                f"follow_terrain unavailable (ray-cast index failed: {exc}); z kept"
            )
        return points
    normals = mesh.face_normals

    best_z: dict[int, float] = {}
    for loc, ray_i, tri_i in zip(locations, index_ray, index_tri):
        # Only upward-facing surfaces are walkable: skips ceilings/undersides
        # so open-topped indoor scans snap to the floor, not the ceiling slab.
        if float(normals[int(tri_i)][2]) <= 0.1:
            continue
        z = float(loc[2])
        i = int(ray_i)
        # Highest walkable hit = roof/terrain, not a floor beneath it. Closed
        # indoor rooms are better served with follow_terrain off (the roof is
        # the highest upward face there).
        if i not in best_z or z > best_z[i]:
            best_z[i] = z

    missed = 0
    out: list[list[float]] = []
    for i, p in enumerate(points):
        if i in best_z:
            out.append([float(p[0]), float(p[1]), best_z[i] + height_m])
        else:
            missed += 1
            out.append([float(p[0]), float(p[1]), float(p[2])])
    if missed and warnings is not None:
        warnings.append(
            f"follow_terrain: {missed}/{len(points)} waypoints have no surface "
            "underneath (outside the mesh footprint); their z was kept as given"
        )
    return out
