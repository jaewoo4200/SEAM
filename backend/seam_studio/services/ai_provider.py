"""AI material-suggestion providers (HANDOFF.md section 9).

Provider chain: ollama_text (optional local LLM) -> rule_based (always
available) -> disabled (only when SIONNATWIN_AI_ENABLED=off). Visual/PBR
information is passed to providers strictly as *evidence* via
``build_evidence`` and never treated as RF ground truth; every suggestion
requires explicit user confirmation before it can mutate the scene.

Optional dependencies (httpx network calls) are used lazily inside functions
and every failure degrades to the rule-based provider - the app must work
with no Ollama server, no GPU, no network.
"""

import abc
import base64
import io
import json
import re
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from pydantic import ValidationError

from seam_studio.core.config import get_settings
from seam_studio.schemas.ai import (
    AIModelInfo,
    AIModelsResponse,
    AIProviderStatus,
    AssignmentRule,
    EvidenceImage,
    MaterialAlternative,
    MaterialSuggestion,
    MaterialSuggestionResponse,
    ProviderModels,
    SuggestMaterialsRequest,
)
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.scene import Prim, Scene
from seam_studio.schemas.validation import ValidationIssue

PROMPT_VERSION = "v2"

FALLBACK_MATERIAL_ID = "unknown_rf"
FALLBACK_CONFIDENCE = 0.2
ALTERNATIVE_CONFIDENCE_FACTOR = 0.5
# Floor for the category-score normalizer so a degenerate all-zero score object
# cannot divide by zero when deriving confidence.
_CATEGORY_SCORE_EPS = 1e-6

# Keyword table (HANDOFF 9.3). Order matters: earlier rules win confidence
# ties (e.g. "brick_wall" recommends itu_brick with itu_concrete alternative).
_KEYWORD_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("window", "glass", "pane"), "itu_glass"),
    (("brick",), "itu_brick"),
    (("wall", "concrete", "cement"), "itu_concrete"),
    (("metal", "steel", "aluminum", "frame"), "metal"),
    (("wood", "timber", "tree", "trunk", "bark"), "itu_wood"),
    (("leaf", "foliage", "canopy", "grass", "vegetation"), "vegetation_custom"),
    (("road", "asphalt", "street"), "asphalt_custom"),
    (("terrain", "soil", "ground"), "ground"),
)


class AIParseError(ValueError):
    """Raised when an AI response payload is completely unusable."""


class NoTextProviderError(RuntimeError):
    """Raised when a text-LLM feature is requested but no provider is available.

    The API layer maps this to HTTP 409 (the rule-generation and
    validation-explanation features genuinely require an LLM; unlike material
    suggestions there is no keyword fallback that makes sense).
    """


def _strip_data_url_prefix(data_url: str | None) -> str | None:
    """Return the bare base64 body of a ``data:...;base64,<b64>`` URL.

    Ollama's ``images`` field wants raw base64 without the ``data:`` prefix.
    Non-data-URL strings (already-bare base64) pass through unchanged.
    """
    if not data_url:
        return None
    marker = "base64,"
    idx = data_url.find(marker)
    if data_url.startswith("data:") and idx != -1:
        return data_url[idx + len(marker):]
    return data_url


def _effective_images(
    screenshots: list[str] | None, screenshot: str | None
) -> list[str]:
    """Normalize the two screenshot inputs into one ordered list.

    ``screenshots`` (the multi-view list) wins when present; otherwise the
    single ``screenshot`` becomes a one-item list. Falsy entries are dropped so
    an empty string never masquerades as an attached image.
    """
    if screenshots:
        return [img for img in screenshots if img]
    return [screenshot] if screenshot else []


def _is_vision_rejection(exc: Exception) -> bool:
    """True when ``exc`` looks like the server refusing image input.

    Treats an HTTP 4xx (typically 400) from the chat-completions call, or an
    error message mentioning images/vision/multimodal, as a signal to retry
    text-only. Kept lenient so a text-only local model that simply errors on
    the multimodal payload still degrades gracefully instead of dropping all
    the way to the rule-based provider.
    """
    try:
        import httpx  # lazy: optional runtime dependency path

        if isinstance(exc, httpx.HTTPStatusError):
            status = exc.response.status_code
            if 400 <= status < 500:
                return True
    except Exception:  # pragma: no cover - httpx import guard
        pass
    text = (str(exc) or exc.__class__.__name__).lower()
    return any(
        token in text
        for token in ("image", "vision", "multimodal", "not support", "unsupported")
    )


def build_evidence(
    prim: Prim, library: RFMaterialLibrary, scene: Optional[Scene] = None
) -> dict:
    """Collect the ONLY visual information any provider is allowed to see.

    Evidence, never truth: names, tags and texture basenames are hints for
    suggestion providers; the RF binding is untouched. ``library`` is part of
    the pinned signature for providers that want to cross-check evidence
    against material categories; the rule/LLM evidence itself never includes
    RF state.

    ``mesh_name`` (from ``prim.mesh_ref.mesh_name``) is scanned like the other
    textual hints. ``neighbor_context`` (up to 4 sibling prim names sharing the
    same parent path segment) is informational only - it is shown to the LLM as
    surrounding context but never keyword-scanned, so a neighbor named "glass"
    cannot silently flip this prim to glass.
    """
    visual = prim.visual
    texture = visual.base_color_texture if visual else None
    texture_basename = (
        PurePosixPath(texture.replace("\\", "/")).name if texture else None
    )
    evidence = {
        "prim_id": prim.id,
        "name": prim.name,
        "mesh_name": prim.mesh_ref.mesh_name if prim.mesh_ref else None,
        "semantic_tags": list(prim.semantic_tags),
        "visual_material_id": visual.material_id if visual else None,
        "visual_material_name": visual.material_name if visual else None,
        "texture_basename": texture_basename,
        "neighbor_context": _neighbor_context(prim, scene) if scene else [],
    }
    # Geo-context seam (extension point, informational only): present only
    # when the scene is georeferenced (OSM imports set origin_lat_lon_alt).
    # Local-first: nothing here or downstream fetches imagery over the
    # network - a future evidence source can attach a LOCAL photo taken at
    # these coordinates as one more image.
    if scene is not None:
        origin = scene.coordinate_system.origin_lat_lon_alt
        if origin is not None:
            evidence["geo_context"] = {"origin_lat_lon_alt": list(origin)}
    return evidence


def _neighbor_context(prim: Prim, scene: Scene, limit: int = 4) -> list[str]:
    """Up to ``limit`` sibling prim names sharing the parent path segment.

    Siblings are other prims whose id has the same parent directory as this
    prim (e.g. everything under "/buildings/b07/"). Informational only: this
    list is never keyword-scanned, just surfaced to the LLM as context.
    """
    parent = prim.id.rsplit("/", 1)[0]
    names: list[str] = []
    for other in scene.prims:
        if other.id == prim.id:
            continue
        if other.id.rsplit("/", 1)[0] != parent:
            continue
        names.append(other.name)
        if len(names) >= limit:
            break
    return names


def _evidence_fields(evidence: dict) -> list[tuple[str, str, float]]:
    """(human label, text to scan, confidence) in decreasing priority."""
    fields: list[tuple[str, str, float]] = [
        ("prim name", str(evidence.get("name") or ""), 0.9),
        (
            "visual material name",
            str(
                evidence.get("visual_material_name")
                or evidence.get("visual_material_id")
                or ""
            ),
            0.8,
        ),
        ("mesh name", str(evidence.get("mesh_name") or ""), 0.7),
    ]
    for tag in evidence.get("semantic_tags") or []:
        fields.append(("semantic tag", str(tag), 0.65))
    fields.append(("texture filename", str(evidence.get("texture_basename") or ""), 0.6))
    return fields


# Qualcomm-style per-category prompt variants: 2-3 descriptive phrasings that
# help an LLM recognize each category from visual/name evidence. Categories not
# listed here fall back to a single generic phrasing built from the name.
_CATEGORY_VARIANT_PHRASES: dict[str, tuple[str, ...]] = {
    "glass": ("a glazed window pane", "a transparent glass facade"),
    "concrete": ("a poured concrete wall", "a bare cement surface"),
    "brick": ("a red brick wall", "a masonry brick facade"),
    "metal": ("a bare metal panel", "a brushed steel or aluminum surface"),
    "wood": ("a wooden plank or timber beam", "a natural tree trunk / bark"),
    "vegetation": ("dense green foliage or leaves", "a grassy or canopy surface"),
    "road": ("a dark asphalt road surface", "a paved street"),
    "ground": ("bare soil or terrain", "a dirt / earth ground surface"),
}


def _category_variant_lines(library: RFMaterialLibrary) -> str:
    """Prompt block listing each library category with descriptive variants.

    One line per distinct ``RFMaterial.category`` present in the library, in
    first-seen order. Known categories use the curated phrasings above; unknown
    ones get a single generic phrasing so the model still has something to
    reason over.
    """
    lines: list[str] = []
    seen: set[str] = set()
    for mat in library.materials:
        category = mat.category
        if category in seen:
            continue
        seen.add(category)
        variants = _CATEGORY_VARIANT_PHRASES.get(category)
        if variants:
            phrases = "; ".join(f'"{v}"' for v in variants)
        else:
            phrases = f'"a {category.replace("_", " ")} surface"'
        lines.append(f"- {category}: {phrases}")
    return "\n".join(lines)


def _texture_image_for_geometry(geometry) -> object | None:
    """Return the baseColor PIL image of a trimesh geometry, or None.

    Handles both PBR materials (``material.baseColorTexture``) and simple
    textured materials (``material.image`` / ``visual.image``). Anything without
    a usable image (untextured meshes, vertex colors) yields None.
    """
    visual = getattr(geometry, "visual", None)
    if visual is None:
        return None
    material = getattr(visual, "material", None)
    if material is not None:
        image = getattr(material, "baseColorTexture", None)
        if image is not None:
            return image
        image = getattr(material, "image", None)
        if image is not None:
            return image
    return getattr(visual, "image", None)


def extract_prim_texture_crops(
    project_dir: Path,
    scene: Scene,
    prim_ids: list[str],
    max_crops: int = 6,
    size: int = 256,
) -> list[dict]:
    """Extract per-prim texture crops for the VLM, best evidence first.

    Image source preference per prim:
    1. The ORIGINAL texture file persisted at import time
       (``prim.visual.base_color_texture``, project-relative under
       ``visual/textures/``) - full-resolution evidence.
    2. The baseColor image embedded in the visual GLB (resolved through
       ``mesh_ref.mesh_name``) - viewer-sized fallback.

    The image is then cropped to the prim's UV bounding box when its UVs use
    only a sub-region of the texture (drape-projected bundles map terrain to
    an inner window of an aerial ortho; unwrapped meshes often pack into a
    corner) - without this the VLM sees the whole atlas instead of the prim's
    own surface. Finally downscaled to ``size`` px (longest side) and encoded
    as a JPEG data URL. Prims without any texture are skipped silently.
    Returns at most ``max_crops`` entries, in prim order, shaped
    ``[{"prim_id", "data_url"}]``. Best-effort: any failure (missing GLB,
    trimesh/PIL error) degrades to text/screenshots only.
    """
    if max_crops <= 0 or not prim_ids:
        return []
    try:
        from PIL import Image  # lazy: optional runtime dependency path

        from seam_studio.services import mesh_tools
    except Exception:  # pragma: no cover - import guard
        return []

    # Group target prims by the visual asset they reference so each GLB is
    # loaded once. Most scenes share a single asset_uri.
    crops: list[dict] = []
    scene_cache: dict[str, object] = {}
    project_root = project_dir.resolve()
    for prim_id in prim_ids:
        if len(crops) >= max_crops:
            break
        prim = scene.prim_by_id(prim_id)
        if prim is None or prim.mesh_ref is None:
            continue
        asset_uri = prim.mesh_ref.asset_uri
        if asset_uri not in scene_cache:
            try:
                scene_cache[asset_uri] = mesh_tools.load_visual_scene(
                    project_dir, asset_uri
                )
            except Exception:
                scene_cache[asset_uri] = None
        tm_scene = scene_cache[asset_uri]
        geometry = None
        if tm_scene is not None:
            try:
                geometry = _resolve_prim_geometry(tm_scene, prim.mesh_ref.mesh_name)
            except Exception:
                geometry = None

        # Image source: original persisted file first, GLB baseColor second.
        image = None
        tex_rel = prim.visual.base_color_texture if prim.visual else None
        if tex_rel:
            try:
                tex_path = (project_root / tex_rel).resolve()
                if tex_path.is_file() and tex_path.is_relative_to(project_root):
                    image = Image.open(tex_path)
                    image.load()
            except Exception:
                image = None
        if image is None and geometry is not None:
            try:
                image = _texture_image_for_geometry(geometry)
            except Exception:
                image = None
        if image is None:
            continue
        try:
            image = _crop_to_uv_bbox(geometry, image)
            data_url = _encode_crop(Image, image, size)
        except Exception:
            # One bad prim must not sink the whole batch.
            continue
        if data_url is not None:
            crops.append({"prim_id": prim_id, "data_url": data_url})
    return crops


def _crop_to_uv_bbox(geometry, image):
    """Crop a texture image to the prim's used UV region, when meaningful.

    UVs are clamped to [0, 1] (tiling wraps around anyway) and the crop only
    happens when the used region is a real sub-window (< 90% of either axis) -
    a fully-unwrapped [0,1]^2 mesh keeps the whole image. V is flipped because
    UV origin is bottom-left while PIL's is top-left. Any failure returns the
    image unchanged (evidence quality upgrade, never a gate).
    """
    try:
        uv = getattr(getattr(geometry, "visual", None), "uv", None)
        if uv is None or len(uv) == 0:
            return image
        u = [min(1.0, max(0.0, float(p[0]))) for p in uv]
        v = [min(1.0, max(0.0, float(p[1]))) for p in uv]
        u0, u1, v0, v1 = min(u), max(u), min(v), max(v)
        if (u1 - u0) > 0.9 and (v1 - v0) > 0.9:
            return image  # uses (nearly) the whole texture: nothing to crop
        w, h = image.size
        left = int(u0 * w)
        right = min(w, max(left + 8, int(u1 * w)))
        top = int((1.0 - v1) * h)
        bottom = min(h, max(top + 8, int((1.0 - v0) * h)))
        if right - left < 4 or bottom - top < 4:
            # Degenerate UVs (e.g. all-zero) collapse the window to nothing;
            # the whole image is better evidence than an empty crop.
            return image
        return image.crop((left, top, right, bottom))
    except Exception:
        return image


def _resolve_prim_geometry(tm_scene, mesh_name: str):
    """Resolve the trimesh geometry named ``mesh_name`` (geometry or node name).

    Mirrors the resolution order in ``mesh_tools.extract_prim_mesh`` but returns
    the geometry object itself (untransformed) so its visual/material survives -
    ``extract_prim_mesh`` copies and world-transforms, which is unnecessary here
    and can drop the texture visual.
    """
    if mesh_name in tm_scene.geometry:
        return tm_scene.geometry.get(mesh_name)
    for node in sorted(tm_scene.graph.nodes_geometry):
        if node == mesh_name:
            _, geometry_name = tm_scene.graph[node]
            return tm_scene.geometry.get(geometry_name)
    return None


def _encode_crop(image_module, image, size: int) -> Optional[str]:
    """Downscale a PIL image to ``size`` px and return a JPEG data URL."""
    img = image
    if getattr(img, "mode", None) not in ("RGB", "L"):
        img = img.convert("RGB")
    img = img.copy()
    img.thumbnail((size, size), image_module.Resampling.LANCZOS)
    buffer = io.BytesIO()
    img.save(buffer, format="JPEG", quality=80)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


class MaterialSuggestionProvider(abc.ABC):
    """Provider abstraction (HANDOFF 9.2)."""

    name: str = "abstract"
    # True for providers that can consume image evidence (viewport screenshots
    # and texture crops). False providers ignore all image inputs.
    multimodal: bool = False

    @abc.abstractmethod
    def is_available(self) -> bool: ...

    @abc.abstractmethod
    def suggest(
        self,
        scene: Scene,
        library: RFMaterialLibrary,
        prim_ids: list[str],
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        texture_crops: list[dict] | None = None,
        model: str | None = None,
    ) -> MaterialSuggestionResponse: ...


class RuleBasedProvider(MaterialSuggestionProvider):
    """Keyword rules over visual evidence. Always available, never networked.

    Ignores images: rules operate on textual evidence only.
    """

    name = "rule_based"

    def is_available(self) -> bool:
        return True

    def suggest(
        self,
        scene: Scene,
        library: RFMaterialLibrary,
        prim_ids: list[str],
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        texture_crops: list[dict] | None = None,
        model: str | None = None,
    ) -> MaterialSuggestionResponse:
        # ``model`` is accepted for signature parity but ignored: rules have no
        # model.
        suggestions: list[MaterialSuggestion] = []
        warnings: list[str] = []
        for prim_id in prim_ids:
            prim = scene.prim_by_id(prim_id)
            if prim is None:
                warnings.append(f"prim not found in scene: {prim_id}")
                continue
            suggestion = self._suggest_for_prim(prim, library, warnings)
            if suggestion is not None:
                suggestions.append(suggestion)
        return MaterialSuggestionResponse(
            suggestions=suggestions,
            provider=self.name,
            model=None,
            prompt_version=PROMPT_VERSION,
            warnings=warnings,
        )

    def _suggest_for_prim(
        self, prim: Prim, library: RFMaterialLibrary, warnings: list[str]
    ) -> Optional[MaterialSuggestion]:
        evidence = build_evidence(prim, library)
        # material_id -> {"confidence": best score, "evidence": readable hits}
        hits: dict[str, dict] = {}
        for label, text, confidence in _evidence_fields(evidence):
            lowered = text.lower()
            if not lowered:
                continue
            for keywords, material_id in _KEYWORD_RULES:
                for keyword in keywords:
                    if keyword in lowered:
                        entry = hits.setdefault(
                            material_id, {"confidence": 0.0, "evidence": []}
                        )
                        entry["confidence"] = max(entry["confidence"], confidence)
                        entry["evidence"].append(f"{label} contains '{keyword}'")
                        break  # one evidence line per rule per field

        library_ids = library.ids()
        for material_id in [m for m in hits if m not in library_ids]:
            warnings.append(
                f"{prim.id}: rule matched material '{material_id}' "
                "not present in library; skipped"
            )
            del hits[material_id]

        if not hits:
            if FALLBACK_MATERIAL_ID not in library_ids:
                warnings.append(
                    f"{prim.id}: no keyword evidence and "
                    f"'{FALLBACK_MATERIAL_ID}' missing from library; skipped"
                )
                return None
            return MaterialSuggestion(
                prim_id=prim.id,
                recommended_rf_material_id=FALLBACK_MATERIAL_ID,
                confidence=FALLBACK_CONFIDENCE,
                evidence=["no keyword evidence"],
                needs_user_confirmation=True,
            )

        # Stable sort: insertion order (field priority, then table order)
        # breaks confidence ties.
        ranked = sorted(hits.items(), key=lambda kv: -kv[1]["confidence"])
        top_id, top = ranked[0]
        alternatives = [
            MaterialAlternative(
                rf_material_id=material_id,
                confidence=round(
                    entry["confidence"] * ALTERNATIVE_CONFIDENCE_FACTOR, 4
                ),
            )
            for material_id, entry in ranked[1:]
        ]
        return MaterialSuggestion(
            prim_id=prim.id,
            recommended_rf_material_id=top_id,
            confidence=top["confidence"],
            evidence=top["evidence"],
            alternatives=alternatives,
            needs_user_confirmation=True,
        )


# base_url -> (monotonic timestamp, reachable, human detail)
_PROBE_TTL_S = 30.0
_probe_cache: dict[str, tuple[float, bool, str]] = {}


def _probe_ollama(base_url: str) -> tuple[bool, str]:
    now = time.monotonic()
    cached = _probe_cache.get(base_url)
    if cached is not None and now - cached[0] < _PROBE_TTL_S:
        return cached[1], cached[2]
    try:
        import httpx  # lazy: optional runtime dependency path

        response = httpx.get(f"{base_url}/api/tags", timeout=1.5)
        response.raise_for_status()
        ok, detail = True, f"{base_url}: reachable"
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        ok, detail = False, f"{base_url}: not reachable ({reason})"
    _probe_cache[base_url] = (now, ok, detail)
    return ok, detail


def _probe_openai(base_url: str) -> tuple[bool, str]:
    """Reachability probe for an OpenAI-compatible server (LM Studio).

    GET {base_url}/models with a short timeout, cached like the Ollama probe.
    The cache is shared but keyed by url, so the OpenAI base url never collides
    with the Ollama one.
    """
    now = time.monotonic()
    cached = _probe_cache.get(base_url)
    if cached is not None and now - cached[0] < _PROBE_TTL_S:
        return cached[1], cached[2]
    try:
        import httpx  # lazy: optional runtime dependency path

        response = httpx.get(f"{base_url}/models", timeout=1.5)
        response.raise_for_status()
        ok, detail = True, f"{base_url}: reachable"
    except Exception as exc:
        reason = str(exc) or exc.__class__.__name__
        ok, detail = False, f"{base_url}: not reachable ({reason})"
    _probe_cache[base_url] = (now, ok, detail)
    return ok, detail


# base_url -> (monotonic timestamp, discovered model ids). Cached like the
# probe cache and honoring the same TTL so the model picker does not hammer the
# local server. Keyed by url so the OpenAI and Ollama lists never collide.
_model_cache: dict[str, tuple[float, list[str]]] = {}


def list_openai_models(base_url: str) -> list[str]:
    """Model ids served by an OpenAI-compatible server (LM Studio), or [].

    GET {base_url}/models and reads ``payload["data"][*]["id"]``, dropping
    embedding models (ids containing "embed") which cannot answer chat. Short
    timeout and TTL-cached like the reachability probes; any failure (offline,
    bad JSON, unexpected shape) degrades to an empty list.
    """
    now = time.monotonic()
    cached = _model_cache.get(base_url)
    if cached is not None and now - cached[0] < _PROBE_TTL_S:
        return cached[1]
    try:
        import httpx  # lazy: optional runtime dependency path

        response = httpx.get(f"{base_url}/models", timeout=1.5)
        response.raise_for_status()
        payload = response.json()
        data = payload.get("data") if isinstance(payload, dict) else None
        models = [
            str(item["id"])
            for item in (data or [])
            if isinstance(item, dict) and item.get("id")
            and "embed" not in str(item["id"]).lower()
        ]
    except Exception:
        models = []
    _model_cache[base_url] = (now, models)
    return models


def list_ollama_models(base_url: str) -> list[str]:
    """Model names served by an Ollama server, or [].

    GET {base_url}/api/tags and reads ``models[*].name``. Short timeout and
    TTL-cached like :func:`list_openai_models`; any failure degrades to [].
    """
    now = time.monotonic()
    cached = _model_cache.get(base_url)
    if cached is not None and now - cached[0] < _PROBE_TTL_S:
        return cached[1]
    try:
        import httpx  # lazy: optional runtime dependency path

        response = httpx.get(f"{base_url}/api/tags", timeout=1.5)
        response.raise_for_status()
        payload = response.json()
        models_raw = payload.get("models") if isinstance(payload, dict) else None
        models = [
            str(item["name"])
            for item in (models_raw or [])
            if isinstance(item, dict) and item.get("name")
        ]
    except Exception:
        models = []
    _model_cache[base_url] = (now, models)
    return models


class OllamaTextProvider(MaterialSuggestionProvider):
    """Local text LLM via an Ollama-compatible /api/chat endpoint.

    Any failure (connect, timeout, bad JSON, schema mismatch) falls back
    internally to the rule-based provider; the response's ``provider`` field
    always names the provider that actually answered.
    """

    name = "ollama_text"
    multimodal = True

    def is_available(self) -> bool:
        settings = get_settings().ai
        if settings.enabled == "off":
            return False
        ok, _ = _probe_ollama(settings.base_url)
        return ok

    def suggest(
        self,
        scene: Scene,
        library: RFMaterialLibrary,
        prim_ids: list[str],
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        texture_crops: list[dict] | None = None,
        model: str | None = None,
    ) -> MaterialSuggestionResponse:
        settings = get_settings().ai
        warnings: list[str] = []
        evidence_list: list[dict] = []
        for prim_id in prim_ids:
            prim = scene.prim_by_id(prim_id)
            if prim is None:
                warnings.append(f"prim not found in scene: {prim_id}")
                continue
            evidence_list.append(build_evidence(prim, library))
        if not evidence_list:
            return MaterialSuggestionResponse(
                suggestions=[],
                provider=self.name,
                model=model or settings.text_model,
                prompt_version=PROMPT_VERSION,
                warnings=warnings,
            )
        # Any image (viewport screenshots and/or per-prim texture crops)
        # upgrades this call to the vision model; the ollama chat API takes
        # images as base64 (no data: prefix) on the user message. Viewport
        # screenshots come first, then texture crops in prim order.
        view_images = _effective_images(screenshots, screenshot)
        crops = texture_crops or []
        image_urls = view_images + [c["data_url"] for c in crops]
        has_image = bool(image_urls)
        images_b64 = [
            b64 for img in image_urls if (b64 := _strip_data_url_prefix(img))
        ]
        # Default model tracks whether an image is attached (vision vs text);
        # an explicit override wins over both, subject to the discovery
        # guardrail below.
        default_model = settings.vision_model if has_image else settings.text_model
        effective_model = model or default_model
        if model is not None:
            available = list_ollama_models(settings.base_url)
            if available and model not in available:
                effective_model = default_model
                warnings.append(
                    f"requested model '{model}' is not loaded; "
                    f"used default '{default_model}'"
                )
        model = effective_model
        if has_image and settings.vision_model != settings.text_model:
            # Honesty over silence: attaching an image swaps to the vision
            # model, which may be smaller/weaker than the configured text one.
            warnings.append(
                f"screenshot attached: using vision model '{settings.vision_model}' "
                f"instead of text model '{settings.text_model}'"
            )
        try:
            import httpx  # lazy: never required at import time

            messages = self._build_messages(
                evidence_list,
                library,
                num_views=len(view_images),
                crop_prim_ids=[c["prim_id"] for c in crops],
            )
            if has_image and images_b64:
                # Ollama attaches images to the user message via an 'images' list.
                messages[-1] = {**messages[-1], "images": images_b64}
            response = httpx.post(
                f"{settings.base_url}/api/chat",
                json={
                    "model": model,
                    "messages": messages,
                    "stream": False,
                    "format": "json",
                    "options": {"temperature": 0},
                },
                # Vision ceiling for image-carrying calls (see LocalOpenAI).
                timeout=settings.vision_timeout_s if has_image else settings.timeout_s,
            )
            response.raise_for_status()
            payload = response.json()
            raw_text = ""
            if isinstance(payload, dict):
                raw_text = (payload.get("message") or {}).get("content") or ""
            suggestions, parse_warnings = parse_ai_response(raw_text, scene, library)
            return MaterialSuggestionResponse(
                suggestions=suggestions,
                provider=self.name,
                model=model,
                prompt_version=PROMPT_VERSION,
                warnings=warnings + parse_warnings,
            )
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            # Parity with local_openai: if the model rejected the IMAGE payload
            # (text-only model behind a vision-model name), retry without it
            # before dropping all the way to rules.
            if has_image:
                try:
                    return self.suggest(scene, library, prim_ids, model=model)
                except Exception:  # noqa: BLE001 - fall through to rules below
                    pass
            fallback = RuleBasedProvider().suggest(scene, library, prim_ids)
            fallback.warnings = [
                f"ollama_text failed: {reason}; fell back to rule_based",
                *fallback.warnings,
            ]
            return fallback

    @staticmethod
    def _build_messages(
        evidence_list: list[dict],
        library: RFMaterialLibrary,
        num_views: int = 0,
        crop_prim_ids: list[str] | None = None,
    ) -> list[dict]:
        """Build the v2 (category-score) chat prompt.

        ``num_views`` is how many viewport screenshots are attached (0 = none);
        ``crop_prim_ids`` names the prims whose texture crops are appended after
        the screenshots, in image order. Both feed prompt lines that map image
        order to meaning; the actual image bytes are attached by the caller.
        """
        crop_prim_ids = crop_prim_ids or []
        library_lines = "\n".join(
            f"- {mat.id} (category: {mat.category}){': ' + mat.notes if mat.notes else ''}"
            for mat in library.materials
        )
        category_lines = _category_variant_lines(library)
        schema_example = {
            "suggestions": [
                {
                    "prim_id": "/buildings/b07/window_12",
                    "recommended_rf_material_id": "itu_glass",
                    "category_scores": {"glass": 0.82, "concrete": 0.12, "metal": 0.06},
                    "evidence": [
                        "prim name contains 'window'",
                        "visual material name contains 'glass'",
                    ],
                    "alternatives": [
                        {"rf_material_id": "metal", "confidence": 0.11}
                    ],
                    "needs_user_confirmation": True,
                }
            ]
        }
        system = (
            "You assign RF (radio-frequency) materials to 3D scene objects for "
            "wireless ray-tracing simulation. The visual evidence you receive "
            "(names, tags, texture filenames, images) is a hint, never ground "
            "truth. For each object, think of every material CATEGORY through "
            "its descriptive variants below and score how well the object "
            "matches each category, then pick the best-fitting rf material id. "
            "Respond ONLY with JSON."
        )
        # Image-mapping prompt lines: viewport screenshots first, then crops.
        image_note = ""
        image_index = 1
        if num_views:
            if num_views == 1:
                image_note += (
                    "An image of the current 3D viewport is attached. It is "
                    "EVIDENCE only (visual appearance), never RF ground truth; "
                    "weigh it exactly like the textual hints below.\n\n"
                )
            else:
                image_note += (
                    f"The first {num_views} attached images are different "
                    "camera angles of the SAME scene. They are EVIDENCE only "
                    "(visual appearance), never RF ground truth; weigh them "
                    "exactly like the textual hints below.\n\n"
                )
            image_index += num_views
        if crop_prim_ids:
            crop_lines = "\n".join(
                f"- image {image_index + offset}: texture crop of prim {pid}"
                for offset, pid in enumerate(crop_prim_ids)
            )
            image_note += (
                "The following attached images are close-up surface texture "
                "crops taken from each prim's OWN texture map - the strongest "
                "visual evidence available (still EVIDENCE, never RF ground "
                "truth). When a crop conflicts with name-based hints, weigh "
                "the crop higher:\n"
                f"{crop_lines}\n\n"
            )
        user = (
            f"{image_note}"
            "Allowed rf material ids (use ONLY these for "
            "recommended_rf_material_id and alternatives):\n"
            f"{library_lines}\n\n"
            "Material categories and descriptive variants to reason over:\n"
            f"{category_lines}\n\n"
            "Objects to classify, one suggestion per object "
            "(visual evidence only):\n"
            f"{json.dumps(evidence_list, indent=2)}\n\n"
            "Respond ONLY with JSON exactly matching this schema example "
            "(no prose, no markdown):\n"
            f"{json.dumps(schema_example, indent=2)}\n\n"
            "Rules: category_scores maps each plausible category to a number in "
            "[0, 1] (higher = better match); recommended_rf_material_id must be "
            "an allowed id whose category is your top-scoring category; evidence "
            "entries are short human-readable strings citing the input evidence; "
            "needs_user_confirmation must be true."
        )
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]


class LocalOpenAIProvider(MaterialSuggestionProvider):
    """Local SOTA LLM via LM Studio's OpenAI-compatible server.

    Targets a reasoning model (e.g. google/gemma-4-31b) served at
    ``settings.openai_url``/chat/completions. Reasoning models place their
    chain-of-thought in ``message.reasoning_content`` and the actual answer in
    ``message.content``; we read ``content`` (falling back to
    ``reasoning_content`` only if content is empty) and let
    :func:`parse_ai_response` strip any leftover preamble by extracting the
    first ``{...}`` block. ``max_tokens`` is generous so the reasoning budget
    does not starve the answer, and temperature is 0 for determinism.

    Any failure (connect, timeout, bad JSON, schema mismatch) falls back
    internally to the rule-based provider, exactly like the Ollama provider.
    """

    name = "local_openai"
    multimodal = True

    def is_available(self) -> bool:
        settings = get_settings().ai
        if settings.enabled == "off":
            return False
        ok, _ = _probe_openai(settings.openai_url)
        return ok

    def suggest(
        self,
        scene: Scene,
        library: RFMaterialLibrary,
        prim_ids: list[str],
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        texture_crops: list[dict] | None = None,
        model: str | None = None,
    ) -> MaterialSuggestionResponse:
        settings = get_settings().ai
        warnings: list[str] = []
        # Effective model = explicit override or the settings default. Guardrail:
        # when discovery lists models and the requested one is not among them,
        # fall back to the default and warn (only when the list is non-empty, so
        # an unreachable/empty discovery never blocks a valid request).
        effective_model = model or settings.openai_model
        if model is not None:
            available = list_openai_models(settings.openai_url)
            if available and model not in available:
                effective_model = settings.openai_model
                warnings.append(
                    f"requested model '{model}' is not loaded; "
                    f"used default '{settings.openai_model}'"
                )
        evidence_list: list[dict] = []
        for prim_id in prim_ids:
            prim = scene.prim_by_id(prim_id)
            if prim is None:
                warnings.append(f"prim not found in scene: {prim_id}")
                continue
            evidence_list.append(build_evidence(prim, library))
        if not evidence_list:
            return MaterialSuggestionResponse(
                suggestions=[],
                provider=self.name,
                model=effective_model,
                prompt_version=PROMPT_VERSION,
                warnings=warnings,
            )
        # Viewport screenshots first, then per-prim texture crops, in one
        # ordered image list; the prompt maps image order to meaning.
        view_images = _effective_images(screenshots, screenshot)
        crops = texture_crops or []
        image_urls = view_images + [c["data_url"] for c in crops]
        num_views = len(view_images)
        crop_prim_ids = [c["prim_id"] for c in crops]
        try:
            import httpx  # lazy: never required at import time

            has_image = bool(image_urls)
            try:
                raw_text = self._call(
                    evidence_list,
                    library,
                    settings,
                    image_urls if has_image else [],
                    num_views=num_views if has_image else 0,
                    crop_prim_ids=crop_prim_ids if has_image else [],
                    model=effective_model,
                )
            except Exception as img_exc:
                # Graceful degradation: some servers reject image input for a
                # text-only model (typically HTTP 400). Retry WITHOUT the images
                # and record the downgrade rather than failing to rules.
                if has_image and _is_vision_rejection(img_exc):
                    warnings.append(
                        f"vision input rejected by {effective_model}; "
                        "used text only"
                    )
                    raw_text = self._call(
                        evidence_list, library, settings, [], model=effective_model
                    )
                else:
                    raise
            suggestions, parse_warnings = parse_ai_response(raw_text, scene, library)
            return MaterialSuggestionResponse(
                suggestions=suggestions,
                provider=self.name,
                model=effective_model,
                prompt_version=PROMPT_VERSION,
                warnings=warnings + parse_warnings,
            )
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            fallback = RuleBasedProvider().suggest(scene, library, prim_ids)
            fallback.warnings = [
                f"local_openai failed: {reason}; fell back to rule_based",
                *fallback.warnings,
            ]
            return fallback

    @staticmethod
    def _call(
        evidence_list: list[dict],
        library: RFMaterialLibrary,
        settings,
        image_urls: list[str],
        num_views: int = 0,
        crop_prim_ids: list[str] | None = None,
        model: str | None = None,
    ) -> str:
        """One chat-completions round-trip; returns the raw answer text.

        ``model`` is the effective model for the request body; None keeps the
        settings default. Raises on transport/HTTP errors so the caller can
        distinguish a vision-rejection (retry without image) from other
        failures (fall back to rules)."""
        import httpx  # lazy: never required at import time

        messages = OllamaTextProvider._build_messages(
            evidence_list,
            library,
            num_views=num_views if image_urls else 0,
            crop_prim_ids=crop_prim_ids if image_urls else [],
        )
        if image_urls:
            # OpenAI multimodal content: the text prompt plus one image_url
            # part per image (viewport screenshots then texture crops).
            text_part = {"type": "text", "text": messages[-1]["content"]}
            image_parts = [
                {"type": "image_url", "image_url": {"url": url}}
                for url in image_urls
            ]
            messages[-1] = {**messages[-1], "content": [text_part, *image_parts]}
        response = httpx.post(
            f"{settings.openai_url}/chat/completions",
            json={
                "model": model or settings.openai_model,
                "messages": messages,
                "temperature": 0,
                # Reasoning models spend tokens on chain-of-thought before
                # the answer; keep the budget generous so JSON is not cut.
                "max_tokens": 2000,
                "stream": False,
            },
            # Image-carrying calls get the vision ceiling: a local 20-30B VLM
            # needs model load + multi-image prefill, which routinely blows the
            # text timeout (live-verified: 4 crops on gemma-4-31b > 60 s).
            timeout=settings.vision_timeout_s if image_urls else settings.timeout_s,
        )
        response.raise_for_status()
        return LocalOpenAIProvider._extract_content(response.json())

    @staticmethod
    def _extract_content(payload: object) -> str:
        """Pull the answer text out of an OpenAI chat-completions payload.

        Prefers ``message.content``; falls back to ``message.reasoning_content``
        only when content is empty (some reasoning servers stream the whole
        answer, JSON included, into reasoning_content)."""
        if not isinstance(payload, dict):
            return ""
        choices = payload.get("choices")
        if not isinstance(choices, list) or not choices:
            return ""
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if not isinstance(message, dict):
            return ""
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            return content
        reasoning = message.get("reasoning_content")
        if isinstance(reasoning, str):
            return reasoning
        return content if isinstance(content, str) else ""


class DisabledProvider(MaterialSuggestionProvider):
    """Explicit manual-only mode (SIONNATWIN_AI_ENABLED=off)."""

    name = "disabled"

    def is_available(self) -> bool:
        return get_settings().ai.enabled == "off"

    def suggest(
        self,
        scene: Scene,
        library: RFMaterialLibrary,
        prim_ids: list[str],
        screenshot: str | None = None,
        screenshots: list[str] | None = None,
        texture_crops: list[dict] | None = None,
        model: str | None = None,
    ) -> MaterialSuggestionResponse:
        return MaterialSuggestionResponse(
            suggestions=[],
            provider=self.name,
            model=None,
            prompt_version=PROMPT_VERSION,
            warnings=["AI assistance disabled"],
        )


_FENCED_BLOCK_RE = re.compile(r"^```(?:json)?\s*\n(.*?)\n?```\s*$", re.DOTALL)


def _extract_json_object(text: str) -> Optional[str]:
    """Return the first brace-balanced ``{...}`` block in ``text``, or None.

    Reasoning models sometimes emit a chain-of-thought preamble before the JSON
    answer. This scans for the first ``{``, then walks forward tracking brace
    depth (ignoring braces inside double-quoted strings, honoring backslash
    escapes) until the matching ``}``.
    """
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _clamp_confidence(value: object) -> object:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return min(1.0, max(0.0, float(value)))


def _derive_from_category_scores(
    scores: object,
) -> Optional[tuple[float, str]]:
    """(confidence, evidence line) from a valid ``category_scores`` object.

    Returns None when ``scores`` is not a usable mapping of category ->
    non-negative number, so the caller can fall back to the model's
    self-reported confidence. Confidence is the normalized margin
    ``top_score / max(sum(scores), eps)`` (Qualcomm-style category-score
    aggregation, adapted to an LLM), clamped to [0, 1].
    """
    if not isinstance(scores, dict) or not scores:
        return None
    clean: dict[str, float] = {}
    for category, value in scores.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None  # malformed -> fall back to self-reported confidence
        numeric = float(value)
        if numeric < 0:
            return None
        clean[str(category)] = numeric
    if not clean:
        return None
    total = sum(clean.values())
    top_category, top_score = max(clean.items(), key=lambda kv: kv[1])
    confidence = min(1.0, max(0.0, top_score / max(total, _CATEGORY_SCORE_EPS)))
    ranked = sorted(clean.items(), key=lambda kv: -kv[1])
    detail = ", ".join(f"{cat} {score:g}" for cat, score in ranked)
    return confidence, f"category scores: {detail}"


def parse_ai_response(
    raw_text: str, scene: Scene, library: RFMaterialLibrary
) -> tuple[list[MaterialSuggestion], list[str]]:
    """Parse and strictly validate an AI JSON payload. Pure - no I/O.

    Malformed items, unknown prims and unknown materials are dropped with
    warnings; a completely unusable payload raises :class:`AIParseError`
    (callers convert that to a rule-based fallback).
    """
    text = (raw_text or "").strip()
    fenced = _FENCED_BLOCK_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        # Reasoning models may wrap the answer in a chain-of-thought preamble:
        # fall back to the first brace-balanced {...} block before giving up.
        block = _extract_json_object(text)
        if block is not None:
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                raise AIParseError(f"AI response is not valid JSON: {exc}") from exc
        else:
            raise AIParseError(f"AI response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("suggestions"), list):
        raise AIParseError("AI response is missing a top-level 'suggestions' list")

    library_ids = library.ids()
    allowed_keys = set(MaterialSuggestion.model_fields)
    suggestions: list[MaterialSuggestion] = []
    warnings: list[str] = []
    for index, item in enumerate(payload["suggestions"]):
        if not isinstance(item, dict):
            warnings.append(f"dropped suggestion #{index}: not a JSON object")
            continue
        data = {k: v for k, v in item.items() if k in allowed_keys}
        # v2 (Qualcomm category-score aggregation): when the model returns a
        # valid category_scores object, DERIVE confidence from it and record the
        # scores as evidence. Malformed scores fall back to the self-reported
        # confidence path unchanged.
        derived = _derive_from_category_scores(item.get("category_scores"))
        if derived is not None:
            confidence, evidence_line = derived
            data["confidence"] = confidence
            existing_evidence = data.get("evidence")
            evidence_lines = (
                list(existing_evidence)
                if isinstance(existing_evidence, list)
                else []
            )
            data["evidence"] = [evidence_line, *evidence_lines]
        if "confidence" in data:
            data["confidence"] = _clamp_confidence(data["confidence"])
        if isinstance(data.get("alternatives"), list):
            for alt in data["alternatives"]:
                if isinstance(alt, dict) and "confidence" in alt:
                    alt["confidence"] = _clamp_confidence(alt["confidence"])
        try:
            suggestion = MaterialSuggestion.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            warnings.append(
                f"dropped malformed suggestion #{index}: "
                f"{first.get('loc')}: {first.get('msg')}"
            )
            continue
        if scene.prim_by_id(suggestion.prim_id) is None:
            warnings.append(f"dropped suggestion for unknown prim: {suggestion.prim_id}")
            continue
        if suggestion.recommended_rf_material_id not in library_ids:
            warnings.append(
                f"dropped suggestion for {suggestion.prim_id}: unknown material "
                f"'{suggestion.recommended_rf_material_id}'"
            )
            continue
        kept_alternatives = [
            alt for alt in suggestion.alternatives if alt.rf_material_id in library_ids
        ]
        if len(kept_alternatives) != len(suggestion.alternatives):
            dropped = [
                alt.rf_material_id
                for alt in suggestion.alternatives
                if alt.rf_material_id not in library_ids
            ]
            warnings.append(
                f"dropped unknown alternative materials for "
                f"{suggestion.prim_id}: {dropped}"
            )
            suggestion.alternatives = kept_alternatives
        # MVP never auto-applies; confirmation is mandatory regardless of
        # what the model claims (HANDOFF 9.5).
        suggestion.needs_user_confirmation = True
        suggestions.append(suggestion)
    return suggestions, warnings


def get_provider_statuses() -> list[AIProviderStatus]:
    """Availability of every provider, in AUTO-SELECTION order. Never raises.

    The list order mirrors _select_provider's chain (disabled -> local_openai
    -> ollama_text -> rule_based), so the FIRST available entry is the
    provider a non-forced AI action would actually use. The header status
    chip and the AI panel's provider list both rely on that; rule_based
    always-available at the front would make the chip lie whenever a local
    LLM is up (QA follow-up).
    """
    statuses: list[AIProviderStatus] = []
    try:
        settings = get_settings().ai
        off = settings.enabled == "off"
        statuses.append(
            AIProviderStatus(
                name="disabled",
                available=off,
                model=None,
                detail=(
                    "AI assistance disabled (SEAM_AI_ENABLED=off; legacy alias: "
                    "SIONNATWIN_AI_ENABLED)"
                    if off
                    else "inactive (AI assistance is enabled)"
                ),
            )
        )
        if off:
            statuses.append(
                AIProviderStatus(
                    name="local_openai",
                    available=False,
                    model=settings.openai_model,
                    detail=(
                        f"{settings.openai_url} ({settings.openai_model}): "
                        "disabled (SEAM_AI_ENABLED=off; legacy alias: "
                        "SIONNATWIN_AI_ENABLED)"
                    ),
                )
            )
            statuses.append(
                AIProviderStatus(
                    name="ollama_text",
                    available=False,
                    model=settings.text_model,
                    detail=f"{settings.base_url}: disabled (SEAM_AI_ENABLED=off; "
                    "legacy alias: SIONNATWIN_AI_ENABLED)",
                )
            )
        else:
            ok_oai, detail_oai = _probe_openai(settings.openai_url)
            statuses.append(
                AIProviderStatus(
                    name="local_openai",
                    available=ok_oai,
                    model=settings.openai_model,
                    detail=f"{detail_oai} (model {settings.openai_model})",
                    # Discovered model ids (empty when the server is unreachable).
                    available_models=(
                        list_openai_models(settings.openai_url) if ok_oai else []
                    ),
                )
            )
            ok, detail = _probe_ollama(settings.base_url)
            statuses.append(
                AIProviderStatus(
                    name="ollama_text",
                    available=ok,
                    model=settings.text_model,
                    detail=detail,
                    available_models=(
                        list_ollama_models(settings.base_url) if ok else []
                    ),
                )
            )
    except Exception as exc:  # settings/probe must never break /health
        statuses.append(
            AIProviderStatus(
                name="ollama_text",
                available=False,
                model=None,
                detail=f"status probe failed: {exc}",
            )
        )
    statuses.append(
        AIProviderStatus(
            name="rule_based",
            available=True,
            model=None,
            detail="keyword rules; always available",
        )
    )
    return statuses


def _provider_models_entry(
    provider: str,
    available: bool,
    default_model: Optional[str],
    discovered: list[str],
    detail: str,
) -> ProviderModels:
    """Build one ProviderModels, marking the default model in the list.

    The default model is always offered even when discovery did not surface it
    (an unreachable server, or a server that simply does not list the configured
    default), so the picker never shows an empty list for a configured provider.
    ``is_default`` marks the settings default; ``label`` is the bare model id.
    """
    ids: list[str] = list(discovered)
    if default_model and default_model not in ids:
        ids.insert(0, default_model)
    models = [
        AIModelInfo(id=mid, label=mid, is_default=(mid == default_model))
        for mid in ids
    ]
    return ProviderModels(
        provider=provider,
        available=available,
        models=models,
        default_model=default_model,
        detail=detail,
    )


def get_provider_models() -> AIModelsResponse:
    """Selectable models per provider for the model picker. Never raises.

    Covers the two model-bearing providers (local_openai + ollama_text);
    rule_based/disabled are omitted since they have no model. ``available``
    mirrors the provider probe state and the model list is discovered from the
    server (empty/default-only when unreachable or AI is off).
    """
    settings = get_settings().ai
    off = settings.enabled == "off"
    if off:
        return AIModelsResponse(
            providers=[
                _provider_models_entry(
                    "local_openai",
                    available=False,
                    default_model=settings.openai_model,
                    discovered=[],
                    detail="disabled (SEAM_AI_ENABLED=off; legacy alias: SIONNATWIN_AI_ENABLED)",
                ),
                _provider_models_entry(
                    "ollama_text",
                    available=False,
                    default_model=settings.text_model,
                    discovered=[],
                    detail="disabled (SEAM_AI_ENABLED=off; legacy alias: SIONNATWIN_AI_ENABLED)",
                ),
            ]
        )
    ok_oai, detail_oai = _probe_openai(settings.openai_url)
    ok_ollama, detail_ollama = _probe_ollama(settings.base_url)
    return AIModelsResponse(
        providers=[
            _provider_models_entry(
                "local_openai",
                available=ok_oai,
                default_model=settings.openai_model,
                discovered=list_openai_models(settings.openai_url) if ok_oai else [],
                detail=detail_oai,
            ),
            _provider_models_entry(
                "ollama_text",
                available=ok_ollama,
                default_model=settings.text_model,
                discovered=list_ollama_models(settings.base_url) if ok_ollama else [],
                detail=detail_ollama,
            ),
        ]
    )


_PROVIDER_CLASSES: dict[str, type[MaterialSuggestionProvider]] = {
    RuleBasedProvider.name: RuleBasedProvider,
    OllamaTextProvider.name: OllamaTextProvider,
    LocalOpenAIProvider.name: LocalOpenAIProvider,
    DisabledProvider.name: DisabledProvider,
}


def resolve_target_prim_ids(scene: Scene, request: SuggestMaterialsRequest) -> list[str]:
    """Explicit prim ids, else every mesh prim without an RF material."""
    if request.prim_ids:
        return list(request.prim_ids)
    return [
        prim.id
        for prim in scene.prims
        if prim.type == "mesh_primitive" and prim.rf.material_id is None
    ]


def _select_provider(request: SuggestMaterialsRequest) -> MaterialSuggestionProvider:
    if request.provider is not None:
        provider_cls = _PROVIDER_CLASSES.get(request.provider)
        if provider_cls is None:
            raise ValueError(
                f"unknown AI provider: {request.provider!r} "
                f"(expected one of {sorted(_PROVIDER_CLASSES)})"
            )
        return provider_cls()
    # DisabledProvider is only available when SIONNATWIN_AI_ENABLED=off, in
    # which case it must win; otherwise prefer the SOTA local LLM (LM Studio),
    # then the Ollama text model, then keyword rules.
    for provider in (
        DisabledProvider(),
        LocalOpenAIProvider(),
        OllamaTextProvider(),
        RuleBasedProvider(),
    ):
        if provider.is_available():
            return provider
    return RuleBasedProvider()


def suggest_materials(
    scene: Scene,
    library: RFMaterialLibrary,
    request: SuggestMaterialsRequest,
    project_dir: Optional[Path] = None,
) -> MaterialSuggestionResponse:
    """Entry point used by the /ai/suggest-materials endpoint.

    ``project_dir`` (the resolved project directory) is required only to extract
    texture crops; when None the crop feature is off. Raises ValueError for an
    unknown forced provider (API maps it to 400).
    """
    provider = _select_provider(request)
    targets = resolve_target_prim_ids(scene, request)
    if not targets:
        return MaterialSuggestionResponse(
            suggestions=[],
            provider=provider.name,
            model=None,
            prompt_version=PROMPT_VERSION,
            warnings=["no target prims: every mesh prim already has an RF material"],
        )
    # Multi-view screenshots: the plural list wins; the legacy single field is
    # treated as a one-item list for back-compat.
    screenshots = _effective_images(
        request.screenshot_data_urls, request.screenshot_data_url
    )
    # Texture crops only apply to a multimodal-capable provider and require a
    # project_dir; otherwise the feature stays off (back-compat default).
    texture_crops: list[dict] = []
    if request.attach_texture_crops and provider.multimodal and project_dir is not None:
        texture_crops = extract_prim_texture_crops(project_dir, scene, targets)
    response = provider.suggest(
        scene,
        library,
        targets,
        screenshots=screenshots or None,
        texture_crops=texture_crops or None,
        model=request.model,
    )
    if response.prompt_version is None:
        response.prompt_version = PROMPT_VERSION
    # Research provenance: persist the crops the provider actually saw. Gated
    # on the RESPONDING provider being the multimodal one that was selected -
    # after an internal fallback (e.g. VLM timeout -> rule_based) the crops
    # were never consumed, and claiming them as evidence would be dishonest.
    if (
        texture_crops
        and project_dir is not None
        and response.provider == provider.name
    ):
        response.evidence_images = _persist_evidence_crops(
            project_dir, texture_crops
        )
    return response


def _persist_evidence_crops(
    project_dir: Path, crops: list[dict]
) -> Optional[list[EvidenceImage]]:
    """Write attached crops under ai/evidence/<batch>/ and return their refs.

    Batch dirs are timestamped (UTC, second resolution) with a short random
    suffix so re-runs within the same second cannot collide. Best-effort: any
    write failure returns None rather than failing the suggestion batch.
    """
    import secrets
    from datetime import datetime, timezone

    try:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        batch = f"{stamp}-{secrets.token_hex(2)}"
        out_dir = project_dir / "ai" / "evidence" / batch
        out_dir.mkdir(parents=True, exist_ok=True)
        refs: list[EvidenceImage] = []
        for crop in crops:
            data_url = crop.get("data_url") or ""
            payload = _strip_data_url_prefix(data_url)
            if not payload:
                continue
            leaf = re.sub(r"[^a-z0-9_\-]+", "_", str(crop["prim_id"]).lower()).strip("_")
            path = out_dir / f"{leaf or 'prim'}.jpg"
            path.write_bytes(base64.b64decode(payload))
            refs.append(
                EvidenceImage(
                    prim_id=crop["prim_id"],
                    asset_path=f"ai/evidence/{batch}/{path.name}",
                )
            )
        return refs or None
    except Exception:  # pragma: no cover - best-effort persistence
        return None


# --------------------------------------------------------------- text LLM tasks
# Rule generation and validation-explanation are TEXT-only LLM tasks (no image
# path, no rule fallback). They reuse the provider-selection conventions of the
# suggestion path: prefer the local SOTA reasoning model (LM Studio / OpenAI-
# compatible) then the Ollama text model. When neither is reachable they raise
# NoTextProviderError, which the API maps to HTTP 409.


def _select_text_provider(provider: Optional[str] = None) -> tuple[str, str]:
    """(provider_name, model) for the best available TEXT LLM.

    Mirrors ``_select_provider`` ordering (local_openai then ollama_text) but
    excludes the rule-based/disabled providers, which cannot answer free-text
    prompts. ``provider`` forces a specific text provider when it names a
    text-capable one; any other value (e.g. "rule_based") is ignored and the
    auto-selection order applies. Raises :class:`NoTextProviderError` when AI is
    off or no server is reachable.
    """
    settings = get_settings().ai
    if settings.enabled == "off":
        raise NoTextProviderError(
            "AI assistance is disabled (SEAM_AI_ENABLED=off; legacy alias: "
            "SIONNATWIN_AI_ENABLED)"
        )
    if provider == "local_openai":
        if LocalOpenAIProvider().is_available():
            return "local_openai", settings.openai_model
        raise NoTextProviderError(
            f"local_openai is not reachable (at {settings.openai_url})"
        )
    if provider == "ollama_text":
        if OllamaTextProvider().is_available():
            return "ollama_text", settings.text_model
        raise NoTextProviderError(
            f"ollama_text is not reachable (at {settings.base_url})"
        )
    if LocalOpenAIProvider().is_available():
        return "local_openai", settings.openai_model
    if OllamaTextProvider().is_available():
        return "ollama_text", settings.text_model
    raise NoTextProviderError(
        "no text LLM provider is reachable "
        f"(tried local_openai at {settings.openai_url} and "
        f"ollama_text at {settings.base_url})"
    )


def _call_text_llm(
    provider_name: str,
    model: str,
    system: str,
    user: str,
    force_json: bool = False,
) -> str:
    """One text-only round-trip to ``provider_name``; returns the raw answer.

    Raises on transport/HTTP errors so the caller can decide how to degrade.
    ``force_json`` asks the server for a JSON object (honored by Ollama's
    ``format`` field; OpenAI-compatible servers get a response_format hint).
    """
    import httpx  # lazy: never required at import time

    settings = get_settings().ai
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    if provider_name == "local_openai":
        body: dict = {
            "model": model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": 2000,
            "stream": False,
        }
        if force_json:
            body["response_format"] = {"type": "json_object"}
        response = httpx.post(
            f"{settings.openai_url}/chat/completions",
            json=body,
            timeout=settings.timeout_s,
        )
        if response.status_code == 400 and force_json:
            # Some OpenAI-compatible servers (LM Studio with certain models)
            # reject response_format outright; the parser tolerates prose
            # wrappers, so retry once without the hint.
            body.pop("response_format", None)
            response = httpx.post(
                f"{settings.openai_url}/chat/completions",
                json=body,
                timeout=settings.timeout_s,
            )
        response.raise_for_status()
        return LocalOpenAIProvider._extract_content(response.json())
    # ollama_text
    body = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": 0},
    }
    if force_json:
        body["format"] = "json"
    response = httpx.post(
        f"{settings.base_url}/api/chat",
        json=body,
        timeout=settings.timeout_s,
    )
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        return (payload.get("message") or {}).get("content") or ""
    return ""


# Few-shot examples in the SEAM spec style: a natural-language instruction and
# the rule JSON it should produce. These pin the output shape and demonstrate
# the case-insensitive, multi-term matching contract.
_RULE_FEW_SHOTS: tuple[dict, ...] = (
    {
        "instruction": "window 들어간 object는 glass로",
        "rules": [
            {
                "id": "rule_window_glass",
                "match_name_contains": ["window", "glass"],
                "rf_material_id": "itu_glass",
                "note": "window/glass named objects -> glass",
            }
        ],
    },
    {
        "instruction": "wall이나 concrete는 콘크리트 재질로",
        "rules": [
            {
                "id": "rule_wall_concrete",
                "match_name_contains": ["wall", "concrete", "cement"],
                "rf_material_id": "itu_concrete",
                "note": "walls -> concrete",
            }
        ],
    },
)


def _build_rule_generation_messages(
    instruction: str, library: RFMaterialLibrary
) -> tuple[str, str]:
    """(system, user) prompt for assignment-rule generation.

    The user prompt lists every allowed rf material id (anti-hallucination) and
    two SEAM-style few-shot examples, then the actual instruction.
    """
    library_lines = "\n".join(
        f"- {mat.id} (category: {mat.category})" for mat in library.materials
    )
    few_shots = "\n\n".join(
        "Instruction: "
        + json.dumps(example["instruction"], ensure_ascii=False)
        + "\nRules JSON:\n"
        + json.dumps({"rules": example["rules"]}, ensure_ascii=False, indent=2)
        for example in _RULE_FEW_SHOTS
    )
    schema_example = {
        "rules": [
            {
                "id": "rule_window_glass",
                "match_name_contains": ["window", "glass"],
                "rf_material_id": "itu_glass",
                "note": "short human note (optional)",
            }
        ]
    }
    system = (
        "You convert a natural-language material-assignment instruction into a "
        "list of deterministic name-matching rules for a wireless ray-tracing "
        "scene. Each rule matches 3D objects whose name/mesh/tag CONTAINS any "
        "of match_name_contains (case-insensitive) and assigns them one RF "
        "material. Use ONLY the allowed rf material ids. Respond ONLY with JSON."
    )
    user = (
        "Allowed rf material ids (use ONLY these for rf_material_id):\n"
        f"{library_lines}\n\n"
        "Examples:\n"
        f"{few_shots}\n\n"
        "Respond ONLY with JSON exactly matching this schema example "
        "(no prose, no markdown):\n"
        f"{json.dumps(schema_example, indent=2)}\n\n"
        "Rules: id is a short lowercase slug (letters, digits, _ or -); "
        "match_name_contains has at least one lowercase search term; "
        "rf_material_id must be one of the allowed ids above.\n\n"
        f"Instruction: {json.dumps(instruction, ensure_ascii=False)}\n"
        "Rules JSON:"
    )
    return system, user


def parse_rules_response(
    raw_text: str, library: RFMaterialLibrary
) -> tuple[list[AssignmentRule], list[str]]:
    """Parse + validate an AI rule payload. Pure - no I/O.

    Tolerates code fences and reasoning preambles exactly like
    :func:`parse_ai_response`. Malformed rules are dropped with a warning; a
    rule whose ``rf_material_id`` is not in the library is dropped and warned
    (same anti-hallucination stance as suggestions). A completely unusable
    payload raises :class:`AIParseError`.
    """
    text = (raw_text or "").strip()
    fenced = _FENCED_BLOCK_RE.match(text)
    if fenced:
        text = fenced.group(1).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        block = _extract_json_object(text)
        if block is not None:
            try:
                payload = json.loads(block)
            except json.JSONDecodeError:
                raise AIParseError(f"AI response is not valid JSON: {exc}") from exc
        else:
            raise AIParseError(f"AI response is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("rules"), list):
        raise AIParseError("AI response is missing a top-level 'rules' list")

    library_ids = library.ids()
    allowed_keys = set(AssignmentRule.model_fields)
    rules: list[AssignmentRule] = []
    warnings: list[str] = []
    for index, item in enumerate(payload["rules"]):
        if not isinstance(item, dict):
            warnings.append(f"dropped rule #{index}: not a JSON object")
            continue
        data = {k: v for k, v in item.items() if k in allowed_keys}
        try:
            rule = AssignmentRule.model_validate(data)
        except ValidationError as exc:
            first = exc.errors()[0]
            warnings.append(
                f"dropped malformed rule #{index}: "
                f"{first.get('loc')}: {first.get('msg')}"
            )
            continue
        if rule.rf_material_id not in library_ids:
            warnings.append(
                f"dropped rule {rule.id!r}: unknown material "
                f"'{rule.rf_material_id}'"
            )
            continue
        rules.append(rule)
    return rules, warnings


def _resolve_text_model(
    provider_name: str, model: Optional[str]
) -> tuple[str, list[str]]:
    """(effective model, warnings) for a text-LLM provider given an override.

    ``model`` None keeps the provider's settings default. When an explicit model
    is requested, it is validated against the provider's discovery list with the
    same guardrail as the suggestion path: a non-empty list that does not
    contain the requested model falls back to the default and warns; an
    empty/unreachable list lets the request through unchanged.
    """
    settings = get_settings().ai
    default_model = (
        settings.openai_model if provider_name == "local_openai" else settings.text_model
    )
    if model is None:
        return default_model, []
    if provider_name == "local_openai":
        available = list_openai_models(settings.openai_url)
    else:
        available = list_ollama_models(settings.base_url)
    if available and model not in available:
        return default_model, [
            f"requested model '{model}' is not loaded; used default '{default_model}'"
        ]
    return model, []


def generate_assignment_rules(
    instruction: str,
    library: RFMaterialLibrary,
    provider: Optional[str] = None,
    model: Optional[str] = None,
) -> tuple[list[AssignmentRule], str, Optional[str], list[str]]:
    """(rules, provider, model, warnings) from a natural-language instruction.

    Prompts the best available text LLM with the library id list and SEAM-style
    few-shot examples, parses the JSON with the same fence/brace tolerance as
    :func:`parse_ai_response`, and validates every ``rf_material_id`` against
    the library (unknown ids dropped + warned). ``provider`` forces a specific
    text provider ("local_openai"/"ollama_text"); ``model`` overrides the model
    on the selected provider (subject to the discovery guardrail). Raises
    :class:`NoTextProviderError` when no LLM is reachable (API maps to 409) and
    :class:`AIParseError` when the LLM answer is unusable.
    """
    provider_name, _ = _select_text_provider(provider)
    effective_model, model_warnings = _resolve_text_model(provider_name, model)
    system, user = _build_rule_generation_messages(instruction, library)
    raw_text = _call_text_llm(
        provider_name, effective_model, system, user, force_json=True
    )
    rules, warnings = parse_rules_response(raw_text, library)
    return rules, provider_name, effective_model, model_warnings + warnings


def _rule_matches_prim(rule: AssignmentRule, prim: Prim) -> Optional[str]:
    """The first ``match_name_contains`` term that hits this prim, or None.

    Case-insensitively scans the prim's name, mesh_name, semantic tags and
    visual material name/id. ``neighbor_context`` is deliberately NOT scanned
    (it is informational only).
    """
    haystacks: list[str] = [prim.name]
    if prim.mesh_ref is not None:
        haystacks.append(prim.mesh_ref.mesh_name)
    haystacks.extend(prim.semantic_tags)
    if prim.visual is not None:
        if prim.visual.material_name:
            haystacks.append(prim.visual.material_name)
        if prim.visual.material_id:
            haystacks.append(prim.visual.material_id)
    lowered = [h.lower() for h in haystacks if h]
    for term in rule.match_name_contains:
        needle = term.lower()
        if any(needle in h for h in lowered):
            return term
    return None


# Prims already trusted at this level are never overridden by a generated rule.
_RULE_PROTECTED_STATUSES = ("user_confirmed", "measurement_calibrated")


def apply_rules(
    scene: Scene, library: RFMaterialLibrary, rules: list[AssignmentRule]
) -> MaterialSuggestionResponse:
    """Turn assignment rules into material SUGGESTIONS (never auto-applied).

    For every mesh prim that is not already user_confirmed or
    measurement_calibrated, the first matching rule (in rule order) wins and
    produces a MaterialSuggestion with confidence 0.7, an evidence line naming
    the rule + matched term, and needs_user_confirmation=True. Rules whose
    ``rf_material_id`` is not in the library are skipped with a warning. The
    response provider is ``"rule_generated"``.
    """
    library_ids = library.ids()
    warnings: list[str] = []
    valid_rules: list[AssignmentRule] = []
    for rule in rules:
        if rule.rf_material_id not in library_ids:
            warnings.append(
                f"rule {rule.id!r} skipped: unknown material "
                f"'{rule.rf_material_id}'"
            )
            continue
        valid_rules.append(rule)

    suggestions: list[MaterialSuggestion] = []
    for prim in scene.prims:
        if prim.type != "mesh_primitive":
            continue
        if prim.rf.assignment_status in _RULE_PROTECTED_STATUSES:
            continue
        for rule in valid_rules:
            term = _rule_matches_prim(rule, prim)
            if term is None:
                continue
            suggestions.append(
                MaterialSuggestion(
                    prim_id=prim.id,
                    recommended_rf_material_id=rule.rf_material_id,
                    confidence=0.7,
                    evidence=[f"rule {rule.id}: name contains '{term}'"],
                    needs_user_confirmation=True,
                )
            )
            break  # first matching rule wins
    return MaterialSuggestionResponse(
        suggestions=suggestions,
        provider="rule_generated",
        model=None,
        prompt_version=PROMPT_VERSION,
        warnings=warnings,
    )


def _build_explain_messages(
    issues: list[ValidationIssue], library: RFMaterialLibrary
) -> tuple[str, str]:
    """(system, user) prompt for the validation-explanation task."""
    issue_lines = "\n".join(
        f"- [{issue.severity}] {issue.code}"
        + (f" (prim {issue.prim_id})" if issue.prim_id else "")
        + (f" (device {issue.device_id})" if issue.device_id else "")
        + f": {issue.message}"
        for issue in issues
    )
    if not issue_lines:
        issue_lines = "- (no issues reported)"
    system = (
        "You are an RF-simulation engineer's assistant. Given a scene "
        "validation report, explain in plain English what is causing the "
        "issues and give concrete, actionable fixes. Be concise and specific; "
        "no markdown headers, no JSON - just a short readable explanation."
    )
    user = (
        "Scene validation issues (code, severity, affected object, message):\n"
        f"{issue_lines}\n\n"
        "Explain the likely causes and the concrete steps to fix each class of "
        "issue. Keep it brief and engineer-facing."
    )
    return system, user


def explain_validation_warnings(
    issues: list[ValidationIssue], library: RFMaterialLibrary
) -> tuple[str, str, Optional[str], list[str]]:
    """(explanation, provider, model, warnings) for a validation report.

    Feeds the validation issues to the best available text LLM and returns a
    concise plain-text explanation of causes and fixes. Raises
    :class:`NoTextProviderError` when no LLM is reachable (API maps to 409).
    """
    provider_name, model = _select_text_provider()
    system, user = _build_explain_messages(issues, library)
    explanation = _call_text_llm(provider_name, model, system, user)
    return explanation.strip(), provider_name, model, []
