"""Simulation configuration stored in the canonical scene."""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel


class RadioMapGridConfig(StrictModel):
    cell_size_m: float = Field(default=2.0, gt=0.0)
    # Height above ground for the planar measurement grid.
    height_m: float = 1.5
    # Default matches Sionna RT's preview/render default (path gain in dB).
    metric: Literal["path_gain_db", "rss_dbm"] = "path_gain_db"


class SimulationConfig(StrictModel):
    id: str = "default"
    name: str = "Default"
    # "auto" resolves to the sionna backend when installed, else mock.
    backend: Literal["auto", "mock", "sionna"] = "auto"
    # Default 28 GHz to match the FTC/lab-room mmWave ISAC digital twin.
    frequency_hz: float = Field(default=28e9, gt=0.0)
    max_depth: int = Field(default=3, ge=0, le=10)
    # None means all devices of that kind in the scene.
    tx_ids: Optional[list[str]] = None
    rx_ids: Optional[list[str]] = None
    los: bool = True
    reflection: bool = True
    diffraction: bool = False
    scattering: bool = False
    # Ray-launching sample budget (consumer-level default, refinable later).
    num_samples: int = Field(default=100_000, ge=1)
    radio_map: RadioMapGridConfig = Field(default_factory=RadioMapGridConfig)


class SimulateRequest(StrictModel):
    """Body for POST /simulate/paths and /simulate/radio-map."""

    # Use a config stored in the scene by id...
    config_id: Optional[str] = None
    # ...or supply an inline config (wins over config_id).
    config: Optional[SimulationConfig] = None


class TrajectorySimulateRequest(StrictModel):
    """Body for POST /simulate/trajectory: move one RX along waypoints."""

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    # RX device to move; None = the first rx in the scene.
    ue_id: Optional[str] = None
    # Explicit waypoints (meters, Z-up)...
    waypoints: Optional[list[list[float]]] = None
    # ...or a straight line: start -> end sampled at num_points.
    start_m: Optional[list[float]] = None
    end_m: Optional[list[float]] = None
    num_points: int = Field(default=8, ge=2, le=200)
    dt_s: float = Field(default=0.1, gt=0.0)
