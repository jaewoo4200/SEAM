"""Sionna RT backend integration tests.

Skipped entirely when sionna-rt is not installed (the app's core contract:
Sionna is optional). When installed, these exercise the real Dr.Jit/Mitsuba
solver on a tiny scene and assert results normalize into the shared schema.
"""

from pathlib import Path

import pytest
import trimesh

from app.schemas.materials import AssignRequest
from app.schemas.results import PathResultSet, RadioMapResultSet
from app.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import RadioMapGridConfig, SimulationConfig
from app.services.availability import sionna_available
from app.services.material_assignment import assign_materials
from app.services.project_store import load_default_library
from app.services.simulation_backends.sionna_backend import SionnaBackend

pytestmark = pytest.mark.skipif(
    not sionna_available(), reason="sionna-rt not installed (optional backend)"
)


def _demo_scene() -> Scene:
    return Scene(
        scene_id="sionna_it",
        name="Sionna IT",
        prims=[
            Prim(
                id="/ground",
                name="ground",
                semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
            Prim(
                id="/wall",
                name="wall",
                semantic_tags=["building", "wall"],
                mesh_ref=MeshRef(mesh_name="wall"),
                rf=RFBinding(
                    material_id="asphalt_custom",  # a constant (non-ITU) material
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


@pytest.fixture()
def project(tmp_path: Path) -> Path:
    proj = tmp_path / "sionna_it.sionnatwin"
    (proj / "visual").mkdir(parents=True)
    (proj / "rf").mkdir()
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(60.0, 60.0, 0.2))
    ground.apply_translation((0.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    # Wall set off the tx->rx axis (which runs along y=0) so the line of
    # sight stays clear; it can still serve as a reflector.
    wall = trimesh.creation.box(extents=(0.3, 10.0, 8.0))
    wall.apply_translation((8.0, 6.0, 4.0))
    tm.add_geometry(wall, geom_name="wall", node_name="wall")
    (proj / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))
    return proj


def test_sionna_available_true():
    assert SionnaBackend().is_available() is True


def test_sionna_simulate_paths_returns_los(project: Path):
    scene = _demo_scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", frequency_hz=3.5e9, max_depth=3, num_samples=200_000)

    result = SionnaBackend().simulate_paths(project, scene, library, cfg)

    assert result.backend == "sionna"
    assert result.metadata["engine"] == "sionna"
    # A clear line of sight exists tx->rx, so at least the LoS path is found.
    los = [p for p in result.paths if p.path_type == "los"]
    assert los, f"expected a LoS path; warnings={result.warnings}"
    # Free-space delay over ~16.9 m is ~56 ns; sanity bound well within [10, 200].
    assert 10.0 < los[0].delay_ns < 200.0
    assert -140.0 < los[0].power_dbm < 0.0
    # Any interaction must map to one of our canonical RF materials.
    for path in result.paths:
        for inter in path.interactions:
            assert inter.rf_material_id in {"ground", "asphalt_custom"}
    # Normalizes into the shared schema (what the API persists).
    PathResultSet.model_validate(result.model_dump())


def test_sionna_radio_map_populates_grid(project: Path):
    scene = _demo_scene()
    library = load_default_library()
    cfg = SimulationConfig(
        id="default",
        frequency_hz=3.5e9,
        max_depth=2,
        num_samples=200_000,
        radio_map=RadioMapGridConfig(cell_size_m=4.0, height_m=1.5, metric="rss_dbm"),
    )

    result = SionnaBackend().simulate_radio_map(project, scene, library, cfg)

    assert result.backend == "sionna"
    assert result.grid.nx > 1 and result.grid.ny > 1
    assert len(result.values) == result.grid.ny
    assert all(len(row) == result.grid.nx for row in result.values)
    populated = [v for row in result.values for v in row if v is not None]
    assert populated, f"expected some covered cells; warnings={result.warnings}"
    RadioMapResultSet.model_validate(result.model_dump())

def test_multi_antenna_los_matches_fspl(project: Path):
    """Audit regression (B2): multi-port arrays must NOT inflate per-path power.

    The old reduction summed raw power over every rx_ant x tx_ant port pair,
    making a 4x4->4x4 cross-pol link (16x32 = 512 ports) report a LOS path
    10*log10(512) ~= 27 dB STRONGER than free space. With the reference-element
    convention the LOS path power must equal FSPL + tx power within a small
    tolerance, and the complex amplitude must keep a real phase.
    """
    import math

    library = load_default_library()
    cfg = SimulationConfig(id="default", frequency_hz=3.5e9, max_depth=1, num_samples=200_000)

    def solve(rx_pol: str):
        scene = _demo_scene()
        for dev, pol in ((scene.devices[0], "V"), (scene.devices[1], rx_pol)):
            dev.antenna.pattern = "iso"
            dev.antenna.polarization = pol
            dev.antenna.num_rows = 4
            dev.antenna.num_cols = 4
        result = SionnaBackend().simulate_paths(project, scene, library, cfg)
        los = [p for p in result.paths if p.path_type == "los"]
        assert los, f"expected LoS ({rx_pol}); warnings={result.warnings}"
        return los[0], result

    base = _demo_scene()
    dist = math.dist(base.devices[0].position, base.devices[1].position)
    fspl_db = 20.0 * math.log10(4.0 * math.pi * dist * cfg.frequency_hz / 299_792_458.0)
    expected_dbm = 30.0 - fspl_db

    # V->V 4x4 arrays (256 port pairs): reference-element LOS gain == exact
    # FSPL. The old port-power summing would sit ~24 dB above this.
    los_vv, result_vv = solve("V")
    assert abs(los_vv.power_dbm - expected_dbm) < 2.0, (
        f"LOS {los_vv.power_dbm:.2f} dBm vs FSPL-expected {expected_dbm:.2f} dBm "
        f"(a ~24 dB gap means port-power summing regressed)"
    )
    # Phase must be preserved (the old sqrt(sum(power)) collapsed it to 0).
    assert any(abs(p.phase_rad) > 1e-6 for p in result_vv.paths), (
        "all path phases are exactly 0 - complex amplitude was lost"
    )

    # V->cross (lab_room's shape, 512 port pairs): the +/-45deg reference port
    # sees half the V-polarized power -> exactly a 3 dB polarization split
    # below FSPL, and nothing like the old +27 dB inflation.
    los_vx, _ = solve("cross")
    assert abs(los_vx.power_dbm - (expected_dbm - 3.01)) < 2.0, (
        f"cross-pol LOS {los_vx.power_dbm:.2f} dBm vs expected "
        f"{expected_dbm - 3.01:.2f} dBm (FSPL - 3 dB pol split)"
    )
