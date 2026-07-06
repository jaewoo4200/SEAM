"""Stable ray-tracing backend interface (HANDOFF.md section 7).

All backends normalize into the backend-neutral result schemas in
``app.schemas.results``. Backends never persist anything - the API layer owns
result ids, storage, and provenance. Backends therefore leave ``result_id``
as a placeholder and ``created_at`` unset so their output stays deterministic
and the caller can stamp storage metadata afterwards.
"""

import abc
import math
from pathlib import Path
from typing import Optional

from app.schemas.compile import CompileResult
from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import BeamformingResult, PathResultSet, RadioMapResultSet
from app.schemas.scene import Scene
from app.schemas.simulation import BeamformingRequest, SimulationConfig

# Placeholder result_id used by backends; the API layer replaces it with the
# canonical "<backend>_<kind>_<nnn>" id when the result is stored.
UNSAVED_RESULT_ID = "unsaved"


def _direction_angles_deg(dx: float, dy: float, dz: float) -> Optional[list[float]]:
    """[azimuth_deg, elevation_deg] of a direction vector (Z-up), or None."""
    norm = math.sqrt(dx * dx + dy * dy + dz * dz)
    if norm <= 1e-12:
        return None
    az = math.degrees(math.atan2(dy, dx))
    el = math.degrees(math.asin(max(-1.0, min(1.0, dz / norm))))
    return [az, el]


def geometric_departure_arrival_deg(
    vertices: list[list[float]],
) -> tuple[Optional[list[float]], Optional[list[float]]]:
    """(aod, aoa) as [azimuth_deg, elevation_deg] from a path polyline.

    Departure points from the TX toward the first bounce (or the RX for LoS);
    arrival points FROM the RX back toward the last bounce - the direction the
    wave arrives from. Exact for specular ray paths, so this doubles as both
    the mock's angle model and the sionna fallback when solver angle tensors
    are unavailable.
    """
    if len(vertices) < 2:
        return None, None
    tx, first = vertices[0], vertices[1]
    last, rx = vertices[-2], vertices[-1]
    aod = _direction_angles_deg(
        first[0] - tx[0], first[1] - tx[1], first[2] - tx[2]
    )
    aoa = _direction_angles_deg(
        last[0] - rx[0], last[1] - rx[1], last[2] - rx[2]
    )
    return aod, aoa


class BackendUnavailableError(RuntimeError):
    """Raised when a named backend cannot run on this machine."""


class RayTracingBackend(abc.ABC):
    name: str

    @abc.abstractmethod
    def is_available(self) -> bool: ...

    def capabilities(self) -> dict:
        """Structured feature map for GET /api/backends.

        Keys are stable and additive; frontends must treat missing keys as
        False. Subclasses override to advertise what they can actually solve.
        """
        return {
            "paths": True,
            "radio_map": True,
            "mesh_radio_map": True,  # service-level (chunked probe solves)
            "cir": True,  # derived from paths via /analyze/channel
            "beamforming": False,
            "doppler": False,
            "diffraction": False,
            "gpu": False,
        }

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

    def simulate_beamforming(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
        request: "BeamformingRequest",
    ) -> BeamformingResult:
        """MIMO beamforming gain over one link. Default: unsupported."""
        txs = [d for d in scene.devices if d.kind == "tx"]
        rxs = [d for d in scene.devices if d.kind == "rx"]
        return BeamformingResult(
            backend=self.name,
            simulation_config_id=config.id,
            tx_id=txs[0].id if txs else "",
            rx_id=rxs[0].id if rxs else "",
            frequency_hz=config.frequency_hz,
            tx_array=[request.tx_rows, request.tx_cols],
            rx_array=[request.rx_rows, request.rx_cols],
            warnings=[f"beamforming not supported by the {self.name} backend"],
        )
