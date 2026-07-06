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

from app.core.config import get_settings
from app.schemas.ai import (
    AIProviderStatus,
    MaterialAlternative,
    MaterialSuggestion,
    MaterialSuggestionResponse,
    SuggestMaterialsRequest,
)
from app.schemas.materials import RFMaterialLibrary
from app.schemas.scene import Prim, Scene

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


def build_evidence(prim: Prim, library: RFMaterialLibrary) -> dict:
    """Collect the ONLY visual information any provider is allowed to see.

    Evidence, never truth: names, tags and texture basenames are hints for
    suggestion providers; the RF binding is untouched. ``library`` is part of
    the pinned signature for providers that want to cross-check evidence
    against material categories; the rule/LLM evidence itself never includes
    RF state.
    """
    visual = prim.visual
    texture = visual.base_color_texture if visual else None
    texture_basename = (
        PurePosixPath(texture.replace("\\", "/")).name if texture else None
    )
    return {
        "prim_id": prim.id,
        "name": prim.name,
        "semantic_tags": list(prim.semantic_tags),
        "visual_material_id": visual.material_id if visual else None,
        "visual_material_name": visual.material_name if visual else None,
        "texture_basename": texture_basename,
    }


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
    size: int = 128,
) -> list[dict]:
    """Extract per-prim baseColor texture crops from the project's visual GLB.

    For each target prim, resolve its geometry through ``mesh_ref.mesh_name``
    against the loaded visual scene; if the geometry's material carries a
    baseColor texture image, downscale it to ``size`` px (longest side) and
    encode a JPEG data URL. Prims without a texture (or without geometry) are
    skipped silently. Returns at most ``max_crops`` entries, in prim order,
    shaped ``[{"prim_id", "data_url"}]``. Best-effort: any failure (missing
    GLB, trimesh/PIL error) yields an empty list rather than raising, so the
    suggest flow degrades to text/screenshots only.
    """
    if max_crops <= 0 or not prim_ids:
        return []
    try:
        from PIL import Image  # lazy: optional runtime dependency path

        from app.services import mesh_tools
    except Exception:  # pragma: no cover - import guard
        return []

    # Group target prims by the visual asset they reference so each GLB is
    # loaded once. Most scenes share a single asset_uri.
    crops: list[dict] = []
    scene_cache: dict[str, object] = {}
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
        if tm_scene is None:
            continue
        try:
            geometry = _resolve_prim_geometry(tm_scene, prim.mesh_ref.mesh_name)
            if geometry is None:
                continue
            image = _texture_image_for_geometry(geometry)
            if image is None:
                continue
            data_url = _encode_crop(Image, image, size)
        except Exception:
            # One bad prim must not sink the whole batch.
            continue
        if data_url is not None:
            crops.append({"prim_id": prim_id, "data_url": data_url})
    return crops


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
    ) -> MaterialSuggestionResponse:
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
                model=settings.text_model,
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
        model = settings.vision_model if has_image else settings.text_model
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
                timeout=settings.timeout_s,
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
                    return self.suggest(scene, library, prim_ids)
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
                "The following attached images are close-up texture crops of "
                "specific prims (EVIDENCE only, in this order):\n"
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
                model=settings.openai_model,
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
                )
            except Exception as img_exc:
                # Graceful degradation: some servers reject image input for a
                # text-only model (typically HTTP 400). Retry WITHOUT the images
                # and record the downgrade rather than failing to rules.
                if has_image and _is_vision_rejection(img_exc):
                    warnings.append(
                        f"vision input rejected by {settings.openai_model}; "
                        "used text only"
                    )
                    raw_text = self._call(evidence_list, library, settings, [])
                else:
                    raise
            suggestions, parse_warnings = parse_ai_response(raw_text, scene, library)
            return MaterialSuggestionResponse(
                suggestions=suggestions,
                provider=self.name,
                model=settings.openai_model,
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
    ) -> str:
        """One chat-completions round-trip; returns the raw answer text.

        Raises on transport/HTTP errors so the caller can distinguish a
        vision-rejection (retry without image) from other failures (fall back
        to rules)."""
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
                "model": settings.openai_model,
                "messages": messages,
                "temperature": 0,
                # Reasoning models spend tokens on chain-of-thought before
                # the answer; keep the budget generous so JSON is not cut.
                "max_tokens": 2000,
                "stream": False,
            },
            timeout=settings.timeout_s,
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
    """Availability of every provider. Never raises (health endpoint)."""
    statuses: list[AIProviderStatus] = []
    statuses.append(
        AIProviderStatus(
            name="rule_based",
            available=True,
            model=None,
            detail="keyword rules; always available",
        )
    )
    try:
        settings = get_settings().ai
        off = settings.enabled == "off"
        if off:
            statuses.append(
                AIProviderStatus(
                    name="local_openai",
                    available=False,
                    model=settings.openai_model,
                    detail=(
                        f"{settings.openai_url} ({settings.openai_model}): "
                        "disabled (SIONNATWIN_AI_ENABLED=off)"
                    ),
                )
            )
            statuses.append(
                AIProviderStatus(
                    name="ollama_text",
                    available=False,
                    model=settings.text_model,
                    detail=f"{settings.base_url}: disabled (SIONNATWIN_AI_ENABLED=off)",
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
                )
            )
            ok, detail = _probe_ollama(settings.base_url)
            statuses.append(
                AIProviderStatus(
                    name="ollama_text",
                    available=ok,
                    model=settings.text_model,
                    detail=detail,
                )
            )
        statuses.append(
            AIProviderStatus(
                name="disabled",
                available=off,
                model=None,
                detail=(
                    "AI assistance disabled (SIONNATWIN_AI_ENABLED=off)"
                    if off
                    else "inactive (AI assistance is enabled)"
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
    return statuses


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
    )
    if response.prompt_version is None:
        response.prompt_version = PROMPT_VERSION
    return response
