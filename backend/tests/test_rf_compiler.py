"""Tests for mesh_tools + rf_compiler (RF projection compile, HANDOFF sec 10)."""

import json
import xml.etree.ElementTree as ET
from pathlib import Path

import numpy as np
import pytest
import trimesh

from app.schemas.materials import RFMaterialLibrary
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene
from app.services import mesh_tools, project_store
from app.services.rf_compiler import compile_project

WALL_ID = "/buildings/b01/wall"
GLASS_ID = "/buildings/b01/glass"
UNASSIGNED_ID = "/buildings/b01/misc"


def _build_glb(project_dir: Path) -> None:
    """Two named boxes with baked world transforms, exported as GLB."""
    wall = trimesh.creation.box(extents=[4.0, 0.2, 3.0])
    wall.apply_translation([0.0, 0.0, 1.5])
    glass = trimesh.creation.box(extents=[1.0, 0.05, 1.0])
    glass.apply_translation([2.0, 1.0, 1.5])
    tm_scene = trimesh.Scene()
    tm_scene.add_geometry(wall, geom_name="wall_box", node_name="wall_box")
    tm_scene.add_geometry(glass, geom_name="glass_box", node_name="glass_box")
    visual = project_dir / "visual"
    visual.mkdir(parents=True, exist_ok=True)
    tm_scene.export(visual / "scene.glb")


def _build_scene() -> Scene:
    return Scene(
        scene_id="compiler_test",
        name="Compiler Test",
        prims=[
            Prim(
                id=WALL_ID,
                name="wall",
                mesh_ref=MeshRef(mesh_name="wall_box"),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
            Prim(
                id=GLASS_ID,
                name="glass",
                mesh_ref=MeshRef(mesh_name="glass_box"),
                rf=RFBinding(
                    material_id="itu_glass",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
            Prim(id=UNASSIGNED_ID, name="misc", mesh_ref=MeshRef(mesh_name="wall_box")),
        ],
    )


@pytest.fixture()
def library() -> RFMaterialLibrary:
    return project_store.load_default_library()


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    project_dir = tmp_path / "compiler_test.sionnatwin"
    _build_glb(project_dir)
    return project_dir


def test_compile_groups_and_meshes(project: Path, library: RFMaterialLibrary) -> None:
    result = compile_project(project, _build_scene(), library)

    assert result.ok is True
    assert result.errors == []
    assert [g.rf_material_id for g in result.material_groups] == [
        "itu_concrete",
        "itu_glass",
    ]

    concrete, glass = result.material_groups
    assert concrete.prim_ids == [WALL_ID]
    assert glass.prim_ids == [GLASS_ID]
    assert concrete.mesh_file == "rf/meshes/itu_concrete.ply"
    assert glass.mesh_file == "rf/meshes/itu_glass.ply"

    for group in result.material_groups:
        ply_path = project / group.mesh_file
        assert ply_path.is_file()
        mesh = trimesh.load_mesh(ply_path)
        assert len(mesh.faces) > 0
        assert group.face_count == len(mesh.faces)


def test_compile_world_coordinates(project: Path, library: RFMaterialLibrary) -> None:
    result = compile_project(project, _build_scene(), library)
    glass_mesh = trimesh.load_mesh(project / result.material_groups[1].mesh_file)
    # glass_box was translated to [2, 1, 1.5] before export (baked transform).
    assert np.allclose(glass_mesh.bounds.mean(axis=0), [2.0, 1.0, 1.5], atol=1e-5)


def test_generated_xml(project: Path, library: RFMaterialLibrary) -> None:
    result = compile_project(project, _build_scene(), library)
    assert result.scene_xml == "rf/generated_scene.xml"

    root = ET.parse(project / "rf" / "generated_scene.xml").getroot()
    assert root.tag == "scene"
    assert root.attrib["version"] == "2.1.0"

    bsdf_ids = [b.attrib["id"] for b in root.findall("bsdf")]
    assert bsdf_ids == ["mat-itu_concrete", "mat-itu_glass"]

    shapes = root.findall("shape")
    assert [s.attrib["id"] for s in shapes] == ["shape-itu_concrete", "shape-itu_glass"]
    filenames = [s.find("string").attrib["value"] for s in shapes]
    assert filenames == ["meshes/itu_concrete.ply", "meshes/itu_glass.ply"]
    for shape in shapes:
        ref = shape.find("ref")
        assert ref.attrib["name"] == "bsdf"
        assert ref.attrib["id"] == shape.attrib["id"].replace("shape-", "mat-")
        assert shape.find("boolean").attrib == {"name": "face_normals", "value": "true"}


def test_unassigned_prim_skipped(project: Path, library: RFMaterialLibrary) -> None:
    result = compile_project(project, _build_scene(), library)
    assert UNASSIGNED_ID in result.skipped_prim_ids
    assert any(UNASSIGNED_ID in w for w in result.warnings)
    for group in result.material_groups:
        assert UNASSIGNED_ID not in group.prim_ids


def test_manifest_and_mappings(project: Path, library: RFMaterialLibrary) -> None:
    result = compile_project(project, _build_scene(), library)
    assert result.manifest == "rf/compile_manifest.json"

    manifest = json.loads(
        (project / "rf" / "compile_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["scene_id"] == "compiler_test"
    assert [g["rf_material_id"] for g in manifest["groups"]] == [
        "itu_concrete",
        "itu_glass",
    ]
    for group in manifest["groups"]:
        # itu_* library materials are not "constant" model -> no custom params.
        assert group["custom_material"] is None
        assert group["face_count"] > 0
    assert UNASSIGNED_ID in manifest["skipped_prim_ids"]

    object_map = json.loads(
        (project / "mapping" / "object_map.json").read_text(encoding="utf-8")
    )
    assert object_map[WALL_ID] == {
        "mesh_name": "wall_box",
        "rf_material_id": "itu_concrete",
        "group_mesh_file": "rf/meshes/itu_concrete.ply",
    }
    assert object_map[GLASS_ID]["rf_material_id"] == "itu_glass"

    face_group_map = json.loads(
        (project / "mapping" / "face_group_map.json").read_text(encoding="utf-8")
    )
    assert face_group_map[WALL_ID] is None
    assert face_group_map[GLASS_ID] is None

    for rel in result.generated_files:
        assert (project / rel).is_file()


def test_compile_is_deterministic(project: Path, library: RFMaterialLibrary) -> None:
    compile_project(project, _build_scene(), library)
    xml_first = (project / "rf" / "generated_scene.xml").read_bytes()
    manifest_first = (project / "rf" / "compile_manifest.json").read_bytes()

    compile_project(project, _build_scene(), library)
    assert (project / "rf" / "generated_scene.xml").read_bytes() == xml_first
    assert (project / "rf" / "compile_manifest.json").read_bytes() == manifest_first


def test_stale_group_meshes_removed(project: Path, library: RFMaterialLibrary) -> None:
    compile_project(project, _build_scene(), library)
    assert (project / "rf" / "meshes" / "itu_glass.ply").is_file()

    scene = _build_scene()
    scene.prims = [p for p in scene.prims if p.id != GLASS_ID]
    result = compile_project(project, scene, library)

    assert [g.rf_material_id for g in result.material_groups] == ["itu_concrete"]
    assert not (project / "rf" / "meshes" / "itu_glass.ply").exists()
    assert (project / "rf" / "meshes" / "itu_concrete.ply").is_file()


def test_missing_visual_asset_placeholder(
    tmp_path: Path, library: RFMaterialLibrary
) -> None:
    project_dir = tmp_path / "empty_project.sionnatwin"
    project_dir.mkdir()

    result = compile_project(project_dir, _build_scene(), library)

    assert result.ok is True
    assert result.material_groups == []
    assert any("visual/scene.glb" in w for w in result.warnings)
    # All three prims skipped: two lost their asset, one had no RF material.
    assert set(result.skipped_prim_ids) == {WALL_ID, GLASS_ID, UNASSIGNED_ID}

    root = ET.parse(project_dir / "rf" / "generated_scene.xml").getroot()
    assert root.findall("shape") == []
    manifest = json.loads(
        (project_dir / "rf" / "compile_manifest.json").read_text(encoding="utf-8")
    )
    assert manifest["groups"] == []


def test_face_group_placeholder_warning(project: Path, library: RFMaterialLibrary) -> None:
    scene = _build_scene()
    scene.prims[0].mesh_ref = MeshRef(mesh_name="wall_box", face_group="wall_south")
    result = compile_project(project, scene, library)

    assert result.ok is True
    assert any("face_group split not yet implemented" in w for w in result.warnings)
    face_group_map = json.loads(
        (project / "mapping" / "face_group_map.json").read_text(encoding="utf-8")
    )
    assert face_group_map[WALL_ID] == "wall_south"


def test_extract_prim_mesh_applies_node_transform(tmp_path: Path) -> None:
    box = trimesh.creation.box(extents=[1.0, 1.0, 1.0])
    offset = np.eye(4)
    offset[:3, 3] = [10.0, -5.0, 2.0]
    tm_scene = trimesh.Scene()
    tm_scene.add_geometry(
        box, geom_name="moved_box", node_name="moved_node", transform=offset
    )
    project_dir = tmp_path / "xform.sionnatwin"
    (project_dir / "visual").mkdir(parents=True)
    tm_scene.export(project_dir / "visual" / "scene.glb")

    loaded = mesh_tools.load_visual_scene(project_dir, "visual/scene.glb")
    assert loaded is not None

    # Resolution by geometry name; world transform from the instancing node.
    by_geometry = mesh_tools.extract_prim_mesh(loaded, MeshRef(mesh_name="moved_box"))
    assert by_geometry is not None
    assert np.allclose(by_geometry.bounds.mean(axis=0), [10.0, -5.0, 2.0], atol=1e-5)

    # Resolution by scene-graph node name.
    by_node = mesh_tools.extract_prim_mesh(loaded, MeshRef(mesh_name="moved_node"))
    assert by_node is not None
    assert np.allclose(by_node.bounds.mean(axis=0), [10.0, -5.0, 2.0], atol=1e-5)

    # The source geometry must stay untouched (transform applied to a copy).
    assert np.allclose(loaded.geometry["moved_box"].bounds.mean(axis=0), [0.0, 0.0, 0.0])

    assert mesh_tools.extract_prim_mesh(loaded, MeshRef(mesh_name="nope")) is None


def test_load_visual_scene_missing_file(tmp_path: Path) -> None:
    assert mesh_tools.load_visual_scene(tmp_path, "visual/scene.glb") is None
