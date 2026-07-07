"""Textured-bundle import: PLY UV + PNG bitmap -> TextureVisuals + persisted originals.

Pins the new textured-import pipeline (mitsuba_import.import_mitsuba_scene's
4-tuple return, POST /projects/import zip path). Everything is built from a
tiny synthetic bundle at runtime; nothing depends on the reference bundle.

The bundle mirrors a Blender-style export: PLY meshes carrying UVs under a
NON-standard ``meshes_tex/`` dir, PNG textures under ``textures/``, and bsdf
ids like ``mat-itu_concrete.001`` whose ``<texture type="bitmap">`` is nested
inside the ``twosided`` -> ``diffuse`` wrapper. A third mesh has NO UVs and a
flat rgb bsdf, exercising the flat-color fallback.
"""

import io
import json
import zipfile
from pathlib import Path

import numpy as np
import trimesh
from PIL import Image

from app.services.mitsuba_import import import_mitsuba_scene
from app.services.project_store import load_default_library

# --------------------------------------------------------------------------- #
# Synthetic-bundle helpers                                                     #
# --------------------------------------------------------------------------- #

# Textured scene XML: two UV meshes under meshes_tex/ bound to bsdfs whose
# bitmap texture is nested twosided -> diffuse (Blender export shape); one
# no-UV mesh bound to a flat rgb bsdf.
_TEXTURED_XML = """<?xml version='1.0' encoding='utf-8'?>
<scene version="3.0.0">
  <bsdf type="twosided" id="mat-itu_concrete.001">
    <bsdf type="diffuse">
      <texture type="bitmap" name="reflectance">
        <string name="filename" value="textures/concrete_a.png"/>
      </texture>
    </bsdf>
  </bsdf>
  <bsdf type="twosided" id="mat-itu_concrete.002">
    <bsdf type="diffuse">
      <texture type="bitmap" name="reflectance">
        <string name="filename" value="textures/concrete_b.png"/>
      </texture>
    </bsdf>
  </bsdf>
  <bsdf type="twosided" id="mat-itu_concrete">
    <bsdf type="diffuse"><rgb name="reflectance" value="0.50 0.50 0.50"/></bsdf>
  </bsdf>
  <shape type="ply" id="mesh-wall_a">
    <string name="filename" value="meshes_tex/wall_a.ply"/>
    <ref id="mat-itu_concrete.001"/>
  </shape>
  <shape type="ply" id="mesh-wall_b">
    <string name="filename" value="meshes_tex/wall_b.ply"/>
    <ref id="mat-itu_concrete.002"/>
  </shape>
  <shape type="ply" id="mesh-floor">
    <string name="filename" value="meshes_tex/floor.ply"/>
    <ref id="mat-itu_concrete"/>
  </shape>
</scene>
"""

# Decoy plain XML: resolves the SAME three meshes (from another dir) but has NO
# bitmap textures. Present in the zip so the tie-break (textured variant wins on
# an equal resolved-mesh count) is actually exercised.
_DECOY_XML = """<?xml version='1.0' encoding='utf-8'?>
<scene version="3.0.0">
  <bsdf type="twosided" id="mat-itu_concrete">
    <bsdf type="diffuse"><rgb name="reflectance" value="0.50 0.50 0.50"/></bsdf>
  </bsdf>
  <shape type="ply" id="mesh-w1">
    <string name="filename" value="m/wall_a.ply"/><ref id="mat-itu_concrete"/>
  </shape>
  <shape type="ply" id="mesh-w2">
    <string name="filename" value="m/wall_b.ply"/><ref id="mat-itu_concrete"/>
  </shape>
  <shape type="ply" id="mesh-w3">
    <string name="filename" value="m/floor.ply"/><ref id="mat-itu_concrete"/>
  </shape>
</scene>
"""


def _uv_box_ply(seed: int) -> bytes:
    """A unit box PLY carrying per-vertex UVs (round-trips through trimesh)."""
    box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    rng = np.random.default_rng(seed)
    uv = rng.random((len(box.vertices), 2))
    box.visual = trimesh.visual.texture.TextureVisuals(uv=uv)
    return box.export(file_type="ply")


def _plain_box_ply() -> bytes:
    """A unit box PLY with NO UV coordinates (flat-color fallback path)."""
    return trimesh.creation.box(extents=(2.0, 2.0, 2.0)).export(file_type="ply")


def _png_bytes(color: tuple[int, int, int]) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (32, 32), color).save(buf, format="PNG")
    return buf.getvalue()


def _write_bundle(root: Path) -> Path:
    """Materialize the textured bundle on disk; return the scene XML path."""
    (root / "meshes_tex").mkdir(parents=True, exist_ok=True)
    (root / "textures").mkdir(parents=True, exist_ok=True)
    (root / "meshes_tex" / "wall_a.ply").write_bytes(_uv_box_ply(1))
    (root / "meshes_tex" / "wall_b.ply").write_bytes(_uv_box_ply(2))
    (root / "meshes_tex" / "floor.ply").write_bytes(_plain_box_ply())
    (root / "textures" / "concrete_a.png").write_bytes(_png_bytes((200, 30, 30)))
    (root / "textures" / "concrete_b.png").write_bytes(_png_bytes((30, 30, 200)))
    xml_path = root / "scene_textured.xml"
    xml_path.write_text(_TEXTURED_XML, encoding="utf-8")
    return xml_path


def _bundle_zip_bytes() -> bytes:
    """A zip of the textured bundle plus a decoy plain XML, macOS cruft, and a
    ../ traversal entry (injected via writestr, which bypasses zip path sanity).
    """
    uv_a = _uv_box_ply(1)
    uv_b = _uv_box_ply(2)
    floor = _plain_box_ply()
    png_a = _png_bytes((200, 30, 30))
    png_b = _png_bytes((30, 30, 200))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        # Textured variant.
        zf.writestr("bundle/scene_textured.xml", _TEXTURED_XML)
        zf.writestr("bundle/meshes_tex/wall_a.ply", uv_a)
        zf.writestr("bundle/meshes_tex/wall_b.ply", uv_b)
        zf.writestr("bundle/meshes_tex/floor.ply", floor)
        zf.writestr("bundle/textures/concrete_a.png", png_a)
        zf.writestr("bundle/textures/concrete_b.png", png_b)
        # Decoy plain XML in ANOTHER dir, resolving the same 3 meshes.
        zf.writestr("plain/scene.xml", _DECOY_XML)
        zf.writestr("plain/m/wall_a.ply", uv_a)
        zf.writestr("plain/m/wall_b.ply", uv_b)
        zf.writestr("plain/m/floor.ply", floor)
        # macOS archive cruft (must be skipped) and a traversal entry (rejected).
        zf.writestr("__MACOSX/._junk", b"junk")
        zf.writestr("../evil.txt", b"pwned")
    return buf.getvalue()


def _glb_geoms_with_texture(glb_bytes: bytes) -> int:
    """Count geometries in a GLB whose baseColor is an actual image."""
    reloaded = trimesh.load(io.BytesIO(glb_bytes), file_type="glb")
    count = 0
    for geometry in reloaded.geometry.values():
        material = getattr(getattr(geometry, "visual", None), "material", None)
        image = getattr(material, "baseColorTexture", None) if material else None
        if image is not None:
            count += 1
    return count


# --------------------------------------------------------------------------- #
# Direct importer                                                              #
# --------------------------------------------------------------------------- #


def test_import_textured_bundle_builds_texture_visuals(tmp_path: Path):
    """import_mitsuba_scene on the bundle yields 3 prims: two textured (UV +
    PNG -> TextureVisuals, base_color_texture persisted) and one flat-color
    (no UV). texture_files maps the two dests to existing sources, and the
    exported GLB reloads with baseColorTexture on exactly the two textured
    geometries."""
    xml_path = _write_bundle(tmp_path)
    library = load_default_library()

    scene, tm_scene, warnings, texture_files = import_mitsuba_scene(
        xml_path, "textured", library, scene_name="Textured"
    )

    assert len(scene.prims) == 3
    by_name = {p.name: p for p in scene.prims}
    assert set(by_name) == {"wall_a", "wall_b", "floor"}

    # The two UV meshes carry a persisted texture reference under visual/textures/.
    assert by_name["wall_a"].visual.base_color_texture == "visual/textures/concrete_a.png"
    assert by_name["wall_b"].visual.base_color_texture == "visual/textures/concrete_b.png"

    # The no-UV mesh keeps a flat preview color and no texture reference.
    assert by_name["floor"].visual.base_color_texture is None
    assert by_name["floor"].visual.base_color_rgba == [0.5, 0.5, 0.5, 1.0]

    # texture_files maps each project-relative dest to an EXISTING source file.
    assert set(texture_files) == {
        "visual/textures/concrete_a.png",
        "visual/textures/concrete_b.png",
    }
    for dest, src in texture_files.items():
        assert Path(src).is_file(), f"{dest} -> {src} does not exist"

    # No missing-texture / no-UV warnings for the textured pair; a clean import.
    assert not any("texture not found" in w for w in warnings)
    assert not any("could not load texture" in w for w in warnings)

    # The exported GLB embeds baseColor images on exactly the two textured geoms.
    glb_bytes = tm_scene.export(file_type="glb")
    assert _glb_geoms_with_texture(glb_bytes) == 2

    # All three concrete-ish materials resolve to the library's itu_concrete
    # (exact id, id-suffix .001/.002, and the plain id all map by token).
    assert {p.rf.material_id for p in scene.prims} == {"itu_concrete"}


# --------------------------------------------------------------------------- #
# Zip end-to-end via TestClient                                               #
# --------------------------------------------------------------------------- #


def test_import_zip_bundle_persists_textures_and_wins_tie(api_client):
    """The zip path picks the textured XML over the plain decoy (equal resolved
    mesh count, textured wins), copies both PNGs into visual/textures/, records
    them in provenance, escapes nothing, and the served scene carries the
    per-prim base_color_texture. The api_client fixture redirects project roots
    under tmp_path, so nothing leaks into the real projects/ dir."""
    resp = api_client.post(
        "/api/projects/import",
        files=[("file", ("bundle.zip", _bundle_zip_bytes(), "application/zip"))],
        data={"project_id": "ziptex", "name": "Zip Tex", "environment": "outdoor"},
    )
    assert resp.status_code == 201, resp.text
    project_dir = Path(resp.json()["path"])

    # Textures persisted at full resolution under visual/textures/.
    textures_dir = project_dir / "visual" / "textures"
    assert textures_dir.is_dir()
    assert sorted(p.name for p in textures_dir.iterdir()) == [
        "concrete_a.png",
        "concrete_b.png",
    ]

    # Provenance points at the TEXTURED xml (tie won) and counts the two textures.
    provenance = json.loads((project_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["source_xml"] == "bundle/scene_textured.xml"
    assert provenance["textures_persisted"] == 2

    # The traversal entry escaped nothing: no evil.txt under the project dir or
    # anywhere the extractor could have reached from it.
    assert list(project_dir.rglob("evil.txt")) == []
    assert not (project_dir.parent / "evil.txt").exists()

    # The served scene carries per-prim texture bindings for the two UV meshes.
    scene = api_client.get("/api/projects/ziptex/scene").json()
    tex_by_name = {p["name"]: p["visual"]["base_color_texture"] for p in scene["prims"]}
    assert tex_by_name["wall_a"] == "visual/textures/concrete_a.png"
    assert tex_by_name["wall_b"] == "visual/textures/concrete_b.png"
    assert tex_by_name["floor"] is None

    # The persisted textures are servable as project assets.
    asset = api_client.get("/api/projects/ziptex/assets/visual/textures/concrete_a.png")
    assert asset.status_code == 200


def test_import_zip_without_xml_returns_400(api_client):
    """A zip with no <scene> XML is rejected with the specific bundle message."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("bundle/readme.txt", b"just some files, no scene here")
        zf.writestr("bundle/meshes_tex/wall_a.ply", _uv_box_ply(1))
    resp = api_client.post(
        "/api/projects/import",
        files=[("file", ("bundle.zip", buf.getvalue(), "application/zip"))],
        data={"project_id": "noxml", "name": "No XML", "environment": "auto"},
    )
    assert resp.status_code == 400, resp.text
    assert "no Mitsuba <scene> XML" in resp.json()["detail"]
    assert "noxml" not in {
        p["project_id"] for p in api_client.get("/api/projects").json()
    }


# --------------------------------------------------------------------------- #
# Plain-XML regression (untextured path unchanged)                            #
# --------------------------------------------------------------------------- #

# A meshes/-referencing XML with flat rgb bsdfs, uploaded with companion PLYs
# via the multipart `meshes` field (no textures anywhere).
_PLAIN_XML = """<?xml version='1.0' encoding='utf-8'?>
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


def test_import_plain_xml_path_unchanged(api_client):
    """Regression: a plain meshes/-referencing XML uploaded flat with its PLYs
    still imports two prims with mapped materials and no persisted textures -
    the textured pipeline additions must not perturb the untextured path."""
    ply = trimesh.creation.box(extents=(1.0, 1.0, 1.0)).export(file_type="ply")
    resp = api_client.post(
        "/api/projects/import",
        files=[
            ("file", ("scene.xml", _PLAIN_XML.encode("utf-8"), "application/xml")),
            ("meshes", ("wall.ply", ply, "application/octet-stream")),
            ("meshes", ("window.ply", ply, "application/octet-stream")),
        ],
        data={"project_id": "plainxml", "name": "Plain XML", "environment": "indoor"},
    )
    assert resp.status_code == 201, resp.text
    project_dir = Path(resp.json()["path"])

    scene = api_client.get("/api/projects/plainxml/scene").json()
    assert len(scene["prims"]) == 2
    assert {p["rf"]["material_id"] for p in scene["prims"]} == {
        "itu_concrete",
        "itu_glass",
    }
    # No textures: no base_color_texture on any prim, no textures dir, and
    # provenance records zero persisted textures.
    assert all(p["visual"]["base_color_texture"] is None for p in scene["prims"])
    assert not (project_dir / "visual" / "textures").exists()
    provenance = json.loads((project_dir / "provenance.json").read_text(encoding="utf-8"))
    assert provenance["textures_persisted"] == 0
