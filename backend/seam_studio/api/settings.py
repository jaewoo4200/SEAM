"""Global (non-project) app settings: the local-AI endpoint/model overlay.

Persisted as ``~/.seam/settings.json`` and merged inside get_settings(), so a
PUT takes effect for the running process — no backend restart. The overlay
WINS over the ``SEAM_*`` env vars (a settings dialog that env silently
overrides would be a no-op exactly where it is most needed); the response's
``env_fields`` makes any shadowed env var visible to the dialog.
"""

from urllib.parse import urlparse

from fastapi import APIRouter, HTTPException

from seam_studio.core import config, paths
from seam_studio.schemas.ai import AISettingsResponse, AISettingsUpdate

router = APIRouter(tags=["settings"])

# Wire field -> the SEAM_*/SIONNATWIN_* suffix _env() looks up.
_ENV_NAMES = {
    "ollama_url": "OLLAMA_URL",
    "text_model": "AI_TEXT_MODEL",
    "openai_url": "OPENAI_URL",
    "openai_model": "OPENAI_MODEL",
}
_MAX_LEN = 300


def _clean_url(raw: str, field: str) -> str | None:
    """Normalized URL, or None when the field is being cleared."""
    value = raw.strip().rstrip("/")
    if not value:
        return None
    if len(value) > _MAX_LEN:
        raise HTTPException(422, f"{field}: too long (max {_MAX_LEN} chars)")
    try:
        parsed = urlparse(value)
    except ValueError:
        # e.g. unbalanced brackets in an IPv6 host ("http://[::1:11434") —
        # a user typo must be a field error, not a 500.
        parsed = None
    if parsed is None or parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise HTTPException(
            422,
            f"{field}: must be an http(s) URL with a host, "
            "e.g. http://localhost:1234/v1",
        )
    return value


def _clean_model(raw: str, field: str) -> str | None:
    value = raw.strip()
    if not value:
        return None
    if len(value) > _MAX_LEN or any(c in value for c in "\r\n\t"):
        raise HTTPException(422, f"{field}: invalid model id")
    return value


def _response() -> AISettingsResponse:
    overlay, warnings = config.load_ai_overlay()
    ai = config.get_settings().ai
    # The overlay WINS over env/default, so any field present in the overlay
    # must equal the resolved value. A mismatch means the file moved under
    # the lru_cache (a hand edit of the documented ~/.seam/settings.json, or
    # another process's PUT) — re-resolve instead of returning a response
    # that contradicts itself. rstrip matters: get_settings() normalizes
    # URLs but the raw overlay may carry a trailing slash.
    if any(
        getattr(ai, field_name).rstrip("/") != value.rstrip("/")
        for field_name, value in overlay.items()
    ):
        config.get_settings.cache_clear()
        ai = config.get_settings().ai
    field_to_key = {f: k for k, f in config.AI_OVERLAY_FIELDS.items()}
    return AISettingsResponse(
        ollama_url=ai.base_url,
        text_model=ai.text_model,
        openai_url=ai.openai_url,
        openai_model=ai.openai_model,
        overlay_fields=sorted(field_to_key[f] for f in overlay),
        env_fields=sorted(
            key
            for key, name in _ENV_NAMES.items()
            if config._env(name) is not None
        ),
        overlay_path=str(paths.settings_file()),
        warnings=warnings,
    )


@router.get("/settings/ai", response_model=AISettingsResponse)
def get_ai_settings() -> AISettingsResponse:
    """The effective local-AI endpoints/models + where each value comes from."""
    return _response()


@router.put("/settings/ai", response_model=AISettingsResponse)
def put_ai_settings(body: AISettingsUpdate) -> AISettingsResponse:
    """Replace the AI overlay; applies to the running process immediately."""
    values = {
        "base_url": _clean_url(body.ollama_url, "ollama_url"),
        "text_model": _clean_model(body.text_model, "text_model"),
        "openai_url": _clean_url(body.openai_url, "openai_url"),
        "openai_model": _clean_model(body.openai_model, "openai_model"),
    }
    try:
        config.save_ai_overlay(values)
    except OSError as exc:
        # The cache is deliberately NOT cleared here: the running process must
        # keep matching what is actually on disk.
        raise HTTPException(500, f"could not write settings overlay: {exc}")
    config.get_settings.cache_clear()
    # Lazy import (the AI provider module may probe a local server on import
    # paths); clears the 30s reachability/model caches so the next /ai/status
    # re-probes the NEW endpoints instead of serving entries for the old ones.
    from seam_studio.services import ai_provider

    ai_provider.invalidate_probe_caches()
    return _response()
