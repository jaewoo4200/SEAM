"""Detector models and Monte Carlo (services/detector.py) and POST
/analysis/pd-curve: the Swerling 0/1/3 closed forms against scipy and their
derivation, the Monte Carlo against the closed forms, the empirical
threshold, Wilson intervals, and the route.

Monte Carlo checks use fixed seeds (deterministic) and a 99.9 % Wilson
interval, so they are not flaky; the 95 % interval the API reports is
pinned separately.
"""

import math

import numpy as np
import pytest
from pydantic import ValidationError
from scipy import stats

from seam_studio.schemas.sensing import DETECTOR_MODELS, PdCurveRequest
from seam_studio.services.detector import (
    DetectorError,
    marcum_q1,
    monte_carlo_pd,
    noise_reference,
    noise_seed,
    pd_curve,
    pd_swerling,
    snr_for_pd,
    wilson_ci,
)
from seam_studio.services.isac import snr_for_pd_db
from seam_studio.services.sensing_track import swerling1_pd

PFA = 1e-6
T = -math.log(PFA)
Z999 = 3.2905267314919255  # two-sided 99.9 %


def _within(mc, analytic: float, z: float = Z999) -> bool:
    lo, hi = wilson_ci(round(mc.pd * mc.trials), mc.trials, z)
    return lo <= analytic <= hi


# --------------------------------------------------------------- analytic


def test_swerling1_is_the_v0112_expression_bit_for_bit():
    xs = np.arange(-30.0, 60.0, 0.37)
    assert np.array_equal(pd_swerling(xs, PFA, "swerling1"), swerling1_pd(xs, PFA))
    assert np.array_equal(pd_swerling(xs, PFA), swerling1_pd(xs, PFA))
    for v in xs:
        assert pd_swerling(float(v), PFA) == swerling1_pd(float(v), PFA)
        assert pd_swerling(float(v), 1e-3, "swerling1") == swerling1_pd(float(v), 1e-3)
    grid = np.array([[0.0, 13.0], [21.0, -5.0]])
    assert np.array_equal(pd_swerling(grid, PFA), swerling1_pd(grid, PFA))
    for model in DETECTOR_MODELS:
        assert pd_swerling(None, PFA, model) == PFA
    with pytest.raises(DetectorError):
        pd_swerling(10.0, PFA, "swerling2")  # type: ignore[arg-type]
    with pytest.raises(DetectorError):
        pd_swerling(10.0, 1.0)


@pytest.mark.parametrize("pfa", [0.1, 1e-3, 1e-6, 1e-12, 1e-300])
def test_swerling0_series_matches_scipy_ncx2(pfa):
    t = -math.log(pfa)
    db = np.arange(-20.0, 60.01, 0.25)
    ref = stats.ncx2.sf(2.0 * t, 2, 2.0 * 10.0 ** (db / 10.0))
    ours = pd_swerling(db, pfa, "swerling0")
    assert ours.shape == db.shape
    # The guide's "about 1e-13" (measured 3e-15 down to Pfa 1e-12, 1.2e-13
    # at 1e-300).
    assert np.max(np.abs(ours - ref)) <= (5e-13 if pfa < 1e-100 else 1e-14)
    # Scalars take the same path.
    assert pd_swerling(13.0, pfa, "swerling0") == pytest.approx(
        float(stats.ncx2.sf(2.0 * t, 2, 2.0 * 10.0**1.3)), abs=1e-13
    )
    # No echo (S = 0) detects at the false-alarm rate.
    assert pd_swerling(np.array([-np.inf]), pfa, "swerling0")[0] == pytest.approx(pfa, rel=1e-12)


def test_marcum_q1_matches_scipy():
    a = np.array([0.0, 0.5, 1.0, 3.0, 7.0, 20.0, 40.0])
    for b in (0.0, 0.3, 2.0, 5.0, 37.0):
        ref = stats.ncx2.sf(b * b, 2, a * a)
        assert np.allclose(marcum_q1(a, b), ref, rtol=0.0, atol=1e-11)
    assert marcum_q1(3.0, 2.0) == pytest.approx(float(stats.ncx2.sf(4.0, 2, 9.0)), abs=1e-12)
    assert marcum_q1(0.0, 2.0) == pytest.approx(math.exp(-2.0), rel=1e-12)
    assert marcum_q1(1.5, 0.0) == 1.0


def _gamma_mixture_pd(s_lin: float, t: float, shape: int) -> float:
    """Pd = P(Pois(T) <= K) with K | sigma ~ Pois(sigma), sigma ~
    Gamma(shape, S / shape): K is negative binomial (the guide's derivation),
    summed numerically."""
    theta = s_lin / shape
    p = 1.0 / (1.0 + theta)
    k = np.arange(0, 20000)
    return float(np.sum(stats.nbinom.pmf(k, shape, p) * stats.poisson.cdf(k, t)))


def test_swerling3_closed_form_and_the_gamma1_analogue():
    for db in (-5.0, 5.0, 13.0, 17.3, 25.0):
        s = 10.0 ** (db / 10.0)
        # Gamma(2, S/2): the Swerling-3 closed form.
        assert pd_swerling(db, PFA, "swerling3") == pytest.approx(
            _gamma_mixture_pd(s, T, 2), abs=1e-10
        )
        # Gamma(1, S): the same steps reproduce Swerling 1.
        assert swerling1_pd(db, PFA) == pytest.approx(_gamma_mixture_pd(s, T, 1), abs=1e-10)
        # Mahafza's form of the same expression.
        mahafza = math.exp(-T / (1 + s / 2)) * (1 + 2 * T * s / (2 + s) ** 2)
        assert pd_swerling(db, PFA, "swerling3") == pytest.approx(mahafza, rel=1e-12)
    arr = np.array([5.0, 13.0, 25.0])
    assert np.allclose(pd_swerling(arr, PFA, "swerling3"),
                       [pd_swerling(float(v), PFA, "swerling3") for v in arr], rtol=1e-15)


def test_snr_for_pd_values_and_monotone():
    # Pfa 1e-6: Pd 0.9 and Pd 0.5 (contract numbers).
    expected = {"swerling0": (13.18, 11.24), "swerling1": (21.14, 12.77), "swerling3": (17.30, 11.95)}
    for model, (at90, at50) in expected.items():
        assert snr_for_pd(0.9, PFA, model) == pytest.approx(at90, abs=0.01)
        assert snr_for_pd(0.5, PFA, model) == pytest.approx(at50, abs=0.01)
        snr = snr_for_pd(0.9, PFA, model)
        assert pd_swerling(snr, PFA, model) == pytest.approx(0.9, abs=1e-9)
        targets = [0.05, 0.2, 0.5, 0.8, 0.9, 0.99, 0.999]
        values = [snr_for_pd(p, PFA, model) for p in targets]
        assert all(b > a for a, b in zip(values, values[1:]))
        assert snr_for_pd(PFA, PFA, model) is None
        assert snr_for_pd(PFA / 2, PFA, model) is None
    # swerling1 is ISAC's closed form, bit for bit.
    for p in (0.5, 0.9, 0.99):
        assert snr_for_pd(p, PFA, "swerling1") == snr_for_pd_db(p, PFA)
    with pytest.raises(DetectorError):
        snr_for_pd(1.0, PFA, "swerling0")


def test_wilson_ci():
    lo, hi = wilson_ci(180_000, 200_000)
    assert (lo, hi) == pytest.approx((0.89868, 0.90131), abs=1e-5)
    lo, hi = wilson_ci(0, 200_000)
    assert lo == 0.0 and hi == pytest.approx(1.92e-5, rel=1e-3)
    assert wilson_ci(200_000, 200_000)[1] == 1.0
    assert wilson_ci(0, 0) == (0.0, 1.0)
    # center + half rounds to 1 - 1 ulp for some n (20 000 did on the demo,
    # so an analytic Pd of exactly 1.0 fell outside its own interval).
    for n in (3, 7, 1000, 20_000, 123_457, 2_000_000):
        assert wilson_ci(n, n)[1] == 1.0
        assert wilson_ci(0, n)[0] == 0.0


# ------------------------------------------------------------ Monte Carlo


@pytest.mark.parametrize(
    "model,snrs",
    [
        ("swerling0", (8.0, 10.0, 12.0, 13.0, 14.0)),
        ("swerling1", (10.0, 13.0, 16.0, 20.0, 25.0)),
        ("swerling3", (10.0, 13.0, 16.0, 20.0, 25.0)),
    ],
)
def test_monte_carlo_matches_the_closed_forms(model, snrs):
    for i, db in enumerate(snrs):
        mc = monte_carlo_pd(db, PFA, 200_000, model, seed=[7, i], cpi_pulses=4096)
        analytic = pd_swerling(db, PFA, model)
        assert _within(mc, analytic), (model, db, mc.pd, analytic)
        assert mc.ci_low <= mc.pd <= mc.ci_high
        assert mc.threshold == pytest.approx(T) and not mc.empirical_threshold
        # Independent noise run: the measured Pfa interval holds 1e-6.
        assert mc.pfa_ci_low <= PFA <= mc.pfa_ci_high


def test_monte_carlo_seeds_are_streams():
    a = monte_carlo_pd(13.0, 1e-3, 50_000, "swerling3", seed=[0, 1, 2])
    b = monte_carlo_pd(13.0, 1e-3, 50_000, "swerling3", seed=[0, 1, 2])
    c = monte_carlo_pd(13.0, 1e-3, 50_000, "swerling3", seed=[0, 1, 3])
    assert a == b and a.pd != c.pd
    # measure_pfa=False skips the noise run but draws the same signal stream.
    d = monte_carlo_pd(13.0, 1e-3, 50_000, "swerling3", seed=[0, 1, 2], measure_pfa=False)
    assert d.pd == a.pd and math.isnan(d.pfa_measured)
    # No echo: the detector fires at the false-alarm rate.
    e = monte_carlo_pd(None, 1e-2, 100_000, seed=4)
    assert e.ci_low <= 1e-2 <= e.ci_high


def test_explicit_pulses_and_sufficient_statistic_agree():
    # explicit_pulses draws all 16 pulses of each of 100k trials (1.6e6 <=
    # 4e6); the default draws z = A + CN(0, 1) directly. Same law, so both
    # hold the analytic Pd.
    pfa, db = 1e-3, 15.0
    for model in DETECTOR_MODELS:
        explicit = monte_carlo_pd(db, pfa, 100_000, model, seed=5, cpi_pulses=16,
                                  explicit_pulses=True)
        direct = monte_carlo_pd(db, pfa, 100_000, model, seed=6, cpi_pulses=16)
        analytic = pd_swerling(db, pfa, model)
        assert _within(explicit, analytic) and _within(direct, analytic), model
        # Two independent estimates of one probability.
        se = math.sqrt(2.0 * analytic * (1.0 - analytic) / 100_000)
        assert abs(explicit.pd - direct.pd) <= 4.0 * se
        assert explicit.pfa_ci_low <= pfa <= explicit.pfa_ci_high


def test_monte_carlo_cost_does_not_grow_with_cpi_pulses(monkeypatch):
    # 976 trials x 4096 pulses used to fit the explicit-pulse budget (4e6),
    # so it drew every pulse: ~1000x the cost of 977 trials, unseen by the
    # trials x estimates caps. Now every estimate draws one complex sample
    # per trial (plus one per trial for the amplitude and the Pfa run).
    from seam_studio.services import detector

    real = detector._cn
    drawn: list[int] = []

    def spy(rng, shape):
        drawn.append(int(np.prod(shape)))
        return real(rng, shape)

    monkeypatch.setattr(detector, "_cn", spy)
    n = 976
    for model in DETECTOR_MODELS:
        drawn.clear()
        big = monte_carlo_pd(15.0, 1e-3, n, model, seed=[1, 2], cpi_pulses=4096)
        assert sum(drawn) <= 3 * n
        # cpi_pulses no longer changes the draw at all.
        assert big == monte_carlo_pd(15.0, 1e-3, n, model, seed=[1, 2], cpi_pulses=1)
    drawn.clear()
    ref = noise_reference(1e-3, n, noise_seed(0), 4096)
    assert sum(drawn) == n and not ref.explicit_pulses
    assert ref == noise_reference(1e-3, n, noise_seed(0), 1)
    # The explicit draw is opt-in (and its N-fold cost with it).
    drawn.clear()
    monte_carlo_pd(15.0, 1e-3, n, seed=[1, 2], cpi_pulses=4096, explicit_pulses=True,
                   measure_pfa=False)
    assert sum(drawn) == n + n * 4096


def test_noise_stream_never_aliases_a_pd_stream():
    # SeedSequence zero-pads keys shorter than 4 words: [s, 999] (coverage
    # cell 999) and [s, 999, 0] (ISAC tx 999, beam 0) are one stream, which
    # was the noise pair's [s, 999, 0]. noise_seed ends in a nonzero word.
    s = 7

    def draws(key):
        return np.random.default_rng(key).random(4)

    assert np.array_equal(draws([s, 999]), draws([s, 999, 0]))  # the padding
    noise = draws(noise_seed(s))
    assert noise_seed(s) == [s, 999, 0, 1]
    for key in ([s, 999], [s, 999, 0], [s, 999, 0, 0], [s, 0], [s, 0, 0]):
        assert not np.array_equal(draws(key), noise), key


def test_empirical_threshold_reproduces_pfa():
    pfa = 1e-3
    mc = monte_carlo_pd(None, pfa, 40_000, seed=1, empirical_threshold=True)
    assert mc.empirical_threshold
    assert mc.threshold != pytest.approx(-math.log(pfa), abs=1e-12)
    assert abs(mc.threshold - (-math.log(pfa))) < 0.5
    assert mc.pfa_ci_low <= pfa <= mc.pfa_ci_high
    # The shared pair: the realized Pfa of an estimated threshold varies by
    # about sqrt(pfa / n) on top of run 2's binomial spread.
    n = 200_000
    ref = noise_reference(pfa, n, noise_seed(0), 1, True)
    assert ref.empirical_threshold == ref.threshold
    assert abs(ref.pfa_measured - pfa) <= 4.0 * math.sqrt(2.0 * pfa / n)
    analytic = noise_reference(pfa, 40_000, noise_seed(0))
    assert analytic.empirical_threshold is None and analytic.threshold == -math.log(pfa)


def test_detector_errors():
    with pytest.raises(DetectorError, match="20 / pfa = 20000"):
        monte_carlo_pd(10.0, 1e-3, 19_999, empirical_threshold=True)
    with pytest.raises(DetectorError, match="monte_carlo_trials"):
        monte_carlo_pd(10.0, 1e-3, 0)
    with pytest.raises(DetectorError, match="monte_carlo_trials"):
        monte_carlo_pd(10.0, 1e-3, 2_000_001)
    with pytest.raises(DetectorError, match="unknown detector model"):
        monte_carlo_pd(10.0, 1e-3, 10, "swerling4")  # type: ignore[arg-type]
    with pytest.raises(DetectorError):
        noise_reference(1e-3, 100, empirical_threshold=True)
    # The caller's threshold skips the empirical run (and its trial check).
    mc = monte_carlo_pd(10.0, 1e-3, 100, empirical_threshold=True, threshold=5.0)
    assert mc.threshold == 5.0


# ---------------------------------------------------------------- pd-curve


def test_pd_curve_service():
    out = pd_curve(PdCurveRequest())
    assert len(out.snr_db) == 71 and out.snr_db[0] == -5.0 and out.snr_db[-1] == 30.0
    assert [m.model for m in out.models] == list(DETECTOR_MODELS)
    assert out.threshold == pytest.approx(T)
    assert out.pfa_measured is None and out.empirical_threshold is None
    for m in out.models:
        assert len(m.pd) == 71
        assert m.pd_mc is None and m.mc_max_abs_deviation is None
        assert all(b >= a for a, b in zip(m.pd, m.pd[1:]))
        assert m.snr_for_pd_target_db == snr_for_pd(0.9, PFA, m.model)
    assert out.metadata["mc_method"] == "none (analytic only)"

    req = PdCurveRequest(
        snr_min_db=8.0, snr_max_db=20.0, step_db=2.0, models=["swerling3", "swerling0"],
        monte_carlo_trials=100_000, cpi_pulses=4096, seed=3,
    )
    out = pd_curve(req)
    assert [m.model for m in out.models] == ["swerling3", "swerling0"]
    assert out.pfa_measured is not None and len(out.pfa_measured_ci) == 2
    assert out.metadata["mc_method"].startswith("sufficient statistic")
    for m in out.models:
        assert len(m.pd_mc) == len(m.pd_mc_ci_low) == len(m.pd_mc_ci_high) == 7
        assert m.mc_max_abs_deviation == pytest.approx(
            max(abs(a - b) for a, b in zip(m.pd_mc, m.pd))
        )
        assert m.mc_max_abs_deviation < 0.01
        assert m.mc_within_ci_fraction >= 5 / 7
        # Streams [seed, model index in DETECTOR_MODELS, snr index].
        index = DETECTOR_MODELS.index(m.model)
        point = monte_carlo_pd(12.0, PFA, 100_000, m.model, seed=[3, index, 2], cpi_pulses=4096)
        assert m.pd_mc[2] == point.pd


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Pd", "project_id": "pd_curve"})
    assert resp.status_code == 201, resp.text
    return api_client


def _post(client, body=None, project_id: str = "pd_curve"):
    return client.post(f"/api/projects/{project_id}/analysis/pd-curve", json=body)


def test_api_pd_curve(client):
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["snr_db"]) == 71
    assert {m["model"]: round(m["snr_for_pd_target_db"], 2) for m in body["models"]} == {
        "swerling0": 13.18, "swerling1": 21.14, "swerling3": 17.30
    }
    assert all(m["pd_mc"] is None for m in body["models"])
    assert body["pfa_measured"] is None
    resp = _post(client, {"monte_carlo_trials": 20_000, "snr_min_db": 10, "snr_max_db": 12,
                          "models": ["swerling1", "swerling1"], "pfa": 1e-3,
                          "empirical_threshold": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [m["model"] for m in body["models"]] == ["swerling1"]
    assert len(body["models"][0]["pd_mc"]) == 5
    assert body["empirical_threshold"] is not None and body["pfa_measured"] is not None
    assert any("empirical threshold" in w for w in body["warnings"])
    # Not persisted.
    assert client.get("/api/projects/pd_curve/scene").json()["result_sets"] == []


def test_api_pd_curve_errors(client, monkeypatch):
    assert _post(client, project_id="ghost").status_code == 404
    # 2e6 trials x 71 points x 3 models > 2e8.
    resp = _post(client, {"monte_carlo_trials": 2_000_000})
    assert resp.status_code == 422 and "at most 200000000" in resp.text
    assert _post(client, {"monte_carlo_trials": 2_000_001}).status_code == 422
    assert _post(client, {"snr_min_db": 10, "snr_max_db": 0}).status_code == 422
    assert _post(client, {"empirical_threshold": True, "monte_carlo_trials": 1000}).status_code == 422
    assert _post(client, {"models": ["swerling2"]}).status_code == 422
    with pytest.raises(ValidationError):
        PdCurveRequest(step_db=0.001, snr_min_db=-100, snr_max_db=200)

    from seam_studio.services import detector

    def boom(request):
        raise detector.DetectorError("bad detector input")

    monkeypatch.setattr(detector, "pd_curve", boom)
    resp = _post(client)
    assert resp.status_code == 400 and resp.json()["detail"] == "bad detector input"
