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
from app.schemas.validation import Severity, ValidationIssue, ValidationReport

# Geometry-density thresholds (per HANDOFF geometry guardrails). Triangle
# counts above these slow ray tracing dramatically; the AABB extents bracket
# the plausible scale for a metric scene (a sub-0.5 m or >50 km scene is almost
# always a unit/import error rather than reality).
_PRIM_TRIANGLE_WARN = 500_000
_SCENE_TRIANGLE_WARN = 2_000_000
_SCALE_MIN_M = 0.5
_SCALE_MAX_M = 50_000.0

# Authoritative ITU material frequency-band table (single source of truth;
# both this validator and the Mitsuba importer read it, so it must not be
# duplicated/drifted elsewhere). ITU-R P.2040 defines the ground family
# (medium_dry / wet / very_dry ground) only over ~1-10 GHz; above that the
# frequency-dependent model is undefined and Sionna raises "Properties of ITU
# material '<name>' are not defined for this frequency". Each entry maps a
# material *category* whose model is itu_frequency_dependent to its upper band
# edge (Hz) and a constant-model, band-safe replacement id in the default
# library. Keyed by category so it covers every ground material at once.
_ITU_BAND_MAX_HZ: dict[str, float] = {
    "ground": 10e9,
}
_ITU_SAFE_ALTERNATIVE: dict[str, str] = {
    "ground": "ground_28ghz",
}


def itu_out_of_band(mat, frequency_hz: float) -> bool:
    """True if ``mat`` is an ITU frequency-dependent material used above its
    ITU-R P.2040 validity band at ``frequency_hz``. Constant-model materials
    (including the safe replacements) are always in band."""
    if mat is None or mat.model != "itu_frequency_dependent":
        return False
    band_max = _ITU_BAND_MAX_HZ.get(mat.category)
    return band_max is not None and frequency_hz > band_max


def itu_band_max_hz(category: str) -> Optional[float]:
    """Upper ITU-R P.2040 band edge (Hz) for a material category, or None."""
    return _ITU_BAND_MAX_HZ.get(category)


def itu_safe_alternative(category: str) -> Optional[str]:
    """Band-safe constant-model replacement material id for a category, or None."""
    return _ITU_SAFE_ALTERNATIVE.get(category)

# 1-3 concrete next steps per issue code. The validator attaches these so the
# frontend can render actionable buttons/hints without duplicating the table.
_SUGGESTED_ACTIONS: dict[str, list[str]] = {
    "DUPLICATE_PRIM_ID": [
        "Rename one of the prims so every id is unique",
    ],
    "MISSING_MESH_REF": [
        "Attach a mesh_ref pointing at the prim's geometry",
        "Or change the prim type to 'group' if it has no geometry",
    ],
    "UNKNOWN_PARENT": [
        "Create the missing parent group",
        "Or clear parent_id to leave the prim at the scene root",
    ],
    "UNKNOWN_RF_MATERIAL": [
        "Pick an existing material in the RF Materials tab",
        "Or add the missing material to the project library",
    ],
    "MISSING_RF_MATERIAL": [
        "Assign an RF material in the RF Materials tab",
        "Run rule-based or AI suggestion",
    ],
    "VISUAL_RF_MISMATCH": [
        "Confirm the RF material matches the real surface",
        "Or reassign to the visually indicated material",
    ],
    "MISSING_THICKNESS": [
        "Set thickness_m on the prim's RF binding",
        "Or set a default thickness_m on the material",
    ],
    "UNCONFIRMED_SUGGESTION": [
        "Review and confirm the suggested RF material",
        "Or reject it to leave the prim unassigned",
    ],
    "MATERIAL_OUT_OF_BAND": [
        "Swap to a constant material valid at this frequency (e.g. ground_28ghz)",
    ],
    "UNSUPPORTED_MESH_REF": [
        "Import the referenced visual asset into the project",
        "Or update the mesh_ref to an asset that exists",
    ],
    "NO_DEVICES": [
        "Add at least one tx and one rx device before simulating",
    ],
    "TOO_MANY_TRIANGLES": [
        "Decimate/simplify the geometry before RF compilation",
        "Or split the mesh into per-material RF proxies",
    ],
    "NON_MANIFOLD_OR_OPEN_MESH": [
        "Verify open shells are intended (normal for building facades)",
        "Close the mesh if transmission through it must be modeled",
    ],
    "SCALE_SUSPICIOUS": [
        "Check the import unit scale (meters expected, Z-up)",
        "Re-import or rescale the asset to metric extents",
    ],
}


def _issue(
    severity: Severity,
    code: str,
    message: str,
    *,
    prim_id: Optional[str] = None,
    device_id: Optional[str] = None,
) -> ValidationIssue:
    """Construct a ValidationIssue with the code's canonical suggested_actions."""
    return ValidationIssue(
        severity=severity,
        code=code,
        message=message,
        prim_id=prim_id,
        device_id=device_id,
        suggested_actions=list(_SUGGESTED_ACTIONS.get(code, [])),
    )

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
    return _issue(
        "warning",
        "VISUAL_RF_MISMATCH",
        (
            f"visual evidence for prim {prim.id!r} suggests category "
            f"'{evidence_category}', but assigned RF material "
            f"{material.id!r} has category '{assigned_category}'"
        ),
        prim_id=prim.id,
    )


def _geometry_issues(scene: Scene, project_dir: Optional[Path]) -> list[ValidationIssue]:
    """Best-effort geometry sanity checks against the loaded visual asset.

    Only runs when ``project_dir`` is given and the asset actually loads;
    everything here is defensive - a broken/missing asset yields no issues
    rather than an exception (validation must never raise on content).
    """
    if project_dir is None:
        return []

    # Import lazily so validation has no hard dependency on trimesh when no
    # project_dir is passed (the common in-memory validation path).
    try:
        from app.services import mesh_tools
    except Exception:  # pragma: no cover - trimesh import guard
        return []

    asset_uri = scene.assets.visual_scene_uri or "visual/scene.glb"
    try:
        tm_scene = mesh_tools.load_visual_scene(project_dir, asset_uri)
    except Exception:
        return []
    if tm_scene is None:
        return []

    issues: list[ValidationIssue] = []
    total_faces = 0
    counted_any = False

    for prim in scene.prims:
        if prim.mesh_ref is None:
            continue
        try:
            mesh = mesh_tools.extract_prim_mesh(tm_scene, prim.mesh_ref)
        except Exception:
            mesh = None
        if mesh is None:
            continue
        try:
            face_count = int(len(mesh.faces))
        except Exception:
            continue
        counted_any = True
        total_faces += face_count

        if face_count > _PRIM_TRIANGLE_WARN:
            issues.append(
                _issue(
                    "warning",
                    "TOO_MANY_TRIANGLES",
                    (
                        f"prim {prim.id!r} geometry has {face_count:,} triangles "
                        f"(> {_PRIM_TRIANGLE_WARN:,}); RF ray tracing will be slow "
                        "- consider decimating or using an RF proxy mesh"
                    ),
                    prim_id=prim.id,
                )
            )

        # Watertightness is informational: open shells are normal for building
        # facades, but a solid the user expects to transmit through behaves
        # differently when it is not closed.
        try:
            watertight = bool(mesh.is_watertight)
        except Exception:
            watertight = True
        if not watertight:
            issues.append(
                _issue(
                    "info",
                    "NON_MANIFOLD_OR_OPEN_MESH",
                    (
                        f"prim {prim.id!r} geometry is not watertight (open or "
                        "non-manifold); open shells are normal for building "
                        "facades, but transmission through them behaves "
                        "differently than through a closed solid"
                    ),
                    prim_id=prim.id,
                )
            )

    if counted_any and total_faces > _SCENE_TRIANGLE_WARN:
        issues.append(
            _issue(
                "warning",
                "TOO_MANY_TRIANGLES",
                (
                    f"scene geometry totals {total_faces:,} triangles "
                    f"(> {_SCENE_TRIANGLE_WARN:,}); RF compilation and ray "
                    "tracing may be very slow"
                ),
            )
        )

    # Scale sanity: measure the whole loaded asset's AABB (Z-up meters). A
    # sub-0.5 m or >50 km max extent is almost always a unit/import-scale error.
    try:
        extents = tm_scene.bounds  # (2, 3) min/max, or None when empty
    except Exception:
        extents = None
    if extents is not None:
        try:
            max_extent = float(
                max(extents[1][i] - extents[0][i] for i in range(3))
            )
        except Exception:
            max_extent = None  # type: ignore[assignment]
        if max_extent is not None and max_extent > 0.0 and (
            max_extent < _SCALE_MIN_M or max_extent > _SCALE_MAX_M
        ):
            issues.append(
                _issue(
                    "warning",
                    "SCALE_SUSPICIOUS",
                    (
                        f"loaded geometry spans {max_extent:.3g} m on its largest "
                        f"axis, outside the plausible {_SCALE_MIN_M}-{_SCALE_MAX_M:g} m "
                        "range; check the import unit scale (meters, Z-up)"
                    ),
                )
            )

    return issues


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
                _issue(
                    "error",
                    "DUPLICATE_PRIM_ID",
                    f"duplicate prim id: {prim.id!r}",
                    prim_id=prim.id,
                )
            )
        seen.add(prim.id)

    for prim in scene.prims:
        if prim.type == "mesh_primitive" and prim.mesh_ref is None:
            issues.append(
                _issue(
                    "error",
                    "MISSING_MESH_REF",
                    f"mesh_primitive prim {prim.id!r} has no mesh_ref",
                    prim_id=prim.id,
                )
            )

        if prim.parent_id is not None and prim.parent_id not in prim_ids:
            # Missing intermediate groups are common in partially authored
            # scenes; the hierarchy is organizational, not geometric.
            issues.append(
                _issue(
                    "warning",
                    "UNKNOWN_PARENT",
                    (
                        f"prim {prim.id!r} references parent_id "
                        f"{prim.parent_id!r} which does not exist"
                    ),
                    prim_id=prim.id,
                )
            )

        rf = prim.rf
        if rf.material_id is not None and rf.material_id not in library_ids:
            issues.append(
                _issue(
                    "error",
                    "UNKNOWN_RF_MATERIAL",
                    (
                        f"prim {prim.id!r} references RF material "
                        f"{rf.material_id!r} which is not in the project library"
                    ),
                    prim_id=prim.id,
                )
            )

        # A mesh_primitive with no material AND a non-rejected status is a real
        # gap; a "rejected" prim deliberately has no material, so stay silent.
        if (
            prim.type == "mesh_primitive"
            and rf.material_id is None
            and rf.assignment_status != "rejected"
        ):
            issues.append(
                _issue(
                    "warning",
                    "MISSING_RF_MATERIAL",
                    f"prim {prim.id!r} has no RF material assigned",
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
                _issue(
                    "warning",
                    "MISSING_THICKNESS",
                    (
                        f"prim {prim.id!r} uses transmissive RF material "
                        f"{material.id!r} but no thickness_m is set on the "
                        "prim or the material"
                    ),
                    prim_id=prim.id,
                )
            )

        if rf.assignment_status in _SUGGESTED_STATUSES:
            issues.append(
                _issue(
                    "info",
                    "UNCONFIRMED_SUGGESTION",
                    (
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
                    _issue(
                        "warning",
                        "UNSUPPORTED_MESH_REF",
                        (
                            f"prim {prim.id!r} mesh_ref asset "
                            f"{prim.mesh_ref.asset_uri!r} does not exist in "
                            "the project folder"
                        ),
                        prim_id=prim.id,
                    )
                )

    # Material frequency-band guardrail: ITU-R P.2040 ground models are only
    # defined up to ~10 GHz (see _ITU_BAND_MAX_HZ). If the scene's primary
    # simulation frequency is above that, flag ITU ground materials so the user
    # swaps to a constant (e.g. ground_28ghz) — an accuracy footgun the RT
    # engine won't catch until the solve fails.
    freq = scene.simulation_configs[0].frequency_hz if scene.simulation_configs else None
    if freq is not None:
        flagged: set[str] = set()
        for prim in scene.prims:
            mat = library.get(prim.rf.material_id) if prim.rf.material_id else None
            if mat and mat.id not in flagged and itu_out_of_band(mat, freq):
                flagged.add(mat.id)
                band_max = itu_band_max_hz(mat.category)
                safe = itu_safe_alternative(mat.category)
                fix = (
                    f"use a constant material such as {safe!r} for mmWave"
                    if safe
                    else "use a constant-model material valid at this frequency"
                )
                issues.append(
                    _issue(
                        "warning",
                        "MATERIAL_OUT_OF_BAND",
                        (
                            f"material {mat.id!r} (ITU {mat.category}) is used at "
                            f"{freq / 1e9:.1f} GHz, beyond the ~{band_max / 1e9:.0f} GHz "
                            f"ITU-R P.2040 validity range; {fix}"
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
                _issue(
                    "error",
                    "UNKNOWN_RF_MATERIAL",
                    (
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
            _issue(
                "info",
                "NO_DEVICES",
                (
                    "scene has no "
                    + " and no ".join(missing)
                    + " device; simulations would produce no paths"
                ),
            )
        )

    issues.extend(_geometry_issues(scene, project_dir))

    return ValidationReport.from_issues(issues)
