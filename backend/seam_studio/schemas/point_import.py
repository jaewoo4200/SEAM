"""Request/response schemas for the point/device/trajectory import API.

One JSON schema a user can hand-author or export from a GPS tool, covering
devices (TX/RX/UE) and UE trajectory waypoints in BOTH cartesian (local ENU
Z-up meters) and geographic (WGS84 lat/lon) coordinates. See
``docs/point_import.md`` and ``app.services.point_import`` for the resolution
rules (auto-detection, AGL, underground warnings, geodetic conversion).
"""

from typing import Annotated, Literal, Optional, Union

from pydantic import Field

from .common import StrictModel, Vec3
from .devices import Antenna

# A point given as an array: [x, y] or [x, y, z] (cartesian ENU meters, Z-up).
PointArray = Annotated[list[float], Field(min_length=2, max_length=3)]


class PointObject(StrictModel):
    """A point given as an object, in either coordinate system.

    Cartesian: ``x``/``y`` (+ optional ``z`` or ``agl_m``). Geographic:
    ``lat``/``lon`` (+ optional ``alt_m`` or ``agl_m``). The presence of
    ``lat`` AND ``lon`` selects the geographic reading. ``agl_m`` (height above
    the scene surface) and ``z``/``alt_m`` (absolute height) are mutually
    exclusive; if both are given AGL wins and a warning is emitted.
    """

    # Cartesian (local ENU meters, Z-up).
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    # Optional [yaw, pitch, roll] degrees (ENU) — for trajectory waypoints, the
    # UE's antenna orientation at this point; ignored for device points (a
    # device carries its own orientation_deg field).
    orientation_deg: Optional[list[float]] = None
    # Geographic (WGS84 degrees + absolute altitude in meters).
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_m: Optional[float] = None
    # Height above the scene surface (terrain raycast); overrides z/alt_m.
    agl_m: Optional[float] = None


# Any accepted point form. A JSON array resolves to PointArray, a JSON object
# to PointObject (pydantic smart-union picks the one that validates).
PointInput = Union[PointArray, PointObject]


class ImportDevice(StrictModel):
    """One device to import.

    Location may be given as ``position`` (any point form) OR as top-level
    coordinate fields (``x``/``y``/``z`` or ``lat``/``lon``/``alt_m``/
    ``agl_m``). ``kind`` defaults to ``rx`` and ``id`` is auto-generated
    (``tx_00N`` / ``rx_00N``) when omitted.
    """

    id: Optional[str] = None
    # Validated in the service so the error is an actionable 400, not a 422.
    kind: Optional[str] = None
    name: Optional[str] = None
    orientation_deg: Optional[Vec3] = None
    velocity_m_s: Optional[Vec3] = None
    power_dbm: Optional[float] = None
    antenna: Optional[Antenna] = None
    color: Optional[str] = None
    # Location as a self-contained point...
    position: Optional[PointInput] = None
    # ...or as top-level coordinate fields (auto-detected the same way).
    x: Optional[float] = None
    y: Optional[float] = None
    z: Optional[float] = None
    lat: Optional[float] = None
    lon: Optional[float] = None
    alt_m: Optional[float] = None
    agl_m: Optional[float] = None


class DeviceImportRequest(StrictModel):
    devices: list[ImportDevice] = Field(min_length=1)
    # upsert: update an existing device by id (default); add: 409 on collision.
    mode: Literal["upsert", "add"] = "upsert"


class DeviceImportResponse(StrictModel):
    added_ids: list[str] = Field(default_factory=list)
    updated_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class TrajectoryImportRequest(StrictModel):
    # Echoed back to the caller; the endpoint does NOT mutate the scene.
    ue_id: Optional[str] = None
    points: list[PointInput] = Field(min_length=1)
    # Default AGL height for any waypoint that gives neither z nor agl_m.
    # Pass null to place such waypoints at z = 0 instead.
    agl_m: Optional[float] = 1.5


class TrajectoryImportResponse(StrictModel):
    ue_id: Optional[str] = None
    # Fully cartesian waypoints [[x, y, z], ...] for the trajectory-routes UI.
    waypoints: list[Vec3] = Field(default_factory=list)
    # Parallel to waypoints: per-waypoint [yaw, pitch, roll] degrees, or null
    # where a point gave no orientation. Fed into UERoute.orientations_deg so
    # the moving UE's antenna turns with it. Omitted entirely (None) when no
    # waypoint carried an orientation.
    orientations_deg: Optional[list[Optional[Vec3]]] = None
    warnings: list[str] = Field(default_factory=list)
