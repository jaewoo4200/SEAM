"""Radio devices (transmitters/receivers) placed in the canonical scene.

Decision: device ids are short ("tx_001", "rx_001"), not path-like prim ids.
Devices are not geometry prims; result schemas reference these short ids
directly (HANDOFF.md section 11 examples). The frontend scene tree shows them
under a synthetic "/devices" node.
"""

from typing import Literal, Optional

from pydantic import Field, model_validator

from .common import StrictModel, Vec3


class Antenna(StrictModel):
    # Sionna RT antenna pattern name ("iso", "dipole", "hw_dipole", "tr38901").
    pattern: str = "iso"
    polarization: Literal["V", "H", "VH", "cross"] = "V"
    num_rows: int = Field(default=1, ge=1)
    num_cols: int = Field(default=1, ge=1)
    # Element spacing in wavelengths (sionna PlanarArray convention; 0.5 =
    # half-wavelength). Exposed for parity with the Sionna RT GUI array panel.
    vertical_spacing: float = Field(default=0.5, gt=0.0)
    horizontal_spacing: float = Field(default=0.5, gt=0.0)


class Device(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    name: str = ""
    kind: Literal["tx", "rx"]
    position: Vec3
    # Yaw, pitch, roll in degrees (ENU frame).
    orientation_deg: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    # Velocity [vx, vy, vz] m/s in the scene's Z-up world frame. When set, the
    # Sionna backend applies it to this device's Transmitter/Receiver so solved
    # paths carry a per-path Doppler shift (f_d = v.k/lambda). None (default) =
    # stationary; the device geometry/ray tracing is unaffected either way.
    velocity_m_s: Optional[Vec3] = None
    # Transmit power; ignored for rx.
    power_dbm: float = 30.0
    antenna: Antenna = Field(default_factory=Antenna)
    # Display color for viewer markers; defaults by kind per the AODT legend
    # (TX red, UE/RX blue) when not explicitly set.
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def _default_color_by_kind(self) -> "Device":
        if self.color is None:
            object.__setattr__(
                self, "color", "#ff0000" if self.kind == "tx" else "#2e9bff"
            )
        return self
