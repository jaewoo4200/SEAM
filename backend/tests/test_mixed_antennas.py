"""Mixed antenna configurations (F09).

Sionna has one tx_array and one rx_array per scene, so the first selected
TX's (RX's) antenna applies to every TX (RX): the solve says so when the
selected devices differ. The mock's sensing echo applies each device's own
element pattern and orientation instead of isotropic antennas.
"""

from __future__ import annotations

import math
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest
import trimesh

from seam_studio.schemas.devices import Antenna, Device
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.sensing import ResolvedSensingTarget
from seam_studio.services.sensing_coverage import element_gain_db
from seam_studio.services.simulation_backends import sionna_backend
from seam_studio.services.simulation_backends.mock_backend import MockBackend

from .conftest import requires_sionna

C = 299_792_458.0


def _dev(dev_id: str, kind: str, antenna: Antenna | None = None, pos=(0.0, 0.0, 10.0)) -> Device:
    return Device(id=dev_id, kind=kind, position=list(pos), antenna=antenna or Antenna())


@pytest.fixture()
def fake_arrays(monkeypatch):
    built: list[Antenna] = []

    def fake(antenna, warnings, **kw):
        built.append(antenna)
        return SimpleNamespace(antenna=antenna)

    monkeypatch.setattr(sionna_backend, "_make_planar_array", fake)
    return built


def test_apply_arrays_warns_once_naming_the_differing_devices(fake_arrays):
    tr = Antenna(pattern="tr38901", num_rows=4, num_cols=4)
    txs = [_dev("tx_a", "tx", tr), _dev("tx_b", "tx", tr.model_copy()), _dev("tx_c", "tx"),
           _dev("tx_d", "tx", tr.model_copy(update={"polarization": "cross"}))]
    two_rows = Antenna(num_rows=2)
    rxs = [_dev("rx_a", "rx", two_rows), _dev("rx_b", "rx", Antenna(num_rows=2, vertical_spacing=0.7))]
    rt_scene = SimpleNamespace()
    warnings: list[str] = []
    sionna_backend._apply_arrays(rt_scene, txs, rxs, warnings)
    assert rt_scene.tx_array.antenna == tr and rt_scene.rx_array.antenna == two_rows
    assert warnings == [
        "sionna applies tx antenna of 'tx_a' to all selected TXs (scene-level array); "
        "differing antennas on tx_c, tx_d are not individually honored",
        "sionna applies rx antenna of 'rx_a' to all selected RXs (scene-level array); "
        "differing antennas on rx_b are not individually honored",
    ]


def test_apply_arrays_is_quiet_for_identical_antennas_and_radio_map_receivers(fake_arrays):
    same = [_dev("tx_a", "tx"), _dev("tx_b", "tx")]
    warnings: list[str] = []
    sionna_backend._apply_arrays(SimpleNamespace(), same, [_dev("rx_a", "rx")], warnings)
    assert warnings == []
    # The radio map ignores receivers, so only a TX mismatch is reported there.
    mixed_rx = [_dev("rx_a", "rx"), _dev("rx_b", "rx", Antenna(pattern="dipole"))]
    sionna_backend._apply_arrays(SimpleNamespace(), same, mixed_rx, warnings, warn_rx=False)
    assert warnings == []


def test_apply_arrays_ignores_spacing_along_a_one_element_axis(fake_arrays):
    # Spacing places nothing on a 1-element axis: two 1x1 devices (or a 1x4 row)
    # differing only there get exactly what they specify from the scene array.
    single = [_dev("tx_a", "tx"), _dev("tx_b", "tx", Antenna(vertical_spacing=0.7,
                                                               horizontal_spacing=0.6))]
    row = [_dev("rx_a", "rx", Antenna(num_cols=4)),
           _dev("rx_b", "rx", Antenna(num_cols=4, vertical_spacing=0.9))]
    warnings: list[str] = []
    sionna_backend._apply_arrays(SimpleNamespace(), single, row, warnings)
    assert warnings == []


def test_mock_echo_applies_each_devices_element_gain():
    config = SimulationConfig(backend="mock", frequency_hz=3.5e9)
    lam = C / config.frequency_hz
    target = ResolvedSensingTarget(
        actor_id="a", model="constant", object_type=None, model_type=None, rcs_dbsm=5.0,
        xpr_db=None, random_components=False, size_m=(1, 1, 1), center=(40.0, 20.0, 30.0),
        orientation_deg=(0, 0, 0), velocity_m_s=(0, 0, 0),
    )
    tx_iso, rx_iso = _dev("t", "tx"), _dev("r", "rx", pos=(60.0, -10.0, 5.0))
    tx_tr = tx_iso.model_copy(
        update={"antenna": Antenna(pattern="tr38901"), "orientation_deg": [25.0, 0.0, 0.0]}
    )
    rx_tr = rx_iso.model_copy(
        update={"antenna": Antenna(pattern="tr38901"), "orientation_deg": [160.0, 0.0, 0.0]}
    )
    iso = MockBackend._sensing_path(1, tx_iso, target, rx_iso, config, lam)
    tr = MockBackend._sensing_path(1, tx_tr, target, rx_tr, config, lam)
    sp = np.array([target.center])
    gains = (
        element_gain_db(tx_tr, sp - np.array(tx_tr.position))[0]
        + element_gain_db(rx_tr, sp - np.array(rx_tr.position))[0]
    )
    assert abs(gains) > 1.0
    assert tr.power_dbm == pytest.approx(iso.power_dbm + gains, abs=1e-9)
    assert tr.delay_ns == iso.delay_ns and tr.doppler_hz == iso.doppler_hz


@requires_sionna
def test_sionna_paths_solve_warns_about_mixed_tx_antennas(tmp_path: Path, monkeypatch):
    from seam_studio.services.project_store import load_default_library

    monkeypatch.setattr(
        sionna_backend, "_solves_since_full_flush", sionna_backend._solves_since_full_flush
    )
    project = tmp_path / "mixed.seam"
    (project / "visual").mkdir(parents=True)
    (project / "rf").mkdir()
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(80.0, 80.0, 0.2))
    ground.apply_translation((0.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    (project / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))
    scene = Scene(
        scene_id="mixed",
        prims=[
            Prim(
                id="/ground", name="ground", semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(material_id="ground", assignment_status="user_confirmed",
                             assignment_sources=["user"]),
            )
        ],
        devices=[
            _dev("tx_a", "tx", Antenna(pattern="tr38901")),
            _dev("tx_b", "tx", pos=(10.0, 0.0, 10.0)),
            _dev("rx_a", "rx", pos=(30.0, 5.0, 1.5)),
        ],
    )
    config = SimulationConfig(backend="sionna", frequency_hz=3.5e9, max_depth=1,
                              num_samples=100_000)
    result = sionna_backend.SionnaBackend().simulate_paths(
        project, scene, load_default_library(), config
    )
    assert result.paths, result.warnings
    hits = [w for w in result.warnings if w.startswith("sionna applies tx antenna of 'tx_a'")]
    assert hits == [
        "sionna applies tx antenna of 'tx_a' to all selected TXs (scene-level array); "
        "differing antennas on tx_b are not individually honored"
    ]
    assert not any("rx antenna" in w for w in result.warnings)
    assert math.isfinite(result.paths[0].power_dbm)
