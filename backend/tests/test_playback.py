"""GT-vs-DT playback pack builder + routes (issue #3).

Pins what the playback dashboard depends on: pack frames align with the sensor
manifest by frame index (poseless frames dropped, loudly), each frame carries
the serving link's KPIs plus a codebook sweep reprojected onto WORLD azimuth,
stride/max_frames keep the first and last frame so a capped pack still spans
the whole drive, and the pack persists like any other result set.

Mock backend throughout: its analytic sweep is already world-anchored, so the
expected beam azimuths are exactly the sweep angles (axis 0).
"""

import json
import math
from pathlib import Path

import pytest

from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import PlaybackResultSet
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.sensors import SensorFrame, SensorFramePose, SensorManifest
from seam_studio.schemas.simulation import PlaybackBuildRequest, SimulationConfig
from seam_studio.services.playback import build_playback
from seam_studio.services.project_store import load_default_library
from seam_studio.services.sensor_store import MANIFEST_REL
from seam_studio.services.simulation_backends.mock_backend import MockBackend

PID = "playback_src"

CFG = {"id": "e2e", "name": "e2e", "backend": "mock", "frequency_hz": 28e9, "max_depth": 3}


# --------------------------------------------------------------- fixtures


def _scene(scene_id: str = "playback") -> Scene:
    """One TX and the RX the manifest tracks; the mock backend loads no meshes,
    so a single ground prim is enough geometry for reflections."""
    return Scene(
        scene_id=scene_id,
        name="Playback",
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
            Device(id="rx_001", name="UE", kind="rx", position=[20.0, 0.0, 1.5]),
        ],
    )


# Two drive segments (a 99 s clock jump between frame 2 and frame 3) and one
# frame without a pose - the shapes a concatenated drive dataset really has.
_FRAMES = [
    (0, 0.0, [20.0, 0.0, 1.5]),
    (1, 0.5, [24.0, 2.0, 1.5]),
    (2, 1.0, [28.0, 4.0, 1.5]),
    (3, 100.0, None),
    (4, 100.5, [32.0, -3.0, 1.5]),
    (5, 101.0, [36.0, -6.0, 1.5]),
]


def _manifest(entity_id: str = "rx_001") -> SensorManifest:
    return SensorManifest(
        entity_id=entity_id,
        frames=[
            SensorFrame(
                index=index,
                time_s=time_s,
                pose=SensorFramePose(position=pos) if pos is not None else None,
            )
            for index, time_s, pos in _FRAMES
        ],
    )


def _cfg() -> SimulationConfig:
    return SimulationConfig(id="default", backend="mock")


def _build(request: PlaybackBuildRequest = None) -> PlaybackResultSet:
    return build_playback(
        MockBackend(),
        Path("."),
        _scene(),
        load_default_library(),
        _cfg(),
        request or PlaybackBuildRequest(),
        _manifest(),
    )


def _sweep_angles(request: PlaybackBuildRequest) -> list[float]:
    angles: list[float] = []
    a = request.sweep_start_deg
    while a <= request.sweep_stop_deg + 1e-9:
        angles.append(round(a, 6))
        a += request.sweep_step_deg
    return angles


# ------------------------------------------------------ (a) frames + KPIs


def test_frames_align_with_manifest_and_carry_link_kpis():
    result = _build()

    # One pack frame per POSED manifest frame, keyed by the dataset's own index.
    assert [f.index for f in result.frames] == [0, 1, 2, 4, 5]
    assert [f.time_s for f in result.frames] == [0.0, 0.5, 1.0, 100.5, 101.0]
    assert result.tx_id == "tx_001" and result.rx_id == "rx_001"
    assert result.metadata["frame_count"] == 5

    for frame, (_i, _t, pos) in zip(
        result.frames, [f for f in _FRAMES if f[2] is not None]
    ):
        assert frame.rx_position == pytest.approx(pos)
        assert frame.n_paths > 0
        assert frame.rss_dbm is not None and math.isfinite(frame.rss_dbm)
        assert frame.rss_coherent_dbm is not None
        assert frame.tau_rms_ns is not None and frame.tau_rms_ns >= 0.0
        # Absolute beam power exists wherever the backend evaluated the sweep.
        finite = [v for v in frame.beam_power_dbm if v is not None]
        assert finite and all(math.isfinite(v) for v in finite)
        assert len(frame.beam_power_dbm) == len(frame.beam_azimuth_deg)
        # Stored rays belong to the serving link and are capped strongest-first.
        assert len(frame.paths) <= PlaybackBuildRequest().max_paths_per_frame
        assert all(p.tx_id == "tx_001" and p.rx_id == "rx_001" for p in frame.paths)
        powers = [p.power_dbm for p in frame.paths]
        assert powers == sorted(powers, reverse=True)

    # The RX moves per frame, so the link metrics must not be constant.
    assert len({round(f.rss_dbm, 6) for f in result.frames}) > 1


def test_source_scene_is_never_mutated():
    scene = _scene()
    build_playback(
        MockBackend(), Path("."), scene, load_default_library(), _cfg(),
        PlaybackBuildRequest(), _manifest(),
    )
    assert scene.device_by_id("rx_001").position == [20.0, 0.0, 1.5]


# ------------------------------------------------------- (b) world azimuth


def test_mock_beam_azimuth_equals_sweep_angles():
    """The mock's analytic lobe is centered on the geometric TX->RX bearing, so
    its sweep angles are already WORLD azimuth: axis 0, no reprojection."""
    request = PlaybackBuildRequest()
    result = _build(request)
    expected = _sweep_angles(request)

    assert result.metadata["axis_mode"] == "mock0"
    for frame in result.frames:
        assert frame.beam_azimuth_deg == pytest.approx(expected)
        assert frame.best_beam_deg is not None
        assert request.sweep_start_deg <= frame.best_beam_deg <= request.sweep_stop_deg
        # The peak of the curve sits at the reported best beam.
        finite = [(v, a) for v, a in zip(frame.beam_power_dbm, frame.beam_azimuth_deg)
                  if v is not None]
        peak_az = max(finite)[1]
        assert abs(peak_az - frame.best_beam_deg) <= request.sweep_step_deg


# --------------------------------------------------- (c) stride + max_frames


def test_frame_stride_and_max_frames_keep_first_and_last():
    # Posed frames are [0, 1, 2, 4, 5]; stride 2 selects [0, 2, 5].
    strided = _build(PlaybackBuildRequest(frame_stride=2, max_frames=400))
    assert [f.index for f in strided.frames] == [0, 2, 5]

    # Capping subsamples EVENLY with both endpoints kept (a short pack must
    # still span the whole drive, not just its beginning).
    capped = _build(PlaybackBuildRequest(frame_stride=2, max_frames=2))
    assert [f.index for f in capped.frames] == [0, 5]
    assert capped.metadata["frame_count"] == 2

    uncapped_first_last = _build(PlaybackBuildRequest(max_frames=3))
    assert [f.index for f in uncapped_first_last.frames] == [0, 2, 5]


# ------------------------------------------------------- (d) poseless frames


def test_poseless_frame_is_skipped_with_a_warning():
    result = _build()

    assert all(f.index != 3 for f in result.frames)
    skipped = [w for w in result.warnings if "without a pose" in w]
    assert len(skipped) == 1, result.warnings
    assert skipped[0].startswith("1 frame(s)")


def test_manifest_without_any_pose_raises_value_error():
    manifest = SensorManifest(
        entity_id="rx_001", frames=[SensorFrame(index=0, time_s=0.0)]
    )
    with pytest.raises(ValueError):
        build_playback(
            MockBackend(), Path("."), _scene(), load_default_library(), _cfg(),
            PlaybackBuildRequest(), manifest,
        )


def test_unknown_rx_id_raises_value_error():
    with pytest.raises(ValueError):
        build_playback(
            MockBackend(), Path("."), _scene(), load_default_library(), _cfg(),
            PlaybackBuildRequest(rx_id="rx_404"), _manifest(),
        )


# ---------------------------------------------------------------- (e) routes


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Playback", "project_id": PID})
    assert resp.status_code == 201, resp.text
    put = api_client.put(
        f"/api/projects/{PID}/scene", json=_scene(PID).model_dump(mode="json")
    )
    assert put.status_code == 200, put.text
    _write_manifest(_project_dir(PID), _manifest())
    return api_client


def _project_dir(project_id: str) -> Path:
    from seam_studio.api import deps

    return deps.get_store().resolve(project_id)


def _write_manifest(project_dir: Path, manifest: SensorManifest) -> None:
    path = project_dir / MANIFEST_REL
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.model_dump(mode="json")), encoding="utf-8")


def test_route_builds_persists_and_round_trips(client):
    resp = client.post(
        f"/api/projects/{PID}/simulate/playback", json={"config": CFG, "max_frames": 3}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "playback"
    assert [f["index"] for f in body["frames"]] == [0, 2, 5]
    assert body["tx_id"] == "tx_001" and body["rx_id"] == "rx_001"
    assert body["result_id"].startswith("mock_playback_")

    # The scene gained a playback ref pointing at the stored file.
    refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    playback_refs = [ref for ref in refs if ref["kind"] == "playback"]
    assert len(playback_refs) == 1
    assert playback_refs[0]["result_id"] == body["result_id"]

    # Latest, then the explicit id: both round-trip the persisted pack.
    for query in ("", f"?result_id={body['result_id']}"):
        got = client.get(f"/api/projects/{PID}/results/playback{query}")
        assert got.status_code == 200, got.text
        loaded = PlaybackResultSet.model_validate(got.json())
        assert loaded.result_id == body["result_id"]
        assert [f.index for f in loaded.frames] == [0, 2, 5]
        assert loaded.frames[0].rss_dbm == pytest.approx(body["frames"][0]["rss_dbm"])
        assert loaded.frames[0].beam_azimuth_deg == pytest.approx(
            body["frames"][0]["beam_azimuth_deg"]
        )


# ---------------------------------------------------- (f) no manifest -> 404


def test_route_404_when_project_has_no_sensor_data(api_client):
    resp = api_client.post(
        "/api/projects", json={"name": "NoSensors", "project_id": "playback_bare"}
    )
    assert resp.status_code == 201, resp.text
    api_client.put(
        "/api/projects/playback_bare/scene",
        json=_scene("playback_bare").model_dump(mode="json"),
    )

    got = api_client.post(
        "/api/projects/playback_bare/simulate/playback", json={"config": CFG}
    )
    assert got.status_code == 404
    assert "no sensor data" in got.json()["detail"]


def test_device_axis_wraps_to_canonical_range(tmp_path: Path):
    """Review finding: a west-facing array (yaw 170) pushed beam azimuths to
    110..230 while the GT convention is atan2's (-180, 180] — the merged chart
    then drew the two curves 360 deg apart. Pack azimuths must come out
    wrapped."""

    class SionnaLabeledMock(MockBackend):
        # Relabel so build_playback takes the 'device' axis branch (the mock
        # branch pins axis 0 and can never exercise the wrap).
        def simulate_beamforming(self, *args, **kwargs):
            result = super().simulate_beamforming(*args, **kwargs)
            result.backend = "sionna"
            return result

    scene = _scene()
    tx = next(d for d in scene.devices if d.kind == "tx")
    tx.orientation_deg = [170.0, 0.0, 0.0]
    manifest = _manifest()
    request = PlaybackBuildRequest(use_device_orientation=True)
    result = build_playback(
        SionnaLabeledMock(), tmp_path, scene, load_default_library(),
        SimulationConfig(id="default", backend="mock"), request, manifest,
    )
    for f in result.frames:
        for az in f.beam_azimuth_deg:
            assert -180.0 < az <= 180.0, az
        if f.best_beam_deg is not None:
            assert -180.0 < f.best_beam_deg <= 180.0
    # Sweep 110..230 wrapped: the run must contain angles from BOTH ends of
    # the canonical range (positive near 180 and negative past the seam).
    sample = result.frames[0].beam_azimuth_deg
    assert any(a > 150 for a in sample) and any(a < -100 for a in sample)
