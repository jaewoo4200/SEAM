"""Project folder metadata models."""

from typing import Literal, Optional

from pydantic import Field

from .ai import AIProviderStatus
from .common import StrictModel


class ProjectInfo(StrictModel):
    # Folder name without the .seam suffix (legacy .sionnatwin projects keep loading).
    project_id: str
    name: str
    # Absolute path of the project folder on this machine.
    path: str
    scene_id: Optional[str] = None
    created_at: Optional[str] = None
    modified_at: Optional[str] = None


class ProjectCreateRequest(StrictModel):
    name: str = Field(min_length=1)
    # Derived from name when omitted (lowercased, non-alnum -> "_").
    project_id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_\-]+$")
    # Only "empty" is materialized today; the API always creates an empty project.
    template: Literal["empty"] = "empty"


class HealthBackendStatus(StrictModel):
    name: str
    available: bool
    detail: str = ""


class HealthResponse(StrictModel):
    status: Literal["ok"] = "ok"
    app: str = "seam-studio"
    version: str
    schema_version: str
    sionna_available: bool
    backends: list[HealthBackendStatus] = Field(default_factory=list)
    ai_providers: list[AIProviderStatus] = Field(default_factory=list)
    project_roots: list[str] = Field(default_factory=list)
