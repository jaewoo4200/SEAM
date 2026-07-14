"""Scenario simulation and live-sync contracts for dynamic actors.

The Actor model itself lives in schemas/scene.py (it is scene content); this
module holds the time-indexed scenario results and the real-world state-sync
payloads that animate actors/devices (V2X, AI-RAN closed loops).
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel, Vec3
from .results import RayPath
from .simulation import SimulationConfig


class ActorState(StrictModel):
    id: str
    position: Vec3
    orientation_deg: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class DeviceState(StrictModel):
    id: str
    position: Vec3


class LinkMetrics(StrictModel):
    tx_id: str
    rx_id: str
    rss_dbm: Optional[float] = None
    path_gain_db: Optional[float] = None
    # Co-channel interference at this RX from every OTHER TX in the frame
    # (full-buffer); None when the scene has a single TX or nothing arrives.
    interference_dbm: Optional[float] = None
    # True SINR = S / (I + N); equals the SNR when interference_dbm is None.
    sinr_db: Optional[float] = None
    rms_delay_spread_ns: Optional[float] = None
    path_count: int = 0


class ScenarioFrame(StrictModel):
    time_s: float
    actor_states: list[ActorState] = Field(default_factory=list)
    device_states: list[DeviceState] = Field(default_factory=list)
    links: list[LinkMetrics] = Field(default_factory=list)
    # Full ray paths for this frame (heavy; included when requested).
    paths: Optional[list[RayPath]] = None


class ScenarioResultSet(StrictModel):
    result_id: str
    kind: Literal["scenario"] = "scenario"
    backend: str
    simulation_config_id: str
    created_at: Optional[str] = None
    frames: list[ScenarioFrame] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class ScenarioSimulateRequest(StrictModel):
    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    num_frames: int = Field(default=20, ge=1, le=1000)
    dt_s: float = Field(default=0.1, gt=0.0)
    include_paths: bool = True


class LiveStateUpdate(StrictModel):
    """External real-world state push (positions from GPS/mocap/logs).

    The digital twin applies these to matching devices/actors; with
    resimulate=True the path solver re-runs and returns fresh links so a
    closed loop (measure -> sync -> predict -> act) can run continuously.
    """

    timestamp: Optional[str] = None
    devices: list[DeviceState] = Field(default_factory=list)
    actors: list[ActorState] = Field(default_factory=list)
    resimulate: bool = False
    persist: bool = False  # write positions into the stored scene


class LiveStateResponse(StrictModel):
    applied_devices: list[str] = Field(default_factory=list)
    applied_actors: list[str] = Field(default_factory=list)
    unknown_ids: list[str] = Field(default_factory=list)
    links: list[LinkMetrics] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
