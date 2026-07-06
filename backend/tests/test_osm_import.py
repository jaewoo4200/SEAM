"""Tests for the one-shot OpenStreetMap import (service + route).

No network: every test that reaches the geometry/assembly path supplies a
canned Overpass JSON (two building ways with geometry - one tagged ``height``,
one tagged ``building:levels`` - plus one degenerate <3-point way that must be
skipped). The route tests monkeypatch ``fetch_overpass`` so the endpoint is
never contacted.

There is also an offline unit test of the bbox / meters-per-degree math and an
opt-in live smoke test (skipped unless SEAM_TEST_LIVE_OSM is set).
"""

import io
import math
import os

import pytest
import trimesh

from app.services import osm_import
from app.services.osm_import import (
    bbox_for,
    import_osm_project,
    meters_per_degree,
)
from app.services.project_store import ProjectStore, load_default_library

# Center used across the geometry tests (arbitrary urban point).
CENTER_LAT = 36.3721
CENTER_LON = 127.3604


def _offset_lonlat(lat0, lon0, dx_m, dy_m):
    """Place a point dx_m east / dy_m north of the center, in lon/lat."""
    m_per_deg_lon, m_per_deg_lat = meters_per_degree(lat0)
    return lon0 + dx_m / m_per_deg_lon, lat0 + dy_m / m_per_deg_lat


def _square(lat0, lon0, cx_m, cy_m, side_m):
    """A closed square footprint (list of {lat, lon} nodes) centered at
    (cx_m, cy_m) meters from (lat0, lon0), with the OSM closing vertex."""
    half = side_m / 2.0
    corners = [
        (cx_m - half, cy_m - half),
        (cx_m + half, cy_m - half),
        (cx_m + half, cy_m + half),
        (cx_m - half, cy_m + half),
    ]
    nodes = []
    for dx, dy in corners:
        lon, lat = _offset_lonlat(lat0, lon0, dx, dy)
        nodes.append({"lat": lat, "lon": lon})
    nodes.append(dict(nodes[0]))  # close the ring like OSM does
    return nodes


def canned_overpass():
    """Two valid buildings + one degenerate (2-point) way."""
    return {
        "elements": [
            {
                "type": "way",
                "id": 1001,
                "tags": {"building": "yes", "height": "12 m"},
                "geometry": _square(CENTER_LAT, CENTER_LON, -30.0, 0.0, 20.0),
            },
            {
                "type": "way",
                "id": 1002,
                "tags": {"building": "residential", "building:levels": "4"},
                "geometry": _square(CENTER_LAT, CENTER_LON, 30.0, 0.0, 25.0),
            },
            {
                # Degenerate: only two points -> must be skipped.
                "type": "way",
                "id": 1003,
                "tags": {"building": "yes"},
                "geometry": [
                    {"lat": CENTER_LAT, "lon": CENTER_LON},
                    {"lat": CENTER_LAT + 1e-5, "lon": CENTER_LON + 1e-5},
                ],
            },
        ]
    }


@pytest.fixture()
def store(tmp_path):
    root = tmp_path / "projects"
    root.mkdir()
    return ProjectStore(roots=[root])


@pytest.fixture()
def library():
    return load_default_library()


# --------------------------------------------------------------- geodesy


def test_bbox_meters_per_degree_matches_known_constants():
    # At lat 36.37, 1 km E-W spans 1000 / (111320*cos(lat)) degrees of lon,
    # and 1 km N-S spans 1000 / 110540 degrees of lat. Check the underlying
    # meters-per-degree against the 111.32 / 110.54 km-per-deg reference.
    m_per_deg_lon, m_per_deg_lat = meters_per_degree(36.37)
    expected_lon = 111320.0 * math.cos(math.radians(36.37))
    assert abs(m_per_deg_lon - expected_lon) / expected_lon < 0.01
    assert abs(m_per_deg_lat - 110540.0) / 110540.0 < 0.01

    # A 1 km x 1 km bbox is symmetric about the center and its spans convert
    # back to ~1 km within 1%.
    south, west, north, east = bbox_for(36.37, 127.36, 1000.0, 1000.0)
    ew_m = (east - west) * m_per_deg_lon
    ns_m = (north - south) * m_per_deg_lat
    assert abs(ew_m - 1000.0) / 1000.0 < 0.01
    assert abs(ns_m - 1000.0) / 1000.0 < 0.01
    assert abs((south + north) / 2.0 - 36.37) < 1e-9
    assert abs((west + east) / 2.0 - 127.36) < 1e-9


# ------------------------------------------------------ service assembly


def test_import_creates_ready_project(store, library):
    result = import_osm_project(
        store,
        library,
        name="Sample OSM",
        lat=CENTER_LAT,
        lon=CENTER_LON,
        width_m=500.0,
        height_m=500.0,
        project_id="sample_osm",
        overpass_json=canned_overpass(),
    )
    assert result["project_id"] == "sample_osm"
    assert result["num_buildings"] == 2
    assert result["num_skipped"] == 1  # the degenerate 2-point way

    project_dir = store.resolve("sample_osm")
    assert project_dir.name.endswith(".seam")

    # GLB exists and loads with named geometries matching the prims.
    glb_path = project_dir / "visual" / "scene.glb"
    assert glb_path.is_file()
    loaded = trimesh.load(io.BytesIO(glb_path.read_bytes()), file_type="glb")
    names = set(loaded.geometry.keys())
    assert "ground" in names
    assert {"building_000", "building_001"} <= names

    # Scene: one prim per building + a ground prim, materials + status + tags.
    scene = store.load_scene("sample_osm")
    assert scene.environment == "outdoor"
    assert scene.coordinate_system.origin_lat_lon_alt == [CENTER_LAT, CENTER_LON, 0.0]
    building_prims = [p for p in scene.prims if "building" in p.semantic_tags]
    ground_prims = [p for p in scene.prims if "ground" in p.semantic_tags]
    assert len(building_prims) == 2
    assert len(ground_prims) == 1
    for p in building_prims:
        assert p.rf.material_id == "itu_concrete"
        assert p.rf.assignment_status == "rule_suggested"
        assert p.rf.assignment_sources == ["osm_import"]
        assert p.mesh_ref.mesh_name in names
    ground = ground_prims[0]
    assert ground.rf.material_id == "ground_28ghz"
    assert ground.rf.assignment_sources == ["osm_import"]
    assert "terrain" in ground.semantic_tags

    # A default simulation config was written.
    assert len(scene.simulation_configs) == 1

    # Provenance recorded the import event.
    prov = store.load_json("sample_osm", "provenance.json")
    events = [e for e in prov["events"] if e.get("type") == "import_osm"]
    assert len(events) == 1
    assert events[0]["num_buildings"] == 2
    assert events[0]["lat"] == CENTER_LAT


def test_building_heights_honored(store, library):
    # height="12 m" -> 12 m; building:levels="4" -> 4 * 3 = 12 m here, so use a
    # 6-level building to distinguish it (6*3 = 18 m).
    data = canned_overpass()
    data["elements"][1]["tags"] = {"building": "yes", "building:levels": "6"}
    import_osm_project(
        store,
        library,
        name="Heights",
        lat=CENTER_LAT,
        lon=CENTER_LON,
        project_id="heights",
        overpass_json=data,
    )
    project_dir = store.resolve("heights")
    loaded = trimesh.load(
        io.BytesIO((project_dir / "visual" / "scene.glb").read_bytes()),
        file_type="glb",
    )
    # building_000 carries height=12 m, building_001 carries 6 levels * 3 = 18 m.
    b0_h = loaded.geometry["building_000"].bounds[1][2]
    b1_h = loaded.geometry["building_001"].bounds[1][2]
    assert abs(b0_h - 12.0) < 1e-3
    assert abs(b1_h - 18.0) < 1e-3


def test_default_height_used_when_no_tags(store, library):
    data = {
        "elements": [
            {
                "type": "way",
                "id": 2001,
                "tags": {"building": "yes"},  # no height / levels
                "geometry": _square(CENTER_LAT, CENTER_LON, 0.0, 0.0, 20.0),
            }
        ]
    }
    import_osm_project(
        store,
        library,
        name="Defaults",
        lat=CENTER_LAT,
        lon=CENTER_LON,
        project_id="defaults",
        default_building_height_m=7.5,
        overpass_json=data,
    )
    project_dir = store.resolve("defaults")
    loaded = trimesh.load(
        io.BytesIO((project_dir / "visual" / "scene.glb").read_bytes()),
        file_type="glb",
    )
    assert abs(loaded.geometry["building_000"].bounds[1][2] - 7.5) < 1e-3


def test_unknown_material_raises(store, library):
    with pytest.raises(ValueError, match="unknown default_building_material"):
        import_osm_project(
            store,
            library,
            name="Bad Mat",
            lat=CENTER_LAT,
            lon=CENTER_LON,
            project_id="bad_mat",
            default_building_material="does_not_exist",
            overpass_json=canned_overpass(),
        )
    with pytest.raises(ValueError, match="unknown ground_material"):
        import_osm_project(
            store,
            library,
            name="Bad Ground",
            lat=CENTER_LAT,
            lon=CENTER_LON,
            project_id="bad_ground",
            ground_material="nope",
            overpass_json=canned_overpass(),
        )
    # Nothing was created for the failed imports.
    assert not list(store.list_projects())


def test_out_of_range_args_raise(store, library):
    for kwargs in (
        {"lat": 200.0, "lon": 0.0},
        {"lat": 0.0, "lon": 500.0},
        {"lat": 0.0, "lon": 0.0, "width_m": 10.0},  # below 50 m
        {"lat": 0.0, "lon": 0.0, "height_m": 9000.0},  # above 3000 m
    ):
        with pytest.raises(ValueError):
            import_osm_project(
                store,
                library,
                name="Range",
                project_id="range",
                overpass_json=canned_overpass(),
                **kwargs,
            )


def test_duplicate_project_id_raises(store, library):
    import_osm_project(
        store,
        library,
        name="First",
        lat=CENTER_LAT,
        lon=CENTER_LON,
        project_id="dup_osm",
        overpass_json=canned_overpass(),
    )
    with pytest.raises(ValueError, match="already exists"):
        import_osm_project(
            store,
            library,
            name="Second",
            lat=CENTER_LAT,
            lon=CENTER_LON,
            project_id="dup_osm",
            overpass_json=canned_overpass(),
        )


# ------------------------------------------------------------- API route


def test_route_imports_via_monkeypatched_fetch(api_client, monkeypatch):
    monkeypatch.setattr(osm_import, "fetch_overpass", lambda *a, **k: canned_overpass())
    resp = api_client.post(
        "/api/projects/import-osm",
        json={
            "name": "Route OSM",
            "project_id": "route_osm",
            "lat": CENTER_LAT,
            "lon": CENTER_LON,
            "width_m": 500,
            "height_m": 500,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["project_id"] == "route_osm"
    assert body["num_buildings"] == 2
    assert body["num_skipped"] == 1

    # The project shows up and its GLB is served.
    listed = {p["project_id"] for p in api_client.get("/api/projects").json()}
    assert "route_osm" in listed
    glb = api_client.get("/api/projects/route_osm/assets/visual/scene.glb")
    assert glb.status_code == 200


def test_route_unreachable_maps_to_502(api_client, monkeypatch):
    def boom(*a, **k):
        raise osm_import.OverpassError("no internet; check your connection")

    monkeypatch.setattr(osm_import, "fetch_overpass", boom)
    resp = api_client.post(
        "/api/projects/import-osm",
        json={"name": "Down", "project_id": "down_osm", "lat": CENTER_LAT, "lon": CENTER_LON},
    )
    assert resp.status_code == 502, resp.text
    assert "connection" in resp.json()["detail"].lower()


def test_route_timeout_maps_to_504(api_client, monkeypatch):
    def slow(*a, **k):
        raise osm_import.OverpassTimeout("overpass timed out")

    monkeypatch.setattr(osm_import, "fetch_overpass", slow)
    resp = api_client.post(
        "/api/projects/import-osm",
        json={"name": "Slow", "project_id": "slow_osm", "lat": CENTER_LAT, "lon": CENTER_LON},
    )
    assert resp.status_code == 504, resp.text


def test_route_unknown_material_maps_to_400(api_client, monkeypatch):
    monkeypatch.setattr(osm_import, "fetch_overpass", lambda *a, **k: canned_overpass())
    resp = api_client.post(
        "/api/projects/import-osm",
        json={
            "name": "Bad",
            "project_id": "bad_osm",
            "lat": CENTER_LAT,
            "lon": CENTER_LON,
            "default_building_material": "nope",
        },
    )
    assert resp.status_code == 400, resp.text


# ------------------------------------------------------ live smoke (opt-in)


@pytest.mark.skipif(
    not os.environ.get("SEAM_TEST_LIVE_OSM"),
    reason="live Overpass smoke test; set SEAM_TEST_LIVE_OSM=1 to enable",
)
def test_live_overpass_smoke(store, library):
    # Tiny 100 m x 100 m bbox in central Daejeon; just exercise the real fetch
    # + assembly path. Not asserting a building count (OSM data changes).
    result = import_osm_project(
        store,
        library,
        name="Live Smoke",
        lat=CENTER_LAT,
        lon=CENTER_LON,
        width_m=100.0,
        height_m=100.0,
        project_id="live_smoke",
    )
    assert result["project_id"] == "live_smoke"
    assert (store.resolve("live_smoke") / "visual" / "scene.glb").is_file()
