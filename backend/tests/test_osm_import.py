"""Tests for the one-shot OpenStreetMap import (service + route).

No network: every test that reaches the geometry/assembly path supplies a
canned Overpass JSON (two building ways with geometry - one tagged ``height``,
one tagged ``building:levels`` - plus one degenerate <3-point way that must be
skipped). The route tests monkeypatch ``fetch_overpass`` so the endpoint is
never contacted.

There is also an offline unit test of the bbox / meters-per-degree math and an
opt-in live smoke test (skipped unless SEAM_TEST_LIVE_OSM is set).
"""

import hashlib
import io
import json
import math
import os
from datetime import datetime, timedelta

import pytest
import trimesh

from seam_studio.services import osm_import
from seam_studio.services.osm_import import (
    OVERPASS_META_REL,
    OVERPASS_PAYLOAD_REL,
    bbox_for,
    import_osm_project,
    meters_per_degree,
)
from seam_studio.services.project_store import ProjectStore, load_default_library

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


def canned_overpass_one_building():
    """A DIFFERENT valid payload (one building) for freeze-precedence tests."""
    return {
        "elements": [
            {
                "type": "way",
                "id": 3001,
                "tags": {"building": "yes", "height": "9 m"},
                "geometry": _square(CENTER_LAT, CENTER_LON, 0.0, 40.0, 18.0),
            }
        ]
    }


# A verbatim-shaped Overpass distress signal: HTTP 200, well-formed body, but
# the element list is whatever the server managed before it gave up.
REMARK_TIMEOUT = (
    'runtime error: Query timed out in "query" at line 1 after 25 seconds.'
)


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


# -------------------------------------------------- Overpass payload freezer


class CountingFetch:
    """Stand-in for ``fetch_overpass`` that records every call.

    Tests assert on ``calls`` to prove the freezer really avoided the network -
    a reuse path that silently re-fetched would still produce a correct project,
    so call counting is the only honest signal.
    """

    def __init__(self, payload=None):
        self.payload = payload if payload is not None else canned_overpass()
        self.calls: list[tuple] = []

    def __call__(self, bbox, **kwargs):
        self.calls.append(tuple(bbox))
        return self.payload

    @property
    def count(self) -> int:
        return len(self.calls)


def _import(store, library, project_id, **kwargs):
    """Import at the shared center with a 500 x 500 m box (one stable bbox)."""
    return import_osm_project(
        store,
        library,
        name=project_id,
        lat=CENTER_LAT,
        lon=CENTER_LON,
        width_m=500.0,
        height_m=500.0,
        project_id=project_id,
        **kwargs,
    )


def _meta(store, project_id) -> dict:
    return store.load_json(project_id, OVERPASS_META_REL)


def _import_event(store, project_id) -> dict:
    prov = store.load_json(project_id, "provenance.json")
    events = [e for e in prov["events"] if e.get("type") == "import_osm"]
    assert len(events) == 1
    return events[0]


def _rewrite_freeze(store, project_id, raw: bytes, **meta_updates) -> str:
    """Replace a project's frozen payload bytes and re-stamp its sidecar.

    Returns the new digest. Used to stage the states a store can be found in
    (hand-edited, pretty-printed, poisoned) without going through an import.
    """
    (store.resolve(project_id) / OVERPASS_PAYLOAD_REL).write_bytes(raw)
    meta = _meta(store, project_id)
    meta["sha256"] = hashlib.sha256(raw).hexdigest()
    meta.update(meta_updates)
    store.save_json(project_id, OVERPASS_META_REL, meta)
    return meta["sha256"]


def test_live_fetch_freezes_payload_with_sha_and_provenance(
    store, library, monkeypatch
):
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "frozen_one")
    assert fetch.count == 1

    # The raw payload landed inside the project folder.
    payload_file = store.resolve("frozen_one") / OVERPASS_PAYLOAD_REL
    assert payload_file.is_file()
    raw = payload_file.read_bytes()
    # It is the same OSM data, not a re-derived summary.
    assert json.loads(raw.decode("utf-8")) == canned_overpass()

    # The recorded digest is a bare hex sha256 of exactly those bytes, so
    # `sha256sum osm/overpass.json` verifies the project as published.
    digest = hashlib.sha256(raw).hexdigest()
    meta = _meta(store, "frozen_one")
    assert meta["sha256"] == digest
    assert meta["source"] == "overpass"
    assert meta["payload_uri"] == OVERPASS_PAYLOAD_REL
    assert meta["endpoint"] == osm_import.OVERPASS_URL
    assert meta["bbox"] == list(bbox_for(CENTER_LAT, CENTER_LON, 500.0, 500.0))

    # fetched_at is a UTC ISO-8601 instant.
    fetched_at = datetime.fromisoformat(meta["fetched_at"])
    assert fetched_at.tzinfo is not None
    assert fetched_at.utcoffset() == timedelta(0)

    # Provenance carries the same stamp (prefixed keys; the event is flat).
    event = _import_event(store, "frozen_one")
    assert event["overpass_sha256"] == digest
    assert event["overpass_source"] == "overpass"
    assert event["overpass_fetched_at"] == meta["fetched_at"]
    assert event["overpass_endpoint"] == osm_import.OVERPASS_URL
    assert event["overpass_bbox"] == meta["bbox"]
    assert event["overpass_payload_uri"] == OVERPASS_PAYLOAD_REL


def test_second_import_reuses_frozen_payload_without_network(
    store, library, monkeypatch
):
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    first = _import(store, library, "reuse_first")
    assert fetch.count == 1

    second = _import(store, library, "reuse_second")
    # The whole point: no second network call for the same bbox.
    assert fetch.count == 1
    assert second["num_buildings"] == first["num_buildings"]
    # A cached import must not look like a fresh one to the caller.
    assert not first["warnings"]
    assert any("reused frozen Overpass payload" in w for w in second["warnings"])

    meta_first = _meta(store, "reuse_first")
    meta_second = _meta(store, "reuse_second")
    # Same bytes, and the ORIGINAL fetch instant/endpoint travel forward - that
    # pair, not the re-import time, is what dates the OSM snapshot.
    assert meta_second["sha256"] == meta_first["sha256"]
    assert meta_second["fetched_at"] == meta_first["fetched_at"]
    assert meta_second["endpoint"] == meta_first["endpoint"]
    # ...but the reuse is visible rather than disguised as a fresh fetch.
    assert meta_second["source"] == "frozen"
    assert _import_event(store, "reuse_second")["overpass_source"] == "frozen"

    # The second project got its own full copy, so it stands alone...
    payload_file = store.resolve("reuse_second") / OVERPASS_PAYLOAD_REL
    raw_second = payload_file.read_bytes()
    assert json.loads(raw_second.decode("utf-8")) == canned_overpass()
    # ...and re-freezing is byte-stable: the carried-forward sha still verifies
    # the re-written bytes. If it did not, the copy would fail its own hash
    # check and the reuse chain would silently break after one hop.
    assert hashlib.sha256(raw_second).hexdigest() == meta_first["sha256"]
    first_file = store.resolve("reuse_first") / OVERPASS_PAYLOAD_REL
    assert raw_second == first_file.read_bytes()

    # Which means a third import chains off the copy just as well.
    _import(store, library, "reuse_third")
    assert fetch.count == 1
    assert _meta(store, "reuse_third")["sha256"] == meta_first["sha256"]


def test_different_bbox_does_not_reuse_the_freeze(store, library, monkeypatch):
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "bbox_a")
    assert fetch.count == 1
    # Same center, different rectangle -> a different Overpass query.
    import_osm_project(
        store,
        library,
        name="bbox_b",
        lat=CENTER_LAT,
        lon=CENTER_LON,
        width_m=800.0,
        height_m=500.0,
        project_id="bbox_b",
    )
    assert fetch.count == 2


def test_refresh_forces_a_live_refetch(store, library, monkeypatch):
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "refresh_first")
    assert fetch.count == 1
    _import(store, library, "refresh_second", refresh=True)
    assert fetch.count == 2
    assert _meta(store, "refresh_second")["source"] == "overpass"


def test_corrupted_freeze_is_ignored_and_refetched(store, library, monkeypatch):
    """A freeze whose bytes no longer match its sha is never reused.

    Fallback semantics: the payload is dropped (NOT trusted, NOT an error) and
    the import fetches fresh data, so a truncated or hand-edited file degrades
    to today's behaviour instead of poisoning the reproducibility claim.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "corrupt_src")
    assert fetch.count == 1

    payload_file = store.resolve("corrupt_src") / OVERPASS_PAYLOAD_REL
    good = payload_file.read_text(encoding="utf-8")
    payload_file.write_text(good[: len(good) // 2], encoding="utf-8")

    _import(store, library, "corrupt_dst")
    assert fetch.count == 2  # the tampered freeze was not trusted
    # The new project's own freeze is intact and self-verifying.
    raw = (store.resolve("corrupt_dst") / OVERPASS_PAYLOAD_REL).read_bytes()
    assert _meta(store, "corrupt_dst")["sha256"] == hashlib.sha256(raw).hexdigest()


def test_missing_sidecar_is_ignored_and_refetched(store, library, monkeypatch):
    """A payload with no (or an unreadable) sidecar is unverifiable -> ignored."""
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "nosidecar_src")
    assert fetch.count == 1
    (store.resolve("nosidecar_src") / OVERPASS_META_REL).unlink()

    _import(store, library, "nosidecar_dst")
    assert fetch.count == 2


def test_swapped_but_still_valid_freeze_is_ignored_and_refetched(
    store, library, monkeypatch
):
    """Content, not parseability, is what the digest protects.

    The payload here stays perfectly good JSON of exactly the expected shape -
    only its CONTENT was swapped, with the sidecar's sha left untouched. That is
    the case a truncation test cannot see (a half file fails the JSON parser
    anyway), so this is what actually pins the digest check.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "swapped_src")
    assert fetch.count == 1
    recorded = _meta(store, "swapped_src")["sha256"]

    payload_file = store.resolve("swapped_src") / OVERPASS_PAYLOAD_REL
    swapped = json.dumps(canned_overpass_one_building()).encode("utf-8")
    payload_file.write_bytes(swapped)  # sidecar sha deliberately NOT updated
    assert json.loads(payload_file.read_text(encoding="utf-8"))["elements"]
    assert hashlib.sha256(swapped).hexdigest() != recorded

    _import(store, library, "swapped_dst")
    assert fetch.count == 2  # the swapped payload was not trusted
    assert _meta(store, "swapped_dst")["source"] == "overpass"


def test_newest_matching_freeze_wins(store, library, monkeypatch):
    """With several freezes of one bbox, the LATEST fetch is the one served."""
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    fetch.payload = canned_overpass()  # two buildings
    _import(store, library, "newest_old")
    fetch.payload = canned_overpass_one_building()  # one building
    _import(store, library, "newest_new", refresh=True)
    assert fetch.count == 2

    # Pin the ordering instead of racing the clock (both imports happen in the
    # same second), and put the OLDER payload in the folder that sorts LAST so
    # a lookup that picked the wrong end of the list would be caught.
    for pid, when in (
        ("newest_old", "2020-01-01T00:00:00+00:00"),
        ("newest_new", "2026-01-01T00:00:00+00:00"),
    ):
        meta = _meta(store, pid)
        meta["fetched_at"] = when
        store.save_json(pid, OVERPASS_META_REL, meta)

    third = _import(store, library, "newest_third")
    assert fetch.count == 2  # served from a freeze
    assert third["num_buildings"] == 1  # ...specifically the 2026 one
    meta_third = _meta(store, "newest_third")
    assert meta_third["sha256"] == _meta(store, "newest_new")["sha256"]
    assert meta_third["fetched_at"] == "2026-01-01T00:00:00+00:00"


def test_lookup_shortlists_on_sidecars_and_parses_one_payload(
    store, library, monkeypatch
):
    """Non-matching freezes are ruled out by their sidecar, never parsed.

    Payloads run to megabytes, so a lookup that loaded every freeze to read the
    bbox off it would make every import scale with the size of the store. The
    sidecar already carries ``bbox_key``; this pins that it is what filters, and
    that the surviving candidates are parsed one at a time (newest first) rather
    than all up front.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "scan_match")  # the bbox we will ask for
    import_osm_project(  # same center, different rectangle -> different bbox
        store,
        library,
        name="scan_other",
        lat=CENTER_LAT,
        lon=CENTER_LON,
        width_m=800.0,
        height_m=500.0,
        project_id="scan_other",
    )
    _import(store, library, "scan_injected", overpass_json=canned_overpass())
    assert fetch.count == 2

    loaded: list = []
    real_load = osm_import.load_frozen_payload

    def counting_load(project_dir):
        loaded.append(project_dir)
        return real_load(project_dir)

    monkeypatch.setattr(osm_import, "load_frozen_payload", counting_load)
    found = osm_import.find_frozen_payload(
        store, bbox_for(CENTER_LAT, CENTER_LON, 500.0, 500.0)
    )
    assert found is not None
    assert loaded == [store.resolve("scan_match")]


def test_sidecar_with_a_non_string_fetched_at_survives_the_lookup(
    store, library, monkeypatch
):
    """A hand-edited sidecar degrades the ordering, it does not crash imports.

    ``fetched_at`` is only ever compared, so a value of the wrong type would
    raise TypeError out of the sort - i.e. one bad sidecar anywhere in the store
    would take down every import of that region.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "badtime_a")
    _import(store, library, "badtime_z", refresh=True)
    assert fetch.count == 2

    # Two candidates, so the sort really has to compare them; "badtime_z" wins
    # the (empty, empty) tie on folder name and is the one with the bad value.
    for pid, when in (("badtime_a", ""), ("badtime_z", 1234567890)):
        meta = _meta(store, pid)
        meta["fetched_at"] = when
        store.save_json(pid, OVERPASS_META_REL, meta)

    _import(store, library, "badtime_dst")
    assert fetch.count == 2  # no TypeError, and the freeze is still usable
    assert _meta(store, "badtime_dst")["fetched_at"] is None


def test_non_canonical_freeze_is_reused_and_rechains(store, library, monkeypatch):
    """A valid-but-reformatted freeze reuses, and its COPY verifies itself.

    The source here is pretty-printed with a correctly updated sidecar, so it is
    honest - just not byte-canonical. Re-freezing re-serializes canonically, so
    carrying the source's sha forward would leave the new project holding a
    digest that does not describe its own file, breaking the chain one hop later.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "pretty_src")
    assert fetch.count == 1
    payload = json.loads(
        (store.resolve("pretty_src") / OVERPASS_PAYLOAD_REL).read_text(
            encoding="utf-8"
        )
    )
    pretty_sha = _rewrite_freeze(
        store, "pretty_src", json.dumps(payload, indent=2).encode("utf-8")
    )

    _import(store, library, "pretty_dst")
    assert fetch.count == 1  # the reformatted freeze still verified

    raw_dst = (store.resolve("pretty_dst") / OVERPASS_PAYLOAD_REL).read_bytes()
    meta_dst = _meta(store, "pretty_dst")
    assert meta_dst["sha256"] == hashlib.sha256(raw_dst).hexdigest()
    assert meta_dst["sha256"] != pretty_sha  # recomputed, not carried
    assert json.loads(raw_dst.decode("utf-8")) == canned_overpass()

    # And the next hop chains off the copy exactly as it should.
    _import(store, library, "pretty_third")
    assert fetch.count == 1


def test_fetch_overpass_rejects_a_partial_answer_with_a_remark(monkeypatch):
    """HTTP 200 + a failure ``remark`` is a failure, not a payload."""
    import httpx

    class _Resp:
        def __init__(self, body):
            self._body = body

        def raise_for_status(self):
            return None

        def json(self):
            return self._body

    bbox = bbox_for(CENTER_LAT, CENTER_LON, 500.0, 500.0)

    body = dict(canned_overpass(), remark=REMARK_TIMEOUT)
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(body))
    with pytest.raises(osm_import.OverpassError, match="timed out"):
        osm_import.fetch_overpass(bbox)

    # A remark that is not a failure report is not an error (the gate is not a
    # blanket ban on the field).
    chatty = dict(canned_overpass(), remark="considered 42 elements")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _Resp(chatty))
    assert osm_import.fetch_overpass(bbox) == chatty


def test_frozen_payload_carrying_a_remark_is_never_reused(
    store, library, monkeypatch
):
    """Heals a store poisoned before the fetch-time gate existed.

    A truncated answer that was frozen with its bbox recorded would otherwise be
    served to every later import of that region - the partial scene outliving
    the one bad fetch that produced it.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "remark_src")
    assert fetch.count == 1
    poisoned = dict(canned_overpass(), remark=REMARK_TIMEOUT)
    _rewrite_freeze(store, "remark_src", json.dumps(poisoned).encode("utf-8"))

    _import(store, library, "remark_dst")
    assert fetch.count == 2
    assert _meta(store, "remark_dst")["source"] == "overpass"


def test_freeze_from_another_endpoint_is_not_reused(store, library, monkeypatch):
    """The endpoint is part of the cache identity, not just a recorded field.

    OVERPASS_URL is env-overridable, so a mirror or a local extract can be
    serving different data for the same bbox; substituting one for the other
    would be a silent source swap.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "endpoint_a")
    assert fetch.count == 1
    assert _meta(store, "endpoint_a")["endpoint"] == osm_import.OVERPASS_URL

    other = "http://localhost:12345/api/interpreter"
    monkeypatch.setattr(osm_import, "OVERPASS_URL", other)
    _import(store, library, "endpoint_b")
    assert fetch.count == 2
    assert _meta(store, "endpoint_b")["endpoint"] == other

    # ...and a third import against the new endpoint chains off B, not A.
    _import(store, library, "endpoint_c")
    assert fetch.count == 2
    assert _meta(store, "endpoint_c")["endpoint"] == other


def test_frozen_payload_dirs_finds_a_root_that_is_itself_a_project(
    store, library, monkeypatch
):
    """A store root can BE a project folder (opened-a-single-project layout)."""
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "root_proj")
    project_dir = store.resolve("root_proj")

    single = ProjectStore(roots=[project_dir])
    assert osm_import._frozen_payload_dirs(single) == [project_dir]
    found = osm_import.find_frozen_payload(
        single, bbox_for(CENTER_LAT, CENTER_LON, 500.0, 500.0)
    )
    assert found is not None
    assert found.source == "frozen"


def test_injected_payload_is_frozen_but_not_offered_for_reuse(
    store, library, monkeypatch
):
    """Canned data is recorded for the project, yet never serves other imports.

    We cannot vouch that an injected payload actually covers the requested
    bbox, so it is frozen with ``source: injected`` and a null bbox and stays
    out of the reuse index.
    """
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    _import(store, library, "injected_one", overpass_json=canned_overpass())
    assert fetch.count == 0

    meta = _meta(store, "injected_one")
    assert meta["source"] == "injected"
    assert meta["bbox"] is None
    assert meta["fetched_at"] is None
    assert meta["endpoint"] is None
    raw = (store.resolve("injected_one") / OVERPASS_PAYLOAD_REL).read_bytes()
    assert meta["sha256"] == hashlib.sha256(raw).hexdigest()

    # A later live import of the same region still goes to the network.
    _import(store, library, "injected_two")
    assert fetch.count == 1


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


def test_route_reuses_freeze_and_honors_refresh(api_client, monkeypatch):
    """End-to-end: the route freezes, reuses, and re-fetches on demand."""
    fetch = CountingFetch()
    monkeypatch.setattr(osm_import, "fetch_overpass", fetch)

    def post(project_id, **extra):
        return api_client.post(
            "/api/projects/import-osm",
            json={
                "name": project_id,
                "project_id": project_id,
                "lat": CENTER_LAT,
                "lon": CENTER_LON,
                "width_m": 500,
                "height_m": 500,
                **extra,
            },
        )

    first = post("freeze_route_a")
    assert first.status_code == 200
    assert fetch.count == 1
    assert not first.json()["warnings"]
    # Same bbox, new project: served from the first project's frozen payload...
    second = post("freeze_route_b")
    assert second.status_code == 200
    assert fetch.count == 1
    # ...and the response says so, because it is otherwise byte-identical to a
    # live import - the only place a user can notice the data is not fresh.
    body = second.json()
    assert body["num_buildings"] == first.json()["num_buildings"]
    reuse = [w for w in body["warnings"] if "reused frozen Overpass payload" in w]
    assert len(reuse) == 1
    assert "refresh=true" in reuse[0]
    # Explicit opt-in to fresh OSM data.
    third = post("freeze_route_c", refresh=True)
    assert third.status_code == 200
    assert fetch.count == 2
    assert not third.json()["warnings"]

    # The frozen payload is fetchable as a project asset (no new route needed).
    asset = api_client.get(
        f"/api/projects/freeze_route_a/assets/{OVERPASS_PAYLOAD_REL}"
    )
    assert asset.status_code == 200
    assert json.loads(asset.content.decode("utf-8")) == canned_overpass()


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
