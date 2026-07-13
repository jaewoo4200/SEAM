"""Channel-analysis persistence + positions-only live feed.

Covers the follow-up contracts:
  POST /api/projects/{id}/analyze/channel  with persist=true
      -> stores a kind="channel" result set (ref + results/*.json) and stamps
         result_id/created_at on the response
  GET  /api/projects/{id}/results/channel  -> latest (or specific) stored run
  GET  /api/projects/{id}/scene/positions  -> device/actor pose table only

Runs against the shared tmp-rooted ``api_client`` fixture; the mock backend
solves the demo scene, so no Sionna/GPU is needed.
"""

import pytest

from .conftest import make_demo_scene

PID = "chanpos_src"

CFG = {
    "id": "e2e",
    "name": "e2e",
    "backend": "mock",
    "frequency_hz": 28e9,
    "max_depth": 3,
}


@pytest.fixture()
def client(api_client):
    resp = api_client.post(
        "/api/projects", json={"name": "ChanPos", "project_id": PID}
    )
    assert resp.status_code == 201
    scene = make_demo_scene(scene_id=PID)
    put = api_client.put(
        f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json")
    )
    assert put.status_code == 200
    return api_client


# ------------------------------------------------------- channel persistence


def test_analyze_without_persist_stores_nothing(client):
    r = client.post(f"/api/projects/{PID}/analyze/channel", json={"config": CFG})
    assert r.status_code == 200
    body = r.json()
    assert body["result_id"] == "unsaved"
    refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    assert [ref for ref in refs if ref["kind"] == "channel"] == []
    # Nothing stored -> the getter 404s.
    assert client.get(f"/api/projects/{PID}/results/channel").status_code == 404


def test_persisted_analysis_round_trips_via_results_endpoint(client):
    r = client.post(
        f"/api/projects/{PID}/analyze/channel",
        json={"config": CFG, "persist": True},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["result_id"] != "unsaved"
    assert body["created_at"] is not None

    # A kind="channel" ref landed on the scene, with size stamped.
    refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    chan = [ref for ref in refs if ref["kind"] == "channel"]
    assert len(chan) == 1
    assert chan[0]["result_id"] == body["result_id"]
    assert chan[0]["size_bytes"] and chan[0]["size_bytes"] > 0

    # Latest getter returns the same analysis; explicit id too.
    latest = client.get(f"/api/projects/{PID}/results/channel")
    assert latest.status_code == 200
    assert latest.json()["result_id"] == body["result_id"]
    assert latest.json()["rss_dbm"] == body["rss_dbm"]
    by_id = client.get(
        f"/api/projects/{PID}/results/channel",
        params={"result_id": body["result_id"]},
    )
    assert by_id.status_code == 200
    assert by_id.json()["result_id"] == body["result_id"]


def test_persisted_channel_run_participates_in_prune(client):
    for _ in range(3):
        r = client.post(
            f"/api/projects/{PID}/analyze/channel",
            json={"config": CFG, "persist": True},
        )
        assert r.status_code == 200
    pruned = client.post(
        f"/api/projects/{PID}/results/prune",
        json={"keep_latest": 1, "kinds": ["channel"]},
    )
    assert pruned.status_code == 200
    assert len(pruned.json()["removed"]) == 2
    refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    assert len([ref for ref in refs if ref["kind"] == "channel"]) == 1


# ------------------------------------------------------- positions live feed


def test_scene_positions_returns_pose_table_only(client):
    scene = client.get(f"/api/projects/{PID}/scene").json()
    r = client.get(f"/api/projects/{PID}/scene/positions")
    assert r.status_code == 200
    body = r.json()
    assert set(body.keys()) == {"devices", "actors"}
    assert len(body["devices"]) == len(scene["devices"])
    by_id = {d["id"]: d for d in body["devices"]}
    for dev in scene["devices"]:
        assert by_id[dev["id"]]["position"] == dev["position"]
        assert by_id[dev["id"]]["orientation_deg"] == dev["orientation_deg"]
    # Pose rows carry exactly id/position/orientation — no scene payload leaks.
    assert set(body["devices"][0].keys()) == {"id", "position", "orientation_deg"}


def test_scene_positions_unknown_project_404(client):
    assert client.get("/api/projects/nope/scene/positions").status_code == 404
