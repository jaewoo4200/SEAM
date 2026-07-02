"""Trimesh helpers used by the RF projection compiler.

All meshes returned here are in world coordinates (Z-up ENU meters): node
transforms from the visual asset's scene graph are baked into a copy of the
geometry so exported RF submeshes align with the canonical scene.
"""

from pathlib import Path
from typing import Optional

import numpy as np
import trimesh

from app.schemas.scene import MeshRef


def load_visual_scene(project_dir: Path, asset_uri: str) -> Optional[trimesh.Scene]:
    """Load the visual asset as a trimesh Scene, or None when the file is missing.

    ``force="scene"`` so single-mesh files still come back as a Scene; callers
    turn a None into a structured warning instead of crashing (HANDOFF 10.1).
    """
    path = project_dir / asset_uri
    if not path.is_file():
        return None
    return trimesh.load(path, force="scene")


def _first_node_for_geometry(tm_scene: trimesh.Scene, geometry_name: str) -> Optional[str]:
    for node in sorted(tm_scene.graph.nodes_geometry):
        _, gname = tm_scene.graph[node]
        if gname == geometry_name:
            return node
    return None


def extract_prim_mesh(tm_scene: trimesh.Scene, mesh_ref: MeshRef) -> Optional[trimesh.Trimesh]:
    """Resolve a MeshRef against the loaded visual scene.

    Resolution order: geometry name == mesh_name, else scene-graph node name
    == mesh_name. The instancing node's world transform is applied to a COPY
    of the geometry so the returned mesh is in world coordinates.

    face_group handling is the MVP Mode 2 placeholder: the whole mesh is
    returned and the caller is expected to add a warning.
    """
    name = mesh_ref.mesh_name
    geometry_name: Optional[str] = None
    node_name: Optional[str] = None

    if name in tm_scene.geometry:
        geometry_name = name
        node_name = _first_node_for_geometry(tm_scene, name)
    else:
        for node in sorted(tm_scene.graph.nodes_geometry):
            if node == name:
                _, geometry_name = tm_scene.graph[node]
                node_name = node
                break

    if geometry_name is None:
        return None
    geometry = tm_scene.geometry.get(geometry_name)
    if not isinstance(geometry, trimesh.Trimesh):
        return None

    mesh = geometry.copy()
    if node_name is not None:
        transform, _ = tm_scene.graph[node_name]
        if transform is not None:
            mesh.apply_transform(np.asarray(transform, dtype=np.float64))
    return mesh


def concatenate_meshes(meshes: list[trimesh.Trimesh]) -> trimesh.Trimesh:
    """Concatenate world-space meshes into one Trimesh for group export."""
    if len(meshes) == 1:
        return meshes[0]
    return trimesh.util.concatenate(meshes)
