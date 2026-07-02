"""Pure scene validation.

``validate_scene`` inspects the canonical scene against the project RF
material library and returns a ValidationReport. It never mutates the scene
and never raises for content problems - every finding becomes a typed issue
(codes documented in app/schemas/validation.py).
"""

import re
from pathlib import Path
from typing import Optional

from app.schemas.materials import RFMaterialLibrary
from app.schemas.scene import Prim, Scene
from app.schemas.validation import ValidationIssue, ValidationReport

# Evidence keywords for the VISUAL_RF_MISMATCH heuristic. Matching is
# exact-token over the prim's visual evidence text, so e.g. "fiberglass" does
# not hit "glass" and false positives stay rare (rule 3: visual info is only
# suggestion evidence, never RF truth).
# Keep in sync with ai_provider's rule table: the app must never flag an
# assignment its own suggester just recommended. Where both tables match a
# prim (e.g. name "walls" + visual "brick"), the exactly-one-category rule
# below keeps ambiguous evidence silent.
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "glass": ("glass", "window", "pane"),
    "concrete": ("concrete", "cement", "wall"),
    "brick": ("brick",),
    "metal": ("metal", "steel", "aluminum"),
    "wood": ("wood", "timber", "trunk", "bark"),
    "vegetation": ("tree", "leaf", "foliage", "grass"),
    "asphalt": ("asphalt", "road", "street"),
}

# Assigned categories that never trigger a mismatch: they carry no visual
# expectation ("unknown_rf" is a deliberate placeholder).
_MISMATCH_EXEMPT_CATEGORIES = {"unknown", "generic", ""}

# Category pairs that are physically plausible together and must not warn:
# grass-covered soil is legitimately "ground", a road shoulder may be tagged
# terrain, etc. Only genuinely suspicious contradictions (glass vs concrete)
# are worth the user's attention.
_COMPATIBLE_CATEGORIES: tuple[frozenset[str], ...] = (
    frozenset({"ground", "vegetation", "asphalt"}),
    # A tree trunk is wood; a leafy canopy is vegetation - both grow on the
    # same prim names/tags, so never contradict each other.
    frozenset({"wood", "vegetation"}),
)


def _categories_compatible(a: str, b: str) -> bool:
    return any(a in fam and b in fam for fam in _COMPATIBLE_CATEGORIES)

_SUGGESTED_STATUSES = ("rule_suggested", "ai_suggested")


def _evidence_tokens(prim: Prim) -> set[str]:
    parts: list[str] = [prim.name]
    parts.extend(prim.semantic_tags)
    if prim.visual is not None:
        if prim.visual.material_name:
            parts.append(prim.visual.material_name)
        if prim.visual.material_id:
            parts.append(prim.visual.material_id)
        if prim.visual.base_color_texture:
            # Basename only: directory names like "textures/" are not evidence.
            uri = prim.visual.base_color_texture.replace("\\", "/")
            parts.append(uri.rsplit("/", 1)[-1])
    tokens = re.split(r"[^a-z0-9]+", " ".join(parts).lower())
    return {t for t in tokens if t}


def _evidence_categories(prim: Prim) -> set[str]:
    tokens = _evidence_tokens(prim)
    return {cat for cat, kws in CATEGORY_KEYWORDS.items() if tokens & set(kws)}


def _mismatch_issue(prim: Prim, library: RFMaterialLibrary) -> Optional[ValidationIssue]:
    if prim.rf.material_id is None:
        return None
    # Calibrated assignments outrank any name-based heuristic.
    if prim.rf.assignment_status == "measurement_calibrated":
        return None
    material = library.get(prim.rf.material_id)
    if material is None:
        return None  # reported separately as UNKNOWN_RF_MATERIAL
    assigned_category = (material.category or "").strip().lower()
    if assigned_category in _MISMATCH_EXEMPT_CATEGORIES:
        return None
    hits = _evidence_categories(prim)
    # Require unambiguous evidence: exactly one category indicated.
    if len(hits) != 1:
        return None
    evidence_category = next(iter(hits))
    if evidence_category == assigned_category:
        return None
    if _categories_compatible(evidence_category, assigned_category):
        return None
    return ValidationIssue(
        severity="warning",
        code="VISUAL_RF_MISMATCH",
        message=(
            f"visual evidence for prim {prim.id!r} suggests category "
            f"'{evidence_category}', but assigned RF material "
            f"{material.id!r} has category '{assigned_category}'"
        ),
        prim_id=prim.id,
    )


def validate_scene(
    scene: Scene,
    library: RFMaterialLibrary,
    project_dir: Path | None = None,
) -> ValidationReport:
    issues: list[ValidationIssue] = []
    library_ids = library.ids()
    prim_ids = {p.id for p in scene.prims}

    # Defense in depth: the Scene model already rejects duplicates at parse
    # time, but scenes built programmatically can bypass full re-validation.
    seen: set[str] = set()
    reported_dups: set[str] = set()
    for prim in scene.prims:
        if prim.id in seen and prim.id not in reported_dups:
            reported_dups.add(prim.id)
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="DUPLICATE_PRIM_ID",
                    message=f"duplicate prim id: {prim.id!r}",
                    prim_id=prim.id,
                )
            )
        seen.add(prim.id)

    for prim in scene.prims:
        if prim.type == "mesh_primitive" and prim.mesh_ref is None:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="MISSING_MESH_REF",
                    message=f"mesh_primitive prim {prim.id!r} has no mesh_ref",
                    prim_id=prim.id,
                )
            )

        if prim.parent_id is not None and prim.parent_id not in prim_ids:
            # Missing intermediate groups are common in partially authored
            # scenes; the hierarchy is organizational, not geometric.
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="UNKNOWN_PARENT",
                    message=(
                        f"prim {prim.id!r} references parent_id "
                        f"{prim.parent_id!r} which does not exist"
                    ),
                    prim_id=prim.id,
                )
            )

        rf = prim.rf
        if rf.material_id is not None and rf.material_id not in library_ids:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="UNKNOWN_RF_MATERIAL",
                    message=(
                        f"prim {prim.id!r} references RF material "
                        f"{rf.material_id!r} which is not in the project library"
                    ),
                    prim_id=prim.id,
                )
            )

        if prim.type == "mesh_primitive" and rf.material_id is None:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="MISSING_RF_MATERIAL",
                    message=f"prim {prim.id!r} has no RF material assigned",
                    prim_id=prim.id,
                )
            )

        mismatch = _mismatch_issue(prim, library)
        if mismatch is not None:
            issues.append(mismatch)

        material = library.get(rf.material_id) if rf.material_id else None
        if (
            material is not None
            and material.transmissive
            and rf.thickness_m is None
            and material.thickness_m is None
        ):
            issues.append(
                ValidationIssue(
                    severity="warning",
                    code="MISSING_THICKNESS",
                    message=(
                        f"prim {prim.id!r} uses transmissive RF material "
                        f"{material.id!r} but no thickness_m is set on the "
                        "prim or the material"
                    ),
                    prim_id=prim.id,
                )
            )

        if rf.assignment_status in _SUGGESTED_STATUSES:
            issues.append(
                ValidationIssue(
                    severity="info",
                    code="UNCONFIRMED_SUGGESTION",
                    message=(
                        f"prim {prim.id!r} RF material {rf.material_id!r} is "
                        f"only {rf.assignment_status} and awaits user "
                        "confirmation"
                    ),
                    prim_id=prim.id,
                )
            )

        if project_dir is not None and prim.mesh_ref is not None:
            asset = project_dir / prim.mesh_ref.asset_uri
            if not asset.is_file():
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="UNSUPPORTED_MESH_REF",
                        message=(
                            f"prim {prim.id!r} mesh_ref asset "
                            f"{prim.mesh_ref.asset_uri!r} does not exist in "
                            "the project folder"
                        ),
                        prim_id=prim.id,
                    )
                )

    # Material frequency-band guardrail: ITU-R P.2040 ground models are only
    # defined up to ~10 GHz. If the scene's primary simulation frequency is
    # above that, flag ITU ground materials so the user swaps to a constant
    # (e.g. ground_28ghz) — an accuracy footgun the RT engine won't catch.
    freq = scene.simulation_configs[0].frequency_hz if scene.simulation_configs else None
    if freq is not None and freq > 10e9:
        flagged: set[str] = set()
        for prim in scene.prims:
            mat = library.get(prim.rf.material_id) if prim.rf.material_id else None
            if (
                mat
                and mat.model == "itu_frequency_dependent"
                and mat.category == "ground"
                and mat.id not in flagged
            ):
                flagged.add(mat.id)
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        code="MATERIAL_OUT_OF_BAND",
                        message=(
                            f"material {mat.id!r} (ITU ground) is used at "
                            f"{freq / 1e9:.1f} GHz, beyond the ~10 GHz ITU-R "
                            "P.2040 validity range; use a constant material "
                            "such as 'ground_28ghz' for mmWave"
                        ),
                        prim_id=prim.id,
                    )
                )

    # Actors are compiled as their own RF shapes, so an unknown RF material is
    # as fatal for an actor as for a prim (reuse the same code, with the actor
    # id in the message so the UI can point at it).
    for actor in scene.actors:
        if actor.rf_material_id is not None and actor.rf_material_id not in library_ids:
            issues.append(
                ValidationIssue(
                    severity="error",
                    code="UNKNOWN_RF_MATERIAL",
                    message=(
                        f"actor {actor.id!r} references RF material "
                        f"{actor.rf_material_id!r} which is not in the project library"
                    ),
                )
            )

    has_tx = any(d.kind == "tx" for d in scene.devices)
    has_rx = any(d.kind == "rx" for d in scene.devices)
    if not (has_tx and has_rx):
        missing = [k for k, ok in (("tx", has_tx), ("rx", has_rx)) if not ok]
        issues.append(
            ValidationIssue(
                severity="info",
                code="NO_DEVICES",
                message=(
                    "scene has no "
                    + " and no ".join(missing)
                    + " device; simulations would produce no paths"
                ),
            )
        )

    return ValidationReport.from_issues(issues)
