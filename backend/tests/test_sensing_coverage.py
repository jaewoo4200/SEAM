"""Sensing coverage map: grid sizing, the bistatic radar equation per cell,
LOS gating through the backend's segment_los hook, the per-cell link and
fusion counts, request validation, POST /simulate/sensing-coverage, and the
Phase C detector models with their Monte Carlo spot check.

Mock backend throughout (no occlusion); the Mitsuba LOS test and the
cross-check against real RCS echoes are in test_isac_sionna.py.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Scene, SceneBounds
from seam_studio.schemas.sensing import (
    TR38901_NOMINAL_RCS_DBSM,
    SensingCoverageRequest,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.detector import pd_swerling
from seam_studio.services.project_store import load_default_library
from seam_studio.services.sensing import (
    ResolvedSensingTarget,
    bistatic_radar_gain_db,
)
from seam_studio.services.sensing_coverage import (
    SensingCoverageError,
    cell_centers,
    coverage_grid,
    element_gain_db,
    geometry_groups,
    mc_spot_cells,
    polarization_loss_db,
    run_sensing_coverage,
)
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm

C = 299_792_458.0
PID = "coverage_api"
MOCK_CFG = {"id": "default", "backend": "mock", "frequency_hz": 3.5e9}


def _dev(device_id, kind, position, **kw):
    return Device(id=device_id, kind=kind, position=list(position), **kw)


def _scene(n_tx: int = 1, scene_id: str = "cov") -> Scene:
    """n_tx TRPs at 25 m with a co-located sensing rx each."""
    sites = [(0.0, 0.0, 25.0), (150.0, 0.0, 25.0), (60.0, 120.0, 25.0)]
    devices = []
    for i in range(n_tx):
        devices.append(_dev(f"t{i + 1}", "tx", sites[i], power_dbm=43.0))
        devices.append(_dev(f"t{i + 1}_rx", "rx", sites[i]))
    return Scene(scene_id=scene_id, devices=devices)


def _run(scene: Scene, backend=None, config=None, **request):
    req = SensingCoverageRequest(**request)
    return run_sensing_coverage(
        backend or MockBackend(), Path("."), scene, load_default_library(),
        config or SimulationConfig(**MOCK_CFG), req,
    )


# ----------------------------------------------------------------- grid


def test_coverage_grid_explicit_extent():
    req = SensingCoverageRequest(center_xy=[10.0, -5.0], size_xy=[95.0, 40.0], cell_size_m=10.0, height_m=30.0)
    grid, warnings = coverage_grid(req, None)
    assert warnings == []
    assert grid.origin == [10.0 - 47.5, -5.0 - 20.0, 30.0]
    assert (grid.nx, grid.ny, grid.cell_size_m, grid.height_m) == (10, 4, 10.0, 30.0)
    centers = cell_centers(grid)
    assert centers.shape == (40, 3)
    # Row-major: index j * nx + i.
    assert centers[0].tolist() == [-37.5 + 5.0, -25.0 + 5.0, 30.0]
    assert centers[1].tolist() == [-37.5 + 15.0, -25.0 + 5.0, 30.0]
    assert centers[10].tolist() == [-37.5 + 5.0, -25.0 + 15.0, 30.0]


def test_coverage_grid_auto_extent_padding():
    req = SensingCoverageRequest(cell_size_m=10.0)
    bounds = SceneBounds(min=[-100.0, 0.0, 0.0], max=[100.0, 50.0, 20.0])
    grid, _ = coverage_grid(req, bounds)
    # pad = min(15, max(3, 0.15 * 200)) = 15
    assert grid.origin[:2] == [-115.0, -15.0]
    assert (grid.nx, grid.ny) == (23, 8)
    small = SceneBounds(min=[0.0, 0.0, 0.0], max=[8.0, 6.0, 3.0])
    grid, _ = coverage_grid(SensingCoverageRequest(cell_size_m=1.0), small)
    # pad = max(3, 1.2) = 3
    assert grid.origin[:2] == [-3.0, -3.0] and (grid.nx, grid.ny) == (14, 12)
    with pytest.raises(ValueError, match="no geometry or devices"):
        coverage_grid(req, None)


def test_coverage_grid_cap():
    req = SensingCoverageRequest(center_xy=[0.0, 0.0], size_xy=[3000.0, 3000.0], cell_size_m=1.0)
    grid, warnings = coverage_grid(req, None)
    assert grid.nx * grid.ny <= 40_000
    assert grid.cell_size_m > 15.0
    assert len(warnings) == 1
    assert warnings[0].startswith("coverage grid capped at 40000 cells: cell size coarsened from 1 m to ")


def test_geometry_groups():
    a, b, c = [0, 0, 25], [150, 0, 25], [60, 120, 25]
    links = [(a, a), (a, b), (b, a), (b, b), (a, [0.2, 0, 25]), (c, a)]
    # Reciprocal pair one group; a co-located pair counts as the monostatic one.
    assert geometry_groups(links) == [0, 1, 1, 2, 0, 3]


@pytest.mark.parametrize("offset", [0.0, 0.4, 0.6, 0.8, 1.0])
def test_geometry_groups_with_offset_sensing_panels(offset):
    # 4 TRPs whose sensing rx sits `offset` m from the tx (auto pairing takes
    # up to 1 m): A -> B_rx and B -> A_rx are still one ellipsoid, so the 16
    # links span 4 monostatic + 6 bistatic = 10 geometries at every offset.
    sites = [(0.0, 0.0, 25.0), (150.0, 0.0, 25.0), (60.0, 120.0, 25.0), (-80.0, 90.0, 25.0)]
    txs = [list(s) for s in sites]
    rxs = [[s[0] + offset, s[1], s[2]] for s in sites]
    groups = geometry_groups([(t, r) for t in txs for r in rxs])
    assert len(set(groups)) == 10
    assert groups[0 * 4 + 1] == groups[1 * 4 + 0]
    assert len({groups[i * 4 + i] for i in range(4)}) == 4
    # 1.5 m apart is a separate site again.
    far = geometry_groups([(txs[0], [1.5, 0.0, 25.0]), (txs[0], txs[0])])
    assert far == [0, 1]


# ------------------------------------------------------- radar equation


def test_radar_equation_one_cell_by_hand():
    scene = _scene(1)
    result = _run(scene, center_xy=[50.0, 20.0], size_xy=[40.0, 20.0], cell_size_m=10.0, height_m=60.0)
    assert result.tx_ids == ["t1"] and result.sensing_rx_ids == ["t1_rx"]
    assert result.rcs_dbsm == TR38901_NOMINAL_RCS_DBSM["uav-small-size"] == -12.81
    config = SimulationConfig(**MOCK_CFG)
    lam = C / config.frequency_hz
    j, i = 1, 2
    cell = [30.0 + (i + 0.5) * 10.0, 10.0 + (j + 0.5) * 10.0, 60.0]
    r = math.dist(cell, [0.0, 0.0, 25.0])
    expected = (
        43.0
        + 10 * math.log10(lam**2 * 10 ** (-12.81 / 10) / ((4 * math.pi) ** 3 * r**4))
        - noise_floor_dbm(config)
        + 10 * math.log10(4096)
    )
    assert result.values["best_snr_db"][j][i] == pytest.approx(expected, abs=1e-9)
    assert result.values["pd_best"][j][i] == pytest.approx(
        1e-6 ** (1 / (1 + 10 ** (expected / 10))), abs=1e-12
    )
    assert result.metadata["los_model"] == "none (mock: no geometry occlusion)"
    # 4 x 2 cells x 2 devices.
    assert result.metadata["num_rays"] == 8 * 2
    assert any("no geometry occlusion" in w for w in result.warnings)


def test_bistatic_radar_gain_matches_the_mock_echo():
    config = SimulationConfig(**MOCK_CFG)
    lam = C / config.frequency_hz
    tx = _dev("t", "tx", (0, 0, 10), power_dbm=30.0)
    rx = _dev("r", "rx", (40, 30, 2))
    target = ResolvedSensingTarget(
        actor_id="a", model="constant", object_type=None, model_type=None, rcs_dbsm=7.0,
        xpr_db=None, random_components=False, size_m=(1, 1, 1), center=(20.0, -5.0, 15.0),
        orientation_deg=(0, 0, 0), velocity_m_s=(0, 0, 0),
    )
    path = MockBackend._sensing_path(1, tx, target, rx, config, lam)
    r_t = math.dist(tx.position, target.center)
    r_r = math.dist(target.center, rx.position)
    assert bistatic_radar_gain_db(lam, 7.0, r_t, r_r) == path.path_gain_db
    vec = bistatic_radar_gain_db(lam, 7.0, np.array([r_t, 0.01]), np.array([r_r, 5.0]))
    assert vec[0] == pytest.approx(path.path_gain_db, abs=1e-12)
    # Legs clamp at 0.1 m like friis_dbm.
    assert vec[1] == pytest.approx(bistatic_radar_gain_db(lam, 7.0, 0.1, 5.0), abs=1e-12)


def test_cell_equals_the_mock_echo_with_absorption():
    # A target at a cell center gives the same echo the coverage map predicts
    # there, gas absorption over R_t + R_r included.
    scene = _scene(2)
    config = SimulationConfig(
        **{**MOCK_CFG, "atmospheric_absorption": True, "absorption_db_per_km": 20.0}
    )
    result = _run(
        scene, config=config, rcs_dbsm=-3.0,
        center_xy=[75.0, 40.0], size_xy=[20.0, 20.0], cell_size_m=10.0, height_m=50.0,
    )
    center = (65.0 + 15.0, 30.0 + 15.0, 50.0)  # cell (j, i) = (1, 1)
    target = ResolvedSensingTarget(
        actor_id="a", model="constant", object_type=None, model_type=None, rcs_dbsm=-3.0,
        xpr_db=None, random_components=False, size_m=(1, 1, 1), center=center,
        orientation_deg=(0, 0, 0), velocity_m_s=(0, 0, 0),
    )
    lam = C / config.frequency_hz
    devices = {d.id: d for d in scene.devices}
    echoes = [
        MockBackend._sensing_path(1, devices[t], target, devices[r], config, lam).power_dbm
        for t in ("t1", "t2") for r in ("t1_rx", "t2_rx")
    ]
    expected = max(echoes) - noise_floor_dbm(config) + 10 * math.log10(4096)
    assert result.values["best_snr_db"][1][1] == pytest.approx(expected, abs=1e-9)
    no_gas = _run(
        scene, rcs_dbsm=-3.0,
        center_xy=[75.0, 40.0], size_xy=[20.0, 20.0], cell_size_m=10.0, height_m=50.0,
    )
    assert no_gas.values["best_snr_db"][1][1] > expected + 1.0


def test_frequency_scaling_is_lambda_squared():
    scene = _scene(2)
    kw = dict(center_xy=[75.0, 0.0], size_xy=[200.0, 100.0], cell_size_m=20.0)
    low = _run(scene, config=SimulationConfig(**MOCK_CFG), **kw)
    high = _run(scene, config=SimulationConfig(**{**MOCK_CFG, "frequency_hz": 28e9}), **kw)
    a = np.array(low.values["best_snr_db"], dtype=float)
    b = np.array(high.values["best_snr_db"], dtype=float)
    assert np.all(np.isfinite(a))
    assert np.allclose(a - b, 20 * math.log10(8), atol=1e-9)


def test_steered_array_gain():
    scene = _scene(1)
    kw = dict(center_xy=[50.0, 0.0], size_xy=[60.0, 60.0], cell_size_m=10.0)
    none = _run(scene, **kw)
    steered = _run(scene, array_gain="steered", **kw)
    assert none.array_gain_db == 0.0
    assert steered.array_gain_db == pytest.approx(2 * 10 * math.log10(16))
    a = np.array(none.values["best_snr_db"], dtype=float)
    b = np.array(steered.values["best_snr_db"], dtype=float)
    assert np.allclose(b - a, 10 * math.log10(16) + 10 * math.log10(16), atol=1e-9)
    asym = _run(scene, array_gain="steered", tx_rows=2, tx_cols=8, rx_rows=1, rx_cols=4, **kw)
    assert asym.array_gain_db == pytest.approx(10 * math.log10(16) + 10 * math.log10(4))


def _tr38901_dbi(az_deg: float, el_deg: float) -> float:
    """TR 38.901 Table 7.3-1 element, local azimuth / elevation."""
    a_v = -min(12.0 * (el_deg / 65.0) ** 2, 30.0)
    a_h = -min(12.0 * (az_deg / 65.0) ** 2, 30.0)
    return 8.0 - min(-(a_v + a_h), 30.0)


def test_element_gain_db_patterns_and_frame():
    def gain(pattern, orientation, az_el):
        d = _dev("d", "tx", (0, 0, 0), orientation_deg=list(orientation))
        d.antenna.pattern = pattern
        a, e = (math.radians(v) for v in az_el)
        k = np.array([[math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)]])
        return float(element_gain_db(d, 7.0 * k)[0])

    assert gain("iso", (40, 10, 0), (12, 30)) == 0.0
    assert gain("bogus", (0, 0, 0), (0, 0)) == 0.0  # sionna backend: iso fallback
    assert gain("tr38901", (0, 0, 0), (0, 0)) == pytest.approx(8.0, abs=1e-12)
    assert gain("tr38901", (0, 0, 0), (34.7, 14.2)) == pytest.approx(_tr38901_dbi(34.7, 14.2))
    # Yaw 90 turns boresight to world +y; behind the panel is the 30 dB floor.
    assert gain("tr38901", (90, 0, 0), (90, 0)) == pytest.approx(8.0, abs=1e-12)
    assert gain("tr38901", (0, 0, 0), (180, 0)) == pytest.approx(-22.0, abs=1e-12)
    # Sionna pitch sign: -15 tilts boresight up to elevation +15.
    assert gain("tr38901", (0, -15, 0), (0, 15)) == pytest.approx(8.0, abs=1e-9)
    assert gain("tr38901", (0, 15, 0), (0, 15)) == pytest.approx(_tr38901_dbi(0, 30), abs=1e-9)
    # Vertical dipoles: 1.5 sin^2(theta) and the half-wave closed form.
    el = 25.0
    assert gain("dipole", (0, 0, 0), (70, el)) == pytest.approx(
        10 * math.log10(1.5 * math.cos(math.radians(el)) ** 2), abs=1e-9
    )
    c = math.cos(math.radians(el))
    hw = 1.643 * (math.cos(math.pi / 2 * math.sin(math.radians(el))) / c) ** 2
    assert gain("hw_dipole", (0, 0, 0), (-20, el)) == pytest.approx(10 * math.log10(hw), abs=1e-9)


@pytest.mark.parametrize(
    "yaw,expected",
    # Sionna RCSSolver echo minus the iso map at the two cells (live check
    # of the review): the per-leg element gain on both legs.
    [(0.0, {(65.0, 45.0): 8.0, (-15.0, -35.0): -44.0}),
     (180.0, {(65.0, 45.0): -44.0, (-15.0, -35.0): -13.7})],
)
def test_element_pattern_on_both_legs(yaw, expected):
    tx_pos = (0.0, 0.0, 10.0)
    devices = [
        _dev("t1", "tx", tx_pos, power_dbm=30.0, orientation_deg=[yaw, 0, 0]),
        _dev("t1_rx", "rx", tx_pos, orientation_deg=[yaw, 0, 0]),
    ]
    iso = Scene(scene_id="iso", devices=devices)
    tr = Scene(scene_id="tr", devices=[d.model_copy(deep=True) for d in devices])
    for d in tr.devices:
        d.antenna.pattern = "tr38901"
    kw = dict(
        rcs_dbsm=10.0, height_m=30.0, cell_size_m=10.0, center_xy=[40.0, 0.0], size_xy=[120.0, 120.0]
    )
    a, b = _run(iso, **kw), _run(tr, **kw)
    assert b.metadata["element_patterns"] == {"t1": "tr38901", "t1_rx": "tr38901"}
    grid = a.grid
    for (x, y), delta in expected.items():
        i = int((x - grid.origin[0]) // grid.cell_size_m)
        j = int((y - grid.origin[1]) // grid.cell_size_m)
        az = (math.degrees(math.atan2(y, x)) - yaw + 180.0) % 360.0 - 180.0
        el = math.degrees(math.atan2(30.0 - 10.0, math.hypot(x, y)))
        diff = b.values["best_snr_db"][j][i] - a.values["best_snr_db"][j][i]
        assert diff == pytest.approx(2 * _tr38901_dbi(az, el), abs=1e-9)
        assert diff == pytest.approx(delta, abs=0.05)


# --------------------------------------------------------- polarization (v0.1.14)


def _pol_dev(device_id, kind, position, pol="V", orientation=(0.0, 0.0, 0.0), pattern="iso"):
    d = _dev(device_id, kind, position, orientation_deg=list(orientation))
    d.antenna.polarization = pol
    d.antenna.pattern = pattern
    return d


@pytest.mark.parametrize("pitch", [-45.0, -15.0, 15.0, 30.0])
def test_monostatic_polarization_is_cos_2psi(pitch):
    # A monostatic echo keeps |cos 2psi| of an element whose field is tilted
    # by psi from the world theta-hat; along the pitch axis psi is the pitch.
    tx = _pol_dev("t", "tx", (0.0, 0.0, 10.0), orientation=(0.0, pitch, 0.0))
    rx = tx.model_copy(update={"id": "r", "kind": "rx"})
    along_axis = np.array([[0.0, 35.0, 10.0], [0.0, -20.0, 10.0]])
    f = abs(math.cos(2.0 * math.radians(pitch)))
    got = polarization_loss_db(tx, rx, along_axis)
    if f < 1e-10:  # 45 deg: the echo arrives cross-polarized, no echo
        assert np.all(got == -np.inf)
    else:
        assert got == pytest.approx([20.0 * math.log10(f)] * 2, abs=1e-9)

    # Any other direction: psi from the element's field in the world basis.
    pts = np.array([[30.0, 12.0, 25.0], [-14.0, -35.0, 30.0], [50.0, -50.0, 2.0]])
    expected = []
    beta = math.radians(pitch)
    for p in pts:
        k = (p - np.array(tx.position)) / np.linalg.norm(p - np.array(tx.position))
        # local = Ry(beta)^T k; V field = R theta_l.
        kl = np.array([
            math.cos(beta) * k[0] - math.sin(beta) * k[2], k[1],
            math.sin(beta) * k[0] + math.cos(beta) * k[2],
        ])
        tl, pl = math.acos(kl[2]), math.atan2(kl[1], kl[0])
        th_l = np.array([math.cos(tl) * math.cos(pl), math.cos(tl) * math.sin(pl), -math.sin(tl)])
        u = np.array([
            math.cos(beta) * th_l[0] + math.sin(beta) * th_l[2], th_l[1],
            -math.sin(beta) * th_l[0] + math.cos(beta) * th_l[2],
        ])
        t, ph = math.acos(k[2]), math.atan2(k[1], k[0])
        theta_hat = np.array([math.cos(t) * math.cos(ph), math.cos(t) * math.sin(ph), -math.sin(t)])
        phi_hat = np.array([-math.sin(ph), math.cos(ph), 0.0])
        psi = math.atan2(u @ phi_hat, u @ theta_hat)
        expected.append(20.0 * math.log10(abs(math.cos(2.0 * psi))))
    assert polarization_loss_db(tx, rx, pts) == pytest.approx(expected, abs=1e-9)


@pytest.mark.parametrize("pol", ["V", "H", "VH"])
@pytest.mark.parametrize("yaw", [0.0, 37.0, -120.0])
def test_unpitched_co_polarized_links_have_no_polarization_term(pol, yaw):
    tx = _pol_dev("t", "tx", (0.0, 0.0, 10.0), pol, (yaw, 0.0, 0.0))
    for rx_pos in ((0.0, 0.0, 10.0), (40.0, -25.0, 3.0)):
        rx = _pol_dev("r", "rx", rx_pos, pol, (-yaw, 0.0, 0.0))
        pts = cell_centers(coverage_grid(
            SensingCoverageRequest(center_xy=[0.0, 0.0], size_xy=[80.0, 80.0], cell_size_m=10.0),
            None,
        )[0])
        assert np.array_equal(polarization_loss_db(tx, rx, pts), np.zeros(len(pts)))
    # ... so the v0.1.13 map keeps its bytes and its metadata.
    scene = Scene(scene_id="p", devices=[tx, _pol_dev("t_rx", "rx", tx.position, pol, (yaw, 0.0, 0.0))])
    assert "polarization_model" not in _run(scene, center_xy=[50.0, 0.0], size_xy=[40.0, 40.0]).metadata


def test_cross_polarized_colocated_panel_has_no_echo():
    # A +-45 deg (cross, first port) co-located panel at zero tilt receives its
    # own echo cross-polarized: Sionna's echo is a deep null there.
    tx = _pol_dev("t", "tx", (0.0, 0.0, 10.0), "cross")
    rx = _pol_dev("t_rx", "rx", (0.0, 0.0, 10.0), "cross")
    pts = np.array([[30.0, 5.0, 30.0], [-20.0, 40.0, 10.0]])
    assert np.all(polarization_loss_db(tx, rx, pts) == -np.inf)
    result = _run(
        Scene(scene_id="x", devices=[tx, rx]),
        center_xy=[40.0, 0.0], size_xy=[40.0, 40.0], cell_size_m=10.0,
    )
    assert all(v is None for row in result.values["best_snr_db"] for v in row)
    assert result.summary.median_best_snr_db is None
    assert result.metadata["polarization_model"] == (
        "per-leg projection (sionna world theta/phi basis, first port)"
    )
    target = ResolvedSensingTarget(
        actor_id="a", model="constant", object_type=None, model_type=None, rcs_dbsm=10.0,
        xpr_db=None, random_components=False, size_m=(1, 1, 1), center=(30.0, 5.0, 30.0),
        orientation_deg=(0, 0, 0), velocity_m_s=(0, 0, 0),
    )
    config = SimulationConfig(**MOCK_CFG)
    assert MockBackend._sensing_path(1, tx, target, rx, config, C / config.frequency_hz) is None
    # A depolarizing (XPR) target is not modeled: the mock keeps its echo.
    xpr = ResolvedSensingTarget(**{**target.__dict__, "xpr_db": 10.0})
    assert MockBackend._sensing_path(1, tx, xpr, rx, config, C / config.frequency_hz) is not None


@pytest.mark.parametrize("rx_pos", [(0.0, 0.0, 25.0), (60.0, 40.0, 20.0)])
def test_mock_echo_equals_the_map_with_pattern_tilt_and_polarization(rx_pos):
    # A target at a cell center returns the map's value on the mock with a
    # tr38901 element at yaw 30, pitch -15 (element gains and polarization
    # on both, mono- and bistatic).
    orient = (30.0, -15.0, 0.0)
    tx = _pol_dev("t1", "tx", (0.0, 0.0, 25.0), "V", orient, "tr38901")
    tx.power_dbm = 43.0
    rx = _pol_dev("t1_rx", "rx", rx_pos, "V", orient, "tr38901")
    scene = Scene(scene_id="p", devices=[tx, rx])
    config = SimulationConfig(**MOCK_CFG)
    result = _run(
        scene, rcs_dbsm=5.0, sensing_rx_ids=["t1_rx"],
        center_xy=[25.0, 60.0], size_xy=[20.0, 20.0], cell_size_m=10.0, height_m=50.0,
    )
    center = (30.0, 65.0, 50.0)  # cell (j, i) = (1, 1)
    pol = polarization_loss_db(tx, rx, np.array([center]))[0]
    assert pol < -0.1  # the tilt costs something here
    target = ResolvedSensingTarget(
        actor_id="a", model="constant", object_type=None, model_type=None, rcs_dbsm=5.0,
        xpr_db=None, random_components=False, size_m=(1, 1, 1), center=center,
        orientation_deg=(0, 0, 0), velocity_m_s=(0, 0, 0),
    )
    path = MockBackend._sensing_path(1, tx, target, rx, config, C / config.frequency_hz)
    expected = path.power_dbm - noise_floor_dbm(config) + 10 * math.log10(4096)
    assert result.values["best_snr_db"][1][1] == pytest.approx(expected, abs=1e-9)
    gains = (
        element_gain_db(tx, np.array([center]) - np.array(tx.position))[0]
        + element_gain_db(rx, np.array([center]) - np.array(rx.position))[0]
    )
    iso_tx = tx.model_copy(deep=True)
    iso_rx = rx.model_copy(deep=True)
    for d in (iso_tx, iso_rx):
        d.antenna.pattern = "iso"
        d.orientation_deg = [0.0, 0.0, 0.0]
    iso = MockBackend._sensing_path(1, iso_tx, target, iso_rx, config, C / config.frequency_hz)
    assert path.power_dbm == pytest.approx(iso.power_dbm + gains + pol, abs=1e-9)


# ------------------------------------------------------------ blocking


class _HalfBlocked(MockBackend):
    """Every leg ending at x < 0 is blocked."""

    def segment_los(self, project_dir, scene, library, config, starts, ends):
        return np.asarray(ends)[:, 0] >= 0.0, []


class _Broken(MockBackend):
    def segment_los(self, *args):
        raise RuntimeError("ray test exploded")


def test_blocked_cells():
    scene = _scene(2)
    result = _run(
        scene, backend=_HalfBlocked(), center_xy=[0.0, 0.0], size_xy=[100.0, 40.0],
        cell_size_m=10.0, threshold_db=-30.0,
    )
    assert result.metadata["los_model"] == "mitsuba ray_test (static scene, actor meshes hidden)"
    grid = result.grid
    xs = grid.origin[0] + (np.arange(grid.nx) + 0.5) * grid.cell_size_m
    blocked = xs < 0.0
    for j in range(grid.ny):
        for i in range(grid.nx):
            if blocked[i]:
                assert result.values["best_snr_db"][j][i] is None
                assert result.values["pd_best"][j][i] is None
                assert result.values["n_links_detected"][j][i] == 0.0
                assert result.values["fusion_feasible"][j][i] == 0.0
            else:
                assert result.values["best_snr_db"][j][i] is not None
                assert result.values["n_links_detected"][j][i] == 4.0
    open_pct = 100.0 * (~blocked).sum() / grid.nx
    s = result.summary
    assert (s.num_cells, s.num_links, s.num_geometries) == (grid.nx * grid.ny, 4, 3)
    assert s.pct_cells_los == pytest.approx(open_pct)
    assert s.pct_cells_detected == pytest.approx(open_pct)
    assert all(link.los_fraction == pytest.approx(open_pct / 100.0) for link in result.links)
    finite = [v for row in result.values["best_snr_db"] for v in row if v is not None]
    assert s.median_best_snr_db == pytest.approx(float(np.median(finite)))


def test_los_failure_degrades_gracefully():
    result = _run(_scene(1), backend=_Broken(), center_xy=[0.0, 0.0], size_xy=[20.0, 20.0])
    assert any(
        w == "sensing coverage LOS test failed: ray test exploded; see logs" for w in result.warnings
    )
    assert result.grid.nx == 2 and result.grid.ny == 2
    assert all(v is None for row in result.values["best_snr_db"] for v in row)
    assert all(v == 0.0 for row in result.values["fusion_feasible"] for v in row)
    s = result.summary
    assert (s.pct_cells_los, s.pct_cells_detected, s.pct_cells_fusion_feasible) == (0.0, 0.0, 0.0)
    assert s.median_best_snr_db is None and s.num_links == 1


# ---------------------------------------------------------------- fusion


def test_fusion_counts_distinct_geometries():
    scene = _scene(3)
    kw = dict(center_xy=[75.0, 40.0], size_xy=[40.0, 40.0], cell_size_m=20.0, threshold_db=-30.0)
    result = _run(scene, **kw)
    assert [(lk.tx_id, lk.rx_id) for lk in result.links][:3] == [
        ("t1", "t1_rx"), ("t1", "t2_rx"), ("t1", "t3_rx")
    ]
    groups = {(lk.tx_id, lk.rx_id): lk.geometry_group for lk in result.links}
    assert groups[("t1", "t2_rx")] == groups[("t2", "t1_rx")]
    assert groups[("t1", "t1_rx")] != groups[("t2", "t2_rx")]
    assert result.summary.num_links == 9 and result.summary.num_geometries == 6
    assert all(v == 9.0 for row in result.values["n_links_detected"] for v in row)
    assert all(v == 1.0 for row in result.values["fusion_feasible"] for v in row)
    assert result.summary.pct_cells_fusion_feasible == 100.0
    six = _run(scene, min_links_for_fusion=6, **kw)
    assert all(v == 1.0 for row in six.values["fusion_feasible"] for v in row)
    seven = _run(scene, min_links_for_fusion=7, **kw)
    assert all(v == 0.0 for row in seven.values["fusion_feasible"] for v in row)
    assert seven.summary.pct_cells_fusion_feasible == 0.0


# ------------------------------------------------------------ validation


def test_request_validation():
    with pytest.raises(ValidationError, match="not both"):
        SensingCoverageRequest(rcs_dbsm=0.0, object_type="human")
    with pytest.raises(ValidationError, match="go together"):
        SensingCoverageRequest(center_xy=[0.0, 0.0])
    with pytest.raises(ValidationError, match="must be > 0"):
        SensingCoverageRequest(center_xy=[0.0, 0.0], size_xy=[10.0, 0.0])
    with pytest.raises(ValidationError):
        SensingCoverageRequest(cell_size_m=0.0)
    # Non-finite or absurd extents are 422s, not an OverflowError (500).
    for bad in (
        dict(center_xy=[0.0, 0.0], size_xy=[1e300, 1e300]),
        dict(center_xy=[0.0, 0.0], size_xy=[math.inf, 10.0]),
        dict(center_xy=[math.nan, 0.0], size_xy=[10.0, 10.0]),
        dict(center_xy=[1e8, 0.0], size_xy=[10.0, 10.0]),
        dict(cell_size_m=math.inf),
        dict(height_m=math.inf),
        dict(height_m=math.nan),
    ):
        with pytest.raises(ValidationError):
            SensingCoverageRequest(**bad)
    # A subnormal cell (valid, > 0) cannot size the grid: 400, not 500.
    tiny = SensingCoverageRequest(center_xy=[0.0, 0.0], size_xy=[1e6, 1e6], cell_size_m=1e-310)
    with pytest.raises(SensingCoverageError, match="coverage grid cannot be sized"):
        coverage_grid(tiny, None)
    assert SensingCoverageRequest(object_type="human").resolved_rcs_dbsm() == -1.37
    assert SensingCoverageRequest(rcs_dbsm=3.0).resolved_rcs_dbsm() == 3.0
    result = _run(_scene(1), object_type="vehicle-single-sp", center_xy=[0, 0], size_xy=[10, 10])
    assert result.rcs_dbsm == 11.25 and result.metadata["rcs_m2"] == pytest.approx(10 ** 1.125)


# ------------------------------------------------------------------- API


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Coverage", "project_id": PID})
    assert resp.status_code == 201, resp.text
    put = api_client.put(
        f"/api/projects/{PID}/scene", json=_scene(2, scene_id=PID).model_dump(mode="json")
    )
    assert put.status_code == 200, put.text
    return api_client


def _post(client, body=None, project_id: str = PID):
    return client.post(
        f"/api/projects/{project_id}/simulate/sensing-coverage",
        json={"config": MOCK_CFG} if body is None else body,
    )


def test_api_coverage_persists_and_reloads(client):
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "sensing_coverage"
    assert body["result_id"] == "mock_sensing_coverage_001"
    assert body["metadata"]["request_hash"].startswith("sha256:")
    grid = body["grid"]
    # Device-only project: bounds = the devices, padded 15 m.
    assert grid["origin"] == [-15.0, -15.0, 60.0]
    assert set(body["values"]) == {"best_snr_db", "n_links_detected", "pd_best", "fusion_feasible"}
    for rows in body["values"].values():
        assert len(rows) == grid["ny"] and all(len(r) == grid["nx"] for r in rows)
    assert body["summary"]["num_links"] == 4
    refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    assert [(r["kind"], r["result_id"]) for r in refs] == [
        ("sensing_coverage", "mock_sensing_coverage_001")
    ]
    assert client.get(f"/api/projects/{PID}/results/sensing-coverage").json() == body
    by_id = client.get(
        f"/api/projects/{PID}/results/sensing-coverage",
        params={"result_id": "mock_sensing_coverage_001"},
    )
    assert by_id.json() == body
    assert client.get(
        f"/api/projects/{PID}/results/sensing-coverage", params={"result_id": "nope"}
    ).status_code == 404


def test_api_coverage_errors(client):
    assert _post(client, project_id="ghost").status_code == 404
    assert _post(client, {"config_id": "nope"}).status_code == 404
    resp = _post(client, {"config": MOCK_CFG, "tx_ids": ["ghost"]})
    assert resp.status_code == 400 and "tx device not found: ghost" in resp.json()["detail"]
    assert _post(client, {"config": MOCK_CFG, "rcs_dbsm": 1.0, "object_type": "human"}).status_code == 422
    huge = {"config": MOCK_CFG, "center_xy": [0.0, 0.0], "size_xy": [1e300, 1e300]}
    assert _post(client, huge).status_code == 422
    resp = client.post(
        f"/api/projects/{PID}/simulate/sensing-coverage",
        content=json.dumps({"config": MOCK_CFG, "center_xy": [0, 0], "size_xy": [math.inf, 10]}),
        headers={"Content-Type": "application/json"},
    )
    assert resp.status_code == 422, resp.text
    tiny = {
        "config": MOCK_CFG, "center_xy": [0.0, 0.0], "size_xy": [1e6, 1e6], "cell_size_m": 1e-310
    }
    resp = _post(client, tiny)
    assert resp.status_code == 400 and "cannot be sized" in resp.json()["detail"]
    scene = _scene(2, scene_id=PID)
    scene.devices = [d for d in scene.devices if d.id != "t2_rx"]
    client.put(f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json"))
    resp = _post(client)
    assert resp.status_code == 400 and "tx t2 has no sensing receiver" in resp.json()["detail"]
    assert client.get(f"/api/projects/{PID}/scene").json()["result_sets"] == []


def test_api_coverage_prune(client):
    assert _post(client).status_code == 200
    resp = client.post(
        f"/api/projects/{PID}/results/prune",
        json={"kinds": ["sensing_coverage"], "keep_latest": 0},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == ["mock_sensing_coverage_001"]


def test_capabilities(api_client):
    listing = {b["name"]: b for b in api_client.get("/api/backends").json()}
    assert listing["mock"]["capabilities"]["sensing_coverage"] is True
    assert listing["mock"]["capabilities"]["occlusion"] is False
    if "sionna" in listing:
        assert listing["sionna"]["capabilities"]["occlusion"] is True


# ---------------------------------------------------------- Phase C


def test_geometry_groups_is_the_shared_helper():
    from seam_studio.services import sensing, sensing_coverage

    assert sensing_coverage.geometry_groups is sensing.geometry_groups
    assert geometry_groups is sensing.geometry_groups


def test_default_detector_keeps_the_v0112_numbers():
    scene = _scene(3)
    kw = dict(center_xy=[75.0, 40.0], size_xy=[400.0, 400.0], cell_size_m=10.0)
    default = _run(scene, **kw)
    explicit = _run(scene, detector={"model": "swerling1", "monte_carlo_trials": 0}, **kw)
    a = default.model_dump(mode="json")
    b = explicit.model_dump(mode="json")
    for d in (a, b):
        d["metadata"].pop("elapsed_s")
    assert a == b
    assert "detector" not in default.metadata
    assert "detector" not in default.metadata["request"]
    assert default.summary.mc_spot_check is None
    best = np.array(default.values["best_snr_db"], dtype=float)
    pd = np.array(default.values["pd_best"], dtype=float)
    assert np.array_equal(pd, 1e-6 ** (1.0 / (1.0 + 10.0 ** (best / 10.0))))


def test_detector_model_changes_pd_best_only():
    scene = _scene(2)
    kw = dict(center_xy=[75.0, 0.0], size_xy=[200.0, 100.0], cell_size_m=10.0)
    base = _run(scene, **kw)
    for model in ("swerling0", "swerling3"):
        other = _run(scene, detector={"model": model}, **kw)
        assert other.values["best_snr_db"] == base.values["best_snr_db"]
        assert other.values["fusion_feasible"] == base.values["fusion_feasible"]
        best = np.array(other.values["best_snr_db"], dtype=float)
        assert np.allclose(
            np.array(other.values["pd_best"], dtype=float),
            pd_swerling(best, 1e-6, model), rtol=0.0, atol=1e-15,
        )
        assert other.metadata["detector"]["model"] == model
        assert other.summary.mc_spot_check is None


def test_mc_spot_cells_quantiles_and_ties():
    best = np.array([5.0, -np.inf, 1.0, 9.0, 3.0, 7.0, 7.0])
    has = np.isfinite(best)
    # Echo values 5, 1, 9, 3, 7, 7: quantiles 0/25/50/75/100 % (nearest).
    assert mc_spot_cells(best, has) == [2, 4, 0, 5, 3]
    # Ties go to the lowest index not taken yet; fewer cells than quantiles.
    flat = np.array([4.0, 4.0, 4.0])
    assert mc_spot_cells(flat, np.ones(3, dtype=bool)) == [0, 1, 2]
    assert mc_spot_cells(best, np.zeros(7, dtype=bool)) == []


def test_mc_spot_check_within_ci():
    scene = _scene(2)
    trials = 200_000
    result = _run(
        scene, center_xy=[75.0, 0.0], size_xy=[600.0, 400.0], cell_size_m=10.0,
        detector={"model": "swerling3", "monte_carlo_trials": trials, "seed": 2},
    )
    checks = result.summary.mc_spot_check
    assert len(checks) == 5
    grid = result.grid
    snrs = [c.snr_db for c in checks]
    assert snrs == sorted(snrs)
    finite = [v for row in result.values["best_snr_db"] for v in row if v is not None]
    assert snrs[0] == min(finite) and snrs[-1] == max(finite)
    assert len({tuple(c.cell) for c in checks}) == 5
    for c in checks:
        ix, iy = c.cell
        assert 0 <= ix < grid.nx and 0 <= iy < grid.ny
        assert result.values["best_snr_db"][iy][ix] == c.snr_db
        assert result.values["pd_best"][iy][ix] == c.pd
        assert c.ci_low <= c.pd <= c.ci_high, c
        assert c.ci_low <= c.pd_mc <= c.ci_high
    det = result.metadata["detector"]
    assert det["model"] == "swerling3" and det["monte_carlo_trials"] == trials
    assert det["pfa_measured_ci"][0] <= 1e-6 <= det["pfa_measured_ci"][1]


def test_api_coverage_detector(client):
    resp = _post(client, {"config": MOCK_CFG, "detector": {"model": "swerling0",
                                                           "monte_carlo_trials": 10_000}})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["summary"]["mc_spot_check"]) == 5
    assert body["metadata"]["request"]["detector"]["model"] == "swerling0"
    resp = _post(client, {"config": MOCK_CFG, "detector": {"monte_carlo_trials": 1000,
                                                           "empirical_threshold": True}})
    assert resp.status_code == 422
