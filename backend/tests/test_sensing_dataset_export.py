"""Sensing dataset export (POST /export/sensing-dataset): the links / echoes
tables of stored scenario results in one zip (npz, csv, parquet), its
manifest, the frame-level split, the kinematics fallback for results that
predate per-frame nodes, and the 4xx rules. Mock backend throughout."""

import csv
import io
import json
import math
import sys
import zipfile

import numpy as np
import pytest

from seam_studio.services import sensing_dataset_export
from seam_studio.services.sensing_dataset_export import (
    ECHO_COLUMNS,
    LINK_COLUMNS,
    assign_splits,
    split_counts,
)

from .test_sensing_track import MOCK_CFG, _ego_radar_scene, _trp_scene, _uav

PID = "ds_api"
C = 299_792_458.0


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Dataset", "project_id": PID})
    assert resp.status_code == 201, resp.text
    _put_scene(api_client, _trp_scene(scene_id=PID))
    return api_client


def _put_scene(client, scene):
    put = client.put(f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json"))
    assert put.status_code == 200, put.text


def _run(client, num_frames=5, **sensing) -> dict:
    payload = {"config": MOCK_CFG, "num_frames": num_frames, "dt_s": 0.5, "include_paths": False}
    if sensing.pop("off", False) is False:
        payload["sensing"] = {"enabled": True, "cpi_pulses": 4096, **sensing}
    resp = client.post(f"/api/projects/{PID}/simulate/scenario", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _export(client, **body):
    return client.post(f"/api/projects/{PID}/export/sensing-dataset", json=body or None)


def _zip(client, body: dict) -> zipfile.ZipFile:
    resp = client.get(body["download_url"])
    assert resp.status_code == 200, resp.text
    return zipfile.ZipFile(io.BytesIO(resp.content))


def _npz(zf: zipfile.ZipFile, name: str = "links.npz"):
    return np.load(io.BytesIO(zf.read(name)), allow_pickle=False)


def _same(value, cell) -> bool:
    if value is None:
        return (isinstance(cell, float) and math.isnan(cell)) or cell == ""
    if isinstance(value, float):
        return float(cell) == pytest.approx(value, abs=1e-12)
    return cell == value


def _project_dir():
    from seam_studio.api import deps

    return deps.get_store().resolve(PID)


# ------------------------------------------------------------ links table


def test_links_rows_align_with_frames(client):
    result = _run(client, measurement_noise=True)
    resp = _export(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result_ids"] == [result["result_id"]]
    assert body["num_rows"] == 5 * 16 * 1 == body["rows_per_result"][result["result_id"]]
    assert body["rows_per_split"] == {"all": 80} and body["num_echo_rows"] == 0
    assert body["files"] == ["manifest.json", "README.txt", "links.npz"]
    assert body["export_dir"] == "export/sensing_dataset"
    assert body["zip_name"].startswith("sensing_dataset_") and body["zip_name"].endswith(".zip")

    zf = _zip(client, body)
    assert zf.namelist() == body["files"]
    path = _project_dir() / body["export_dir"] / body["zip_name"]
    assert path.read_bytes() == client.get(body["download_url"]).content
    assert body["size_bytes"] == path.stat().st_size
    links = _npz(zf)
    assert list(links.files) == [c.name for c in LINK_COLUMNS]
    assert all(len(links[c]) == 80 for c in links.files)
    assert links["result_id"].dtype.kind == "U" and links["frame_index"].dtype == np.int64

    for k in (0, 17, 46, 79):  # spot rows: row = frame * 16 + link
        i, j = divmod(k, 16)
        frame = result["frames"][i]
        report = frame["sensing"]["links"][j]
        est = frame["sensing"]["estimates"][0]
        echo = next(p for p in frame["sensing"]["echoes"] if p["path_id"] == report["path_id"])
        node = {n["id"]: n for n in frame["sensing"]["nodes"]}
        assert links["frame_index"][k] == i and links["time_s"][k] == frame["time_s"]
        for key in ("tx_id", "rx_id", "target_id", "snr_db", "measured_range_m",
                    "measured_doppler_hz", "doppler_hz", "detected", "reason", "multipath",
                    "num_echoes"):
            assert _same(report[key], links[key][k].item()), (k, key)
        assert [links[f"tx_pos_{a}"][k] for a in "xyz"] == node[report["tx_id"]]["position"]
        assert [links[f"rx_vel_{a}"][k] for a in "xyz"] == node[report["rx_id"]]["velocity"]
        assert [links[f"position_true_{a}"][k] for a in "xyz"] == est["position_true"]
        assert [links[f"position_est_{a}"][k] for a in "xyz"] == est["position_est"]
        assert links["est_status"][k] == est["status"]
        assert links["n_links_used"][k] == est["n_links_used"]
        assert _same(echo["aod_deg"] and echo["aod_deg"][0], links["aod_az_deg"][k].item())
        assert _same(echo["aoa_deg"] and echo["aoa_deg"][1], links["aoa_el_deg"][k].item())
    # None -> NaN / "": no pfa and no tracking in this run.
    assert np.all(np.isnan(links["pd"])) and np.all(np.isnan(links["track_position_x"]))
    assert set(links["track_status"].tolist()) == {""}
    assert body["detected_fraction"] == pytest.approx(float(np.mean(links["detected"])))

    manifest = json.loads(zf.read("manifest.json"))
    assert manifest["schema"] == "seam.sensing_dataset" and manifest["schema_version"] == 1
    assert manifest["project_id"] == PID and manifest["formats"] == ["npz"]
    assert manifest["tables"] == {"links": {"rows": 80, "files": ["links.npz"]}}
    assert manifest["split"] is None and manifest["rows_per_split"] == {"all": 80}
    reasons = manifest["label_balance"]["reason_counts"]
    assert sum(reasons.values()) == 80 and reasons.get("detected", 0) == int(links["detected"].sum())
    entry = manifest["results"][0]
    sensing = dict(result["metadata"]["sensing"])
    sensing.pop("targets")
    assert entry["sensing"] == sensing  # the run constants, verbatim
    assert entry["kinematics_source"] == "frames"
    assert entry["scene_hash"] == result["metadata"]["scene_hash"] == entry["current_scene_hash"]
    assert entry["num_frames"] == 5 and entry["dt_s"] == 0.5 and entry["num_rows"] == 80
    assert [c["name"] for c in manifest["columns"]["links"]] == list(links.files)
    assert {c["role"] for c in manifest["columns"]["links"]} == {
        "meta", "feature", "label", "estimate", "track"
    }
    assert "oracle" in manifest["conventions"]["association"]
    assert "allow_pickle=False" in zf.read("README.txt").decode()
    assert body["warnings"] == []

    prov = json.loads((_project_dir() / "provenance.json").read_text(encoding="utf-8"))
    last = prov["events"][-1]
    assert last["type"] == "export_sensing_dataset" and last["rows"] == 80
    assert last["result_ids"] == [result["result_id"]]


def test_track_and_pd_columns(client):
    result = _run(client, pfa=1e-6, tracking={"enabled": True}, measurement_noise=True)
    body = _export(client).json()
    links = _npz(_zip(client, body))
    for k in (0, 20, 79):
        i, j = divmod(k, 16)
        report = result["frames"][i]["sensing"]["links"][j]
        est = result["frames"][i]["sensing"]["estimates"][0]
        assert links["pd"][k] == report["pd"]
        assert links["track_status"][k] == est["track_status"]
        assert [links[f"track_position_{a}"][k] for a in "xyz"] == est["track_position"]
        assert links["track_updates"][k] == est["track_updates"]
    assert set(links["track_status"].tolist()) == {"init", "tracking"}


# ------------------------------------------------------------ echoes, formats


def test_echoes_table_and_format_parity(client):
    pytest.importorskip("pyarrow")
    import pyarrow.parquet as pq

    result = _run(client, num_frames=3, include_comm_paths=True)
    body = _export(client, formats=["npz", "csv", "parquet"], include_echo_paths=True).json()
    assert body["files"] == [
        "manifest.json", "README.txt", "links.npz", "links.csv", "links.parquet",
        "echoes.npz", "echoes.csv", "echoes.parquet",
    ]
    n_echoes = sum(len(f["sensing"]["echoes"]) for f in result["frames"])
    assert body["num_echo_rows"] == n_echoes
    zf = _zip(client, body)
    echoes = _npz(zf, "echoes.npz")
    assert list(echoes.files) == [c.name for c in ECHO_COLUMNS]
    frame0 = result["frames"][0]["sensing"]
    for k, path in enumerate(frame0["echoes"]):
        assert echoes["path_id"][k] == path["path_id"]
        assert echoes["target_id"][k] == (path["target_id"] or "")
        assert echoes["bistatic_range_m"][k] == pytest.approx(C * path["delay_ns"] * 1e-9)
        assert echoes["direct"][k] == ([i["type"] for i in path["interactions"]] == ["sensing"])
        reported = any(
            r["path_id"] == path["path_id"] and r["tx_id"] == path["tx_id"]
            and r["rx_id"] == path["rx_id"] for r in frame0["links"]
        )
        assert echoes["reported"][k] == reported
    assert echoes["target_id"].tolist().count("") > 0  # comm paths
    assert int(echoes["reported"].sum()) == sum(
        1 for f in result["frames"] for r in f["sensing"]["links"] if r["path_id"]
    )

    for table, columns in (("links", LINK_COLUMNS), ("echoes", ECHO_COLUMNS)):
        ref = _npz(zf, f"{table}.npz")
        rows = list(csv.reader(io.StringIO(zf.read(f"{table}.csv").decode())))
        assert rows[0] == [c.name for c in columns]
        parquet = pq.read_table(io.BytesIO(zf.read(f"{table}.parquet")))
        assert parquet.column_names == rows[0]
        for i, c in enumerate(columns):
            want = ref[c.name]
            text = [r[i] for r in rows[1:]]
            got_pq = parquet.column(c.name).to_numpy(zero_copy_only=False)
            if c.dtype == "float64":
                parsed = np.array([float(t) if t else math.nan for t in text])
                np.testing.assert_array_equal(parsed, want)  # repr() round-trips exactly
                np.testing.assert_array_equal(got_pq, want)  # NaN kept as NaN
            elif c.dtype == "bool":
                assert text == ["true" if v else "false" for v in want]
                assert got_pq.tolist() == want.tolist()
            else:
                assert text == [str(v) for v in want.tolist()]
                assert got_pq.tolist() == want.tolist()


# ------------------------------------------------------------ split


def test_split_is_by_frame_and_seed_stable(client):
    first = _run(client)
    second = _run(client)
    split = {"train": 0.8, "val": 0.1, "test": 0.1, "seed": 7}
    body = _export(client, split=split).json()
    assert body["result_ids"] == [first["result_id"], second["result_id"]]
    assert body["rows_per_split"] == {"train": 128, "val": 16, "test": 16}
    links = _npz(_zip(client, body))
    units: dict = {}
    for rid, frame, s in zip(links["result_id"], links["frame_index"], links["split"]):
        units.setdefault((str(rid), int(frame)), set()).add(str(s))
    assert len(units) == 10 and all(len(v) == 1 for v in units.values())
    labels = [next(iter(units[u])) for u in sorted(units, key=lambda u: (u[0], u[1]))]
    assert labels == assign_splits(10, type("S", (), split))
    assert sorted(labels) == ["test"] + ["train"] * 8 + ["val"]

    again = _npz(_zip(client, _export(client, split=split).json()))
    assert again["split"].tolist() == links["split"].tolist()
    other = _npz(_zip(client, _export(client, split={**split, "seed": 8}).json()))
    assert other["split"].tolist() != links["split"].tolist()
    manifest = json.loads(_zip(client, body).read("manifest.json"))
    assert manifest["split"] == {**split, "unit": "frame"}


def test_assign_splits_rounding():
    from seam_studio.schemas.sensing import SensingDatasetSplit

    assert assign_splits(3, None) == ["all"] * 3
    halves = assign_splits(3, SensingDatasetSplit(train=0.5, val=0.5, test=0.0))
    assert sorted(halves) == ["train", "train", "val"]  # tie 1.5 / 1.5: train first
    assert assign_splits(0, SensingDatasetSplit()) == []
    # A tie goes to the smaller fraction: val keeps its frame (4.5 / 0.5 / 0).
    assert split_counts(5, SensingDatasetSplit(train=0.9, val=0.1, test=0.0)) == [4, 1, 0]


@pytest.mark.parametrize("frames", [1, 5, 15])
@pytest.mark.parametrize(
    "fractions", [(0.9, 0.1, 0.0), (0.5, 0.5, 0.0), (0.7, 0.3, 0.0), (0.8, 0.0, 0.2),
                  (0.0, 0.5, 0.5), (0.8, 0.1, 0.1)],
)
def test_split_counts_never_fill_a_zero_fraction(frames, fractions):
    # round() half-to-even used to leave frames for "test" at test = 0 (and
    # take them from val): {0.9, 0.1, 0} at F = 5 gave 4 / 0 / 1.
    from seam_studio.schemas.sensing import SensingDatasetSplit

    split = SensingDatasetSplit(train=fractions[0], val=fractions[1], test=fractions[2])
    counts = split_counts(frames, split)
    assert sum(counts) == frames
    for n, f in zip(counts, fractions):
        assert abs(n - f * frames) < 1.0
        if f == 0.0:
            assert n == 0
    labels = assign_splits(frames, split)
    assert [labels.count(s) for s in ("train", "val", "test")] == counts


# ------------------------------------------------------------ kinematics fallback


def test_results_without_nodes_fall_back_to_the_scene(client):
    _put_scene(client, _ego_radar_scene(_uav(), devices=_trp_scene(scene_id=PID).devices)
               .model_copy(update={"scene_id": PID}))
    result = _run(client, num_frames=3)
    fresh = _npz(_zip(client, _export(client).json()))

    # A v0.1.12 result has no per-frame nodes.
    path = _project_dir() / "results" / f"{result['result_id']}.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    for frame in stored["frames"]:
        frame["sensing"].pop("nodes")
    path.write_text(json.dumps(stored), encoding="utf-8")

    body = _export(client).json()
    assert body["warnings"] == [
        f"result {result['result_id']} predates v0.1.13: tx/rx kinematics reconstructed "
        "from the current scene"
    ]
    zf = _zip(client, body)
    old = _npz(zf)
    for col in ("tx_pos_x", "tx_pos_y", "rx_pos_z", "tx_vel_x", "rx_vel_x", "rx_vel_y"):
        np.testing.assert_allclose(old[col], fresh[col], rtol=0, atol=1e-9)
    assert np.any(old["tx_vel_x"] == 15.0)  # the ego radar rides its car
    entry = json.loads(zf.read("manifest.json"))["results"][0]
    assert entry["kinematics_source"] == "current_scene"

    scene = client.get(f"/api/projects/{PID}/scene").json()  # keeps the result refs
    scene["devices"][0]["position"] = [0.0, 0.0, 30.0]
    assert client.put(f"/api/projects/{PID}/scene", json=scene).status_code == 200
    warnings = _export(client).json()["warnings"]
    assert warnings[0].endswith("(scene changed since the run)")


# ------------------------------------------------------------ errors


def test_errors(client, monkeypatch):
    resp = _export(client)
    assert resp.status_code == 400
    assert "no stored scenario result has sensing frames" in resp.json()["detail"]
    plain = _run(client, num_frames=2, off=True)
    assert _export(client).status_code == 400  # auto mode skips it, finds nothing
    resp = _export(client, result_ids=[plain["result_id"]])
    assert resp.status_code == 400 and "has no sensing frames" in resp.json()["detail"]
    resp = _export(client, result_ids=["mock_scenario_999"])
    assert resp.status_code == 404 and "unknown scenario result" in resp.json()["detail"]
    missing = client.post("/api/projects/nope/export/sensing-dataset", json={})
    assert missing.status_code == 404
    assert _export(client, formats=["hdf5"]).status_code == 422
    assert _export(client, split={"train": 0.5, "val": 0.1, "test": 0.1}).status_code == 422

    sensed = _run(client, num_frames=2)
    out_dir = _project_dir() / "export" / "sensing_dataset"
    monkeypatch.setitem(sys.modules, "pyarrow", None)
    resp = _export(client, formats=["npz", "parquet"])
    assert resp.status_code == 400 and "pyarrow" in resp.json()["detail"]
    assert "seam-studio[parquet]" in resp.json()["detail"]
    monkeypatch.delitem(sys.modules, "pyarrow")

    monkeypatch.setattr(sensing_dataset_export, "MAX_SENSING_DATASET_ROWS", 40)
    resp = _export(client, result_ids=[sensed["result_id"]], include_echo_paths=True)
    assert resp.status_code == 400
    assert "32 link rows + 32 echo rows = 64 rows; at most 40" in resp.json()["detail"]
    assert not out_dir.exists() or not any(out_dir.iterdir())  # nothing written
    ok = _export(client, result_ids=[sensed["result_id"]])
    assert ok.status_code == 200 and ok.json()["num_rows"] == 32


def test_a_partial_selection_can_skip_runs_without_sensing(client):
    # The UI lists every scenario run and cannot tell which have sensing: a
    # partial selection sends skip_without_sensing instead of failing on the
    # first such run with 400.
    plain = _run(client, num_frames=2, off=True)
    sensed = _run(client, num_frames=2)
    ids = [plain["result_id"], sensed["result_id"]]
    assert _export(client, result_ids=ids).status_code == 400
    resp = _export(client, result_ids=ids, skip_without_sensing=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result_ids"] == [sensed["result_id"]] and body["num_rows"] == 32
    assert body["warnings"] == [f"scenario result {plain['result_id']} has no sensing frames; skipped"]
    resp = _export(client, result_ids=[plain["result_id"]], skip_without_sensing=True)
    assert resp.status_code == 400
    assert "none of the listed scenario results has sensing frames" in resp.json()["detail"]
    # Unknown ids still answer 404.
    resp = _export(client, result_ids=["mock_scenario_999"], skip_without_sensing=True)
    assert resp.status_code == 404


def test_schema_invalid_stored_result_is_skipped_or_404(client):
    # Valid JSON that fails the result schema (hand-edited, or another
    # version) used to escape as a pydantic ValidationError: 500.
    broken = _run(client, num_frames=2)
    sensed = _run(client, num_frames=2)
    path = _project_dir() / "results" / f"{broken['result_id']}.json"
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["frames"] = "not a list"
    path.write_text(json.dumps(stored), encoding="utf-8")

    resp = _export(client)  # auto mode: skipped, named in the warnings
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result_ids"] == [sensed["result_id"]]
    assert body["warnings"] == [
        f"result file missing or unreadable: results/{broken['result_id']}.json; skipped"
    ]
    resp = _export(client, result_ids=[broken["result_id"]])
    assert resp.status_code == 404 and "unreadable" in resp.json()["detail"]


def test_zip_names_never_collide(client):
    _run(client, num_frames=1)
    names = {_export(client).json()["zip_name"] for _ in range(3)}
    assert len(names) == 3
