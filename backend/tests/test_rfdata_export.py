"""WS2 tests: expanded material library, trajectory metrics, RFData export."""

import csv
import io
import json
from pathlib import Path

import pytest

from app.schemas.devices import Device
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import (
    SimulationConfig,
    TrajectorySimulateRequest,
    UERoute,
)
from app.services.project_store import load_default_library
from app.services.rfdata_export import export_rfdata
from app.services.simulation_backends.mock_backend import MockBackend
from app.services.trajectory import resolve_waypoints, run_trajectory


def _scene() -> Scene:
    return Scene(
        scene_id="ws2",
        name="WS2 Scene",
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
            Device(id="rx_001", name="RX", kind="rx", position=[20.0, 0.0, 1.5], color="#2e9bff"),
        ],
    )


# ------------------------------------------------------------ material library


def test_library_has_itu_p2040_and_human_materials():
    lib = load_default_library()
    ids = lib.ids()
    for expected in [
        "itu_marble",
        "itu_ceiling_board",
        "itu_plasterboard",
        "itu_plywood",
        "itu_floorboard",
        "ground_28ghz",
        "human_body",
    ]:
        assert expected in ids, f"missing material {expected}"


def test_human_body_default_is_skin_28ghz():
    lib = load_default_library()
    hb = lib.get("human_body")
    assert hb is not None
    assert hb.model == "constant"
    assert hb.relative_permittivity == 8.6
    assert hb.conductivity_s_per_m == 19.0
    assert hb.thickness_m == 0.002


def test_itu_materials_carry_itu_name():
    lib = load_default_library()
    assert lib.get("itu_marble").itu_name == "itu_marble"
    assert lib.get("itu_ceiling_board").itu_name == "itu_ceiling_board"


def test_default_frequency_is_28ghz():
    assert SimulationConfig().frequency_hz == 28e9


# ----------------------------------------------------------------- trajectory


def test_resolve_waypoints_line():
    req = TrajectorySimulateRequest(start_m=[0, 0, 1.5], end_m=[10, 0, 1.5], num_points=6)
    wps = resolve_waypoints(req)
    assert len(wps) == 6
    assert wps[0] == [0.0, 0.0, 1.5]
    assert wps[-1] == [10.0, 0.0, 1.5]


def test_run_trajectory_mock_metrics(tmp_path: Path):
    scene = _scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock")
    req = TrajectorySimulateRequest(
        start_m=[5.0, 0.0, 1.5], end_m=[40.0, 0.0, 1.5], num_points=5, dt_s=0.1
    )
    result = run_trajectory(MockBackend(), tmp_path, scene, library, cfg, req)

    assert result.ue_id == "rx_001"
    assert len(result.samples) == 5
    for i, s in enumerate(result.samples):
        assert s.time_s == pytest.approx(i * 0.1)
        assert s.path_count >= 1
        assert s.rss_dbm is not None
        assert s.path_gain_db is not None
        assert s.rms_delay_spread_ns is not None and s.rms_delay_spread_ns >= 0.0
    # RSS should weaken as the UE moves away from the TX.
    assert result.samples[-1].rss_dbm < result.samples[0].rss_dbm


# ------------------------------------------------------------------- export


def test_export_rfdata_writes_full_contract(tmp_path: Path):
    scene = _scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock", frequency_hz=28e9)
    backend = MockBackend()

    paths = backend.simulate_paths(tmp_path, scene, library, cfg)
    radio_map = backend.simulate_radio_map(tmp_path, scene, library, cfg)
    trajectory = run_trajectory(
        backend, tmp_path, scene, library, cfg,
        TrajectorySimulateRequest(start_m=[5, 0, 1.5], end_m=[40, 0, 1.5], num_points=4),
    )

    summary = export_rfdata(
        tmp_path, scene, cfg, created_at="2026-07-02T00:00:00+00:00",
        paths=paths, radio_map=radio_map, trajectory=trajectory,
    )
    base = tmp_path / "export" / "rfdata"
    for name in [
        "scenario_meta.json",
        "devices.json",
        "paths.json",
        "trajectory.csv",
        "radio_map.csv",
        "calibration_points.json",
    ]:
        assert (base / name).is_file(), f"missing {name}"
    assert summary["has_paths"] and summary["has_radio_map"] and summary["has_trajectory"]

    meta = json.loads((base / "scenario_meta.json").read_text())
    assert meta["unit"] == "meter"
    assert meta["frequency_hz"] == 28e9
    assert meta["coordinate_transform"]["scale"] == 100.0

    devices = json.loads((base / "devices.json").read_text())
    assert devices["transmitters"][0]["id"] == "tx_001"
    assert devices["receivers"][0]["id"] == "rx_001"

    paths_json = json.loads((base / "paths.json").read_text())
    assert paths_json["schema_version"] == "1.0"
    frame = paths_json["paths_by_time"][0]
    assert frame["ue_id"] == "rx_001"
    assert frame["paths"][0]["type"] in {"LOS", "REFLECTION", "DIFFRACTION", "SCATTERING", "TRANSMISSION", "UNKNOWN"}
    assert len(frame["paths"][0]["points_m"]) >= 2

    rows = list(csv.reader(io.StringIO((base / "trajectory.csv").read_text())))
    assert rows[0] == ["time_s", "ue_id", "x_m", "y_m", "z_m", "rss_dbm", "sinr_db", "path_gain_db"]
    assert len(rows) == 1 + 4  # header + 4 waypoints

    rm_rows = list(csv.reader(io.StringIO((base / "radio_map.csv").read_text())))
    assert rm_rows[0] == ["x_m", "y_m", "z_m", "rss_dbm", "sinr_db", "path_gain_db"]
    assert len(rm_rows) > 1


def _multi_ue_scene() -> Scene:
    s = _scene()
    s.devices.append(
        Device(id="rx_002", name="RX2", kind="rx", position=[0.0, 20.0, 1.5])
    )
    return s


def test_trajectory_csv_multi_ue_has_ue_id_column_and_step_major_rows(tmp_path: Path):
    """Multi-UE trajectory export writes one row per STEP-MAJOR sample, tagged
    with its ue_id (B1). The ue_id column lets the AODT viewer split the file
    into per-UE sequences."""
    scene = _multi_ue_scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock")
    backend = MockBackend()
    trajectory = run_trajectory(
        backend, tmp_path, scene, library, cfg,
        TrajectorySimulateRequest(
            routes=[
                UERoute(ue_id="rx_001", waypoints=[[5.0, 0.0, 1.5], [40.0, 0.0, 1.5]]),
                UERoute(ue_id="rx_002", waypoints=[[2.0, 2.0, 1.5], [8.0, 8.0, 1.5]]),
            ],
            num_points=3, dt_s=0.1,
        ),
    )
    export_rfdata(
        tmp_path, scene, cfg, created_at="2026-07-02T00:00:00+00:00",
        trajectory=trajectory,
    )
    rows = list(csv.reader(io.StringIO(
        (tmp_path / "export" / "rfdata" / "trajectory.csv").read_text()
    )))
    assert rows[0] == ["time_s", "ue_id", "x_m", "y_m", "z_m", "rss_dbm", "sinr_db", "path_gain_db"]
    # 2 UEs x 3 steps = 6 data rows, STEP-MAJOR (both UEs at each step).
    assert len(rows) == 1 + 6
    ue_col = [r[1] for r in rows[1:]]
    assert ue_col == ["rx_001", "rx_002", "rx_001", "rx_002", "rx_001", "rx_002"]
    # time_s repeats per step (shared across the step's UEs).
    time_col = [float(r[0]) for r in rows[1:]]
    assert time_col == pytest.approx([0.0, 0.0, 0.1, 0.1, 0.2, 0.2])
    # Each row's position matches its sample (rx_001 walks +x, rx_002 walks +xy).
    for row, sample in zip(rows[1:], trajectory.samples):
        assert row[1] == sample.ue_id
        assert [float(c) for c in row[2:5]] == pytest.approx(sample.position)


def test_trajectory_csv_single_ue_still_carries_ue_id_column(tmp_path: Path):
    """Single-UE export is the degenerate case: the ue_id column is still
    present and every row shares one ue_id (fixed AODT schema column)."""
    scene = _scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock")
    trajectory = run_trajectory(
        MockBackend(), tmp_path, scene, library, cfg,
        TrajectorySimulateRequest(start_m=[5, 0, 1.5], end_m=[40, 0, 1.5], num_points=4),
    )
    export_rfdata(
        tmp_path, scene, cfg, created_at="2026-07-02T00:00:00+00:00",
        trajectory=trajectory,
    )
    rows = list(csv.reader(io.StringIO(
        (tmp_path / "export" / "rfdata" / "trajectory.csv").read_text()
    )))
    assert rows[0][1] == "ue_id"
    assert len(rows) == 1 + 4
    assert {r[1] for r in rows[1:]} == {"rx_001"}


def test_export_tolerates_missing_results(tmp_path: Path):
    scene = _scene()
    cfg = SimulationConfig(id="default")
    summary = export_rfdata(tmp_path, scene, cfg, created_at="2026-07-02T00:00:00+00:00")
    assert not summary["has_paths"]
    # paths.json still exists with an empty frame list.
    pj = json.loads((tmp_path / "export" / "rfdata" / "paths.json").read_text())
    assert pj["paths_by_time"] == []


# --------------------------------------------------------------------- API


def test_api_trajectory_and_export_roundtrip(api_client):
    from app.api.deps import get_store

    store = get_store()
    store.create_project("WS2 API", project_id="ws2api")
    store.save_scene("ws2api", _scene())

    P = "/api/projects/ws2api"
    # trajectory
    resp = api_client.post(
        f"{P}/simulate/trajectory",
        json={"config": {"backend": "mock"}, "start_m": [5, 0, 1.5], "end_m": [40, 0, 1.5], "num_points": 4},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "trajectory" and len(body["samples"]) == 4
    assert api_client.get(f"{P}/results/trajectory").json()["result_id"] == body["result_id"]

    # paths + radio map so the export has content
    api_client.post(f"{P}/simulate/paths", json={"config": {"backend": "mock"}})
    api_client.post(f"{P}/simulate/radio-map", json={"config": {"backend": "mock"}})

    ex = api_client.post(f"{P}/export/rfdata", json={})
    assert ex.status_code == 200, ex.text
    summary = ex.json()
    assert len(summary["files"]) == 6
    assert summary["has_paths"] and summary["has_trajectory"] and summary["has_radio_map"]
