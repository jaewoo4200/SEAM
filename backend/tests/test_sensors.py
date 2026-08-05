"""Sensor manifest loading, drive-segment detection, and the GET /sensors route.

Pins the contract the playback dashboard reads: a project without
``sensor_data/manifest.json`` is "no sensor data" (not an error), frames come
back sorted by frame index, a frame-time jump splits the timeline into runs
(playing across a concat boundary looks shuffled), and load problems surface as
warnings instead of failing the request.
"""

import json
from pathlib import Path

import pytest

from seam_studio.services.sensor_store import (
    MANIFEST_REL,
    load_manifest,
    manifest_response,
)


def _write_manifest(project_dir: Path, manifest: dict, *, files=()) -> Path:
    """Write sensor_data/manifest.json plus any dummy frame files it names."""
    path = project_dir / MANIFEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest), encoding="utf-8")
    for rel in files:
        target = project_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"x")
    return path


def _frame(index: int, time_s=None, **kw) -> dict:
    frame = {"index": index}
    if time_s is not None:
        frame["time_s"] = time_s
    frame.update(kw)
    return frame


def _make_project(api_client, project_id: str = "sens") -> str:
    resp = api_client.post(
        "/api/projects", json={"name": project_id, "project_id": project_id}
    )
    assert resp.status_code == 201, resp.text
    return project_id


def _project_dir(project_id: str) -> Path:
    from seam_studio.api import deps

    return deps.get_store().resolve(project_id)


# ------------------------------------------------------- (a) no sensor data


def test_missing_manifest_returns_none(tmp_path: Path):
    assert load_manifest(tmp_path) is None


def test_route_404_when_project_has_no_sensor_data(api_client):
    pid = _make_project(api_client)
    resp = api_client.get(f"/api/projects/{pid}/sensors")
    assert resp.status_code == 404
    assert "no sensor data" in resp.json()["detail"]


def test_route_404_for_unknown_project(api_client):
    resp = api_client.get("/api/projects/nope/sensors")
    assert resp.status_code == 404
    assert "project not found" in resp.json()["detail"]


def test_malformed_manifest_raises_value_error_and_route_422(api_client):
    pid = _make_project(api_client, "bad_manifest")
    project_dir = _project_dir(pid)
    (project_dir / MANIFEST_REL).parent.mkdir(parents=True, exist_ok=True)
    (project_dir / MANIFEST_REL).write_text("{not json", encoding="utf-8")

    with pytest.raises(ValueError):
        load_manifest(project_dir)
    assert api_client.get(f"/api/projects/{pid}/sensors").status_code == 422


def test_schema_violation_raises_value_error(tmp_path: Path):
    # extra="forbid" on the schema models: an unknown key is a hard error, not
    # silently dropped state the UI would then disagree with.
    _write_manifest(tmp_path, {"version": 1, "frames": [], "bogus_key": 1})
    with pytest.raises(ValueError):
        load_manifest(tmp_path)


# --------------------------------------------------- (b) segment detection


def test_segments_split_on_time_jump(tmp_path: Path):
    _write_manifest(
        tmp_path,
        {
            "frames": [
                _frame(0, 0.0), _frame(1, 0.1), _frame(2, 0.2),
                _frame(3, 50.0), _frame(4, 50.1),
            ]
        },
    )
    resp = manifest_response(tmp_path, load_manifest(tmp_path))

    assert [(s.start, s.end) for s in resp.segments] == [(0, 2), (3, 4)]
    assert [s.label for s in resp.segments] == ["Seq 1 (0-2)", "Seq 2 (3-4)"]


def test_uniform_times_are_one_segment(tmp_path: Path):
    # 10 Hz jitter (0.09-0.12 s) must never split a run: 3x median is well under
    # the 2 s floor, so nothing here is a boundary.
    times = [0.0, 0.1, 0.19, 0.31, 0.4, 0.52]
    _write_manifest(tmp_path, {"frames": [_frame(i, t) for i, t in enumerate(times)]})
    resp = manifest_response(tmp_path, load_manifest(tmp_path))

    assert [(s.start, s.end) for s in resp.segments] == [(0, 5)]


def test_backwards_clock_splits(tmp_path: Path):
    # A restarted clock (dt <= 0) is a new run even though the gap is small.
    _write_manifest(
        tmp_path,
        {"frames": [_frame(0, 0.0), _frame(1, 0.1), _frame(2, 0.05), _frame(3, 0.15)]},
    )
    resp = manifest_response(tmp_path, load_manifest(tmp_path))
    assert [(s.start, s.end) for s in resp.segments] == [(0, 1), (2, 3)]


def test_untimed_and_single_frame_manifests_are_one_segment(tmp_path: Path):
    _write_manifest(tmp_path, {"frames": [_frame(i) for i in (7, 8, 9)]})
    untimed = manifest_response(tmp_path, load_manifest(tmp_path))
    assert [(s.start, s.end) for s in untimed.segments] == [(0, 2)]
    assert untimed.segments[0].label == "Seq 1 (7-9)"

    _write_manifest(tmp_path, {"frames": [_frame(4, 12.0)]})
    single = manifest_response(tmp_path, load_manifest(tmp_path))
    assert [(s.start, s.end) for s in single.segments] == [(0, 0)]


def test_empty_manifest_has_no_segments(tmp_path: Path):
    _write_manifest(tmp_path, {"frames": []})
    resp = manifest_response(tmp_path, load_manifest(tmp_path))
    assert resp.segments == []


# ------------------------------------------------------------ (c) warnings


def test_warns_on_missing_file_and_undeclared_channel(tmp_path: Path):
    _write_manifest(
        tmp_path,
        {
            "channels": [{"key": "camera", "kind": "image"}],
            "frames": [
                _frame(0, 0.0, files={"camera": "sensor_data/camera/0.jpg"}),
                # lidar is never declared, and its file is not on disk.
                _frame(1, 0.1, files={"camera": "sensor_data/camera/1.jpg",
                                      "lidar": "sensor_data/lidar/1.pcd"}),
            ],
        },
        files=["sensor_data/camera/0.jpg"],
    )
    resp = manifest_response(tmp_path, load_manifest(tmp_path))

    undeclared = [w for w in resp.warnings if "lidar" in w and "declare" in w]
    assert len(undeclared) == 1, resp.warnings
    missing = [w for w in resp.warnings if "not found" in w]
    assert len(missing) == 1, resp.warnings
    assert "sensor_data/camera/1.jpg" in missing[0]
    assert "sensor_data/lidar/1.pcd" in missing[0]
    assert "sensor_data/camera/0.jpg" not in missing[0]  # that one exists


def test_missing_file_warning_caps_the_listing(tmp_path: Path):
    _write_manifest(
        tmp_path,
        {
            "channels": [{"key": "camera", "kind": "image"}],
            "frames": [
                _frame(i, i * 0.1, files={"camera": f"sensor_data/camera/{i}.jpg"})
                for i in range(8)
            ],
        },
    )
    resp = manifest_response(tmp_path, load_manifest(tmp_path))

    missing = next(w for w in resp.warnings if "not found" in w)
    assert missing.startswith("8 frame file(s)")
    assert "and 3 more" in missing
    assert missing.count("sensor_data/camera/") == 5


def test_warns_on_duplicate_frame_index(tmp_path: Path):
    _write_manifest(
        tmp_path,
        {"frames": [_frame(0, 0.0), _frame(1, 0.1), _frame(1, 0.2)]},
    )
    resp = manifest_response(tmp_path, load_manifest(tmp_path))
    dupes = [w for w in resp.warnings if "duplicate frame index 1" in w]
    assert len(dupes) == 1, resp.warnings


def test_clean_manifest_has_no_warnings(tmp_path: Path):
    _write_manifest(
        tmp_path,
        {
            "channels": [{"key": "camera", "kind": "image"}],
            "frames": [
                _frame(0, 0.0, files={"camera": "sensor_data/camera/0.jpg"}),
                _frame(1, 0.1, files={"camera": "sensor_data/camera/1.jpg"}),
            ],
        },
        files=["sensor_data/camera/0.jpg", "sensor_data/camera/1.jpg"],
    )
    resp = manifest_response(tmp_path, load_manifest(tmp_path))
    assert resp.warnings == []


# ------------------------------------------------------ (d) frame ordering


def test_frames_sorted_by_index_without_mutating_the_manifest(tmp_path: Path):
    _write_manifest(
        tmp_path,
        {"frames": [_frame(5, 0.5), _frame(1, 0.1), _frame(3, 0.3)]},
    )
    manifest = load_manifest(tmp_path)
    resp = manifest_response(tmp_path, manifest)

    assert [f.index for f in resp.manifest.frames] == [1, 3, 5]
    # The caller's manifest (and the on-disk order it mirrors) is untouched.
    assert [f.index for f in manifest.frames] == [5, 1, 3]
    # Segments index the SORTED array, so time is monotonic within a segment.
    assert [(s.start, s.end) for s in resp.segments] == [(0, 2)]


def test_route_returns_sorted_frames_and_segments(api_client):
    pid = _make_project(api_client, "sens_route")
    project_dir = _project_dir(pid)
    _write_manifest(
        project_dir,
        {
            "entity_id": "rx_001",
            "channels": [{"key": "camera", "kind": "image", "label": "Front"}],
            # Shuffled on disk, and a 50 s jump between frame 2 and frame 3.
            "frames": [
                _frame(i, t, files={"camera": f"sensor_data/camera/{i}.jpg"})
                for i, t in ((3, 50.0), (0, 0.0), (4, 50.1), (2, 0.2), (1, 0.1))
            ],
        },
        files=[f"sensor_data/camera/{i}.jpg" for i in range(5)],
    )
    resp = api_client.get(f"/api/projects/{pid}/sensors")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["manifest"]["entity_id"] == "rx_001"
    assert [f["index"] for f in body["manifest"]["frames"]] == [0, 1, 2, 3, 4]
    assert [(s["start"], s["end"]) for s in body["segments"]] == [(0, 2), (3, 4)]
    assert body["warnings"] == []
