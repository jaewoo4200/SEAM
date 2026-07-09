"""Resolve imported points/devices/trajectories to canonical scene coordinates.

Everything the import API needs to turn a hand-authored or GPS-exported JSON
payload into canonical devices and cartesian trajectory waypoints:

- per-point auto-detection of cartesian ([x, y, z]) vs geographic (lat/lon);
- WGS84 geodetic -> ECEF -> local ENU conversion about the scene's geodetic
  anchor (``scene.coordinate_system.origin_lat_lon_alt``), implemented by hand
  with the standard closed-form formulas (no new dependency);
- AGL (height above the scene surface) resolution and an underground warning
  for explicit-z points, both via ``terrain.snap_to_terrain`` raycasts.

The canonical scene is Z-up local ENU meters (East x, North y, Up z), so ENU is
exactly the frame every other part of SEAM already assumes.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Optional

from ..schemas.devices import Device
from ..schemas.point_import import (
    DeviceImportRequest,
    ImportDevice,
    PointObject,
)
from ..schemas.scene import Scene
from .terrain import snap_to_terrain

# ------------------------------------------------------------------- errors


class GeoAnchorMissingError(ValueError):
    """A geographic point was given but the scene has no geodetic anchor."""

    MESSAGE = (
        "scene has no geodetic anchor (coordinate_system.origin_lat_lon_alt); "
        "use cartesian x/y/z or import the scene via OSM"
    )

    def __init__(self, message: str = MESSAGE):
        super().__init__(message)


class DeviceIdCollisionError(ValueError):
    """mode='add' but a device with this id already exists (-> HTTP 409)."""

    def __init__(self, device_id: str):
        self.device_id = device_id
        super().__init__(
            f"device '{device_id}' already exists; use mode='upsert' to update "
            "it, or choose a different id"
        )


# ------------------------------------------------------------ WGS84 geodesy

# WGS84 ellipsoid: semi-major axis (m), flattening, first eccentricity squared.
_WGS84_A = 6378137.0
_WGS84_F = 1.0 / 298.257223563
_WGS84_E2 = _WGS84_F * (2.0 - _WGS84_F)


def _geodetic_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple[float, float, float]:
    """WGS84 geodetic (deg, deg, m) -> geocentric ECEF (m)."""
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    sin_lat = math.sin(lat)
    n = _WGS84_A / math.sqrt(1.0 - _WGS84_E2 * sin_lat * sin_lat)
    x = (n + alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - _WGS84_E2) + alt_m) * sin_lat
    return x, y, z


def geodetic_to_enu(
    lat_deg: float,
    lon_deg: float,
    alt_m: float,
    lat0_deg: float,
    lon0_deg: float,
    alt0_m: float,
) -> tuple[float, float, float]:
    """WGS84 geodetic point -> local ENU meters about the anchor (lat0, lon0).

    x = East, y = North, z = Up, matching the canonical Z-up ENU scene frame.
    """
    x, y, z = _geodetic_to_ecef(lat_deg, lon_deg, alt_m)
    x0, y0, z0 = _geodetic_to_ecef(lat0_deg, lon0_deg, alt0_m)
    dx, dy, dz = x - x0, y - y0, z - z0
    lat0 = math.radians(lat0_deg)
    lon0 = math.radians(lon0_deg)
    sin_lat, cos_lat = math.sin(lat0), math.cos(lat0)
    sin_lon, cos_lon = math.sin(lon0), math.cos(lon0)
    east = -sin_lon * dx + cos_lon * dy
    north = -sin_lat * cos_lon * dx - sin_lat * sin_lon * dy + cos_lat * dz
    up = cos_lat * cos_lon * dx + cos_lat * sin_lon * dy + sin_lat * dz
    return east, north, up


# ------------------------------------------------------------ point parsing

# Sentinel z fed into the terrain raycast when we only want the surface z back:
# on a hit snap_to_terrain returns surface + 0; on a miss it returns this value
# unchanged, so equality against it is an exact hit/miss test.
_SURFACE_PROBE_Z = -1.0e9


class _ParsedPoint:
    """A raw point normalized to horizontal coords + a vertical intent."""

    __slots__ = ("geographic", "h1", "h2", "z", "alt_m", "agl_m")

    def __init__(
        self,
        geographic: bool,
        h1: float,
        h2: float,
        z: Optional[float],
        alt_m: Optional[float],
        agl_m: Optional[float],
    ):
        self.geographic = geographic
        self.h1 = h1  # x (cartesian) or lat (geographic)
        self.h2 = h2  # y (cartesian) or lon (geographic)
        self.z = z
        self.alt_m = alt_m
        self.agl_m = agl_m


def _parse_point(raw: Any, label: str) -> _ParsedPoint:
    """Normalize one accepted point form; raise ValueError on a malformed one."""
    # Array form: [x, y] or [x, y, z] (always cartesian).
    if isinstance(raw, (list, tuple)):
        vals = [float(v) for v in raw]
        if len(vals) == 2:
            return _ParsedPoint(False, vals[0], vals[1], None, None, None)
        if len(vals) == 3:
            return _ParsedPoint(False, vals[0], vals[1], vals[2], None, None)
        raise ValueError(f"{label}: array point must have 2 or 3 numbers, got {len(vals)}")

    # Object form (a validated PointObject, or a plain dict for top-level fields).
    if isinstance(raw, PointObject):
        x, y, z = raw.x, raw.y, raw.z
        lat, lon, alt_m, agl_m = raw.lat, raw.lon, raw.alt_m, raw.agl_m
    elif isinstance(raw, dict):
        x, y, z = raw.get("x"), raw.get("y"), raw.get("z")
        lat, lon, alt_m, agl_m = raw.get("lat"), raw.get("lon"), raw.get("alt_m"), raw.get("agl_m")
    else:
        raise ValueError(f"{label}: unrecognized point form {type(raw).__name__}")

    has_geo = lat is not None or lon is not None
    has_cart = x is not None or y is not None
    if has_geo and has_cart:
        raise ValueError(
            f"{label}: point mixes cartesian (x/y) and geographic (lat/lon); "
            "use one coordinate system per point"
        )
    if has_geo:
        if lat is None or lon is None:
            raise ValueError(f"{label}: geographic point needs both 'lat' and 'lon'")
        return _ParsedPoint(True, float(lat), float(lon), None, alt_m, agl_m)
    if has_cart:
        if x is None or y is None:
            raise ValueError(f"{label}: cartesian point needs both 'x' and 'y'")
        return _ParsedPoint(False, float(x), float(y), z, None, agl_m)
    raise ValueError(f"{label}: point has neither cartesian (x/y) nor geographic (lat/lon) coordinates")


# ------------------------------------------------------------ resolution


def _surface_z(project_dir: Path, scene: Scene, x: float, y: float) -> Optional[float]:
    """Surface z under (x, y) via a terrain raycast, or None if there is no
    mesh / the point is off the mesh footprint. Terrain's own warnings are
    swallowed here; callers emit their own per-point messages."""
    out = snap_to_terrain(
        project_dir, scene, [[x, y, _SURFACE_PROBE_Z]], 0.0, [], fill_gaps=False
    )
    z = out[0][2]
    return None if z == _SURFACE_PROBE_Z else z


def _resolve_vertical(
    project_dir: Path,
    scene: Scene,
    x: float,
    y: float,
    *,
    agl_m: Optional[float],
    z_explicit: Optional[float],
    default_agl: Optional[float],
    label: str,
    warnings: list[str],
) -> float:
    """Resolve a point's z from its AGL / explicit-z / default intent."""
    if agl_m is not None:
        surface = _surface_z(project_dir, scene, x, y)
        if surface is None:
            warnings.append(
                f"{label} has no surface underneath it; kept its height "
                f"{agl_m:.2f} m as an absolute z"
            )
            return float(agl_m)
        return surface + float(agl_m)

    if z_explicit is not None:
        # Do not auto-fix an explicit z; just warn when it is buried.
        surface = _surface_z(project_dir, scene, x, y)
        if surface is not None and z_explicit < surface - 0.05:
            warnings.append(
                f"{label} sits {surface - z_explicit:.1f} m below the surface under it"
            )
        return float(z_explicit)

    # Neither AGL nor explicit z: fall back to the default AGL, else z = 0.
    if default_agl is not None:
        surface = _surface_z(project_dir, scene, x, y)
        return float(default_agl) if surface is None else surface + float(default_agl)
    return 0.0


def resolve_point(
    project_dir: Path,
    scene: Scene,
    raw: Any,
    *,
    default_agl: Optional[float],
    label: str,
    warnings: list[str],
) -> list[float]:
    """Resolve any accepted point form to a cartesian ``[x, y, z]`` (ENU meters).

    Raises :class:`GeoAnchorMissingError` for a geographic point when the scene
    has no geodetic anchor, and :class:`ValueError` for a malformed point.
    """
    p = _parse_point(raw, label)

    if p.geographic:
        anchor = scene.coordinate_system.origin_lat_lon_alt
        if anchor is None:
            raise GeoAnchorMissingError()
        lat0, lon0, alt0 = float(anchor[0]), float(anchor[1]), float(anchor[2])
        # Horizontal from full geodetic->ENU (evaluated at the anchor altitude so
        # the tiny alt->east/north coupling drops out); vertical is handled below.
        east, north, _ = geodetic_to_enu(p.h1, p.h2, alt0, lat0, lon0, alt0)
        x, y = east, north
        # Absolute geographic height maps to ENU up as alt_m - origin_alt.
        z_explicit = None if p.alt_m is None else float(p.alt_m) - alt0
    else:
        x, y = p.h1, p.h2
        z_explicit = p.z

    agl_m = p.agl_m
    if agl_m is not None and z_explicit is not None:
        warnings.append(
            f"{label} gives both an AGL height and an absolute z/alt; AGL wins"
        )
        z_explicit = None

    z = _resolve_vertical(
        project_dir,
        scene,
        x,
        y,
        agl_m=agl_m,
        z_explicit=z_explicit,
        default_agl=default_agl,
        label=label,
        warnings=warnings,
    )
    return [float(x), float(y), float(z)]


# ------------------------------------------------------------ device import

# Device fields copied through verbatim when provided on an ImportDevice.
_DEVICE_EXTRA_FIELDS = ("name", "orientation_deg", "velocity_m_s", "power_dbm", "antenna", "color")


def _next_device_id(kind: str, taken: set[str]) -> str:
    """Next free ``tx_00N`` / ``rx_00N`` id for ``kind`` not in ``taken``."""
    n = 1
    while f"{kind}_{n:03d}" in taken:
        n += 1
    return f"{kind}_{n:03d}"


def _device_raw_point(imp: ImportDevice, label: str) -> Any:
    """The point form for a device: its ``position``, else its top-level
    coordinate fields, else an error."""
    if imp.position is not None:
        return imp.position
    top = {
        k: v
        for k, v in (
            ("x", imp.x), ("y", imp.y), ("z", imp.z),
            ("lat", imp.lat), ("lon", imp.lon), ("alt_m", imp.alt_m), ("agl_m", imp.agl_m),
        )
        if v is not None
    }
    if not top:
        raise ValueError(
            f"{label} has no position (give 'position', or x/y[/z], or lat/lon[/alt_m|agl_m])"
        )
    return top


def import_devices(
    project_dir: Path,
    scene: Scene,
    request: DeviceImportRequest,
) -> tuple[list[str], list[str], list[str]]:
    """Apply a device import to ``scene`` in place.

    Returns ``(added_ids, updated_ids, warnings)``. Raises
    :class:`DeviceIdCollisionError` (mode='add' id clash -> 409),
    :class:`GeoAnchorMissingError` (geographic point, no anchor -> 400), or
    :class:`ValueError` (malformed device -> 400).
    """
    added_ids: list[str] = []
    updated_ids: list[str] = []
    warnings: list[str] = []
    taken: set[str] = {d.id for d in scene.devices}

    for i, imp in enumerate(request.devices):
        label0 = f"device '{imp.id}'" if imp.id else f"device #{i + 1}"

        # Validate kind up front (actionable 400 rather than a schema 422).
        if imp.kind is not None and imp.kind not in ("tx", "rx"):
            raise ValueError(
                f"{label0}: kind must be 'tx' or 'rx', got {imp.kind!r}"
            )

        existing = scene.device_by_id(imp.id) if imp.id else None
        if imp.id and existing is not None and request.mode == "add":
            raise DeviceIdCollisionError(imp.id)

        # Resolve the id (auto-generate when omitted) and the effective kind.
        if imp.id:
            did = imp.id
        else:
            gen_kind = imp.kind or "rx"
            did = _next_device_id(gen_kind, taken)
        kind = imp.kind or (existing.kind if existing is not None else "rx")
        label = f"device '{did}'"

        raw_point = _device_raw_point(imp, label)
        position = resolve_point(
            project_dir, scene, raw_point, default_agl=None, label=label, warnings=warnings
        )

        extras = {
            f: getattr(imp, f) for f in _DEVICE_EXTRA_FIELDS if getattr(imp, f) is not None
        }

        if existing is not None:
            # Upsert: update the moved fields in place, keep everything else.
            existing.kind = kind
            existing.position = position
            for field, value in extras.items():
                setattr(existing, field, value)
            updated_ids.append(did)
            warnings.append(
                f"device '{did}' already existed; updated its position/parameters in place"
            )
        else:
            # Add: construct a fresh Device (validates id pattern, kind, etc.).
            try:
                device = Device(id=did, kind=kind, position=position, **extras)
            except ValueError as exc:
                raise ValueError(f"{label}: {exc}") from exc
            scene.devices.append(device)
            added_ids.append(did)
        taken.add(did)

    return added_ids, updated_ids, warnings


# ------------------------------------------------------- trajectory import


def resolve_waypoints(
    project_dir: Path,
    scene: Scene,
    points: list[Any],
    *,
    default_agl: Optional[float],
    ue_id: Optional[str],
) -> tuple[list[list[float]], list[str]]:
    """Resolve trajectory points to cartesian ``[[x, y, z], ...]`` waypoints.

    Returns ``(waypoints, warnings)``. Raises the same errors as
    :func:`resolve_point`.
    """
    warnings: list[str] = []
    waypoints: list[list[float]] = []
    who = f" of trajectory '{ue_id}'" if ue_id else ""
    for i, raw in enumerate(points):
        label = f"waypoint {i + 1}{who}"
        waypoints.append(
            resolve_point(
                project_dir, scene, raw, default_agl=default_agl, label=label, warnings=warnings
            )
        )
    return waypoints, warnings


# --------------------------------------------------------------- templates

# Served verbatim by GET /import/templates so the frontend can offer a
# downloadable, self-describing starter file. Kept here (not in the route) so
# the shapes are unit-testable and stay next to the resolution logic.
IMPORT_TEMPLATES: dict[str, Any] = {
    "combined_file_example": {
        "devices": [
            {
                "id": "ue_01",
                "kind": "rx",
                "position": [12.0, -4.0, 1.5],
                "orientation_deg": [90, 0, 0],
                "power_dbm": 23.0,
                "name": "car UE",
            },
            {"id": "ue_02", "lat": 37.5563, "lon": 127.0448, "alt_m": 45.2},
            {"id": "ue_03", "lat": 37.5561, "lon": 127.0451, "agl_m": 1.5},
        ],
        "trajectories": [
            {
                "ue_id": "ue_01",
                "points": [
                    {"x": 0, "y": 0, "agl_m": 1.5},
                    {"lat": 37.5560, "lon": 127.0450, "agl_m": 1.5},
                    [30.0, 5.0, 1.5],
                ],
            }
        ],
    },
    "devices_endpoint_example": {
        "mode": "upsert",
        "devices": [
            {"id": "tx_001", "kind": "tx", "position": [0.0, 0.0, 10.0], "power_dbm": 30.0},
            {"kind": "rx", "x": 12.0, "y": -4.0, "agl_m": 1.5, "name": "car UE"},
            {"id": "ue_geo", "lat": 37.5563, "lon": 127.0448, "alt_m": 45.2},
        ],
    },
    "trajectory_endpoint_example": {
        "ue_id": "ue_01",
        "agl_m": 1.5,
        "points": [
            {"x": 0, "y": 0, "agl_m": 1.5},
            {"lat": 37.5560, "lon": 127.0450, "agl_m": 1.5},
            [30.0, 5.0, 1.5],
        ],
    },
    "field_reference": {
        "coordinate_systems": (
            "Cartesian points are local ENU meters, Z-up (x=East, y=North, "
            "z=Up), the canonical scene frame. Geographic points are WGS84 and "
            "REQUIRE the scene's geodetic anchor "
            "(coordinate_system.origin_lat_lon_alt), set by the OSM import; "
            "they are converted geodetic->ECEF->ENU about that anchor."
        ),
        "point_forms": (
            "A point is [x, y], [x, y, z], {x, y, z?}, {x, y, agl_m?}, "
            "{lat, lon, alt_m?} or {lat, lon, agl_m?}. lat+lon selects the "
            "geographic reading; forms may be mixed within one file."
        ),
        "agl_m": (
            "Height above the scene surface in meters: z = (terrain surface "
            "under the point) + agl_m, via a downward raycast onto the visual "
            "mesh. If nothing is under the point, agl_m is kept as an absolute "
            "z and a warning is emitted. agl_m and z/alt_m are mutually "
            "exclusive; if both are given AGL wins (with a warning)."
        ),
        "alt_m": (
            "Absolute geographic altitude in meters; maps to ENU up as "
            "alt_m - origin_alt. Ignored entirely when agl_m is also present."
        ),
        "z": "Absolute cartesian up in meters. Never auto-corrected; a z below "
        "the surface under it only produces a warning.",
        "devices[].id": "Optional; auto-generated tx_00N / rx_00N when omitted.",
        "devices[].kind": "Optional 'tx' or 'rx'; defaults to 'rx'.",
        "devices[].position": "Optional point (any form); alternatively give "
        "x/y/z or lat/lon/alt_m/agl_m at the top level of the device.",
        "devices[].orientation_deg": "Optional [yaw, pitch, roll] degrees (ENU).",
        "devices[].power_dbm": "Optional transmit power (dBm); ignored for rx.",
        "devices[].velocity_m_s": "Optional [vx, vy, vz] m/s for Doppler.",
        "devices[].name": "Optional display name.",
        "mode": "'upsert' (default) updates a device with a matching id in "
        "place; 'add' returns 409 on an id collision.",
        "trajectories[].ue_id": "Optional UE id the waypoints belong to; "
        "echoed back, not resolved against the scene.",
        "trajectories[].points": "List of points (any form) for the trajectory.",
    },
}
