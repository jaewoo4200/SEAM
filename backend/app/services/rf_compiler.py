"""RF projection compiler: canonical scene -> Sionna RT / Mitsuba projection.

Implements HANDOFF.md section 10 in Mode 2 (group source geometry by RF
material). Outputs, all relative to the project folder:

    rf/meshes/<rf_material_id>.ply   world-space geometry per material group
    rf/generated_scene.xml           Mitsuba 3 XML (version 2.1.0)
    rf/compile_manifest.json         group/material data for backends
    mapping/object_map.json          prim id -> mesh/group mapping
    mapping/face_group_map.json      prim id -> face_group (or null)

Determinism contract: groups are sorted by material id and no timestamps are
emitted, so recompiling an unchanged project is byte-identical. Missing
assets or unextractable meshes degrade to skipped prims plus warnings - a
compile never crashes because the visual projection is incomplete.
"""

import json
import math
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Optional

import numpy as np
import trimesh

from app.schemas.common import SCHEMA_VERSION
from app.schemas.compile import CompileResult, MaterialGroup
from app.schemas.materials import RFMaterial, RFMaterialLibrary
from app.schemas.scene import Actor, Prim, Scene
from app.schemas.validation import ValidationReport
from app.services import mesh_tools

MESH_DIR_REL = "rf/meshes"
SCENE_XML_REL = "rf/generated_scene.xml"
MANIFEST_REL = "rf/compile_manifest.json"
OBJECT_MAP_REL = "mapping/object_map.json"
FACE_GROUP_MAP_REL = "mapping/face_group_map.json"


def compile_project(
    project_dir: Path, scene: Scene, library: RFMaterialLibrary
) -> CompileResult:
    warnings: list[str] = []

    validation = _run_validation(scene, library, project_dir, warnings)
    if validation is not None and not validation.ok:
        errors = [
            f"{issue.code}: {issue.message}"
            for issue in validation.issues
            if issue.severity == "error"
        ]
        return CompileResult(ok=False, errors=errors, validation=validation)

    candidates, skipped = _collect_candidates(scene, library, warnings)
    grouped = _extract_grouped_meshes(project_dir, candidates, skipped, warnings)

    generated: list[str] = []
    material_groups = _export_group_meshes(project_dir, grouped, generated)
    # Actors are compiled as individual shapes (never merged into a material
    # group) so the backend can move each one per frame and re-solve.
    actor_exports = _export_actor_meshes(project_dir, scene, library, warnings, generated)
    _write_bytes(
        project_dir / SCENE_XML_REL,
        _mitsuba_xml(material_groups, actor_exports, library),
    )
    generated.append(SCENE_XML_REL)

    _write_json(
        project_dir / MANIFEST_REL,
        _manifest(scene, library, material_groups, actor_exports, skipped, warnings),
    )
    generated.append(MANIFEST_REL)

    object_map, face_group_map = _mappings(scene, material_groups)
    _write_json(project_dir / OBJECT_MAP_REL, object_map)
    generated.append(OBJECT_MAP_REL)
    _write_json(project_dir / FACE_GROUP_MAP_REL, face_group_map)
    generated.append(FACE_GROUP_MAP_REL)

    return CompileResult(
        ok=True,
        backend_format="mitsuba_xml",
        scene_xml=SCENE_XML_REL,
        manifest=MANIFEST_REL,
        mesh_dir=MESH_DIR_REL,
        material_groups=material_groups,
        generated_files=generated,
        skipped_prim_ids=skipped,
        validation=validation,
        warnings=warnings,
    )


# --------------------------------------------------------------- validation


def _run_validation(
    scene: Scene,
    library: RFMaterialLibrary,
    project_dir: Path,
    warnings: list[str],
) -> Optional[ValidationReport]:
    # Lazy import: the validator is a sibling module that may not be present
    # in a partial checkout; compiling without it degrades to a warning.
    try:
        from app.services.scene_validator import validate_scene
    except ImportError:
        warnings.append("scene_validator unavailable; compiled without validation")
        return None
    return validate_scene(scene, library, project_dir)


# ----------------------------------------------------------- prim selection


def _collect_candidates(
    scene: Scene, library: RFMaterialLibrary, warnings: list[str]
) -> tuple[list[Prim], list[str]]:
    """Mesh prims that carry both an RF material and a resolvable mesh_ref."""
    candidates: list[Prim] = []
    skipped: list[str] = []
    for prim in scene.prims:
        if prim.type != "mesh_primitive":
            continue
        if prim.rf.material_id is None:
            skipped.append(prim.id)
            warnings.append(f"prim {prim.id} has no RF material assigned; skipped")
            continue
        if prim.mesh_ref is None:
            skipped.append(prim.id)
            warnings.append(f"prim {prim.id} has no mesh_ref; skipped")
            continue
        if library.get(prim.rf.material_id) is None:
            skipped.append(prim.id)
            warnings.append(
                f"prim {prim.id} references unknown RF material "
                f"{prim.rf.material_id!r}; skipped"
            )
            continue
        candidates.append(prim)
        if (
            prim.rf.thickness_m is not None
            or prim.rf.scattering_coefficient is not None
            or prim.rf.xpd_coefficient is not None
        ):
            warnings.append(
                f"prim {prim.id} has per-prim RF overrides; Mode 2 grouped "
                "export cannot represent them yet and uses the library "
                "material's parameters"
            )
    return candidates, skipped


def _extract_grouped_meshes(
    project_dir: Path,
    candidates: list[Prim],
    skipped: list[str],
    warnings: list[str],
) -> dict[str, list[tuple[Prim, trimesh.Trimesh]]]:
    """Group extracted world-space meshes by RF material id (Mode 2)."""
    asset_scenes: dict[str, Optional[trimesh.Scene]] = {}
    for uri in sorted({prim.mesh_ref.asset_uri for prim in candidates}):
        tm_scene = mesh_tools.load_visual_scene(project_dir, uri)
        if tm_scene is None:
            warnings.append(
                f"visual asset missing: {uri}; RF projection compiled as a "
                "structured placeholder without its geometry"
            )
        asset_scenes[uri] = tm_scene

    grouped: dict[str, list[tuple[Prim, trimesh.Trimesh]]] = {}
    for prim in candidates:
        tm_scene = asset_scenes[prim.mesh_ref.asset_uri]
        if tm_scene is None:
            skipped.append(prim.id)
            continue
        mesh = mesh_tools.extract_prim_mesh(tm_scene, prim.mesh_ref)
        if mesh is None:
            skipped.append(prim.id)
            warnings.append(
                f"mesh {prim.mesh_ref.mesh_name!r} for prim {prim.id} not found "
                f"in {prim.mesh_ref.asset_uri}; skipped"
            )
            continue
        if prim.mesh_ref.face_group is not None:
            warnings.append(
                "face_group split not yet implemented; using whole mesh "
                f"{prim.mesh_ref.mesh_name!r} for prim {prim.id}"
            )
        grouped.setdefault(prim.rf.material_id, []).append((prim, mesh))
    return grouped


# --------------------------------------------------------------- mesh export


def _export_group_meshes(
    project_dir: Path,
    grouped: dict[str, list[tuple[Prim, trimesh.Trimesh]]],
    generated: list[str],
) -> list[MaterialGroup]:
    mesh_dir = project_dir / MESH_DIR_REL
    mesh_dir.mkdir(parents=True, exist_ok=True)

    material_groups: list[MaterialGroup] = []
    current_files: set[str] = set()
    for material_id in sorted(grouped):
        prim_meshes = grouped[material_id]
        combined = mesh_tools.concatenate_meshes([mesh for _, mesh in prim_meshes])
        # RF meshes are pure geometry: drop visual attributes (vertex colors
        # trigger Mitsuba loader warnings and bloat the PLY).
        combined.visual = trimesh.visual.ColorVisuals(mesh=combined)
        filename = f"{material_id}.ply"
        rel = f"{MESH_DIR_REL}/{filename}"
        (mesh_dir / filename).write_bytes(combined.export(file_type="ply"))
        current_files.add(filename)
        generated.append(rel)
        material_groups.append(
            MaterialGroup(
                rf_material_id=material_id,
                prim_ids=sorted(prim.id for prim, _ in prim_meshes),
                mesh_file=rel,
                face_count=int(len(combined.faces)),
            )
        )

    # Remove meshes from previous compiles that no longer map to a group.
    # Actor meshes (actor_*.ply) are handled by _export_actor_meshes and must
    # not be pruned here.
    for stale in sorted(mesh_dir.glob("*.ply")):
        if stale.name.startswith("actor_"):
            continue
        if stale.name not in current_files:
            stale.unlink()
    return material_groups


# --------------------------------------------------------------- actor export


class ActorExport:
    """One compiled actor: its own mesh, shape id, and material binding.

    Kept out of the material groups so the shape stays individually
    addressable (position/orientation settable per frame) in Sionna RT.
    """

    __slots__ = ("actor_id", "mesh_file", "rf_material_id")

    def __init__(self, actor_id: str, mesh_file: str, rf_material_id: str):
        self.actor_id = actor_id
        self.mesh_file = mesh_file
        self.rf_material_id = rf_material_id


def _actor_box_mesh(actor: Actor) -> trimesh.Trimesh:
    """Box actor baked at its authored pose: a box of ``size_m`` whose base
    sits at ``actor.position`` (position is the ground-contact base center, so
    the mesh centroid is lifted by height/2), rotated by the actor's yaw."""
    length, width, height = (float(v) for v in actor.shape.size_m)
    mesh = trimesh.creation.box(extents=[length, width, height])
    yaw_rad = math.radians(float(actor.orientation_deg[2]))
    if yaw_rad:
        rot = trimesh.transformations.rotation_matrix(yaw_rad, [0.0, 0.0, 1.0])
        mesh.apply_transform(rot)
    px, py, pz = (float(v) for v in actor.position)
    # position is the base-center: lift the origin-centered box by height/2.
    mesh.apply_translation([px, py, pz + height / 2.0])
    return mesh


def _actor_mesh(
    project_dir: Path, actor: Actor, warnings: list[str]
) -> Optional[trimesh.Trimesh]:
    """Build an actor's world-space RF mesh: a primitive box, or a named mesh
    extracted from the visual asset (same machinery as prims)."""
    if actor.shape.type == "mesh" and actor.shape.mesh_ref is not None:
        tm_scene = mesh_tools.load_visual_scene(
            project_dir, actor.shape.mesh_ref.asset_uri
        )
        if tm_scene is None:
            warnings.append(
                f"actor {actor.id} visual asset "
                f"{actor.shape.mesh_ref.asset_uri!r} missing; skipped"
            )
            return None
        mesh = mesh_tools.extract_prim_mesh(tm_scene, actor.shape.mesh_ref)
        if mesh is None:
            warnings.append(
                f"actor {actor.id} mesh {actor.shape.mesh_ref.mesh_name!r} not "
                f"found in {actor.shape.mesh_ref.asset_uri}; skipped"
            )
            return None
        return mesh
    return _actor_box_mesh(actor)


def _export_actor_meshes(
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    warnings: list[str],
    generated: list[str],
) -> list[ActorExport]:
    """Export each actor as its own PLY + return the shape bindings.

    Deterministic: actors are processed sorted by id. Actors with an unknown
    RF material are skipped with a warning (the validator also flags this as an
    UNKNOWN_RF_MATERIAL error, so a validated compile never reaches here)."""
    mesh_dir = project_dir / MESH_DIR_REL
    exports: list[ActorExport] = []
    current_files: set[str] = set()
    for actor in sorted(scene.actors, key=lambda a: a.id):
        mat_id = actor.rf_material_id
        if mat_id is None or library.get(mat_id) is None:
            warnings.append(
                f"actor {actor.id} references unknown RF material {mat_id!r}; skipped"
            )
            continue
        mesh = _actor_mesh(project_dir, actor, warnings)
        if mesh is None:
            continue
        mesh_dir.mkdir(parents=True, exist_ok=True)
        # Pure geometry (drop visual attributes) like the group meshes.
        mesh.visual = trimesh.visual.ColorVisuals(mesh=mesh)
        filename = f"actor_{actor.id}.ply"
        rel = f"{MESH_DIR_REL}/{filename}"
        (mesh_dir / filename).write_bytes(mesh.export(file_type="ply"))
        current_files.add(filename)
        generated.append(rel)
        exports.append(ActorExport(actor.id, rel, mat_id))

    # Prune stale actor meshes from previous compiles.
    for stale in sorted(mesh_dir.glob("actor_*.ply")):
        if stale.name not in current_files:
            stale.unlink()
    return exports


# --------------------------------------------------------------- XML output


def _hex_to_rgb01(hex_color: str) -> str:
    values = (int(hex_color[i : i + 2], 16) / 255.0 for i in (1, 3, 5))
    return " ".join(f"{v:.6f}" for v in values)


def _bsdf_id(material: Optional[RFMaterial], material_id: str) -> str:
    # Sionna RT resolves RadioMaterials from the bsdf id with "mat-" stripped,
    # so ITU-backed materials must emit their Sionna built-in name (library id
    # "ground" -> "mat-itu_medium_dry_ground"). Constant/custom materials keep
    # their library id; backends attach their parameters from the manifest.
    if (
        material is not None
        and material.model == "itu_frequency_dependent"
        and material.itu_name
    ):
        return f"mat-{material.itu_name}"
    return f"mat-{material_id}"


def _emit_bsdf(root: ET.Element, bsdf_id: str, material: Optional[RFMaterial]) -> None:
    """One bsdf element per distinct id.

    ITU-backed materials use a plain diffuse bsdf: Sionna RT's loader converts
    ids matching (mat-)itu_* into built-in ITU RadioMaterials, and reflectance
    (the preview color) is visual-only, never RF truth.

    Everything else must be a "radio-material" plugin carrying its constant
    parameters directly - Sionna 1.x load_scene REJECTS shapes whose bsdf is
    not a radio material, so a diffuse bsdf for asphalt_custom would make the
    whole scene unloadable.
    """
    if (
        material is not None
        and material.model == "itu_frequency_dependent"
        and material.itu_name
    ):
        bsdf = ET.SubElement(root, "bsdf", {"type": "diffuse", "id": bsdf_id})
        ET.SubElement(
            bsdf,
            "rgb",
            {"name": "reflectance", "value": _hex_to_rgb01(material.preview_color)},
        )
        return

    bsdf = ET.SubElement(root, "bsdf", {"type": "radio-material", "id": bsdf_id})
    props: list[tuple[str, Optional[float]]] = [
        ("relative_permittivity", material.relative_permittivity if material else None),
        ("conductivity", material.conductivity_s_per_m if material else None),
        ("scattering_coefficient", material.scattering_coefficient if material else None),
        ("xpd_coefficient", material.xpd_coefficient if material else None),
        ("thickness", material.thickness_m if material else None),
    ]
    for name, value in props:
        if value is not None:
            ET.SubElement(bsdf, "float", {"name": name, "value": f"{value:g}"})


def _mitsuba_xml(
    material_groups: list[MaterialGroup],
    actor_exports: list["ActorExport"],
    library: RFMaterialLibrary,
) -> bytes:
    """Mitsuba 3 scene XML for Sionna RT.

    bsdfs are deduplicated by id: two library materials mapping to the same
    ITU built-in share one bsdf. Actor shapes reference the same material
    bsdfs as static geometry (same _bsdf_id machinery) but are emitted as
    separate shapes so the backend can move them per frame.
    """
    root = ET.Element("scene", {"version": "2.1.0"})
    emitted: set[str] = set()

    def emit_bsdf_for(material_id: str) -> None:
        material = library.get(material_id)
        bsdf_id = _bsdf_id(material, material_id)
        if bsdf_id in emitted:
            return
        emitted.add(bsdf_id)
        _emit_bsdf(root, bsdf_id, material)

    for group in material_groups:
        emit_bsdf_for(group.rf_material_id)
    for actor in actor_exports:
        emit_bsdf_for(actor.rf_material_id)

    for group in material_groups:
        material = library.get(group.rf_material_id)
        shape = ET.SubElement(
            root, "shape", {"type": "ply", "id": f"shape-{group.rf_material_id}"}
        )
        ET.SubElement(
            shape,
            "string",
            {"name": "filename", "value": f"meshes/{group.rf_material_id}.ply"},
        )
        ET.SubElement(
            shape, "ref", {"id": _bsdf_id(material, group.rf_material_id), "name": "bsdf"}
        )
        ET.SubElement(shape, "boolean", {"name": "face_normals", "value": "true"})

    for actor in actor_exports:
        material = library.get(actor.rf_material_id)
        shape = ET.SubElement(
            root, "shape", {"type": "ply", "id": f"shape-actor-{actor.actor_id}"}
        )
        # mesh_file is "rf/meshes/actor_<id>.ply"; XML filenames are relative
        # to the XML (which lives in rf/), so strip the leading "rf/".
        filename = actor.mesh_file
        if filename.startswith("rf/"):
            filename = filename[len("rf/"):]
        ET.SubElement(shape, "string", {"name": "filename", "value": filename})
        ET.SubElement(
            shape, "ref", {"id": _bsdf_id(material, actor.rf_material_id), "name": "bsdf"}
        )
        ET.SubElement(shape, "boolean", {"name": "face_normals", "value": "true"})
    ET.indent(root, space="    ")
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)


# ---------------------------------------------------------- manifest / maps


def _custom_material(material: Optional[RFMaterial]) -> Optional[dict]:
    if material is None or material.model != "constant":
        return None
    return {
        "relative_permittivity": material.relative_permittivity,
        "conductivity_s_per_m": material.conductivity_s_per_m,
        "thickness_m": material.thickness_m,
        "scattering_coefficient": material.scattering_coefficient,
        "xpd_coefficient": material.xpd_coefficient,
    }


def _manifest(
    scene: Scene,
    library: RFMaterialLibrary,
    material_groups: list[MaterialGroup],
    actor_exports: list["ActorExport"],
    skipped: list[str],
    warnings: list[str],
) -> dict:
    groups = []
    for group in material_groups:
        material = library.get(group.rf_material_id)
        groups.append(
            {
                "rf_material_id": group.rf_material_id,
                # itu_name lets backends resolve ITU built-ins whose library
                # id differs from the Sionna material name (e.g. "metal").
                "itu_name": material.itu_name if material else None,
                "prim_ids": group.prim_ids,
                "mesh_file": group.mesh_file,
                "face_count": group.face_count,
                "custom_material": _custom_material(material),
            }
        )
    # Actors: individual shapes the backend moves per frame. Also carry any
    # actor material's constant parameters so _apply_custom_materials can
    # re-sync them (their bsdf may be shared with a static group or unique).
    actors = []
    for actor in actor_exports:
        material = library.get(actor.rf_material_id)
        actors.append(
            {
                "actor_id": actor.actor_id,
                "mesh_file": actor.mesh_file,
                "rf_material_id": actor.rf_material_id,
                "itu_name": material.itu_name if material else None,
                "custom_material": _custom_material(material),
            }
        )
    return {
        "schema_version": SCHEMA_VERSION,
        "scene_id": scene.scene_id,
        "groups": groups,
        "actors": actors,
        "skipped_prim_ids": skipped,
        "warnings": warnings,
    }


def _mappings(
    scene: Scene, material_groups: list[MaterialGroup]
) -> tuple[dict, dict]:
    """prim id -> mesh/group maps covering EVERY mesh prim in the scene.

    Uncompiled prims (no RF material / skipped) keep null rf fields, so the
    file has one stable schema whether written by the demo generator or by a
    compile - it is a full visual<->RF mapping, not just the compiled subset.
    """
    group_by_prim: dict[str, MaterialGroup] = {
        prim_id: group for group in material_groups for prim_id in group.prim_ids
    }
    object_map: dict[str, dict] = {}
    face_group_map: dict[str, Optional[str]] = {}
    for prim in scene.prims:
        if prim.mesh_ref is None:
            continue
        group = group_by_prim.get(prim.id)
        object_map[prim.id] = {
            "mesh_name": prim.mesh_ref.mesh_name,
            "rf_material_id": group.rf_material_id if group else None,
            "group_mesh_file": group.mesh_file if group else None,
        }
        face_group_map[prim.id] = prim.mesh_ref.face_group
    return object_map, face_group_map


# ------------------------------------------------------------------ file IO


def _write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def _write_json(path: Path, obj: dict) -> None:
    _write_bytes(path, (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
