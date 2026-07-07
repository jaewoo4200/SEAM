"""Tests for POST /projects/import (scene XML upload).

Builds a self-contained tiny scene at runtime: a minimal Mitsuba XML plus the
one .ply mesh it references, uploaded together via the multipart ``meshes``
field so the importer resolves the geometry. Nothing here depends on the
reference bundle.
"""

import trimesh

# Minimal Sionna/Mitsuba XML: two ITU materials + two ply shapes.
SCENE_XML = """<?xml version='1.0' encoding='utf-8'?>
<scene version="3.0.0">
  <bsdf type="twosided" id="mat-itu_concrete">
    <bsdf type="diffuse"><rgb name="reflectance" value="0.52 0.52 0.50"/></bsdf>
  </bsdf>
  <bsdf type="twosided" id="mat-itu_glass">
    <bsdf type="diffuse"><rgb name="reflectance" value="0.30 0.55 0.75"/></bsdf>
  </bsdf>
  <shape type="ply" id="mesh-wall">
    <string name="filename" value="meshes/wall.ply"/>
    <ref id="mat-itu_concrete"/>
  </shape>
  <shape type="ply" id="mesh-window">
    <string name="filename" value="meshes/window.ply"/>
    <ref id="mat-itu_glass"/>
  </shape>
</scene>
"""


def _ply_bytes() -> bytes:
    return trimesh.creation.box(extents=[1.0, 1.0, 1.0]).export(file_type="ply")


def _import_files():
    """Multipart file tuples for a valid two-shape upload."""
    ply = _ply_bytes()
    return [
        ("file", ("scene.xml", SCENE_XML.encode("utf-8"), "application/xml")),
        ("meshes", ("wall.ply", ply, "application/octet-stream")),
        ("meshes", ("window.ply", ply, "application/octet-stream")),
    ]


class TestImportAPI:
    def test_import_creates_loadable_project(self, api_client):
        resp = api_client.post(
            "/api/projects/import",
            files=_import_files(),
            data={"project_id": "imported_lab", "name": "Imported Lab", "environment": "indoor"},
        )
        assert resp.status_code == 201, resp.text
        info = resp.json()
        assert info["project_id"] == "imported_lab"
        assert info["name"] == "Imported Lab"

        # It shows up in the project list.
        listed = api_client.get("/api/projects").json()
        assert "imported_lab" in {p["project_id"] for p in listed}

        # The scene loads with the two imported prims and their mapped materials.
        scene = api_client.get("/api/projects/imported_lab/scene").json()
        assert scene["environment"] == "indoor"
        mats = {p["rf"]["material_id"] for p in scene["prims"]}
        assert mats == {"itu_concrete", "itu_glass"}
        assert len(scene["prims"]) == 2
        # A default 28 GHz simulation config was written.
        assert scene["simulation_configs"][0]["frequency_hz"] == 28e9

        # The combined visual GLB is served as an asset.
        glb = api_client.get("/api/projects/imported_lab/assets/visual/scene.glb")
        assert glb.status_code == 200
        assert glb.headers["content-type"] == "model/gltf-binary"

    def test_duplicate_project_id_conflicts(self, api_client):
        files = _import_files()
        first = api_client.post(
            "/api/projects/import",
            files=files,
            data={"project_id": "dup_scene", "name": "Dup", "environment": "auto"},
        )
        assert first.status_code == 201, first.text
        second = api_client.post(
            "/api/projects/import",
            files=_import_files(),
            data={"project_id": "dup_scene", "name": "Dup Again", "environment": "auto"},
        )
        assert second.status_code == 409, second.text
        assert "already exists" in second.json()["detail"]

    def test_invalid_project_id_rejected(self, api_client):
        resp = api_client.post(
            "/api/projects/import",
            files=_import_files(),
            data={"project_id": "Bad ID!", "name": "Nope", "environment": "auto"},
        )
        assert resp.status_code == 400
        # Nothing was created.
        assert api_client.get("/api/projects").json() == []

    def test_external_meshes_missing_returns_400(self, api_client):
        # Upload only the XML (no meshes): every shape is skipped -> 400 with a
        # count of the missing meshes and the zip-bundle hint, and no project
        # is created.
        resp = api_client.post(
            "/api/projects/import",
            files=[("file", ("scene.xml", SCENE_XML.encode("utf-8"), "application/xml"))],
            data={"project_id": "no_meshes", "name": "No Meshes", "environment": "auto"},
        )
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert "were not found" in detail and ".zip" in detail
        assert "no_meshes" not in {p["project_id"] for p in api_client.get("/api/projects").json()}

    def test_invalid_environment_rejected(self, api_client):
        resp = api_client.post(
            "/api/projects/import",
            files=_import_files(),
            data={"project_id": "bad_env", "name": "Bad Env", "environment": "space"},
        )
        assert resp.status_code == 400
        assert "environment" in resp.json()["detail"]

    def test_malformed_xml_returns_400(self, api_client):
        resp = api_client.post(
            "/api/projects/import",
            files=[("file", ("scene.xml", b"<scene><not-closed>", "application/xml"))],
            data={"project_id": "broken_xml", "name": "Broken", "environment": "auto"},
        )
        assert resp.status_code == 400, resp.text
        assert "broken_xml" not in {p["project_id"] for p in api_client.get("/api/projects").json()}
