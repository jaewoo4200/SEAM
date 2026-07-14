"""Import a Sionna/Mitsuba scene XML into a canonical SEAM scene.

Handles the two bsdf forms the FTC bundle uses:
- ``<bsdf type="twosided"><bsdf type="diffuse"><rgb name="reflectance"/>`` with
  id ``mat-itu_<class>`` (indoor lab_room);
- ``<bsdf type="itu-radio-material"><string name="type" value="<class>"/>`` with
  an arbitrary id and an ``<rgb name="color"/>`` (outdoor FTC), plus the plain
  ``radio-material`` constant form our own compiler emits.

Each ``<shape type="ply">`` becomes a prim whose RF material is resolved to a
project library material by ITU class (or kept as a constant). The referenced
PLYs are combined into one visual GLB (named per shape) so the existing viewer
renders imported scenes with per-prim picking. These Sionna XMLs are already
Z-up (the FTC conversion bakes the +90 deg X rotation), so no axis fix is
applied here.
"""

import math
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh

from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene, SceneAssets, VisualBinding
from seam_studio.services.scene_validator import (
    itu_band_max_hz,
    itu_out_of_band,
    itu_safe_alternative,
)

# Default project simulation frequency (matches import_scene.py's default
# 28 GHz SimulationConfig). Passed to import_mitsuba_scene so out-of-band ITU
# materials can be remapped to a band-safe alternative at import time.
_DEFAULT_FREQUENCY_HZ = 28e9


# Token fallbacks for material ids that don't carry an ITU class string
# (e.g. the outdoor scene's constant "mat-FTC-ground"). Checked as substrings.
_SEMANTIC_FALLBACK: dict[str, str] = {
    "ground": "ground_28ghz",
    "concrete": "itu_concrete",
    "glass": "itu_glass",
    "metal": "metal",
    "brick": "itu_brick",
    "wood": "itu_wood",
}


def _class_to_library_id(library: RFMaterialLibrary) -> dict[str, str]:
    """ITU class name (e.g. 'concrete', 'metal') -> project material id."""
    mapping: dict[str, str] = {}
    for mat in library.materials:
        if mat.itu_name:
            mapping[mat.itu_name] = mat.id  # 'itu_concrete'
            mapping[mat.itu_name.removeprefix("itu_")] = mat.id  # 'concrete'
    return mapping


def _rgb(el: Optional[ET.Element]) -> Optional[list[float]]:
    if el is None:
        return None
    val = el.get("value", "").split()
    try:
        return [float(v) for v in val[:3]] + [1.0]
    except ValueError:
        return None


def _parse_materials(root: ET.Element) -> dict[str, dict]:
    """bsdf id -> {itu_class, color_rgba, constant params}."""
    materials: dict[str, dict] = {}
    for bsdf in root.findall("bsdf"):
        bid = bsdf.get("id")
        if not bid:
            continue
        btype = bsdf.get("type")
        info: dict = {"itu_class": None, "color_rgba": None, "constant": None}
        # itu class from id "mat-itu_<class>" or from a child <string name="type">
        if bid.startswith("mat-itu_"):
            info["itu_class"] = bid[len("mat-itu_"):]
        type_str = bsdf.find("string[@name='type']")
        if type_str is not None:
            info["itu_class"] = type_str.get("value")
        # preview color: reflectance (nested diffuse), color, or the
        # principled bsdf's base_color (textured/preview XML variants).
        info["color_rgba"] = (
            _rgb(bsdf.find(".//rgb[@name='reflectance']"))
            or _rgb(bsdf.find("rgb[@name='color']"))
            or _rgb(bsdf.find(".//rgb[@name='base_color']"))
        )
        # bitmap texture (Blender textured exports nest it in the diffuse
        # reflectance, through the twosided wrapper). The filename is relative
        # to the XML's directory; the shape loop resolves + persists it.
        tex_el = bsdf.find(".//texture[@type='bitmap']/string[@name='filename']")
        info["texture_file"] = tex_el.get("value") if tex_el is not None else None
        # constant radio-material params
        if btype == "radio-material":
            def fget(name: str) -> Optional[float]:
                f = bsdf.find(f"float[@name='{name}']")
                return float(f.get("value")) if f is not None else None

            info["constant"] = {
                "relative_permittivity": fget("relative_permittivity"),
                "conductivity_s_per_m": fget("conductivity"),
                "thickness_m": fget("thickness"),
            }
        materials[bid] = info
    return materials


def _parse_transform(shape: ET.Element) -> Optional[np.ndarray]:
    """Compose a shape's Mitsuba <transform name="to_world"> into a 4x4 matrix.

    Handles rotate (axis + angle deg), translate, scale, and matrix. Applied in
    document order. Mitsuba's to_world maps object -> world, so the composed
    matrix is the product with later children on the left."""
    xform = shape.find("transform[@name='to_world']")
    if xform is None:
        return None
    M = np.eye(4)

    def vec(el, default):
        v = el.get("value")
        if v is not None:
            parts = [float(x) for x in v.split()]
            return parts if len(parts) == 3 else [parts[0]] * 3
        return [float(el.get(a, d)) for a, d in zip("xyz", default)]

    for child in list(xform):
        T = np.eye(4)
        if child.tag == "rotate":
            # Mitsuba accepts both <rotate x="1" angle="90"/> and
            # <rotate value="ax ay az" angle="90"/>.
            if child.get("value") is not None:
                axis = np.array([float(v) for v in child.get("value").split()[:3]])
            else:
                axis = np.array([float(child.get(a, 0.0)) for a in "xyz"])
            angle = math.radians(float(child.get("angle", 0.0)))
            if np.linalg.norm(axis) > 0:
                T = trimesh.transformations.rotation_matrix(angle, axis)
        elif child.tag == "translate":
            T[:3, 3] = vec(child, ("0", "0", "0"))
        elif child.tag == "scale":
            T[:3, :3] = np.diag(vec(child, ("1", "1", "1")))
        elif child.tag == "matrix":
            vals = [float(x) for x in child.get("value", "").split()]
            if len(vals) == 16:
                T = np.array(vals).reshape(4, 4)
        M = T @ M
    return M


def _shape_base(shape_id: str, filename: str) -> str:
    for prefix in ("mesh-", "shape-"):
        if shape_id.startswith(prefix):
            return shape_id[len(prefix):]
    stem = Path(filename).stem
    return stem.removeprefix("itu_") or shape_id


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "obj"


# Longest side (px) of texture images embedded in the visual GLB. Originals
# can be 1024+ px and multi-MB each (a 60-building campus would produce a
# >100 MB GLB); the viewer only needs facade-level detail, while AI evidence
# crops read the ORIGINAL files persisted under visual/textures/.
_GLB_TEXTURE_MAX_PX = 512


def import_mitsuba_scene(
    xml_path: Path,
    scene_id: str,
    library: RFMaterialLibrary,
    scene_name: str = "",
    default_frequency_hz: float = _DEFAULT_FREQUENCY_HZ,
) -> tuple[Scene, "trimesh.Scene", list[str], dict[str, Path]]:
    """Parse the XML and build (canonical Scene, combined visual trimesh.Scene,
    warnings, texture_files). The GLB is exported by the caller.

    ``texture_files`` maps a project-relative destination path (e.g.
    ``visual/textures/tex_000.png``) to the absolute source file the XML
    referenced; the caller copies them into the project so the original
    full-resolution textures survive for AI evidence crops. Prims whose mesh
    has UVs and whose bsdf carries a bitmap texture get real
    ``TextureVisuals`` in the GLB (downscaled to ``_GLB_TEXTURE_MAX_PX``) and
    ``VisualBinding.base_color_texture`` pointing at the persisted original.

    ``default_frequency_hz`` is the frequency of the project's default
    SimulationConfig (28 GHz by default). When a mapped ITU material is out of
    its ITU-R P.2040 band at that frequency, the binding is remapped to a
    band-safe alternative (when one exists) so the first solve does not fail
    with "not defined for this frequency"; the original mapping is preserved in
    assignment_sources and a warning is emitted."""
    warnings: list[str] = []
    root = ET.parse(xml_path).getroot()
    xml_dir = xml_path.parent
    materials = _parse_materials(root)
    class_map = _class_to_library_id(library)

    # Blender's render-flavored Mitsuba exports (with a <sensor> camera) keep
    # shapes Y-up and bake NO to_world; the Sionna-converted variants add a
    # per-shape rotate x=90. Importing the render flavor gives a sideways
    # scene in our Z-up world - warn instead of guessing the up-axis.
    if root.find("sensor") is not None and all(
        s.find("transform[@name='to_world']") is None
        for s in root.findall("shape")
    ):
        warnings.append(
            "this XML looks like a render export (camera present, shapes carry "
            "no to_world transform): geometry is likely Y-up and will appear "
            "sideways. Prefer the Sionna-converted XML variant (per-shape "
            "rotate x=90) for RF work."
        )

    tm_scene = trimesh.Scene()
    prims: list[Prim] = []
    used_names: set[str] = set()
    # project-relative dest -> absolute source, plus source -> dest to reuse
    # one persisted file for materials sharing a texture.
    texture_files: dict[str, Path] = {}
    texture_dest_by_source: dict[Path, str] = {}

    for shape in root.findall("shape"):
        if shape.get("type") != "ply":
            continue
        fname_el = shape.find("string[@name='filename']")
        ref_el = shape.find("ref")
        if fname_el is None:
            continue
        filename = fname_el.get("value")
        mat_id = ref_el.get("id") if ref_el is not None else None
        base = _sanitize(_shape_base(shape.get("id", ""), filename))
        if base in used_names:
            base = f"{base}_{len(used_names)}"
        used_names.add(base)

        mesh_path = (xml_dir / filename).resolve()
        if not mesh_path.is_file():
            # Skip entirely: a prim whose mesh_ref points at nothing in the
            # combined GLB would be a dangling reference validation can't see.
            warnings.append(f"mesh not found, shape skipped: {filename}")
            continue
        loaded = trimesh.load(mesh_path, force="mesh")
        mesh = loaded if isinstance(loaded, trimesh.Trimesh) else None
        if mesh is None:
            warnings.append(f"could not load mesh as a single Trimesh, shape skipped: {filename}")
            continue
        # Apply the shape's to_world transform (e.g. the +90 deg X
        # Y-up->Z-up fix the outdoor scenes carry per shape).
        xform = _parse_transform(shape)
        if xform is not None:
            mesh = mesh.copy()
            mesh.apply_transform(xform)

        info = materials.get(mat_id, {}) if mat_id else {}
        color = info.get("color_rgba")
        # Textured bsdf + UV-carrying mesh -> real TextureVisuals in the GLB
        # (viewer renders it, AI crops read it). Any missing piece (no UVs, no
        # file, PIL failure) falls back to the flat preview color, keeping
        # untextured bundles byte-identical to before.
        base_color_texture: Optional[str] = None
        tex_rel = info.get("texture_file")
        if tex_rel:
            tex_src = (xml_dir / tex_rel).resolve()
            uv = getattr(mesh.visual, "uv", None)
            if not tex_src.is_file():
                warnings.append(
                    f"shape {base}: texture not found, using flat color: {tex_rel}"
                )
            elif uv is None or len(uv) != len(mesh.vertices):
                warnings.append(
                    f"shape {base}: mesh has no UV coordinates; texture "
                    f"{tex_rel} ignored"
                )
            else:
                try:
                    import io as _io

                    from PIL import Image

                    img = Image.open(tex_src)
                    img.load()
                    if img.mode not in ("RGB", "RGBA"):
                        img = img.convert("RGB")
                    glb_img = img.copy()
                    glb_img.thumbnail(
                        (_GLB_TEXTURE_MAX_PX, _GLB_TEXTURE_MAX_PX),
                        Image.Resampling.LANCZOS,
                    )
                    # Re-encode the embedded copy as JPEG (PIL format drives
                    # the GLB mimeType): 60 campus facades as PNG would bloat
                    # the GLB by tens of MB; JPEG keeps it a few MB. RGBA
                    # (alpha) stays PNG since JPEG cannot carry it.
                    if glb_img.mode == "RGB":
                        buf = _io.BytesIO()
                        glb_img.save(buf, format="JPEG", quality=85)
                        buf.seek(0)
                        glb_img = Image.open(buf)
                        glb_img.load()
                    # Mitsuba texture coordinates are image-space (v origin at
                    # the TOP-left), but trimesh's TextureVisuals expects
                    # OpenGL bottom-left - its glTF exporter then flips v back
                    # to the top-left origin the glTF spec wants. Without this
                    # conversion every atlas samples vertically mirrored and
                    # facades render as scrambled patches (live-verified on
                    # the HYU bundle: corr(world Y, v) was exactly -1).
                    uv_gl = np.array(uv, dtype=np.float64, copy=True)
                    uv_gl[:, 1] = 1.0 - uv_gl[:, 1]
                    # Explicit PBR material: TextureVisuals(image=...) alone
                    # makes trimesh emit SimpleMaterial defaults - a 0.4-grey
                    # baseColorFactor and NO metallicFactor (glTF default 1.0,
                    # fully metallic) - which renders photo textures as dark
                    # broken-looking patches in three.js (live-verified).
                    mesh.visual = trimesh.visual.texture.TextureVisuals(
                        uv=uv_gl,
                        material=trimesh.visual.material.PBRMaterial(
                            baseColorTexture=glb_img,
                            metallicFactor=0.0,
                            roughnessFactor=1.0,
                        ),
                    )
                    dest = texture_dest_by_source.get(tex_src)
                    if dest is None:
                        leaf = Path(tex_rel.replace("\\", "/")).name
                        dest = f"visual/textures/{leaf}"
                        if dest in texture_files:
                            # same basename from a different dir: disambiguate
                            dest = f"visual/textures/{base}_{leaf}"
                        texture_files[dest] = tex_src
                        texture_dest_by_source[tex_src] = dest
                    base_color_texture = dest
                except Exception as exc:
                    warnings.append(
                        f"shape {base}: could not load texture {tex_rel} "
                        f"({exc}); using flat color"
                    )
        if base_color_texture is None and color:
            mesh.visual = trimesh.visual.ColorVisuals(
                mesh=mesh, face_colors=[int(c * 255) for c in color[:3]] + [255]
            )
        tm_scene.add_geometry(mesh, geom_name=base, node_name=base)

        # Resolve RF material: itu class -> library id; then a token fallback
        # (e.g. a constant "ground" material -> the 28 GHz-safe ground); else
        # unknown_rf. The FTC occlusion blocker legitimately stays unknown.
        rf_id: Optional[str] = None
        itu_class = info.get("itu_class")
        if itu_class and itu_class in class_map:
            rf_id = class_map[itu_class]
        if rf_id is None and mat_id:
            low = mat_id.lower()
            for token, lib_id in {**class_map, **_SEMANTIC_FALLBACK}.items():
                if token in low:
                    rf_id = lib_id
                    break
        if rf_id is None:
            rf_id = "unknown_rf"
            warnings.append(
                f"shape {base}: material {mat_id} mapped to unknown_rf "
                "(no matching library material)"
            )

        # ITU frequency-band guardrail at import time. If the mapped material is
        # an ITU model that is undefined at the project's default frequency
        # (e.g. an ITU ground material at 28 GHz), remap to the band-safe
        # constant alternative when one exists so the first solve does not fail
        # with "not defined for this frequency". Materials without a safe swap
        # keep their binding but still warn with the suggested fix. Uses the
        # single-source band table in scene_validator (no local duplication).
        assignment_sources = ["imported_xml"]
        mapped_mat = library.get(rf_id)
        if itu_out_of_band(mapped_mat, default_frequency_hz):
            band_max = itu_band_max_hz(mapped_mat.category)
            safe_id = itu_safe_alternative(mapped_mat.category)
            freq_ghz = default_frequency_hz / 1e9
            band_ghz = band_max / 1e9 if band_max is not None else None
            if safe_id is not None and library.get(safe_id) is not None:
                original_id = rf_id
                rf_id = safe_id
                assignment_sources = [
                    f"imported_xml:{original_id}->{safe_id} "
                    f"(out of ITU band at {freq_ghz:.0f} GHz)"
                ]
                warnings.append(
                    f"shape {base}: ITU material {original_id!r} is undefined "
                    f"above ~{band_ghz:.0f} GHz (ITU-R P.2040); remapped to the "
                    f"28 GHz-safe {safe_id!r} for the project's {freq_ghz:.0f} GHz "
                    "default. Reassign in the RF Materials tab if you lower the "
                    "frequency."
                )
            else:
                warnings.append(
                    f"shape {base}: ITU material {rf_id!r} is undefined above "
                    f"~{band_ghz:.0f} GHz (ITU-R P.2040) but is used at the "
                    f"project's {freq_ghz:.0f} GHz default; lower the frequency "
                    f"below {band_ghz:.0f} GHz or assign a constant-model material."
                )

        prims.append(
            Prim(
                id=f"/{base}",
                name=base,
                type="mesh_primitive",
                semantic_tags=[itu_class] if itu_class else [],
                mesh_ref=MeshRef(
                    asset_uri="visual/scene.glb", mesh_name=base, face_group=None
                ),
                visual=VisualBinding(
                    material_name=mat_id,
                    base_color_rgba=color,
                    base_color_texture=base_color_texture,
                ),
                rf=RFBinding(
                    material_id=rf_id,
                    assignment_status="user_confirmed",
                    assignment_sources=assignment_sources,
                    confidence=1.0,
                ),
            )
        )

    scene = Scene(
        scene_id=scene_id,
        name=scene_name or scene_id,
        assets=SceneAssets(visual_scene_uri="visual/scene.glb"),
        prims=prims,
    )
    return scene, tm_scene, warnings, texture_files
