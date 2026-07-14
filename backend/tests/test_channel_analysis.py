"""Tests for the channel-analysis suite: empirical path-loss models, CIR/CFR
and dispersion math, and the /analyze/channel API roundtrip.

The empirical-model tests are pure and need no backend. The API test forces
the mock backend so it is deterministic whether or not Sionna RT is installed.
"""

import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seam_studio.api import channel as channel_api
from seam_studio.api import deps
from seam_studio.core.config import get_settings
from seam_studio.schemas.channel import ChannelAnalysisRequest
from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import RayPath
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene
from seam_studio.services import channel_analysis as ca
from seam_studio.services.project_store import load_default_library

TX_POS = [0.0, 0.0, 10.0]
RX_POS = [20.0, 0.0, 1.5]


# ------------------------------------------------------- empirical PL models


def test_fspl_known_value_28ghz_100m():
    # Textbook FSPL at 28 GHz over 100 m is ~101.4 dB.
    assert ca.fspl_db(28e9, 100.0) == pytest.approx(101.39, abs=0.05)


def test_fspl_matches_constant_form():
    # 20log10(4*pi*d*f/c) equals the classic constant form. With f in GHz and
    # d in km the additive constant is 92.45 dB (it is 32.45 dB for MHz/m).
    f, d = 3.5e9, 250.0
    closed = ca.fspl_db(f, d)
    alt = 92.45 + 20.0 * math.log10(f / 1e9) + 20.0 * math.log10(d / 1000.0)
    assert closed == pytest.approx(alt, abs=0.01)


def test_ci_n2_equals_fspl_slope():
    # The close-in model with PLE n=2 recovers the free-space slope exactly.
    for d in (10.0, 100.0, 1000.0):
        ci = ca._ci_model(28e9, d, 2.0).path_loss_db
        assert ci == pytest.approx(ca.fspl_db(28e9, d), abs=1e-9)


def test_ci_n3_steeper_than_n2():
    ci2 = ca._ci_model(28e9, 200.0, 2.0).path_loss_db
    ci3 = ca._ci_model(28e9, 200.0, 3.0).path_loss_db
    # n=3 adds 10*log10(200) dB over n=2.
    assert ci3 - ci2 == pytest.approx(10.0 * math.log10(200.0), abs=1e-9)


def test_uma_los_published_sample_point():
    # TR 38.901 UMa-LOS at fc=2 GHz, h_BS=25, h_UT=1.5, d_2D=100 m is ~78 dB
    # (below the breakpoint, so the single-slope 22*log10 branch applies).
    h_bs, h_ut, d2d = 25.0, 1.5, 100.0
    d3d = math.sqrt(d2d**2 + (h_bs - h_ut) ** 2)
    pl = ca._uma_los(2e9, d2d, d3d, h_bs, h_ut)
    assert pl == pytest.approx(78.3, abs=0.5)


def test_uma_nlos_is_max_of_los_and_nlos_prime():
    # NLOS = max(LOS, NLOS'); it must never be below the LOS value.
    models = ca.evaluate_path_loss_models(28e9, 200.0, 25.0, 1.5)
    by_name = {m.model: m for m in models}
    assert by_name["tr38901_uma_nlos"].path_loss_db >= by_name["tr38901_uma_los"].path_loss_db
    assert by_name["tr38901_umi_nlos"].path_loss_db >= by_name["tr38901_umi_los"].path_loss_db
    assert by_name["tr38901_inh_nlos"].path_loss_db >= by_name["tr38901_inh_los"].path_loss_db


def test_pl_models_validity_flags_out_of_range_frequency():
    # 300 GHz is above the 100 GHz TR 38.901 ceiling; FSPL/CI stay valid.
    models = {m.model: m for m in ca.evaluate_path_loss_models(300e9, 100.0, 25.0, 1.5)}
    assert models["tr38901_uma_los"].valid is False
    assert "outside" in models["tr38901_uma_los"].notes
    assert models["fspl"].valid is True
    assert models["ci_n2"].valid is True


def test_pl_models_validity_flags_out_of_range_distance():
    # InH is only defined to 150 m 3D; 500 m is flagged invalid.
    models = {m.model: m for m in ca.evaluate_path_loss_models(30e9, 500.0, 3.0, 1.5)}
    assert models["tr38901_inh_los"].valid is False
    assert "outside" in models["tr38901_inh_los"].notes


def test_pl_models_delta_vs_rt_filled_when_rt_present():
    rt = 120.0
    models = {m.model: m for m in ca.evaluate_path_loss_models(28e9, 100.0, 10.0, 1.5, rt)}
    fspl = models["fspl"]
    assert fspl.delta_vs_rt_db == pytest.approx(fspl.path_loss_db - rt, abs=1e-4)


def test_pl_models_delta_none_without_rt():
    models = ca.evaluate_path_loss_models(28e9, 100.0, 10.0, 1.5, None)
    assert all(m.delta_vs_rt_db is None for m in models)
    # Exactly the nine pinned model names, in order.
    assert [m.model for m in models] == [
        "fspl", "tr38901_uma_los", "tr38901_uma_nlos", "tr38901_umi_los",
        "tr38901_umi_nlos", "tr38901_inh_los", "tr38901_inh_nlos", "ci_n2", "ci_n3",
    ]


# ---------------------------------------------------- CIR / dispersion / CFR


def _two_path() -> list[RayPath]:
    """LoS at 0 dBm / 100 ns and one NLoS reflection at -10 dBm / 200 ns."""
    los = RayPath(
        path_id="p1", tx_id="tx", rx_id="rx", path_type="los",
        vertices=[[0, 0, 0], [1, 0, 0]], power_dbm=0.0, delay_ns=100.0, phase_rad=0.0,
    )
    nlos = RayPath(
        path_id="p2", tx_id="tx", rx_id="rx", path_type="reflection",
        vertices=[[0, 0, 0], [0.5, 1, 0], [1, 0, 0]], power_dbm=-10.0,
        delay_ns=200.0, phase_rad=0.0,
    )
    return [nlos, los]  # deliberately out of delay order


def test_build_cir_sorted_by_delay():
    cir = ca.build_cir(_two_path())
    assert [t.delay_ns for t in cir] == [100.0, 200.0]
    assert cir[0].path_type == "los"
    assert cir[1].path_type == "reflection"


def test_k_factor_hand_computed():
    # P_LoS = 1 mW, P_NLoS = 0.1 mW -> K = 10*log10(1/0.1) = 10 dB.
    assert ca.k_factor_db(_two_path()) == pytest.approx(10.0, abs=1e-9)


def test_k_factor_none_when_no_los_or_no_nlos():
    paths = _two_path()
    los_only = [p for p in paths if p.path_type == "los"]
    nlos_only = [p for p in paths if p.path_type != "los"]
    assert ca.k_factor_db(los_only) is None
    assert ca.k_factor_db(nlos_only) is None


def test_delay_metrics_hand_computed():
    mean, rms = ca.delay_metrics(_two_path())
    # weights 1, 0.1; mean = 120/1.1 = 109.0909 ns.
    assert mean == pytest.approx(120.0 / 1.1, abs=1e-6)
    # var = 909.0909/1.1 = 826.446; rms = 28.748 ns.
    assert rms == pytest.approx(28.7480, abs=1e-3)


def test_coherence_bandwidth_from_rms():
    _, rms = ca.delay_metrics(_two_path())
    coh = ca.coherence_bandwidth_mhz(rms)
    assert coh == pytest.approx(1.0 / (2 * math.pi * rms * 1e-9) / 1e6, abs=1e-6)


def test_coherence_bandwidth_none_on_single_tap():
    _, rms = ca.delay_metrics([_two_path()[1]])  # one path -> rms 0
    assert rms == pytest.approx(0.0)
    assert ca.coherence_bandwidth_mhz(rms) is None
    assert ca.coherence_bandwidth_mhz(None) is None


def test_cfr_length_and_nan_free():
    offsets, mag = ca.compute_cfr(_two_path(), bandwidth_hz=100e6, num_points=128)
    assert len(offsets) == 128
    assert len(mag) == 128
    assert offsets[0] == pytest.approx(-50e6)
    assert offsets[-1] == pytest.approx(50e6)
    assert all(math.isfinite(m) for m in mag)


def test_cfr_empty_paths_is_floor_not_nan():
    offsets, mag = ca.compute_cfr([], bandwidth_hz=100e6, num_points=16)
    assert len(mag) == 16
    assert all(m == -300.0 for m in mag)


# --------------------------------------------------------------------- API


def _scene() -> Scene:
    return Scene(
        scene_id="ch_test",
        name="Channel Test",
        prims=[
            Prim(
                id="/terrain/ground",
                name="ground",
                semantic_tags=["terrain", "ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=TX_POS, power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=RX_POS),
        ],
    )


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    store = deps.get_store()
    store.create_project("Channel Test", project_id="ch_test")
    store.save_scene("ch_test", _scene())
    app = FastAPI()
    app.include_router(channel_api.router, prefix="/api")
    client = TestClient(app)
    try:
        yield client
    finally:
        get_settings.cache_clear()
        deps.get_store.cache_clear()


# Force the mock backend so the roundtrip is deterministic on any machine.
MOCK_REQ = {"config": {"backend": "mock"}}


def test_api_channel_roundtrip(api_client):
    resp = api_client.post("/api/projects/ch_test/analyze/channel", json=MOCK_REQ)
    assert resp.status_code == 200
    body = resp.json()

    assert body["tx_id"] == "tx_001"
    assert body["rx_id"] == "rx_001"
    assert body["backend"] == "mock"
    assert body["frequency_hz"] == 28e9
    assert body["distance_3d_m"] == pytest.approx(math.dist(TX_POS, RX_POS))
    assert body["num_paths"] >= 1
    # Link budget is populated.
    assert body["rss_dbm"] is not None
    assert body["rt_path_loss_db"] is not None
    assert body["snr_db"] is not None
    assert body["shannon_capacity_mbps"] is not None and body["shannon_capacity_mbps"] > 0
    # rt_path_loss = tx_power - rss.
    assert body["rt_path_loss_db"] == pytest.approx(30.0 - body["rss_dbm"], abs=1e-6)

    # CIR sorted, CFR sized and finite.
    delays = [t["delay_ns"] for t in body["cir"]]
    assert delays == sorted(delays)
    assert len(body["cfr_freq_offset_hz"]) == 128
    assert len(body["cfr_mag_db"]) == 128
    assert all(v is not None for v in body["cfr_mag_db"])

    # All nine builtin empirical models present (plugins may add more, e.g.
    # the bundled two_ray_ground example); FSPL delta vs RT computed.
    names = [m["model"] for m in body["pl_models"]]
    assert {
        "fspl", "tr38901_uma_los", "tr38901_uma_nlos", "tr38901_umi_los",
        "tr38901_umi_nlos", "tr38901_inh_los", "tr38901_inh_nlos", "ci_n2", "ci_n3",
    } <= set(names)
    fspl = next(m for m in body["pl_models"] if m["model"] == "fspl")
    assert fspl["delta_vs_rt_db"] == pytest.approx(
        fspl["path_loss_db"] - body["rt_path_loss_db"], abs=1e-3
    )


def test_api_channel_custom_cfr_points_and_ids(api_client):
    resp = api_client.post(
        "/api/projects/ch_test/analyze/channel",
        json={"config": {"backend": "mock"}, "tx_id": "tx_001", "rx_id": "rx_001",
              "num_cfr_points": 64},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["cfr_mag_db"]) == 64


def test_api_channel_unknown_device_400(api_client):
    resp = api_client.post(
        "/api/projects/ch_test/analyze/channel",
        json={"config": {"backend": "mock"}, "rx_id": "does_not_exist"},
    )
    assert resp.status_code == 400


def test_api_channel_unknown_config_400(api_client):
    resp = api_client.post(
        "/api/projects/ch_test/analyze/channel",
        json={"config_id": "missing_cfg"},
    )
    assert resp.status_code == 400


def test_api_channel_unknown_project_404(api_client):
    resp = api_client.post("/api/projects/nope/analyze/channel", json=MOCK_REQ)
    assert resp.status_code == 404


# ---------------------------------------- 3GPP measurement quantities (38.215)


def _lin(dbm: float) -> float:
    return 10.0 ** (dbm / 10.0)


def test_api_rsrp_rssi_rsrq_default_scs30(api_client):
    # Default SCS 30 kHz over the default 100 MHz grid gives 277 resource
    # blocks; RSRP/RSSI/RSRQ follow the TS 38.215 definitions exactly.
    resp = api_client.post("/api/projects/ch_test/analyze/channel", json=MOCK_REQ)
    assert resp.status_code == 200
    body = resp.json()

    n_rb = body["num_resource_blocks"]
    assert n_rb == 277  # int(100e6 / (12 * 30e3))
    assert body["subcarrier_spacing_khz"] == 30.0

    rss = body["rss_dbm"]
    noise_floor = body["metadata"]["noise_floor_dbm"]

    # RSRP = per-resource-element power = RSS spread over the 277*12 subcarriers.
    assert body["rsrp_dbm"] == pytest.approx(rss - 10.0 * math.log10(n_rb * 12), abs=1e-6)

    # RSSI = wideband signal + noise power (no interference term).
    exp_rssi = 10.0 * math.log10(_lin(rss) + _lin(noise_floor))
    assert body["rssi_dbm"] == pytest.approx(exp_rssi, abs=1e-6)

    # RSRQ = N_RB * RSRP / RSSI (linear).
    exp_rsrq = 10.0 * math.log10(n_rb * _lin(body["rsrp_dbm"]) / _lin(body["rssi_dbm"]))
    assert body["rsrq_db"] == pytest.approx(exp_rsrq, abs=1e-6)

    # At high SNR (signal >> noise) RSSI -> signal power, so
    # RSRQ -> 10log10(N_RB * RSRP / RSS) = 10log10(N_RB / N_sc) = 10log10(1/12).
    assert rss - noise_floor > 20.0  # comfortably signal-dominated
    assert body["rsrq_db"] == pytest.approx(10.0 * math.log10(1.0 / 12.0), abs=0.05)


def test_api_rsrp_scs15_doubles_resource_blocks(api_client):
    # Halving the subcarrier spacing doubles the subcarriers per RB-width, so
    # the 100 MHz grid packs 555 RBs (vs 277 at 30 kHz) and RSRP drops because
    # the same wideband RSS is spread over more resource elements.
    resp30 = api_client.post("/api/projects/ch_test/analyze/channel", json=MOCK_REQ)
    resp15 = api_client.post(
        "/api/projects/ch_test/analyze/channel",
        json={"config": {"backend": "mock"}, "subcarrier_spacing_khz": 15.0},
    )
    assert resp30.status_code == 200 and resp15.status_code == 200
    b30, b15 = resp30.json(), resp15.json()

    assert b15["num_resource_blocks"] == 555  # int(100e6 / (12 * 15e3))
    assert b15["subcarrier_spacing_khz"] == 15.0

    n_sc30 = b30["num_resource_blocks"] * 12
    n_sc15 = b15["num_resource_blocks"] * 12
    # RSRP shift is exactly -10log10(N_sc15/N_sc30) (~ -3.01 dB minus the
    # grid-rounding from the int() floor of the RB count).
    exp_delta = -10.0 * math.log10(n_sc15 / n_sc30)
    assert b15["rsrp_dbm"] - b30["rsrp_dbm"] == pytest.approx(exp_delta, abs=1e-6)
    assert exp_delta == pytest.approx(-3.01, abs=0.02)


def test_api_single_tx_reports_no_interference(api_client):
    # The single-TX fixture scene has nothing to interfere: interference_dbm
    # stays None, num_interferers is 0, and SINR collapses to SNR exactly.
    resp = api_client.post("/api/projects/ch_test/analyze/channel", json=MOCK_REQ)
    assert resp.status_code == 200
    body = resp.json()
    assert body["interference_dbm"] is None
    assert body["num_interferers"] == 0
    assert body["sinr_db"] == pytest.approx(body["snr_db"], abs=1e-9)


# ------------------------------------------- co-channel interference (multi-TX)


def _two_tx_scene() -> Scene:
    """The single-TX fixture plus a second, more distant transmitter. tx_002 is
    far enough that its power at the RX is well below the serving tx_001 link, so
    it acts as a weak co-channel interferer rather than the serving cell."""
    scene = _scene()
    scene.devices.insert(
        1,
        Device(id="tx_002", name="TX2", kind="tx", position=[80.0, 20.0, 12.0], power_dbm=30.0),
    )
    return scene


@pytest.fixture()
def api_client_2tx(tmp_path, monkeypatch):
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    store = deps.get_store()
    store.create_project("Channel Test 2TX", project_id="ch_test")
    store.save_scene("ch_test", _two_tx_scene())
    app = FastAPI()
    app.include_router(channel_api.router, prefix="/api")
    client = TestClient(app)
    try:
        yield client
    finally:
        get_settings.cache_clear()
        deps.get_store.cache_clear()


def test_api_two_tx_interference_lowers_sinr(api_client_2tx):
    # tx_001 is the serving link (first TX); tx_002's ray-traced power at the RX
    # is co-channel interference. SINR = S/(I+N) must fall below the noise-only
    # SNR, and the definitions must hold exactly.
    resp = api_client_2tx.post("/api/projects/ch_test/analyze/channel", json=MOCK_REQ)
    assert resp.status_code == 200
    body = resp.json()

    assert body["tx_id"] == "tx_001"  # first TX serves
    assert body["num_interferers"] == 1
    assert body["interference_dbm"] is not None

    rss = body["rss_dbm"]
    noise_floor = body["metadata"]["noise_floor_dbm"]
    intf = body["interference_dbm"]

    # Interference is the weaker link (tx_002 is farther), so it drags SINR down
    # but not below the noise floor.
    assert body["snr_db"] == pytest.approx(rss - noise_floor, abs=1e-9)
    assert body["sinr_db"] < body["snr_db"]

    # SINR = S / (I + N), computed in the linear domain.
    exp_sinr = rss - 10.0 * math.log10(_lin(noise_floor) + _lin(intf))
    assert body["sinr_db"] == pytest.approx(exp_sinr, abs=1e-9)

    # RSSI now carries signal + interference + noise (the single-TX case had no
    # interference term).
    exp_rssi = 10.0 * math.log10(_lin(rss) + _lin(intf) + _lin(noise_floor))
    assert body["rssi_dbm"] == pytest.approx(exp_rssi, abs=1e-9)

    # Extra interference in the RSSI denominator pushes RSRQ below the
    # single-TX, signal-dominated asymptote of 10log10(1/12) ~= -10.79 dB.
    assert body["rsrq_db"] < 10.0 * math.log10(1.0 / 12.0)

    # Capacity uses the SINR, so it sits below the noise-only Shannon bound.
    sinr_lin = _lin(body["sinr_db"])
    exp_cap = body["bandwidth_hz"] * math.log2(1.0 + sinr_lin) / 1e6
    assert body["shannon_capacity_mbps"] == pytest.approx(exp_cap, abs=1e-6)


def test_api_two_tx_serving_tx_002_swaps_signal_and_interferer(api_client_2tx):
    # Selecting tx_002 as the serving TX makes tx_001 the (now stronger)
    # interferer: signal drops, interference rises, still one interferer.
    default = api_client_2tx.post(
        "/api/projects/ch_test/analyze/channel", json=MOCK_REQ
    ).json()
    swapped = api_client_2tx.post(
        "/api/projects/ch_test/analyze/channel",
        json={"config": {"backend": "mock"}, "tx_id": "tx_002"},
    ).json()

    assert swapped["tx_id"] == "tx_002"
    assert swapped["num_interferers"] == 1
    # The serving/interferer powers swap: tx_002's signal == tx_001's interferer
    # power in the default run, and vice-versa.
    assert swapped["rss_dbm"] == pytest.approx(default["interference_dbm"], abs=1e-9)
    assert swapped["interference_dbm"] == pytest.approx(default["rss_dbm"], abs=1e-9)
