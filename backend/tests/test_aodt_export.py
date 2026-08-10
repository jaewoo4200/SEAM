"""AODT results-schema parquet export (services/aodt_export.py).

Pins the contract that matters to an external AODT consumer: every documented
table exists with its exact column names, telemetry/ran_config are absent on
purpose, and the geometry survives a round trip back through our own AODT
reader (import_paths). Playback mode is checked for its time axis - one
time_idx per frame, raypaths spanning them.

Skips wholesale without pyarrow, like the AODT import tests.
"""

import json
import math
from pathlib import Path

import pytest

from seam_studio.schemas.devices import Antenna, Device
from seam_studio.schemas.results import AodtExportRequest
from seam_studio.schemas.scene import (
    Actor,
    ActorTrajectory,
    MeshRef,
    Prim,
    RFBinding,
    Scene,
)
from seam_studio.schemas.sensors import SensorFrame, SensorFramePose, SensorManifest
from seam_studio.schemas.simulation import PlaybackBuildRequest, SimulationConfig
from seam_studio.services import aodt_import
from seam_studio.services.aodt_export import EXPORT_DIR_REL, export_aodt
from seam_studio.services.playback import build_playback
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.mock_backend import MockBackend

try:  # pyarrow is optional; the whole module needs it.
    import pyarrow.parquet as pq

    HAS_PYARROW = True
except ImportError:  # pragma: no cover - env-dependent
    HAS_PYARROW = False

pytestmark = pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")


# The official AODT column names, verbatim (docs.nvidia.com/aerial/aodt).
EXPECTED_COLUMNS = {
    "ues": [
        "ID", "is_manual", "is_manual_mobility", "radiated_power", "height",
        "mech_tilt", "panel", "batch_indices", "waypoint_ids", "waypoint_points",
        "waypoint_stops", "waypoint_speeds", "trajectory_ids", "trajectory_points",
        "trajectory_stops", "trajectory_speeds", "route_positions",
        "route_orientations", "route_speeds", "route_times", "bler_target",
        "is_indoor_mobility",
    ],
    "scatterers": [
        "ID", "is_indoor_mobility", "is_3d", "is_manual", "batch_indices",
        "route_positions", "route_orientations", "route_speeds", "route_times",
    ],
    "rus": [
        "ID", "subcarrier_spacing", "fft_size", "radiated_power", "height",
        "mech_azimuth", "mech_tilt", "panel", "position", "du_id",
        "du_manual_assign",
    ],
    "dus": [
        "ID", "subcarrier_spacing", "fft_size", "num_antennas", "reference_freq",
        "max_channel_bandwidth", "position",
    ],
    "panels": [
        "panel_id", "panel_name", "antenna_names", "antenna_pattern_indices",
        "frequencies", "thetas", "phis", "reference_freq", "dual_polarized",
        "num_loc_antenna_horz", "num_loc_antenna_vert", "antenna_spacing_horz",
        "antenna_spacing_vert", "antenna_roll_angle_first_polz",
        "antenna_roll_angle_second_polz",
    ],
    "patterns": [
        "pattern_id", "pattern_type", "e_theta_re", "e_theta_im", "e_phi_re",
        "e_phi_im",
    ],
    "time_info": ["time_idx", "batch_idx", "slot_idx", "symbol_idx"],
    "cfrs": [
        "time_idx", "ru_id", "ue_id", "ru_ant_el", "ue_ant_el", "cfr_re", "cfr_im",
    ],
    "cirs": [
        "time_idx", "ru_id", "ue_id", "ru_ant_el", "ue_ant_el", "cir_re", "cir_im",
        "cir_delay",
    ],
    "raypaths": [
        "time_idx", "ru_id", "ue_id", "ru_ant_el", "ue_ant_el", "interaction_types",
        "points", "normals", "ampl_re", "ampl_im", "prim_ids", "object_ids",
        "vegetation_depths",
    ],
}


# ------------------------------------------------------------------ fixtures


def _scene() -> Scene:
    """One TX / one RX over a ground prim, plus a moving actor so the
    scatterers table has a route to write."""
    return Scene(
        scene_id="aodt_export",
        name="AODT Export",
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
            Device(
                id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 10.0],
                orientation_deg=[30.0, -5.0, 0.0], power_dbm=30.0,
                antenna=Antenna(num_rows=2, num_cols=4),
            ),
            Device(id="rx_001", name="RX", kind="rx", position=[20.0, 0.0, 1.5]),
        ],
        actors=[
            Actor(
                id="car_1",
                kind="car",
                position=[5.0, 5.0, 0.0],
                trajectory=ActorTrajectory(
                    waypoints=[[5.0, 5.0, 0.0], [7.0, 5.0, 0.0], [9.0, 5.0, 0.0]],
                    dt_s=0.5,
                ),
            ),
        ],
    )


def _cfg() -> SimulationConfig:
    return SimulationConfig(id="default", backend="mock", frequency_hz=28e9)


def _manifest() -> SensorManifest:
    return SensorManifest(
        entity_id="rx_001",
        frames=[
            SensorFrame(
                index=i,
                time_s=0.5 * i,
                pose=SensorFramePose(position=[20.0 + 4.0 * i, 0.0, 1.5]),
            )
            for i in range(3)
        ],
    )


def _export(tmp_path: Path, request: AodtExportRequest = None) -> tuple[dict, Path]:
    scene = _scene()
    library = load_default_library()
    cfg = _cfg()
    backend = MockBackend()
    req = request or AodtExportRequest()

    paths = playback = None
    if req.source == "playback":
        playback = build_playback(
            backend, tmp_path, scene, library, cfg,
            PlaybackBuildRequest(max_paths_per_frame=8), _manifest(),
        )
    else:
        paths = backend.simulate_paths(tmp_path, scene, library, cfg)

    summary = export_aodt(
        tmp_path, scene, library, cfg, req, paths=paths, playback=playback
    )
    return summary, tmp_path / EXPORT_DIR_REL


def _rows(export_dir: Path, table: str) -> list[dict]:
    return pq.read_table(export_dir / f"{table}.parquet").to_pylist()


# ------------------------------------------------------------- table contract


def test_export_writes_every_table_with_exact_columns(tmp_path: Path):
    summary, out = _export(tmp_path)

    assert summary["export_dir"] == EXPORT_DIR_REL
    for table, columns in EXPECTED_COLUMNS.items():
        path = out / f"{table}.parquet"
        assert path.is_file(), f"missing {table}.parquet"
        assert pq.read_schema(path).names == columns, table
        assert f"{EXPORT_DIR_REL}/{table}.parquet" in summary["files"]
        assert summary["tables"][table] == len(_rows(out, table))

    # RAN-simulation tables are deliberately not produced.
    assert not (out / "telemetry.parquet").exists()
    assert not (out / "ran_config.parquet").exists()


def test_entity_tables_describe_the_scene(tmp_path: Path):
    _summary, out = _export(tmp_path)

    ru = _rows(out, "rus")[0]
    assert ru["ID"] == 0
    assert ru["position"] == pytest.approx([0.0, 0.0, 10.0])
    assert ru["height"] == pytest.approx(10.0)
    assert ru["mech_azimuth"] == pytest.approx(30.0)
    assert ru["mech_tilt"] == pytest.approx(-5.0)
    assert ru["radiated_power"] == pytest.approx(1.0)  # 30 dBm -> 1 W
    assert ru["fft_size"] == 64 and ru["subcarrier_spacing"] == pytest.approx(30e3)
    assert ru["du_id"] == 0 and ru["du_manual_assign"] is False

    du = _rows(out, "dus")[0]
    assert du["num_antennas"] == 8  # 2x4 TX array
    assert du["reference_freq"] == pytest.approx(28e3)  # MHz
    assert du["max_channel_bandwidth"] == pytest.approx(100.0)  # MHz

    ue = _rows(out, "ues")[0]
    assert ue["ID"] == 0 and ue["bler_target"] == pytest.approx(0.1)
    assert ue["batch_indices"] == [0]
    # A paths-sourced export has no route.
    assert ue["route_positions"] == [] and ue["route_times"] == []
    assert ue["waypoint_points"] == [] and ue["trajectory_points"] == []

    # TX (2x4) and RX (1x1) are distinct antenna configs -> two panels.
    panels = _rows(out, "panels")
    assert [p["panel_id"] for p in panels] == [0, 1]
    assert panels[0]["panel_name"] == "seam_panel_0"
    assert panels[0]["num_loc_antenna_horz"] == 4
    assert panels[0]["num_loc_antenna_vert"] == 2
    assert len(panels[0]["antenna_names"]) == 8
    assert panels[0]["dual_polarized"] == 0
    # 0.5 lambda at 28 GHz = 5.35 mm = 0.535 cm.
    half_lambda_cm = 0.5 * (299_792_458.0 / 28e9) * 100.0
    assert panels[0]["antenna_spacing_horz"] == pytest.approx(half_lambda_cm, rel=1e-5)

    pattern = _rows(out, "patterns")[0]
    assert pattern["pattern_id"] == 0 and pattern["pattern_type"] == 0
    assert pattern["e_theta_re"] == [[1.0]] and pattern["e_phi_re"] == [[0.0]]

    # The moving actor becomes a scatterer route (3 waypoints, dt 0.5 s).
    scat = _rows(out, "scatterers")[0]
    assert scat["ID"] == 0 and scat["is_3d"] is True and scat["is_manual"] is True
    assert scat["route_positions"][0][0] == pytest.approx([5.0, 5.0, 0.0])
    assert scat["route_times"][0] == pytest.approx([0.0, 0.5, 1.0])
    # 2 m per 0.5 s = 4 m/s on each leg (last speed repeats the final leg).
    assert scat["route_speeds"][0] == pytest.approx([4.0, 4.0, 4.0])


def test_id_map_records_the_string_to_integer_mapping(tmp_path: Path):
    summary, out = _export(tmp_path)
    assert f"{EXPORT_DIR_REL}/id_map.json" in summary["files"]
    id_map = json.loads((out / "id_map.json").read_text())
    assert id_map["rus"] == {"tx_001": 0}
    assert id_map["ues"] == {"rx_001": 0}
    assert id_map["prims"] == {"/ground": 0}
    assert id_map["scatterers"] == {"car_1": 0}
    assert id_map["panels"] == {"tx_001": 0, "rx_001": 1}
    assert id_map["omitted_tables"] == ["telemetry", "ran_config"]


# ----------------------------------------------------------------- round trip


def test_raypaths_round_trip_through_the_aodt_importer(tmp_path: Path):
    """The exported raypaths table must read back through our own AODT reader
    with the same path count, the same path-type multiset, and identical
    polylines (float32 tolerance)."""
    scene = _scene()
    library = load_default_library()
    cfg = _cfg()
    original = MockBackend().simulate_paths(tmp_path, scene, library, cfg)
    export_aodt(
        tmp_path, scene, library, cfg, AodtExportRequest(), paths=original
    )
    out = tmp_path / EXPORT_DIR_REL

    imported = aodt_import.import_paths(out, [])
    assert len(imported.paths) == len(original.paths)
    assert sorted(p.path_type for p in imported.paths) == sorted(
        p.path_type for p in original.paths
    )
    for got, want in zip(imported.paths, original.paths):
        assert len(got.vertices) == len(want.vertices)
        for v_got, v_want in zip(got.vertices, want.vertices):
            assert v_got == pytest.approx(v_want, rel=1e-6, abs=1e-4)
    # The importer also accepts the export directory through the public entry.
    assert aodt_import.import_aodt_results(out, "paths").backend == "aodt_import"


def test_raypaths_rows_carry_aligned_geometry_lists(tmp_path: Path):
    _summary, out = _export(tmp_path)
    rows = _rows(out, "raypaths")
    assert rows

    for row in rows:
        n = len(row["points"])
        assert n >= 2
        # interaction_types is emission + one per bounce; the RX arrival is not
        # an interaction, so it is exactly one shorter than points.
        assert len(row["interaction_types"]) == n - 1
        assert row["interaction_types"][0] == "emission"
        assert all(
            t in {"reflection", "diffraction", "scattering", "transmission"}
            for t in row["interaction_types"][1:]
        )
        assert len(row["normals"]) == n
        assert all(v == [0.0, 0.0, 0.0] for v in row["normals"])
        assert len(row["vegetation_depths"]) == n
        assert len(row["prim_ids"]) == n and row["object_ids"] == row["prim_ids"]
        assert row["prim_ids"][0] == -1 and row["prim_ids"][-1] == -1
        assert len(row["ampl_re"]) == 1 and len(row["ampl_im"]) == 1
        assert row["ru_ant_el"] == {"h": 0, "v": 0}
        assert row["ue_ant_el"] == {"h": 0, "v": 0}
        assert row["time_idx"] == 0 and row["ru_id"] == 0 and row["ue_id"] == 0

    # The reflection path bounces off /ground, which is prim index 0.
    bounced = [r for r in rows if len(r["points"]) > 2]
    assert bounced and all(r["prim_ids"][1] == 0 for r in bounced)


# ------------------------------------------------------------------ cir / cfr


def test_cirs_delays_are_seconds_and_taps_are_delay_sorted(tmp_path: Path):
    scene = _scene()
    library = load_default_library()
    cfg = _cfg()
    original = MockBackend().simulate_paths(tmp_path, scene, library, cfg)
    export_aodt(tmp_path, scene, library, cfg, AodtExportRequest(), paths=original)

    rows = _rows(tmp_path / EXPORT_DIR_REL, "cirs")
    assert len(rows) == 1  # one (time, ru, ue) link
    row = rows[0]
    expected = sorted(p.delay_ns * 1e-9 for p in original.paths)
    assert row["cir_delay"] == pytest.approx(expected, rel=1e-5)
    assert row["cir_delay"] == sorted(row["cir_delay"])
    assert len(row["cir_re"]) == len(row["cir_im"]) == len(expected)
    assert row["ru_ant_el"] == {"h": 0, "v": 0, "p": 0}

    # Amplitudes are channel coefficients: |a| = 10^(gain_dB/20).
    strongest = min(original.paths, key=lambda p: p.delay_ns)
    gain_db = (
        strongest.path_gain_db
        if strongest.path_gain_db is not None
        else strongest.power_dbm - 30.0
    )
    mag = math.hypot(row["cir_re"][0], row["cir_im"][0])
    assert 20.0 * math.log10(mag) == pytest.approx(gain_db, abs=1e-3)


def test_cfrs_has_one_row_per_link_with_fft_size_tones(tmp_path: Path):
    _summary, out = _export(tmp_path, AodtExportRequest(fft_size=32))
    rows = _rows(out, "cfrs")
    assert len(rows) == 1
    assert len(rows[0]["cfr_re"]) == 32 and len(rows[0]["cfr_im"]) == 32
    assert rows[0]["ru_id"] == 0 and rows[0]["ue_id"] == 0
    # A multi-tap channel is frequency selective, never a flat response.
    mags = [math.hypot(re, im) for re, im in zip(rows[0]["cfr_re"], rows[0]["cfr_im"])]
    assert max(mags) > 0.0
    assert _rows(out, "rus")[0]["fft_size"] == 32


# -------------------------------------------------------------------- playback


def test_playback_source_writes_one_time_index_per_frame(tmp_path: Path):
    summary, out = _export(tmp_path, AodtExportRequest(source="playback"))

    time_rows = _rows(out, "time_info")
    assert len(time_rows) == 3  # one per posed manifest frame
    assert [r["time_idx"] for r in time_rows] == [0, 1, 2]
    assert [r["slot_idx"] for r in time_rows] == [0, 1, 2]
    assert all(r["batch_idx"] == 0 and r["symbol_idx"] == 0 for r in time_rows)
    assert summary["tables"]["time_info"] == 3

    raypaths = _rows(out, "raypaths")
    assert {r["time_idx"] for r in raypaths} == {0, 1, 2}
    assert {r["time_idx"] for r in _rows(out, "cirs")} == {0, 1, 2}
    assert {r["time_idx"] for r in _rows(out, "cfrs")} == {0, 1, 2}

    # The moving UE's route comes from the pack frames.
    ue = _rows(out, "ues")[0]
    assert ue["route_times"][0] == pytest.approx([0.0, 0.5, 1.0])
    assert [p[0] for p in ue["route_positions"][0]] == pytest.approx([20.0, 24.0, 28.0])
    assert ue["route_orientations"][0] == [[0.0, 0.0, 0.0]] * 3
    assert ue["route_speeds"][0] == [0.0, 0.0, 0.0]


def test_missing_source_result_raises_export_error(tmp_path: Path):
    from seam_studio.services.aodt_export import AodtExportError

    scene = _scene()
    library = load_default_library()
    with pytest.raises(AodtExportError):
        export_aodt(
            tmp_path, scene, library, _cfg(),
            AodtExportRequest(source="playback"), paths=None, playback=None,
        )


def test_rerun_clears_stale_parquet(tmp_path: Path):
    _summary, out = _export(tmp_path)
    stale = out / "telemetry.parquet"
    stale.write_bytes(b"stale")
    _export(tmp_path)
    assert not stale.exists()


# ------------------------------------------------------------------------ API


def test_api_export_aodt_endpoint(api_client):
    from seam_studio.api.deps import get_store

    store = get_store()
    store.create_project("AODT Export", project_id="aodtexp")
    store.save_scene("aodtexp", _scene())

    P = "/api/projects/aodtexp"
    # No paths result yet -> 404 rather than an empty export.
    assert api_client.post(f"{P}/export/aodt", json={}).status_code == 404

    assert api_client.post(
        f"{P}/simulate/paths", json={"config": {"backend": "mock"}}
    ).status_code == 200

    resp = api_client.post(f"{P}/export/aodt", json={"fft_size": 16})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["export_dir"] == EXPORT_DIR_REL
    assert len(body["files"]) == len(EXPECTED_COLUMNS) + 1  # tables + id_map.json
    assert set(body["tables"]) == set(EXPECTED_COLUMNS)
    assert body["tables"]["raypaths"] > 0

    out = store.resolve("aodtexp") / EXPORT_DIR_REL
    assert len(_rows(out, "cfrs")[0]["cfr_re"]) == 16


def test_api_export_aodt_409_without_pyarrow(api_client, monkeypatch):
    """pyarrow is optional, so the route answers 409 (not 500) without it."""
    from seam_studio.api.deps import get_store
    from seam_studio.services import aodt_export

    store = get_store()
    store.create_project("No PA Export", project_id="aodtexp_nopa")
    store.save_scene("aodtexp_nopa", _scene())
    P = "/api/projects/aodtexp_nopa"
    assert api_client.post(
        f"{P}/simulate/paths", json={"config": {"backend": "mock"}}
    ).status_code == 200

    def boom():
        raise aodt_export.AodtExportUnavailable("pyarrow missing (forced)")

    monkeypatch.setattr(aodt_export, "_require_pyarrow", boom)
    resp = api_client.post(f"{P}/export/aodt", json={})
    assert resp.status_code == 409, resp.text
