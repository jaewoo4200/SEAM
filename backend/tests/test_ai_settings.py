"""GET/PUT /api/settings/ai — the local-AI settings overlay (issue #2).

The overlay must apply to the RUNNING process (no restart), win over env vars
without hiding them, clear per-field via empty strings, and degrade — never
crash — on a corrupt or unwritable file. No test may touch the network: the
settings routes themselves never probe, and probe caches are cleared around
each test so no stale state leaks between tests.
"""

import json

import pytest

from seam_studio.api import deps
from seam_studio.core import config, paths
from seam_studio.services import ai_provider

DEFAULTS = {
    "ollama_url": "http://localhost:11434",
    "text_model": "qwen3:8b",
    "openai_url": "http://localhost:1234/v1",
    "openai_model": "google/gemma-4-31b",
}


@pytest.fixture(autouse=True)
def _isolated_settings(tmp_path, monkeypatch):
    """Point SEAM_HOME at tmp and clear every cache before AND after."""
    monkeypatch.setattr(paths, "SEAM_HOME", tmp_path)
    config.get_settings.cache_clear()
    deps.get_store.cache_clear()
    ai_provider.invalidate_probe_caches()
    yield
    config.get_settings.cache_clear()
    deps.get_store.cache_clear()
    ai_provider.invalidate_probe_caches()


def test_get_defaults_without_overlay(api_client, tmp_path):
    resp = api_client.get("/api/settings/ai")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    for key, value in DEFAULTS.items():
        assert body[key] == value
    assert body["overlay_fields"] == []
    assert str(tmp_path) in body["overlay_path"]


def test_put_applies_without_restart(api_client):
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "openai_url": "http://127.0.0.1:9999/v1",
            "openai_model": "my-model",
            "ollama_url": "",
            "text_model": "",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["openai_url"] == "http://127.0.0.1:9999/v1"
    # The core no-restart claim: the SAME process resolves the new value.
    assert config.get_settings().ai.openai_url == "http://127.0.0.1:9999/v1"
    assert config.get_settings().ai.openai_model == "my-model"
    # Untouched fields stay on their defaults.
    assert config.get_settings().ai.base_url == DEFAULTS["ollama_url"]


def test_overlay_wins_over_env(api_client, monkeypatch):
    monkeypatch.setenv("SEAM_OPENAI_URL", "http://env-host:1/v1")
    config.get_settings.cache_clear()
    assert config.get_settings().ai.openai_url == "http://env-host:1/v1"

    resp = api_client.put(
        "/api/settings/ai",
        json={
            "openai_url": "http://overlay-host:2/v1",
            "openai_model": "",
            "ollama_url": "",
            "text_model": "",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["openai_url"] == "http://overlay-host:2/v1"
    assert "openai_url" in body["env_fields"]  # shadowing is visible, not silent
    assert "openai_url" in body["overlay_fields"]
    assert config.get_settings().ai.openai_url == "http://overlay-host:2/v1"


def test_empty_string_clears_field_back_to_env(api_client, monkeypatch):
    monkeypatch.setenv("SEAM_OPENAI_URL", "http://env-host:1/v1")
    api_client.put(
        "/api/settings/ai",
        json={
            "openai_url": "http://overlay-host:2/v1",
            "openai_model": "",
            "ollama_url": "",
            "text_model": "",
        },
    )
    resp = api_client.put(
        "/api/settings/ai",
        json={"openai_url": "", "openai_model": "", "ollama_url": "", "text_model": ""},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["openai_url"] == "http://env-host:1/v1"  # env came back
    assert "openai_url" not in body["overlay_fields"]


def test_url_normalization_strips_trailing_slash(api_client):
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "openai_url": "http://host:1234/v1/",
            "openai_model": "",
            "ollama_url": "",
            "text_model": "",
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["openai_url"] == "http://host:1234/v1"
    overlay = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert overlay["ai"]["openai_url"] == "http://host:1234/v1"


@pytest.mark.parametrize(
    "bad", ["ftp://host/v1", "not a url", "http://", "http://" + "x" * 400]
)
def test_rejects_bad_urls(api_client, bad):
    resp = api_client.put(
        "/api/settings/ai",
        json={"openai_url": bad, "openai_model": "", "ollama_url": "", "text_model": ""},
    )
    assert resp.status_code == 422, resp.text
    # A rejected PUT must not leave a partial overlay behind.
    assert config.get_settings().ai.openai_url == DEFAULTS["openai_url"]


def test_wire_to_field_mapping(api_client):
    # The wire key is ollama_url but the dataclass field is base_url —
    # guard the rename in AI_OVERLAY_FIELDS.
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "ollama_url": "http://ollama-host:11434",
            "text_model": "",
            "openai_url": "",
            "openai_model": "",
        },
    )
    assert resp.status_code == 200, resp.text
    ai = config.get_settings().ai
    assert ai.base_url == "http://ollama-host:11434"
    assert ai.openai_url == DEFAULTS["openai_url"]


def test_corrupt_overlay_degrades_with_warning(api_client):
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text("{", encoding="utf-8")
    config.get_settings.cache_clear()
    # get_settings itself must not raise on a broken overlay.
    assert config.get_settings().ai.openai_url == DEFAULTS["openai_url"]
    resp = api_client.get("/api/settings/ai")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["warnings"], "corrupt overlay must surface a warning"
    assert body["openai_url"] == DEFAULTS["openai_url"]


def test_write_failure_is_500_and_no_state_change(api_client, monkeypatch):
    def boom(path, text):
        raise OSError("disk full")

    monkeypatch.setattr(config, "_atomic_write_text", boom)
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "openai_url": "http://host:9/v1",
            "openai_model": "",
            "ollama_url": "",
            "text_model": "",
        },
    )
    assert resp.status_code == 500
    # Cache was NOT cleared: the effective value still matches disk (defaults).
    assert config.get_settings().ai.openai_url == DEFAULTS["openai_url"]


def test_probe_caches_cleared_on_put(api_client):
    ai_provider._probe_cache["http://old:1"] = (999.0, False, "stale")
    ai_provider._model_cache["http://old:1"] = (999.0, ["stale-model"])
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "openai_url": "http://new:2/v1",
            "openai_model": "",
            "ollama_url": "",
            "text_model": "",
        },
    )
    assert resp.status_code == 200, resp.text
    assert ai_provider._probe_cache == {}
    assert ai_provider._model_cache == {}


def test_non_ai_overlay_content_preserved(api_client):
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(
        json.dumps({"future_section": {"keep": True}}), encoding="utf-8"
    )
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "openai_url": "http://host:3/v1",
            "openai_model": "",
            "ollama_url": "",
            "text_model": "",
        },
    )
    assert resp.status_code == 200, resp.text
    overlay = json.loads(paths.settings_file().read_text(encoding="utf-8"))
    assert overlay["future_section"] == {"keep": True}
    assert overlay["ai"] == {"openai_url": "http://host:3/v1"}


def test_malformed_ipv6_url_is_422_not_500(api_client):
    # urlparse raises ValueError on unbalanced brackets; must surface as a
    # field error, not an uncaught 500 (review finding).
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "ollama_url": "http://[::1:11434",
            "text_model": "",
            "openai_url": "",
            "openai_model": "",
        },
    )
    assert resp.status_code == 422, resp.text
    # A correctly bracketed IPv6 host still passes.
    resp = api_client.put(
        "/api/settings/ai",
        json={
            "ollama_url": "http://[::1]:11434",
            "text_model": "",
            "openai_url": "",
            "openai_model": "",
        },
    )
    assert resp.status_code == 200, resp.text


def test_get_reresolves_after_hand_edit(api_client):
    # Warm the lru_cache with the defaults, then hand-edit the documented
    # overlay file. GET must not return a self-contradictory response
    # (fresh overlay_fields + stale values) — it re-resolves (review finding).
    assert config.get_settings().ai.openai_url == DEFAULTS["openai_url"]
    paths.settings_file().parent.mkdir(parents=True, exist_ok=True)
    paths.settings_file().write_text(
        json.dumps({"version": 1, "ai": {"openai_url": "http://mybox:1234/v1"}}),
        encoding="utf-8",
    )
    resp = api_client.get("/api/settings/ai")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["openai_url"] == "http://mybox:1234/v1"
    assert body["overlay_fields"] == ["openai_url"]
