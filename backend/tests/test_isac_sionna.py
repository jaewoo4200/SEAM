"""ISAC trade-off and sensing coverage against the real sionna-rt solvers.

Skipped without sionna-rt 2.2 (RCSSolver). When installed these pin what
the mock cannot:

- L1: the path-synthesized TX codebook of run_isac (single-element solve,
  sionna synthetic-array convention) reproduces /simulate/beamforming's
  4x4 codebook sweep with use_device_orientation: same best beam, same
  absolute best-beam power;
- L1b: the production RX synthesis (echo_beam_matrix's receive factor:
  arrival angles, the RX device's orientation) reproduces the RX sweep;
- L2: the coverage map's radar equation + Mitsuba two-leg LOS agree with a
  real RCS echo off a point target placed at a cell center (LOS cells), and
  a building's shadow has no direct echo; segment_los leaves the cached
  scene as a plain load. With tr38901 elements the map's per-leg element
  gain (sensing_coverage.element_gain_db) matches the echo too, and the
  closed forms equal sionna's own pattern functions;
- L1e: the elevation (2-D) codebook on a 4x1 vertical ULA, TX and RX side:
  run_isac's path synthesis and echo_beam_matrix's receive factor against
  the same steering vectors applied to Sionna's synthetic-array channel
  (PlanarArray(num_rows=4, num_cols=1)), best beam on the LoS elevation.
"""

import math
from pathlib import Path

import numpy as np
import pytest
import trimesh

from seam_studio.schemas.devices import Antenna, Device
from seam_studio.schemas.scene import Actor, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.sensing import (
    ISACRequest,
    SensingCoverageRequest,
    SensingSimulateRequest,
)
from seam_studio.schemas.simulation import BeamformingRequest, SimulationConfig
from seam_studio.services.availability import sionna_available, sionna_rcs_available
from seam_studio.services.isac import (
    codebook_angles,
    codebook_weights,
    codebook_weights_2d,
    echo_beam_matrix,
    plan_isac_roles,
    planar_positions,
    run_isac,
)
from seam_studio.services.project_store import load_default_library
from seam_studio.services.sensing import bistatic_radar_gain_db, select_targets
from seam_studio.services.sensing_coverage import element_gain_db, run_sensing_coverage
from seam_studio.services.simulation_backends.sionna_backend import (
    SionnaBackend,
    _load_scene_cached,
    noise_floor_dbm,
)

pytestmark = pytest.mark.skipif(
    not (sionna_available() and sionna_rcs_available()),
    reason="sionna-rt >= 2.2 (sionna.rt.rcs) not installed",
)

FREQ = 3.5e9  # ITU ground / concrete in band
SAMPLES = 200_000
C = 299_792_458.0


def _prim(prim_id: str, material: str, tags: list[str]) -> Prim:
    return Prim(
        id=f"/{prim_id}",
        name=prim_id,
        semantic_tags=tags,
        mesh_ref=MeshRef(mesh_name=prim_id),
        rf=RFBinding(
            material_id=material, assignment_status="user_confirmed", assignment_sources=["user"]
        ),
    )


def _write_site(project_dir: Path, boxes: dict[str, tuple[tuple, tuple]]) -> None:
    """Ground 200 x 200 m plus named boxes {name: (extents, center)}."""
    (project_dir / "visual").mkdir(parents=True, exist_ok=True)
    (project_dir / "rf").mkdir(exist_ok=True)
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(200.0, 200.0, 0.2))
    ground.apply_translation((0.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    for name, (extents, center) in boxes.items():
        box = trimesh.creation.box(extents=extents)
        box.apply_translation(center)
        tm.add_geometry(box, geom_name=name, node_name=name)
    (project_dir / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))


def _config(**overrides) -> SimulationConfig:
    base = dict(backend="sionna", frequency_hz=FREQ, max_depth=2, num_samples=SAMPLES)
    return SimulationConfig(**{**base, **overrides})


# ------------------------------------------------- L1: TX codebook synthesis


@pytest.fixture()
def wall_site(tmp_path: Path) -> Path:
    proj = tmp_path / "isac_l1.seam"
    _write_site(proj, {"wall": ((2.0, 40.0, 20.0), (-30.0, 0.0, 10.0))})
    return proj


def _l1_scene(yaw: float, pitch: float, ue: tuple) -> Scene:
    orientation = [yaw, pitch, 0.0]
    # The target hovers 20 m behind the panel, away from every comm leg, so
    # its absorber (ISAC comm solve) and its mesh (beamforming solve) do not
    # change the comm paths between the two solves.
    behind = [-20.0 * math.cos(math.radians(yaw)), -20.0 * math.sin(math.radians(yaw)), 15.0]
    return Scene(
        scene_id="isac_l1",
        prims=[
            _prim("ground", "ground", ["ground"]),
            _prim("wall", "itu_concrete", ["building", "wall"]),
        ],
        devices=[
            Device(id="tx", kind="tx", position=[0.0, 0.0, 15.0], power_dbm=30.0,
                   orientation_deg=orientation),
            Device(id="tx_rx", kind="rx", position=[0.0, 0.0, 15.0], orientation_deg=orientation),
            Device(id="ue", kind="rx", position=list(ue)),
        ],
        actors=[
            Actor(id="drone", kind="uav", position=[behind[0], behind[1], 15.0 - 0.125],
                  sensing={"model": "constant", "rcs_dbsm": 0.0}),
        ],
    )


def l1_cross_check(project: Path, yaw: float, pitch: float, ue: tuple) -> dict:
    """ISAC TX codebook vs /simulate/beamforming on one geometry (numbers
    returned for the report)."""
    scene = _l1_scene(yaw, pitch, ue)
    library = load_default_library()
    config = _config()
    backend = SionnaBackend()
    request = ISACRequest(tx_rows=4, tx_cols=4, samples_per_sp=SAMPLES)
    plan = plan_isac_roles(scene, None, None, None)
    assert [u.id for u in plan.ues] == ["ue"] and plan.sensing_rx["tx"].id == "tx_rx"
    isac = run_isac(
        backend, project, scene, library, config, request, plan, select_targets(scene, None)
    )
    bf = backend.simulate_beamforming(
        project, scene, library, config,
        BeamformingRequest(tx_id="tx", rx_id="ue", tx_rows=4, tx_cols=4, rx_rows=1, rx_cols=1,
                           use_device_orientation=True),
    )
    tx = isac.txs[0]
    sinr = [b.ue_sinr_db["ue"] for b in tx.beams]
    assert all(v is not None for v in sinr), isac.warnings
    k = int(np.argmax(sinr))
    n0 = noise_floor_dbm(config)
    curve_isac = np.array(sinr) + n0
    curve_bf = np.array(bf.sweep_gain_db[0]) + bf.single_element_dbm
    # Beams within 20 dB of the peak: near-null beams carry the float32
    # run-to-run noise of two separate solves (~0.1 dB).
    main = curve_bf >= curve_bf.max() - 20.0
    return {
        "isac_best_deg": tx.angles_deg[k],
        "bf_best_deg": bf.best_tx_angle_deg,
        "isac_best_dbm": sinr[k] + n0,
        "bf_best_dbm": bf.single_element_dbm + bf.codebook_gain_db,
        "max_curve_diff_db": float(np.max(np.abs(curve_isac - curve_bf))),
        "max_main_curve_diff_db": float(np.max(np.abs(curve_isac - curve_bf)[main])),
        "num_main_beams": int(main.sum()),
        "comm_beam_deg": tx.comm_beam_angle_deg,
        "warnings": isac.warnings + bf.warnings,
    }


@pytest.mark.parametrize(
    "yaw,pitch,ue",
    [(30.0, 0.0, (60.0, 60.0, 1.5)), (-20.0, -15.0, (70.0, -10.0, 1.5))],
)
def test_l1_tx_codebook_matches_beamforming(wall_site: Path, yaw, pitch, ue):
    r = l1_cross_check(wall_site, yaw, pitch, ue)
    assert abs(r["isac_best_deg"] - r["bf_best_deg"]) <= 5.0, r
    assert abs(r["isac_best_dbm"] - r["bf_best_dbm"]) <= 1.0, r
    assert r["comm_beam_deg"] == r["isac_best_deg"]
    # Beam for beam, not only the peak.
    assert r["num_main_beams"] >= 5 and r["max_main_curve_diff_db"] <= 0.1, r


# ------------------------------------------------- L1b: RX side convention


def l1b_cross_check(project: Path, rx_orientation, rx_pos) -> dict:
    scene = Scene(
        scene_id="isac_l1b",
        prims=[
            _prim("ground", "ground", ["ground"]),
            _prim("wall", "itu_concrete", ["building", "wall"]),
        ],
        devices=[
            Device(id="tx", kind="tx", position=[0.0, 0.0, 15.0], power_dbm=30.0),
            Device(id="ue", kind="rx", position=list(rx_pos), orientation_deg=list(rx_orientation)),
        ],
    )
    library = load_default_library()
    config = _config()
    backend = SionnaBackend()
    bf = backend.simulate_beamforming(
        project, scene, library, config,
        BeamformingRequest(tx_rows=1, tx_cols=1, rx_rows=4, rx_cols=4, use_device_orientation=True),
    )
    paths = backend.simulate_paths(project, scene, library, config).paths
    assert paths
    angles = codebook_angles(-60.0, 60.0, 5.0)
    pos = planar_positions(4, 4, 0.5, 0.5)
    w = codebook_weights(pos, angles)
    # The production RX synthesis (echo_beam_matrix's receive factor) with a
    # 1x1 TX: row 0 is the RX codebook sweep of these paths.
    one = planar_positions(1, 1, 0.5, 0.5)
    tx_dev, rx_dev = scene.devices
    h = echo_beam_matrix(paths, tx_dev, rx_dev, one, pos, codebook_weights(one, [0.0]), w)[0]
    m = int(np.argmax(np.abs(h)))
    return {
        "synth_best_deg": angles[m],
        "bf_best_deg": bf.best_rx_angle_deg,
        "synth_best_dbm": 30.0 + 20.0 * math.log10(abs(h[m])),
        "bf_best_dbm": bf.single_element_dbm + bf.codebook_gain_db,
    }


@pytest.mark.parametrize(
    "orientation,rx_pos",
    [([200.0, 0.0, 0.0], (60.0, 30.0, 1.5)), ([-150.0, -10.0, 0.0], (60.0, 30.0, 20.0))],
)
def test_l1b_rx_codebook_matches_beamforming(wall_site: Path, orientation, rx_pos):
    r = l1b_cross_check(wall_site, orientation, rx_pos)
    assert abs(r["synth_best_deg"] - r["bf_best_deg"]) <= 5.0, r
    assert abs(r["synth_best_dbm"] - r["bf_best_dbm"]) <= 1.0, r


# ------------------------------------------- L2: coverage vs real RCS echoes

HEIGHT = 30.0
RCS_DBSM = 10.0
TX_POS = [0.0, 0.0, 10.0]
# Cell centers of the explicit grid below (origin (-20, -60), 10 m cells).
LOS_CELLS = [(-15.0, -35.0), (5.0, 45.0), (25.0, -55.0), (65.0, 45.0), (85.0, -55.0)]
# Behind the 20 m building as seen from the 10 m TRP.
SHADOW_CELLS = [(85.0, 5.0), (85.0, -5.0)]


@pytest.fixture()
def building_site(tmp_path: Path) -> Path:
    proj = tmp_path / "coverage_l2.seam"
    _write_site(proj, {"building": ((20.0, 30.0, 20.0), (40.0, 0.0, 10.0))})
    return proj


def _l2_scene(probe_xy, pattern: str = "iso", yaw: float = 0.0) -> Scene:
    antenna = Antenna(pattern=pattern)
    orientation = [yaw, 0.0, 0.0]
    return Scene(
        scene_id="coverage_l2",
        prims=[
            _prim("ground", "ground", ["ground"]),
            _prim("building", "itu_concrete", ["building"]),
        ],
        devices=[
            Device(id="trp", kind="tx", position=list(TX_POS), power_dbm=30.0,
                   orientation_deg=orientation, antenna=antenna),
            Device(id="trp_rx", kind="rx", position=list(TX_POS),
                   orientation_deg=orientation, antenna=antenna.model_copy()),
        ],
        actors=[
            # 1 m cuboid centered at the cell center (base = height - 0.5).
            Actor(id="probe", kind="uav", position=[probe_xy[0], probe_xy[1], HEIGHT - 0.5],
                  sensing={"model": "constant", "rcs_dbsm": RCS_DBSM, "size_m": [1.0, 1.0, 1.0]}),
        ],
    )


def l2_cross_check(project: Path) -> dict:
    library = load_default_library()
    config = _config(max_depth=1, reflection=False)
    backend = SionnaBackend()
    request = SensingCoverageRequest(
        rcs_dbsm=RCS_DBSM, height_m=HEIGHT, cell_size_m=10.0,
        center_xy=[40.0, 0.0], size_xy=[120.0, 120.0], cpi_pulses=1,
    )
    coverage = run_sensing_coverage(
        backend, project, _l2_scene(LOS_CELLS[0]), library, config, request
    )
    grid = coverage.grid

    def cell_value(xy):
        i = int((xy[0] - grid.origin[0]) // grid.cell_size_m)
        j = int((xy[1] - grid.origin[1]) // grid.cell_size_m)
        return coverage.values["best_snr_db"][j][i]

    rt_scene = _load_scene_cached(project / "rf" / "generated_scene.xml", [])
    clean = (
        rt_scene.sensing_targets == {}
        and ("shape-actor-probe" in rt_scene.objects or "actor-probe" in rt_scene.objects)
    )

    n0 = noise_floor_dbm(config)
    lam = C / FREQ
    rows = []
    for xy in LOS_CELLS + SHADOW_CELLS:
        scene = _l2_scene(xy)
        echo = backend.simulate_sensing(
            project, scene, library, config,
            SensingSimulateRequest(samples_per_sp=SAMPLES), select_targets(scene, None),
        )
        direct = [p for p in echo.paths if [i.type for i in p.interactions] == ["sensing"]]
        r = math.dist(TX_POS, [xy[0], xy[1], HEIGHT])
        rows.append({
            "cell": xy,
            "coverage_snr_db": cell_value(xy),
            "echo_snr_db": max(p.power_dbm for p in direct) - n0 if direct else None,
            "echo_gain_db": max(p.path_gain_db for p in direct) if direct else None,
            "radar_eq_gain_db": bistatic_radar_gain_db(lam, RCS_DBSM, r, r),
            "warnings": echo.warnings,
        })
    return {"coverage": coverage, "rows": rows, "cache_clean": clean}


def test_l2_coverage_matches_rcs_echoes(building_site: Path):
    out = l2_cross_check(building_site)
    coverage = out["coverage"]
    assert coverage.metadata["los_model"].startswith("mitsuba ray_test")
    assert not [w for w in coverage.warnings if "failed" in w], coverage.warnings
    assert out["cache_clean"]
    rows = out["rows"]
    for row in rows[: len(LOS_CELLS)]:
        assert row["coverage_snr_db"] is not None, row
        assert row["echo_snr_db"] is not None, row
        assert abs(row["echo_gain_db"] - row["radar_eq_gain_db"]) <= 1.5, row
        assert abs(row["echo_snr_db"] - row["coverage_snr_db"]) <= 0.01, row
    for row in rows[len(LOS_CELLS):]:
        assert row["coverage_snr_db"] is None, row
        assert row["echo_snr_db"] is None, row
    assert 0.0 < coverage.summary.pct_cells_los < 100.0


def test_segment_los_basics(building_site: Path):
    backend = SionnaBackend()
    scene = _l2_scene((0.0, 50.0))
    library = load_default_library()
    probe_z = HEIGHT - 0.4  # inside the probe's box, whatever its height
    starts = np.array([TX_POS, TX_POS, [0.0, 0.0, 60.0], [0.0, 40.0, probe_z]])
    ends = np.array([[85.0, 5.0, 30.0], [5.0, 45.0, 30.0], [0.0, 0.0, -5.0], [0.0, 60.0, probe_z]])
    mask, warnings = backend.segment_los(building_site, scene, library, _config(), starts, ends)
    # Through the building, clear, through the ground, and through the
    # probe actor's own box (actor meshes are ignored).
    assert mask.tolist() == [False, True, False, True]
    assert not [w for w in warnings if "addressable" in w]
    rt_scene = _load_scene_cached(building_site / "rf" / "generated_scene.xml", [])
    assert "shape-actor-probe" in rt_scene.objects or "actor-probe" in rt_scene.objects
    assert getattr(rt_scene, "_seam_hidden_actor_objects", []) == []
    # Restored: the same leg through the probe box is blocked on the plain scene.
    import mitsuba as mi

    def f32(*v):
        return np.array(v, dtype=np.float32)

    ray = mi.Ray3f(mi.Point3f(f32(0.0), f32(40.0), f32(probe_z)), mi.Vector3f(f32(0.0), f32(1.0), f32(0.0)))
    ray.maxt = mi.Float(f32(20.0))
    assert np.array(rt_scene.mi_scene.ray_test(ray), dtype=bool).tolist() == [True]


# ------------------------------------------ L3: element patterns in coverage


def test_element_gain_matches_sionna_patterns():
    import drjit as dr
    import mitsuba as mi
    from sionna.rt import antenna_pattern as ap

    rng = np.random.default_rng(3)
    theta = rng.uniform(0.01, math.pi - 0.01, 200)
    phi = rng.uniform(-math.pi, math.pi, 200)
    # Local directions with an unrotated device: element_gain_db's frame.
    k = np.stack(
        [np.sin(theta) * np.cos(phi), np.sin(theta) * np.sin(phi), np.cos(theta)], axis=1
    )
    funcs = {
        "iso": ap.v_iso_pattern,
        "dipole": ap.v_dipole_pattern,
        "hw_dipole": ap.v_hw_dipole_pattern,
        "tr38901": ap.v_tr38901_pattern,
    }
    for pattern, func in funcs.items():
        c = func(mi.Float(theta.astype(np.float32)), mi.Float(phi.astype(np.float32)))
        # Linear power: sionna evaluates in float32, which is coarse in dB
        # only at the dipoles' axial nulls.
        ref = np.array(dr.real(c), dtype=float) ** 2 + np.array(dr.imag(c), dtype=float) ** 2
        dev = Device(id="d", kind="tx", position=[0.0, 0.0, 0.0], antenna=Antenna(pattern=pattern))
        mine = 10.0 ** (element_gain_db(dev, k) / 10.0)
        assert np.allclose(mine, ref, rtol=1e-4, atol=1e-6), pattern


@pytest.mark.parametrize("yaw", [0.0, 180.0])
def test_l2_coverage_matches_echo_with_tr38901(building_site: Path, yaw: float):
    # The map adds each leg's element gain in the device frame, as the
    # RCSSolver echo does (iso would be off by +8 / -44 dB here).
    library = load_default_library()
    config = _config(max_depth=1, reflection=False)
    backend = SionnaBackend()
    request = SensingCoverageRequest(
        rcs_dbsm=RCS_DBSM, height_m=HEIGHT, cell_size_m=10.0,
        center_xy=[40.0, 0.0], size_xy=[120.0, 120.0], cpi_pulses=1,
    )
    cells = [(65.0, 45.0), (-15.0, -35.0)]
    coverage = run_sensing_coverage(
        backend, building_site, _l2_scene(cells[0], "tr38901", yaw), library, config, request
    )
    grid = coverage.grid
    n0 = noise_floor_dbm(config)
    for xy in cells:
        scene = _l2_scene(xy, "tr38901", yaw)
        echo = backend.simulate_sensing(
            building_site, scene, library, config,
            SensingSimulateRequest(samples_per_sp=SAMPLES), select_targets(scene, None),
        )
        direct = [p for p in echo.paths if [i.type for i in p.interactions] == ["sensing"]]
        assert direct, echo.warnings
        i = int((xy[0] - grid.origin[0]) // grid.cell_size_m)
        j = int((xy[1] - grid.origin[1]) // grid.cell_size_m)
        echo_snr = max(p.power_dbm for p in direct) - n0
        assert abs(echo_snr - coverage.values["best_snr_db"][j][i]) <= 0.01, (xy, echo_snr)


# ------------------------------- L1e: elevation codebook on a vertical ULA


@pytest.fixture()
def keep_flush_counter(monkeypatch):
    """Leave sionna_backend's solve counter as found: its full cache flush
    every 32 solves would otherwise move with these extra solves and land
    inside another test's two-solve cache-hit check."""
    from seam_studio.services.simulation_backends import sionna_backend

    monkeypatch.setattr(
        sionna_backend, "_solves_since_full_flush", sionna_backend._solves_since_full_flush
    )


def _capture_beamforming_channel(monkeypatch) -> dict:
    """Keep the [rx_ant, tx_ant] channel /simulate/beamforming sweeps (the
    sweep itself still runs)."""
    from seam_studio.services.simulation_backends import sionna_backend

    captured: dict = {}
    original = sionna_backend._codebook_sweep

    def spy(base, H, h00, request, np_, tx_y, rx_y):
        captured["H"] = np.array(H)
        return original(base, H, h00, request, np_, tx_y, rx_y)

    monkeypatch.setattr(sionna_backend, "_codebook_sweep", spy)
    return captured


def _sionna_positions(rows: int, cols: int) -> np.ndarray:
    from seam_studio.services.simulation_backends.sionna_backend import _make_planar_array

    array = _make_planar_array(Antenna(), [], num_rows=rows, num_cols=cols)
    return np.asarray(array.normalized_positions, dtype=float)


def _local_elevation_deg(orientation, src, dst) -> float:
    from seam_studio.services.channel_npz_export import local_frame_matrix

    k = np.asarray(dst, dtype=float) - np.asarray(src, dtype=float)
    local = np.asarray(local_frame_matrix(orientation)).T @ (k / np.linalg.norm(k))
    return math.degrees(math.asin(local[2]))


ELEVATIONS = codebook_angles(-40.0, 60.0, 5.0)


def l1e_tx_cross_check(project: Path, monkeypatch, yaw: float, pitch: float, ue: tuple) -> dict:
    """Elevation beams of a 4x1 vertical TX ULA: run_isac's path synthesis vs
    the same manual steering vectors on Sionna's own synthetic-array channel
    (the L1 harness transposed)."""
    scene = _l1_scene(yaw, pitch, ue)
    library = load_default_library()
    config = _config()
    backend = SionnaBackend()
    request = ISACRequest(
        tx_rows=4, tx_cols=1, rx_rows=4, rx_cols=1, samples_per_sp=SAMPLES,
        sweep_start_deg=0.0, sweep_stop_deg=0.0, sweep_step_deg=5.0,
        elevation_start_deg=ELEVATIONS[0], elevation_stop_deg=ELEVATIONS[-1],
        elevation_step_deg=5.0,
    )
    plan = plan_isac_roles(scene, None, None, None)
    isac = run_isac(
        backend, project, scene, library, config, request, plan, select_targets(scene, None)
    )
    captured = _capture_beamforming_channel(monkeypatch)
    bf = backend.simulate_beamforming(
        project, scene, library, config,
        BeamformingRequest(tx_id="tx", rx_id="ue", tx_rows=4, tx_cols=1, rx_rows=1, rx_cols=1,
                           use_device_orientation=True),
    )
    pos = _sionna_positions(4, 1)
    w = codebook_weights_2d(pos, [0.0], ELEVATIONS)
    H = captured["H"]  # [1, 4]
    curve_bf = 20.0 * np.log10(np.abs(H[0] @ np.conj(w).T)) + 30.0
    tx = isac.txs[0]
    n0 = noise_floor_dbm(config)
    curve_isac = np.array([b.ue_sinr_db["ue"] for b in tx.beams]) + n0
    main = curve_bf >= curve_bf.max() - 20.0
    return {
        "positions_match": bool(np.allclose(pos, planar_positions(4, 1, 0.5, 0.5), atol=1e-6)),
        "isac_best_el": ELEVATIONS[int(np.argmax(curve_isac))],
        "bf_best_el": ELEVATIONS[int(np.argmax(curve_bf))],
        "los_el": _local_elevation_deg([yaw, pitch, 0.0], [0.0, 0.0, 15.0], ue),
        "comm_beam_el": tx.comm_beam_elevation_deg,
        "isac_best_dbm": float(curve_isac.max()),
        "bf_best_dbm": float(curve_bf.max()),
        "max_main_curve_diff_db": float(np.max(np.abs(curve_isac - curve_bf)[main])),
        "num_main_beams": int(main.sum()),
        "single_element_dbm": bf.single_element_dbm,
        "warnings": isac.warnings,
    }


@pytest.mark.parametrize(
    "yaw,pitch,ue",
    [(30.0, 0.0, (40.0, 25.0, 45.0)), (-20.0, -15.0, (60.0, -20.0, 1.5))],
)
def test_l1e_vertical_ula_elevation_codebook_matches_sionna(
    wall_site: Path, monkeypatch, keep_flush_counter, yaw, pitch, ue
):
    r = l1e_tx_cross_check(wall_site, monkeypatch, yaw, pitch, ue)
    assert r["positions_match"], r
    assert abs(r["isac_best_el"] - r["bf_best_el"]) <= 5.0, r
    assert r["comm_beam_el"] == r["isac_best_el"]
    # The best beam sits on the LoS elevation (within one 5 deg step).
    assert abs(r["isac_best_el"] - r["los_el"]) <= 5.0, r
    assert abs(r["isac_best_dbm"] - r["bf_best_dbm"]) <= 1.0, r
    assert r["num_main_beams"] >= 5 and r["max_main_curve_diff_db"] <= 0.1, r


def l1e_rx_cross_check(project: Path, monkeypatch, rx_orientation, rx_pos) -> dict:
    """RX side: echo_beam_matrix's receive factor with 2-D (elevation) beams
    on a 4x1 vertical RX ULA vs Sionna's channel to that ULA."""
    scene = Scene(
        scene_id="isac_l1e_rx",
        prims=[
            _prim("ground", "ground", ["ground"]),
            _prim("wall", "itu_concrete", ["building", "wall"]),
        ],
        devices=[
            Device(id="tx", kind="tx", position=[0.0, 0.0, 15.0], power_dbm=30.0),
            Device(id="ue", kind="rx", position=list(rx_pos), orientation_deg=list(rx_orientation)),
        ],
    )
    library = load_default_library()
    config = _config()
    backend = SionnaBackend()
    captured = _capture_beamforming_channel(monkeypatch)
    backend.simulate_beamforming(
        project, scene, library, config,
        BeamformingRequest(tx_rows=1, tx_cols=1, rx_rows=4, rx_cols=1, use_device_orientation=True),
    )
    paths = backend.simulate_paths(project, scene, library, config).paths
    assert paths
    pos = _sionna_positions(4, 1)
    w = codebook_weights_2d(pos, [0.0], ELEVATIONS)
    one = planar_positions(1, 1, 0.5, 0.5)
    tx_dev, rx_dev = scene.devices
    h = echo_beam_matrix(paths, tx_dev, rx_dev, one, pos, codebook_weights(one, [0.0]), w)[0]
    synth = 30.0 + 20.0 * np.log10(np.abs(h))
    H = captured["H"]  # [4, 1]
    curve_bf = 30.0 + 20.0 * np.log10(np.abs(np.conj(w) @ H[:, 0]))
    main = curve_bf >= curve_bf.max() - 20.0
    return {
        "synth_best_el": ELEVATIONS[int(np.argmax(synth))],
        "bf_best_el": ELEVATIONS[int(np.argmax(curve_bf))],
        "los_el": _local_elevation_deg(rx_orientation, rx_pos, [0.0, 0.0, 15.0]),
        "synth_best_dbm": float(synth.max()),
        "bf_best_dbm": float(curve_bf.max()),
        "max_main_curve_diff_db": float(np.max(np.abs(synth - curve_bf)[main])),
        "num_main_beams": int(main.sum()),
    }


@pytest.mark.parametrize(
    "orientation,rx_pos",
    [([200.0, 0.0, 0.0], (40.0, 20.0, 1.5)), ([-150.0, -10.0, 0.0], (50.0, 30.0, 45.0))],
)
def test_l1e_vertical_ula_rx_elevation_matches_sionna(
    wall_site: Path, monkeypatch, keep_flush_counter, orientation, rx_pos
):
    r = l1e_rx_cross_check(wall_site, monkeypatch, orientation, rx_pos)
    assert abs(r["synth_best_el"] - r["bf_best_el"]) <= 5.0, r
    assert abs(r["synth_best_el"] - r["los_el"]) <= 5.0, r
    assert abs(r["synth_best_dbm"] - r["bf_best_dbm"]) <= 1.0, r
    assert r["num_main_beams"] >= 5 and r["max_main_curve_diff_db"] <= 0.1, r
