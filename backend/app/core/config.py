"""Runtime settings, sourced from environment variables.

Kept dependency-free (no pydantic-settings): a frozen dataclass built once.

Environment variables:
- SIONNATWIN_PROJECT_ROOTS   os.pathsep-separated list of project root dirs
- SIONNATWIN_AI_ENABLED      "auto" (default) | "on" | "off"
- SIONNATWIN_OLLAMA_URL      default http://localhost:11434
- SIONNATWIN_AI_TEXT_MODEL   default qwen3:8b
- SIONNATWIN_AI_VISION_MODEL default qwen2.5vl:3b (used when screenshots are sent)
- SIONNATWIN_AI_TIMEOUT_S    default 60
- SIONNATWIN_AI_AUTO_APPLY   "1"/"true" to let high-confidence suggestions
                             auto-apply (never the default; HANDOFF 9.5)
"""

import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .paths import DEFAULT_PROJECT_ROOTS

APP_VERSION = "0.1.0"


@dataclass(frozen=True)
class AISettings:
    enabled: str = "auto"  # auto | on | off
    provider: str = "ollama"
    base_url: str = "http://localhost:11434"
    text_model: str = "qwen3:8b"
    vision_model: str = "qwen2.5vl:3b"
    timeout_s: float = 60.0
    auto_apply: bool = False


@dataclass(frozen=True)
class Settings:
    project_roots: tuple[Path, ...] = field(
        default_factory=lambda: tuple(DEFAULT_PROJECT_ROOTS)
    )
    ai: AISettings = field(default_factory=AISettings)


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    roots_raw = os.environ.get("SIONNATWIN_PROJECT_ROOTS")
    if roots_raw:
        roots = tuple(Path(p).expanduser() for p in roots_raw.split(os.pathsep) if p.strip())
    else:
        roots = tuple(DEFAULT_PROJECT_ROOTS)

    ai = AISettings(
        enabled=os.environ.get("SIONNATWIN_AI_ENABLED", "auto").strip().lower(),
        base_url=os.environ.get("SIONNATWIN_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
        text_model=os.environ.get("SIONNATWIN_AI_TEXT_MODEL", "qwen3:8b"),
        vision_model=os.environ.get("SIONNATWIN_AI_VISION_MODEL", "qwen2.5vl:3b"),
        timeout_s=float(os.environ.get("SIONNATWIN_AI_TIMEOUT_S", "60")),
        auto_apply=_bool_env("SIONNATWIN_AI_AUTO_APPLY", False),
    )
    return Settings(project_roots=roots, ai=ai)
