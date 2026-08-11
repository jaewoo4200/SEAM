"""One-shot OpenStreetMap import.

Given a geographic center (lat, lon) and a rectangle size, fetch the building
footprints from the Overpass API, extrude them to 3D, drop a ground plane
under them, and assemble a ready-to-simulate SEAM project:

- canonical scene (Z-up ENU meters, snake_case) with one prim per building
  plus a ground prim, every prim carrying a default RF material binding;
- a single visual ``visual/scene.glb`` whose named geometries match each
  prim's ``mesh_ref.mesh_name`` (``ground``, ``building_000`` ...);
- a FROZEN copy of the Overpass payload (``osm/overpass.json``) plus its
  SHA-256 sidecar (``osm/overpass.meta.json``);
- provenance recording the import parameters and the payload digest.

OpenStreetMap is a live, mutable third-party source, so a scene imported today
cannot be rebuilt from the same query a year from now. The freezer closes that
hole: every import stores the exact payload it consumed inside the project
folder, and later imports of the same bbox AND endpoint reuse that verified
payload instead of hitting the network (see :func:`find_frozen_payload`).
Overpass reports server-side truncation as HTTP 200 with a ``remark`` in the
body, so such payloads are rejected at fetch time and never reused.

Geodesy is a local equirectangular (tangent-plane) projection around the
center: accurate to well under a metre for the <=3 km rectangles this importer
supports, and it keeps the canonical scene in the same local-ENU meters every
other part of SEAM assumes.

Network access is confined to :func:`fetch_overpass`; the pure geometry and
assembly helpers take already-fetched Overpass JSON so they can be unit-tested
without a network (the route and tests monkeypatch ``fetch_overpass``).
"""

import hashlib
import json
import logging
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, NamedTuple, Optional

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

# Frozen Overpass payload + its metadata sidecar, project-relative. These live
# INSIDE the project folder (beside visual/scene.glb and rf/generated_scene.xml)
# so the payload travels with the project: zip it, publish it, and the OSM
# snapshot the results were derived from goes with it.
OVERPASS_PAYLOAD_REL = "osm/overpass.json"
OVERPASS_META_REL = "osm/overpass.meta.json"

_logger = logging.getLogger(__name__)


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


def payload_remark(payload: dict[str, Any]) -> str:
    """The payload's ``remark`` as a stripped string, or "" if it carries none.

    Overpass answers a server-side timeout / maxsize abort with HTTP 200 and a
    WELL-FORMED but truncated body whose only distress signal is this field.
    """
    remark = payload.get("remark")
    if isinstance(remark, str) and remark.strip():
        return remark.strip()
    return ""


def fetch_overpass(
    bbox: tuple[float, float, float, float],
    *,
    url: Optional[str] = None,
    timeout_s: float = _OVERPASS_TIMEOUT_S,
) -> dict[str, Any]:
    """POST the Overpass query and return the parsed JSON.

    Raises :class:`OverpassTimeout` on timeout and :class:`OverpassError` when
    the endpoint is unreachable, the response is not usable JSON with an
    ``elements`` list, or the body carries a failure ``remark`` (a 200-with-a-
    truncated-payload; see :func:`payload_remark`).
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
    # A remark naming an error / timeout means the server gave up mid-query: the
    # elements list is a PARTIAL area, which would silently freeze as if it were
    # the whole bbox. Treat it as the failure it is (-> 502).
    remark = payload_remark(data)
    if "error" in remark.lower() or "timed out" in remark.lower():
        raise OverpassError(
            "the OpenStreetMap Overpass API aborted the query and returned "
            f"partial data ({remark}); try again in a moment or reduce the area"
        )
    return data


# -------------------------------------------------------- payload freezer


def _utcnow() -> str:
    """UTC timestamp, ISO-8601 with offset (matches project_store events)."""
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(payload: dict[str, Any]) -> str:
    """The exact text we hash AND write to ``osm/overpass.json``.

    Sorted keys / compact separators, so the digest identifies the payload's
    CONTENT rather than the whitespace and key order of whichever Overpass
    mirror served it. ``json.dumps`` escapes newlines inside strings, so the
    result never contains a literal newline - the text we hash is byte-identical
    to what lands on disk on every platform (no Windows CRLF translation).
    """
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )


def payload_sha256(payload: dict[str, Any]) -> str:
    """Bare lowercase hex SHA-256 of ``payload``'s canonical serialization.

    Unprefixed on purpose (the codebase's other hashes carry a ``sha256:``
    tag): ``sha256sum osm/overpass.json`` must verify a published project
    without anyone having to strip a prefix first.
    """
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def bbox_key(bbox: tuple[float, float, float, float]) -> str:
    """Identity of a fetch region: the same 7-decimal string ``build_query``
    embeds, so two imports match iff they would send the identical query."""
    south, west, north, east = bbox
    return f"{south:.7f},{west:.7f},{north:.7f},{east:.7f}"


class FrozenOverpass(NamedTuple):
    """A resolved Overpass payload plus where it came from.

    ``source`` is one of:

    ``overpass``  fetched live from ``endpoint`` at ``fetched_at``;
    ``frozen``    reused from another project's verified freeze (``fetched_at``
                  and ``endpoint`` are carried forward from the original fetch,
                  which is the reproducibility-meaningful pair);
    ``injected``  supplied by the caller (tests, scripted imports). ``bbox`` is
                  None because we cannot vouch that canned data corresponds to
                  any particular region, which also keeps it OUT of the reuse
                  index - only payloads we actually fetched are offered back.
    """

    payload: dict[str, Any]
    sha256: str
    source: str
    fetched_at: Optional[str] = None
    endpoint: Optional[str] = None
    bbox: Optional[tuple[float, float, float, float]] = None


def freeze_payload(
    store: ProjectStore, project_id: str, frozen: FrozenOverpass
) -> None:
    """Write the payload + its sidecar into the project (atomic, via the store).

    Called for EVERY import, so an OSM project is always self-describing: you
    can rebuild its geometry from ``osm/overpass.json`` with no network at all.
    """
    store.save_text(project_id, OVERPASS_PAYLOAD_REL, _canonical_json(frozen.payload))
    store.save_json(
        project_id,
        OVERPASS_META_REL,
        {
            "sha256": frozen.sha256,
            "source": frozen.source,
            "fetched_at": frozen.fetched_at,
            "endpoint": frozen.endpoint,
            "bbox": list(frozen.bbox) if frozen.bbox is not None else None,
            "bbox_key": bbox_key(frozen.bbox) if frozen.bbox is not None else None,
            "payload_uri": OVERPASS_PAYLOAD_REL,
            "frozen_by": f"seam-studio/{APP_VERSION} (osm_import)",
        },
    )


def load_frozen_payload(project_dir: Path) -> Optional[FrozenOverpass]:
    """Read and VERIFY the frozen payload in ``project_dir``, or return None.

    Never raises. Returns None when the project has no freeze, when either file
    is missing / unreadable / not the JSON we expect, or when the payload's
    recomputed digest disagrees with the sidecar. A truncated or hand-edited
    freeze must never be passed off as the payload whose hash we published, so
    the sane fallback is to ignore it and let the caller fetch fresh data.
    """
    meta_file = project_dir / OVERPASS_META_REL
    payload_file = project_dir / OVERPASS_PAYLOAD_REL
    try:
        meta = json.loads(meta_file.read_text(encoding="utf-8"))
        raw = payload_file.read_bytes()
    except (OSError, ValueError):
        return None
    if not isinstance(meta, dict):
        return None
    recorded = meta.get("sha256")
    actual = hashlib.sha256(raw).hexdigest()
    if not isinstance(recorded, str) or actual != recorded:
        _logger.warning(
            "ignoring frozen Overpass payload in %s: sha256 mismatch "
            "(recorded %s, actual %s)",
            project_dir,
            recorded,
            actual,
        )
        return None
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    if not isinstance(payload, dict) or not isinstance(payload.get("elements"), list):
        return None
    bbox = meta.get("bbox")
    if isinstance(bbox, list) and len(bbox) == 4:
        try:
            bbox_t = tuple(float(v) for v in bbox)  # type: ignore[assignment]
        except (TypeError, ValueError):
            bbox_t = None
    else:
        bbox_t = None
    # Sidecars are hand-editable JSON: coerce anything that is not a string to
    # None rather than letting it out into code that expects str-or-None.
    fetched_at = meta.get("fetched_at")
    endpoint = meta.get("endpoint")
    return FrozenOverpass(
        payload=payload,
        sha256=recorded,
        source="frozen",
        fetched_at=fetched_at if isinstance(fetched_at, str) else None,
        endpoint=endpoint if isinstance(endpoint, str) else None,
        bbox=bbox_t,
    )


def _read_freeze_sidecar(project_dir: Path) -> Optional[dict[str, Any]]:
    """Parse ONLY ``osm/overpass.meta.json`` (small), or None. Never raises.

    The lookup scans sidecars first so a store full of multi-MB payloads costs
    one tiny read per project instead of a full parse per project.
    """
    try:
        meta = json.loads(
            (project_dir / OVERPASS_META_REL).read_text(encoding="utf-8")
        )
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _frozen_payload_dirs(store: ProjectStore) -> list[Path]:
    """Project folders under the store's roots that carry a freeze sidecar.

    Globbed straight off ``store.roots`` rather than walking
    ``list_projects()``: that would parse every project's (multi-MB) scene file
    just to find a cache entry. A root may itself be a single project folder,
    so both shapes are probed. Sorted for a deterministic scan order.
    """
    found: list[Path] = []
    for root in store.roots:
        root = Path(root)
        if not root.is_dir():
            continue
        if (root / OVERPASS_META_REL).is_file():
            found.append(root)
        found.extend(
            sorted(p.parent.parent for p in root.glob(f"*/{OVERPASS_META_REL}"))
        )
    return found


def find_frozen_payload(
    store: ProjectStore,
    bbox: tuple[float, float, float, float],
    *,
    endpoint: Optional[str] = None,
) -> Optional[FrozenOverpass]:
    """Newest verified freeze for ``bbox`` among the store's projects, else None.

    Identity is (bbox, endpoint): ``OVERPASS_URL`` is env-overridable, and a
    payload from a different mirror or a local extract is a different data
    source, not a cache hit. ``endpoint`` defaults to the current
    ``OVERPASS_URL``; carried-forward copies keep the ORIGINAL endpoint in their
    sidecar, so an unchanged environment keeps chaining.

    "Newest" = latest ``fetched_at`` (ties broken by folder name for
    determinism), so an explicit ``refresh=True`` re-fetch becomes the payload
    that later imports of the same region pick up.

    Candidates are shortlisted from the cheap sidecars alone and only then
    verified, newest first, one payload at a time - a store with many freezes
    never parses more than the one it returns. A candidate that fails
    verification (or carries a ``remark``, i.e. was a truncated Overpass answer
    frozen before the fetch-time gate existed) falls through to the next.
    """
    want = bbox_key(bbox)
    want_endpoint = endpoint if endpoint is not None else OVERPASS_URL
    candidates: list[tuple[str, str, Path]] = []
    for project_dir in _frozen_payload_dirs(store):
        meta = _read_freeze_sidecar(project_dir)
        if meta is None:
            continue
        if meta.get("bbox_key") != want or meta.get("endpoint") != want_endpoint:
            continue
        fetched_at = meta.get("fetched_at")
        if not isinstance(fetched_at, str):
            fetched_at = ""  # a hand-edited sidecar must not break the sort
        candidates.append((fetched_at, project_dir.name, project_dir))
    candidates.sort(key=lambda c: (c[0], c[1]), reverse=True)
    for _fetched_at, _name, project_dir in candidates:
        frozen = load_frozen_payload(project_dir)
        if frozen is None or frozen.bbox is None or bbox_key(frozen.bbox) != want:
            continue
        remark = payload_remark(frozen.payload)
        if remark:
            _logger.warning(
                "ignoring frozen Overpass payload in %s: it carries a remark "
                "(%s), so it is a partial answer",
                project_dir,
                remark,
            )
            continue
        return frozen
    return None


def resolve_payload(
    store: ProjectStore,
    bbox: Optional[tuple[float, float, float, float]],
    *,
    overpass_json: Optional[dict[str, Any]] = None,
    refresh: bool = False,
) -> FrozenOverpass:
    """Pick the Overpass payload an import should consume.

    Precedence: caller-injected > verified freeze for this bbox AND the current
    endpoint > live fetch. ``refresh=True`` skips the freeze and always goes to
    the network.

    ``bbox`` is optional purely so an injected payload never forces the caller
    to project one (``bbox_for`` divides by cos(lat), which is degenerate at the
    poles); it is required for every path that consults or contacts Overpass.
    """
    if overpass_json is not None:
        return FrozenOverpass(
            payload=overpass_json,
            sha256=payload_sha256(overpass_json),
            source="injected",
        )
    if bbox is None:
        raise ValueError("bbox is required when no overpass_json is supplied")
    if not refresh:
        frozen = find_frozen_payload(store, bbox, endpoint=OVERPASS_URL)
        if frozen is not None:
            # The carried sha was verified against the SOURCE file's bytes, but
            # freeze_payload re-serializes canonically; recompute it so the
            # sidecar we write always describes the bytes beside it (a no-op for
            # a canonical source, and it keeps the chain honest for one that is
            # valid-but-reformatted).
            return frozen._replace(sha256=payload_sha256(frozen.payload))
    payload = fetch_overpass(bbox)
    return FrozenOverpass(
        payload=payload,
        sha256=payload_sha256(payload),
        source="overpass",
        fetched_at=_utcnow(),
        endpoint=OVERPASS_URL,
        bbox=bbox,
    )


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
    refresh: bool = False,
) -> dict[str, Any]:
    """Fetch OSM buildings and assemble a ready-to-simulate project.

    Validates arguments and material ids (raising :class:`ValueError` for the
    400 cases), resolves the Overpass data, builds the meshes, creates the
    project folder, and writes the scene / GLB / frozen payload / provenance.

    The payload is resolved by :func:`resolve_payload`: an explicit
    ``overpass_json`` wins (tests and scripted imports), else a verified freeze
    of the same bbox and endpoint from an earlier import is reused with NO
    network (and said so in ``warnings``), else Overpass is queried live.
    ``refresh=True`` forces the live fetch. Whatever is used is then frozen into
    this project's ``osm/`` folder.

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

    # --- resolve the payload (injected > frozen reuse > live fetch) ----
    # bbox is projected only when we may talk to Overpass; an injected payload
    # is not tied to a region we can vouch for (see resolve_payload).
    bbox = None if overpass_json is not None else bbox_for(lat, lon, width_m, height_m)
    frozen = resolve_payload(
        store, bbox, overpass_json=overpass_json, refresh=refresh
    )
    overpass_json = frozen.payload

    # --- geometry ------------------------------------------------------
    tm_scene, buildings, num_skipped, warnings = build_meshes(
        overpass_json,
        lat,
        lon,
        width_m,
        height_m,
        default_building_height_m=default_building_height_m,
    )
    if frozen.source == "frozen":
        # Cached and live imports are otherwise indistinguishable in the
        # response; say so, because "no network was touched" is exactly the
        # thing a user asking for fresh OSM data needs to see.
        warnings.append(
            f"reused frozen Overpass payload fetched {frozen.fetched_at} "
            f"(sha256 {frozen.sha256[:12]}...); set refresh=true to re-fetch"
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
    # Freeze the payload BEFORE provenance so a recorded overpass_sha256 always
    # has the file it names sitting next to it.
    freeze_payload(store, pid, frozen)
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
            # Reproducibility stamp. Prefixed because this event is flat and
            # already carries its own `timestamp` / `lat` / `lon`; the sidecar
            # osm/overpass.meta.json holds the same fields unprefixed.
            "overpass_sha256": frozen.sha256,
            "overpass_source": frozen.source,
            "overpass_fetched_at": frozen.fetched_at,
            "overpass_endpoint": frozen.endpoint,
            "overpass_bbox": list(frozen.bbox) if frozen.bbox is not None else None,
            "overpass_payload_uri": OVERPASS_PAYLOAD_REL,
        },
    )

    return {
        "project_id": pid,
        "num_buildings": len(buildings),
        "num_skipped": num_skipped,
        "warnings": warnings,
    }
