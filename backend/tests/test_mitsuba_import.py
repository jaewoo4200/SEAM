"""WS3: Mitsuba/Sionna XML importer tests (uses the reference bundle if present)."""

from pathlib import Path

import pytest

from app.core.paths import REPO_ROOT
from app.services.mitsuba_import import _class_to_library_id, import_mitsuba_scene
from app.services.project_store import load_default_library

LAB_ROOM_XML = REPO_ROOT / "reference-bundle" / "indoor" / "lab_room.xml"
requires_lab_room = pytest.mark.skipif(
    not LAB_ROOM_XML.is_file(), reason="reference bundle not present"
)


def _write_itu_material_scene(tmp_path: Path, itu_class: str) -> Path:
    """Write a minimal self-contained Sionna XML (one ply shape bound to an
    ``itu-radio-material`` of the given class) plus its PLY mesh. Independent of
    the reference bundle so the out-of-band remap tests always run."""
    import trimesh

    mesh = trimesh.creation.box(extents=(4.0, 4.0, 0.1))
    (tmp_path / "meshes").mkdir(exist_ok=True)
    mesh.export(tmp_path / "meshes" / "slab.ply")

    xml = f"""<?xml version='1.0' encoding='utf-8'?>
<scene version="3.0.0">
  <integrator type="path" />
  <bsdf type="itu-radio-material" id="mat-slab">
    <string name="type" value="{itu_class}" />
    <rgb name="color" value="0.5 0.4 0.3" />
  </bsdf>
  <shape type="ply" id="mesh-slab">
    <string name="filename" value="meshes/slab.ply" />
    <ref id="mat-slab" />
  </shape>
</scene>
"""
    xml_path = tmp_path / "scene.xml"
    xml_path.write_text(xml, encoding="utf-8")
    return xml_path


@requires_lab_room
def test_class_map_covers_itu_names():
    m = _class_to_library_id(load_default_library())
    # class token -> library id
    assert m["concrete"] == "itu_concrete"
    assert m["glass"] == "itu_glass"
    assert m["marble"] == "itu_marble"
    assert m["ceiling_board"] == "itu_ceiling_board"
    assert m["metal"] == "metal"  # itu_metal -> our 'metal'


@requires_lab_room
def test_import_lab_room(tmp_path: Path):
    library = load_default_library()
    scene, tm_scene, warnings, _tex = import_mitsuba_scene(
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


@requires_lab_room
def test_imported_scene_validates_clean():
    from app.services.scene_validator import validate_scene

    library = load_default_library()
    scene, _tm, _w, _tex = import_mitsuba_scene(LAB_ROOM_XML, "lab_room", library)
    report = validate_scene(scene, library)
    # All prims have confirmed materials; no error-severity issues.
    assert report.ok, [i.model_dump() for i in report.issues if i.severity == "error"]


OUTDOOR_XML = (
    REPO_ROOT / "reference-bundle" / "outdoor_material_assigned_cv_28ghz_safe.xml"
)


@pytest.mark.skipif(not OUTDOOR_XML.is_file(), reason="outdoor bundle scene not present")
def test_import_outdoor_ftc_applies_transform_and_maps_materials():
    library = load_default_library()
    scene, tm_scene, warnings, _tex = import_mitsuba_scene(
        OUTDOOR_XML, "ftc_outdoor", library, scene_name="FTC Outdoor"
    )
    mats = {p.rf.material_id for p in scene.prims}
    # itu classes resolve, the constant ground -> ground_28ghz, the occlusion
    # blocker legitimately stays unknown_rf.
    assert {"itu_concrete", "itu_glass", "metal", "ground_28ghz"} <= mats
    assert any("occlusion" in w.lower() for w in warnings)

    # The shapes carry a +90deg X rotation (Y-up -> Z-up). After applying it the
    # combined geometry should be Z-up: taller in Z than a degenerate flat slab
    # and with meaningful horizontal extent.
    combined = tm_scene.dump(concatenate=True)
    lo, hi = combined.bounds
    assert (hi[2] - lo[2]) > 1.0  # non-degenerate vertical extent
    assert (hi[0] - lo[0]) > 5.0 and (hi[1] - lo[1]) > 5.0


def test_parse_transform_rotate_x_90():
    import numpy as np
    import xml.etree.ElementTree as ET

    from app.services.mitsuba_import import _parse_transform

    shape = ET.fromstring(
        '<shape><transform name="to_world"><rotate x="1" angle="90"/></transform></shape>'
    )
    M = _parse_transform(shape)
    # +90deg about X maps +Y -> +Z.
    y = M @ np.array([0.0, 1.0, 0.0, 1.0])
    assert abs(y[2] - 1.0) < 1e-6 and abs(y[1]) < 1e-6


# --------------------------------------------------------------------------- #
# ITU frequency-band guardrail at import time (TASK 5).                        #
# --------------------------------------------------------------------------- #


def test_import_remaps_out_of_band_itu_ground_to_28ghz_safe(tmp_path: Path):
    """An ITU ground material (medium_dry_ground) imported into a 28 GHz-default
    project is remapped to the band-safe constant ground_28ghz, with the
    original mapping preserved in assignment_sources and a warning emitted."""
    library = load_default_library()
    xml_path = _write_itu_material_scene(tmp_path, "medium_dry_ground")

    scene, _tm, warnings, _tex = import_mitsuba_scene(
        xml_path, "ground_scene", library, default_frequency_hz=28e9
    )

    (prim,) = scene.prims
    # Binding remapped to the 28 GHz-safe constant material.
    assert prim.rf.material_id == "ground_28ghz"
    # Source trail records the original ITU mapping and why it was swapped.
    (src,) = prim.rf.assignment_sources
    assert src.startswith("imported_xml:ground->ground_28ghz")
    assert "out of ITU band at 28 GHz" in src
    # A user-facing warning is present.
    assert any("ground_28ghz" in w and "remapped" in w for w in warnings)

    # The remapped scene validates without an out-of-band warning: the whole
    # point of remapping at import is that the first solve is band-safe.
    from app.schemas.simulation import SimulationConfig
    from app.services.scene_validator import validate_scene

    scene.simulation_configs = [SimulationConfig(id="default", frequency_hz=28e9)]
    report = validate_scene(scene, library)
    assert not any(i.code == "MATERIAL_OUT_OF_BAND" for i in report.issues)


def test_import_non_remappable_out_of_band_warns_with_fix(tmp_path: Path, monkeypatch):
    """When an out-of-band ITU material has no safe alternative, the binding is
    kept but a warning with the concrete fix text is emitted."""
    import app.services.mitsuba_import as mi

    # Force the "no safe alternative" branch for the ground category.
    monkeypatch.setattr(mi, "itu_safe_alternative", lambda category: None)

    library = load_default_library()
    xml_path = _write_itu_material_scene(tmp_path, "medium_dry_ground")

    scene, _tm, warnings, _tex = import_mitsuba_scene(
        xml_path, "ground_scene", library, default_frequency_hz=28e9
    )

    (prim,) = scene.prims
    # Binding kept (no swap available); still the original ITU ground material.
    assert prim.rf.material_id == "ground"
    assert prim.rf.assignment_sources == ["imported_xml"]
    # Warning carries the actionable fix text.
    assert any(
        "lower the frequency" in w and "constant-model material" in w
        for w in warnings
    )


def test_import_in_band_itu_material_not_remapped(tmp_path: Path):
    """An ITU ground material imported at a frequency inside its band (e.g.
    5 GHz) keeps its ITU binding and emits no remap warning."""
    library = load_default_library()
    xml_path = _write_itu_material_scene(tmp_path, "medium_dry_ground")

    scene, _tm, warnings, _tex = import_mitsuba_scene(
        xml_path, "ground_scene", library, default_frequency_hz=5e9
    )

    (prim,) = scene.prims
    assert prim.rf.material_id == "ground"
    assert prim.rf.assignment_sources == ["imported_xml"]
    assert not any("remapped" in w for w in warnings)


def test_enrich_solve_failure_appends_itu_hint():
    """Pure string helper: a stringified solve failure carrying Sionna's
    'not defined for this frequency' error gets one actionable sentence
    appended; unrelated failures are returned unchanged and the hint is never
    double-appended."""
    from app.services.simulation_backends.sionna_backend import (
        _ITU_OUT_OF_BAND_HINT,
        _enrich_solve_failure,
    )

    raw = (
        "sionna radio map failed: Properties of ITU material "
        "'medium_dry_ground' are not defined for this frequency; see logs"
    )
    enriched = _enrich_solve_failure(raw)
    assert enriched.endswith(_ITU_OUT_OF_BAND_HINT)
    assert "ground_28ghz" in enriched
    assert "RF Materials tab" in enriched

    # Idempotent: already-enriched messages are not re-appended.
    assert _enrich_solve_failure(enriched) == enriched

    # Unrelated failures pass through untouched.
    other = "sionna backend failed: CUDA out of memory; see logs"
    assert _enrich_solve_failure(other) == other
