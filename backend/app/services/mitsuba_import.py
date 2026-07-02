"""Import a Sionna/Mitsuba scene XML into a canonical SionnaTwin scene.

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

import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import trimesh

from app.schemas.materials import RFMaterialLibrary
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene, SceneAssets, VisualBinding


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
        # preview color: reflectance (nested diffuse) or color
        info["color_rgba"] = _rgb(bsdf.find(".//rgb[@name='reflectance']")) or _rgb(
            bsdf.find("rgb[@name='color']")
        )
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


def _shape_base(shape_id: str, filename: str) -> str:
    for prefix in ("mesh-", "shape-"):
        if shape_id.startswith(prefix):
            return shape_id[len(prefix):]
    stem = Path(filename).stem
    return stem.removeprefix("itu_") or shape_id


def _sanitize(name: str) -> str:
    return re.sub(r"[^a-z0-9_]+", "_", name.lower()).strip("_") or "obj"


def import_mitsuba_scene(
    xml_path: Path,
    scene_id: str,
    library: RFMaterialLibrary,
    scene_name: str = "",
) -> tuple[Scene, "trimesh.Scene", list[str]]:
    """Parse the XML and build (canonical Scene, combined visual trimesh.Scene,
    warnings). The GLB is exported by the caller."""
    warnings: list[str] = []
    root = ET.parse(xml_path).getroot()
    xml_dir = xml_path.parent
    materials = _parse_materials(root)
    class_map = _class_to_library_id(library)

    tm_scene = trimesh.Scene()
    prims: list[Prim] = []
    used_names: set[str] = set()

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
            warnings.append(f"mesh not found, prim created without geometry: {filename}")
            mesh = None
        else:
            loaded = trimesh.load(mesh_path, force="mesh")
            mesh = loaded if isinstance(loaded, trimesh.Trimesh) else None
            if mesh is None:
                warnings.append(f"could not load mesh as a single Trimesh: {filename}")

        info = materials.get(mat_id, {}) if mat_id else {}
        color = info.get("color_rgba")
        if mesh is not None:
            if color:
                mesh.visual = trimesh.visual.ColorVisuals(
                    mesh=mesh, face_colors=[int(c * 255) for c in color[:3]] + [255]
                )
            tm_scene.add_geometry(mesh, geom_name=base, node_name=base)

        # Resolve RF material: itu class -> library id, else constant/unknown.
        rf_id: Optional[str] = None
        itu_class = info.get("itu_class")
        if itu_class and itu_class in class_map:
            rf_id = class_map[itu_class]
        elif info.get("constant"):
            rf_id = "unknown_rf"
            warnings.append(
                f"shape {base}: constant material {mat_id} mapped to unknown_rf "
                "(no matching library material)"
            )
        elif mat_id:
            # try token match against known classes
            for token, lib_id in class_map.items():
                if token in (mat_id or "").lower():
                    rf_id = lib_id
                    break
        if rf_id is None:
            rf_id = "unknown_rf"
            warnings.append(f"shape {base}: could not resolve RF material from {mat_id}")

        prims.append(
            Prim(
                id=f"/{base}",
                name=base,
                type="mesh_primitive",
                semantic_tags=[itu_class] if itu_class else [],
                mesh_ref=MeshRef(
                    asset_uri="visual/scene.glb", mesh_name=base, face_group=None
                ),
                visual=VisualBinding(material_name=mat_id, base_color_rgba=color),
                rf=RFBinding(
                    material_id=rf_id,
                    assignment_status="user_confirmed",
                    assignment_sources=["imported_xml"],
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
    return scene, tm_scene, warnings
