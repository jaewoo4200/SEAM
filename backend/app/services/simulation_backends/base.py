"""Stable ray-tracing backend interface (HANDOFF.md section 7).

All backends normalize into the backend-neutral result schemas in
``app.schemas.results``. Backends never persist anything - the API layer owns
result ids, storage, and provenance. Backends therefore leave ``result_id``
as a placeholder and ``created_at`` unset so their output stays deterministic
and the caller can stamp storage metadata afterwards.
"""

import abc
from pathlib import Path

from app.schemas.compile import CompileResult
from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import PathResultSet, RadioMapResultSet
from app.schemas.scene import Scene
from app.schemas.simulation import SimulationConfig

# Placeholder result_id used by backends; the API layer replaces it with the
# canonical "<backend>_<kind>_<nnn>" id when the result is stored.
UNSAVED_RESULT_ID = "unsaved"


class BackendUnavailableError(RuntimeError):
    """Raised when a named backend cannot run on this machine."""


class RayTracingBackend(abc.ABC):
    name: str

    @abc.abstractmethod
    def is_available(self) -> bool: ...

    def compile(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
    ) -> CompileResult:
        # Import inside the method: the compiler service is built by a sibling
        # module and may not exist yet; this package must import regardless.
        try:
            from app.services.rf_compiler import compile_project
        except ImportError as exc:
            return CompileResult(
                ok=False,
                errors=[f"rf compiler unavailable: {exc}"],
                warnings=["rf_compiler service not importable; compile skipped"],
            )
        return compile_project(project_dir, scene, library)

    @abc.abstractmethod
    def simulate_paths(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> PathResultSet: ...

    @abc.abstractmethod
    def simulate_radio_map(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> RadioMapResultSet: ...
