"""Scene schema invariants and scene_validator checks."""

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.materials import RFMaterialLibrary
from app.schemas.projects import ProjectInfo
from app.schemas.scene import Prim, RFBinding, Scene
from app.services.project_store import ProjectStore
from app.services.scene_validator import validate_scene

from .conftest import DEMO_PROJECT_ID, make_demo_scene


def _codes(report) -> set[str]:
    return {issue.code for issue in report.issues}


def _prim_dict(prim_id: str, name: str = "p") -> dict:
    return {
        "id": prim_id,
        "name": name,
        "mesh_ref": {"mesh_name": "m"},
    }


class TestSceneSchema:
    def test_store_roundtrip_preserves_prims_and_rf(
        self, store: ProjectStore, demo_project: ProjectInfo, demo_scene: Scene
    ):
        loaded = store.load_scene(DEMO_PROJECT_ID)
        # save_scene bumps the optimistic-concurrency counter (None -> 1); the
        # rest of the scene must survive the store round-trip byte-for-byte.
        assert loaded.revision == 1
        assert loaded.model_dump(mode="json", exclude={"revision"}) == (
            demo_scene.model_dump(mode="json", exclude={"revision"})
        )
        wall = loaded.prim_by_id("/buildings/b01/wall_01")
        assert wall is not None
        assert wall.rf.material_id == "itu_concrete"
        assert wall.rf.assignment_status == "user_confirmed"
        assert wall.rf.assignment_sources == ["user"]
        window = loaded.prim_by_id("/buildings/b01/window_12")
        assert window is not None
        assert window.rf.material_id is None
        assert window.visual is not None
        assert window.visual.material_name == "blue_glass_pbr"

    def test_duplicate_prim_ids_rejected_at_parse(self):
        raw = {
            "scene_id": "dup",
            "prims": [_prim_dict("/a/b"), _prim_dict("/a/b")],
        }
        with pytest.raises(ValidationError, match="duplicate prim id"):
            Scene.model_validate(raw)

    def test_non_path_like_prim_id_rejected(self):
        for bad in ("wall_01", "/a//b", "/a/b/"):
            with pytest.raises(ValidationError, match="path-like"):
                Prim.model_validate(_prim_dict(bad))

    def test_rf_binding_material_with_unassigned_status_rejected(self):
        with pytest.raises(ValidationError):
            RFBinding(material_id="itu_glass", assignment_status="unassigned")
        with pytest.raises(ValidationError):
            RFBinding(material_id=None, assignment_status="user_confirmed")


class TestValidateScene:
    def test_missing_rf_material_reported(self, library: RFMaterialLibrary):
        report = validate_scene(make_demo_scene(), library)
        codes = _codes(report)
        assert "MISSING_RF_MATERIAL" in codes
        flagged = {
            i.prim_id for i in report.issues if i.code == "MISSING_RF_MATERIAL"
        }
        assert "/buildings/b01/window_12" in flagged
        # Group prims and assigned prims are not flagged.
        assert "/buildings/b01" not in flagged
        assert "/buildings/b01/wall_01" not in flagged

    def test_unknown_rf_material_is_error(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        prim = scene.prim_by_id("/roads/r01/surface")
        prim.rf = RFBinding(
            material_id="not_a_material", assignment_status="user_confirmed"
        )
        report = validate_scene(scene, library)
        assert "UNKNOWN_RF_MATERIAL" in _codes(report)
        assert report.ok is False
        assert report.error_count >= 1

    def test_visual_rf_mismatch_glass_vs_concrete(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        window = scene.prim_by_id("/buildings/b01/window_12")
        assert window.name == "window_12"
        assert window.visual.material_name == "blue_glass_pbr"
        window.rf = RFBinding(
            material_id="itu_concrete", assignment_status="user_confirmed"
        )
        report = validate_scene(scene, library)
        mismatches = [
            i for i in report.issues if i.code == "VISUAL_RF_MISMATCH"
        ]
        assert any(m.prim_id == "/buildings/b01/window_12" for m in mismatches)
        msg = next(
            m.message for m in mismatches
            if m.prim_id == "/buildings/b01/window_12"
        )
        assert "glass" in msg and "concrete" in msg

    def test_no_mismatch_when_assignment_matches_evidence(
        self, library: RFMaterialLibrary
    ):
        # wall_01 has concrete visual evidence and itu_concrete assigned.
        report = validate_scene(make_demo_scene(), library)
        assert not any(
            i.code == "VISUAL_RF_MISMATCH"
            and i.prim_id == "/buildings/b01/wall_01"
            for i in report.issues
        )

    def test_missing_thickness_for_transmissive_material(
        self, library: RFMaterialLibrary
    ):
        # unknown_rf is transmissive with thickness_m null in the library.
        scene = make_demo_scene()
        prim = scene.prim_by_id("/roads/r01/surface")
        prim.rf = RFBinding(
            material_id="unknown_rf", assignment_status="user_confirmed"
        )
        report = validate_scene(scene, library)
        assert any(
            i.code == "MISSING_THICKNESS" and i.prim_id == "/roads/r01/surface"
            for i in report.issues
        )
        # A per-prim thickness override silences the warning.
        prim.rf = RFBinding(
            material_id="unknown_rf",
            assignment_status="user_confirmed",
            thickness_m=0.1,
        )
        report = validate_scene(scene, library)
        assert not any(
            i.code == "MISSING_THICKNESS" and i.prim_id == "/roads/r01/surface"
            for i in report.issues
        )

    def test_unconfirmed_suggestion_is_info(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        prim = scene.prim_by_id("/buildings/b01/window_12")
        prim.rf = RFBinding(
            material_id="itu_glass",
            assignment_status="ai_suggested",
            assignment_sources=["ai:ollama/qwen3:8b"],
            confidence=0.86,
        )
        report = validate_scene(scene, library)
        infos = [
            i for i in report.issues if i.code == "UNCONFIRMED_SUGGESTION"
        ]
        assert any(i.prim_id == "/buildings/b01/window_12" for i in infos)
        assert all(i.severity == "info" for i in infos)

    def test_unsupported_mesh_ref_only_with_project_dir(
        self, store: ProjectStore, demo_project: ProjectInfo, library: RFMaterialLibrary
    ):
        scene = store.load_scene(DEMO_PROJECT_ID)
        no_dir = validate_scene(scene, library)
        assert "UNSUPPORTED_MESH_REF" not in _codes(no_dir)
        # The created project has no visual/scene.glb on disk.
        with_dir = validate_scene(
            scene, library, project_dir=Path(demo_project.path)
        )
        assert "UNSUPPORTED_MESH_REF" in _codes(with_dir)

    def test_no_devices_is_info(self, library: RFMaterialLibrary):
        report = validate_scene(
            make_demo_scene(with_devices=False), library
        )
        assert any(
            i.code == "NO_DEVICES" and i.severity == "info"
            for i in report.issues
        )
        report_with = validate_scene(make_demo_scene(), library)
        assert "NO_DEVICES" not in _codes(report_with)

    def test_unknown_parent_is_warning(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        scene.prims.append(
            Prim.model_validate(
                {
                    "id": "/buildings/b02/wall_01",
                    "name": "wall_01",
                    "parent_id": "/buildings/b02",
                    "mesh_ref": {"mesh_name": "building_02"},
                }
            )
        )
        report = validate_scene(scene, library)
        assert any(
            i.code == "UNKNOWN_PARENT"
            and i.severity == "warning"
            and i.prim_id == "/buildings/b02/wall_01"
            for i in report.issues
        )
