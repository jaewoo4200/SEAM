"""Material assignment service and project/scene/materials API tests."""

import json
from pathlib import Path

import pytest

from app.schemas.materials import (
    AssignRequest,
    BatchAssignRequest,
    RFMaterialLibrary,
    RFOverrides,
)
from app.services.material_assignment import (
    UnknownMaterialError,
    apply_batch,
    assign_materials,
)

from .conftest import make_demo_scene

WINDOW = "/buildings/b01/window_12"
WALL = "/buildings/b01/wall_01"
ROAD = "/roads/r01/surface"
GROUP = "/buildings/b01"


class TestAssignMaterials:
    def test_assign_updates_binding_status_sources_confidence(
        self, library: RFMaterialLibrary
    ):
        scene = make_demo_scene()
        request = AssignRequest(
            prim_ids=[WINDOW],
            rf_material_id="itu_glass",
            assignment_status="user_confirmed",
            sources=["visual_material_name", "user"],
            confidence=0.86,
        )
        response = assign_materials(scene, request, library)
        assert response.updated_prim_ids == [WINDOW]
        assert response.skipped_prim_ids == []
        rf = scene.prim_by_id(WINDOW).rf
        assert rf.material_id == "itu_glass"
        assert rf.assignment_status == "user_confirmed"
        assert rf.assignment_sources == ["visual_material_name", "user"]
        assert rf.confidence == 0.86

    def test_unknown_material_raises(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        with pytest.raises(UnknownMaterialError):
            assign_materials(
                scene,
                AssignRequest(prim_ids=[WINDOW], rf_material_id="kryptonite"),
                library,
            )
        # Scene untouched on failure.
        assert scene.prim_by_id(WINDOW).rf.material_id is None

    def test_unknown_prim_id_is_skipped(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        response = assign_materials(
            scene,
            AssignRequest(
                prim_ids=["/does/not/exist", WINDOW],
                rf_material_id="itu_glass",
            ),
            library,
        )
        assert response.skipped_prim_ids == ["/does/not/exist"]
        assert response.updated_prim_ids == [WINDOW]
        assert any("/does/not/exist" in w for w in response.warnings)

    def test_group_prim_is_skipped_with_warning(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        assert scene.prim_by_id(GROUP).type == "group"
        response = assign_materials(
            scene,
            AssignRequest(prim_ids=[GROUP], rf_material_id="itu_concrete"),
            library,
        )
        # Group prims are not compiled to RF, so they are skipped, not updated,
        # and the binding is left untouched.
        assert response.updated_prim_ids == []
        assert response.skipped_prim_ids == [GROUP]
        assert any(
            GROUP in w and "group prims are not compiled" in w
            for w in response.warnings
        )
        assert scene.prim_by_id(GROUP).rf.material_id is None

    def test_overrides_applied_and_cleared(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        assign_materials(
            scene,
            AssignRequest(
                prim_ids=[WINDOW],
                rf_material_id="itu_glass",
                overrides=RFOverrides(
                    thickness_m=0.012, scattering_coefficient=0.05
                ),
            ),
            library,
        )
        rf = scene.prim_by_id(WINDOW).rf
        assert rf.thickness_m == 0.012
        assert rf.scattering_coefficient == 0.05
        assert rf.xpd_coefficient is None
        # Re-assignment without overrides clears the stale per-prim values.
        assign_materials(
            scene,
            AssignRequest(prim_ids=[WINDOW], rf_material_id="itu_glass"),
            library,
        )
        rf = scene.prim_by_id(WINDOW).rf
        assert rf.thickness_m is None
        assert rf.scattering_coefficient is None

    def test_batch_aggregates_results(self, library: RFMaterialLibrary):
        scene = make_demo_scene()
        batch = BatchAssignRequest(
            assignments=[
                AssignRequest(prim_ids=[WINDOW], rf_material_id="itu_glass"),
                AssignRequest(
                    prim_ids=[ROAD, "/missing/prim"],
                    rf_material_id="asphalt_custom",
                ),
            ]
        )
        response = apply_batch(scene, batch, library)
        assert response.updated_prim_ids == [WINDOW, ROAD]
        assert response.skipped_prim_ids == ["/missing/prim"]
        assert scene.prim_by_id(WINDOW).rf.material_id == "itu_glass"
        assert scene.prim_by_id(ROAD).rf.material_id == "asphalt_custom"

    def test_batch_unknown_material_fails_before_mutating(
        self, library: RFMaterialLibrary
    ):
        scene = make_demo_scene()
        batch = BatchAssignRequest(
            assignments=[
                AssignRequest(prim_ids=[WINDOW], rf_material_id="itu_glass"),
                AssignRequest(prim_ids=[ROAD], rf_material_id="unobtanium"),
            ]
        )
        with pytest.raises(UnknownMaterialError):
            apply_batch(scene, batch, library)
        assert scene.prim_by_id(WINDOW).rf.material_id is None


class TestMaterialsAPI:
    def _create_project(self, api_client, name: str = "API Demo") -> str:
        created = api_client.post("/api/projects", json={"name": name})
        assert created.status_code == 201, created.text
        return created.json()["project_id"]

    def test_full_assignment_flow_persists_across_reload(self, api_client):
        pid = self._create_project(api_client)
        assert pid == "api_demo"

        listed = api_client.get("/api/projects")
        assert listed.status_code == 200
        assert pid in {p["project_id"] for p in listed.json()}

        # PUT the demo scene (scene_id must match the created scene).
        scene_json = make_demo_scene(scene_id=pid).model_dump(mode="json")
        put = api_client.put(f"/api/projects/{pid}/scene", json=scene_json)
        assert put.status_code == 200, put.text

        # scene_id mismatch is a 400.
        bad = make_demo_scene(scene_id="other_scene").model_dump(mode="json")
        assert (
            api_client.put(f"/api/projects/{pid}/scene", json=bad).status_code
            == 400
        )

        # Assign glass to the window via the API.
        assign = api_client.post(
            f"/api/projects/{pid}/rf/assign",
            json={
                "prim_ids": [WINDOW],
                "rf_material_id": "itu_glass",
                "assignment_status": "user_confirmed",
                "sources": ["user"],
                "confidence": 0.9,
                "overrides": {"thickness_m": 0.012},
            },
        )
        assert assign.status_code == 200, assign.text
        assert assign.json()["updated_prim_ids"] == [WINDOW]

        # Reload from disk through the API: assignment persisted.
        reloaded = api_client.get(f"/api/projects/{pid}/scene").json()
        window = next(p for p in reloaded["prims"] if p["id"] == WINDOW)
        assert window["rf"]["material_id"] == "itu_glass"
        assert window["rf"]["assignment_status"] == "user_confirmed"
        assert window["rf"]["thickness_m"] == 0.012

        # Provenance event was appended.
        project_path = Path(
            api_client.get(f"/api/projects/{pid}").json()["path"]
        )
        provenance = json.loads(
            (project_path / "provenance.json").read_text(encoding="utf-8")
        )
        assert any(e.get("type") == "rf_assign" for e in provenance["events"])

    def test_assign_unknown_material_404(self, api_client):
        pid = self._create_project(api_client, name="Assign 404")
        scene_json = make_demo_scene(scene_id=pid).model_dump(mode="json")
        assert (
            api_client.put(
                f"/api/projects/{pid}/scene", json=scene_json
            ).status_code
            == 200
        )
        response = api_client.post(
            f"/api/projects/{pid}/rf/assign",
            json={"prim_ids": [WINDOW], "rf_material_id": "kryptonite"},
        )
        assert response.status_code == 404

    def test_batch_assign_endpoint(self, api_client):
        pid = self._create_project(api_client, name="Batch Demo")
        scene_json = make_demo_scene(scene_id=pid).model_dump(mode="json")
        api_client.put(f"/api/projects/{pid}/scene", json=scene_json)
        response = api_client.post(
            f"/api/projects/{pid}/rf/batch-assign",
            json={
                "assignments": [
                    {"prim_ids": [WINDOW], "rf_material_id": "itu_glass"},
                    {"prim_ids": [ROAD], "rf_material_id": "asphalt_custom"},
                ]
            },
        )
        assert response.status_code == 200, response.text
        assert response.json()["updated_prim_ids"] == [WINDOW, ROAD]

    def test_material_upsert_clears_builtin(self, api_client):
        pid = self._create_project(api_client, name="Mat Edit")
        library = api_client.get(f"/api/projects/{pid}/rf/materials").json()
        glass = next(m for m in library["materials"] if m["id"] == "itu_glass")
        assert glass["builtin"] is True
        glass["thickness_m"] = 0.02
        put = api_client.put(
            f"/api/projects/{pid}/rf/materials/itu_glass", json=glass
        )
        assert put.status_code == 200, put.text
        stored = next(
            m for m in put.json()["materials"] if m["id"] == "itu_glass"
        )
        assert stored["thickness_m"] == 0.02
        assert stored["builtin"] is False
        # Body id must match the path id.
        assert (
            api_client.put(
                f"/api/projects/{pid}/rf/materials/other_id", json=glass
            ).status_code
            == 400
        )

    def test_unassign_clears_binding_and_persists(self, api_client):
        pid = self._create_project(api_client, name="Unassign")
        scene_json = make_demo_scene(scene_id=pid).model_dump(mode="json")
        api_client.put(f"/api/projects/{pid}/scene", json=scene_json)
        # WALL starts user_confirmed with itu_concrete; unassign it.
        resp = api_client.post(
            f"/api/projects/{pid}/rf/unassign", json={"prim_ids": [WALL]}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["updated_prim_ids"] == [WALL]

        reloaded = api_client.get(f"/api/projects/{pid}/scene").json()
        wall = next(p for p in reloaded["prims"] if p["id"] == WALL)
        assert wall["rf"]["material_id"] is None
        assert wall["rf"]["assignment_status"] == "unassigned"

        # Provenance recorded the unassign.
        provenance = json.loads(
            (Path(api_client.get(f"/api/projects/{pid}").json()["path"])
             / "provenance.json").read_text(encoding="utf-8")
        )
        assert any(e.get("type") == "rf_unassign" for e in provenance["events"])

    def test_unassign_unknown_prim_is_skipped(self, api_client):
        pid = self._create_project(api_client, name="Unassign Skip")
        scene_json = make_demo_scene(scene_id=pid).model_dump(mode="json")
        api_client.put(f"/api/projects/{pid}/scene", json=scene_json)
        resp = api_client.post(
            f"/api/projects/{pid}/rf/unassign",
            json={"prim_ids": ["/nope", WALL]},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["updated_prim_ids"] == [WALL]
        assert body["skipped_prim_ids"] == ["/nope"]

    def test_delete_custom_unassigned_material(self, api_client):
        pid = self._create_project(api_client, name="Del Custom")
        # Create a custom material via PUT (upsert).
        custom = {
            "id": "my_custom", "display_name": "My Custom", "category": "custom",
            "model": "constant", "itu_name": None, "relative_permittivity": 3.0,
            "conductivity_s_per_m": 0.01, "thickness_m": 0.1,
            "scattering_coefficient": 0.0, "xpd_coefficient": 0.0,
            "transmissive": True, "preview_color": "#9e9e9e", "notes": "",
            "builtin": False,
        }
        put = api_client.put(
            f"/api/projects/{pid}/rf/materials/my_custom", json=custom
        )
        assert put.status_code == 200, put.text
        assert "my_custom" in {m["id"] for m in put.json()["materials"]}
        # Delete it (unassigned, custom): succeeds and it is gone.
        deleted = api_client.delete(f"/api/projects/{pid}/rf/materials/my_custom")
        assert deleted.status_code == 200, deleted.text
        assert "my_custom" not in {m["id"] for m in deleted.json()["materials"]}

    def test_delete_builtin_material_refused(self, api_client):
        pid = self._create_project(api_client, name="Del Builtin")
        resp = api_client.delete(f"/api/projects/{pid}/rf/materials/itu_glass")
        assert resp.status_code == 400, resp.text
        assert "builtin" in resp.json()["detail"]
        # Still present.
        library = api_client.get(f"/api/projects/{pid}/rf/materials").json()
        assert "itu_glass" in {m["id"] for m in library["materials"]}

    def test_delete_assigned_material_conflicts(self, api_client):
        pid = self._create_project(api_client, name="Del Assigned")
        scene_json = make_demo_scene(scene_id=pid).model_dump(mode="json")
        api_client.put(f"/api/projects/{pid}/scene", json=scene_json)
        # Make a custom material and assign it to the window.
        custom = {
            "id": "assigned_custom", "display_name": "Assigned", "category": "custom",
            "model": "constant", "itu_name": None, "relative_permittivity": 3.0,
            "conductivity_s_per_m": 0.01, "thickness_m": 0.1,
            "scattering_coefficient": 0.0, "xpd_coefficient": 0.0,
            "transmissive": True, "preview_color": "#9e9e9e", "notes": "",
            "builtin": False,
        }
        api_client.put(f"/api/projects/{pid}/rf/materials/assigned_custom", json=custom)
        api_client.post(
            f"/api/projects/{pid}/rf/assign",
            json={"prim_ids": [WINDOW], "rf_material_id": "assigned_custom"},
        )
        resp = api_client.delete(f"/api/projects/{pid}/rf/materials/assigned_custom")
        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert WINDOW in detail["prim_ids"]
        # Unassign, then the delete succeeds.
        api_client.post(f"/api/projects/{pid}/rf/unassign", json={"prim_ids": [WINDOW]})
        again = api_client.delete(f"/api/projects/{pid}/rf/materials/assigned_custom")
        assert again.status_code == 200, again.text

    def test_delete_unknown_material_404(self, api_client):
        pid = self._create_project(api_client, name="Del Unknown")
        resp = api_client.delete(f"/api/projects/{pid}/rf/materials/ghost_material")
        assert resp.status_code == 404

    def test_missing_asset_is_404(self, api_client):
        pid = self._create_project(api_client, name="Assets")
        response = api_client.get(
            f"/api/projects/{pid}/assets/visual/scene.glb"
        )
        assert response.status_code == 404

    def test_asset_roundtrip_and_media_type(self, api_client):
        pid = self._create_project(api_client, name="Asset Types")
        project_path = Path(
            api_client.get(f"/api/projects/{pid}").json()["path"]
        )
        glb = project_path / "visual" / "scene.glb"
        glb.write_bytes(b"glTF fake binary")
        response = api_client.get(f"/api/projects/{pid}/assets/visual/scene.glb")
        assert response.status_code == 200
        assert response.headers["content-type"] == "model/gltf-binary"
        assert response.content == b"glTF fake binary"

    def test_unknown_project_is_404(self, api_client):
        assert api_client.get("/api/projects/nope").status_code == 404
        assert api_client.get("/api/projects/nope/scene").status_code == 404
        assert (
            api_client.get("/api/projects/nope/rf/materials").status_code
            == 404
        )

    def test_validate_endpoint(self, api_client):
        pid = self._create_project(api_client, name="Validate")
        scene_json = make_demo_scene(scene_id=pid).model_dump(mode="json")
        api_client.put(f"/api/projects/{pid}/scene", json=scene_json)
        report = api_client.post(f"/api/projects/{pid}/scene/validate").json()
        codes = {i["code"] for i in report["issues"]}
        assert "MISSING_RF_MATERIAL" in codes
        # No GLB on disk, so mesh refs are unsupported.
        assert "UNSUPPORTED_MESH_REF" in codes
        assert report["ok"] is True  # warnings only, no errors
