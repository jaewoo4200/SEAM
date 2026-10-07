"""Sionna availability = installed AND the Dr.Jit runtime works (F02/F04).

A CPU-only Windows/Linux box without LLVM-C has sionna-rt installed but no
working Dr.Jit backend, so ``import sionna.rt`` fails and every solve would come
back empty. The runtime probe is mocked here (no GPU needed): health and
/api/backends must say why, "auto" must run the mock with a note, and a named
"sionna" request must answer 409 with the reason.
"""

from __future__ import annotations

import subprocess

import pytest

from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import availability
from seam_studio.services.availability import SionnaRuntime
from seam_studio.services.simulation_backends import resolve_backend
from seam_studio.services.simulation_backends import sionna_backend

from .conftest import make_demo_scene

NO_BACKEND_REASON = (
    "sionna-rt installed but no Dr.Jit backend works (no CUDA device and "
    "LLVM-C not found); set DRJIT_LIBLLVM_PATH to LLVM-C.dll/libLLVM"
)


def _clear_caches() -> None:
    availability.sionna_available.cache_clear()
    availability.sionna_rcs_available.cache_clear()


@pytest.fixture()
def fake_runtime(monkeypatch):
    """Install a fake probe result; returns a setter. Caches are cleared on
    both ends so later tests see the real probe again."""
    real_available = availability.sionna_available
    real_rcs = availability.sionna_rcs_available

    def install(runtime: SionnaRuntime) -> None:
        real_available.cache_clear()
        real_rcs.cache_clear()
        monkeypatch.setattr(availability, "sionna_installed", lambda: True)
        monkeypatch.setattr(availability, "sionna_runtime", lambda: runtime)
        # A live test earlier in the session may have set a variant in-process.
        monkeypatch.setattr(sionna_backend, "_active_mitsuba_variant", lambda: None)

    yield install
    monkeypatch.undo()
    real_available.cache_clear()
    real_rcs.cache_clear()


def _project(client) -> str:
    from seam_studio.api import deps

    store = deps.get_store()
    store.create_project("Probe", project_id="probe")
    store.save_scene("probe", make_demo_scene("probe"))
    return "probe"


def test_no_drjit_backend_means_unavailable_with_reason(fake_runtime, api_client):
    fake_runtime(SionnaRuntime(cuda=False, llvm=False, rt_import=False, error=None))
    assert availability.sionna_available() is False
    assert availability.sionna_unavailable_reason() == NO_BACKEND_REASON

    health = api_client.get("/api/health").json()
    assert health["sionna_available"] is False
    sionna = {b["name"]: b for b in health["backends"]}["sionna"]
    assert sionna == {"name": "sionna", "available": False, "detail": NO_BACKEND_REASON}

    listing = {b["name"]: b for b in api_client.get("/api/backends").json()}
    assert listing["sionna"]["available"] is False
    assert listing["sionna"]["detail"] == NO_BACKEND_REASON
    assert listing["sionna"]["capabilities"]["compute"] == "none"
    assert listing["sionna"]["capabilities"]["gpu"] is False

    assert resolve_backend(SimulationConfig(backend="auto")).name == "mock"


def test_auto_runs_the_mock_with_a_note_and_named_sionna_409(fake_runtime, api_client):
    fake_runtime(SionnaRuntime(cuda=False, llvm=False, rt_import=False, error=None))
    pid = _project(api_client)
    resp = api_client.post(
        f"/api/projects/{pid}/simulate/paths", json={"config": {"backend": "auto"}}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["backend"] == "mock"
    assert f"auto backend: {NO_BACKEND_REASON}; ran the mock backend" in body["warnings"]

    named = api_client.post(
        f"/api/projects/{pid}/simulate/paths", json={"config": {"backend": "sionna"}}
    )
    assert named.status_code == 409
    assert named.json()["detail"] == (
        f"backend 'sionna' is not available on this machine: {NO_BACKEND_REASON}"
    )

    # An explicit mock request is the user's choice: no note.
    plain = api_client.post(
        f"/api/projects/{pid}/simulate/paths", json={"config": {"backend": "mock"}}
    )
    assert not any(w.startswith("auto backend:") for w in plain.json()["warnings"])


def test_rt_import_failure_is_reported(fake_runtime):
    fake_runtime(
        SionnaRuntime(cuda=True, llvm=False, rt_import=False, error="ImportError: boom")
    )
    assert availability.sionna_available() is False
    assert availability.sionna_unavailable_reason() == (
        "sionna-rt installed but sionna.rt failed to import: ImportError: boom"
    )
    assert availability.sionna_backend_detail(False) == availability.sionna_unavailable_reason()


def test_llvm_only_is_available_and_reports_llvm(fake_runtime, api_client):
    fake_runtime(SionnaRuntime(cuda=False, llvm=True, rt_import=True, error=None))
    assert availability.sionna_available() is True
    assert availability.sionna_unavailable_reason() is None
    caps = sionna_backend.SionnaBackend().capabilities()
    assert caps["compute"] == "llvm" and caps["gpu"] is False
    listing = {b["name"]: b for b in api_client.get("/api/backends").json()}
    assert listing["sionna"]["available"] is True
    assert listing["sionna"]["capabilities"]["compute"] == "llvm"


def test_cuda_reports_gpu(fake_runtime):
    fake_runtime(SionnaRuntime(cuda=True, llvm=False, rt_import=True, error=None))
    caps = sionna_backend.SionnaBackend().capabilities()
    assert caps["compute"] == "cuda" and caps["gpu"] is True


def test_an_active_variant_still_wins(fake_runtime, monkeypatch):
    fake_runtime(SionnaRuntime(cuda=False, llvm=True, rt_import=True, error=None))
    monkeypatch.setattr(
        sionna_backend, "_active_mitsuba_variant", lambda: "cuda_ad_mono_polarized"
    )
    caps = sionna_backend.SionnaBackend().capabilities()
    assert caps["compute"] == "cuda" and caps["gpu"] is True


def test_probe_timeout_never_hides_an_installed_engine(fake_runtime):
    fake_runtime(
        SionnaRuntime(False, False, False, "runtime probe timed out", timed_out=True)
    )
    assert availability.sionna_available() is True
    assert availability.sionna_unavailable_reason() is None
    assert availability.sionna_backend_detail(True).endswith(
        " (runtime probe timed out; assuming usable)"
    )


def test_probe_parses_the_child_and_survives_failures(monkeypatch):
    probe = availability.sionna_runtime.__wrapped__  # bypass the process cache

    def ran(stdout: str, stderr: str = "", code: int = 0):
        return lambda *a, **k: subprocess.CompletedProcess(a, code, stdout, stderr)

    monkeypatch.setattr(
        subprocess, "run",
        ran('noise\n{"cuda": false, "llvm": true, "rt": true, "error": null}\n'),
    )
    assert probe() == SionnaRuntime(cuda=False, llvm=True, rt_import=True, error=None)

    monkeypatch.setattr(subprocess, "run", ran("", "first\nFatal: segfault\n", code=3))
    assert probe() == SionnaRuntime(False, False, False, "Fatal: segfault")

    def slow(*a, **k):
        raise subprocess.TimeoutExpired(cmd="probe", timeout=60)

    monkeypatch.setattr(subprocess, "run", slow)
    assert probe() == SionnaRuntime(
        False, False, False, "runtime probe timed out", timed_out=True
    )


def test_concurrent_cold_callers_share_one_probe(monkeypatch):
    # /health and /backends fire together on page load; one child, not two.
    import threading
    import time

    calls = []

    def slow_run(*args, **kwargs):
        calls.append(args)
        time.sleep(0.3)
        out = '{"cuda": true, "llvm": false, "rt": true, "error": null}\n'
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(subprocess, "run", slow_run)
    availability.sionna_runtime.cache_clear()
    try:
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(availability.sionna_runtime()))
            for _ in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(5)
        assert len(calls) == 1
        assert results == [SionnaRuntime(True, False, True, None)] * 3
    finally:  # later tests re-probe the real runtime
        availability.sionna_runtime.cache_clear()


def test_real_probe_returns_a_runtime():
    runtime = availability.sionna_runtime()
    assert isinstance(runtime, SionnaRuntime)
    if availability.sionna_installed():
        assert availability.sionna_available() == (
            runtime.timed_out or ((runtime.cuda or runtime.llvm) and runtime.rt_import)
        )


def test_auto_fallback_note_reaches_unstored_readouts(fake_runtime, api_client):
    # The note used to be added only when a result was persisted with its
    # config: beamforming, channel analysis (even persist=True), sweeps,
    # material impact and calibration returned mock numbers without it.
    fake_runtime(SionnaRuntime(cuda=False, llvm=False, rt_import=False, error=None))
    pid = _project(api_client)
    base = f"/api/projects/{pid}"
    note = f"auto backend: {NO_BACKEND_REASON}; ran the mock backend"
    auto = {"backend": "auto"}
    sample = {"rx_position": [20.0, 5.0, 1.5], "measured_path_gain_db": -80.0}
    calls = {
        "beamforming": ("/simulate/beamforming", {"config": auto}),
        "channel": ("/analyze/channel", {"config": auto, "persist": True}),
        "sweep": ("/analyze/channel-sweep", {
            "config": auto, "sweep_field": "tx_power_dbm", "sweep_values": [20.0, 30.0],
        }),
        "spectrogram": ("/analyze/spectrogram", {"config": auto}),
        "material_impact": ("/analyze/material-impact", {"config": auto}),
        "disambiguate": ("/calibrate/disambiguate", {
            "config": auto, "prim_ids": ["/buildings/b01/wall_01"],
            "candidate_material_ids": ["itu_concrete", "itu_brick"],
            "measurements": [sample],
        }),
        "validate": ("/calibrate/validate-trajectory", {
            "config": auto, "measurements": [sample, {**sample, "rx_position": [25.0, 5.0, 1.5]}],
        }),
    }
    for name, (route, body) in calls.items():
        resp = api_client.post(base + route, json=body)
        assert resp.status_code == 200, (name, resp.text)
        assert resp.json()["warnings"].count(note) == 1, name

    stored = api_client.get(f"{base}/results/channel").json()
    assert stored["warnings"].count(note) == 1

    # An explicit mock request is the user's choice: no note anywhere.
    plain = api_client.post(f"{base}/simulate/beamforming", json={"config": {"backend": "mock"}})
    assert not any(w.startswith("auto backend:") for w in plain.json()["warnings"])


def test_runtime_load_failure_is_not_blamed_on_llvm(fake_runtime):
    # drjit/mitsuba failing to import (a DLL error, a wheel for another Python)
    # also leaves cuda and llvm False: installing LLVM would not help.
    fake_runtime(SionnaRuntime(
        cuda=False, llvm=False, rt_import=False,
        error="ImportError: DLL load failed while importing _drjit_ext",
    ))
    reason = availability.sionna_unavailable_reason()
    assert reason == (
        "sionna-rt installed but its Dr.Jit/Mitsuba runtime failed to load: "
        "ImportError: DLL load failed while importing _drjit_ext"
    )
    assert "DRJIT_LIBLLVM_PATH" not in reason


def test_probe_json_wins_over_a_teardown_crash(monkeypatch):
    # The child printed its verdict and then crashed while exiting.
    probe = availability.sionna_runtime.__wrapped__
    out = '{"cuda": true, "llvm": false, "rt": true, "error": null}\n'
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: subprocess.CompletedProcess(a, -11, out, "Segmentation fault\n"),
    )
    assert probe() == SionnaRuntime(cuda=True, llvm=False, rt_import=True, error=None)
