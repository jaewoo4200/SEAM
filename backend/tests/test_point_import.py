"""Point/device/trajectory import: coordinate resolution + the three endpoints.

Covers cartesian and geographic (auto-detected) points, WGS84 geodetic->ENU
conversion, AGL placement + underground warnings via the terrain raycast, and
the upsert/add device semantics. Raycasting tests build a tiny box GLB with
trimesh and skip when trimesh's native ray index is unavailable (mirrors
test_terrain.py).
"""

from pathlib import Path

import pytest
import trimesh

from seam_studio.schemas.point_import import DeviceImportRequest, ImportDevice
from seam_studio.schemas.scene import Scene
from seam_studio.services.point_import import (
    GeoAnchorMissingError,
    geodetic_to_enu,
    import_devices,
    resolve_point,
    resolve_waypoints,
)

# Demo geodetic anchor (matches the input-format examples): Seoul-ish.
ANCHOR = [37.5563, 127.0448, 0.0]
# One arc-second-ish north step; ~111.32 m per 0.001 deg latitude on WGS84.
DEG_N = 0.001


# --------------------------------------------------------------- fixtures


def _box_glb(path: Path, extents=(40.0, 40.0, 4.0), center=(0.0, 0.0, 0.0)) -> None:
    """Write a GLB of one axis-aligned box. Default: top face at z = 2."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tm = trimesh.Scene()
    box = trimesh.creation.box(extents=extents)
    box.apply_translation(center)
    tm.add_geometry(box, geom_name="ground", node_name="ground")
    path.write_bytes(tm.export(file_type="glb"))


def _terrain_ray_available() -> bool:
    try:
        import numpy as np

        box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
        box.ray.intersects_location(
            ray_origins=np.array([[0.0, 0.0, 5.0]]),
            ray_directions=np.array([[0.0, 0.0, -1.0]]),
        )
        return True
    except Exception:  # noqa: BLE001
        return False


ray_only = pytest.mark.skipif(
    not _terrain_ray_available(),
    reason="trimesh raycast unavailable (rtree native index not loadable)",
)


def _anchored_scene() -> Scene:
    scene = Scene(scene_id="s", name="s")
    scene.coordinate_system.origin_lat_lon_alt = list(ANCHOR)
    return scene


def _box_scene(tmp_path: Path) -> Scene:
    """Scene whose visual mesh is a single box (top at z = 2)."""
    _box_glb(tmp_path / "visual" / "scene.glb")
    return Scene(scene_id="b", name="b")


# ---------------------------------------------- geodetic conversion sanity


def test_geodetic_to_enu_north_offset_matches_wgs84():
    # A point 0.001 deg north of the anchor is ~111.32 m north (WGS84), and due
    # east/up are ~0. Pin the pure conversion helper within 0.5%.
    east, north, up = geodetic_to_enu(
        ANCHOR[0] + DEG_N, ANCHOR[1], 0.0, ANCHOR[0], ANCHOR[1], 0.0
    )
    assert abs(east) < 0.5
    assert abs(up) < 0.5
    assert north > 0
    assert abs(north - 111.32) / 111.32 < 0.005


def test_resolve_point_geographic_north_offset(tmp_path: Path):
    # Through resolve_point: a geographic point 0.001 deg north lands at ~+111.32
    # y with x ~ 0. alt_m maps straight to z (origin alt is 0).
    scene = _anchored_scene()
    warnings: list[str] = []
    pt = resolve_point(
        tmp_path,
        scene,
        {"lat": ANCHOR[0] + DEG_N, "lon": ANCHOR[1], "alt_m": 12.0},
        default_agl=None,
        label="p",
        warnings=warnings,
    )
    assert abs(pt[0]) < 0.5
    assert abs(pt[1] - 111.32) / 111.32 < 0.005
    assert pt[2] == pytest.approx(12.0)
    assert warnings == []


def test_resolve_point_geographic_without_anchor_raises(tmp_path: Path):
    scene = Scene(scene_id="noanchor", name="noanchor")  # origin_lat_lon_alt None
    with pytest.raises(GeoAnchorMissingError) as exc:
        resolve_point(
            tmp_path,
            scene,
            {"lat": ANCHOR[0], "lon": ANCHOR[1]},
            default_agl=None,
            label="p",
            warnings=[],
        )
    msg = str(exc.value)
    assert "no geodetic anchor" in msg
    assert "origin_lat_lon_alt" in msg
    assert "import the scene via OSM" in msg


# --------------------------------------------------- AGL + underground


@ray_only
def test_agl_point_snaps_to_surface_plus_height(tmp_path: Path):
    # Box top is z = 2; agl_m 1.5 over it => z = 3.5, regardless of any z given.
    scene = _box_scene(tmp_path)
    warnings: list[str] = []
    pt = resolve_point(
        tmp_path, scene, {"x": 0.0, "y": 0.0, "agl_m": 1.5},
        default_agl=None, label="p", warnings=warnings,
    )
    assert pt == pytest.approx([0.0, 0.0, 3.5])
    assert warnings == []


@ray_only
def test_agl_device_gets_surface_plus_agl(tmp_path: Path):
    # The same AGL resolution through a real device import.
    scene = _box_scene(tmp_path)
    req = DeviceImportRequest(devices=[ImportDevice(id="ue_agl", x=0.0, y=0.0, agl_m=1.5)])
    added, updated, warnings = import_devices(tmp_path, scene, req)
    assert added == ["ue_agl"]
    dev = scene.device_by_id("ue_agl")
    assert dev is not None
    assert dev.position[2] == pytest.approx(3.5)


@ray_only
def test_explicit_z_underground_warns_but_keeps_z(tmp_path: Path):
    # Explicit z below the surface under it is kept (never auto-fixed) but warns.
    scene = _box_scene(tmp_path)
    warnings: list[str] = []
    pt = resolve_point(
        tmp_path, scene, [0.0, 0.0, -1.0],
        default_agl=None, label="device 'buried'", warnings=warnings,
    )
    assert pt == pytest.approx([0.0, 0.0, -1.0])  # z unchanged
    assert len(warnings) == 1
    assert "below the surface" in warnings[0]
    assert "device 'buried'" in warnings[0]


@ray_only
def test_agl_wins_over_explicit_z_with_warning(tmp_path: Path):
    # A point giving BOTH z and agl_m uses AGL (z = surface + agl) and warns.
    scene = _box_scene(tmp_path)
    warnings: list[str] = []
    pt = resolve_point(
        tmp_path, scene, {"x": 0.0, "y": 0.0, "z": 99.0, "agl_m": 1.5},
        default_agl=None, label="p", warnings=warnings,
    )
    assert pt[2] == pytest.approx(3.5)
    assert any("AGL wins" in w for w in warnings)


@ray_only
def test_agl_off_footprint_keeps_height_and_warns(tmp_path: Path):
    # No surface under the point (outside the box footprint): keep agl as an
    # absolute z and warn.
    scene = _box_scene(tmp_path)
    warnings: list[str] = []
    pt = resolve_point(
        tmp_path, scene, {"x": 500.0, "y": 500.0, "agl_m": 1.5},
        default_agl=None, label="p", warnings=warnings,
    )
    assert pt[2] == pytest.approx(1.5)
    assert any("no surface underneath" in w for w in warnings)


# ----------------------------------------------- malformed point parsing


def test_mixed_cartesian_and_geographic_point_rejected(tmp_path: Path):
    scene = _anchored_scene()
    with pytest.raises(ValueError, match="mixes cartesian"):
        resolve_point(
            tmp_path, scene, {"x": 1.0, "y": 2.0, "lat": 37.0, "lon": 127.0},
            default_agl=None, label="p", warnings=[],
        )


def test_geographic_point_missing_lon_rejected(tmp_path: Path):
    scene = _anchored_scene()
    with pytest.raises(ValueError, match="both 'lat' and 'lon'"):
        resolve_point(
            tmp_path, scene, {"lat": 37.0}, default_agl=None, label="p", warnings=[],
        )


# ---------------------------------------------- trajectory resolution


def test_resolve_waypoints_mixed_forms_no_mesh(tmp_path: Path):
    # Cartesian array + geographic object + cartesian object; no mesh so the
    # agl point keeps its height and warns. All resolve to [x, y, z].
    scene = _anchored_scene()
    points = [
        [0.0, 0.0, 1.5],
        {"lat": ANCHOR[0] + DEG_N, "lon": ANCHOR[1], "alt_m": 3.0},
        {"x": 30.0, "y": 5.0, "agl_m": 1.5},
    ]
    wps, warnings = resolve_waypoints(
        tmp_path, scene, points, default_agl=1.5, ue_id="ue_01"
    )
    assert len(wps) == 3
    assert wps[0] == pytest.approx([0.0, 0.0, 1.5])
    assert abs(wps[1][1] - 111.32) / 111.32 < 0.005
    assert wps[1][2] == pytest.approx(3.0)
    assert wps[2] == pytest.approx([30.0, 5.0, 1.5])  # no mesh -> agl kept
    assert any("no surface underneath" in w for w in warnings)


# ============================================================ ENDPOINTS


def _make_project(api_client, project_id: str = "imp") -> str:
    resp = api_client.post(
        "/api/projects", json={"name": project_id, "project_id": project_id}
    )
    assert resp.status_code == 201, resp.text
    return project_id


def _store():
    from seam_studio.api import deps

    return deps.get_store()


def test_import_cartesian_device_lands_in_scene(api_client):
    pid = _make_project(api_client)
    resp = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={
            "devices": [
                {
                    "id": "ue_01",
                    "kind": "rx",
                    "position": [12.0, -4.0, 1.5],
                    "orientation_deg": [90, 0, 0],
                    "power_dbm": 23.0,
                    "name": "car UE",
                }
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["added_ids"] == ["ue_01"]
    assert body["updated_ids"] == []

    scene = api_client.get(f"/api/projects/{pid}/scene").json()
    dev = next(d for d in scene["devices"] if d["id"] == "ue_01")
    assert dev["kind"] == "rx"
    assert dev["position"] == [12.0, -4.0, 1.5]
    assert dev["orientation_deg"] == [90.0, 0.0, 0.0]  # orientation preserved
    assert dev["power_dbm"] == 23.0
    assert dev["name"] == "car UE"


def test_import_auto_generates_ids_by_kind(api_client):
    pid = _make_project(api_client, "autogen")
    resp = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={
            "devices": [
                {"kind": "tx", "position": [0.0, 0.0, 10.0]},
                {"position": [1.0, 1.0, 1.5]},  # kind omitted -> rx
                {"position": [2.0, 2.0, 1.5]},
            ]
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["added_ids"] == ["tx_001", "rx_001", "rx_002"]


def test_geographic_without_anchor_returns_400(api_client):
    pid = _make_project(api_client, "noanchor")
    resp = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={"devices": [{"id": "ue_02", "lat": 37.5563, "lon": 127.0448, "alt_m": 45.2}]},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert "no geodetic anchor" in detail
    assert "import the scene via OSM" in detail
    # Nothing was written.
    scene = api_client.get(f"/api/projects/{pid}/scene").json()
    assert scene["devices"] == []


def test_geographic_device_import_with_anchor(api_client):
    pid = _make_project(api_client, "geo")
    store = _store()
    scene = store.load_scene(pid)
    scene.coordinate_system.origin_lat_lon_alt = list(ANCHOR)
    store.save_scene(pid, scene)

    resp = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={"devices": [{"id": "ue_geo", "lat": ANCHOR[0] + DEG_N, "lon": ANCHOR[1], "alt_m": 5.0}]},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["added_ids"] == ["ue_geo"]
    out = api_client.get(f"/api/projects/{pid}/scene").json()
    dev = next(d for d in out["devices"] if d["id"] == "ue_geo")
    assert abs(dev["position"][0]) < 0.5
    assert abs(dev["position"][1] - 111.32) / 111.32 < 0.005
    assert dev["position"][2] == pytest.approx(5.0)  # alt_m - origin_alt


def test_upsert_updates_and_add_collides(api_client):
    pid = _make_project(api_client, "upsert")
    # Seed one device via add mode.
    r1 = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={"mode": "add", "devices": [{"id": "rx_001", "position": [0.0, 0.0, 1.5]}]},
    )
    assert r1.status_code == 200, r1.text
    assert r1.json()["added_ids"] == ["rx_001"]

    # Upsert the same id: updated, not added, with a warning; position changes.
    r2 = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={"mode": "upsert", "devices": [{"id": "rx_001", "position": [5.0, 5.0, 2.0]}]},
    )
    assert r2.status_code == 200, r2.text
    body = r2.json()
    assert body["updated_ids"] == ["rx_001"]
    assert body["added_ids"] == []
    assert any("already existed" in w for w in body["warnings"])
    scene = api_client.get(f"/api/projects/{pid}/scene").json()
    dev = next(d for d in scene["devices"] if d["id"] == "rx_001")
    assert dev["position"] == [5.0, 5.0, 2.0]
    # Exactly one rx_001 (upsert did not duplicate).
    assert sum(1 for d in scene["devices"] if d["id"] == "rx_001") == 1

    # Add mode on the existing id: 409 conflict.
    r3 = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={"mode": "add", "devices": [{"id": "rx_001", "position": [1.0, 1.0, 1.0]}]},
    )
    assert r3.status_code == 409, r3.text
    assert "already exists" in r3.json()["detail"]


def test_invalid_kind_returns_400(api_client):
    pid = _make_project(api_client, "badkind")
    resp = api_client.post(
        f"/api/projects/{pid}/import/devices",
        json={"devices": [{"id": "x1", "kind": "ue", "position": [0.0, 0.0, 1.0]}]},
    )
    assert resp.status_code == 400, resp.text
    assert "kind must be 'tx' or 'rx'" in resp.json()["detail"]


def test_trajectory_import_returns_cartesian_waypoints(api_client):
    pid = _make_project(api_client, "traj")
    resp = api_client.post(
        f"/api/projects/{pid}/import/trajectory",
        json={
            "ue_id": "ue_01",
            "agl_m": 1.5,
            "points": [[0.0, 0.0, 1.5], {"x": 30.0, "y": 5.0, "agl_m": 1.5}],
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ue_id"] == "ue_01"
    assert body["waypoints"] == [[0.0, 0.0, 1.5], [30.0, 5.0, 1.5]]
    # No scene mutation.
    scene = api_client.get(f"/api/projects/{pid}/scene").json()
    assert scene["devices"] == []


@ray_only
def test_trajectory_import_underground_warning(api_client):
    pid = _make_project(api_client, "trajground")
    store = _store()
    _box_glb(store.resolve(pid) / "visual" / "scene.glb")  # box top at z = 2

    resp = api_client.post(
        f"/api/projects/{pid}/import/trajectory",
        json={"ue_id": "ue_x", "points": [[0.0, 0.0, -1.0], [0.0, 0.0, 5.0]]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Explicit z kept for both waypoints (never auto-corrected).
    assert body["waypoints"][0] == [0.0, 0.0, -1.0]
    assert body["waypoints"][1] == [0.0, 0.0, 5.0]
    assert any("below the surface" in w for w in body["warnings"])
    assert not any("5.0" in w and "below" in w for w in body["warnings"])  # only the buried one


def test_templates_endpoint_returns_both_examples(api_client):
    resp = api_client.get("/api/import/templates")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Both endpoint examples plus the combined file example and a field ref.
    assert "devices" in body["devices_endpoint_example"]
    assert "points" in body["trajectory_endpoint_example"]
    combined = body["combined_file_example"]
    assert "devices" in combined and "trajectories" in combined
    assert isinstance(body["field_reference"], dict)
    assert "coordinate_systems" in body["field_reference"]
    assert "agl_m" in body["field_reference"]


# ------------------------------------- trajectory waypoint orientation (P2)


def test_trajectory_import_carries_per_waypoint_orientation(tmp_path: Path):
    from seam_studio.services.point_import import resolve_trajectory

    scene = Scene(scene_id="t", name="t")
    points = [
        {"x": 0.0, "y": 0.0, "z": 1.5, "orientation_deg": [0, 0, 0]},
        {"x": 10.0, "y": 0.0, "z": 1.5, "orientation_deg": [90, 0, 0]},
        [20.0, 0.0, 1.5],  # bare array: no orientation
    ]
    wps, oris, warnings = resolve_trajectory(
        tmp_path, scene, points, default_agl=1.5, ue_id="ue_01"
    )
    assert wps == [[0.0, 0.0, 1.5], [10.0, 0.0, 1.5], [20.0, 0.0, 1.5]]
    assert oris == [[0.0, 0.0, 0.0], [90.0, 0.0, 0.0], None]


def test_trajectory_import_no_orientation_returns_all_none(tmp_path: Path):
    from seam_studio.services.point_import import resolve_trajectory

    scene = Scene(scene_id="t", name="t")
    wps, oris, _w = resolve_trajectory(
        tmp_path, scene, [[0.0, 0.0, 1.5], [5.0, 0.0, 1.5]], default_agl=1.5, ue_id=None
    )
    assert oris == [None, None]
