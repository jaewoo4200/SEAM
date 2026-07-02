"""Typed contracts for SionnaTwin Studio.

Import from submodules (``app.schemas.scene``); this package re-exports the
most commonly used models for convenience.
"""

from .common import (
    SCHEMA_VERSION,
    ASSIGNMENT_STATUSES,
    ActiveAssignmentStatus,
    AssignmentStatus,
    Transform,
)
from .devices import Antenna, Device
from .materials import (
    AssignRequest,
    AssignResponse,
    BatchAssignRequest,
    RFMaterial,
    RFMaterialLibrary,
    RFOverrides,
)
from .scene import (
    CoordinateSystem,
    MeshRef,
    Prim,
    ResultSetRef,
    RFBinding,
    Scene,
    SceneAssets,
    VisualBinding,
)
from .simulation import RadioMapGridConfig, SimulateRequest, SimulationConfig
from .results import (
    PathInteraction,
    PathResultSet,
    RadioMapGrid,
    RadioMapResultSet,
    RayPath,
)
from .ai import (
    AIProviderStatus,
    ApplySuggestionsRequest,
    MaterialAlternative,
    MaterialSuggestion,
    MaterialSuggestionResponse,
    SuggestionDecision,
    SuggestMaterialsRequest,
)
from .validation import Severity, ValidationIssue, ValidationReport
from .compile import CompileResult, MaterialGroup
from .projects import (
    HealthBackendStatus,
    HealthResponse,
    ProjectCreateRequest,
    ProjectInfo,
)

__all__ = [
    "SCHEMA_VERSION",
    "ASSIGNMENT_STATUSES",
    "ActiveAssignmentStatus",
    "AssignmentStatus",
    "Transform",
    "Antenna",
    "Device",
    "AssignRequest",
    "AssignResponse",
    "BatchAssignRequest",
    "RFMaterial",
    "RFMaterialLibrary",
    "RFOverrides",
    "CoordinateSystem",
    "MeshRef",
    "Prim",
    "ResultSetRef",
    "RFBinding",
    "Scene",
    "SceneAssets",
    "VisualBinding",
    "RadioMapGridConfig",
    "SimulateRequest",
    "SimulationConfig",
    "PathInteraction",
    "PathResultSet",
    "RadioMapGrid",
    "RadioMapResultSet",
    "RayPath",
    "AIProviderStatus",
    "ApplySuggestionsRequest",
    "MaterialAlternative",
    "MaterialSuggestion",
    "MaterialSuggestionResponse",
    "SuggestionDecision",
    "SuggestMaterialsRequest",
    "Severity",
    "ValidationIssue",
    "ValidationReport",
    "CompileResult",
    "MaterialGroup",
    "HealthBackendStatus",
    "HealthResponse",
    "ProjectCreateRequest",
    "ProjectInfo",
]
