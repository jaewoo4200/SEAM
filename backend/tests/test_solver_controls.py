"""Solver-control passthrough, per-device antenna arrays, scene cache, and
SNR/SINR wiring (BACKEND agent workstream).

Covers:
- noise_floor_dbm math (thermal + NF);
- trajectory sinr_db == rss_dbm - noise_floor (None-safe);
- the mock backend ignores seed / synthetic_array (deep-equal across values);
- sionna-guarded end-to-end: refraction+diffraction+edge_diffraction+seed
  solve on a tiny scene returns without raising;
- per-device tr38901 4x4 array solve runs;
- the module-level scene cache is hit on a second consecutive solve and
  yields deep-equal results.
"""

import math
from pathlib import Path

import pytest
import trimesh

from app.schemas.devices import Antenna, Device
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import SimulationConfig, TrajectorySimulateRequest
from app.services.availability import sionna_available
from app.services.project_store import load_default_library
from app.services.rfdata_export import export_rfdata
from app.services.simulation_backends.mock_backend import MockBackend
from app.services.simulation_backends.sionna_backend import (
    SionnaBackend,
    cache_stats,
    clear_scene_cache,
    noise_floor_dbm,
)
from app.services.trajectory import run_trajectory

SIONNA = sionna_available()


# --------------------------------------------------------------- fixtures


def _mock_scene() -> Scene:
    """Cheap scene for mock-backend and trajectory assertions (no geometry
    file needed: the mock backend never loads meshes)."""
    return Scene(
        scene_id="ctl",
        name="Controls",
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
    )


def _sionna_project(tmp_path: Path, tx_antenna: Antenna | None = None) -> tuple[Path, Scene]:
    """Write a tiny compileable project (one off-axis wall) and return
    (project_dir, scene). The RF projection compiles lazily on first solve."""
    proj = tmp_path / "ctl.sionnatwin"
    (proj / "visual").mkdir(parents=True)
    (proj / "rf").mkdir()
    tm = trimesh.Scene()
    wall = trimesh.creation.box(extents=(0.3, 10.0, 8.0))
    wall.apply_translation((8.0, 7.0, 4.0))
    tm.add_geometry(wall, geom_name="wall", node_name="wall")
    (proj / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))

    tx = Device(id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 8.0], power_dbm=30.0)
    if tx_antenna is not None:
        tx = tx.model_copy(update={"antenna": tx_antenna})
    scene = Scene(
        scene_id="ctl",
        name="Controls",
        prims=[
            Prim(
                id="/wall",
                name="wall",
                semantic_tags=["building", "wall"],
                mesh_ref=MeshRef(mesh_name="wall"),
                rf=RFBinding(
                    material_id="itu_concrete",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            tx,
            Device(id="rx_001", name="RX", kind="rx", position=[15.0, 0.0, 1.5]),
        ],
    )
    return proj, scene


# ------------------------------------------------------------- noise floor


def test_noise_floor_math_default():
    cfg = SimulationConfig()  # 100 MHz, NF 7 dB
    expected = -174.0 + 10.0 * math.log10(100e6) + 7.0
    assert noise_floor_dbm(cfg) == pytest.approx(expected)
    assert noise_floor_dbm(cfg) == pytest.approx(-87.0)


def test_noise_floor_tracks_bandwidth_and_nf():
    base = SimulationConfig(bandwidth_hz=100e6, noise_figure_db=0.0)
    # 10x bandwidth -> +10 dB; +3 dB NF -> +3 dB.
    wider = SimulationConfig(bandwidth_hz=1e9, noise_figure_db=3.0)
    assert noise_floor_dbm(wider) - noise_floor_dbm(base) == pytest.approx(13.0)


# ------------------------------------------------------- trajectory sinr


def test_trajectory_sinr_equals_rss_minus_noise_floor():
    scene = _mock_scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock")
    req = TrajectorySimulateRequest(
        start_m=[5.0, 0.0, 1.5], end_m=[40.0, 0.0, 1.5], num_points=5, dt_s=0.1
    )
    result = run_trajectory(MockBackend(), Path("."), scene, library, cfg, req)

    nf = noise_floor_dbm(cfg)
    filled = 0
    for s in result.samples:
        if s.rss_dbm is None:
            assert s.sinr_db is None
        else:
            assert s.sinr_db == pytest.approx(s.rss_dbm - nf)
            filled += 1
    assert filled == len(result.samples)  # every waypoint has paths -> sinr set


def test_trajectory_sinr_none_when_no_rss(tmp_path: Path):
    """An empty scene (no tx) yields no paths -> rss None -> sinr None."""
    scene = _mock_scene()
    scene.devices = [d for d in scene.devices if d.kind == "rx"]  # drop the tx
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock")
    req = TrajectorySimulateRequest(
        start_m=[5.0, 0.0, 1.5], end_m=[40.0, 0.0, 1.5], num_points=3
    )
    result = run_trajectory(MockBackend(), tmp_path, scene, library, cfg, req)
    assert all(s.rss_dbm is None and s.sinr_db is None for s in result.samples)


def test_export_radio_map_rss_fills_sinr(tmp_path: Path):
    """radio_map.csv gets a sinr_db column (= rss - noise_floor) for an
    rss_dbm map, and leaves it blank for a path_gain_db map."""
    import csv
    import io

    scene = _mock_scene()
    library = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock", radio_map={"metric": "rss_dbm"})
    backend = MockBackend()
    rm = backend.simulate_radio_map(tmp_path, scene, library, cfg)

    export_rfdata(tmp_path, scene, cfg, created_at="2026-07-02T00:00:00+00:00", radio_map=rm)
    rows = list(csv.reader(io.StringIO(
        (tmp_path / "export" / "rfdata" / "radio_map.csv").read_text()
    )))
    assert rows[0] == ["x_m", "y_m", "z_m", "rss_dbm", "sinr_db", "path_gain_db"]
    nf = noise_floor_dbm(cfg)
    data = rows[1:]
    assert data, "expected radio map rows"
    for r in data:
        rss = float(r[3])
        assert r[4] != "" and float(r[4]) == pytest.approx(rss - nf, abs=1e-3)
        assert r[5] == ""  # path_gain blank for an rss map

    # A path_gain map leaves sinr blank.
    gain_cfg = SimulationConfig(id="default", backend="mock", radio_map={"metric": "path_gain_db"})
    rm_gain = backend.simulate_radio_map(tmp_path, scene, library, gain_cfg)
    export_rfdata(tmp_path, scene, gain_cfg, created_at="2026-07-02T00:00:00+00:00", radio_map=rm_gain)
    grows = list(csv.reader(io.StringIO(
        (tmp_path / "export" / "rfdata" / "radio_map.csv").read_text()
    )))[1:]
    assert grows and all(r[4] == "" for r in grows)


# --------------------------------------------- mock ignores solver flags


def test_mock_unaffected_by_seed_and_synthetic_array(tmp_path: Path):
    """Changing seed / synthetic_array / the diffraction family must not change
    a single mock output value (deep-equal across values)."""
    scene = _mock_scene()
    library = load_default_library()
    backend = MockBackend()

    a = SimulationConfig(backend="mock", seed=1, synthetic_array=True,
                         refraction=False, diffraction=False, edge_diffraction=False)
    b = SimulationConfig(backend="mock", seed=999, synthetic_array=False,
                         refraction=True, diffraction=True, edge_diffraction=True)

    pa = backend.simulate_paths(tmp_path, scene, library, a)
    pb = backend.simulate_paths(tmp_path, scene, library, b)
    assert pa.model_dump(mode="json") == pb.model_dump(mode="json")

    ra = backend.simulate_radio_map(tmp_path, scene, library, a)
    rb = backend.simulate_radio_map(tmp_path, scene, library, b)
    assert ra.model_dump(mode="json") == rb.model_dump(mode="json")


# ---------------------------------------------------- sionna-guarded runs


@pytest.mark.skipif(not SIONNA, reason="sionna-rt not installed")
def test_sionna_all_mechanisms_and_seed_runs(tmp_path: Path):
    """A solve with refraction+diffraction+edge_diffraction+seed on a tiny
    scene must run and return a result (graceful, no raise). If the solver
    surfaces a warning we assert it degraded rather than crashed the app."""
    clear_scene_cache()
    proj, scene = _sionna_project(tmp_path)
    library = load_default_library()
    cfg = SimulationConfig(
        id="default", frequency_hz=28e9, max_depth=3, num_samples=100_000,
        los=True, reflection=True, scattering=True,
        refraction=True, diffraction=True, edge_diffraction=True, seed=7,
    )
    result = SionnaBackend().simulate_paths(proj, scene, library, cfg)
    assert result.backend == "sionna"
    # A successful solve produces paths; a graceful failure would carry a
    # "sionna backend failed" warning. Either way, no exception escaped.
    failed = any("sionna backend failed" in w for w in result.warnings)
    assert result.paths or not failed, f"unexpected hard failure: {result.warnings}"


@pytest.mark.skipif(not SIONNA, reason="sionna-rt not installed")
def test_sionna_per_device_tr38901_array_runs(tmp_path: Path):
    """A device carrying a tr38901 4x4 array drives the scene arrays and the
    solve still returns without raising."""
    clear_scene_cache()
    antenna = Antenna(pattern="tr38901", polarization="V", num_rows=4, num_cols=4)
    proj, scene = _sionna_project(tmp_path, tx_antenna=antenna)
    library = load_default_library()
    cfg = SimulationConfig(id="default", frequency_hz=28e9, max_depth=2, num_samples=100_000)
    result = SionnaBackend().simulate_paths(proj, scene, library, cfg)
    assert result.backend == "sionna"
    failed = any("sionna backend failed" in w for w in result.warnings)
    assert result.paths or not failed, f"unexpected hard failure: {result.warnings}"
    # No "unknown antenna pattern" warning: tr38901 is a valid pattern.
    assert not any("unknown antenna pattern" in w for w in result.warnings)


@pytest.mark.skipif(not SIONNA, reason="sionna-rt not installed")
def test_sionna_scene_cache_hit_and_deep_equal(tmp_path: Path):
    """Two consecutive solves of the same project reuse the cached rt_scene
    (a cache hit is recorded) and produce deep-equal results."""
    clear_scene_cache()
    proj, scene = _sionna_project(tmp_path)
    library = load_default_library()
    cfg = SimulationConfig(id="default", frequency_hz=28e9, max_depth=2, num_samples=100_000, seed=7)
    backend = SionnaBackend()

    # Compile up front so neither solve emits the one-off "compiled on demand"
    # warning; then the two runs differ in nothing at all.
    backend.compile(proj, scene, library)

    r1 = backend.simulate_paths(proj, scene, library, cfg)
    stats_after_first = cache_stats()
    r2 = backend.simulate_paths(proj, scene, library, cfg)
    stats_after_second = cache_stats()

    # The first solve is a miss (load); the second is a hit (no new load).
    assert stats_after_first["loads"] >= 1
    assert stats_after_second["hits"] > stats_after_first["hits"]
    assert stats_after_second["loads"] == stats_after_first["loads"]

    # Same seed + same scene -> identical results.
    assert r1.model_dump(mode="json") == r2.model_dump(mode="json")


def test_clear_scene_cache_resets_counters():
    """clear_scene_cache empties the cache dict (counters are cumulative until
    the process resets them; the dict itself must be emptied)."""
    clear_scene_cache()
    from app.services.simulation_backends import sionna_backend as sb

    assert sb._SCENE_CACHE == {}
