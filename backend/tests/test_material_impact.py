"""Task T: material-aware vs single-material-baseline channel impact.

The CFR evaluation framework of Lee et al. (KICS 2026): solve the same TX->RX
link twice — once with the scene's assigned materials, once with every prim
rebound to a baseline material — and score NMSE / cosine similarity / signed
dRSS / capacity per position.

These tests force the MOCK backend so they are deterministic on any machine.
The key fixture trick: the only reflecting prim is bound to material `ground`
and the baseline is *also* `ground`, so the material-aware and baseline scenes
are RF-identical and every metric collapses to its mathematical identity
(cos-sim 1, dRSS 0, global NMSE undefined). The real per-position NMSE spread
is verified live on the Sionna backend (lab_room: -6..-17 dB).
"""

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seam_studio.api import channel as channel_api
from seam_studio.api import deps
from seam_studio.core.config import get_settings
import math

from seam_studio.schemas.material_impact import MaterialImpactRequest
from seam_studio.schemas.results import RayPath
from seam_studio.schemas.scene import Device, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.material_impact import _capacity_mbps, _cfr, material_impact
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm


def _scene() -> Scene:
    """One ground prim (material `ground`) plus a tx/rx pair. `ground` is the
    only reflecting prim, so binding the baseline to `ground` too makes the
    material-aware and baseline scenes identical."""
    return Scene(
        scene_id="mi",
        name="mi",
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
        simulation_configs=[SimulationConfig(id="default", frequency_hz=28e9)],
    )


# ----------------------------------------------------- capacity regression
#
# Regression guard for the TX-power double-count bug: _cfr must build H(f)
# from the per-path CHANNEL GAIN (dimensionless), so _capacity_mbps applies
# P_tx exactly once. Before the fix, _cfr used the absolute received power
# (power_dbm) and capacity multiplied P_tx in AGAIN -> ~1000x (30 dB) too big.


def test_capacity_applies_tx_power_once():
    """Single synthetic path with a known channel gain: the Shannon proxy must
    equal B*log2(1+SNR) with SNR = 10^((gain + P_tx - N)/10), and must NOT be
    the ~1000x-inflated value the old received-power CFR produced."""
    tx_power_dbm = 30.0
    path_gain_db = -160.0
    config = SimulationConfig(bandwidth_hz=20e6, noise_figure_db=7.0)
    noise = noise_floor_dbm(config)  # -174 + 10log10(20e6) + 7

    path = RayPath(
        path_id="p0",
        tx_id="tx_001",
        rx_id="rx_001",
        path_type="los",
        vertices=[[0.0, 0.0, 10.0], [20.0, 0.0, 1.5]],
        power_dbm=path_gain_db + tx_power_dbm,  # absolute received power
        path_gain_db=path_gain_db,
        delay_ns=0.0,
        phase_rad=0.0,
    )

    # Single CFR point -> f=0 -> |H| is exactly the single tap gain amplitude.
    _, h = _cfr([path], config.bandwidth_hz, 1, tx_power_dbm)
    cap = _capacity_mbps(h, tx_power_dbm, noise, config.bandwidth_hz)

    snr = 10.0 ** ((path_gain_db + tx_power_dbm - noise) / 10.0)
    expected = config.bandwidth_hz * math.log2(1.0 + snr) / 1e6
    assert cap == pytest.approx(expected, rel=1e-6)

    # The old bug fed received power (power_dbm) into H, then multiplied P_tx
    # again -> SNR inflated by P_tx (10^3). Assert that value is NOT produced.
    snr_buggy = 10.0 ** (tx_power_dbm / 10.0) * 10.0 ** (path.power_dbm / 10.0) / 10.0 ** (noise / 10.0)
    buggy = config.bandwidth_hz * math.log2(1.0 + snr_buggy) / 1e6
    assert buggy > 100.0 * expected  # sanity: the bug really is ~1000x larger
    assert cap < buggy / 100.0


def test_cfr_fallback_gain_matches_explicit_path_gain():
    """When path_gain_db is None, _cfr must fall back to power_dbm - tx_power,
    yielding the identical channel and capacity as the explicit-gain path."""
    tx_power_dbm = 23.0
    config = SimulationConfig(bandwidth_hz=20e6, noise_figure_db=7.0)
    noise = noise_floor_dbm(config)

    def _path(gain_field):
        return RayPath(
            path_id="p", tx_id="tx", rx_id="rx", path_type="los",
            vertices=[[0.0, 0.0, 10.0], [20.0, 0.0, 1.5]],
            power_dbm=-140.0 + tx_power_dbm, path_gain_db=gain_field,
            delay_ns=5.0, phase_rad=0.3,
        )

    _, h_explicit = _cfr([_path(-140.0)], config.bandwidth_hz, 8, tx_power_dbm)
    _, h_fallback = _cfr([_path(None)], config.bandwidth_hz, 8, tx_power_dbm)
    cap_explicit = _capacity_mbps(h_explicit, tx_power_dbm, noise, config.bandwidth_hz)
    cap_fallback = _capacity_mbps(h_fallback, tx_power_dbm, noise, config.bandwidth_hz)
    assert cap_fallback == pytest.approx(cap_explicit, rel=1e-12)


# ------------------------------------------------------------- service level


def test_material_impact_identity_three_waypoints(tmp_path: Path):
    """3 waypoints, baseline == the material actually on the reflecting prim:
    each position sees an identical material/baseline channel, so cos-sim is 1,
    dRSS is 0, and no error accumulates -> global_nmse_db stays None."""
    scene = _scene()
    library = load_default_library()
    config = SimulationConfig(id="default", backend="mock", frequency_hz=28e9)
    waypoints = [[12.0, 0.0, 1.5], [30.0, 0.0, 1.5], [45.0, 5.0, 1.5]]

    report = material_impact(
        MockBackend(), tmp_path, scene, library, config,
        MaterialImpactRequest(config=config, waypoints=waypoints, baseline_material_id="ground"),
    )
    assert report.backend == "mock"
    assert report.tx_id == "tx_001" and report.rx_id == "rx_001"
    assert len(report.positions) == 3
    for wp, row in zip(waypoints, report.positions):
        assert row.position == pytest.approx(wp)
        # Identical channels: cosine similarity is unity and dRSS vanishes.
        assert row.cosine_similarity == pytest.approx(1.0, abs=1e-9)
        assert row.delta_rss_db == pytest.approx(0.0, abs=1e-9)
        # No material vs baseline difference => below the sensitivity gate.
        assert row.material_sensitive is False
    # No per-position error accumulated, so the global NMSE is undefined.
    assert report.global_nmse_db is None
    assert report.mean_cosine_similarity == pytest.approx(1.0, abs=1e-9)
    assert report.mean_delta_rss_db == pytest.approx(0.0, abs=1e-9)
    assert report.material_sensitive_count == 0


def test_material_impact_unknown_baseline_raises(tmp_path: Path):
    scene = _scene()
    with pytest.raises(ValueError):
        material_impact(
            MockBackend(), tmp_path, scene, load_default_library(),
            SimulationConfig(backend="mock"),
            MaterialImpactRequest(config=SimulationConfig(backend="mock"),
                                  baseline_material_id="does_not_exist"),
        )


def test_material_impact_unknown_rx_raises(tmp_path: Path):
    scene = _scene()
    with pytest.raises(ValueError):
        material_impact(
            MockBackend(), tmp_path, scene, load_default_library(),
            SimulationConfig(backend="mock"),
            MaterialImpactRequest(config=SimulationConfig(backend="mock"),
                                  baseline_material_id="ground", rx_id="nope"),
        )


# --------------------------------------------------------------------- route
#
# Route-level TestClient over the channel router (which owns the
# /analyze/material-impact endpoint), same fixture pattern as
# test_channel_analysis.py::api_client.
#


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    store = deps.get_store()
    store.create_project("Material Impact", project_id="mi_test")
    store.save_scene("mi_test", _scene())
    app = FastAPI()
    app.include_router(channel_api.router, prefix="/api")
    # Do not re-raise server exceptions: assert on the returned 500 status.
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        get_settings.cache_clear()
        deps.get_store.cache_clear()


def test_api_material_impact_roundtrip_200(api_client):
    resp = api_client.post(
        "/api/projects/mi_test/analyze/material-impact",
        json={"config": {"backend": "mock"},
              "waypoints": [[12.0, 0.0, 1.5], [30.0, 0.0, 1.5]],
              "baseline_material_id": "ground"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["backend"] == "mock"
    assert body["tx_id"] == "tx_001" and body["rx_id"] == "rx_001"
    assert len(body["positions"]) == 2


def test_api_material_impact_bad_baseline_400(api_client):
    resp = api_client.post(
        "/api/projects/mi_test/analyze/material-impact",
        json={"config": {"backend": "mock"}, "baseline_material_id": "does_not_exist"},
    )
    assert resp.status_code == 400, resp.text


def test_api_material_impact_unknown_project_404(api_client):
    resp = api_client.post(
        "/api/projects/nope/analyze/material-impact",
        json={"config": {"backend": "mock"}, "baseline_material_id": "ground"},
    )
    assert resp.status_code == 404
