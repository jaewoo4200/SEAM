"""POST /simulate/sensing-background-ensemble and GET
/results/sensing-background-ensemble (#30): persistence as result kind
"sensing_background_ensemble" (ref, request_hash, provenance), latest / by-id
reads, pruning, and the 4xx contract. Mock backend."""

import pytest

from seam_studio.schemas.results import SensingBackgroundEnsembleResultSet

from .test_trackb_ensemble import CONFIG, SENSING_RX, _scene

PID = "ens_api"
MOCK_CFG = CONFIG.model_dump(mode="json")
ENSEMBLE = "/api/projects/" + PID + "/simulate/sensing-background-ensemble"
RESULTS = "/api/projects/" + PID + "/results/sensing-background-ensemble"


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Ensemble", "project_id": PID})
    assert resp.status_code == 201, resp.text
    scene = _scene().model_copy(update={"scene_id": PID})
    put = api_client.put(f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json"))
    assert put.status_code == 200, put.text
    return api_client


def _scenario(client, sensing: bool = True) -> str:
    body = {"config": MOCK_CFG, "num_frames": 2, "dt_s": 0.5, "include_paths": False}
    if sensing:
        body["sensing"] = {"enabled": True, "sensing_rx_ids": SENSING_RX}
    resp = client.post(f"/api/projects/{PID}/simulate/scenario", json=body)
    assert resp.status_code == 200, resp.text
    return resp.json()["result_id"]


def _body(result_id: str, **kw) -> dict:
    return {
        "source": {"result_id": result_id, "frame_index": 1},
        "n_realizations": 3,
        "noise_frames_per_realization": 100,
        "seed": 7,
        "perturb": {"material": {"eps_r_pct": 10.0}, "node_pos_sigma_m": 0.2, "cfo_hz": 2.0},
        **kw,
    }


def _refs(client) -> list[tuple[str, str]]:
    scene = client.get(f"/api/projects/{PID}/scene").json()
    return [(r["kind"], r["result_id"]) for r in scene["result_sets"]]


def test_api_ensemble_persists_and_reloads(client):
    source_id = _scenario(client)
    resp = client.post(ENSEMBLE, json=_body(source_id))
    assert resp.status_code == 200, resp.text
    body = resp.json()
    SensingBackgroundEnsembleResultSet.model_validate(body)
    assert body["kind"] == "sensing_background_ensemble"
    assert body["result_id"] == "mock_sensing_background_ensemble_001"
    assert body["backend"] == "mock" and body["simulation_config_id"] == "default"
    assert body["source"] == {"result_id": source_id, "frame_index": 1}
    assert [r["label"] for r in body["realizations"]] == ["nominal", "perturbed", "perturbed"]
    meta = body["metadata"]
    assert meta["request_hash"].startswith("sha256:") and meta["scene_hash"].startswith("sha256:")
    assert meta["request"] == _body(source_id) | {
        "perturb": {
            "material": {"eps_r_pct": 10.0, "sigma_pct": 0.0, "scope": "material"},
            "node_pos_sigma_m": 0.2,
            "cfo_hz": 2.0,
        },
        "background": None,
    }
    assert meta["config_snapshot"]["rx_ids"] == SENSING_RX
    assert _refs(client) == [
        ("scenario", source_id),
        ("sensing_background_ensemble", "mock_sensing_background_ensemble_001"),
    ]

    assert client.get(RESULTS).json() == body
    assert client.get(RESULTS, params={"result_id": body["result_id"]}).json() == body
    assert client.get(RESULTS, params={"result_id": "nope"}).status_code == 404
    # summary=true: everything but the stored paths (what the UI shows).
    summary = client.get(RESULTS, params={"summary": "true"}).json()
    assert [r["background_h0"] for r in summary["realizations"]] == [None] * 3
    for r in summary["realizations"] + body["realizations"]:
        r.pop("background_h0")
    assert summary == body

    # The request hash follows the request, not the run.
    again = client.post(ENSEMBLE, json=_body(source_id)).json()
    assert again["result_id"] == "mock_sensing_background_ensemble_002"
    assert again["metadata"]["request_hash"] == meta["request_hash"]
    other = client.post(ENSEMBLE, json=_body(source_id, seed=8)).json()
    assert other["metadata"]["request_hash"] != meta["request_hash"]
    assert [r["total_power_db"] for r in again["realizations"]] == [
        r["total_power_db"] for r in body["realizations"]
    ]


def test_api_ensemble_prune_and_label(client):
    source_id = _scenario(client)
    first = client.post(ENSEMBLE, json=_body(source_id)).json()["result_id"]
    second = client.post(ENSEMBLE, json=_body(source_id, seed=1)).json()["result_id"]
    resp = client.patch(
        f"/api/projects/{PID}/results/{first}/label", json={"label": "nominal-twin"}
    )
    assert resp.status_code == 200 and resp.json()["kind"] == "sensing_background_ensemble"

    resp = client.post(
        f"/api/projects/{PID}/results/prune",
        json={"kinds": ["sensing_background_ensemble"], "keep_latest": 0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == [second]  # labeled runs survive
    assert _refs(client) == [("scenario", source_id), ("sensing_background_ensemble", first)]
    resp = client.post(f"/api/projects/{PID}/results/prune", json={"keep_latest": 0})
    assert resp.json()["removed"] == [source_id]
    assert client.get(RESULTS).json()["result_id"] == first


def test_api_ensemble_errors(client):
    source_id = _scenario(client)
    plain_id = _scenario(client, sensing=False)

    resp = client.post(ENSEMBLE, json=_body("mock_scenario_999"))
    assert resp.status_code == 404 and "unknown scenario result" in resp.json()["detail"]
    resp = client.post(ENSEMBLE, json=_body(plain_id))
    assert resp.status_code == 400 and "has no sensing" in resp.json()["detail"]
    resp = client.post(ENSEMBLE, json=_body(source_id, source={"result_id": source_id, "frame_index": 2}))
    assert resp.status_code == 400 and "out of range" in resp.json()["detail"]
    prim = _body(source_id, perturb={"material": {"eps_r_pct": 5.0, "scope": "prim"}})
    resp = client.post(ENSEMBLE, json=prim)
    assert resp.status_code == 400 and "scope 'prim'" in resp.json()["detail"]
    for bad in (
        _body(source_id, background={"mode": "both"}),
        _body(source_id, n_realizations=0),
        _body(source_id, n_realizations=201),
        _body(source_id, noise_frames_per_realization=100_001),
        _body(source_id, bogus=1),
    ):
        assert client.post(ENSEMBLE, json=bad).status_code == 422
    assert client.post(
        "/api/projects/ghost/simulate/sensing-background-ensemble", json=_body(source_id)
    ).status_code == 404
    assert client.get(RESULTS).status_code == 404
    assert [k for k, _ in _refs(client)] == ["scenario", "scenario"]


def test_api_ensemble_409_backend_without_sensing(client, monkeypatch):
    from seam_studio.services.simulation_backends.mock_backend import MockBackend

    source_id = _scenario(client)
    caps = MockBackend.capabilities
    monkeypatch.setattr(
        MockBackend, "capabilities", lambda self: {**caps(self), "sensing": False}
    )
    resp = client.post(ENSEMBLE, json=_body(source_id))
    assert resp.status_code == 409 and "needs a backend with sensing" in resp.json()["detail"]
    assert [k for k, _ in _refs(client)] == ["scenario"]


def test_api_ensemble_calibrates_a_cfar_scenario(client):
    # The CFAR detector's h0_calibrated threshold over an ensemble id is the
    # quantile of the maps h0_frame rebuilds from the stored result.
    import math

    import numpy as np

    from seam_studio.schemas.sensing import CfarOptions, DetectionMapOptions
    from seam_studio.services.channel_synthesis import cfar_statistic, dd_map
    from seam_studio.services.numerology import GRID_S, resolve_numerology
    from seam_studio.services.sensing_ensemble import h0_frame

    source_id = _scenario(client)
    ens = client.post(
        ENSEMBLE, json=_body(source_id, n_realizations=2, noise_frames_per_realization=10)
    ).json()
    grid_s = GRID_S.model_dump(mode="json", exclude_defaults=True)
    resp = client.post(
        f"/api/projects/{PID}/simulate/scenario",
        json={
            "config": MOCK_CFG, "num_frames": 1, "dt_s": 0.5, "include_paths": False,
            "sensing": {
                "enabled": True, "sensing_rx_ids": SENSING_RX, "waveform": grid_s,
                "detection": {
                    "mode": "cfar", "threshold": "h0_calibrated",
                    "h0_result_id": ens["result_id"],
                },
            },
        },
    )
    assert resp.status_code == 200, resp.text
    threshold = resp.json()["metadata"]["sensing"]["detection"]["threshold"]
    assert threshold["kind"] == "h0_calibrated"
    assert threshold["source"]["result_id"] == ens["result_id"]

    stored = SensingBackgroundEnsembleResultSet.model_validate(ens)
    num = resolve_numerology(GRID_S, 0.01)
    power = {"trp1": 43.0, "trp2": 40.0}
    values = []
    for r in range(2):
        for m in range(10):
            for link in stored.links:
                y = h0_frame(stored, r, m, link.tx_id, link.rx_id, num, power[link.tx_id], 7.0)
                sigma2 = num.sigma_h2(power[link.tx_id], 7.0)
                stat, _ = cfar_statistic(dd_map(y, num, DetectionMapOptions(), sigma2).P, CfarOptions())
                values.append(stat.ravel())
    values = np.concatenate(values)
    n = values.size
    assert n == 2 * 10 * 6 * 16 * 256
    assert threshold["value"] == np.sort(values)[n - math.floor(n * 1e-4)]


def test_openapi_has_the_ensemble_models():
    from seam_studio.main import app

    schema = app.openapi()
    components = schema["components"]["schemas"]
    assert {"BackgroundEnsembleRequest", "SensingBackgroundEnsembleResultSet"} <= set(components)
    prune = components["ResultsPruneRequest"]["properties"]["kinds"]["anyOf"][0]["items"]
    assert "sensing_background_ensemble" in prune["enum"]
