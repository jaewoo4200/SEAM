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


def _resolve_node(tm_scene: trimesh.Scene, name: str) -> Optional[str]:
    """Resolve a name to a scene-graph node (group OR geometry node).

    Unlike ``extract_prim_mesh`` resolution this accepts group nodes with no
    geometry of their own (e.g. a "building" parent whose children are the
    per-material regions), so the subtree of a Mode-2 mesh can be scoped.
    """
    if name in tm_scene.graph.nodes:
        return name
    # A geometry name that is not itself a node: fall back to its first node.
    if name in tm_scene.geometry:
        return _first_node_for_geometry(tm_scene, name)
    return None


def _world_mesh_for_node(tm_scene: trimesh.Scene, node_name: str) -> Optional[trimesh.Trimesh]:
    """World-space copy of the geometry instanced at ``node_name`` (or None)."""
    transform, geometry_name = tm_scene.graph[node_name]
    if geometry_name is None:
        return None
    geometry = tm_scene.geometry.get(geometry_name)
    if not isinstance(geometry, trimesh.Trimesh):
        return None
    mesh = geometry.copy()
    if transform is not None:
        mesh.apply_transform(np.asarray(transform, dtype=np.float64))
    return mesh


def extract_face_group_mesh(
    tm_scene: trimesh.Scene, mesh_ref: MeshRef
) -> Optional[trimesh.Trimesh]:
    """Mode-2 intra-mesh split: the sub-mesh named by ``mesh_ref.face_group``.

    ``face_group`` is interpreted as the name of a DESCENDANT geometry/node
    under the node subtree that ``mesh_name`` resolves to (the importer/SAM2
    pipeline emits one child geometry per material region). Resolution, most
    specific first:

    1. an exact geometry name ``f"{mesh_name}/{face_group}"`` (the qualified
       path an exporter may emit for a region);
    2. a descendant NODE whose name equals ``face_group``;
    3. a descendant node whose GEOMETRY name equals ``face_group``;
    4. the bare ``face_group`` as a top-level geometry name, only when it is
       unambiguous (referenced by exactly one node).

    The matched geometry is returned in world coordinates (its instancing
    node's world transform baked into a copy). Returns None when the group
    cannot be resolved, when ``mesh_ref.face_group`` is None, or when the
    match is ambiguous.
    """
    face_group = mesh_ref.face_group
    if face_group is None:
        return None

    # (1) qualified "mesh_name/face_group" geometry name.
    qualified = f"{mesh_ref.mesh_name}/{face_group}"
    if qualified in tm_scene.geometry:
        node = _first_node_for_geometry(tm_scene, qualified)
        if node is not None:
            return _world_mesh_for_node(tm_scene, node)

    root = _resolve_node(tm_scene, mesh_ref.mesh_name)
    if root is not None:
        subtree = tm_scene.graph.transforms.successors(root)
        # (2) descendant node whose name == face_group.
        if face_group in subtree and face_group != root:
            mesh = _world_mesh_for_node(tm_scene, face_group)
            if mesh is not None:
                return mesh
        # (3) descendant node whose geometry name == face_group. Deterministic:
        # nodes are considered in sorted order; the qualified/name paths above
        # already handle the common exporter conventions.
        for node in sorted(subtree):
            if node == root:
                continue
            _, gname = tm_scene.graph[node]
            if gname == face_group:
                return _world_mesh_for_node(tm_scene, node)

    # (4) bare geometry name at any level, only if referenced by exactly one
    # node (unambiguous).
    if face_group in tm_scene.geometry:
        matching = [
            node
            for node in sorted(tm_scene.graph.nodes_geometry)
            if tm_scene.graph[node][1] == face_group
        ]
        if len(matching) == 1:
            return _world_mesh_for_node(tm_scene, matching[0])

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
