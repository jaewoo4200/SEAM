"""Task F: compiler intra-mesh face-group split (Mode 2).

A prim's ``mesh_ref.face_group`` names a descendant geometry/node under the
subtree that ``mesh_name`` resolves to. The compiler must project each named
region as its own material group; an unresolvable group must degrade to the
whole mesh with a spec-aligned ``NO_FACE_GROUP`` warning, never crash.
"""

import json
from pathlib import Path

import numpy as np
import trimesh

from app.schemas.materials import RFMaterialLibrary
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene
from app.services import mesh_tools, project_store
from app.services.rf_compiler import compile_project

BUILDING = "building"
WALL_FACES = 12   # a bare box
WIN_FACES = 48    # one subdivision of a box (4x)

WALL_ID = "/buildings/b01/wall"
WIN_ID = "/buildings/b01/win"


def _build_grouped_glb(project_dir: Path) -> None:
    """Parent node "building" with child geometries "wall" and "win".

    The two children have distinct triangle counts so a face-group split can be
    told apart from the whole-mesh fallback purely by ``face_count``. The
    parent is wired under the scene root so the GLB exports.
    """
    wall = trimesh.creation.box(extents=[4.0, 0.2, 3.0])
    wall.apply_translation([0.0, 0.0, 1.5])
    win = trimesh.creation.box(extents=[1.0, 0.05, 1.0]).subdivide()
    win.apply_translation([2.0, 1.0, 1.5])
    assert len(wall.faces) == WALL_FACES
    assert len(win.faces) == WIN_FACES

    tm_scene = trimesh.Scene()
    tm_scene.add_geometry(
        wall, geom_name="wall", node_name="wall", parent_node_name=BUILDING
    )
    tm_scene.add_geometry(
        win, geom_name="win", node_name="win", parent_node_name=BUILDING
    )
    # world -> building -> {wall, win}; connect building to the root so export
    # can resolve every node's parent chain.
    tm_scene.graph.update(
        frame_from="world", frame_to=BUILDING, matrix=np.eye(4)
    )
    visual = project_dir / "visual"
    visual.mkdir(parents=True, exist_ok=True)
    tm_scene.export(visual / "scene.glb")


def _split_scene() -> Scene:
    """Two prims sharing mesh_name "building", split by face_group + material."""
    return Scene(
        scene_id="face_group_test",
        name="Face Group Test",
        prims=[
            Prim(
                id=WALL_ID,
                name="wall",
                mesh_ref=MeshRef(mesh_name=BUILDING, face_group="wall"),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
            Prim(
                id=WIN_ID,
                name="win",
                mesh_ref=MeshRef(mesh_name=BUILDING, face_group="win"),
                rf=RFBinding(
                    material_id="itu_glass",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
    )


def _library() -> RFMaterialLibrary:
    return project_store.load_default_library()


def _project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "face_group_test.sionnatwin"
    _build_grouped_glb(project_dir)
    return project_dir


# ---------------------------------------------------------- mesh_tools unit


def test_extract_face_group_by_geometry_name(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    tm_scene = mesh_tools.load_visual_scene(project_dir, "visual/scene.glb")
    assert tm_scene is not None

    wall = mesh_tools.extract_face_group_mesh(
        tm_scene, MeshRef(mesh_name=BUILDING, face_group="wall")
    )
    win = mesh_tools.extract_face_group_mesh(
        tm_scene, MeshRef(mesh_name=BUILDING, face_group="win")
    )
    assert wall is not None and len(wall.faces) == WALL_FACES
    assert win is not None and len(win.faces) == WIN_FACES
    # World coordinates: baked translations survive the split.
    assert np.allclose(wall.bounds.mean(axis=0), [0.0, 0.0, 1.5], atol=1e-5)
    assert np.allclose(win.bounds.mean(axis=0), [2.0, 1.0, 1.5], atol=1e-5)


def test_extract_face_group_none_when_unresolvable(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    tm_scene = mesh_tools.load_visual_scene(project_dir, "visual/scene.glb")
    assert tm_scene is not None

    assert (
        mesh_tools.extract_face_group_mesh(
            tm_scene, MeshRef(mesh_name=BUILDING, face_group="does_not_exist")
        )
        is None
    )
    # No face_group -> nothing to split.
    assert (
        mesh_tools.extract_face_group_mesh(
            tm_scene, MeshRef(mesh_name=BUILDING, face_group=None)
        )
        is None
    )


def test_extract_face_group_qualified_and_bare_names(tmp_path: Path) -> None:
    """Exporters may emit region geometry as "mesh_name/face_group"."""
    wall = trimesh.creation.box(extents=[4.0, 0.2, 3.0])
    win = trimesh.creation.box(extents=[1.0, 0.05, 1.0]).subdivide()
    tm_scene = trimesh.Scene()
    tm_scene.add_geometry(
        wall, geom_name="building/wall", node_name="n_wall", parent_node_name=BUILDING
    )
    tm_scene.add_geometry(
        win, geom_name="building/win", node_name="n_win", parent_node_name=BUILDING
    )
    tm_scene.graph.update(frame_from="world", frame_to=BUILDING, matrix=np.eye(4))
    project_dir = tmp_path / "qualified.sionnatwin"
    (project_dir / "visual").mkdir(parents=True)
    tm_scene.export(project_dir / "visual" / "scene.glb")

    loaded = mesh_tools.load_visual_scene(project_dir, "visual/scene.glb")
    assert loaded is not None

    # Qualified "building/wall" resolved from mesh_name + face_group.
    qualified = mesh_tools.extract_face_group_mesh(
        loaded, MeshRef(mesh_name=BUILDING, face_group="wall")
    )
    assert qualified is not None and len(qualified.faces) == WALL_FACES
    # Bare unambiguous geometry name, even when mesh_name does not scope it.
    bare = mesh_tools.extract_face_group_mesh(
        loaded, MeshRef(mesh_name="nonexistent", face_group="building/win")
    )
    assert bare is not None and len(bare.faces) == WIN_FACES


# ------------------------------------------------------------- compile flow


def test_compile_splits_face_groups(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    result = compile_project(project_dir, _split_scene(), _library())

    assert result.ok is True
    assert result.skipped_prim_ids == []
    # No warnings: both groups resolved cleanly (spec: "log nothing" on success).
    assert not any("NO_FACE_GROUP" in w for w in result.warnings)

    by_mat = {g.rf_material_id: g for g in result.material_groups}
    assert set(by_mat) == {"itu_concrete", "itu_glass"}
    assert by_mat["itu_concrete"].prim_ids == [WALL_ID]
    assert by_mat["itu_glass"].prim_ids == [WIN_ID]

    # PLY triangle counts match the individual children, proving the split
    # used the sub-mesh and not the whole "building" subtree.
    assert by_mat["itu_concrete"].face_count == WALL_FACES
    assert by_mat["itu_glass"].face_count == WIN_FACES
    concrete_ply = trimesh.load_mesh(project_dir / by_mat["itu_concrete"].mesh_file)
    glass_ply = trimesh.load_mesh(project_dir / by_mat["itu_glass"].mesh_file)
    assert len(concrete_ply.faces) == WALL_FACES
    assert len(glass_ply.faces) == WIN_FACES


def test_unresolvable_face_group_falls_back_to_whole_mesh(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    # mesh_name "wall" is a real leaf; face_group is bogus -> whole-mesh fallback.
    scene = Scene(
        scene_id="fallback_test",
        name="Fallback Test",
        prims=[
            Prim(
                id=WALL_ID,
                name="wall",
                mesh_ref=MeshRef(mesh_name="wall", face_group="ghost_region"),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            )
        ],
    )
    result = compile_project(project_dir, scene, _library())

    assert result.ok is True
    assert WALL_ID not in result.skipped_prim_ids
    # Spec-aligned code; warning retains the prim id and the unresolved group.
    fallback = [w for w in result.warnings if w.startswith("NO_FACE_GROUP: ")]
    assert len(fallback) == 1
    assert WALL_ID in fallback[0]
    assert "ghost_region" in fallback[0]

    group = result.material_groups[0]
    assert group.rf_material_id == "itu_concrete"
    assert group.face_count == WALL_FACES  # whole "wall" mesh used


def test_face_group_split_is_deterministic(tmp_path: Path) -> None:
    project_dir = _project(tmp_path)
    library = _library()

    compile_project(project_dir, _split_scene(), library)
    xml_first = (project_dir / "rf" / "generated_scene.xml").read_bytes()
    manifest_first = (project_dir / "rf" / "compile_manifest.json").read_bytes()

    compile_project(project_dir, _split_scene(), library)
    assert (project_dir / "rf" / "generated_scene.xml").read_bytes() == xml_first
    assert (project_dir / "rf" / "compile_manifest.json").read_bytes() == manifest_first

    # Manifest face counts reflect the per-child split, not the merged subtree.
    manifest = json.loads(manifest_first.decode("utf-8"))
    counts = {g["rf_material_id"]: g["face_count"] for g in manifest["groups"]}
    assert counts == {"itu_concrete": WALL_FACES, "itu_glass": WIN_FACES}
