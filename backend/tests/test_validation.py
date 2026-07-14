"""Task B: validation statuses, suggested_actions, and geometry guardrails.

Covers the new AssignmentStatus values (rule_assigned / rejected), the
RFBinding consistency invariant for the no-material statuses, the
suggested_actions attached to every issue, and the project_dir-driven geometry
checks (triangle density, watertightness, scale) against synthetic GLBs.
"""

from pathlib import Path

import pytest
import trimesh

from seam_studio.schemas.materials import RFMaterial, RFMaterialLibrary
from seam_studio.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from seam_studio.services import scene_validator
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scene_validator import validate_scene


# --------------------------------------------------------- status round-trips


def test_new_statuses_roundtrip():
    """rule_assigned carries a material; rejected keeps material_id None."""
    ra = RFBinding(material_id="itu_concrete", assignment_status="rule_assigned")
    assert ra.assignment_status == "rule_assigned"
    assert ra.material_id == "itu_concrete"

    rej = RFBinding(assignment_status="rejected")
    assert rej.assignment_status == "rejected"
    assert rej.material_id is None

    # Full JSON round-trip preserves both.
    for binding in (ra, rej):
        again = RFBinding.model_validate_json(binding.model_dump_json())
        assert again == binding


def test_rejected_with_material_is_invalid():
    """A rejected (or unassigned) binding must not carry a material_id."""
    with pytest.raises(ValueError):
        RFBinding(material_id="itu_concrete", assignment_status="rejected")
    with pytest.raises(ValueError):
        RFBinding(material_id="itu_concrete", assignment_status="unassigned")


def test_rule_assigned_without_material_is_invalid():
    """A material-bearing status with no material_id is rejected."""
    with pytest.raises(ValueError):
        RFBinding(assignment_status="rule_assigned")


# ------------------------------------------------------- suggested_actions


def _mesh_prim(prim_id: str, **rf_kwargs) -> Prim:
    return Prim(
        id=prim_id,
        name=prim_id.rsplit("/", 1)[-1],
        mesh_ref=MeshRef(mesh_name=prim_id.rsplit("/", 1)[-1]),
        rf=RFBinding(**rf_kwargs),
    )


def test_missing_rf_material_has_suggested_actions():
    scene = Scene(
        scene_id="v",
        name="v",
        prims=[_mesh_prim("/wall")],  # no material -> MISSING_RF_MATERIAL
    )
    report = validate_scene(scene, load_default_library())
    issue = next(i for i in report.issues if i.code == "MISSING_RF_MATERIAL")
    assert issue.suggested_actions
    assert any("RF Materials" in a for a in issue.suggested_actions)
    assert 1 <= len(issue.suggested_actions) <= 3


def test_every_issue_carries_suggested_actions():
    """Whatever fires, each issue exposes 1-3 concrete actions (or none only
    for codes we deliberately leave empty - none such today)."""
    scene = Scene(
        scene_id="v2",
        name="v2",
        prims=[
            _mesh_prim("/a"),  # missing material
            _mesh_prim("/b", material_id="not_in_library",
                       assignment_status="user_confirmed"),  # unknown material
        ],
    )  # no devices -> NO_DEVICES too
    report = validate_scene(scene, load_default_library())
    assert report.issues
    for issue in report.issues:
        assert isinstance(issue.suggested_actions, list)
        assert issue.suggested_actions, f"{issue.code} has no suggested_actions"
        assert len(issue.suggested_actions) <= 3


def test_rejected_prim_does_not_warn_missing_material():
    """A deliberately rejected prim must NOT raise MISSING_RF_MATERIAL."""
    scene = Scene(
        scene_id="v3",
        name="v3",
        prims=[_mesh_prim("/wall", assignment_status="rejected")],
    )
    report = validate_scene(scene, load_default_library())
    assert "MISSING_RF_MATERIAL" not in {i.code for i in report.issues}


# --------------------------------------------------------- geometry checks


def _export_glb(path: Path, meshes: dict[str, trimesh.Trimesh]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tm = trimesh.Scene()
    for name, mesh in meshes.items():
        tm.add_geometry(mesh, geom_name=name, node_name=name)
    path.write_bytes(tm.export(file_type="glb"))


def _geo_scene(mesh_name: str) -> Scene:
    return Scene(
        scene_id="geo",
        name="geo",
        prims=[
            Prim(
                id="/obj",
                name="obj",
                mesh_ref=MeshRef(mesh_name=mesh_name),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            )
        ],
        devices=[
            Device(id="tx_001", kind="tx", position=[0.0, 0.0, 3.0]),
            Device(id="rx_001", kind="rx", position=[5.0, 0.0, 1.5]),
        ],
    )


def test_scale_suspicious_fires_on_tiny_scene(tmp_path: Path):
    """A whole scene spanning <0.5 m is almost certainly a unit-scale error."""
    tiny = trimesh.creation.box(extents=[0.01, 0.01, 0.01])
    _export_glb(tmp_path / "visual" / "scene.glb", {"obj": tiny})
    report = validate_scene(_geo_scene("obj"), load_default_library(), project_dir=tmp_path)
    codes = {i.code for i in report.issues}
    assert "SCALE_SUSPICIOUS" in codes
    issue = next(i for i in report.issues if i.code == "SCALE_SUSPICIOUS")
    assert issue.severity == "warning" and issue.suggested_actions


def test_scale_suspicious_fires_on_huge_scene(tmp_path: Path):
    huge = trimesh.creation.box(extents=[100000.0, 10.0, 10.0])
    _export_glb(tmp_path / "visual" / "scene.glb", {"obj": huge})
    report = validate_scene(_geo_scene("obj"), load_default_library(), project_dir=tmp_path)
    assert "SCALE_SUSPICIOUS" in {i.code for i in report.issues}


def test_normal_scale_scene_has_no_scale_or_triangle_warning(tmp_path: Path):
    box = trimesh.creation.box(extents=[4.0, 3.0, 2.5])
    _export_glb(tmp_path / "visual" / "scene.glb", {"obj": box})
    report = validate_scene(_geo_scene("obj"), load_default_library(), project_dir=tmp_path)
    codes = {i.code for i in report.issues}
    assert "SCALE_SUSPICIOUS" not in codes
    assert "TOO_MANY_TRIANGLES" not in codes


def test_too_many_triangles_fires(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """Dense geometry above the per-prim threshold warns. The threshold is
    lowered so a modest real GLB trips the real code path without generating a
    multi-million-face mesh in CI."""
    monkeypatch.setattr(scene_validator, "_PRIM_TRIANGLE_WARN", 100)
    sphere = trimesh.creation.icosphere(subdivisions=3)  # 1280 faces > 100
    assert len(sphere.faces) > 100
    _export_glb(tmp_path / "visual" / "scene.glb", {"obj": sphere})
    report = validate_scene(_geo_scene("obj"), load_default_library(), project_dir=tmp_path)
    issue = next(i for i in report.issues if i.code == "TOO_MANY_TRIANGLES")
    assert issue.severity == "warning" and issue.prim_id == "/obj"


def test_non_manifold_open_mesh_is_info(tmp_path: Path):
    """An open (non-watertight) mesh is reported as INFO, not an error."""
    # A single triangle is the canonical open surface: not watertight.
    open_mesh = trimesh.Trimesh(
        vertices=[[0, 0, 0], [1, 0, 0], [0, 1, 0]], faces=[[0, 1, 2]]
    )
    assert not open_mesh.is_watertight
    _export_glb(tmp_path / "visual" / "scene.glb", {"obj": open_mesh})
    report = validate_scene(_geo_scene("obj"), load_default_library(), project_dir=tmp_path)
    issue = next(i for i in report.issues if i.code == "NON_MANIFOLD_OR_OPEN_MESH")
    assert issue.severity == "info"
    assert issue.suggested_actions


def test_watertight_box_has_no_open_mesh_issue(tmp_path: Path):
    box = trimesh.creation.box(extents=[4.0, 3.0, 2.5])
    assert box.is_watertight
    _export_glb(tmp_path / "visual" / "scene.glb", {"obj": box})
    report = validate_scene(_geo_scene("obj"), load_default_library(), project_dir=tmp_path)
    assert "NON_MANIFOLD_OR_OPEN_MESH" not in {i.code for i in report.issues}


def test_geometry_checks_skipped_without_project_dir():
    """No project_dir -> no geometry checks, no exceptions."""
    scene = _geo_scene("obj")
    report = validate_scene(scene, load_default_library())  # no project_dir
    geo_codes = {"TOO_MANY_TRIANGLES", "NON_MANIFOLD_OR_OPEN_MESH", "SCALE_SUSPICIOUS"}
    assert not (geo_codes & {i.code for i in report.issues})


def test_missing_asset_yields_no_geometry_issues(tmp_path: Path):
    """project_dir set but no GLB on disk: geometry checks are silent (the
    missing-asset case is reported separately as UNSUPPORTED_MESH_REF)."""
    report = validate_scene(_geo_scene("obj"), load_default_library(), project_dir=tmp_path)
    geo_codes = {"TOO_MANY_TRIANGLES", "NON_MANIFOLD_OR_OPEN_MESH", "SCALE_SUSPICIOUS"}
    assert not (geo_codes & {i.code for i in report.issues})


# ------------------------------------------------------------- validate route


def test_validate_route_passes_project_dir(api_client, tmp_path: Path):
    """The scene/validate endpoint resolves the project dir so geometry checks
    run end to end."""
    from seam_studio.api.deps import get_store

    store = get_store()
    store.create_project("Geo API", project_id="geoapi")
    store.save_scene("geoapi", _geo_scene("obj"))
    tiny = trimesh.creation.box(extents=[0.01, 0.01, 0.01])
    _export_glb(store.resolve("geoapi") / "visual" / "scene.glb", {"obj": tiny})

    resp = api_client.post("/api/projects/geoapi/scene/validate")
    assert resp.status_code == 200, resp.text
    codes = {i["code"] for i in resp.json()["issues"]}
    assert "SCALE_SUSPICIOUS" in codes
