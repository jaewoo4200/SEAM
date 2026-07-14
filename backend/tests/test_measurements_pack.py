"""Measurement round-trip pack: time-ordered CSV import + measured-vs-
predicted trajectory validation (POST /calibrate/validate-trajectory).

Everything runs on the deterministic mock backend (GPU-free): synthetic
measurements are built from the mock's OWN trajectory predictions plus a known
level offset, so the endpoint must recover the offset exactly and its stats
must equal the calibration module's own alignment math on the same numbers.
"""

from pathlib import Path

import pytest

from seam_studio.schemas.calibration import MeasurementSample, TrajectoryValidationRequest
from seam_studio.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import SimulationConfig, TrajectorySimulateRequest
from seam_studio.services.calibration import _stats
from seam_studio.services.measurement_validation import order_measurements
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.trajectory import run_trajectory

POSITIONS = [[10.0, 0.0, 1.5], [25.0, 5.0, 1.5], [40.0, -5.0, 1.5], [55.0, 0.0, 1.5]]


def _scene() -> Scene:
    return Scene(
        scene_id="measpack",
        name="measpack",
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
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 10.0], power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=[20.0, 0.0, 1.5]),
        ],
        # backend pinned to mock so the API default config resolution never
        # depends on whether sionna is installed on the test machine.
        simulation_configs=[SimulationConfig(id="default", backend="mock", frequency_hz=28e9)],
    )


def _create_project(pid: str) -> None:
    from seam_studio.api.deps import get_store

    store = get_store()
    store.create_project("Meas Pack", project_id=pid)
    store.save_scene(pid, _scene())


def _mock_predicted_gains(tmp_path: Path, positions: list[list[float]]) -> list[float]:
    """The mock backend's own per-point path gains along ``positions``, via the
    SAME run_trajectory solve the endpoint replays (samples align 1:1)."""
    config = SimulationConfig(
        id="default", backend="mock", frequency_hz=28e9, tx_ids=["tx_001"]
    )
    result = run_trajectory(
        MockBackend(), tmp_path, _scene(), load_default_library(), config,
        TrajectorySimulateRequest(
            waypoints=[list(p) for p in positions],
            serving_tx_id="tx_001",
            num_points=min(max(len(positions), 2), 200),
        ),
    )
    gains = [s.path_gain_db for s in result.samples]
    assert all(g is not None for g in gains)
    return gains  # type: ignore[return-value]


# ------------------------------------------------------- time-ordered import


def test_time_s_roundtrips_on_sample():
    s = MeasurementSample(rx_position=[1.0, 2.0, 1.5], measured_path_gain_db=-90.0, time_s=3.25)
    assert MeasurementSample.model_validate_json(s.model_dump_json()).time_s == 3.25
    # Optional: omitting it defaults to None.
    assert MeasurementSample(rx_position=[0, 0, 1.5], measured_path_gain_db=-90.0).time_s is None


def test_import_csv_time_column_sorts_rows(api_client):
    _create_project("meastime")
    csv_text = (
        "measurement_id,time_s,x,y,z,measured_path_gain_db\n"
        "late,2.0,30.0,0.0,1.5,-100.0\n"
        "first,0.5,10.0,0.0,1.5,-92.0\n"
        "mid,1.25,20.0,0.0,1.5,-96.0\n"
    )
    resp = api_client.post(
        "/api/projects/meastime/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == 0
    assert [m["measurement_id"] for m in body["measurements"]] == ["first", "mid", "late"]
    assert [m["time_s"] for m in body["measurements"]] == [0.5, 1.25, 2.0]
    # GET re-parses the stored raw CSV with the same time ordering.
    got = api_client.get("/api/projects/meastime/calibrate/measurements")
    assert got.status_code == 200, got.text
    assert [m["measurement_id"] for m in got.json()["measurements"]] == ["first", "mid", "late"]


@pytest.mark.parametrize("alias", ["time", "t", "timestamp_s"])
def test_import_csv_time_header_aliases(api_client, alias):
    _create_project(f"measalias_{alias}")
    csv_text = (
        f"measurement_id,{alias},x,y,z,measured_path_gain_db\n"
        "b,9.0,30.0,0.0,1.5,-100.0\n"
        "a,1.0,10.0,0.0,1.5,-92.0\n"
    )
    resp = api_client.post(
        f"/api/projects/measalias_{alias}/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["measurement_id"] for m in body["measurements"]] == ["a", "b"]
    assert [m["time_s"] for m in body["measurements"]] == [1.0, 9.0]


def test_import_csv_without_time_preserves_file_order(api_client):
    _create_project("measnotime")
    csv_text = (
        "measurement_id,x,y,z,measured_path_gain_db\n"
        "z_row,30.0,0.0,1.5,-100.0\n"
        "a_row,10.0,0.0,1.5,-92.0\n"
    )
    resp = api_client.post(
        "/api/projects/measnotime/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["measurement_id"] for m in body["measurements"]] == ["z_row", "a_row"]
    assert all(m["time_s"] is None for m in body["measurements"])


def test_import_csv_unparseable_time_kept_as_untimed(api_client):
    """A bad time value never drops the row - it degrades to time-less (ordered
    after the timed rows) and is counted in a warning."""
    _create_project("measbadtime")
    csv_text = (
        "measurement_id,time_s,x,y,z,measured_path_gain_db\n"
        "broken,not_a_time,30.0,0.0,1.5,-100.0\n"
        "timed,1.0,10.0,0.0,1.5,-92.0\n"
    )
    resp = api_client.post(
        "/api/projects/measbadtime/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["skipped"] == 0
    assert [m["measurement_id"] for m in body["measurements"]] == ["timed", "broken"]
    assert body["measurements"][1]["time_s"] is None
    assert any("unparseable time" in w for w in body["warnings"])


def test_order_measurements_untimed_after_timed():
    mixed = [
        MeasurementSample(rx_position=[0, 0, 1.5], measured_path_gain_db=-90.0),
        MeasurementSample(rx_position=[1, 0, 1.5], measured_path_gain_db=-91.0, time_s=2.0),
        MeasurementSample(rx_position=[2, 0, 1.5], measured_path_gain_db=-92.0, time_s=1.0),
    ]
    ordered = order_measurements(mixed)
    assert [m.time_s for m in ordered] == [1.0, 2.0, None]
    # No times at all -> file order untouched.
    untimed = [mixed[0], mixed[0]]
    assert order_measurements(untimed) == untimed


# ------------------------------------------------- validate-trajectory (API)


def test_validate_trajectory_inline_offset_recovered(api_client, tmp_path):
    """Measured = the mock's own prediction + 7 dB, presented SHUFFLED with
    time stamps that restore the true order: the endpoint must time-order the
    log, recover the 7 dB level offset, and report ~0 residual RMSE."""
    _create_project("valinline")
    predicted = _mock_predicted_gains(tmp_path, POSITIONS)
    shuffle = [2, 0, 3, 1]
    meas = [
        {
            "rx_position": POSITIONS[i],
            "measured_path_gain_db": predicted[i] + 7.0,
            "time_s": float(i),
        }
        for i in shuffle
    ]
    resp = api_client.post(
        "/api/projects/valinline/calibrate/validate-trajectory",
        json={"measurements": meas},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "mock"
    assert body["tx_id"] == "tx_001"
    stats = body["stats"]
    assert stats["n"] == len(POSITIONS)
    assert stats["level_offset_db"] == pytest.approx(7.0, abs=1e-9)
    assert stats["rmse_db"] == pytest.approx(0.0, abs=1e-9)
    assert stats["mean_abs_error_db"] == pytest.approx(0.0, abs=1e-9)

    points = body["points"]
    assert len(points) == len(POSITIONS)
    # Points come back TIME-ordered (the shuffled input was re-ordered).
    assert [p["position"] for p in points] == POSITIONS
    assert [p["time_s"] for p in points] == [0.0, 1.0, 2.0, 3.0]
    assert [p["index"] for p in points] == list(range(len(POSITIONS)))
    for p, pred in zip(points, predicted):
        assert p["predicted_db"] == pytest.approx(pred, abs=1e-9)
        assert p["aligned_predicted_db"] == pytest.approx(pred + 7.0, abs=1e-9)
        assert p["measured_db"] == pytest.approx(pred + 7.0, abs=1e-9)
        assert p["error_db"] == pytest.approx(0.0, abs=1e-9)


def test_validate_trajectory_stats_match_calibration_math(api_client, tmp_path):
    """Noisy measurements: the endpoint's stats must equal the calibration
    module's own level-offset alignment (_stats) on the same numbers."""
    _create_project("valstats")
    predicted = _mock_predicted_gains(tmp_path, POSITIONS)
    noise = [1.5, -2.0, 0.5, 3.0]
    measured = [p + 7.0 + n for p, n in zip(predicted, noise)]
    meas = [
        {"rx_position": pos, "measured_path_gain_db": m, "time_s": float(k)}
        for k, (pos, m) in enumerate(zip(POSITIONS, measured))
    ]
    resp = api_client.post(
        "/api/projects/valstats/calibrate/validate-trajectory",
        json={"measurements": meas},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()

    ref, _, _ = _stats(measured, predicted)  # the calibration module's math
    assert ref.rmse_db > 0.5  # the noise makes this a non-trivial residual
    stats = body["stats"]
    assert stats["n"] == ref.n_links
    assert stats["level_offset_db"] == pytest.approx(ref.level_offset_db, abs=1e-9)
    assert stats["rmse_db"] == pytest.approx(ref.rmse_db, abs=1e-9)
    assert stats["mean_abs_error_db"] == pytest.approx(ref.mean_abs_error_db, abs=1e-9)
    for p, m, pred in zip(body["points"], measured, predicted):
        assert p["error_db"] == pytest.approx(
            (pred + ref.level_offset_db) - m, abs=1e-9
        )


def test_validate_trajectory_uses_stored_measurements(api_client, tmp_path):
    """No inline measurements -> the project's imported CSV is validated,
    time-ordered the same way (rows deliberately shuffled in the file)."""
    _create_project("valstored")
    predicted = _mock_predicted_gains(tmp_path, POSITIONS[:3])
    rows = [
        f"{float(i)!r},{POSITIONS[i][0]!r},{POSITIONS[i][1]!r},{POSITIONS[i][2]!r},{predicted[i] + 7.0!r}"
        for i in [1, 2, 0]  # shuffled file order; time restores it
    ]
    csv_text = "time_s,x,y,z,measured_path_gain_db\n" + "\n".join(rows) + "\n"
    imp = api_client.post(
        "/api/projects/valstored/calibrate/measurements/import-csv",
        json={"csv_text": csv_text},
    )
    assert imp.status_code == 200, imp.text

    resp = api_client.post(
        "/api/projects/valstored/calibrate/validate-trajectory", json={}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stats"]["n"] == 3
    assert body["stats"]["level_offset_db"] == pytest.approx(7.0, abs=1e-9)
    assert body["stats"]["rmse_db"] == pytest.approx(0.0, abs=1e-9)
    assert [p["position"] for p in body["points"]] == POSITIONS[:3]


def test_validate_trajectory_subsamples_to_max_points(api_client, tmp_path):
    _create_project("valsub")
    predicted = _mock_predicted_gains(tmp_path, POSITIONS)
    meas = [
        {"rx_position": pos, "measured_path_gain_db": g + 7.0, "time_s": float(k)}
        for k, (pos, g) in enumerate(zip(POSITIONS, predicted))
    ]
    resp = api_client.post(
        "/api/projects/valsub/calibrate/validate-trajectory",
        json={"measurements": meas, "max_points": 2},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["stats"]["n"] == 2
    # Even subsampling keeps the first and last points of the ordered log.
    assert [p["position"] for p in body["points"]] == [POSITIONS[0], POSITIONS[-1]]
    assert any("subsampled" in w for w in body["warnings"])


def test_validate_trajectory_empty_measurements_4xx(api_client):
    _create_project("valempty")
    # Inline empty list violates the schema's min_length -> 422.
    resp = api_client.post(
        "/api/projects/valempty/calibrate/validate-trajectory",
        json={"measurements": []},
    )
    assert resp.status_code == 422
    # No inline measurements and none imported -> 400.
    resp = api_client.post(
        "/api/projects/valempty/calibrate/validate-trajectory", json={}
    )
    assert resp.status_code == 400
    assert "none imported" in resp.json()["detail"]


def test_validate_trajectory_unknown_tx_400(api_client):
    _create_project("valbadtx")
    resp = api_client.post(
        "/api/projects/valbadtx/calibrate/validate-trajectory",
        json={
            "tx_id": "tx_nope",
            "measurements": [
                {"rx_position": [10.0, 0.0, 1.5], "measured_path_gain_db": -90.0}
            ],
        },
    )
    assert resp.status_code == 400
    assert "unknown tx device" in resp.json()["detail"]


def test_validate_trajectory_unknown_project_404(api_client):
    resp = api_client.post(
        "/api/projects/nope/calibrate/validate-trajectory", json={}
    )
    assert resp.status_code == 404


def test_validation_request_defaults():
    """The documented request shape: everything optional but max_points."""
    req = TrajectoryValidationRequest()
    assert req.tx_id is None and req.measurements is None and req.max_points == 200
