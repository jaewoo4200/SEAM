"""Tests for AI model discovery, the /ai/models endpoint, and per-request
model overrides.

No network: every httpx call is monkeypatched. The discovery helpers cache by
url, so ``_fresh_caches`` clears the probe and model caches between tests.
"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from seam_studio.api import ai as ai_api
from seam_studio.api import deps
from seam_studio.core.config import get_settings
from seam_studio.schemas.ai import SuggestMaterialsRequest
from seam_studio.schemas.scene import MeshRef, Prim, Scene, VisualBinding
from seam_studio.services import ai_provider
from seam_studio.services.ai_provider import (
    LocalOpenAIProvider,
    OllamaTextProvider,
    get_provider_models,
    list_ollama_models,
    list_openai_models,
    suggest_materials,
)
from seam_studio.services.project_store import load_default_library

WINDOW_ID = "/buildings/b01/window_12"


@pytest.fixture(autouse=True)
def _fresh_caches():
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    ai_provider._probe_cache.clear()
    ai_provider._model_cache.clear()
    yield
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    ai_provider._probe_cache.clear()
    ai_provider._model_cache.clear()


@pytest.fixture()
def library():
    return load_default_library()


@pytest.fixture()
def scene():
    return Scene(
        scene_id="ai_test",
        name="AI Test",
        prims=[
            Prim(
                id=WINDOW_ID,
                name="window_12",
                mesh_ref=MeshRef(mesh_name="building_01"),
                visual=VisualBinding(
                    material_id="blue_glass_pbr",
                    material_name="blue_glass_pbr",
                ),
            ),
        ],
    )


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _openai_payload(content: str) -> dict:
    return {"choices": [{"message": {"content": content, "reasoning_content": ""}}]}


def _valid_payload() -> dict:
    return {
        "suggestions": [
            {
                "prim_id": WINDOW_ID,
                "recommended_rf_material_id": "itu_glass",
                "confidence": 0.86,
                "evidence": ["prim name contains 'window'"],
                "needs_user_confirmation": True,
            }
        ]
    }


# ------------------------------------------------------------ discovery helpers


def test_list_openai_models_parses_data_ids(monkeypatch):
    payload = {
        "data": [
            {"id": "google/gemma-4-31b"},
            {"id": "qwen2.5-coder-7b"},
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(payload))
    models = list_openai_models("http://localhost:1234/v1")
    assert models == ["google/gemma-4-31b", "qwen2.5-coder-7b"]


def test_list_openai_models_drops_embedding_models(monkeypatch):
    payload = {
        "data": [
            {"id": "google/gemma-4-31b"},
            {"id": "text-embedding-nomic-v1.5"},
            {"id": "some-EMBED-model"},
        ]
    }
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(payload))
    models = list_openai_models("http://localhost:1234/v1")
    # Both "embed"-containing ids (case-insensitive) are dropped.
    assert models == ["google/gemma-4-31b"]


def test_list_openai_models_unreachable_returns_empty(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    assert list_openai_models("http://localhost:1234/v1") == []


def test_list_ollama_models_parses_names(monkeypatch):
    payload = {"models": [{"name": "qwen3:8b"}, {"name": "qwen2.5vl:3b"}]}
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse(payload))
    models = list_ollama_models("http://localhost:11434")
    assert models == ["qwen3:8b", "qwen2.5vl:3b"]


def test_list_ollama_models_unreachable_returns_empty(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    assert list_ollama_models("http://localhost:11434") == []


# ----------------------------------------------------------- get_provider_models


def test_get_provider_models_reachable_shape(monkeypatch):
    def _get(url, *a, **k):
        if "/models" in url:
            return _FakeResponse({"data": [{"id": "google/gemma-4-31b"}]})
        if "/api/tags" in url:
            return _FakeResponse({"models": [{"name": "qwen3:8b"}]})
        raise httpx.ConnectError("no")

    monkeypatch.setattr(httpx, "get", _get)
    resp = get_provider_models()
    by_provider = {p.provider: p for p in resp.providers}
    assert set(by_provider) == {"local_openai", "ollama_text"}

    oai = by_provider["local_openai"]
    assert oai.available is True
    assert oai.default_model == "google/gemma-4-31b"
    assert any(m.id == "google/gemma-4-31b" and m.is_default for m in oai.models)

    ollama = by_provider["ollama_text"]
    assert ollama.available is True
    # Default (qwen3:8b) is offered even though discovery only returned it once.
    assert any(m.id == "qwen3:8b" and m.is_default for m in ollama.models)


def test_get_provider_models_unreachable_available_false_empty_discovery(monkeypatch):
    def _raise(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    resp = get_provider_models()
    by_provider = {p.provider: p for p in resp.providers}
    for name in ("local_openai", "ollama_text"):
        entry = by_provider[name]
        assert entry.available is False
        # No discovered models; only the configured default is offered.
        assert all(m.is_default for m in entry.models)
        assert entry.default_model is not None


# ---------------------------------------------------------------------- endpoint


def _make_app() -> FastAPI:
    app = FastAPI()
    app.include_router(ai_api.router, prefix="/api")
    return app


@pytest.fixture()
def client(tmp_path, monkeypatch, scene):
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    store = deps.get_store()
    info = store.create_project("AI Test", project_id="ai_test")
    store.save_scene("ai_test", scene)
    return TestClient(_make_app()), store, Path(info.path)


def test_api_ai_models_endpoint_shape(client, monkeypatch):
    http, _store, _dir = client

    def _get(url, *a, **k):
        if "/models" in url:
            return _FakeResponse({"data": [{"id": "google/gemma-4-31b"}]})
        if "/api/tags" in url:
            return _FakeResponse({"models": [{"name": "qwen3:8b"}]})
        raise httpx.ConnectError("no")

    monkeypatch.setattr(httpx, "get", _get)
    response = http.get("/api/projects/ai_test/ai/models")
    assert response.status_code == 200
    body = response.json()
    providers = {p["provider"]: p for p in body["providers"]}
    assert set(providers) == {"local_openai", "ollama_text"}
    oai = providers["local_openai"]
    assert oai["available"] is True
    assert oai["default_model"] == "google/gemma-4-31b"
    assert {"id", "label", "is_default"} <= set(oai["models"][0])


def test_api_ai_models_unknown_project_404(client):
    http, _store, _dir = client
    response = http.get("/api/projects/nope/ai/models")
    assert response.status_code == 404


# ------------------------------------------------------- per-request model override


def _capture_post(payload: dict):
    calls: list[dict] = []

    def _post(*args, **kwargs):
        calls.append(kwargs)
        return _FakeResponse(payload)

    return calls, _post


def test_openai_model_override_reaches_request_body(scene, library, monkeypatch):
    # Discovery lists the requested model, so the override is honored verbatim.
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _FakeResponse({"data": [{"id": "custom-model"}]})
    )
    calls, post = _capture_post(_openai_payload(json.dumps(_valid_payload())))
    monkeypatch.setattr(httpx, "post", post)
    response = LocalOpenAIProvider().suggest(
        scene, library, [WINDOW_ID], model="custom-model"
    )
    assert response.provider == "local_openai"
    assert response.model == "custom-model"
    assert calls[0]["json"]["model"] == "custom-model"


def test_openai_unknown_model_falls_back_with_warning(scene, library, monkeypatch):
    # Discovery lists only the default; an unknown override triggers the
    # guardrail: fall back to the settings default and warn.
    settings = get_settings().ai
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _FakeResponse({"data": [{"id": settings.openai_model}]}),
    )
    calls, post = _capture_post(_openai_payload(json.dumps(_valid_payload())))
    monkeypatch.setattr(httpx, "post", post)
    response = LocalOpenAIProvider().suggest(
        scene, library, [WINDOW_ID], model="not-loaded"
    )
    assert calls[0]["json"]["model"] == settings.openai_model
    assert response.model == settings.openai_model
    assert any("requested model 'not-loaded' is not loaded" in w for w in response.warnings)


def test_openai_unknown_model_no_guardrail_when_discovery_empty(scene, library, monkeypatch):
    # Empty/unreachable discovery must NOT block the request: the override is
    # used as-is (the guardrail only fires on a non-empty list).
    def _raise(*a, **k):
        raise httpx.ConnectError("no")

    monkeypatch.setattr(httpx, "get", _raise)
    calls, post = _capture_post(_openai_payload(json.dumps(_valid_payload())))
    monkeypatch.setattr(httpx, "post", post)
    response = LocalOpenAIProvider().suggest(
        scene, library, [WINDOW_ID], model="whatever"
    )
    assert calls[0]["json"]["model"] == "whatever"
    assert not any("is not loaded" in w for w in response.warnings)


def test_ollama_model_override_reaches_request_body(scene, library, monkeypatch):
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: _FakeResponse({"models": [{"name": "llama3:70b"}]})
    )
    calls, post = _capture_post({"message": {"content": json.dumps(_valid_payload())}})
    monkeypatch.setattr(httpx, "post", post)
    response = OllamaTextProvider().suggest(
        scene, library, [WINDOW_ID], model="llama3:70b"
    )
    assert response.model == "llama3:70b"
    assert calls[0]["json"]["model"] == "llama3:70b"


def test_ollama_unknown_model_falls_back_with_warning(scene, library, monkeypatch):
    settings = get_settings().ai
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: _FakeResponse({"models": [{"name": settings.text_model}]}),
    )
    calls, post = _capture_post({"message": {"content": json.dumps(_valid_payload())}})
    monkeypatch.setattr(httpx, "post", post)
    response = OllamaTextProvider().suggest(
        scene, library, [WINDOW_ID], model="ghost:1b"
    )
    assert calls[0]["json"]["model"] == settings.text_model
    assert any("requested model 'ghost:1b' is not loaded" in w for w in response.warnings)


def test_suggest_materials_threads_model_and_records_source(
    tmp_path, scene, library, monkeypatch
):
    # End-to-end through suggest_materials + the API: model override honored and
    # logged as model_source="user".
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    store = deps.get_store()
    info = store.create_project("AI Test", project_id="ai_test")
    store.save_scene("ai_test", scene)

    def _get(url, *a, **k):
        if "/models" in url:
            return _FakeResponse({"data": [{"id": "custom-model"}]})
        raise httpx.ConnectError("ollama down")

    calls, post = _capture_post(_openai_payload(json.dumps(_valid_payload())))
    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(httpx, "post", post)

    http = TestClient(_make_app())
    response = http.post(
        "/api/projects/ai_test/ai/suggest-materials",
        json={"prim_ids": [WINDOW_ID], "provider": "local_openai", "model": "custom-model"},
    )
    assert response.status_code == 200
    assert response.json()["model"] == "custom-model"

    log = (Path(info.path) / "ai" / "suggestions.jsonl").read_text(encoding="utf-8")
    record = next(
        json.loads(line)
        for line in log.splitlines()
        if line.strip() and json.loads(line).get("event") == "suggested"
    )
    assert record["model"] == "custom-model"
    assert record["model_source"] == "user"
