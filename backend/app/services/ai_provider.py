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
import json
import re
import time
from pathlib import PurePosixPath
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

PROMPT_VERSION = "v1"

FALLBACK_MATERIAL_ID = "unknown_rf"
FALLBACK_CONFIDENCE = 0.2
ALTERNATIVE_CONFIDENCE_FACTOR = 0.5

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


class MaterialSuggestionProvider(abc.ABC):
    """Provider abstraction (HANDOFF 9.2)."""

    name: str = "abstract"

    @abc.abstractmethod
    def is_available(self) -> bool: ...

    @abc.abstractmethod
    def suggest(
        self, scene: Scene, library: RFMaterialLibrary, prim_ids: list[str]
    ) -> MaterialSuggestionResponse: ...


class RuleBasedProvider(MaterialSuggestionProvider):
    """Keyword rules over visual evidence. Always available, never networked."""

    name = "rule_based"

    def is_available(self) -> bool:
        return True

    def suggest(
        self, scene: Scene, library: RFMaterialLibrary, prim_ids: list[str]
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

    def is_available(self) -> bool:
        settings = get_settings().ai
        if settings.enabled == "off":
            return False
        ok, _ = _probe_ollama(settings.base_url)
        return ok

    def suggest(
        self, scene: Scene, library: RFMaterialLibrary, prim_ids: list[str]
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
        try:
            import httpx  # lazy: never required at import time

            response = httpx.post(
                f"{settings.base_url}/api/chat",
                json={
                    "model": settings.text_model,
                    "messages": self._build_messages(evidence_list, library),
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
                model=settings.text_model,
                prompt_version=PROMPT_VERSION,
                warnings=warnings + parse_warnings,
            )
        except Exception as exc:
            reason = str(exc) or exc.__class__.__name__
            fallback = RuleBasedProvider().suggest(scene, library, prim_ids)
            fallback.warnings = [
                f"ollama_text failed: {reason}; fell back to rule_based",
                *fallback.warnings,
            ]
            return fallback

    @staticmethod
    def _build_messages(evidence_list: list[dict], library: RFMaterialLibrary) -> list[dict]:
        library_lines = "\n".join(
            f"- {mat.id} (category: {mat.category}){': ' + mat.notes if mat.notes else ''}"
            for mat in library.materials
        )
        schema_example = {
            "suggestions": [
                {
                    "prim_id": "/buildings/b07/window_12",
                    "recommended_rf_material_id": "itu_glass",
                    "confidence": 0.86,
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
            "(names, tags, texture filenames) is a hint, never ground truth. "
            "Respond ONLY with JSON."
        )
        user = (
            "Allowed rf material ids (use ONLY these for "
            "recommended_rf_material_id and alternatives):\n"
            f"{library_lines}\n\n"
            "Objects to classify, one suggestion per object "
            "(visual evidence only):\n"
            f"{json.dumps(evidence_list, indent=2)}\n\n"
            "Respond ONLY with JSON exactly matching this schema example "
            "(no prose, no markdown):\n"
            f"{json.dumps(schema_example, indent=2)}\n\n"
            "Rules: confidence is a number in [0, 1]; evidence entries are "
            "short human-readable strings citing the input evidence; "
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

    def is_available(self) -> bool:
        settings = get_settings().ai
        if settings.enabled == "off":
            return False
        ok, _ = _probe_openai(settings.openai_url)
        return ok

    def suggest(
        self, scene: Scene, library: RFMaterialLibrary, prim_ids: list[str]
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
        try:
            import httpx  # lazy: never required at import time

            messages = OllamaTextProvider._build_messages(evidence_list, library)
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
            raw_text = self._extract_content(response.json())
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
        self, scene: Scene, library: RFMaterialLibrary, prim_ids: list[str]
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
    scene: Scene, library: RFMaterialLibrary, request: SuggestMaterialsRequest
) -> MaterialSuggestionResponse:
    """Entry point used by the /ai/suggest-materials endpoint.

    Raises ValueError for an unknown forced provider (API maps it to 400).
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
    response = provider.suggest(scene, library, targets)
    if response.prompt_version is None:
        response.prompt_version = PROMPT_VERSION
    return response
