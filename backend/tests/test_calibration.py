"""Task D: measurement-based calibration + material out-of-band guardrail."""

from pathlib import Path

import pytest

from app.schemas.calibration import (
    CalibrationRequest,
    DisambiguationRequest,
    MeasurementSample,
)
from app.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import SimulationConfig
from app.services.calibration import calibrate_material, disambiguate_materials
from app.services.project_store import load_default_library
from app.services.scene_validator import validate_scene
from app.services.simulation_backends.mock_backend import MockBackend


def _scene(freq_hz: float = 28e9, with_wall: bool = False) -> Scene:
    prims = [
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
    ]
    if with_wall:
        prims.append(
            Prim(
                id="/wall",
                name="wall",
                semantic_tags=["building", "wall"],
                mesh_ref=MeshRef(mesh_name="wall"),
                transform={"translation": [10.0, 5.0, 0.0]},
                rf=RFBinding(
                    material_id="asphalt_custom",  # constant-model wall
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            )
        )
    return Scene(
        scene_id="cal",
        name="cal",
        prims=prims,
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
    # The sweep must actually move the prediction (regression for the no-op
    # bug where trial materials never reached the solver).
    finite = [r for r in report.grid_rmse_db if r is not None]
    assert len(set(round(r, 9) for r in finite)) > 1, "grid sweep had no effect"
    assert not any("did not change" in w for w in report.warnings)


def test_calibration_recovers_true_scattering(tmp_path: Path):
    """Measurements generated at S=0.1 while the library says S=0.3: the grid
    fit must pick ~0.1 and report a real improvement."""
    scene = _scene()
    library = load_default_library()
    config = SimulationConfig(id="default", backend="mock", frequency_hz=28e9)
    backend = MockBackend()
    positions = [[10.0, 0.0, 1.5], [25.0, 5.0, 1.5], [40.0, -5.0, 1.5]]

    from app.services.calibration import _simulate_path_gains

    truth_lib = library.model_copy(deep=True)
    truth_lib.get("ground").scattering_coefficient = 0.1
    req0 = CalibrationRequest(
        config=config,
        measurements=[MeasurementSample(rx_position=p, measured_path_gain_db=0.0) for p in positions],
        target_material_id="ground",
    )
    truth = _simulate_path_gains(backend, tmp_path, scene, truth_lib, config, req0)
    measurements = [
        MeasurementSample(rx_position=p, measured_path_gain_db=t)  # type: ignore[arg-type]
        for p, t in zip(positions, truth)
    ]

    report = calibrate_material(
        backend, tmp_path, scene, library, config,
        CalibrationRequest(config=config, measurements=measurements,
                           target_material_id="ground", param="scattering_coefficient"),
    )
    assert report.fitted_value == pytest.approx(0.1, abs=1e-9)
    assert report.after.rmse_db < report.before.rmse_db


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


def test_api_calibration_and_apply(api_client, tmp_path):
    from app.api.deps import get_store
    from app.services.calibration import _simulate_path_gains

    store = get_store()
    store.create_project("Cal API", project_id="calapi")
    store.save_scene("calapi", _scene(with_wall=True))

    # Build measurements from a 'true' scattering of 0.1 (library ships 0.3).
    # LoS is disabled and a second (wall) bounce with a different material is
    # present, so the ground scattering change is identifiable per link (a
    # LoS-dominated link hides it below the apply gate).
    positions = [[12.0, 0.0, 1.5], [30.0, 0.0, 1.5], [45.0, 5.0, 1.5]]
    config = SimulationConfig(id="default", backend="mock", frequency_hz=28e9, los=False)
    truth_lib = load_default_library().model_copy(deep=True)
    truth_lib.get("ground").scattering_coefficient = 0.1
    req0 = CalibrationRequest(
        config=config,
        measurements=[MeasurementSample(rx_position=p, measured_path_gain_db=0.0) for p in positions],
        target_material_id="ground",
    )
    truth = _simulate_path_gains(
        MockBackend(), tmp_path, _scene(with_wall=True), truth_lib, config, req0
    )
    meas = [
        {"rx_position": p, "measured_path_gain_db": t}
        for p, t in zip(positions, truth)
    ]

    P = "/api/projects/calapi"
    resp = api_client.post(
        f"{P}/calibrate/materials",
        json={"config": {"backend": "mock", "los": False}, "measurements": meas,
              "target_material_id": "ground", "param": "scattering_coefficient", "apply": True},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["applied"] is True
    assert report["before"]["n_links"] == 3
    assert report["fitted_value"] == pytest.approx(0.1)
    assert report["after"]["rmse_db"] < report["before"]["rmse_db"]

    # Applied -> library updated + prims promoted to measurement_calibrated.
    assert store.load_materials("calapi").get("ground").scattering_coefficient == pytest.approx(0.1)
    scene = store.load_scene("calapi")
    ground_prim = next(p for p in scene.prims if p.rf.material_id == "ground")
    assert ground_prim.rf.assignment_status == "measurement_calibrated"
    assert "calibration" in ground_prim.rf.assignment_sources


def test_api_apply_gate_refuses_without_improvement(api_client):
    """Measurements already consistent with the baseline: apply must be
    refused (no meaningful RMSE improvement) and nothing persisted."""
    from app.api.deps import get_store
    from app.services.calibration import _simulate_path_gains

    store = get_store()
    store.create_project("Cal Gate", project_id="calgate")
    store.save_scene("calgate", _scene())

    positions = [[12.0, 0.0, 1.5], [30.0, 0.0, 1.5]]
    config = SimulationConfig(id="default", backend="mock", frequency_hz=28e9)
    req0 = CalibrationRequest(
        config=config,
        measurements=[MeasurementSample(rx_position=p, measured_path_gain_db=0.0) for p in positions],
        target_material_id="ground",
    )
    from pathlib import Path as _P
    import tempfile

    base = _simulate_path_gains(
        MockBackend(), _P(tempfile.mkdtemp()), _scene(), load_default_library(), config, req0
    )
    meas = [
        {"rx_position": p, "measured_path_gain_db": b}
        for p, b in zip(positions, base)
    ]
    resp = api_client.post(
        "/api/projects/calgate/calibrate/materials",
        json={"config": {"backend": "mock"}, "measurements": meas,
              "target_material_id": "ground", "param": "scattering_coefficient", "apply": True},
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()
    assert report["applied"] is False
    assert any("apply skipped" in w for w in report["warnings"])
    scene = store.load_scene("calgate")
    ground_prim = next(p for p in scene.prims if p.rf.material_id == "ground")
    assert ground_prim.rf.assignment_status == "user_confirmed"  # unchanged


# ------------------------------------------------------ RF disambiguation
#
# The RF-sensing disambiguation step (Dai et al., JSTEAP 2025): rank candidate
# materials for a prim by how well re-simulating the measured links with each
# candidate matches the measurements. The MOCK backend is *material-blind* for
# ITU frequency-dependent entries — its reflection loss only adds the
# scattering term (10*S) and never the eps/sigma that separate real glass
# types — so two ITU candidates with equal S produce identical predictions.
# These tests exercise the service on that mock and therefore assert the
# indistinguishable-warning path, not a physical winner (the real separation is
# verified live on the Sionna backend).


def _disambig_measurements(tmp_path: Path, config: SimulationConfig,
                           positions: list[list[float]]) -> list[MeasurementSample]:
    """Synthetic measurements = the mock's own path gains at `positions` for
    the scene as-shipped, so every candidate produces comparable links."""
    scene = _scene(with_wall=True)
    library = load_default_library()
    from app.services.calibration import _simulate_path_gains

    req0 = CalibrationRequest(
        config=config,
        measurements=[MeasurementSample(rx_position=p, measured_path_gain_db=0.0) for p in positions],
        target_material_id="ground",
    )
    sim = _simulate_path_gains(MockBackend(), tmp_path, scene, library, config, req0)
    assert all(s is not None for s in sim)
    return [
        MeasurementSample(rx_position=p, measured_path_gain_db=s)  # type: ignore[arg-type]
        for p, s in zip(positions, sim)
    ]


def test_disambiguation_indistinguishable_on_mock(tmp_path: Path):
    """Two ITU candidates with equal scattering (itu_concrete/itu_wood, both
    S=0.2) bound to the wall prim: both score with n_links>0 and identical
    RMSE, so the mock cannot separate them — expect the exact indistinguishable
    warning and best_material_id None (not an arbitrary pick)."""
    scene = _scene(with_wall=True)
    library = load_default_library()
    config = SimulationConfig(id="default", backend="mock", frequency_hz=28e9)
    positions = [[12.0, 0.0, 1.5], [30.0, 0.0, 1.5], [45.0, 5.0, 1.5]]
    measurements = _disambig_measurements(tmp_path, config, positions)

    report = disambiguate_materials(
        MockBackend(), tmp_path, scene, library, config,
        DisambiguationRequest(
            config=config,
            prim_ids=["/wall"],
            candidate_material_ids=["itu_concrete", "itu_wood"],
            measurements=measurements,
        ),
    )
    assert report.backend == "mock"
    assert {c.material_id for c in report.candidates} == {"itu_concrete", "itu_wood"}
    assert all(c.n_links > 0 for c in report.candidates)
    assert all(c.rmse_db is not None for c in report.candidates)
    # Material-blind mock => identical fit => flat RMSE spread => no winner.
    assert report.best_material_id is None
    assert any(
        "indistinguishable at these positions" in w for w in report.warnings
    )


def test_disambiguation_unknown_prim_raises(tmp_path: Path):
    scene = _scene(with_wall=True)
    with pytest.raises(ValueError):
        disambiguate_materials(
            MockBackend(), tmp_path, scene, load_default_library(),
            SimulationConfig(backend="mock"),
            DisambiguationRequest(
                prim_ids=["/does_not_exist"],
                candidate_material_ids=["itu_concrete", "itu_wood"],
                measurements=[
                    MeasurementSample(rx_position=[12.0, 0.0, 1.5], measured_path_gain_db=-90.0)
                ],
            ),
        )


def test_disambiguation_unknown_candidate_skipped(tmp_path: Path):
    """An unknown candidate material is skipped with a warning; the remaining
    (known) candidate is still scored and, being the only scored entry, wins."""
    scene = _scene(with_wall=True)
    library = load_default_library()
    config = SimulationConfig(id="default", backend="mock", frequency_hz=28e9)
    positions = [[12.0, 0.0, 1.5], [30.0, 0.0, 1.5]]
    measurements = _disambig_measurements(tmp_path, config, positions)

    report = disambiguate_materials(
        MockBackend(), tmp_path, scene, library, config,
        DisambiguationRequest(
            config=config,
            prim_ids=["/wall"],
            candidate_material_ids=["does_not_exist", "itu_concrete"],
            measurements=measurements,
        ),
    )
    assert any("unknown candidate material skipped: does_not_exist" in w for w in report.warnings)
    # Only the known candidate is scored (the bogus one never becomes a row).
    assert [c.material_id for c in report.candidates] == ["itu_concrete"]
    scored = next(c for c in report.candidates if c.material_id == "itu_concrete")
    assert scored.n_links > 0 and scored.rmse_db is not None
    # A single scored candidate skips the flat-spread guard, so it is the best.
    assert report.best_material_id == "itu_concrete"


# --------------------------------------------- measurement CSV import + GET


def _create_cal_project(api_client, pid: str = "measproj") -> None:
    from app.api.deps import get_store

    store = get_store()
    store.create_project("Meas API", project_id=pid)
    store.save_scene(pid, _scene())


def test_measurement_id_roundtrips_on_sample():
    from app.schemas.calibration import MeasurementSample

    s = MeasurementSample(
        measurement_id="m-42", rx_position=[1.0, 2.0, 1.5], measured_path_gain_db=-95.0
    )
    assert s.measurement_id == "m-42"
    again = MeasurementSample.model_validate_json(s.model_dump_json())
    assert again.measurement_id == "m-42"
    # Optional: omitting it defaults to None.
    assert MeasurementSample(rx_position=[0, 0, 1.5], measured_path_gain_db=-90.0).measurement_id is None


def test_import_measurements_csv_happy_and_get(api_client):
    _create_cal_project(api_client, "measok")
    csv_text = (
        "measurement_id,x,y,z,tx_id,measured_path_gain_db\n"
        "m1,10.0,0.0,1.5,tx_001,-92.3\n"
        "m2,25.0,5.0,1.5,tx_001,-101.0\n"
    )
    resp = api_client.post(
        "/api/projects/measok/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == 0
    assert len(body["measurements"]) == 2
    m1 = body["measurements"][0]
    assert m1["measurement_id"] == "m1"
    assert m1["rx_position"] == [10.0, 0.0, 1.5]
    assert m1["tx_id"] == "tx_001"
    assert m1["measured_path_gain_db"] == pytest.approx(-92.3)

    # GET re-parses the persisted raw CSV and returns the same rows.
    got = api_client.get("/api/projects/measok/calibrate/measurements")
    assert got.status_code == 200, got.text
    assert len(got.json()["measurements"]) == 2
    assert got.json()["measurements"][1]["measurement_id"] == "m2"


def test_import_measurements_csv_skips_bad_rows(api_client):
    _create_cal_project(api_client, "measbad")
    csv_text = (
        "measurement_id,x,y,z,measured_path_gain_db\n"
        "good,10.0,0.0,1.5,-92.3\n"
        "no_gain,25.0,5.0,1.5,\n"          # missing gain -> skipped
        "bad_number,not_a_float,0.0,1.5,-90.0\n"  # unparseable coord -> skipped
        "missing_col,5.0,1.5,-88.0\n"      # too few columns -> skipped
    )
    resp = api_client.post(
        "/api/projects/measbad/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["measurements"]) == 1
    assert body["measurements"][0]["measurement_id"] == "good"
    assert body["skipped"] == 3
    assert any("skipped 3" in w for w in body["warnings"])


def test_import_measurements_csv_alias_headers(api_client):
    """rx_x/rx_y/rx_z position columns and rsrp_dbm gain alias are accepted."""
    _create_cal_project(api_client, "measalias")
    csv_text = (
        "rx_x,rx_y,rx_z,rsrp_dbm\n"
        "12.0,0.0,1.5,-70.5\n"
        "30.0,0.0,1.5,-80.0\n"
    )
    resp = api_client.post(
        "/api/projects/measalias/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == 0
    assert len(body["measurements"]) == 2
    assert body["measurements"][0]["rx_position"] == [12.0, 0.0, 1.5]
    assert body["measurements"][0]["measured_path_gain_db"] == pytest.approx(-70.5)
    # No explicit id column -> measurement_id is None.
    assert body["measurements"][0]["measurement_id"] is None


def test_get_measurements_404_when_none_imported(api_client):
    _create_cal_project(api_client, "measempty")
    resp = api_client.get("/api/projects/measempty/calibrate/measurements")
    assert resp.status_code == 404


def test_import_measurements_csv_unknown_project_404(api_client):
    resp = api_client.post(
        "/api/projects/nope/calibrate/measurements/import-csv",
        json={"csv_text": "x,y,z,measured_path_gain_db\n1,2,1.5,-90\n"},
    )
    assert resp.status_code == 404
