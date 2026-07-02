"""WS3: Mitsuba/Sionna XML importer tests (uses the reference bundle if present)."""

from pathlib import Path

import pytest

from app.core.paths import REPO_ROOT
from app.services.mitsuba_import import _class_to_library_id, import_mitsuba_scene
from app.services.project_store import load_default_library

LAB_ROOM_XML = REPO_ROOT / "sionna-rt-gui-jaewoo-examples" / "indoor" / "lab_room.xml"
pytestmark = pytest.mark.skipif(
    not LAB_ROOM_XML.is_file(), reason="reference bundle not present"
)


def test_class_map_covers_itu_names():
    m = _class_to_library_id(load_default_library())
    # class token -> library id
    assert m["concrete"] == "itu_concrete"
    assert m["glass"] == "itu_glass"
    assert m["marble"] == "itu_marble"
    assert m["ceiling_board"] == "itu_ceiling_board"
    assert m["metal"] == "metal"  # itu_metal -> our 'metal'


def test_import_lab_room(tmp_path: Path):
    library = load_default_library()
    scene, tm_scene, warnings = import_mitsuba_scene(
        LAB_ROOM_XML, "lab_room", library, scene_name="Lab Room"
    )

    assert scene.scene_id == "lab_room"
    assert len(scene.prims) == 5
    mats = {p.rf.material_id for p in scene.prims}
    assert mats == {"itu_concrete", "itu_glass", "itu_marble", "metal", "itu_ceiling_board"}

    # Every prim is a mesh_primitive with an imported, user-confirmed binding
    # and a mesh_ref that resolves inside the combined GLB.
    glb_meshes = set(tm_scene.geometry.keys())
    for prim in scene.prims:
        assert prim.type == "mesh_primitive"
        assert prim.rf.assignment_status == "user_confirmed"
        assert "imported_xml" in prim.rf.assignment_sources
        assert prim.mesh_ref is not None
        assert prim.mesh_ref.mesh_name in glb_meshes

    # No unresolved-material warnings for the clean indoor scene.
    assert not any("could not resolve" in w for w in warnings)

    # The combined GLB round-trips with the expected named meshes.
    out = tmp_path / "scene.glb"
    out.write_bytes(tm_scene.export(file_type="glb"))
    import trimesh

    reloaded = set(trimesh.load(out).geometry.keys())
    assert reloaded == {"ceiling_board", "concrete", "glass", "marble", "metal"}


def test_imported_scene_validates_clean():
    from app.services.scene_validator import validate_scene

    library = load_default_library()
    scene, _tm, _w = import_mitsuba_scene(LAB_ROOM_XML, "lab_room", library)
    report = validate_scene(scene, library)
    # All prims have confirmed materials; no error-severity issues.
    assert report.ok, [i.model_dump() for i in report.issues if i.severity == "error"]
