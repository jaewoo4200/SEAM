"""Channel-dataset npz export (services/channel_npz_export.py + route).

Pins the contract an AODT/HYRAY consumer depends on: the 13 canonical keys
with the reference file's ranks and dtypes, per-link path axes sorted
strongest-first and zero-padded, is_nlos derived from the solved path types,
local-frame departure angles built from the TX device's own orientation, the
normalization_db amplitude convention, and - the part that makes it an
*export* rather than a solve - that nothing outside export/ is touched.

Mock backend throughout (one LoS + one ground bounce + one wall bounce per
link), so the expected orderings and angles are analytic.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest

from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import ChannelNpzExportRequest
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.channel_npz_export import (
    EXPORT_DIR_REL,
    METADATA_NAME,
    NPZ_KEYS,
    NPZ_NAME,
    ChannelNpzExportError,
    export_channel_npz,
    local_frame_matrix,
)
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.mock_backend import MockBackend

PID = "npz_src"

CFG = {
    "id": "npz",
    "name": "npz",
    "backend": "mock",
    "frequency_hz": 28e9,
    "max_depth": 3,
}

# Rank + dtype of every key in the lab's HYRAY_b000.npz reference layout.
REFERENCE_LAYOUT: dict[str, tuple[int, str]] = {
    "num_TRP": (0, "int64"),
    "num_trajectory": (0, "int64"),
    "batch": (0, "int64"),
    "dataset_TRP_pos": (2, "float32"),
    "dataset_TRP_ori_angle": (2, "float32"),
    "dataset_UE_pos": (2, "float32"),
    "sorted_dataset_ampl": (3, "complex128"),
    "sorted_dataset_azimuth": (3, "float32"),
    "sorted_dataset_elevation": (3, "float32"),
    "sorted_dataset_toa": (3, "float32"),
    "is_nlos": (2, "bool"),
    "ue_id": (1, "int32"),
    "time_idx": (1, "int32"),
}

UE_POSITIONS = [
    [25.0, 5.0, 1.5],
    [40.0, -12.0, 1.5],
    [10.0, 30.0, 1.5],
]


def _scene(scene_id: str = "npz") -> Scene:
    """Two TXs with DIFFERENT orientations (so the local-frame rotation is
    actually exercised) and one authored RX the probe clones its antenna from.
    The mock backend loads no meshes, so a ground prim is enough geometry."""
    return Scene(
        scene_id=scene_id,
        name="Channel npz",
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
                id="tx_001",
                name="TRP 1",
                kind="tx",
                position=[0.0, 0.0, 10.0],
                orientation_deg=[0.0, 0.0, 0.0],
                power_dbm=30.0,
            ),
            Device(
                id="tx_002",
                name="TRP 2",
                kind="tx",
                position=[60.0, 20.0, 12.0],
                orientation_deg=[135.0, -10.0, 0.0],
                power_dbm=30.0,
            ),
            Device(id="rx_001", name="UE", kind="rx", position=[25.0, 5.0, 1.5]),
        ],
    )


def _export(tmp_path: Path, **overrides) -> tuple[dict, Path]:
    project_dir = tmp_path / "proj"
    project_dir.mkdir(parents=True, exist_ok=True)
    request = ChannelNpzExportRequest(
        ue_positions=UE_POSITIONS, max_paths=8, **overrides
    )
    summary = export_channel_npz(
        MockBackend(),
        project_dir,
        _scene(),
        load_default_library(),
        SimulationConfig(**CFG),
        request,
        [list(p) for p in UE_POSITIONS],
    )
    return summary, project_dir


def _load(project_dir: Path):
    return np.load(project_dir / EXPORT_DIR_REL / NPZ_NAME)


# ---------------------------------------------------------------- (a) schema


def test_npz_carries_the_thirteen_reference_keys(tmp_path: Path):
    summary, project_dir = _export(tmp_path)
    data = _load(project_dir)

    assert set(data.files) == set(NPZ_KEYS) == set(REFERENCE_LAYOUT)
    for key, (rank, dtype) in REFERENCE_LAYOUT.items():
        arr = data[key]
        assert arr.ndim == rank, f"{key} rank {arr.ndim} != {rank}"
        assert arr.dtype == np.dtype(dtype), f"{key} dtype {arr.dtype} != {dtype}"

    u, t, p = len(UE_POSITIONS), 2, 8
    assert data["num_TRP"] == t
    assert data["batch"] == 0
    assert data["dataset_TRP_pos"].shape == (t, 3)
    assert data["dataset_TRP_ori_angle"].shape == (t, 2)
    assert data["dataset_UE_pos"].shape == (u, 3)
    for key in (
        "sorted_dataset_ampl",
        "sorted_dataset_azimuth",
        "sorted_dataset_elevation",
        "sorted_dataset_toa",
    ):
        assert data[key].shape == (u, t, p)
    assert data["is_nlos"].shape == (u, t)
    assert data["ue_id"].tolist() == [0, 1, 2]
    assert data["time_idx"].tolist() == [0, 0, 0]
    # Default ue_ids are distinct, so every UE is its own "trajectory".
    assert data["num_trajectory"] == u

    assert summary["num_tx"] == t
    assert summary["num_ue"] == u
    assert summary["link_count"] == u * t
    assert summary["max_paths"] == p
    assert summary["shapes"]["sorted_dataset_ampl"] == [u, t, p]
    assert summary["files"] == [
        f"{EXPORT_DIR_REL}/{NPZ_NAME}",
        f"{EXPORT_DIR_REL}/{METADATA_NAME}",
    ]
    assert summary["size_bytes"] > 0
    assert summary["elapsed_s"] >= 0.0


def test_trp_rows_follow_the_scene_devices(tmp_path: Path):
    _, project_dir = _export(tmp_path)
    data = _load(project_dir)
    assert data["dataset_TRP_pos"].tolist() == [[0.0, 0.0, 10.0], [60.0, 20.0, 12.0]]
    # [yaw_deg, pitch_deg] of each TX device, in degrees.
    assert data["dataset_TRP_ori_angle"].tolist() == [[0.0, 0.0], [135.0, -10.0]]
    assert data["dataset_UE_pos"].tolist() == [[float(c) for c in p] for p in UE_POSITIONS]


def test_metadata_sidecar_records_the_conventions(tmp_path: Path):
    _, project_dir = _export(tmp_path, normalization_db=-5.06)
    meta = json.loads(
        (project_dir / EXPORT_DIR_REL / METADATA_NAME).read_text(encoding="utf-8")
    )
    assert meta["layout"] == "aodt_hyray_npz_v1"
    assert meta["normalization_db"] == pytest.approx(-5.06)
    assert meta["tx_ids"] == ["tx_001", "tx_002"]
    assert "radians" in meta["conventions"]["sorted_dataset_azimuth"]
    assert meta["conventions"]["sorted_dataset_toa"].endswith("seconds")


# ------------------------------------------------- (b) ordering + padding


def test_paths_are_sorted_strongest_first_and_zero_padded(tmp_path: Path):
    _, project_dir = _export(tmp_path)
    data = _load(project_dir)
    ampl = data["sorted_dataset_ampl"]
    toa = data["sorted_dataset_toa"]

    for u in range(ampl.shape[0]):
        for t in range(ampl.shape[1]):
            mag = np.abs(ampl[u, t])
            filled = int((mag > 0).sum())
            assert filled > 0, "the mock backend always solves at least LoS"
            # Strongest-first over the filled prefix...
            assert np.all(np.diff(mag[:filled]) <= 1e-30)
            # ...and everything past it is exactly zero on every array.
            assert np.all(ampl[u, t, filled:] == 0)
            assert np.all(toa[u, t, filled:] == 0)
            assert np.all(data["sorted_dataset_azimuth"][u, t, filled:] == 0)
            assert np.all(data["sorted_dataset_elevation"][u, t, filled:] == 0)


def test_max_paths_truncates_to_the_strongest_and_warns(tmp_path: Path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    scene = _scene()
    library = load_default_library()
    config = SimulationConfig(**CFG)
    positions = [list(UE_POSITIONS[0])]

    full = export_channel_npz(
        MockBackend(), project_dir, scene, library, config,
        ChannelNpzExportRequest(ue_positions=positions, max_paths=8), positions,
    )
    assert full["warnings"] == [] or "max_paths" not in " ".join(full["warnings"])
    strongest = np.abs(_load(project_dir)["sorted_dataset_ampl"][0, 0, 0])

    capped = export_channel_npz(
        MockBackend(), project_dir, scene, library, config,
        ChannelNpzExportRequest(ue_positions=positions, max_paths=1), positions,
    )
    data = _load(project_dir)
    assert data["sorted_dataset_ampl"].shape == (1, 2, 1)
    # The single kept path is the strongest one the uncapped run reported.
    assert np.abs(data["sorted_dataset_ampl"][0, 0, 0]) == pytest.approx(strongest)
    assert any("max_paths=1" in w for w in capped["warnings"])
    assert capped["path_count"] == 2  # one path per link, two links


# ------------------------------------------------------------- (c) is_nlos


def test_is_nlos_tracks_the_solved_path_types(tmp_path: Path):
    """LoS on => every link has a los path => is_nlos False everywhere; LoS off
    => only bounces survive => is_nlos True everywhere."""
    _, project_dir = _export(tmp_path)
    assert not _load(project_dir)["is_nlos"].any()

    nlos_dir = tmp_path / "nlos"
    nlos_dir.mkdir()
    positions = [list(p) for p in UE_POSITIONS]
    export_channel_npz(
        MockBackend(),
        nlos_dir,
        _scene(),
        load_default_library(),
        SimulationConfig(**{**CFG, "los": False}),
        ChannelNpzExportRequest(ue_positions=positions, max_paths=8),
        positions,
    )
    data = np.load(nlos_dir / EXPORT_DIR_REL / NPZ_NAME)
    assert data["is_nlos"].all()
    # Reflections still populate the amplitude axis (NLOS != empty).
    assert np.abs(data["sorted_dataset_ampl"]).sum() > 0


# ------------------------------------------------- (d) amplitude convention


def test_normalization_db_scales_every_amplitude(tmp_path: Path):
    plain, plain_dir = _export(tmp_path / "a")
    shifted, shifted_dir = _export(tmp_path / "b", normalization_db=-5.06)
    a = _load(plain_dir)["sorted_dataset_ampl"]
    b = _load(shifted_dir)["sorted_dataset_ampl"]

    expected = 10.0 ** (-5.06 / 20.0)
    filled = np.abs(a) > 0
    assert filled.any()
    np.testing.assert_allclose(
        np.abs(b[filled]), np.abs(a[filled]) * expected, rtol=1e-9
    )
    # Phase is untouched by an amplitude normalization.
    np.testing.assert_allclose(np.angle(b[filled]), np.angle(a[filled]), atol=1e-12)
    assert plain["link_count"] == shifted["link_count"]


def test_amplitude_matches_the_path_gain_and_phase(tmp_path: Path):
    """|a| == 10**(path_gain_db/20) and arg(a) == phase_rad, per link."""
    _, project_dir = _export(tmp_path)
    scene = _scene()
    result = MockBackend().simulate_paths(
        project_dir, scene, load_default_library(), SimulationConfig(**CFG)
    )
    solved = [
        p for p in result.paths if p.tx_id == "tx_001" and p.rx_id == "rx_001"
    ]
    strongest = max(solved, key=lambda p: p.path_gain_db)
    a = _load(project_dir)["sorted_dataset_ampl"][0, 0, 0]
    assert abs(a) == pytest.approx(10.0 ** (strongest.path_gain_db / 20.0), rel=1e-6)
    assert math.cos(np.angle(a)) == pytest.approx(math.cos(strongest.phase_rad), abs=1e-6)


def test_toa_is_seconds(tmp_path: Path):
    _, project_dir = _export(tmp_path)
    toa = _load(project_dir)["sorted_dataset_toa"]
    # UE 0 is 25 m out and 8.5 m below tx_001 -> LoS delay ~ 88 ns.
    d = math.dist([0.0, 0.0, 10.0], UE_POSITIONS[0])
    assert toa[0, 0, 0] == pytest.approx(d / 299_792_458.0, rel=1e-3)


# --------------------------------------------------------- (e) local frame


def test_local_frame_matrix_matches_the_sionna_rotation_convention():
    """R = Rz(yaw) Ry(pitch) Rx(roll), the matrix sionna-rt builds from the
    orientation triple sionna_backend hands its Transmitter."""
    a, b, g = 0.3, 0.2, 0.1  # radians
    R = np.asarray(local_frame_matrix([math.degrees(a), math.degrees(b), math.degrees(g)]))

    def rz(t):
        return np.array([[math.cos(t), -math.sin(t), 0], [math.sin(t), math.cos(t), 0], [0, 0, 1]])

    def ry(t):
        return np.array([[math.cos(t), 0, math.sin(t)], [0, 1, 0], [-math.sin(t), 0, math.cos(t)]])

    def rx(t):
        return np.array([[1, 0, 0], [0, math.cos(t), -math.sin(t)], [0, math.sin(t), math.cos(t)]])

    np.testing.assert_allclose(R, rz(a) @ ry(b) @ rx(g), atol=1e-12)
    # Orthonormal, so world -> local is the transpose (what the exporter uses).
    np.testing.assert_allclose(R @ R.T, np.eye(3), atol=1e-12)


def test_departure_angles_are_in_each_tx_local_frame(tmp_path: Path):
    """tx_001 has zero orientation (local == world) and tx_002 is yawed 135 deg
    / pitched -10 deg: the same UE therefore gets different local angles, and
    the zero-orientation TX's angles equal the world LoS bearing."""
    _, project_dir = _export(tmp_path)
    data = _load(project_dir)
    az = data["sorted_dataset_azimuth"]
    el = data["sorted_dataset_elevation"]

    ue = np.asarray(UE_POSITIONS[0])
    d = ue - np.asarray([0.0, 0.0, 10.0])
    d = d / np.linalg.norm(d)
    assert az[0, 0, 0] == pytest.approx(math.atan2(d[1], d[0]), abs=1e-5)
    # Elevation is the ZENITH angle (0 = straight up), not elevation-above-XY.
    assert el[0, 0, 0] == pytest.approx(math.acos(d[2]), abs=1e-5)
    assert 0.0 <= el.min() and el.max() <= math.pi
    assert -math.pi <= az.min() and az.max() <= math.pi

    # Same UE, rotated TX: the local angle is the world bearing pushed through
    # R.T of the tx_002 orientation.
    R = np.asarray(local_frame_matrix([135.0, -10.0, 0.0]))
    d2 = ue - np.asarray([60.0, 20.0, 12.0])
    d2 = d2 / np.linalg.norm(d2)
    v = R.T @ d2
    assert az[0, 1, 0] == pytest.approx(math.atan2(v[1], v[0]), abs=1e-5)
    assert el[0, 1, 0] == pytest.approx(math.acos(v[2]), abs=1e-5)
    assert az[0, 1, 0] != pytest.approx(az[0, 0, 0], abs=1e-3)


# ------------------------------------------------------- (f) passthroughs


def test_ue_ids_and_time_idx_pass_through(tmp_path: Path):
    _, project_dir = _export(tmp_path, ue_ids=[7, 7, 9], time_idx=[0, 1, 0], batch=3)
    data = _load(project_dir)
    assert data["ue_id"].tolist() == [7, 7, 9]
    assert data["time_idx"].tolist() == [0, 1, 0]
    assert data["batch"] == 3
    assert data["num_trajectory"] == 2  # two distinct ue ids


def test_tx_ids_select_and_order_the_trp_axis(tmp_path: Path):
    _, project_dir = _export(tmp_path, tx_ids=["tx_002"])
    data = _load(project_dir)
    assert data["num_TRP"] == 1
    assert data["dataset_TRP_pos"].tolist() == [[60.0, 20.0, 12.0]]
    assert data["sorted_dataset_ampl"].shape == (3, 1, 8)


def test_unknown_tx_id_raises(tmp_path: Path):
    with pytest.raises(ChannelNpzExportError, match="unknown tx device: nope"):
        _export(tmp_path, tx_ids=["nope"])


def test_empty_ue_list_raises(tmp_path: Path):
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    with pytest.raises(ChannelNpzExportError, match="no UE positions"):
        export_channel_npz(
            MockBackend(), project_dir, _scene(), load_default_library(),
            SimulationConfig(**CFG),
            ChannelNpzExportRequest(ue_positions=UE_POSITIONS), [],
        )


# ------------------------------------------------------------- (g) routes


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Npz", "project_id": PID})
    assert resp.status_code == 201, resp.text
    put = api_client.put(
        f"/api/projects/{PID}/scene", json=_scene(PID).model_dump(mode="json")
    )
    assert put.status_code == 200, put.text
    return api_client


def _project_dir(project_id: str) -> Path:
    from seam_studio.api import deps

    return deps.get_store().resolve(project_id)


def test_route_writes_the_npz_and_returns_the_summary(client):
    resp = client.post(
        f"/api/projects/{PID}/export/channel-npz",
        json={"config": CFG, "ue_positions": UE_POSITIONS, "max_paths": 16},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["export_dir"] == EXPORT_DIR_REL
    assert body["num_ue"] == 3 and body["num_tx"] == 2
    assert body["link_count"] == 6
    assert body["shapes"]["sorted_dataset_ampl"] == [3, 2, 16]

    data = np.load(_project_dir(PID) / EXPORT_DIR_REL / NPZ_NAME)
    assert set(data.files) == set(NPZ_KEYS)
    assert data["sorted_dataset_ampl"].shape == (3, 2, 16)


def test_export_leaves_the_scene_and_results_untouched(client):
    project_dir = _project_dir(PID)
    scene_file = next(project_dir.glob("scene.*.json"))
    before_scene = scene_file.read_bytes()
    before_results = sorted(p.name for p in (project_dir / "results").glob("*")) if (
        project_dir / "results"
    ).exists() else []
    before_refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]

    resp = client.post(
        f"/api/projects/{PID}/export/channel-npz",
        json={"config": CFG, "ue_positions": UE_POSITIONS, "max_paths": 4},
    )
    assert resp.status_code == 200, resp.text

    # Byte-identical scene file: no revision bump, no result ref, and above all
    # no leftover UE probe device.
    assert scene_file.read_bytes() == before_scene
    after_results = sorted(p.name for p in (project_dir / "results").glob("*")) if (
        project_dir / "results"
    ).exists() else []
    assert after_results == before_results
    after_refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    assert after_refs == before_refs
    device_ids = {d["id"] for d in client.get(f"/api/projects/{PID}/scene").json()["devices"]}
    assert device_ids == {"tx_001", "tx_002", "rx_001"}


def test_route_ue_source_devices_uses_the_scene_receivers(client):
    resp = client.post(
        f"/api/projects/{PID}/export/channel-npz",
        json={"config": CFG, "ue_source": "devices", "max_paths": 4},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["num_ue"] == 1
    data = np.load(_project_dir(PID) / EXPORT_DIR_REL / NPZ_NAME)
    assert data["dataset_UE_pos"].tolist() == [[25.0, 5.0, 1.5]]


def test_route_ue_source_trajectory_reads_a_stored_result(client):
    solved = client.post(
        f"/api/projects/{PID}/simulate/trajectory",
        json={
            "config": CFG,
            "ue_id": "rx_001",
            "start_m": [10.0, 0.0, 1.5],
            "end_m": [40.0, 0.0, 1.5],
            "num_points": 4,
        },
    )
    assert solved.status_code == 200, solved.text

    resp = client.post(
        f"/api/projects/{PID}/export/channel-npz",
        json={"config": CFG, "ue_source": "trajectory", "max_paths": 4},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["num_ue"] == 4
    data = np.load(_project_dir(PID) / EXPORT_DIR_REL / NPZ_NAME)
    assert data["dataset_UE_pos"][0].tolist() == pytest.approx([10.0, 0.0, 1.5])
    assert data["dataset_UE_pos"][-1].tolist() == pytest.approx([40.0, 0.0, 1.5])


def test_route_rejects_explicit_source_without_positions(client):
    resp = client.post(
        f"/api/projects/{PID}/export/channel-npz", json={"config": CFG}
    )
    assert resp.status_code == 422, resp.text


def test_route_rejects_a_mismatched_passthrough_column(client):
    resp = client.post(
        f"/api/projects/{PID}/export/channel-npz",
        json={"config": CFG, "ue_positions": UE_POSITIONS, "ue_ids": [1, 2]},
    )
    assert resp.status_code == 400
    assert "ue_ids" in resp.json()["detail"]


def test_route_404_when_devices_source_has_no_receiver(api_client):
    bare = _scene("npz_bare")
    bare.devices = [d for d in bare.devices if d.kind != "rx"]
    assert api_client.post(
        "/api/projects", json={"name": "Bare", "project_id": "npz_bare"}
    ).status_code == 201
    api_client.put(
        "/api/projects/npz_bare/scene", json=bare.model_dump(mode="json")
    )
    resp = api_client.post(
        "/api/projects/npz_bare/export/channel-npz",
        json={"config": CFG, "ue_source": "devices"},
    )
    assert resp.status_code == 404
    assert "no rx device" in resp.json()["detail"]


def test_route_logs_provenance(client):
    client.post(
        f"/api/projects/{PID}/export/channel-npz",
        json={"config": CFG, "ue_positions": UE_POSITIONS, "max_paths": 4},
    )
    data = json.loads(
        (_project_dir(PID) / "provenance.json").read_text(encoding="utf-8")
    )
    exports = [e for e in data["events"] if e.get("type") == "export_channel_npz"]
    assert len(exports) == 1
    assert exports[0]["num_ue"] == 3 and exports[0]["ue_source"] == "explicit"
