"""WS4: MIMO beamforming gain (mock stub always; real Sionna when installed).

Also covers the route contract: explicit tx_id/rx_id that don't resolve must
400 instead of silently falling back to the first device (audit M3).
"""

from pathlib import Path

import pytest
import trimesh
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seam_studio.api import deps
from seam_studio.api import simulate as simulate_api
from seam_studio.core.config import get_settings
from seam_studio.schemas.materials import AssignRequest
from seam_studio.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import BeamformingRequest, SimulationConfig
from seam_studio.services.availability import sionna_available
from seam_studio.services.material_assignment import assign_materials
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend


def _scene() -> Scene:
    return Scene(
        scene_id="bf",
        name="BF",
        prims=[
            Prim(
                id="/wall",
                name="wall",
                semantic_tags=["building", "wall"],
                mesh_ref=MeshRef(mesh_name="wall"),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 8.0], power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=[15.0, 0.0, 1.5]),
        ],
    )


def test_mock_beamforming_stub_scales_with_array(tmp_path: Path):
    scene = _scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock")

    r1 = MockBackend().simulate_beamforming(
        tmp_path, scene, library, cfg, BeamformingRequest(tx_rows=1, tx_cols=1, rx_rows=1, rx_cols=1)
    )
    r16 = MockBackend().simulate_beamforming(
        tmp_path, scene, library, cfg, BeamformingRequest(tx_rows=4, tx_cols=4, rx_rows=4, rx_cols=4)
    )
    assert r1.backend == "mock"
    assert r1.tx_mrt_gain_db == pytest.approx(0.0, abs=1e-6)  # 10log10(1) = 0
    assert r16.tx_mrt_gain_db == pytest.approx(12.0412, abs=0.01)  # 10log10(16)
    assert r16.svd_gain_db > r16.tx_mrt_gain_db  # both-ends adds RX gain
    assert r16.single_element_dbm is not None


# ------------------------------------------------------ route contract (M3)


@pytest.fixture()
def bf_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient over the simulate router with one project holding _scene()."""
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    store = deps.get_store()
    store.create_project("BF Test", project_id="bf_test")
    store.save_scene("bf_test", _scene())
    app = FastAPI()
    app.include_router(simulate_api.router, prefix="/api")
    try:
        yield TestClient(app)
    finally:
        get_settings.cache_clear()
        deps.get_store.cache_clear()


def test_beamforming_route_valid_ids(bf_client):
    resp = bf_client.post(
        "/api/projects/bf_test/simulate/beamforming",
        json={
            "config": {"backend": "mock"},
            "tx_id": "tx_001",
            "rx_id": "rx_001",
            "tx_rows": 4, "tx_cols": 4, "rx_rows": 4, "rx_cols": 4,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "mock"
    assert body["tx_mrt_gain_db"] == pytest.approx(12.0412, abs=0.01)


def test_beamforming_route_unknown_ids_400(bf_client):
    # A typo'd tx_id used to silently fall back to devices[0] and return
    # plausible-but-wrong numbers (audit M3). Now it must 400.
    resp = bf_client.post(
        "/api/projects/bf_test/simulate/beamforming",
        json={"config": {"backend": "mock"}, "tx_id": "ghost_tx"},
    )
    assert resp.status_code == 400
    assert "tx device not found: ghost_tx" in resp.json()["detail"]

    resp = bf_client.post(
        "/api/projects/bf_test/simulate/beamforming",
        json={"config": {"backend": "mock"}, "rx_id": "ghost_rx"},
    )
    assert resp.status_code == 400
    assert "rx device not found: ghost_rx" in resp.json()["detail"]

    # Wrong kind: an rx id passed as tx_id must also 400, not "work".
    resp = bf_client.post(
        "/api/projects/bf_test/simulate/beamforming",
        json={"config": {"backend": "mock"}, "tx_id": "rx_001"},
    )
    assert resp.status_code == 400


@pytest.mark.skipif(not sionna_available(), reason="sionna-rt not installed")
def test_sionna_beamforming_real_gain(tmp_path: Path):
    proj = tmp_path / "bf.sionnatwin"
    (proj / "visual").mkdir(parents=True)
    (proj / "rf").mkdir()
    tm = trimesh.Scene()
    # Wall off the y=0 TX->RX axis so the line of sight stays clear.
    wall = trimesh.creation.box(extents=(0.3, 10.0, 8.0))
    wall.apply_translation((8.0, 7.0, 4.0))
    tm.add_geometry(wall, geom_name="wall", node_name="wall")
    (proj / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))

    scene = _scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", frequency_hz=28e9, max_depth=2, num_samples=200_000)
    bf = SionnaBackend()

    r1 = bf.simulate_beamforming(
        proj, scene, library, cfg, BeamformingRequest(tx_rows=1, tx_cols=1, rx_rows=1, rx_cols=1)
    )
    r4 = bf.simulate_beamforming(
        proj, scene, library, cfg, BeamformingRequest(tx_rows=4, tx_cols=4, rx_rows=4, rx_cols=4)
    )
    assert r4.backend == "sionna"
    assert r4.num_paths > 0, f"expected paths; warnings={r4.warnings}"
    assert r1.single_element_dbm is not None and r4.single_element_dbm is not None
    # A single element gives ~0 dB beamforming gain.
    assert abs(r1.tx_mrt_gain_db) < 1.0
    # 4x4 (16-element) TX-MRT approaches 10*log10(16) = 12 dB; both-ends SVD
    # adds RX array gain on top (roughly double, ~24 dB).
    assert 8.0 < r4.tx_mrt_gain_db < 14.0, r4.tx_mrt_gain_db
    assert r4.svd_gain_db > r4.tx_mrt_gain_db + 5.0
