"""Tests for the research channel pack: TR 36.777 UMa-AV (UAV) path loss, the
link parameter sweep endpoint, and the Doppler-time spectrogram endpoint.

Model math is checked against hand-computed closed forms; the API tests force
the mock backend so everything runs deterministically with no Sionna and no
GPU. The spectrogram's Doppler comes from the geometric device-velocity
fallback on the mock backend (the sionna backend surfaces solver Doppler in
metadata instead).
"""

import math

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seam_studio.api import channel as channel_api
from seam_studio.api import deps
from seam_studio.core.config import get_settings
from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import RayPath
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene
from seam_studio.services import channel_analysis as ca

C = 299_792_458.0
TX_POS = [0.0, 0.0, 10.0]
RX_POS = [20.0, 0.0, 1.5]


# ------------------------------------------------ TR 36.777 UMa-AV path loss


def test_uma_av_los_hand_computed():
    # Table B-1 LOS at fc=2 GHz, d_3D=1000 m, h_UT=100 m:
    # PL = 28 + 22*log10(1000) + 20*log10(2) = 28 + 66 + 6.0206 = 100.0206 dB.
    expected = 28.0 + 22.0 * 3.0 + 20.0 * math.log10(2.0)
    out = ca._tr36777("uma_av_los", 2e9, 1000.0, 100.0)
    assert out.path_loss_db == pytest.approx(expected, abs=1e-9)
    assert out.path_loss_db == pytest.approx(100.02, abs=0.01)
    assert out.valid is True
    assert out.notes == ""


def test_uma_av_nlos_hand_computed():
    # Table B-2 NLOS at fc=2 GHz, d_3D=1000 m, h_UT=100 m:
    # PL = -17.5 + (46 - 7*log10(100))*log10(1000) + 20*log10(40*pi*2/3)
    #    = -17.5 + 32*3 + 20*log10(83.776) = -17.5 + 96 + 38.462 = 116.962 dB.
    expected = (
        -17.5
        + (46.0 - 7.0 * math.log10(100.0)) * math.log10(1000.0)
        + 20.0 * math.log10(40.0 * math.pi * 2.0 / 3.0)
    )
    out = ca._tr36777("uma_av_nlos", 2e9, 1000.0, 100.0)
    assert out.path_loss_db == pytest.approx(expected, abs=1e-9)
    assert out.path_loss_db == pytest.approx(116.96, abs=0.01)
    assert out.valid is True


def test_uma_av_nlos_exponent_relaxes_with_altitude():
    # (46 - 7*log10(h_UT)) shrinks as the UAV climbs: at fixed distance the
    # NLOS loss at 300 m must be below the loss at 30 m.
    low = ca._tr36777("uma_av_nlos", 2e9, 1000.0, 30.0).path_loss_db
    high = ca._tr36777("uma_av_nlos", 2e9, 1000.0, 300.0).path_loss_db
    assert high < low
    # Exactly 7*log10(300/30)*log10(1000) = 21 dB apart.
    assert low - high == pytest.approx(7.0 * math.log10(10.0) * 3.0, abs=1e-9)


def test_uma_av_altitude_gating():
    # In range [22.5, 300] m: valid, no note. Outside: evaluated anyway
    # (extrapolated) but flagged, mirroring the TR 38.901 distance gating.
    for kind in ("uma_av_los", "uma_av_nlos"):
        assert ca._tr36777(kind, 2e9, 500.0, 22.5).valid is True
        assert ca._tr36777(kind, 2e9, 500.0, 300.0).valid is True

        ground = ca._tr36777(kind, 2e9, 500.0, 1.5)
        assert ground.valid is False
        assert "outside" in ground.notes and "UMa-AV" in ground.notes
        assert math.isfinite(ground.path_loss_db)

        too_high = ca._tr36777(kind, 2e9, 500.0, 400.0)
        assert too_high.valid is False
        assert math.isfinite(too_high.path_loss_db)


def test_uma_av_nlos_extrapolation_finite_near_ground():
    # h_UT is floored inside the log so a (flagged-invalid) ground-level UT
    # never raises or returns NaN.
    out = ca._tr36777("uma_av_nlos", 2e9, 100.0, 0.5)
    assert out.valid is False
    assert math.isfinite(out.path_loss_db)


def test_evaluate_models_aerial_opt_in():
    # Default keeps the pinned nine-model list (pre-aerial contract)...
    default = [m.model for m in ca.evaluate_path_loss_models(28e9, 100.0, 25.0, 1.5)]
    assert "tr36777_uma_av_los" not in default
    assert "tr36777_uma_av_nlos" not in default
    assert len(default) == 9
    # ...and include_aerial adds exactly the two UMa-AV rows with delta filled.
    rt = 120.0
    models = {
        m.model: m
        for m in ca.evaluate_path_loss_models(
            2e9, 500.0, 25.0, 100.0, rt, include_aerial=True
        )
    }
    assert len(models) == 11
    for name in ("tr36777_uma_av_los", "tr36777_uma_av_nlos"):
        assert models[name].valid is True  # h_UT=100 m is in range
        assert models[name].delta_vs_rt_db == pytest.approx(
            models[name].path_loss_db - rt, abs=1e-3
        )


# ------------------------------------------------ geometric per-path Doppler


def _los_path(tx_pos, rx_pos) -> RayPath:
    return RayPath(
        path_id="p1", tx_id="tx", rx_id="rx", path_type="los",
        vertices=[list(tx_pos), list(rx_pos)], power_dbm=-60.0, delay_ns=50.0,
    )


def test_geometric_doppler_closing_rx_hand_computed():
    # RX at +x closing on the TX at 30 m/s: f_d = +v*f/c (~350.2 Hz at 3.5 GHz).
    tx = Device(id="tx", kind="tx", position=[0.0, 0.0, 0.0])
    rx = Device(id="rx", kind="rx", position=[100.0, 0.0, 0.0],
                velocity_m_s=[-30.0, 0.0, 0.0])
    (fd,) = ca.geometric_doppler_hz([_los_path(tx.position, rx.position)], tx, rx, 3.5e9)
    assert fd == pytest.approx(30.0 * 3.5e9 / C, abs=1e-6)


def test_geometric_doppler_sums_both_endpoints():
    # TX moving +x (toward RX) and RX moving -x (toward TX) add up.
    tx = Device(id="tx", kind="tx", position=[0.0, 0.0, 0.0],
                velocity_m_s=[10.0, 0.0, 0.0])
    rx = Device(id="rx", kind="rx", position=[100.0, 0.0, 0.0],
                velocity_m_s=[-30.0, 0.0, 0.0])
    (fd,) = ca.geometric_doppler_hz([_los_path(tx.position, rx.position)], tx, rx, 3.5e9)
    assert fd == pytest.approx(40.0 * 3.5e9 / C, abs=1e-6)


def test_geometric_doppler_static_is_zero():
    tx = Device(id="tx", kind="tx", position=[0.0, 0.0, 0.0])
    rx = Device(id="rx", kind="rx", position=[100.0, 0.0, 0.0])
    assert ca.geometric_doppler_hz([_los_path(tx.position, rx.position)], tx, rx, 3.5e9) == [0.0]


# --------------------------------------------------------------------- API


def _scene(scene_id: str, rx_velocity=None) -> Scene:
    return Scene(
        scene_id=scene_id,
        name="Channel Pack",
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
            Device(id="rx_001", name="RX", kind="rx", position=RX_POS,
                   velocity_m_s=rx_velocity),
        ],
    )


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    store = deps.get_store()
    # Static link plus a copy whose RX drives toward the TX at 3 m/s.
    store.create_project("Channel Pack", project_id="ch_pack")
    store.save_scene("ch_pack", _scene("ch_pack"))
    store.create_project("Channel Pack Moving", project_id="ch_pack_mov")
    store.save_scene("ch_pack_mov", _scene("ch_pack_mov", rx_velocity=[-3.0, 0.0, 0.0]))
    app = FastAPI()
    app.include_router(channel_api.router, prefix="/api")
    client = TestClient(app)
    try:
        yield client
    finally:
        get_settings.cache_clear()
        deps.get_store.cache_clear()


MOCK = {"backend": "mock"}


def test_api_channel_includes_aerial_rows(api_client):
    # The normal analysis now carries the UMa-AV rows; on this terrestrial
    # link (h_UT = 1.5 m) they are present but flagged out of altitude range.
    resp = api_client.post("/api/projects/ch_pack/analyze/channel", json={"config": MOCK})
    assert resp.status_code == 200
    models = {m["model"]: m for m in resp.json()["pl_models"]}
    for name in ("tr36777_uma_av_los", "tr36777_uma_av_nlos"):
        assert name in models
        assert models[name]["valid"] is False
        assert "UMa-AV" in models[name]["notes"]
        assert models[name]["path_loss_db"] is not None
        assert models[name]["delta_vs_rt_db"] is not None


# ------------------------------------------------------------- sweep endpoint


def _sweep(api_client, project_id, body):
    return api_client.post(f"/api/projects/{project_id}/analyze/channel-sweep", json=body)


def test_sweep_frequency_monotonic_path_loss(api_client):
    # Mock powers are Friis-based: every doubling of frequency adds exactly
    # 20*log10(2) dB of path loss to every path, hence to the total.
    values = [7e9, 14e9, 28e9, 56e9]
    resp = _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "frequency_hz", "sweep_values": values,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["tx_id"] == "tx_001"
    assert body["rx_id"] == "rx_001"
    assert body["backend"] == "mock"
    assert body["sweep_field"] == "frequency_hz"
    assert [r["value"] for r in body["rows"]] == values

    pls = [r["path_loss_db"] for r in body["rows"]]
    assert all(pl is not None for pl in pls)
    assert pls == sorted(pls) and pls[0] < pls[-1]  # strictly increasing
    for a, b in zip(pls, pls[1:]):
        assert b - a == pytest.approx(20.0 * math.log10(2.0), abs=1e-6)
    # Two-path link: dispersion KPIs populated and null-safe.
    assert all(r["rms_delay_spread_ns"] is not None for r in body["rows"])
    assert all(r["k_factor_db"] is not None for r in body["rows"])


def test_sweep_tx_power_shifts_rss_not_path_loss(api_client):
    resp = _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "tx_power_dbm", "sweep_values": [10.0, 20.0, 30.0],
    })
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    rss = [r["rss_dbm"] for r in rows]
    assert rss[1] - rss[0] == pytest.approx(10.0, abs=1e-9)
    assert rss[2] - rss[1] == pytest.approx(10.0, abs=1e-9)
    # Path loss (tx power - RSS) is power-invariant.
    pls = [r["path_loss_db"] for r in rows]
    assert pls[0] == pytest.approx(pls[1], abs=1e-9)
    assert pls[1] == pytest.approx(pls[2], abs=1e-9)


def test_sweep_noise_figure_lowers_snr(api_client):
    resp = _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "noise_figure_db", "sweep_values": [0.0, 7.0, 14.0],
    })
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    # NF adds straight into the noise floor: SNR drops dB-for-dB, RSS untouched.
    assert rows[0]["rss_dbm"] == pytest.approx(rows[2]["rss_dbm"], abs=1e-9)
    assert rows[0]["snr_db"] - rows[1]["snr_db"] == pytest.approx(7.0, abs=1e-9)
    assert rows[1]["snr_db"] - rows[2]["snr_db"] == pytest.approx(7.0, abs=1e-9)


def test_sweep_bandwidth_lowers_snr_by_10log10_ratio(api_client):
    resp = _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "bandwidth_hz", "sweep_values": [25e6, 100e6],
    })
    assert resp.status_code == 200
    rows = resp.json()["rows"]
    # 4x bandwidth = 10log10(4) dB more thermal noise.
    assert rows[0]["snr_db"] - rows[1]["snr_db"] == pytest.approx(
        10.0 * math.log10(4.0), abs=1e-9
    )


def test_sweep_request_validation_422(api_client):
    # Fewer than 2 points is not a sweep.
    assert _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "frequency_hz", "sweep_values": [1e9],
    }).status_code == 422
    # More than 50 points exceeds the interactive ceiling.
    assert _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "frequency_hz", "sweep_values": [1e9] * 51,
    }).status_code == 422
    # Only the four whitelisted fields are sweepable.
    assert _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "max_depth", "sweep_values": [1.0, 2.0],
    }).status_code == 422


def test_sweep_out_of_range_value_400(api_client):
    # A negative frequency fails SimulationConfig validation at the patch
    # point -> 400, not a math domain error / 500.
    resp = _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "frequency_hz", "sweep_values": [-1e9, 1e9],
    })
    assert resp.status_code == 400


def test_sweep_unknown_rx_400(api_client):
    resp = _sweep(api_client, "ch_pack", {
        "config": MOCK, "sweep_field": "frequency_hz",
        "sweep_values": [1e9, 2e9], "rx_id": "does_not_exist",
    })
    assert resp.status_code == 400


# ------------------------------------------------------ spectrogram endpoint


def _spectrogram(api_client, project_id, body):
    return api_client.post(f"/api/projects/{project_id}/analyze/spectrogram", json=body)


def test_spectrogram_static_link_well_formed_grid(api_client):
    resp = _spectrogram(api_client, "ch_pack", {"config": MOCK})
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "mock"
    assert body["window"] == 128
    assert body["hop"] == 64
    assert body["num_paths"] >= 1

    # Defaults: 1.0 s at 500 Hz = 500 samples -> 1 + (500-128)//64 = 6 frames.
    assert len(body["times_s"]) == 6
    assert len(body["magnitude_db"]) == 6
    assert all(len(row) == 128 for row in body["magnitude_db"])
    assert all(math.isfinite(v) for row in body["magnitude_db"] for v in row)

    # fftshifted Doppler axis: ascending, 0 Hz dead center, +-fs/2 span.
    dop = body["doppler_hz"]
    assert len(dop) == 128
    assert dop == sorted(dop)
    assert dop[64] == 0.0
    assert dop[0] == pytest.approx(-250.0)

    # Nothing moves: all the energy sits in the 0 Hz bin of every frame.
    for row in body["magnitude_db"]:
        assert max(range(128), key=lambda j: row[j]) == 64


def test_spectrogram_moving_rx_peak_at_expected_doppler(api_client):
    # RX closes on the TX at 3 m/s; at 3.5 GHz the LoS Doppler is
    # f_d = v * (d_2D/d_3D) * f/c with the radial factor 20/sqrt(472.25)
    # from the tx[0,0,10] -> rx[20,0,1.5] geometry (~32.2 Hz).
    resp = _spectrogram(api_client, "ch_pack_mov", {
        "config": {"backend": "mock", "frequency_hz": 3.5e9},
    })
    assert resp.status_code == 200
    body = resp.json()
    assert body["metadata"]["doppler_source"] == "geometric"

    grid = body["magnitude_db"]
    dop = body["doppler_hz"]
    peak = max(
        ((i, j) for i in range(len(grid)) for j in range(len(dop))),
        key=lambda ij: grid[ij[0]][ij[1]],
    )
    peak_dop = dop[peak[1]]
    expected = 3.0 * (20.0 / math.sqrt(472.25)) * 3.5e9 / C
    bin_width = 500.0 / 128.0
    assert peak_dop != 0.0
    assert abs(peak_dop - expected) <= bin_width + 1e-9


def test_spectrogram_duration_clipped_to_sample_cap(api_client):
    # 10 s at 4 kHz asks for 40000 samples; the series is clipped to 8192 with
    # a warning, and the frame times stay inside the clipped window.
    resp = _spectrogram(api_client, "ch_pack", {
        "config": MOCK, "duration_s": 10.0, "sampling_frequency_hz": 4000.0,
        "window": 512,
    })
    assert resp.status_code == 200
    body = resp.json()
    assert any("clipped" in w for w in body["warnings"])
    assert body["metadata"]["num_samples"] == 8192
    assert body["times_s"][-1] <= 8192 / 4000.0


def test_spectrogram_window_longer_than_series_400(api_client):
    # 0.1 s at 500 Hz = 50 samples cannot fill a 128-sample window.
    resp = _spectrogram(api_client, "ch_pack", {"config": MOCK, "duration_s": 0.1})
    assert resp.status_code == 400
    assert "window" in resp.json()["detail"]


def test_spectrogram_grid_cap_400(api_client):
    # hop=1 over the full 8192-sample series would emit millions of cells.
    resp = _spectrogram(api_client, "ch_pack", {
        "config": MOCK, "duration_s": 10.0, "sampling_frequency_hz": 4000.0,
        "window": 512, "hop": 1,
    })
    assert resp.status_code == 400
    assert "grid" in resp.json()["detail"]


def test_spectrogram_unknown_project_404(api_client):
    resp = _spectrogram(api_client, "nope", {"config": MOCK})
    assert resp.status_code == 404
