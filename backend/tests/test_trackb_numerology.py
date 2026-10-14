"""Track B #23: OFDM numerology -> integration gain, B_occ, resolutions.

The reference side is plain math in this file (never the module under
test): the request's table (612 SC x 280 symbols = 171,360 RE, B_occ
18.36 MHz, c/B_occ 16.33 m; Grid S 256 x 16 = 4096 = 36.12 dB on the
v0.1.15 20 MHz floor) and the 10 -> 50 ms CPI scaling (+6.99 dB). Without a
waveform every service keeps its v0.1.15 output, pinned by literals.
"""

import math
from pathlib import Path

import numpy as np
import pytest

from seam_studio.schemas.actors import ScenarioSimulateRequest
from seam_studio.schemas.channel import ChannelAnalysisRequest
from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Actor, ActorTrajectory, Scene
from seam_studio.schemas.sensing import (
    ISACRequest,
    PdCurveRequest,
    PilotOptions,
    SensingCoverageRequest,
    SensingTrackOptions,
    WaveformOptions,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.channel_analysis import analyze_channel
from seam_studio.services.detector import DetectorError, pd_curve
from seam_studio.services.isac import plan_isac_roles, run_isac
from seam_studio.services.numerology import (
    GRID_S,
    NR_30K_51PRB,
    NumerologyError,
    effective_cpi_pulses,
    resolve_numerology,
    symbol_duration_s,
)
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import run_scenario
from seam_studio.services.sensing import select_targets
from seam_studio.services.sensing_coverage import (
    SensingCoverageError,
    coverage_grid,
    run_sensing_coverage,
)
from seam_studio.services.sensing_track import detection_constants
from seam_studio.services.simulation_backends.mock_backend import MockBackend

C = 299_792_458.0
F1 = 3.5e9
MOCK_CFG = {
    "id": "default", "backend": "mock", "frequency_hz": F1,
    "bandwidth_hz": 2e7, "noise_figure_db": 7.0,
}
TRPS = {
    "trp1": (82.75, -57.74, 20.84),
    "trp3": (78.64, 87.18, 19.51),
    "trp4": (-50.5, 84.57, 18.62),
}
NR = NR_30K_51PRB.model_dump(mode="json")
GS = GRID_S.model_dump(mode="json")


def _config(**kw) -> SimulationConfig:
    return SimulationConfig(**{**MOCK_CFG, **kw})


def _rel(a: float, b: float, tol: float = 1e-12) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b), 1e-300)


def _reference(scs, n_active, t_sym, sc_step, sym_step, cpi_s, nf=7.0) -> dict:
    """The numerology table from first principles."""
    symbols = math.floor(cpi_s / t_sym + 1e-9)
    k = math.ceil(n_active / sc_step)
    n = math.ceil(symbols / sym_step)
    b_occ = n_active * scs
    return {
        "symbol_duration_s": t_sym,
        "symbols_per_cpi": symbols,
        "grid_k": k,
        "grid_n": n,
        "n_re_per_cpi": k * n,
        "integration_gain_db": 10.0 * math.log10(k * n),
        "b_occ_hz": b_occ,
        "bistatic_range_resolution_m": C / b_occ,
        "range_resolution_m": C / (2.0 * b_occ),
        "doppler_resolution_hz": 1.0 / (n * sym_step * t_sym),
        "noise_floor_dbm": -174.0 + 10.0 * math.log10(b_occ) + nf,
        "delay_bin_s": 1.0 / (k * sc_step * scs),
        "pilot_fraction": 1.0 / (sc_step * sym_step),
    }


# Golden table (P0 contract 4.2): preset, cpi_s -> exact values.
GOLDEN = [
    (NR_30K_51PRB, 0.01, 0.5e-3 / 14, 280, 612, 280, 171_360, 52.33909, 18.36e6,
     16.328565, 100.0, -94.36127),
    (NR_30K_51PRB, 0.05, 0.5e-3 / 14, 1400, 612, 1400, 856_800, 59.32879, 18.36e6,
     16.328565, 20.0, -94.36127),
    (GRID_S, 0.01, 1 / 14_400, 144, 256, 16, 4096, 36.12360, 20.0e6,
     14.989623, 100.0, -93.98970),
    (GRID_S, 0.05, 1 / 14_400, 720, 256, 80, 20_480, 43.11330, 20.0e6,
     14.989623, 20.0, -93.98970),
]


@pytest.mark.parametrize("row", GOLDEN, ids=["nr_10ms", "nr_50ms", "gridS_10ms", "gridS_50ms"])
def test_numerology_table_exact(row):
    w, cpi, t_sym, symbols, k, n, n_re, gain, b_occ, rng, dres, floor = row
    num = resolve_numerology(w, cpi)
    ref = _reference(w.scs_hz, w.n_active_sc, t_sym, w.pilot.subcarrier_step,
                     w.pilot.symbol_step, cpi)
    for key, value in ref.items():
        got = num.noise_floor_dbm(7.0) if key == "noise_floor_dbm" else getattr(num, key)
        assert _rel(got, value), (key, got, value)
    assert (num.symbols_per_cpi, num.grid_k, num.grid_n, num.n_re_per_cpi) == (symbols, k, n, n_re)
    # The table's rounded figures.
    assert num.integration_gain_db == pytest.approx(gain, abs=5e-6)
    assert num.b_occ_hz == b_occ
    assert num.bistatic_range_resolution_m == pytest.approx(rng, abs=5e-7)
    assert num.doppler_resolution_hz == pytest.approx(dres, rel=1e-12)
    assert num.noise_floor_dbm(7.0) == pytest.approx(floor, abs=5e-6)

    consts = detection_constants(_config(), SensingTrackOptions(cpi_s=cpi, waveform=w))
    for key in ("b_occ_hz", "n_re_per_cpi", "grid_k", "grid_n", "symbols_per_cpi",
                "symbol_duration_s", "pilot_fraction"):
        assert _rel(consts[key], ref[key]), key
    assert consts["cpi_pulses"] == n_re
    assert _rel(consts["integration_gain_db"], ref["integration_gain_db"])
    assert _rel(consts["noise_floor_dbm"], ref["noise_floor_dbm"])
    assert _rel(consts["bistatic_range_resolution_m"], C / b_occ)
    assert _rel(consts["doppler_resolution_hz"], ref["doppler_resolution_hz"])
    assert _rel(consts["velocity_resolution_m_s"], (C / F1) * ref["doppler_resolution_hz"] / 2.0)
    assert _rel(consts["n0_dbm_per_re"], -174.0 + 10.0 * math.log10(w.scs_hz) + 7.0)
    assert consts["numerology"]["n_re_per_cpi"] == n_re


@pytest.mark.parametrize("w", [NR_30K_51PRB, GRID_S], ids=["nr", "gridS"])
def test_cpi_50ms_adds_6_99_db_at_the_same_pilot_density(w):
    g10 = detection_constants(_config(), SensingTrackOptions(cpi_s=0.01, waveform=w))
    g50 = detection_constants(_config(), SensingTrackOptions(cpi_s=0.05, waveform=w))
    gain = g50["integration_gain_db"] - g10["integration_gain_db"]
    assert gain == pytest.approx(10.0 * math.log10(5.0), abs=1e-9)
    assert gain == pytest.approx(6.98970, abs=5e-6)
    assert g50["noise_floor_dbm"] == g10["noise_floor_dbm"]


def test_grid_s_reproduces_4096_and_the_v0115_floor():
    consts = detection_constants(_config(), SensingTrackOptions(waveform=GRID_S))
    assert consts["n_re_per_cpi"] == 4096 == consts["cpi_pulses"]
    assert consts["integration_gain_db"] == pytest.approx(36.123599479677736, abs=1e-12)
    # B_occ = 1280 x 15.625 kHz = 20 MHz: the v0.1.15 nominal floor, unchanged.
    assert consts["noise_floor_dbm"] == pytest.approx(-93.98970004336019, abs=1e-9)
    v0115 = detection_constants(_config(), SensingTrackOptions(cpi_pulses=4096))
    assert consts["noise_floor_dbm"] == pytest.approx(v0115["noise_floor_dbm"], abs=1e-9)
    assert consts["integration_gain_db"] == pytest.approx(v0115["integration_gain_db"], abs=1e-12)
    # Pilot axes: comb-5 from the centred grid, f_k = (k - 128) 78.125 kHz.
    f, t = resolve_numerology(GRID_S, 0.01).axes("pilot")
    np.testing.assert_array_equal(f, (np.arange(256) * 5 - 640) * 15_625.0)
    np.testing.assert_allclose(t, np.arange(16) * 9 / 14_400, rtol=1e-15)
    fa, ta = resolve_numerology(NR_30K_51PRB, 0.01).axes("active")
    assert fa.shape == (612,) and ta.shape == (280,)
    assert fa[0] == -306 * 30e3 and fa[-1] == 305 * 30e3


def test_symbol_durations():
    def w(scs, cp="normal", n_active=12):
        return WaveformOptions(scs_hz=scs, n_fft=4096, n_active_sc=n_active, cp=cp)

    assert symbol_duration_s(w(15e3)) == pytest.approx(1e-3 / 14, rel=1e-15)
    assert symbol_duration_s(w(30e3)) == pytest.approx(0.5e-3 / 14, rel=1e-15)
    assert symbol_duration_s(w(60e3, "extended")) == pytest.approx(0.25e-3 / 12, rel=1e-15)
    assert symbol_duration_s(w(20e3)) == pytest.approx((1 / 20e3) * (1 + 144 / 2048), rel=1e-15)
    assert symbol_duration_s(GRID_S) == 1 / 14_400


def test_numerology_errors():
    with pytest.raises(NumerologyError, match="shorter than one waveform symbol"):
        resolve_numerology(NR_30K_51PRB, 1e-5)
    long = NR_30K_51PRB.model_copy(update={"symbols_per_cpi": 400})
    with pytest.raises(NumerologyError, match="do not fit the CPI"):
        resolve_numerology(long, 0.01)
    fits = NR_30K_51PRB.model_copy(update={"symbols_per_cpi": 140})
    assert resolve_numerology(fits, 0.01).n_re_per_cpi == 612 * 140
    # Block pilots every 4th symbol: K = 612, N = ceil(280 / 4).
    block = NR_30K_51PRB.model_copy(
        update={"pilot": PilotOptions(pattern="block", symbol_step=4)}
    )
    num = resolve_numerology(block, 0.01)
    assert (num.grid_k, num.grid_n, num.pilot_fraction) == (612, 70, 0.25)
    assert num.doppler_resolution_hz == pytest.approx(100.0, rel=1e-12)
    with pytest.raises(ValueError):
        PilotOptions(pattern="block", subcarrier_step=2)


# ------------------------------------------------------------ v0.1.15 pins

V0115_CONSTS = {
    "frequency_hz": 3500000000.0,
    "wavelength_m": 0.085654988,
    "bandwidth_hz": 20000000.0,
    "noise_figure_db": 7.0,
    "noise_floor_dbm": -93.98970004336019,
    "integration_gain_db": 0.0,
    "threshold_db": 13.0,
    "cpi_s": 0.01,
    "mti_min_doppler_hz": 100.0,
    "range_resolution_m": 7.49481145,
    "bistatic_range_resolution_m": 14.9896229,
    "doppler_resolution_hz": 100.0,
    "velocity_resolution_m_s": 4.2827494,
    "mti_blind_speed_m_s": 4.2827494,
}
V0115_SENSING_KEYS = [
    "options", "frequency_hz", "wavelength_m", "bandwidth_hz", "noise_figure_db",
    "noise_floor_dbm", "integration_gain_db", "threshold_db", "cpi_s", "mti_min_doppler_hz",
    "range_resolution_m", "bistatic_range_resolution_m", "doppler_resolution_hz",
    "velocity_resolution_m_s", "mti_blind_speed_m_s", "num_links", "sensing_rx_ids",
    "sensing_rx_rule", "target_ids", "targets", "detection_rate", "frames_ge3_links_rate",
    "median_position_error_m", "median_velocity_error_m_s",
]


def test_detection_constants_without_waveform_is_v0115():
    consts = detection_constants(_config(), SensingTrackOptions())
    assert list(consts) == list(V0115_CONSTS)
    assert consts == V0115_CONSTS
    # cpi_s alone still moves only the Doppler figures (v0.1.15 semantics).
    c50 = detection_constants(_config(), SensingTrackOptions(cpi_s=0.05, cpi_pulses=4096))
    assert c50["integration_gain_db"] == pytest.approx(10.0 * math.log10(4096), abs=1e-12)
    assert list(c50) == list(V0115_CONSTS)


def _uav() -> Actor:
    return Actor(
        id="uav_01", kind="uav", position=[-30.0, 0.0, 60.0],
        trajectory=ActorTrajectory(
            waypoints=[[-30.0, 0.0, 60.0], [90.0, 0.0, 60.0]], speed_m_s=10.0, mode="once"
        ),
        sensing={"model": "tr38901", "object_type": "uav-small-size"},
    )


def _scene(ue: bool = False) -> Scene:
    devices = [Device(id=k, kind="tx", position=list(p), power_dbm=43.0) for k, p in TRPS.items()]
    devices += [Device(id=f"{k}_rx", kind="rx", position=list(p)) for k, p in TRPS.items()]
    if ue:
        devices.append(Device(id="ue1", kind="rx", position=[40.0, -20.0, 15.0]))
    return Scene(scene_id="numerology", devices=devices, actors=[_uav()])


def _scenario(**sensing):
    request = ScenarioSimulateRequest(
        num_frames=3, dt_s=0.5, include_paths=False, sensing={"enabled": True, **sensing}
    )
    return run_scenario(
        MockBackend(), Path("."), _scene(), load_default_library(), _config(), request
    )


def test_scenario_without_waveform_keeps_the_v0115_shape():
    result = _scenario(cpi_pulses=4096)
    assert list(result.metadata["sensing"]) == V0115_SENSING_KEYS
    for frame in result.frames:
        dumped = frame.model_dump(mode="json")
        assert set(dumped["sensing"]) == {"echoes", "links", "estimates", "nodes"}
        assert all("detection_index" not in r for r in dumped["sensing"]["links"])
    assert not [w for w in result.warnings if "integration gain" in w]


def test_scenario_waveform_drives_the_snr_axis():
    plain = _scenario(cpi_pulses=4096)
    wave = _scenario(waveform=NR)
    meta = wave.metadata["sensing"]
    assert meta["n_re_per_cpi"] == 171_360 and meta["b_occ_hz"] == 18.36e6
    assert meta["sensing_tx_ids"] == list(TRPS)
    assert meta["options"]["waveform"]["n_active_sc"] == 612
    # Same echo, per-RE SNR on B_occ: +10log10(171360/4096) gain and the
    # 0.37 dB lower floor.
    delta = 10.0 * math.log10(171_360 / 4096) + 10.0 * math.log10(20e6 / 18.36e6)
    for f_plain, f_wave in zip(plain.frames, wave.frames):
        for a, b in zip(f_plain.sensing.links, f_wave.sensing.links):
            assert b.snr_db - a.snr_db == pytest.approx(delta, abs=1e-9)
    gs = _scenario(waveform=GS)
    for f_plain, f_gs in zip(plain.frames, gs.frames):
        assert [r.snr_db for r in f_gs.sensing.links] == pytest.approx(
            [r.snr_db for r in f_plain.sensing.links], abs=1e-9
        )


def test_w1_and_w2_warnings():
    w1 = _scenario(cpi_s=0.05, cpi_pulses=4096)
    msgs = [w for w in w1.warnings if "integration gain does not follow cpi_s" in w]
    assert msgs == [
        "sensing: integration gain does not follow cpi_s without sensing.waveform "
        "(cpi_s 0.05 s, cpi_pulses 4096 -> 36.12 dB)"
    ]
    w2 = _scenario(cpi_pulses=4096, waveform=NR)
    msgs = [w for w in w2.warnings if "overridden by the waveform numerology" in w]
    assert msgs == [
        "sensing: cpi_pulses 4096 overridden by the waveform numerology: "
        "171360 pilot REs per CPI (612 x 280)"
    ]
    assert w2.metadata["sensing"]["options"]["cpi_pulses"] == 4096
    assert w2.metadata["sensing"]["cpi_pulses"] == 171_360
    # Matching cpi_pulses, default cpi_s, or no explicit cpi_pulses: silent.
    for kw in ({"cpi_pulses": 171_360, "waveform": NR}, {"cpi_pulses": 4096},
               {"cpi_s": 0.05}, {"cpi_s": 0.05, "waveform": NR}):
        assert not [w for w in _scenario(**kw).warnings if "cpi_pulses" in w], kw

    eff, warns, num = effective_cpi_pulses(None, 0.05, 4096, True, "isac")
    assert (eff, num) == (4096, None) and warns[0].startswith("isac: integration gain")


def _isac(**request):
    scene = _scene(ue=True)
    scene.actors = [
        Actor(id="uav_01", kind="uav", position=[55.0, 35.0, 19.5],
              sensing={"model": "constant", "rcs_dbsm": -10.0, "size_m": [1.0, 1.0, 1.0]})
    ]
    req = ISACRequest(**request)
    plan = plan_isac_roles(scene, req.tx_ids, req.ue_rx_ids, req.sensing_rx_ids)
    return run_isac(
        MockBackend(), Path("."), scene, load_default_library(), _config(), req, plan,
        select_targets(scene, req.target_actor_ids),
    )


def _coverage(**request):
    req = SensingCoverageRequest(center_xy=[0.0, 0.0], size_xy=[100.0, 100.0],
                                 cell_size_m=20.0, **request)
    return run_sensing_coverage(
        MockBackend(), Path("."), _scene(), load_default_library(), _config(), req
    )


def test_isac_warnings_and_original_request_metadata():
    w1 = _isac(cpi_s=0.05, cpi_pulses=4096)
    assert any(w.startswith("isac: integration gain does not follow cpi_s") for w in w1.warnings)
    w2 = _isac(cpi_pulses=4096, waveform=NR)
    assert any("isac: cpi_pulses 4096 overridden" in w and "171360" in w for w in w2.warnings)
    assert w2.metadata["request"]["cpi_pulses"] == 4096
    assert w2.noise_floor_dbm == pytest.approx(-174 + 10 * math.log10(18.36e6) + 7.0, abs=1e-12)
    # The beam SNRs move by the gain and floor difference.
    base = _isac()
    delta = 10.0 * math.log10(171_360 / 4096) + 10.0 * math.log10(20e6 / 18.36e6)
    for tb, tw in zip(base.txs, w2.txs):
        for bb, bw in zip(tb.beams, tw.beams):
            if bb.sensing_snr_db is not None:
                assert bw.sensing_snr_db - bb.sensing_snr_db == pytest.approx(delta, abs=1e-9)
    assert "request" in base.metadata and "waveform" not in base.metadata["request"]


@pytest.mark.parametrize("cpi_s", [0.01, 0.05])
@pytest.mark.parametrize("w", [NR, GS], ids=["nr", "gridS"])
def test_services_share_one_numerology(w, cpi_s):
    num = resolve_numerology(WaveformOptions(**w), cpi_s)
    scen = _scenario(waveform=w, cpi_s=cpi_s).metadata["sensing"]
    isac = _isac(waveform=w, cpi_s=cpi_s).metadata
    cov = _coverage(waveform=w, cpi_s=cpi_s).metadata
    pd = pd_curve(PdCurveRequest(waveform=w, cpi_s=cpi_s)).metadata
    for meta in (scen, isac, cov, pd):
        assert meta["n_re_per_cpi"] == num.n_re_per_cpi
        assert meta["b_occ_hz"] == num.b_occ_hz
    for meta in (scen, isac, cov):
        assert meta["integration_gain_db"] == pytest.approx(num.integration_gain_db, abs=1e-12)
        assert meta["noise_floor_dbm"] == pytest.approx(num.noise_floor_dbm(7.0), abs=1e-12)
    assert pd_curve(PdCurveRequest(waveform=w, cpi_s=cpi_s)).cpi_pulses == num.n_re_per_cpi
    ca = analyze_channel(
        Path("."), _scene(), load_default_library(),
        ChannelAnalysisRequest(config=_config(), tx_id="trp1", rx_id="trp3_rx",
                               waveform=WaveformOptions(**w)),
    )
    assert ca.metadata["numerology"]["b_occ_hz"] == num.b_occ_hz
    assert ca.subcarrier_spacing_khz == w["scs_hz"] / 1e3
    assert ca.num_resource_blocks == w["n_active_sc"] // 12


def test_channel_analysis_51_prb_and_rsrp():
    def run(**kw):
        return analyze_channel(
            Path("."), _scene(), load_default_library(),
            ChannelAnalysisRequest(config=_config(), tx_id="trp1", rx_id="trp3_rx", **kw),
        )

    plain = run()
    nr = run(waveform=NR_30K_51PRB)
    # v0.1.15: int(20 MHz / (12 x 30 kHz)) = 55 RB; the waveform: 51 PRB.
    assert plain.num_resource_blocks == 55 and "numerology" not in plain.metadata
    assert nr.num_resource_blocks == 51 and nr.subcarrier_spacing_khz == 30.0
    assert nr.rsrp_dbm - plain.rsrp_dbm == pytest.approx(10 * math.log10(660 / 612), abs=1e-9)


def test_channel_analysis_takes_the_waveform_cpi(monkeypatch):
    """No cpi_s on channel analysis: the numerology covers symbols_per_cpi
    symbols (else 10 ms, at least one symbol), so the guide's 50 ms preset
    and a symbol longer than 10 ms are accepted; a waveform the numerology
    rejects answers before the solve."""
    def run(waveform):
        return analyze_channel(
            Path("."), _scene(), load_default_library(),
            ChannelAnalysisRequest(config=_config(), tx_id="trp1", rx_id="trp3_rx",
                                   waveform=waveform),
        )

    base = run(NR_30K_51PRB)
    nr50 = run(NR_30K_51PRB.model_copy(update={"symbols_per_cpi": 1400}))
    num = nr50.metadata["numerology"]
    assert num["symbols_per_cpi"] == 1400 and num["cpi_s"] == pytest.approx(0.05)
    assert nr50.rsrp_dbm == base.rsrp_dbm and nr50.num_resource_blocks == 51
    long_symbol = run(WaveformOptions(n_fft=1024, symbol_duration_s=0.02))
    assert long_symbol.metadata["numerology"]["symbols_per_cpi"] == 1

    def no_solve(*a, **k):
        raise AssertionError("solved before the numerology check")

    monkeypatch.setattr(MockBackend, "simulate_paths", no_solve)
    with pytest.raises(NumerologyError, match="pilot REs per CPI"):
        run(NR_30K_51PRB.model_copy(update={"symbols_per_cpi": 200_000}))


def test_pilot_re_count_is_capped():
    num = resolve_numerology(NR_30K_51PRB, 1.0)
    assert num.n_re_per_cpi == 17_136_000
    with pytest.raises(NumerologyError, match=r"612 x 280000 = 171360000 pilot REs per CPI"):
        resolve_numerology(NR_30K_51PRB, 10.0)


def test_coverage_and_pd_curve():
    plain = _coverage()
    assert "numerology" not in plain.metadata
    assert not [w for w in plain.warnings if "cpi" in w]
    unused = _coverage(cpi_s=0.05)
    assert "sensing-coverage: cpi_s is unused without waveform" in unused.warnings
    assert unused.metadata["integration_gain_db"] == plain.metadata["integration_gain_db"]
    gs = _coverage(waveform=GS)
    # Grid S at 10 ms is the v0.1.15 default (4096 pulses, 20 MHz): same map.
    assert gs.values["best_snr_db"] == plain.values["best_snr_db"]
    nr = _coverage(waveform=NR, cpi_pulses=4096)
    assert any("sensing-coverage: cpi_pulses 4096 overridden" in w for w in nr.warnings)
    assert nr.metadata["request"]["cpi_pulses"] == 4096

    with pytest.raises(SensingCoverageError, match="shorter than one waveform symbol"):
        coverage_grid(SensingCoverageRequest(waveform=GS, cpi_s=1e-6, center_xy=[0, 0],
                                             size_xy=[10, 10]), None)

    pd = pd_curve(PdCurveRequest(cpi_pulses=8, waveform=GS))
    assert pd.cpi_pulses == 4096
    assert any("pd-curve: cpi_pulses 8 overridden" in w for w in pd.warnings)
    with pytest.raises(DetectorError, match="shorter than one waveform symbol"):
        pd_curve(PdCurveRequest(waveform=GS, cpi_s=1e-6))
    assert "numerology" not in pd_curve(PdCurveRequest()).metadata


def test_api_scenario_400_for_a_waveform_that_does_not_fit(api_client):
    pid = "numerology_api"
    assert api_client.post("/api/projects", json={"name": "N", "project_id": pid}).status_code == 201
    scene = _scene().model_copy(update={"scene_id": pid}).model_dump(mode="json")
    assert api_client.put(f"/api/projects/{pid}/scene", json=scene).status_code == 200
    body = {"config": MOCK_CFG, "num_frames": 2, "dt_s": 0.5, "include_paths": False}
    resp = api_client.post(
        f"/api/projects/{pid}/simulate/scenario",
        json={**body, "sensing": {"enabled": True, "cpi_s": 1e-5, "waveform": NR}},
    )
    assert resp.status_code == 400 and "shorter than one waveform symbol" in resp.json()["detail"]
    ok = api_client.post(
        f"/api/projects/{pid}/simulate/scenario",
        json={**body, "sensing": {"enabled": True, "waveform": GS}},
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["metadata"]["sensing"]["n_re_per_cpi"] == 4096


def test_api_isac_coverage_pd_curve_400_for_a_waveform_that_does_not_fit(api_client):
    pid = "numerology_api_isac"
    assert api_client.post("/api/projects", json={"name": "N", "project_id": pid}).status_code == 201
    scene = _scene(ue=True)
    scene.actors = [
        Actor(id="uav_01", kind="uav", position=[55.0, 35.0, 19.5],
              sensing={"model": "constant", "rcs_dbsm": -10.0, "size_m": [1.0, 1.0, 1.0]})
    ]
    body = scene.model_copy(update={"scene_id": pid}).model_dump(mode="json")
    assert api_client.put(f"/api/projects/{pid}/scene", json=body).status_code == 200
    bad = {"cpi_s": 1e-5, "waveform": NR}
    for route, req in (
        ("simulate/isac", {"config": MOCK_CFG, **bad}),
        ("simulate/sensing-coverage", {
            "config": MOCK_CFG, "center_xy": [0.0, 0.0], "size_xy": [40.0, 40.0],
            "cell_size_m": 20.0, **bad,
        }),
        ("analysis/pd-curve", bad),
    ):
        resp = api_client.post(f"/api/projects/{pid}/{route}", json=req)
        assert resp.status_code == 400, (route, resp.status_code, resp.text)
        assert "shorter than one waveform symbol" in resp.json()["detail"], route
    # 612 x 280,000 pilot REs at 10 s: 400 everywhere (ISAC was a 500 after
    # both solves; pd-curve answered cpi_pulses 171,360,000).
    huge = {"cpi_s": 10.0, "waveform": NR}
    for route, req in (
        ("simulate/isac", {"config": MOCK_CFG, **huge}),
        ("simulate/sensing-coverage", {
            "config": MOCK_CFG, "center_xy": [0.0, 0.0], "size_xy": [40.0, 40.0],
            "cell_size_m": 20.0, **huge,
        }),
        ("analysis/pd-curve", huge),
    ):
        resp = api_client.post(f"/api/projects/{pid}/{route}", json=req)
        assert resp.status_code == 400, (route, resp.status_code, resp.text)
        assert "pilot REs per CPI" in resp.json()["detail"], route
