"""Runtime settings, sourced from environment variables.

Kept dependency-free (no pydantic-settings): a frozen dataclass built once.

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

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from .paths import DEFAULT_PROJECT_ROOTS

APP_VERSION = "0.1.0"


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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    roots_raw = _env("PROJECT_ROOTS")
    if roots_raw:
        roots = tuple(Path(p).expanduser() for p in roots_raw.split(os.pathsep) if p.strip())
    else:
        roots = tuple(DEFAULT_PROJECT_ROOTS)

    ai = AISettings(
        enabled=_env("AI_ENABLED", "auto").strip().lower(),
        base_url=_env("OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        text_model=_env("AI_TEXT_MODEL", "qwen3:8b"),
        vision_model=_env("AI_VISION_MODEL", "qwen2.5vl:3b"),
        openai_url=_env("OPENAI_URL", "http://localhost:1234/v1").rstrip("/"),
        openai_model=_env("OPENAI_MODEL", "google/gemma-4-31b"),
        timeout_s=float(_env("AI_TIMEOUT_S", "60")),
        auto_apply=_bool_env("AI_AUTO_APPLY", False),
    )
    return Settings(project_roots=roots, ai=ai)
