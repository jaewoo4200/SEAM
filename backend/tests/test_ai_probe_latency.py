"""AI reachability probes never make /health or /ai/status wait (F31).

On a machine without Ollama / LM Studio the probes used to run one after the
other with a 1.5 s timeout each, and their 30 s cache always expired before
the GUI's 45 s poll: every poll paid ~6 s. Now both probes run concurrently,
a url never probed is waited for at most 1 s ("probing" meanwhile), and an
expired entry is served at once while one background refresh runs.
"""

from __future__ import annotations

import threading
import time
import types

import httpx
import pytest

from seam_studio.core.config import get_settings
from seam_studio.services import ai_provider, availability

HANG_S = 3.0


@pytest.fixture()
def slow_servers(monkeypatch):
    """httpx.get hangs (up to 3 s, until released) then times out, like an
    unreachable host that drops packets. Yields the call log."""
    release = threading.Event()
    calls: list[str] = []
    lock = threading.Lock()

    def slow_get(url, *args, **kwargs):
        with lock:
            calls.append(url)
        release.wait(HANG_S)
        raise httpx.ConnectTimeout("timed out")

    monkeypatch.setenv("SEAM_AI_ENABLED", "auto")
    get_settings.cache_clear()
    ai_provider.invalidate_probe_caches()
    monkeypatch.setattr(httpx, "get", slow_get)
    yield types.SimpleNamespace(calls=calls, release=release)
    release.set()
    _drain()
    ai_provider.invalidate_probe_caches()
    get_settings.cache_clear()


def _drain(timeout_s: float = 5.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with ai_provider._probe_lock:
            busy = bool(ai_provider._inflight) or bool(ai_provider._refreshing)
        if not busy:
            return
        time.sleep(0.01)
    raise AssertionError("AI probes still running")


def _details() -> dict[str, str]:
    return {s.name: s.detail for s in ai_provider.get_provider_statuses()}


def test_cold_status_answers_within_the_wait_with_probing(slow_servers):
    settings = get_settings().ai
    t0 = time.perf_counter()
    details = _details()
    assert time.perf_counter() - t0 < 1.5
    assert details["local_openai"].startswith(
        f"{settings.openai_url}: probing (no answer within 1.0 s yet)"
    )
    assert details["ollama_text"] == f"{settings.base_url}: probing (no answer within 1.0 s yet)"
    assert ai_provider._probe_cache == {}  # "probing" is never cached

    slow_servers.release.set()
    _drain()
    t0 = time.perf_counter()
    details = _details()
    assert time.perf_counter() - t0 < 0.1
    assert "not reachable" in details["local_openai"]
    assert "not reachable" in details["ollama_text"]
    assert len(slow_servers.calls) == 2  # one probe per url, no retries


def test_expired_entry_is_served_at_once_with_one_refresh(slow_servers, monkeypatch):
    slow_servers.release.set()
    _details()
    _drain()
    slow_servers.release.clear()
    before = len(slow_servers.calls)

    real = time.monotonic
    later = types.SimpleNamespace(
        monotonic=lambda: real() + ai_provider._PROBE_TTL_S + 1.0,
        perf_counter=time.perf_counter,
        sleep=time.sleep,
        time=time.time,
    )
    monkeypatch.setattr(ai_provider, "time", later)
    t0 = time.perf_counter()
    first = _details()
    second = _details()
    assert time.perf_counter() - t0 < 0.1
    assert "not reachable" in first["local_openai"] and first == second
    with ai_provider._probe_lock:
        assert len(ai_provider._refreshing) == 2  # openai + ollama, once each
    slow_servers.release.set()
    _drain()
    assert len(slow_servers.calls) - before == 2


def test_health_does_not_wait_on_ai_probes(slow_servers, api_client):
    availability.sionna_available()  # the runtime probe is a separate, one-time cost
    t0 = time.perf_counter()
    resp = api_client.get("/api/health")
    assert time.perf_counter() - t0 < 1.5
    assert resp.status_code == 200
    by_name = {p["name"]: p for p in resp.json()["ai_providers"]}
    assert "probing" in by_name["ollama_text"]["detail"]
    assert by_name["disabled"]["available"] is False
    assert by_name["disabled"]["detail"] == (
        "not selected (AI assistance is on; SEAM_AI_ENABLED=auto)"
    )


def test_cold_health_overlaps_the_runtime_probe(slow_servers, api_client, monkeypatch):
    # A fresh process pays the ~1 s Sionna runtime probe and the 1 s AI wait
    # once; run one after the other they took ~2 s, overlapped ~1 s.
    import subprocess

    def slow_probe(*args, **kwargs):
        time.sleep(1.0)
        out = '{"cuda": true, "llvm": false, "rt": true, "error": null}\n'
        return subprocess.CompletedProcess(args, 0, out, "")

    monkeypatch.setattr(availability, "sionna_installed", lambda: True)
    monkeypatch.setattr(subprocess, "run", slow_probe)
    availability.sionna_available.cache_clear()
    availability.sionna_rcs_available.cache_clear()
    availability.sionna_runtime.cache_clear()
    try:
        t0 = time.perf_counter()
        resp = api_client.get("/api/health")
        assert time.perf_counter() - t0 < 1.5
        assert resp.json()["sionna_available"] is True
    finally:  # later tests re-probe the real runtime
        availability.sionna_available.cache_clear()
        availability.sionna_rcs_available.cache_clear()
        availability.sionna_runtime.cache_clear()


def test_invalidate_drops_answers_still_in_flight(slow_servers):
    _details()  # both probes now hang in the pool
    ai_provider.invalidate_probe_caches()
    slow_servers.release.set()
    _drain()
    assert ai_provider._probe_cache == {}


def test_a_probe_started_before_a_cache_clear_never_answers_after_it(slow_servers, monkeypatch):
    # The in-flight probe of the old generation used to be reused after
    # invalidate_probe_caches(): a re-probe right after PUT /ai/settings (or the
    # next test) got the previous answer instead of probing again.
    url = "http://127.0.0.1:9/v1"
    ok, detail = ai_provider._probe_openai(url, 0.05)
    assert not ok and "probing" in detail  # first probe still hanging
    ai_provider.invalidate_probe_caches()
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: types.SimpleNamespace(raise_for_status=lambda: None)
    )
    slow_servers.release.set()  # the old probe now answers "not reachable"
    assert ai_provider._probe_openai(url, 2.0) == (True, f"{url}: reachable")
    _drain()
    assert ai_provider._inflight == {}
