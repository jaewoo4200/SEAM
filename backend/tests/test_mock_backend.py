"""Tests for the simulation backend interface, mock backend, and simulate API."""

import math
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.schemas.devices import Device
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import SimulationConfig
from app.services.availability import sionna_available
from app.services.project_store import load_default_library
from app.services.simulation_backends import (
    BackendUnavailableError,
    get_backend,
    resolve_backend,
)
from app.services.simulation_backends.mock_backend import (
    SPEED_OF_LIGHT,
    MockBackend,
)
from app.services.simulation_backends.sionna_backend import SionnaBackend

# Whether Sionna RT is installed in this environment. The backend registry
# tests below branch on it so the suite passes both with and without Sionna:
# when installed, "auto" resolves to sionna and a named "sionna" request runs.
SIONNA_INSTALLED = sionna_available()

TX_POS = [0.0, 0.0, 10.0]
RX_POS = [20.0, 0.0, 1.5]
GROUND_PRIM_ID = "/terrain/ground"
BUILDING_PRIM_ID = "/buildings/b01/walls"


def make_scene() -> Scene:
    return Scene(
        scene_id="sim_test",
        name="Sim Test",
        prims=[
            Prim(
                id=BUILDING_PRIM_ID,
                name="walls",
                semantic_tags=["building", "wall"],
                mesh_ref=MeshRef(mesh_name="building_01"),
                transform={"translation": [10.0, 5.0, 0.0]},
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
            Prim(
                id=GROUND_PRIM_ID,
                name="ground",
                semantic_tags=["terrain", "ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=TX_POS, power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=RX_POS),
        ],
    )


@pytest.fixture()
def scene() -> Scene:
    return make_scene()


@pytest.fixture()
def library():
    return load_default_library()


@pytest.fixture()
def backend() -> MockBackend:
    return MockBackend()


# --------------------------------------------------------------- mock paths


def test_mock_is_available(backend):
    assert backend.is_available() is True


def test_simulate_paths_los_and_reflections(backend, scene, library, tmp_path):
    result = backend.simulate_paths(tmp_path, scene, library, SimulationConfig())

    assert result.backend == "mock"
    assert len(result.paths) >= 2
    los_paths = [p for p in result.paths if p.path_type == "los"]
    assert len(los_paths) == 1

    los = los_paths[0]
    dist = math.dist(TX_POS, RX_POS)
    expected_delay_ns = dist / SPEED_OF_LIGHT * 1e9
    assert los.delay_ns == pytest.approx(expected_delay_ns, rel=0.01)
    assert los.vertices == [TX_POS, RX_POS]
    assert los.tx_id == "tx_001"
    assert los.rx_id == "rx_001"

    reflections = [p for p in result.paths if p.path_type == "reflection"]
    assert reflections
    for path in reflections:
        assert path.power_dbm < los.power_dbm
        assert len(path.vertices) == 3
        assert path.delay_ns > los.delay_ns

    # Path ids are sequential.
    assert [p.path_id for p in result.paths] == [
        f"path_{i:04d}" for i in range(1, len(result.paths) + 1)
    ]
    assert result.metadata["engine"] == "mock-deterministic-v1"
    assert result.metadata["num_tx"] == 1
    assert result.metadata["num_rx"] == 1


def test_ground_bounce_interaction_carries_ground_prim(backend, scene, library, tmp_path):
    result = backend.simulate_paths(tmp_path, scene, library, SimulationConfig())
    ground_paths = [
        p
        for p in result.paths
        if p.interactions and p.interactions[0].point[2] == 0.0
    ]
    assert len(ground_paths) == 1
    interaction = ground_paths[0].interactions[0]
    assert interaction.type == "reflection"
    assert interaction.prim_id == GROUND_PRIM_ID
    assert interaction.rf_material_id == "ground"


def test_wall_bounce_interaction_references_building(backend, scene, library, tmp_path):
    result = backend.simulate_paths(tmp_path, scene, library, SimulationConfig())
    wall_paths = [
        p
        for p in result.paths
        if p.interactions and p.interactions[0].prim_id == BUILDING_PRIM_ID
    ]
    assert len(wall_paths) == 1
    interaction = wall_paths[0].interactions[0]
    assert interaction.rf_material_id == "itu_concrete"
    # Bounce point anchored at the prim translation, lifted to mean tx/rx z.
    assert interaction.point[0] == pytest.approx(10.0)
    assert interaction.point[1] == pytest.approx(5.0)
    assert interaction.point[2] == pytest.approx((TX_POS[2] + RX_POS[2]) / 2.0)


def test_simulate_paths_is_deterministic(backend, scene, library, tmp_path):
    config = SimulationConfig()
    first = backend.simulate_paths(tmp_path, scene, library, config)
    second = backend.simulate_paths(tmp_path, make_scene(), library, config)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


def test_max_depth_zero_yields_only_los(backend, scene, library, tmp_path):
    config = SimulationConfig(max_depth=0)
    result = backend.simulate_paths(tmp_path, scene, library, config)
    assert len(result.paths) == 1
    assert result.paths[0].path_type == "los"


# ----------------------------------------------------------- mock radio map


def test_radio_map_grid_dimensions(backend, scene, library, tmp_path):
    config = SimulationConfig()
    result = backend.simulate_radio_map(tmp_path, scene, library, config)

    # Union bbox of device positions and prim anchors, padded 20 m:
    # x in [0, 20] -> [-20, 40]; y in [0, 5] -> [-20, 25]; cell 2 m.
    cell = config.radio_map.cell_size_m
    assert result.grid.origin == [-20.0, -20.0, config.radio_map.height_m]
    assert result.grid.nx == math.ceil(60.0 / cell)
    assert result.grid.ny == math.ceil(45.0 / cell)
    assert result.grid.cell_size_m == cell
    assert len(result.values) == result.grid.ny
    assert all(len(row) == result.grid.nx for row in result.values)
    assert all(v is not None for row in result.values for v in row)
    assert result.tx_id == "tx_001"
    # Default metric follows Sionna RT (path gain in dB).
    assert result.metric == "path_gain_db"


def test_radio_map_metric_switch_shifts_by_tx_power(backend, scene, library, tmp_path):
    rss_cfg = SimulationConfig(radio_map={"metric": "rss_dbm"})
    gain_cfg = SimulationConfig(radio_map={"metric": "path_gain_db"})
    rss = backend.simulate_radio_map(tmp_path, scene, library, rss_cfg)
    gain = backend.simulate_radio_map(tmp_path, scene, library, gain_cfg)

    assert gain.metric == "path_gain_db"
    tx_power = 30.0
    for row_rss, row_gain in zip(rss.values, gain.values):
        for v_rss, v_gain in zip(row_rss, row_gain):
            assert v_rss - v_gain == pytest.approx(tx_power)


def test_radio_map_is_deterministic(backend, scene, library, tmp_path):
    config = SimulationConfig()
    first = backend.simulate_radio_map(tmp_path, scene, library, config)
    second = backend.simulate_radio_map(tmp_path, make_scene(), library, config)
    assert first.model_dump(mode="json") == second.model_dump(mode="json")


# --------------------------------------------------- backend registry/sionna


def test_sionna_backend_availability_matches_probe():
    assert SionnaBackend().is_available() == SIONNA_INSTALLED


@pytest.mark.skipif(SIONNA_INSTALLED, reason="sionna installed: named request succeeds")
def test_resolve_named_sionna_raises_when_absent():
    with pytest.raises(BackendUnavailableError):
        resolve_backend(SimulationConfig(backend="sionna"))


def test_resolve_auto_prefers_sionna_when_available():
    backend = resolve_backend(SimulationConfig(backend="auto"))
    assert backend.name == ("sionna" if SIONNA_INSTALLED else "mock")


@pytest.mark.skipif(not SIONNA_INSTALLED, reason="requires sionna-rt installed")
def test_resolve_named_sionna_when_installed():
    assert resolve_backend(SimulationConfig(backend="sionna")).name == "sionna"


def test_get_backend_unknown_name():
    with pytest.raises(ValueError):
        get_backend("does_not_exist")


# ------------------------------------------------------------------ API


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    """TestClient against a temp project root with a ready-to-simulate project."""
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))

    from app.api.deps import get_store
    from app.core.config import get_settings

    get_settings.cache_clear()
    get_store.cache_clear()

    from app.main import app

    store = get_store()
    store.create_project("Sim Test", project_id="sim_test")
    scene = make_scene()
    store.save_scene("sim_test", scene)

    client = TestClient(app)
    try:
        yield client, tmp_path
    finally:
        get_settings.cache_clear()
        get_store.cache_clear()


# Force the mock backend so these persistence/roundtrip assertions are
# deterministic whether or not Sionna RT is installed in the environment.
MOCK_REQ = {"config": {"backend": "mock"}}


def test_api_simulate_and_results_roundtrip(api_client):
    client, root = api_client

    resp = client.post("/api/projects/sim_test/simulate/paths", json=MOCK_REQ)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_id"] == "mock_paths_001"
    assert body["backend"] == "mock"  # actual backend, not "auto"
    assert body["created_at"] is not None
    assert len(body["paths"]) >= 2

    result_file = (
        Path(root) / "sim_test.sionnatwin" / "results" / "mock_paths_001.json"
    )
    assert result_file.is_file()

    # Second run increments the sequence number.
    resp2 = client.post("/api/projects/sim_test/simulate/paths", json=MOCK_REQ)
    assert resp2.status_code == 200
    assert resp2.json()["result_id"] == "mock_paths_002"

    # Latest = last ref of the kind.
    latest = client.get("/api/projects/sim_test/results/paths")
    assert latest.status_code == 200
    assert latest.json()["result_id"] == "mock_paths_002"

    by_id = client.get(
        "/api/projects/sim_test/results/paths", params={"result_id": "mock_paths_001"}
    )
    assert by_id.status_code == 200
    assert by_id.json() == client.get(
        "/api/projects/sim_test/results/paths",
        params={"result_id": "mock_paths_001"},
    ).json()
    assert by_id.json()["result_id"] == "mock_paths_001"

    unknown = client.get(
        "/api/projects/sim_test/results/paths", params={"result_id": "nope"}
    )
    assert unknown.status_code == 404

    # Scene now records both refs.
    from app.api.deps import get_store

    refs = get_store().load_scene("sim_test").result_sets
    assert [r.result_id for r in refs] == ["mock_paths_001", "mock_paths_002"]
    assert all(r.kind == "paths" for r in refs)


def test_api_radio_map_roundtrip_and_missing_result(api_client):
    client, _root = api_client

    # No radio map results yet -> 404.
    assert client.get("/api/projects/sim_test/results/radio-map").status_code == 404

    resp = client.post("/api/projects/sim_test/simulate/radio-map", json=MOCK_REQ)
    assert resp.status_code == 200
    body = resp.json()
    assert body["result_id"] == "mock_radio_map_001"
    assert body["kind"] == "radio_map"

    fetched = client.get("/api/projects/sim_test/results/radio-map")
    assert fetched.status_code == 200
    assert fetched.json()["result_id"] == "mock_radio_map_001"


def test_api_unknown_project_404(api_client):
    client, _root = api_client
    assert client.post("/api/projects/nope/simulate/paths", json={}).status_code == 404
    assert client.get("/api/projects/nope/results/paths").status_code == 404


def test_api_unknown_config_id_404(api_client):
    client, _root = api_client
    resp = client.post(
        "/api/projects/sim_test/simulate/paths", json={"config_id": "missing_cfg"}
    )
    assert resp.status_code == 404


@pytest.mark.skipif(SIONNA_INSTALLED, reason="sionna installed: named request runs, no 409")
def test_api_named_sionna_backend_409_when_absent(api_client):
    client, _root = api_client
    resp = client.post(
        "/api/projects/sim_test/simulate/paths",
        json={"config": {"backend": "sionna"}},
    )
    assert resp.status_code == 409
