"""H0 calibration ensemble (#30, services/sensing_ensemble.py).

Mock backend throughout (every number is analytic and reproducible
exactly), plus two live sionna-rt checks (``requires_sionna_rcs``): the same
deterministic request twice reproduces each realization's background power
within 0.1 dB, and a perturbed ensemble leaves the project's RF projection
untouched. The reference side of every check (perturbation draws, noise
keys, CFR synthesis, beta-binomial interval) is plain numpy / scipy written
here from the formulas, never the module under test.
"""

import hashlib
import math
from pathlib import Path

import numpy as np
import pytest
from scipy.stats import betabinom

from seam_studio.schemas.actors import ScenarioSimulateRequest
from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import SensingBackgroundEnsembleResultSet
from seam_studio.schemas.scene import Actor, ActorTrajectory, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.sensing import (
    BackgroundEnsembleRequest,
    BackgroundOptions,
    CfarOptions,
    DetectionMapOptions,
    WaveformOptions,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import sensing_ensemble
from seam_studio.services.channel_synthesis import (
    calibrate_threshold,
    cfar_statistic,
    dd_map,
    synthesize_cfr,
)
from seam_studio.services.numerology import GRID_S, resolve_numerology
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import run_scenario
from seam_studio.services.sensing_ensemble import (
    EnsembleError,
    h0_frame,
    h0_noise,
    prepare_ensemble,
    realization_paths,
    run_background_ensemble,
)
from seam_studio.services.simulation_backends.mock_backend import MockBackend

from .conftest import requires_sionna_rcs

C = 299_792_458.0
F_C = 3.5e9
NF = 7.0
SEED = 11
CONFIG = SimulationConfig(
    id="default", backend="mock", frequency_hz=F_C, bandwidth_hz=2e7, noise_figure_db=NF
)
TRP1, TRP2 = [0.0, 0.0, 20.0], [120.0, -60.0, 25.0]
FAR_RX = [150.0, 90.0, 15.0]
SENSING_RX = ["trp1_rx", "trp2_rx", "far_rx"]
LINKS = [(tx, rx) for tx in ("trp1", "trp2") for rx in SENSING_RX]
PERTURB = {
    "material": {"eps_r_pct": 20.0, "sigma_pct": 50.0},
    "node_pos_sigma_m": 0.3,
    "cfo_hz": 4.0,
}


def _rf(material: str) -> RFBinding:
    return RFBinding(
        material_id=material, assignment_status="user_confirmed", assignment_sources=["user"]
    )


def _scene(rider: bool = False) -> Scene:
    """Two TRPs (TX + a sensing RX 0.4 / 0.5 m away: one site each), a
    bistatic sensing RX, a comm UE, an asphalt ground and a wall (constant
    materials, so the mock's reflection loss follows eps_r), and one target."""
    devices = [
        Device(id="trp1", kind="tx", position=TRP1, power_dbm=43.0),
        Device(id="trp2", kind="tx", position=TRP2, power_dbm=40.0),
        Device(id="trp1_rx", kind="rx", position=[0.4, 0.0, 20.0]),
        Device(id="trp2_rx", kind="rx", position=[120.0, -60.5, 25.0]),
        Device(id="far_rx", kind="rx", position=FAR_RX),
        Device(id="ue_1", kind="rx", position=[30.0, 30.0, 1.5]),
    ]
    actors = [
        Actor(
            id="uav_01",
            kind="uav",
            position=[40.0, 20.0, 60.0],
            trajectory=ActorTrajectory(
                waypoints=[[40.0, 20.0, 60.0], [100.0, 20.0, 60.0]], speed_m_s=10.0
            ),
            sensing={"model": "constant", "rcs_dbsm": 0.0},
        )
    ]
    if rider:
        actors.append(
            Actor(
                id="van",
                kind="car",
                position=[150.0, 90.0, 0.0],
                trajectory=ActorTrajectory(
                    waypoints=[[150.0, 90.0, 0.0], [150.0, 150.0, 0.0]], speed_m_s=12.0
                ),
                attached_device_ids=["far_rx"],
            )
        )
    prims = [
        Prim(id="/ground", name="ground", semantic_tags=["ground"], rf=_rf("asphalt_custom")),
        Prim(
            id="/buildings/b1/wall", name="wall", semantic_tags=["building", "wall"],
            rf=_rf("unknown_rf"),
        ),
    ]
    return Scene(
        scene_id="ens", prims=prims, devices=devices, actors=actors,
        simulation_configs=[CONFIG],
    )


def _source(scene: Scene, *, sensing_on: bool = True, **sensing):
    request = ScenarioSimulateRequest(
        num_frames=3, dt_s=0.5, include_paths=False,
        sensing={"enabled": True, "sensing_rx_ids": SENSING_RX, **sensing} if sensing_on else None,
    )
    result = run_scenario(MockBackend(), Path("."), scene, load_default_library(), CONFIG, request)
    # What api.simulate._persist_result stamps.
    result.result_id = "mock_scenario_001"
    result.metadata["config_snapshot"] = CONFIG.model_dump(mode="json")
    return result


def _request(**kw) -> BackgroundEnsembleRequest:
    body = {
        "source": {"result_id": "mock_scenario_001", "frame_index": 1},
        "n_realizations": 4,
        "noise_frames_per_realization": 50,
        "seed": SEED,
        **kw,
    }
    return BackgroundEnsembleRequest.model_validate(body)


def _run(scene: Scene, source, request, **kw) -> SensingBackgroundEnsembleResultSet:
    return run_background_ensemble(
        MockBackend(), Path("."), scene, load_default_library(), request, source, **kw
    )


def _rng(seed: int, r: int) -> np.random.Generator:
    return np.random.default_rng(np.random.SeedSequence(entropy=[seed, 0x5EA030], spawn_key=(r,)))


def _id32(s: str) -> int:
    return int.from_bytes(hashlib.sha256(s.encode("utf-8")).digest()[:4], "little")


def _ref_noise(seed, r, m, tx, rx, shape, sigma2) -> np.ndarray:
    # H0 calibration noise: stream 0x5EA031 (in-run noise is 0x5EA024).
    ss = np.random.SeedSequence(entropy=[seed, 0x5EA031], spawn_key=(r, m, _id32(tx), _id32(rx)))
    g = np.random.Generator(np.random.PCG64(ss))
    re = g.standard_normal(shape)
    im = g.standard_normal(shape)
    return (re + 1j * im) * math.sqrt(sigma2 / 2.0)


def _ref_cfr(paths, f, t, cfo_hz: float) -> np.ndarray:
    h = np.zeros((len(t), len(f)), dtype=np.complex128)
    for p in paths:
        a = 10.0 ** (p.path_gain_db / 20.0) * np.exp(1j * p.phase_rad)
        tau = p.delay_ns * 1e-9
        h += a * np.exp(-2j * np.pi * f[None, :] * tau) * np.exp(
            2j * np.pi * (p.doppler_hz + cfo_hz) * t[:, None]
        )
    return h


def _untimed(ens: SensingBackgroundEnsembleResultSet) -> dict:
    """The result minus its wall-clock fields."""
    out = ens.model_dump(mode="json")
    out["metadata"].pop("total_solve_s")
    for rz in out["realizations"]:
        rz.pop("solve_s")
    return out


def _link(paths, tx, rx):
    return [p for p in paths if p.tx_id == tx and p.rx_id == rx]


@pytest.fixture(scope="module")
def scene() -> Scene:
    return _scene()


@pytest.fixture(scope="module")
def source(scene):
    return _source(scene)


# ------------------------------------------------------------ draws


def test_same_request_twice_is_identical_and_draws_match_numpy(scene, source):
    request = _request(perturb=PERTURB)
    ens = _run(scene, source, request)
    again = _run(scene, source, request)
    # #30 acceptance 1 (mock: exactly, not just within 0.1 dB).
    assert _untimed(again) == _untimed(ens)
    assert [r.total_power_db for r in again.realizations] == [
        r.total_power_db for r in ens.realizations
    ]

    names = sorted(
        {p.rf.material_id for p in scene.prims} | {a.rf_material_id for a in scene.actors}
    )
    assert ens.metadata["material_names"] == names
    assert ens.metadata["sites"] == [["trp1", "trp1_rx"], ["trp2", "trp2_rx"], ["far_rx"]]
    assert ens.metadata["bistatic_rx_ids"] == ["far_rx"]
    assert [(lk.tx_id, lk.rx_id) for lk in ens.links] == LINKS
    assert [n.id for n in ens.nodes] == ["trp1", "trp2", *SENSING_RX]
    assert ens.background.mode == "h0_removed" and ens.background.deterministic

    r0 = ens.realizations[0]
    assert (r0.label, r0.material_draws, r0.node_offsets_m, r0.cfo_hz) == ("nominal", {}, {}, {})
    for r in range(1, 4):
        rz = ens.realizations[r]
        rng = _rng(SEED, r)
        for name in names:
            d_eps, d_sig = rng.normal(0.0, 0.2), rng.normal(0.0, 0.5)
            assert rz.material_draws[name].eps_r_scale == 1.0 + d_eps
            assert rz.material_draws[name].sigma_scale == 1.0 + d_sig
        for site in (["trp1", "trp1_rx"], ["trp2", "trp2_rx"], ["far_rx"]):
            offset = list(rng.normal(0.0, 0.3, 3))
            for dev_id in site:  # one draw per site
                assert rz.node_offsets_m[dev_id] == offset
        assert rz.cfo_hz == {"far_rx": rng.normal(0.0, 4.0)}  # co-located RX: no CFO
        assert rz.label == "perturbed" and rz.source_realization == r
        assert rz.background_h0 is not None and rz.num_paths == len(rz.background_h0)
    assert ens.metadata["solves"] == 4
    assert ens.metadata["noise_key"].startswith(f"SeedSequence(entropy=[{SEED}, 0x5EA031]")


def test_nominal_and_cfo_only_realizations_reuse_realization_zero(scene, source):
    ens = _run(scene, source, _request(n_realizations=3))
    assert ens.metadata["solves"] == 1
    r0 = ens.realizations[0]
    assert r0.background_h0 and r0.source_realization == 0
    for rz in ens.realizations[1:]:
        assert rz.label == "nominal" and rz.background_h0 is None
        assert rz.source_realization == 0 and rz.solve_s == 0.0
        assert rz.total_power_db == r0.total_power_db and rz.num_paths == r0.num_paths
        assert realization_paths(ens, rz.index) == r0.background_h0

    # CFO acts at synthesis only: perturbed label, but the nominal solve.
    cfo_only = _run(scene, source, _request(perturb={"cfo_hz": 4.0}))
    assert cfo_only.metadata["solves"] == 1
    full = _run(scene, source, _request(perturb=PERTURB))
    for r in range(1, 4):
        rz = cfo_only.realizations[r]
        assert rz.label == "perturbed" and rz.background_h0 is None
        assert rz.source_realization == 0
        assert (rz.material_draws, rz.node_offsets_m) == ({}, {})
        # Every draw is taken at sigma 0 too: the CFO stream does not move.
        assert rz.cfo_hz == full.realizations[r].cfo_hz


def test_material_scales_reach_the_solve(scene, source):
    ens = _run(scene, source, _request(perturb={"material": {"eps_r_pct": 30.0}}))
    library = load_default_library()

    def reflectance(eps: float) -> float:
        sq = math.sqrt(eps)
        return ((sq - 1.0) / (sq + 1.0)) ** 2

    nominal = ens.realizations[0].background_h0
    for rz in ens.realizations[1:]:
        assert rz.node_offsets_m == {} and rz.cfo_hz == {}
        for p, q in zip(nominal, rz.background_h0):
            assert (p.path_id, p.delay_ns) == (q.path_id, q.delay_ns)
            if not p.interactions:  # LoS: no material
                assert q.power_dbm == p.power_dbm
                continue
            mid = p.interactions[0].rf_material_id
            eps = library.get(mid).relative_permittivity
            eps_new = max(1.0, eps * rz.material_draws[mid].eps_r_scale)
            expected = 10.0 * math.log10(reflectance(eps_new) / reflectance(eps))
            assert q.power_dbm - p.power_dbm == pytest.approx(expected, abs=1e-9)


def test_node_offsets_move_the_link_endpoints(scene, source):
    ens = _run(scene, source, _request(perturb={"node_pos_sigma_m": 0.5}))
    nodes = {n.id: n.position for n in ens.nodes}
    for rz in ens.realizations[1:]:
        assert rz.material_draws == {} and len(rz.node_offsets_m) == 5
        for p in rz.background_h0:
            if p.interactions:
                continue
            tx = [a + b for a, b in zip(nodes[p.tx_id], rz.node_offsets_m[p.tx_id])]
            rx = [a + b for a, b in zip(nodes[p.rx_id], rz.node_offsets_m[p.rx_id])]
            assert p.vertices[0] == pytest.approx(tx, abs=1e-12)
            assert p.vertices[-1] == pytest.approx(rx, abs=1e-12)
            assert p.delay_ns == pytest.approx(math.dist(tx, rx) / C * 1e9, rel=1e-12)


def test_source_frame_kinematics_of_a_riding_receiver():
    scene = _scene(rider=True)
    source = _source(scene)
    ens = _run(scene, source, _request(n_realizations=1))
    frame_nodes = {n.id: n for n in source.frames[1].sensing.nodes}
    assert {n.id: (n.position, n.velocity) for n in ens.nodes} == {
        k: (n.position, n.velocity) for k, n in frame_nodes.items()
    }
    # t = 0.5 s: the van (12 m/s along +y) carried far_rx 6 m.
    assert frame_nodes["far_rx"].position == pytest.approx([150.0, 96.0, 15.0])
    assert frame_nodes["far_rx"].velocity == pytest.approx([0.0, 12.0, 0.0])
    los = next(
        p for p in ens.realizations[0].background_h0
        if (p.tx_id, p.rx_id) == ("trp1", "far_rx") and not p.interactions
    )
    k_arr = np.subtract([150.0, 96.0, 15.0], TRP1)
    k_arr /= np.linalg.norm(k_arr)
    expected = -float(np.dot([0.0, 12.0, 0.0], k_arr)) / (C / F_C)
    assert los.doppler_hz == pytest.approx(expected, abs=1e-9)
    assert los.doppler_hz < 0.0  # the receiver moves away from trp1: opening


# ------------------------------------------------------------ H0 frames


def test_h0_frame_is_numpy_synthesis_plus_the_keyed_noise(scene, source):
    ens = _run(scene, source, _request(perturb=PERTURB))
    num = resolve_numerology(GRID_S, 0.01)
    f = (5.0 * np.arange(256) - 640.0) * 15_625.0
    t = np.arange(16) * 9.0 / 14_400.0
    n0_dbm = -174.0 + 10.0 * math.log10(15_625.0) + NF
    es_dbm = 43.0 - 10.0 * math.log10(1280)
    sigma2 = 10.0 ** ((n0_dbm - es_dbm) / 10.0)
    for r, m, rx in ((0, 0, "far_rx"), (1, 3, "far_rx"), (2, 49, "trp2_rx")):
        y = h0_frame(ens, r, m, "trp1", rx, num, 43.0, NF)
        assert y.shape == (16, 256) and y.dtype == np.complex128
        cfo = ens.realizations[r].cfo_hz.get(rx, 0.0)
        rz = ens.realizations[r]
        paths = _link(ens.realizations[rz.source_realization].background_h0, "trp1", rx)
        assert paths
        h_ref = _ref_cfr(paths, f, t, cfo)
        w_ref = _ref_noise(SEED, r, m, "trp1", rx, (16, 256), sigma2)
        assert np.array_equal(h0_noise(ens, r, m, "trp1", rx, (16, 256), sigma2), w_ref)
        assert np.linalg.norm(y - (h_ref + w_ref)) / np.linalg.norm(h_ref) < 1e-12
    # CFO is a Doppler offset (positive = closing): on a static link every
    # path rotates by e^{+j 2 pi cfo t_n} over slow time.
    cfo = ens.realizations[1].cfo_hz["far_rx"]
    h = _ref_cfr(_link(ens.realizations[1].background_h0, "trp1", "far_rx"), f, t, cfo)
    still = _ref_cfr(_link(ens.realizations[1].background_h0, "trp1", "far_rx"), f, t, 0.0)
    assert np.allclose(h, still * np.exp(2j * np.pi * cfo * t)[:, None], rtol=1e-12, atol=0.0)


def test_any_h0_frame_rebuilds_bit_identically_from_the_stored_result(scene, source):
    ens = _run(scene, source, _request(perturb=PERTURB))
    stored = SensingBackgroundEnsembleResultSet.model_validate_json(ens.model_dump_json())
    num = resolve_numerology(GRID_S, 0.01)
    for r, m, (tx, rx) in ((0, 7, LINKS[2]), (3, 49, LINKS[4]), (2, 0, LINKS[0])):
        first = h0_frame(stored, r, m, tx, rx, num, 40.0, NF)
        assert np.array_equal(first, h0_frame(stored, r, m, tx, rx, num, 40.0, NF))
        assert np.array_equal(first, h0_frame(ens, r, m, tx, rx, num, 40.0, NF))
    with pytest.raises(EnsembleError, match="noise frame 50 out of range"):
        h0_frame(ens, 0, 50, "trp1", "far_rx", num, 40.0, NF)
    with pytest.raises(EnsembleError, match="realization 4 out of range"):
        h0_frame(ens, 4, 0, "trp1", "far_rx", num, 40.0, NF)
    with pytest.raises(EnsembleError, match="link trp1 -> ue_1 is not in"):
        h0_frame(ens, 0, 0, "trp1", "ue_1", num, 40.0, NF)


def test_adjacent_noise_frames_are_uncorrelated(scene, source):
    ens = _run(scene, source, _request(n_realizations=2))
    shape = (16, 256)  # Grid S: 4096 REs per frame

    def w(r, m, tx="trp1", rx="far_rx"):
        return h0_noise(ens, r, m, tx, rx, shape, 1.0)

    def rho(pairs) -> float:
        num = sum(np.vdot(b, a) for a, b in pairs)
        den = math.sqrt(
            sum(float(np.vdot(a, a).real) for a, _ in pairs)
            * sum(float(np.vdot(b, b).real) for _, b in pairs)
        )
        return abs(num) / den

    frames = [w(0, m) for m in range(31)]
    pairs = list(zip(frames[:-1], frames[1:]))  # 30 x 4096 = 122,880 REs
    assert sum(a.size for a, _ in pairs) >= 100_000
    assert rho(pairs) < 0.01
    assert rho([(w(0, m), w(1, m)) for m in range(30)]) < 0.01  # across realizations
    assert rho([(w(0, m), w(0, m, "trp2")) for m in range(30)]) < 0.01  # across links
    power = np.mean([np.mean(np.abs(x) ** 2) for x in frames])
    assert 0.98 <= power <= 1.02


def test_system_pfa_calibration_holds_out_of_sample(scene, source):
    # #30 acceptance 4: threshold on frames 0..9,999, held-out 10,000..19,999
    # inside the beta-binomial 95 % interval of the calibration (n 10,000,
    # P_FA 1e-2 -> m = 100 -> [74, 129]). A -40 dBm TX keeps the nominal
    # background near the noise; the calibration is distribution-free anyway.
    # Fixed seed (112 held-out alarms); a correct pipeline lands outside the
    # 95 % interval for about one seed in 20 (seeds 1-8: 72-138 alarms, seeds
    # 3 and 7 outside).
    ens = _run(
        scene, source, _request(n_realizations=1, noise_frames_per_realization=20_000, seed=1)
    )
    num = resolve_numerology(
        WaveformOptions(scs_hz=30e3, n_fft=64, n_active_sc=64, symbols_per_cpi=8), 0.01
    )
    assert (num.grid_k, num.grid_n) == (64, 8)
    map_opts = DetectionMapOptions(window="rect", zero_pad=1)
    cfar_opts = CfarOptions(type="ca", guard=[1, 1], train=[2, 1])
    p_tx = -40.0
    sigma2 = num.sigma_h2(p_tx, NF)
    f, t = num.axes("pilot")
    # The nominal background once; every frame adds its keyed noise (the
    # loop's Y equals h0_frame bit for bit, checked on a few frames).
    h = synthesize_cfr(_link(realization_paths(ens, 0), "trp1", "far_rx"), f, t, p_tx)
    stats = np.empty(20_000)
    for m in range(20_000):
        y = h + h0_noise(ens, 0, m, "trp1", "far_rx", h.shape, sigma2)
        if m % 5000 == 0:
            assert np.array_equal(y, h0_frame(ens, 0, m, "trp1", "far_rx", num, p_tx, NF))
        dmap = dd_map(y, num, map_opts, sigma2)
        stat, _ = cfar_statistic(dmap.P, cfar_opts)
        stats[m] = stat[~dmap.notch].max()
    n, pfa = 10_000, 1e-2
    m = math.floor(n * pfa)
    thr = calibrate_threshold(stats[:n], pfa)
    assert thr == np.sort(stats[:n])[n - m]
    alarms = int(np.count_nonzero(stats[n:] > thr))
    lo, hi = betabinom(n, a=m, b=n - m + 1).ppf([0.025, 0.975])
    assert (lo, hi) == (74.0, 129.0)
    assert lo <= alarms <= hi, alarms


# ------------------------------------------------------------ setup


def test_prepare_resolves_the_source_frame(scene, source):
    setup = prepare_ensemble(scene, _request(), source)
    assert setup.tx_ids == ["trp1", "trp2"] and setup.rx_ids == SENSING_RX
    assert setup.config.tx_ids == ["trp1", "trp2"] and setup.config.rx_ids == SENSING_RX
    assert [t.actor_id for t in setup.targets] == ["uav_01"]
    # The target at the frame pose (t = 0.5 s, 10 m/s along +x).
    assert setup.targets[0].center[0] == pytest.approx(45.0)
    assert setup.time_s == 0.5 and setup.warnings == []

    source.metadata["scene_hash"] = "sha256:then"
    try:
        stale = prepare_ensemble(scene, _request(), source, scene_hash="sha256:now")
    finally:
        source.metadata.pop("scene_hash")
    assert any("scene changed" in w for w in stale.warnings)

    lax = prepare_ensemble(
        scene, _request(background={"mode": "h0_removed", "deterministic": False}), source
    )
    assert any("RT sampling noise" in w for w in lax.warnings)


def test_prepare_errors(scene, source):
    with pytest.raises(EnsembleError, match="scope 'prim' is not supported"):
        prepare_ensemble(scene, _request(perturb={"material": {"scope": "prim"}}), source)
    with pytest.raises(EnsembleError, match="frame_index 3 is out of range"):
        prepare_ensemble(scene, _request(source={"result_id": "x", "frame_index": 3}), source)
    plain = _source(scene, sensing_on=False)
    with pytest.raises(EnsembleError, match="frame 1 of mock_scenario_001 has no sensing"):
        prepare_ensemble(scene, _request(), plain)
    gone = scene.model_copy(update={"actors": []})
    with pytest.raises(EnsembleError, match="target actor uav_01"):
        prepare_ensemble(gone, _request(), source)
    no_rx = scene.model_copy(update={"devices": [d for d in scene.devices if d.id != "far_rx"]})
    with pytest.raises(EnsembleError, match="rx far_rx of mock_scenario_001"):
        prepare_ensemble(no_rx, _request(), source)

    snapshotless = source.model_copy(deep=True)
    snapshotless.metadata.pop("config_snapshot")
    setup = prepare_ensemble(scene, _request(), snapshotless)
    assert setup.config.id == "default"
    assert any("no usable config_snapshot" in w for w in setup.warnings)
    with pytest.raises(EnsembleError, match="no config_snapshot"):
        prepare_ensemble(scene.model_copy(update={"simulation_configs": []}), _request(), snapshotless)

    with pytest.raises(ValueError, match="must be 'h0_removed'"):
        _request(background={"mode": "both"})


def test_background_options_come_from_the_source_run(scene):
    source = _source(
        scene,
        background={"mode": "both", "diffuse_reflection": False, "max_paths_per_link": 2},
    )
    ens = _run(scene, source, _request(n_realizations=1))
    assert ens.background == BackgroundOptions(
        mode="h0_removed", diffuse_reflection=False, max_paths_per_link=2
    )
    r0 = ens.realizations[0]
    for tx, rx in LINKS:
        assert len(_link(r0.background_h0, tx, rx)) <= 2
    assert r0.dropped_paths == 6  # 3 mock paths (LoS, ground, wall) per link, 2 kept
    # The source frame's own H0 is the nominal realization.
    assert r0.background_h0 == source.frames[1].sensing.background_h0 or (
        source.frames[1].sensing.background_h0 is None  # a cache hit: frame 0 holds it
        and r0.background_h0 == source.frames[0].sensing.background_h0
    )


def test_targetless_background_source(scene):
    bare = scene.model_copy(update={"actors": []})
    source = _source(bare, background={"mode": "h0_removed"})
    ens = _run(bare, source, _request(n_realizations=2, perturb={"node_pos_sigma_m": 0.1}))
    assert ens.metadata["target_ids"] == []
    assert ens.metadata["solves"] == 2 and all(r.background_h0 for r in ens.realizations)


def test_stored_path_budget(scene, source, monkeypatch):
    monkeypatch.setattr(sensing_ensemble, "MAX_ENSEMBLE_STORED_PATHS", 10)
    with pytest.raises(EnsembleError, match="background.max_paths_per_link"):
        _run(scene, source, _request(perturb=PERTURB))
    # Nominal reuse stores one set of paths only.
    monkeypatch.setattr(sensing_ensemble, "MAX_ENSEMBLE_STORED_PATHS", 20)
    assert _run(scene, source, _request()).metadata["solves"] == 1


def test_failed_solve_is_an_error_not_an_empty_realization(scene, source):
    """A solve the backend reports as failed (sionna degrades to no paths
    and a warning) must not be stored as a noise-only H0 realization."""

    class Flaky(MockBackend):
        calls = 0

        def simulate_background(self, *args, **kwargs):
            result = super().simulate_background(*args, **kwargs)
            Flaky.calls += 1
            if Flaky.calls == 2:
                result.paths = []
                result.warnings = ["sionna backend failed: CUDA out of memory; see logs"]
                result.metadata = {**result.metadata, "solve_failed": True}
            return result

    with pytest.raises(EnsembleError, match="realization 1: background solve failed: sionna"):
        run_background_ensemble(
            Flaky(), Path("."), scene, load_default_library(), _request(perturb=PERTURB), source
        )


def test_progress_ticks(scene, source):
    ticks: list[tuple[int, int]] = []
    _run(scene, source, _request(n_realizations=3), tick=lambda d, n: ticks.append((d, n)))
    assert ticks == [(0, 3), (1, 3), (2, 3), (3, 3)]


# ------------------------------------------------------------ sionna


SIONNA_TRPS = {"trp_a": (-40.0, -30.0, 15.0), "trp_b": (40.0, -25.0, 20.0)}


def _sionna_scene() -> Scene:
    devices = [
        Device(id=k, kind="tx", position=list(p), power_dbm=43.0) for k, p in SIONNA_TRPS.items()
    ]
    devices += [
        Device(id=f"{k}_rx", kind="rx", position=[p[0] + 0.5, p[1], p[2]])
        for k, p in SIONNA_TRPS.items()
    ]
    devices.append(Device(id="far_rx", kind="rx", position=[10.0, 45.0, 12.0]))
    return Scene(
        scene_id="ens_it",
        prims=[
            Prim(
                id="/ground", name="ground", semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"), rf=_rf("ground"),
            )
        ],
        devices=devices,
        actors=[
            Actor(
                id="tgt_01", kind="custom", position=[-5.0, 2.0, 30.0],
                trajectory=ActorTrajectory(
                    waypoints=[[-5.0, 2.0, 30.0], [25.0, 2.0, 30.0]], speed_m_s=10.0
                ),
                sensing={"model": "constant", "rcs_dbsm": 10.0},
            )
        ],
    )


def _files(root: Path) -> dict[str, tuple[int, str]]:
    return {
        str(p.relative_to(root)): (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest())
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


@requires_sionna_rcs
def test_sionna_ensemble_reproducible_and_project_untouched(tmp_path: Path, monkeypatch):
    from seam_studio.services.simulation_backends import sionna_backend
    from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend

    from .test_sensing_sionna import _write_ground

    monkeypatch.setattr(sionna_backend, "_FULL_FLUSH_EVERY", 10**9)
    project = tmp_path / "ens_it.seam"
    _write_ground(project)
    scene = _sionna_scene()
    library = load_default_library()
    config = SimulationConfig(
        backend="sionna", frequency_hz=F_C, bandwidth_hz=2e7, max_depth=2,
        reflection=True, num_samples=100_000,
    )
    rxs = ["trp_a_rx", "trp_b_rx", "far_rx"]
    backend = SionnaBackend()
    source = run_scenario(
        backend, project, scene, library, config,
        ScenarioSimulateRequest(
            num_frames=1, dt_s=0.5, include_paths=False,
            sensing={"enabled": True, "sensing_rx_ids": rxs, "samples_per_sp": 100_000},
        ),
    )
    source.result_id = "sionna_scenario_001"
    source.metadata["config_snapshot"] = config.model_dump(mode="json")
    request = BackgroundEnsembleRequest.model_validate({
        "source": {"result_id": "sionna_scenario_001", "frame_index": 0},
        "n_realizations": 3,
        "noise_frames_per_realization": 10,
        "seed": 5,
        "perturb": {
            "material": {"eps_r_pct": 15.0, "sigma_pct": 30.0},
            "node_pos_sigma_m": 0.2, "cfo_hz": 3.0,
        },
        "background": {"mode": "h0_removed", "num_samples": 100_000},
    })
    before = _files(project / "rf")
    assert before
    first = run_background_ensemble(backend, project, scene, library, request, source)
    second = run_background_ensemble(backend, project, scene, library, request, source)
    assert not [w for w in first.warnings if "failed" in w or "not perturbed" in w], first.warnings
    assert first.metadata["material_names"], first.metadata
    assert first.metadata["solves"] == 3

    for a, b in zip(first.realizations, second.realizations):
        assert a.material_draws == b.material_draws and a.node_offsets_m == b.node_offsets_m
        assert a.total_power_db.keys() == b.total_power_db.keys()
        for key, value in a.total_power_db.items():
            other = b.total_power_db[key]
            if value is None:
                assert other is None, key
            else:
                assert other == pytest.approx(value, abs=0.1), (a.index, key)
    nominal = first.realizations[0].total_power_db
    assert any(
        v is not None and abs(v - nominal[k]) > 1e-3
        for rz in first.realizations[1:]
        for k, v in rz.total_power_db.items()
    )
    # #30 acceptance 7: perturbations act in memory, never on the projection.
    assert _files(project / "rf") == before
