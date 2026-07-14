"""Compute-engine registry schema.

An "engine" is a concrete Sionna installation the paths solver can run on.
The builtin engine is the sionna-rt import living in the backend's own venv;
additional engines are separate virtual environments (different sionna-rt
versions) driven through a subprocess worker so researchers can pick the
engine version their study requires (see docs/sionna_versions.md).
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel


class EngineInfo(StrictModel):
    # Stable id referenced by SimulationConfig.engine ("builtin" is implicit).
    id: str
    label: str
    kind: Literal["builtin", "subprocess"] = "subprocess"
    # Worker adapter. "sionna_rt" drives any standalone sionna-rt 1.x/2.x venv
    # through engine_workers/sionna_rt_worker.py; legacy TF-era (sionna<=0.19)
    # adapters are a documented roadmap item.
    adapter: Literal["builtin", "sionna_rt"] = "sionna_rt"
    # Interpreter of the engine venv (subprocess engines only).
    python: Optional[str] = None
    available: bool = False
    # Probed sionna / sionna-rt version string, when importable.
    version: Optional[str] = None
    detail: str = ""


class EngineListResponse(StrictModel):
    engines: list[EngineInfo] = Field(default_factory=list)
