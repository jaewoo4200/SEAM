"""Radio devices (transmitters/receivers) placed in the canonical scene.

Decision: device ids are short ("tx_001", "rx_001"), not path-like prim ids.
Devices are not geometry prims; result schemas reference these short ids
directly (HANDOFF.md section 11 examples). The frontend scene tree shows them
under a synthetic "/devices" node.
"""

from typing import Literal

from pydantic import Field

from .common import StrictModel, Vec3


class Antenna(StrictModel):
    # Sionna RT antenna pattern name ("iso", "dipole", "hw_dipole", "tr38901").
    pattern: str = "iso"
    polarization: Literal["V", "H", "VH", "cross"] = "V"
    num_rows: int = Field(default=1, ge=1)
    num_cols: int = Field(default=1, ge=1)


class Device(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    name: str = ""
    kind: Literal["tx", "rx"]
    position: Vec3
    # Yaw, pitch, roll in degrees (ENU frame).
    orientation_deg: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    # Transmit power; ignored for rx.
    power_dbm: float = 30.0
    antenna: Antenna = Field(default_factory=Antenna)
    # Display color for viewer markers.
    color: str = Field(default="#ff4136", pattern=r"^#[0-9a-fA-F]{6}$")
