"""One-shot OpenStreetMap import.

Given a geographic center (lat, lon) and a rectangle size, fetch the building
footprints from the Overpass API, extrude them to 3D, drop a ground plane
under them, and assemble a ready-to-simulate SEAM project:

- canonical scene (Z-up ENU meters, snake_case) with one prim per building
  plus a ground prim, every prim carrying a default RF material binding;
- a single visual ``visual/scene.glb`` whose named geometries match each
  prim's ``mesh_ref.mesh_name`` (``ground``, ``building_000`` ...);
- provenance recording the import parameters.

Geodesy is a local equirectangular (tangent-plane) projection around the
center: accurate to well under a metre for the <=3 km rectangles this importer
supports, and it keeps the canonical scene in the same local-ENU meters every
other part of SEAM assumes.

Network access is confined to :func:`fetch_overpass`; the pure geometry and
assembly helpers take already-fetched Overpass JSON so they can be unit-tested
without a network (the route and tests monkeypatch ``fetch_overpass``).
"""

import math
import os
from typing import Any, Optional

import trimesh

from seam_studio.core.config import APP_VERSION
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.scene import (
    CoordinateSystem,
    MeshRef,
    Prim,
    RFBinding,
    Scene,
    SceneAssets,
    VisualBinding,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.project_store import ProjectStore

# ---------------------------------------------------------------- constants

# Overpass endpoint, overridable via env. Follows core/config.py's _env pattern
# (SEAM_* preferred, SIONNATWIN_* fallback) but reads os.environ directly so we
# never edit config.py for this feature.
_DEFAULT_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_URL = (
    os.environ.get("SEAM_OVERPASS_URL")
    or os.environ.get("SIONNATWIN_OVERPASS_URL")
    or _DEFAULT_OVERPASS_URL
)

# Meters per degree of latitude / longitude (at the equator), from the WGS-84
# mean values. Longitude scales by cos(lat). Good to <1% for any lat, and the
# error over a <=3 km tangent-plane rectangle is sub-metre.
_M_PER_DEG_LAT = 110540.0
_M_PER_DEG_LON_EQUATOR = 111320.0

# Default building height (m) when a footprint carries no height/levels tag.
_DEFAULT_BUILDING_HEIGHT_M = 10.0
# Meters per building level (typical storey height) for building:levels.
_M_PER_LEVEL = 3.0
# Ground plane thickness (m) and horizontal margin (m) beyond the bbox.
_GROUND_THICKNESS_M = 0.2
_GROUND_MARGIN_M = 5.0
# Hard cap on buildings kept (largest-area first); protects the solver and GLB.
_MAX_BUILDINGS = 2000
# Overpass request budget (s).
_OVERPASS_TIMEOUT_S = 30.0
# Rectangle size bounds (m), per the API contract.
_MIN_SIZE_M = 50.0
_MAX_SIZE_M = 3000.0


class OverpassError(RuntimeError):
    """Overpass was unreachable or returned unusable data (-> HTTP 502)."""


class OverpassTimeout(OverpassError):
    """Overpass did not answer within the timeout (-> HTTP 504)."""


# ------------------------------------------------------------------ geodesy


def meters_per_degree(lat_deg: float) -> tuple[float, float]:
    """Return (m per deg longitude, m per deg latitude) at ``lat_deg``.

    Longitude scales with cos(lat); latitude is treated as constant (the small
    WGS-84 variation is negligible for a local tangent plane).
    """
    m_per_deg_lon = _M_PER_DEG_LON_EQUATOR * math.cos(math.radians(lat_deg))
    return m_per_deg_lon, _M_PER_DEG_LAT


def lonlat_to_enu(
    lon_pt: float, lat_pt: float, lon0: float, lat0: float
) -> tuple[float, float]:
    """Project a lon/lat point to local ENU meters about (lon0, lat0).

    x is East, y is North (Z-up ENU, matching the canonical scene).
    """
    m_per_deg_lon, m_per_deg_lat = meters_per_degree(lat0)
    x_east = (lon_pt - lon0) * m_per_deg_lon
    y_north = (lat_pt - lat0) * m_per_deg_lat
    return x_east, y_north


def bbox_for(
    lat: float, lon: float, width_m: float, height_m: float
) -> tuple[float, float, float, float]:
    """Geographic bbox (south, west, north, east) for a W x H rectangle
    centered on (lat, lon). ``width_m`` is the E-W span, ``height_m`` the N-S.
    """
    m_per_deg_lon, m_per_deg_lat = meters_per_degree(lat)
    dlat = (height_m / 2.0) / m_per_deg_lat
    dlon = (width_m / 2.0) / m_per_deg_lon
    return (lat - dlat, lon - dlon, lat + dlat, lon + dlon)


# ---------------------------------------------------------------- overpass


def build_query(bbox: tuple[float, float, float, float]) -> str:
    """Overpass QL fetching every building way+relation in ``bbox`` with
    geometry inlined (``out geom``)."""
    south, west, north, east = bbox
    b = f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"
    return (
        "[out:json][timeout:25];"
        "("
        f'way["building"]({b});'
        f'relation["building"]({b});'
        ");"
        "out geom;"
    )


def fetch_overpass(
    bbox: tuple[float, float, float, float],
    *,
    url: Optional[str] = None,
    timeout_s: float = _OVERPASS_TIMEOUT_S,
) -> dict[str, Any]:
    """POST the Overpass query and return the parsed JSON.

    Raises :class:`OverpassTimeout` on timeout and :class:`OverpassError` when
    the endpoint is unreachable or the response is not usable JSON with an
    ``elements`` list.
    """
    import httpx  # lazy: keeps the module import free of a hard network dep

    endpoint = url or OVERPASS_URL
    query = build_query(bbox)
    # Overpass mirrors reject anonymous library user agents with 406 (usage
    # policy asks for an identifying UA); verified live against overpass-api.de.
    headers = {
        "User-Agent": "SEAM-Studio/0.1 (local research tool; OSM scene import)",
        "Accept": "application/json",
    }
    try:
        resp = httpx.post(
            endpoint, data={"data": query}, headers=headers, timeout=timeout_s
        )
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise OverpassTimeout(
            "the OpenStreetMap Overpass API timed out; try again in a moment "
            "or reduce the area"
        ) from exc
    except httpx.HTTPError as exc:
        raise OverpassError(
            "could not reach the OpenStreetMap Overpass API; check your "
            "internet connection and retry"
        ) from exc
    try:
        data = resp.json()
    except ValueError as exc:
        raise OverpassError(
            "the OpenStreetMap Overpass API returned an unreadable response; "
            "check your internet connection and retry"
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get("elements"), list):
        raise OverpassError(
            "the OpenStreetMap Overpass API returned unexpected data; check "
            "your internet connection and retry"
        )
    return data


# ---------------------------------------------------------------- geometry


def _height_from_tags(tags: dict[str, Any], default_height_m: float) -> float:
    """Resolve a building height (m) from OSM tags.

    ``height`` (metres, possibly with a trailing unit) wins; else
    ``building:levels`` * storey height; else the default.
    """
    raw_h = tags.get("height")
    if raw_h is not None:
        try:
            # "12", "12 m", "12m", "12.5 meters" -> 12.0
            token = str(raw_h).strip().lower().split()[0].replace("m", "")
            h = float(token)
            if h > 0:
                return h
        except (ValueError, IndexError):
            pass
    raw_levels = tags.get("building:levels")
    if raw_levels is not None:
        try:
            levels = float(str(raw_levels).strip().split()[0])
            if levels > 0:
                return levels * _M_PER_LEVEL
        except (ValueError, IndexError):
            pass
    return default_height_m


def _footprint_enu(
    element: dict[str, Any], lon0: float, lat0: float
) -> Optional[list[tuple[float, float]]]:
    """Extract a way's outer footprint as ENU (x, y) points, or None if the
    element has no usable geometry."""
    geom = element.get("geometry")
    if not isinstance(geom, list) or len(geom) < 3:
        return None
    pts: list[tuple[float, float]] = []
    for node in geom:
        try:
            lon = float(node["lon"])
            lat = float(node["lat"])
        except (KeyError, TypeError, ValueError):
            continue
        pts.append(lonlat_to_enu(lon, lat, lon0, lat0))
    # Drop a duplicated closing vertex (OSM ways repeat the first node).
    if len(pts) >= 2 and pts[0] == pts[-1]:
        pts = pts[:-1]
    if len(pts) < 3:
        return None
    return pts


def build_meshes(
    overpass_json: dict[str, Any],
    lat: float,
    lon: float,
    width_m: float,
    height_m: float,
    *,
    default_building_height_m: float = _DEFAULT_BUILDING_HEIGHT_M,
) -> tuple[trimesh.Scene, list[dict[str, Any]], int, list[str]]:
    """Turn Overpass JSON into a named visual trimesh.Scene + per-building
    metadata.

    Returns ``(tm_scene, buildings, num_skipped, warnings)`` where each entry
    in ``buildings`` is ``{"mesh_name", "osm_id", "height_m"}`` in the same
    order as the prims should be created (ground is added to ``tm_scene`` but
    is not part of ``buildings``).
    """
    from shapely.geometry import Polygon
    from trimesh.creation import extrude_polygon

    warnings: list[str] = []
    skipped = 0

    # First pass: build valid footprints with their extrusion height + area,
    # so we can apply the largest-area cap deterministically before extruding.
    candidates: list[dict[str, Any]] = []
    for element in overpass_json.get("elements", []):
        if element.get("type") not in ("way", "relation"):
            continue
        pts = _footprint_enu(element, lon, lat)
        if pts is None:
            skipped += 1
            continue
        polygon = Polygon(pts)
        if (not polygon.is_valid) or polygon.is_empty or polygon.area <= 0.0:
            skipped += 1
            continue
        tags = element.get("tags") or {}
        h = _height_from_tags(tags, default_building_height_m)
        candidates.append(
            {
                "polygon": polygon,
                "area": float(polygon.area),
                "height_m": h,
                "osm_id": element.get("id"),
            }
        )

    # Cap: keep the largest-area buildings when over the limit.
    if len(candidates) > _MAX_BUILDINGS:
        candidates.sort(key=lambda c: c["area"], reverse=True)
        dropped = len(candidates) - _MAX_BUILDINGS
        candidates = candidates[:_MAX_BUILDINGS]
        warnings.append(
            f"scene has more than {_MAX_BUILDINGS} buildings; kept the "
            f"{_MAX_BUILDINGS} largest by footprint area and dropped {dropped}."
        )

    tm_scene = trimesh.Scene()
    buildings: list[dict[str, Any]] = []
    for i, cand in enumerate(candidates):
        mesh_name = f"building_{i:03d}"
        try:
            mesh = extrude_polygon(cand["polygon"], height=cand["height_m"])
        except Exception:  # degenerate polygon that survived is_valid
            skipped += 1
            continue
        if mesh is None or len(mesh.faces) == 0:
            skipped += 1
            continue
        mesh.visual = trimesh.visual.ColorVisuals(
            mesh=mesh, face_colors=[200, 200, 205, 255]
        )
        tm_scene.add_geometry(mesh, geom_name=mesh_name, node_name=mesh_name)
        buildings.append(
            {
                "mesh_name": mesh_name,
                "osm_id": cand["osm_id"],
                "height_m": cand["height_m"],
            }
        )

    # Ground plane: a thin box covering the bbox (plus a margin) with its top
    # face at z = 0 so buildings sit on it.
    ground = _ground_mesh(width_m, height_m)
    tm_scene.add_geometry(ground, geom_name="ground", node_name="ground")

    return tm_scene, buildings, skipped, warnings


def _ground_mesh(width_m: float, height_m: float) -> trimesh.Trimesh:
    """Thin ground box spanning the rectangle (+ margin), top face at z = 0."""
    ext_x = width_m + 2 * _GROUND_MARGIN_M
    ext_y = height_m + 2 * _GROUND_MARGIN_M
    ground = trimesh.creation.box(extents=[ext_x, ext_y, _GROUND_THICKNESS_M])
    # box() is centered at the origin; drop it so its top is at z = 0.
    ground.apply_translation([0.0, 0.0, -_GROUND_THICKNESS_M / 2.0])
    ground.visual = trimesh.visual.ColorVisuals(
        mesh=ground, face_colors=[120, 130, 120, 255]
    )
    return ground


# ------------------------------------------------------------ project build


def import_osm_project(
    store: ProjectStore,
    library: RFMaterialLibrary,
    *,
    name: str,
    lat: float,
    lon: float,
    width_m: float = 500.0,
    height_m: float = 500.0,
    project_id: Optional[str] = None,
    default_building_material: str = "itu_concrete",
    ground_material: str = "ground_28ghz",
    default_building_height_m: float = _DEFAULT_BUILDING_HEIGHT_M,
    overpass_json: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Fetch OSM buildings and assemble a ready-to-simulate project.

    Validates arguments and material ids (raising :class:`ValueError` for the
    400 cases), fetches the Overpass data (unless ``overpass_json`` is supplied,
    which the tests use to avoid the network), builds the meshes, creates the
    project folder, and writes the scene / GLB / provenance.

    Returns ``{"project_id", "num_buildings", "num_skipped", "warnings"}``.
    Network failures propagate as :class:`OverpassError` / :class:`OverpassTimeout`.
    """
    # --- argument validation (400) -------------------------------------
    if not (-90.0 <= lat <= 90.0):
        raise ValueError(f"lat {lat} is out of range (-90..90)")
    if not (-180.0 <= lon <= 180.0):
        raise ValueError(f"lon {lon} is out of range (-180..180)")
    for label, val in (("width_m", width_m), ("height_m", height_m)):
        if not (_MIN_SIZE_M <= val <= _MAX_SIZE_M):
            raise ValueError(
                f"{label} {val} is out of range "
                f"({_MIN_SIZE_M:.0f}..{_MAX_SIZE_M:.0f} m)"
            )
    if default_building_height_m <= 0:
        raise ValueError("default_building_height_m must be > 0")

    # --- material validation against the library (400) -----------------
    for label, mat_id in (
        ("default_building_material", default_building_material),
        ("ground_material", ground_material),
    ):
        if library.get(mat_id) is None:
            raise ValueError(
                f"unknown {label} {mat_id!r}: not in the RF material library"
            )

    # --- fetch (unless a canned response was provided) -----------------
    if overpass_json is None:
        bbox = bbox_for(lat, lon, width_m, height_m)
        overpass_json = fetch_overpass(bbox)

    # --- geometry ------------------------------------------------------
    tm_scene, buildings, num_skipped, warnings = build_meshes(
        overpass_json,
        lat,
        lon,
        width_m,
        height_m,
        default_building_height_m=default_building_height_m,
    )

    # --- canonical scene ----------------------------------------------
    prims: list[Prim] = []
    for b in buildings:
        prims.append(
            Prim(
                id=f"/buildings/{b['mesh_name']}",
                name=b["mesh_name"],
                type="mesh_primitive",
                semantic_tags=["building"],
                mesh_ref=MeshRef(
                    asset_uri="visual/scene.glb", mesh_name=b["mesh_name"]
                ),
                visual=VisualBinding(base_color_rgba=[0.78, 0.78, 0.80, 1.0]),
                rf=RFBinding(
                    material_id=default_building_material,
                    assignment_status="rule_suggested",
                    assignment_sources=["osm_import"],
                ),
            )
        )
    prims.append(
        Prim(
            id="/ground",
            name="ground",
            type="mesh_primitive",
            semantic_tags=["ground", "terrain"],
            mesh_ref=MeshRef(asset_uri="visual/scene.glb", mesh_name="ground"),
            visual=VisualBinding(base_color_rgba=[0.47, 0.51, 0.47, 1.0]),
            rf=RFBinding(
                material_id=ground_material,
                assignment_status="rule_suggested",
                assignment_sources=["osm_import"],
            ),
        )
    )

    scene = Scene(
        scene_id=project_id or "",  # replaced below with the real id
        name=name or "OSM Import",
        environment="outdoor",
        coordinate_system=CoordinateSystem(origin_lat_lon_alt=[lat, lon, 0.0]),
        assets=SceneAssets(visual_scene_uri="visual/scene.glb"),
        prims=prims,
        simulation_configs=[SimulationConfig()],
    )

    # --- materialize the project --------------------------------------
    # create_project derives the id from name when project_id is None and raises
    # ValueError if the id already exists (both -> 400 at the route).
    info = store.create_project(name=name or "OSM Import", project_id=project_id)
    pid = info.project_id
    scene.scene_id = pid

    project_dir = store.resolve(pid)
    (project_dir / "visual").mkdir(parents=True, exist_ok=True)
    (project_dir / "visual" / "scene.glb").write_bytes(
        tm_scene.export(file_type="glb")
    )
    store.save_scene(pid, scene)
    store.append_provenance(
        pid,
        {
            "type": "import_osm",
            "created_by": f"seam-studio/{APP_VERSION} (osm_import)",
            "lat": lat,
            "lon": lon,
            "width_m": width_m,
            "height_m": height_m,
            "num_buildings": len(buildings),
            "num_skipped": num_skipped,
        },
    )

    return {
        "project_id": pid,
        "num_buildings": len(buildings),
        "num_skipped": num_skipped,
        "warnings": warnings,
    }
