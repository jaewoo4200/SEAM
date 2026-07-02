"""Task D: measurement-based calibration + material out-of-band guardrail."""

from pathlib import Path

import pytest

from app.schemas.calibration import CalibrationRequest, MeasurementSample
from app.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import SimulationConfig
from app.services.calibration import calibrate_material
from app.services.project_store import load_default_library
from app.services.scene_validator import validate_scene
from app.services.simulation_backends.mock_backend import MockBackend


def _scene(freq_hz: float = 28e9) -> Scene:
    return Scene(
        scene_id="cal",
        name="cal",
        prims=[
            Prim(
                id="/ground",
                name="ground",
                semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",  # ITU medium_dry_ground
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 10.0], power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=[20.0, 0.0, 1.5]),
        ],
        simulation_configs=[SimulationConfig(id="default", frequency_hz=freq_hz)],
    )


# --------------------------------------------------- out-of-band guardrail


def test_itu_ground_flagged_above_10ghz():
    scene = _scene(freq_hz=28e9)
    report = validate_scene(scene, load_default_library())
    codes = {i.code for i in report.issues}
    assert "MATERIAL_OUT_OF_BAND" in codes
    oob = next(i for i in report.issues if i.code == "MATERIAL_OUT_OF_BAND")
    assert oob.severity == "warning" and "ground_28ghz" in oob.message


def test_itu_ground_ok_below_10ghz():
    scene = _scene(freq_hz=3.5e9)
    report = validate_scene(scene, load_default_library())
    assert "MATERIAL_OUT_OF_BAND" not in {i.code for i in report.issues}


# ------------------------------------------------------------ calibration


def test_calibration_offset_and_report(tmp_path: Path):
    """Feed measured = mock-simulated + a constant 7 dB offset; calibration
    must recover the offset and drive residual RMSE to ~0."""
    scene = _scene()
    library = load_default_library()
    config = SimulationConfig(id="default", backend="mock", frequency_hz=28e9)
    backend = MockBackend()

    positions = [[10.0, 0.0, 1.5], [25.0, 5.0, 1.5], [40.0, -5.0, 1.5]]
    # First get the mock's own path gains to build consistent synthetic measurements.
    base_req = CalibrationRequest(
        config=config,
        measurements=[MeasurementSample(rx_position=p, measured_path_gain_db=0.0) for p in positions],
        target_material_id="ground",
        param="scattering_coefficient",
    )
    from app.services.calibration import _simulate_path_gains

    sim = _simulate_path_gains(backend, tmp_path, scene, library, config, base_req)
    assert all(s is not None for s in sim)
    measurements = [
        MeasurementSample(rx_position=p, measured_path_gain_db=s + 7.0)  # type: ignore[operator]
        for p, s in zip(positions, sim)
    ]

    report = calibrate_material(
        backend, tmp_path, scene, library, config,
        CalibrationRequest(config=config, measurements=measurements,
                           target_material_id="ground", param="scattering_coefficient"),
    )
    assert report.before.n_links == 3
    # The level offset absorbs the 7 dB; residual RMSE is ~0.
    assert report.before.level_offset_db == pytest.approx(7.0, abs=0.2)
    assert report.after.rmse_db < 0.5
    assert len(report.grid_values) == len(report.grid_rmse_db)
    assert report.fitted_value is not None


def test_calibration_unknown_material_raises(tmp_path: Path):
    scene = _scene()
    with pytest.raises(ValueError):
        calibrate_material(
            MockBackend(), tmp_path, scene, load_default_library(),
            SimulationConfig(backend="mock"),
            CalibrationRequest(
                measurements=[MeasurementSample(rx_position=[1, 0, 1.5], measured_path_gain_db=-90.0)],
                target_material_id="does_not_exist",
            ),
        )


def test_api_calibration_and_apply(api_client):
    from app.api.deps import get_store

    store = get_store()
    store.create_project("Cal API", project_id="calapi")
    store.save_scene("calapi", _scene())

    P = "/api/projects/calapi"
    meas = [
        {"rx_position": [12.0, 0.0, 1.5], "measured_path_gain_db": -70.0},
        {"rx_position": [30.0, 0.0, 1.5], "measured_path_gain_db": -80.0},
    ]
    resp = api_client.post(
        f"{P}/calibrate/materials",
        json={"config": {"backend": "mock"}, "measurements": meas,
              "target_material_id": "ground", "param": "scattering_coefficient", "apply": True},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["applied"] is True
    assert report["before"]["n_links"] == 2

    # Applied -> prims using 'ground' are promoted to measurement_calibrated.
    scene = store.load_scene("calapi")
    ground_prim = next(p for p in scene.prims if p.rf.material_id == "ground")
    assert ground_prim.rf.assignment_status == "measurement_calibrated"
    assert "calibration" in ground_prim.rf.assignment_sources
