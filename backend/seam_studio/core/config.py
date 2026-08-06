"""Runtime settings, sourced from environment variables + a settings overlay.

Kept dependency-free (no pydantic-settings): a frozen dataclass built once.

The four local-AI endpoint/model settings (ollama_url, text_model, openai_url,
openai_model) can ALSO come from ``~/.seam/settings.json`` (written by the
in-app AI-settings dialog via PUT /api/settings/ai). The overlay WINS over the
env vars — otherwise the dialog would be a silent no-op on machines whose
owners already exported SEAM_* — and applies to the running process because
the API clears this module's cache after every write.

Environment variables (SEAM rename): every setting prefers a ``SEAM_*`` name
and falls back to the legacy ``SIONNATWIN_*`` name via the ``_env`` helper -
``SEAM_*`` wins when both are set. Names below list the SEAM form; the
``SIONNATWIN_*`` equivalent is accepted for back-compat.

- SEAM_PROJECT_ROOTS   os.pathsep-separated list of project root dirs
- SEAM_AI_ENABLED      "auto" (default) | "on" | "off"
- SEAM_OLLAMA_URL      default http://localhost:11434
- SEAM_AI_TEXT_MODEL   default qwen3:8b
- SEAM_AI_VISION_MODEL default qwen2.5vl:3b (used when screenshots are sent)
- SEAM_OPENAI_URL      default http://localhost:1234/v1 (LM Studio, OpenAI-compatible)
- SEAM_OPENAI_MODEL    default google/gemma-4-31b
- SEAM_AI_TIMEOUT_S    default 60
- SEAM_AI_AUTO_APPLY   "1"/"true" to let high-confidence suggestions
                       auto-apply (never the default; HANDOFF 9.5)
"""

import json
import os
import uuid
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .paths import DEFAULT_PROJECT_ROOTS

APP_VERSION = "0.1.4"


@dataclass(frozen=True)
class AISettings:
    enabled: str = "auto"  # auto | on | off
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    text_model: str = "qwen3:8b"
    vision_model: str = "qwen2.5vl:3b"
    # LM Studio's OpenAI-compatible server (a SOTA local reasoning model).
    openai_url: str = "http://localhost:1234/v1"
    openai_model: str = "google/gemma-4-31b"
    timeout_s: float = 60.0
    # Separate ceiling for multimodal (image-carrying) calls: a local 20-30B
    # VLM needs model load + multi-image prefill, which routinely exceeds the
    # text timeout. Only used when images are attached.
    vision_timeout_s: float = 300.0
    auto_apply: bool = False


@dataclass(frozen=True)
class Settings:
    project_roots: tuple[Path, ...] = field(
        default_factory=lambda: tuple(DEFAULT_PROJECT_ROOTS)
    )
    ai: AISettings = field(default_factory=AISettings)


def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Read a setting env var, preferring ``SEAM_<name>`` over the legacy
    ``SIONNATWIN_<name>``. ``SEAM_*`` wins when both are set; returns
    ``default`` when neither is present.
    """
    value = os.environ.get(f"SEAM_{name}")
    if value is None:
        value = os.environ.get(f"SIONNATWIN_{name}")
    return default if value is None else value


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


# Overlay/wire key -> AISettings field. The wire names mirror the env-var
# semantics (SEAM_OLLAMA_URL -> base_url) so settings.json reads naturally
# and the dialog labels map 1:1; only base_url needs the rename.
AI_OVERLAY_FIELDS: dict[str, str] = {
    "ollama_url": "base_url",
    "text_model": "text_model",
    "openai_url": "openai_url",
    "openai_model": "openai_model",
}


def load_ai_overlay() -> tuple[dict[str, str], list[str]]:
    """(AISettings-field -> value, warnings) from ``~/.seam/settings.json``.

    Never raises: a missing file yields ({}, []), and an unreadable or
    malformed one yields ({}, [reason]) so the app falls back to env +
    defaults instead of failing to start.
    """
    from .paths import settings_file  # local: keeps the import graph flat

    path = settings_file()
    if not path.is_file():
        return {}, []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"{path}: unreadable settings overlay ({exc}); using env/defaults"]
    ai = raw.get("ai") if isinstance(raw, dict) else None
    if not isinstance(ai, dict):
        return {}, []
    out: dict[str, str] = {}
    for key, field_name in AI_OVERLAY_FIELDS.items():
        value = ai.get(key)
        if isinstance(value, str) and value.strip():
            out[field_name] = value.strip()
    return out, []


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    roots_raw = _env("PROJECT_ROOTS")
    if roots_raw:
        roots = tuple(Path(p).expanduser() for p in roots_raw.split(os.pathsep) if p.strip())
    else:
        roots = tuple(DEFAULT_PROJECT_ROOTS)

    overlay, _ = load_ai_overlay()

    def _ai(field_name: str, env_name: str, default: str) -> str:
        # Overlay wins over env: the settings dialog must never be a silent
        # no-op on a machine where SEAM_* is exported (see module docstring).
        value = overlay.get(field_name)
        return value if value is not None else _env(env_name, default)

    ai = AISettings(
        enabled=_env("AI_ENABLED", "auto").strip().lower(),
        base_url=_ai("base_url", "OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        text_model=_ai("text_model", "AI_TEXT_MODEL", "qwen3:8b"),
        vision_model=_env("AI_VISION_MODEL", "qwen2.5vl:3b"),
        openai_url=_ai("openai_url", "OPENAI_URL", "http://localhost:1234/v1").rstrip("/"),
        openai_model=_ai("openai_model", "OPENAI_MODEL", "google/gemma-4-31b"),
        timeout_s=float(_env("AI_TIMEOUT_S", "60")),
        vision_timeout_s=float(_env("AI_VISION_TIMEOUT_S", "300")),
        auto_apply=_bool_env("AI_AUTO_APPLY", False),
    )
    return Settings(project_roots=roots, ai=ai)


def _atomic_write_text(path: Path, text: str) -> None:
    # Mirrors services.project_store._atomic_write_text. Duplicated on
    # purpose: project_store imports this module, so importing it back
    # would be a cycle.
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(f"{path.suffix}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save_ai_overlay(values: dict[str, Optional[str]]) -> Path:
    """Replace the ``ai`` block of ``~/.seam/settings.json``; returns the path.

    ``values`` is keyed by AISettings FIELD name; a None value drops the key
    (that field reverts to env/default). Any pre-existing non-``ai`` content
    is preserved. Raises OSError when the file cannot be written — the caller
    must NOT clear the settings cache in that case, so the running process
    keeps matching what is on disk.
    """
    from .paths import settings_file

    path = settings_file()
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(existing, dict):
            existing = {}
    except (OSError, json.JSONDecodeError):
        existing = {}
    field_to_key = {f: k for k, f in AI_OVERLAY_FIELDS.items()}
    ai_block = {
        field_to_key[field_name]: value
        for field_name, value in values.items()
        if value is not None
    }
    existing["version"] = 1
    existing["ai"] = ai_block
    _atomic_write_text(path, json.dumps(existing, indent=2) + "\n")
    return path
