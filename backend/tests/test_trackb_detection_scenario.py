"""Track B #24 in a scenario: sensing.detection.mode "cfar" (mock backend,
Grid S): stored detections, oracle-gated link reports, the achieved P_FA
report, H0-calibrated thresholds from a background-only scenario and from a
sensing_background_ensemble, and the unchanged threshold mode. The map of a
stored link is rebuilt with the independent references of
test_trackb_cfar.py."""

import math

import numpy as np
import pytest

from seam_studio.schemas.scene import Actor
from seam_studio.services.sensing_detection import MEASUREMENT_NOISE_NOTE

from .test_sensing_track import MOCK_CFG, _config, _scenario, _trp_scene, _uav
from .test_trackb_cfar import (
    H0_STREAM,
    ref_cfar_stat_masked,
    ref_map,
    ref_noise,
    ref_quantile,
    ref_synth,
)
from .test_trackb_synthesis import ref_training_rows

C = 299_792_458.0
GRID_S = {
    "scs_hz": 15_625.0, "n_fft": 2048, "n_active_sc": 1280, "cp": "normal",
    "symbol_duration_s": 1.0 / 14_400,
    "pilot": {"pattern": "comb", "subcarrier_step": 5, "symbol_step": 9},
}
K, N = 256, 16
DFP, TP = 5 * 15_625.0, 9 / 14_400
F = (np.arange(K) * 5 - 640) * 15_625.0
T = np.arange(N) * TP
CFAR = {"mode": "cfar", "map": {"window": "hann", "clutter_removal": "mean_subtract"}}
PID = "trackb_detect"


# Hann + mean_subtract on the 16-row Grid S map: 0 Hz notched, |v| <= 1 do not train.
TRAIN_ROWS = ref_training_rows("hann", 1, N, True, None)
TESTED_ROWS = np.arange(N) != N // 2


def _cfar_run(scene=None, num_frames=3, **over):
    sensing = {
        "waveform": GRID_S, "background": {"mode": "both", "store": "both"}, "detection": CFAR,
        **over,
    }
    return _scenario(scene or _trp_scene(), _config(), num_frames=num_frames, **sensing)


def _ref_stat(P):
    return ref_cfar_stat_masked(P, [1, 1], [8, 4], "ca", TRAIN_ROWS)


def _sigma2(p_tx=43.0, nf=7.0):
    return 10 ** ((-174 + 10 * math.log10(15_625.0) + nf - (p_tx - 10 * math.log10(1280))) / 10)


def _circ(d, length):
    d = d % length
    return min(d, length - d)


def _in_gate(det, report, gate=1):
    """Within ``gate`` cells of the cell nearest the true (u*, v*)."""
    u_star = report.bistatic_range_m / C * K * DFP
    v_star = report.doppler_hz * N * TP
    u_c, v_c = math.floor(u_star + 0.5), math.floor(v_star + 0.5)
    return _circ(det.cell_tau - u_c, K) <= gate and _circ(det.cell_nu - v_c, N) <= gate


# ------------------------------------------------------------ in-run detection


def test_cfar_scenario_reports_follow_the_gated_detection():
    result = _cfar_run()
    meta = result.metadata["sensing"]
    det_meta = meta["detection"]
    res_m = meta["bistatic_range_resolution_m"]
    assert res_m == pytest.approx(C / 20e6)
    detected = 0
    fa = 0
    for frame in result.frames:
        s = frame.sensing
        assert isinstance(s.detections, list)
        for d in s.detections:
            assert d.statistic > d.threshold == det_meta["threshold"]["value"]
            assert -N // 2 <= d.cell_nu < N // 2 and 0 <= d.cell_tau < K
            assert d.range_sum_m == pytest.approx(C * d.delay_ns * 1e-9)
        for r in s.links:
            assert r.reason in ("detected", "cfar_miss")
            link_dets = [d for d in s.detections if (d.tx_id, d.rx_id) == (r.tx_id, r.rx_id)]
            gated = [d for d in link_dets if _in_gate(d, r)]
            if r.reason == "cfar_miss":
                assert not gated and not r.detected and r.detection_index is None
                assert r.measured_range_m is None and r.range_bin is None
                continue
            detected += 1
            d = s.detections[r.detection_index]
            assert (d.tx_id, d.rx_id) == (r.tx_id, r.rx_id) and r.detected
            assert d.statistic == max(g.statistic for g in gated)
            assert r.measured_range_m == d.range_sum_m
            assert r.measured_doppler_hz == d.doppler_hz
            assert r.range_bin == math.floor(d.range_sum_m / res_m)
            assert r.doppler_bin == round(d.doppler_hz * 0.01)
            # Truth-side fields stay the oracle's.
            assert r.bistatic_range_m == pytest.approx(C * 1e-9 * next(
                p.delay_ns for p in s.echoes if p.path_id == r.path_id
                and (p.tx_id, p.rx_id) == (r.tx_id, r.rx_id)
            ))
            assert abs(r.measured_range_m - r.bistatic_range_m) < 1.5 * C / (K * DFP)
        # Every stored detection outside all gates of its link is a false alarm.
        for d in s.detections:
            if not any(_in_gate(d, r) for r in s.links if (r.tx_id, r.rx_id) == (d.tx_id, d.rx_id)):
                fa += 1
    assert detected >= 0.75 * 3 * 16
    assert all(e.status == "ok" for f in result.frames for e in f.sensing.estimates)
    assert max(e.position_error_m for f in result.frames for e in f.sensing.estimates) < 5.0

    assert det_meta["mode"] == "cfar" and det_meta["frames"] == 3
    assert det_meta["map"]["shape"] == [N, K]
    assert det_meta["map"]["notched_rows"] == [0] and det_meta["map"]["notch_bins"] == 0
    assert det_meta["map"]["non_training_rows"] == [-1, 0, 1]
    assert det_meta["achieved"]["cell"]["dispersion"] >= 1.0
    assert det_meta["cfar"]["n_train"] == 200 and det_meta["cfar"]["alpha"] > 0
    assert det_meta["detections_total"] == det_meta["detections_stored"]
    achieved = det_meta["achieved"]
    assert achieved["cell"]["false_alarms"] == fa
    assert achieved["link_frame"]["trials"] == 48 and achieved["system"]["trials"] == 3
    for block in achieved.values():
        lo, hi = block["ci95"]
        assert 0.0 <= lo <= block["pfa"] <= hi <= 1.0
    assert det_meta["noise"]["seed"] == 0
    assert meta["sensing_tx_ids"] == ["trp1", "trp3", "trp4", "trp6"]


def test_stored_detections_equal_an_independent_map():
    """Frame 1, link trp1 -> trp3_rx: echoes + H1 background + thermal noise
    from the run's noise key, rebuilt with numpy (Hann, mean_subtract with
    its 0 Hz notch and non-training rows, brute-force CA-CFAR): the same
    detection cells as stored."""
    result = _cfar_run(noise_seed=5)
    frames = result.frames
    s = frames[1].sensing
    h1 = s.background_h1 or frames[s.background_meta.h1.source_frame].sensing.background_h1
    tx, rx = "trp1", "trp3_rx"
    mine = [p for p in s.echoes if p.target_id is not None] + list(h1)
    rows = [
        (p.path_gain_db, p.phase_rad, p.delay_ns, p.doppler_hz)
        for p in mine
        if (p.tx_id, p.rx_id) == (tx, rx)
    ]
    sigma2 = _sigma2()
    Y = ref_synth(rows, F, T) + ref_noise(5, 1, tx, rx, (N, K), sigma2)
    P = ref_map(Y, "hann", 1, sigma2, mean_subtract=True)
    stat = _ref_stat(P)
    alpha = 200 * (1e-4 ** (-1 / 200) - 1)
    hits = (stat > alpha) & TESTED_ROWS[:, None]
    cells = {(int(u), int(r) - N // 2) for r, u in zip(*np.nonzero(hits))}
    stored = [d for d in s.detections if (d.tx_id, d.rx_id) == (tx, rx)]
    assert {(d.cell_tau, d.cell_nu) for d in stored} == cells and cells
    for d in stored:
        assert d.statistic == pytest.approx(stat[d.cell_nu + N // 2, d.cell_tau], rel=1e-9)
        assert d.power_db == pytest.approx(10 * math.log10(P[d.cell_nu + N // 2, d.cell_tau]))
    assert [d.statistic for d in stored] == sorted((d.statistic for d in stored), reverse=True)


def test_weak_target_is_a_cfar_miss():
    weak = Actor(**{**_uav().model_dump(), "sensing": {"model": "constant", "rcs_dbsm": -80.0}})
    result = _cfar_run(scene=_trp_scene(actors=[weak]))
    reasons = {r.reason for f in result.frames for r in f.sensing.links}
    assert reasons == {"cfar_miss"}
    assert all(e.status == "insufficient_links" for f in result.frames for e in f.sensing.estimates)


def test_detection_cap_keeps_the_gated_detection():
    result = _cfar_run(detection={**CFAR, "max_detections_per_link": 1, "oracle_gate_cells": 2})
    det = result.metadata["sensing"]["detection"]
    assert det["detections_total"] > det["detections_stored"]
    for frame in result.frames:
        s = frame.sensing
        for r in s.links:
            if r.detected:
                d = s.detections[r.detection_index]
                assert (d.tx_id, d.rx_id) == (r.tx_id, r.rx_id) and _in_gate(d, r, gate=2)


def test_measurement_noise_note_and_targetless_run():
    result = _cfar_run(measurement_noise=True, num_frames=1)
    assert any(w.endswith(MEASUREMENT_NOISE_NOTE) for w in result.warnings)
    empty = _cfar_run(scene=_trp_scene(actors=[]), background={"mode": "h0_removed"},
                      num_frames=2)
    for frame in empty.frames:
        assert frame.sensing.links == [] and frame.sensing.estimates == []
        assert isinstance(frame.sensing.detections, list)
    assert empty.metadata["sensing"]["detection"]["achieved"]["link_frame"]["trials"] == 32


def test_threshold_mode_is_unchanged():
    base = _scenario(_trp_scene(), _config(), num_frames=3, cpi_pulses=4096,
                     measurement_noise=True)
    explicit = _scenario(_trp_scene(), _config(), num_frames=3, cpi_pulses=4096,
                         measurement_noise=True, detection={"mode": "threshold"})
    a, b = base.model_dump(mode="json"), explicit.model_dump(mode="json")
    assert "detection" not in a["metadata"]["sensing"]["options"]
    popped = b["metadata"]["sensing"]["options"].pop("detection")
    assert popped["mode"] == "threshold" and popped["threshold"] == "analytic"
    assert a == b
    assert set(a["frames"][0]["sensing"]) == {"echoes", "links", "estimates", "nodes"}


# ------------------------------------------------------------ H0 calibration (API)


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Track B det", "project_id": PID})
    assert resp.status_code == 201, resp.text
    _put_scene(api_client, _trp_scene(scene_id=PID))
    return api_client


def _put_scene(client, scene):
    put = client.put(f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json"))
    assert put.status_code == 200, put.text


def _set_actors(client, actors):
    """Swap the scene's actors, keeping its stored result refs."""
    scene = client.get(f"/api/projects/{PID}/scene").json()
    scene["actors"] = [a.model_dump(mode="json") for a in actors]
    put = client.put(f"/api/projects/{PID}/scene", json=scene)
    assert put.status_code == 200, put.text


def _post_run(client, num_frames=2, **sensing):
    payload = {"config": MOCK_CFG, "num_frames": num_frames, "dt_s": 0.5, "include_paths": False,
               "sensing": {"enabled": True, **sensing}}
    return client.post(f"/api/projects/{PID}/simulate/scenario", json=payload)


def _calibrated(h0_id, **cfar):
    return {**CFAR, "threshold": "h0_calibrated", "h0_result_id": h0_id,
            "cfar": {"pfa_cell": 1e-3, **cfar}}


def _reference_threshold(maps, pfa):
    values = [
        _ref_stat(ref_map(Y, "hann", 1, s2, mean_subtract=True))[TESTED_ROWS].ravel()
        for Y, s2 in maps
    ]
    return ref_quantile(np.concatenate(values), pfa), sum(v.size for v in values)


def test_h0_calibrated_from_a_background_only_scenario(client):
    _set_actors(client, [])
    h0 = _post_run(client, num_frames=2, waveform=GRID_S, background={"mode": "h0_removed"},
                   noise_seed=3)
    assert h0.status_code == 200, h0.text
    h0 = h0.json()
    assert all(f["sensing"]["links"] == [] for f in h0["frames"])
    _set_actors(client, [_uav()])
    run = _post_run(client, num_frames=2, waveform=GRID_S, detection=_calibrated(h0["result_id"]))
    assert run.status_code == 200, run.text
    threshold = run.json()["metadata"]["sensing"]["detection"]["threshold"]
    assert threshold["kind"] == "h0_calibrated"
    src = threshold["source"]
    assert src["result_id"] == h0["result_id"] and src["kind"] == "scenario"
    assert src["frames"] == 2 and src["maps"] == 32 and src["n_values"] == 32 * K * (N - 1)
    # Independent: the stored H0 paths + the H0 run's seed on the calibration stream.
    bg0 = h0["frames"][0]["sensing"]["background_h0"]
    maps = []
    for i in range(2):
        for tx in ("trp1", "trp3", "trp4", "trp6"):
            for rx in ("trp1_rx", "trp3_rx", "trp4_rx", "trp6_rx"):
                rows = [(p["path_gain_db"], p["phase_rad"], p["delay_ns"], p["doppler_hz"])
                        for p in bg0 if (p["tx_id"], p["rx_id"]) == (tx, rx)]
                maps.append((ref_synth(rows, F, T) + ref_noise(3, i, tx, rx, (N, K), _sigma2(),
                                                               stream=H0_STREAM),
                             _sigma2()))
    expected, n = _reference_threshold(maps, 1e-3)
    assert n == src["n_values"] and src["m"] == math.floor(n * 1e-3)
    assert threshold["value"] == pytest.approx(expected, rel=1e-6)


def test_h0_calibrated_from_an_ensemble(client):
    source = _post_run(client, num_frames=1, waveform=GRID_S, background={"mode": "h0_removed"})
    assert source.status_code == 200, source.text
    ens = client.post(
        f"/api/projects/{PID}/simulate/sensing-background-ensemble",
        json={"source": {"result_id": source.json()["result_id"], "frame_index": 0},
              "n_realizations": 2, "noise_frames_per_realization": 3, "seed": 9},
    )
    assert ens.status_code == 200, ens.text
    ens = ens.json()
    run = _post_run(client, num_frames=1, waveform=GRID_S, detection=_calibrated(ens["result_id"]))
    assert run.status_code == 200, run.text
    threshold = run.json()["metadata"]["sensing"]["detection"]["threshold"]
    src = threshold["source"]
    assert src["kind"] == "sensing_background_ensemble"
    assert src["frames"] == 6 and src["maps"] == 6 * 16
    paths = {r["index"]: r["background_h0"] for r in ens["realizations"]}
    maps = []
    for real in ens["realizations"]:
        bg = paths[real["source_realization"]]
        for m in range(3):
            for link in ens["links"]:
                tx, rx = link["tx_id"], link["rx_id"]
                rows = [(p["path_gain_db"], p["phase_rad"], p["delay_ns"],
                         p["doppler_hz"] + real["cfo_hz"].get(rx, 0.0))
                        for p in bg if (p["tx_id"], p["rx_id"]) == (tx, rx)]
                Y = ref_synth(rows, F, T) + ref_noise(9, m, tx, rx, (N, K), _sigma2(),
                                                      realization=real["index"], stream=H0_STREAM)
                maps.append((Y, _sigma2()))
    expected, n = _reference_threshold(maps, 1e-3)
    assert n == src["n_values"]
    assert threshold["value"] == pytest.approx(expected, rel=1e-6)


def test_bad_h0_sources_and_map_sizes_answer_400(client):
    plain = _post_run(client, num_frames=1, cpi_pulses=4096).json()
    cases = {
        "unknown": "unknown result",
        plain["result_id"]: "no H0 background frames",
    }
    for h0_id, message in cases.items():
        resp = _post_run(client, num_frames=1, waveform=GRID_S, detection=_calibrated(h0_id))
        assert resp.status_code == 400 and message in resp.json()["detail"], resp.text
    resp = _post_run(client, num_frames=1, waveform=GRID_S,
                     detection={**CFAR, "cfar": {"train": [200, 4]}})
    assert resp.status_code == 400 and "does not fit" in resp.json()["detail"]
    resp = _post_run(client, num_frames=1, detection=CFAR)
    assert resp.status_code == 422 and "needs sensing.waveform" in resp.text
    # 1400 x 612 pilot REs x zero_pad 8^2 = 54.8 M map cells: 400 before any solve.
    nr = {"scs_hz": 30e3, "n_fft": 1024, "n_active_sc": 612}
    resp = _post_run(client, num_frames=1, waveform=nr, cpi_s=0.05,
                     detection={**CFAR, "map": {"zero_pad": 8}})
    assert resp.status_code == 400 and "at most 16777216" in resp.json()["detail"], resp.text
    resp = _post_run(client, num_frames=1, waveform=GRID_S,
                     detection={**CFAR, "map": {"zero_doppler_notch_bins": 8}})
    assert resp.status_code == 400 and "notches every one" in resp.json()["detail"], resp.text
