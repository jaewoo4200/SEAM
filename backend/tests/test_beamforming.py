"""WS4: MIMO beamforming gain (mock stub always; real Sionna when installed)."""

from pathlib import Path

import pytest
import trimesh

from app.schemas.materials import AssignRequest
from app.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import BeamformingRequest, SimulationConfig
from app.services.availability import sionna_available
from app.services.material_assignment import assign_materials
from app.services.project_store import load_default_library
from app.services.simulation_backends.mock_backend import MockBackend
from app.services.simulation_backends.sionna_backend import SionnaBackend


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
