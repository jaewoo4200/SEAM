"""Tests for AI suggestion providers, the response parser, and the /ai API.

No network: every httpx call is monkeypatched. Only this module's own tests
run here (sibling test files may not exist yet).
"""

import json
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import ai as ai_api
from app.api import deps
from app.core.config import get_settings
from app.schemas.ai import SuggestMaterialsRequest
from app.schemas.scene import MeshRef, Prim, Scene, VisualBinding
from app.services import ai_provider
from app.services.ai_provider import (
    AIParseError,
    LocalOpenAIProvider,
    OllamaTextProvider,
    RuleBasedProvider,
    _extract_json_object,
    get_provider_statuses,
    parse_ai_response,
    suggest_materials,
)
from app.services.project_store import load_default_library

WINDOW_ID = "/buildings/b01/window_12"
WALLS_ID = "/buildings/b02/walls"
BLOB_ID = "/misc/blob_01"


@pytest.fixture(autouse=True)
def _fresh_caches():
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    ai_provider._probe_cache.clear()
    yield
    get_settings.cache_clear()
    deps.get_store.cache_clear()
    ai_provider._probe_cache.clear()


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
                    base_color_texture="visual/textures/blue_glass.png",
                ),
            ),
            Prim(
                id=WALLS_ID,
                name="building_02_walls",
                semantic_tags=["building"],
                mesh_ref=MeshRef(mesh_name="building_02"),
            ),
            Prim(
                id=BLOB_ID,
                name="blob_01",
                mesh_ref=MeshRef(mesh_name="blob"),
            ),
        ],
    )


# ------------------------------------------------------------ rule provider


def test_rule_provider_window_glass(scene, library):
    response = RuleBasedProvider().suggest(scene, library, [WINDOW_ID])
    assert response.provider == "rule_based"
    assert len(response.suggestions) == 1
    suggestion = response.suggestions[0]
    assert suggestion.recommended_rf_material_id == "itu_glass"
    assert suggestion.confidence >= 0.8
    evidence_text = " ".join(suggestion.evidence)
    assert "window" in evidence_text
    assert "glass" in evidence_text
    assert suggestion.needs_user_confirmation is True


def test_rule_provider_wall_concrete(scene, library):
    response = RuleBasedProvider().suggest(scene, library, [WALLS_ID])
    assert len(response.suggestions) == 1
    suggestion = response.suggestions[0]
    assert suggestion.recommended_rf_material_id == "itu_concrete"
    assert suggestion.confidence >= 0.8  # prim name hit


def test_rule_provider_unmatched_prim_gets_unknown_rf(scene, library):
    response = RuleBasedProvider().suggest(scene, library, [BLOB_ID])
    assert len(response.suggestions) == 1
    suggestion = response.suggestions[0]
    assert suggestion.recommended_rf_material_id == "unknown_rf"
    assert suggestion.confidence <= 0.3
    assert suggestion.evidence == ["no keyword evidence"]


def test_rule_provider_missing_prim_warns(scene, library):
    response = RuleBasedProvider().suggest(scene, library, ["/nope/missing"])
    assert response.suggestions == []
    assert any("not found" in w for w in response.warnings)


# ------------------------------------------------------------------ parser


def _valid_payload() -> dict:
    return {
        "suggestions": [
            {
                "prim_id": WINDOW_ID,
                "recommended_rf_material_id": "itu_glass",
                "confidence": 0.86,
                "evidence": ["prim name contains 'window'"],
                "alternatives": [{"rf_material_id": "metal", "confidence": 0.11}],
                "needs_user_confirmation": True,
            }
        ]
    }


def test_parse_valid_payload_roundtrips(scene, library):
    suggestions, warnings = parse_ai_response(
        json.dumps(_valid_payload()), scene, library
    )
    assert warnings == []
    assert len(suggestions) == 1
    suggestion = suggestions[0]
    assert suggestion.prim_id == WINDOW_ID
    assert suggestion.recommended_rf_material_id == "itu_glass"
    assert suggestion.confidence == 0.86
    assert suggestion.evidence == ["prim name contains 'window'"]
    assert suggestion.alternatives[0].rf_material_id == "metal"
    assert suggestion.alternatives[0].confidence == 0.11
    assert suggestion.needs_user_confirmation is True


def test_parse_clamps_confidence(scene, library):
    payload = _valid_payload()
    payload["suggestions"][0]["confidence"] = 1.7
    payload["suggestions"][0]["alternatives"][0]["confidence"] = -0.4
    suggestions, _ = parse_ai_response(json.dumps(payload), scene, library)
    assert suggestions[0].confidence == 1.0
    assert suggestions[0].alternatives[0].confidence == 0.0


def test_parse_drops_unknown_material_with_warning(scene, library):
    payload = _valid_payload()
    payload["suggestions"][0]["recommended_rf_material_id"] = "vibranium"
    suggestions, warnings = parse_ai_response(json.dumps(payload), scene, library)
    assert suggestions == []
    assert any("vibranium" in w for w in warnings)


def test_parse_drops_unknown_prim_with_warning(scene, library):
    payload = _valid_payload()
    payload["suggestions"][0]["prim_id"] = "/not/in/scene"
    suggestions, warnings = parse_ai_response(json.dumps(payload), scene, library)
    assert suggestions == []
    assert any("/not/in/scene" in w for w in warnings)


def test_parse_drops_malformed_item_with_warning(scene, library):
    payload = _valid_payload()
    payload["suggestions"].append({"prim_id": WALLS_ID})  # missing required fields
    suggestions, warnings = parse_ai_response(json.dumps(payload), scene, library)
    assert len(suggestions) == 1
    assert any("malformed" in w for w in warnings)


def test_parse_garbage_raises(scene, library):
    with pytest.raises(AIParseError):
        parse_ai_response("sure! here are my suggestions:", scene, library)
    with pytest.raises(AIParseError):
        parse_ai_response('["not", "an", "object"]', scene, library)


def test_parse_tolerates_fenced_code_block(scene, library):
    raw = "```json\n" + json.dumps(_valid_payload()) + "\n```"
    suggestions, warnings = parse_ai_response(raw, scene, library)
    assert warnings == []
    assert suggestions[0].recommended_rf_material_id == "itu_glass"


# --------------------------------------------------------- ollama fallback


def test_ollama_provider_falls_back_to_rules_on_connect_error(
    scene, library, monkeypatch
):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _raise)
    response = OllamaTextProvider().suggest(scene, library, [WINDOW_ID])
    assert response.provider == "rule_based"
    assert response.warnings
    assert response.warnings[0].startswith("ollama_text failed:")
    assert "fell back to rule_based" in response.warnings[0]
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"


def test_get_provider_statuses_never_raises_with_unreachable_ollama(monkeypatch):
    monkeypatch.setenv("SIONNATWIN_OLLAMA_URL", "http://127.0.0.1:9")
    get_settings.cache_clear()

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    statuses = get_provider_statuses()
    by_name = {s.name: s for s in statuses}
    assert by_name["rule_based"].available is True
    assert by_name["ollama_text"].available is False
    assert "http://127.0.0.1:9" in by_name["ollama_text"].detail


def test_suggest_materials_unknown_provider_raises_value_error(scene, library):
    with pytest.raises(ValueError):
        suggest_materials(
            scene, library, SuggestMaterialsRequest(provider="skynet")
        )


def test_suggest_materials_defaults_to_unassigned_mesh_prims(scene, library):
    request = SuggestMaterialsRequest(provider="rule_based")
    response = suggest_materials(scene, library, request)
    assert {s.prim_id for s in response.suggestions} == {WINDOW_ID, WALLS_ID, BLOB_ID}
    assert response.prompt_version == "v1"


# ---------------------------------------------- reasoning-preamble extraction


def test_extract_json_object_strips_reasoning_preamble():
    text = (
        "Let me think about this. The window is glass, so itu_glass fits.\n"
        '{"suggestions": [{"prim_id": "x", "recommended_rf_material_id": "itu_glass"}]}'
    )
    block = _extract_json_object(text)
    assert block is not None
    assert json.loads(block)["suggestions"][0]["recommended_rf_material_id"] == "itu_glass"


def test_extract_json_object_ignores_braces_in_strings():
    # A brace inside a JSON string value must not confuse depth tracking.
    text = 'prefix {"note": "a } brace", "n": 1} suffix'
    block = _extract_json_object(text)
    assert json.loads(block) == {"note": "a } brace", "n": 1}


def test_extract_json_object_none_without_brace():
    assert _extract_json_object("no json here at all") is None


def test_parse_tolerates_reasoning_preamble(scene, library):
    raw = (
        "Reasoning: the prim name and texture both say glass, so I recommend "
        "itu_glass with high confidence.\n\n"
        + json.dumps(_valid_payload())
    )
    suggestions, warnings = parse_ai_response(raw, scene, library)
    assert warnings == []
    assert suggestions[0].recommended_rf_material_id == "itu_glass"


# --------------------------------------------------------- local_openai (LM Studio)


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _openai_payload(content: str, reasoning: str = "") -> dict:
    return {
        "choices": [
            {"message": {"content": content, "reasoning_content": reasoning}}
        ]
    }


def test_local_openai_unavailable_when_probe_fails(monkeypatch):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    assert LocalOpenAIProvider().is_available() is False


def test_local_openai_available_when_probe_succeeds(monkeypatch):
    monkeypatch.setattr(httpx, "get", lambda *a, **k: _FakeResponse({"data": []}))
    # _FakeResponse.raise_for_status is a no-op, so the probe reads as reachable.
    assert LocalOpenAIProvider().is_available() is True


def test_local_openai_recommends_itu_glass_from_json_content(scene, library, monkeypatch):
    payload = _openai_payload(json.dumps(_valid_payload()))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(payload))
    response = LocalOpenAIProvider().suggest(scene, library, [WINDOW_ID])
    assert response.provider == "local_openai"
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"


def test_local_openai_extracts_json_after_reasoning_preamble(scene, library, monkeypatch):
    content = (
        "The prim is named window_12 and its visual material is blue_glass_pbr, "
        "so glass is the right RF material.\n"
        + json.dumps(_valid_payload())
    )
    monkeypatch.setattr(
        httpx, "post", lambda *a, **k: _FakeResponse(_openai_payload(content))
    )
    response = LocalOpenAIProvider().suggest(scene, library, [WINDOW_ID])
    assert response.provider == "local_openai"
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"


def test_local_openai_reads_reasoning_content_when_content_empty(scene, library, monkeypatch):
    # Some reasoning servers put the whole answer (JSON included) in
    # reasoning_content and leave content empty.
    payload = _openai_payload("", reasoning=json.dumps(_valid_payload()))
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(payload))
    response = LocalOpenAIProvider().suggest(scene, library, [WINDOW_ID])
    assert response.provider == "local_openai"
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"


def test_local_openai_garbage_falls_back_to_rules(scene, library, monkeypatch):
    payload = _openai_payload("I cannot help with that request, sorry.")
    monkeypatch.setattr(httpx, "post", lambda *a, **k: _FakeResponse(payload))
    response = LocalOpenAIProvider().suggest(scene, library, [WINDOW_ID])
    assert response.provider == "rule_based"
    assert response.warnings[0].startswith("local_openai failed:")
    assert "fell back to rule_based" in response.warnings[0]
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"


def test_local_openai_connect_error_falls_back_to_rules(scene, library, monkeypatch):
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _raise)
    response = LocalOpenAIProvider().suggest(scene, library, [WINDOW_ID])
    assert response.provider == "rule_based"
    assert response.warnings[0].startswith("local_openai failed:")


def test_get_provider_statuses_includes_local_openai(monkeypatch):
    monkeypatch.setenv("SIONNATWIN_OPENAI_URL", "http://127.0.0.1:9/v1")
    monkeypatch.setenv("SIONNATWIN_OPENAI_MODEL", "google/gemma-4-31b")
    get_settings.cache_clear()

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    by_name = {s.name: s for s in get_provider_statuses()}
    assert "local_openai" in by_name
    assert by_name["local_openai"].available is False
    assert by_name["local_openai"].model == "google/gemma-4-31b"
    assert "google/gemma-4-31b" in by_name["local_openai"].detail


def test_local_openai_prefers_when_available(scene, library, monkeypatch):
    # With the OpenAI probe up and Ollama down, the default chain must pick
    # local_openai over ollama_text.
    def _get(url, *args, **kwargs):
        if "/models" in url:
            return _FakeResponse({"data": []})  # OpenAI probe up
        raise httpx.ConnectError("ollama down")  # Ollama /api/tags down

    monkeypatch.setattr(httpx, "get", _get)
    monkeypatch.setattr(
        httpx, "post",
        lambda *a, **k: _FakeResponse(_openai_payload(json.dumps(_valid_payload()))),
    )
    request = SuggestMaterialsRequest(prim_ids=[WINDOW_ID])
    response = suggest_materials(scene, library, request)
    assert response.provider == "local_openai"


# ------------------------------------------------------------------- API


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


def _read_log(project_dir: Path) -> list[dict]:
    log_file = project_dir / "ai" / "suggestions.jsonl"
    assert log_file.is_file()
    return [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def test_api_suggest_materials_writes_jsonl(client):
    http, _store, project_dir = client
    response = http.post(
        "/api/projects/ai_test/ai/suggest-materials",
        json={"prim_ids": [WINDOW_ID], "provider": "rule_based"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "rule_based"
    assert body["suggestions"][0]["recommended_rf_material_id"] == "itu_glass"

    records = _read_log(project_dir)
    suggested = [r for r in records if r["event"] == "suggested"]
    assert len(suggested) == 1
    assert suggested[0]["provider"] == "rule_based"
    assert suggested[0]["prompt_version"] == "v1"
    assert suggested[0]["input_prim_ids"] == [WINDOW_ID]
    assert suggested[0]["suggestions"][0]["prim_id"] == WINDOW_ID


def test_api_apply_suggestions_approve_and_reject(client):
    http, store, project_dir = client
    suggest = http.post(
        "/api/projects/ai_test/ai/suggest-materials",
        json={"prim_ids": [WINDOW_ID], "provider": "rule_based"},
    ).json()

    response = http.post(
        "/api/projects/ai_test/ai/apply-suggestions",
        json={
            "decisions": [
                {"prim_id": WINDOW_ID, "action": "approve"},
                {"prim_id": WALLS_ID, "action": "reject"},
            ],
            "suggestions": suggest["suggestions"],
            "provider": "rule_based",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["updated_prim_ids"] == [WINDOW_ID]

    saved = store.load_scene("ai_test")
    window = saved.prim_by_id(WINDOW_ID)
    assert window.rf.material_id == "itu_glass"
    assert window.rf.assignment_status == "user_confirmed"
    assert window.rf.assignment_sources == ["ai:rule_based", "user"]
    assert window.rf.confidence == pytest.approx(0.9)

    walls = saved.prim_by_id(WALLS_ID)
    assert walls.rf.material_id is None
    assert walls.rf.assignment_status == "unassigned"

    decisions = [r for r in _read_log(project_dir) if r["event"] == "decision"]
    assert {d["action"] for d in decisions} == {"approve", "reject"}
    approve = next(d for d in decisions if d["action"] == "approve")
    assert approve["prim_id"] == WINDOW_ID
    assert approve["final_rf_material_id"] == "itu_glass"
    reject = next(d for d in decisions if d["action"] == "reject")
    assert reject["final_rf_material_id"] is None

    provenance = json.loads(
        (project_dir / "provenance.json").read_text(encoding="utf-8")
    )
    ai_events = [e for e in provenance["events"] if e.get("type") == "ai_apply"]
    assert ai_events[-1]["approved"] == 1
    assert ai_events[-1]["rejected"] == 1
    assert ai_events[-1]["edited"] == 0


def test_api_apply_suggestions_edit_requires_material(client):
    http, _store, _project_dir = client
    response = http.post(
        "/api/projects/ai_test/ai/apply-suggestions",
        json={
            "decisions": [{"prim_id": WINDOW_ID, "action": "edit"}],
            "provider": "rule_based",
        },
    )
    assert response.status_code == 400


def test_api_apply_suggestions_approve_without_suggestion_is_400(client):
    http, _store, _project_dir = client
    response = http.post(
        "/api/projects/ai_test/ai/apply-suggestions",
        json={
            "decisions": [{"prim_id": WINDOW_ID, "action": "approve"}],
            "provider": "rule_based",
        },
    )
    assert response.status_code == 400


def test_api_status_and_unknown_project_404(client, monkeypatch):
    http, _store, _project_dir = client

    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "get", _raise)
    response = http.get("/api/projects/ai_test/ai/status")
    assert response.status_code == 200
    names = {s["name"] for s in response.json()}
    assert {"rule_based", "ollama_text", "disabled"} <= names

    missing = http.post(
        "/api/projects/nope/ai/suggest-materials",
        json={"provider": "rule_based"},
    )
    assert missing.status_code == 404


# --------------------------------------------------- multimodal / vision (offline)

_TINY_PNG_DATA_URL = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class _CapturingPost:
    """Records the kwargs of the last httpx.post and returns a canned payload."""

    def __init__(self, payload: dict):
        self._payload = payload
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self._payload)


def test_local_openai_builds_multimodal_content_when_screenshot_present(
    scene, library, monkeypatch
):
    capture = _CapturingPost(_openai_payload(json.dumps(_valid_payload())))
    monkeypatch.setattr(httpx, "post", capture)
    response = LocalOpenAIProvider().suggest(
        scene, library, [WINDOW_ID], screenshot=_TINY_PNG_DATA_URL
    )
    assert response.provider == "local_openai"
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"

    # One POST built OpenAI multimodal content on the user message.
    assert len(capture.calls) == 1
    messages = capture.calls[0]["json"]["messages"]
    user_msg = messages[-1]
    assert user_msg["role"] == "user"
    content = user_msg["content"]
    assert isinstance(content, list)
    kinds = [part["type"] for part in content]
    assert kinds == ["text", "image_url"]
    assert content[1]["image_url"]["url"] == _TINY_PNG_DATA_URL
    # The image-is-evidence note is threaded into the text prompt.
    assert "EVIDENCE only" in content[0]["text"]
    assert "current 3D viewport" in content[0]["text"]


def test_local_openai_plain_string_content_without_screenshot(
    scene, library, monkeypatch
):
    capture = _CapturingPost(_openai_payload(json.dumps(_valid_payload())))
    monkeypatch.setattr(httpx, "post", capture)
    LocalOpenAIProvider().suggest(scene, library, [WINDOW_ID])
    messages = capture.calls[0]["json"]["messages"]
    # No screenshot -> plain string content, exactly like before.
    assert isinstance(messages[-1]["content"], str)
    assert "current 3D viewport" not in messages[-1]["content"]


def test_local_openai_degrades_to_text_when_image_rejected(scene, library, monkeypatch):
    good = _openai_payload(json.dumps(_valid_payload()))
    calls: list[dict] = []

    def _post(*args, **kwargs):
        calls.append(kwargs)
        # First (multimodal) call rejected as HTTP 400; text-only retry succeeds.
        content = kwargs["json"]["messages"][-1]["content"]
        if isinstance(content, list):
            request = httpx.Request("POST", "http://x/chat/completions")
            resp = httpx.Response(400, request=request, text="model does not support images")
            raise httpx.HTTPStatusError("400", request=request, response=resp)
        return _FakeResponse(good)

    monkeypatch.setattr(httpx, "post", _post)
    response = LocalOpenAIProvider().suggest(
        scene, library, [WINDOW_ID], screenshot=_TINY_PNG_DATA_URL
    )
    # Degraded, not fallen back to rules: still local_openai with a real answer.
    assert response.provider == "local_openai"
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"
    assert any("vision input rejected by" in w for w in response.warnings)
    assert any("used text only" in w for w in response.warnings)
    # Two calls: the rejected multimodal attempt, then the text-only retry.
    assert len(calls) == 2
    assert isinstance(calls[0]["json"]["messages"][-1]["content"], list)
    assert isinstance(calls[1]["json"]["messages"][-1]["content"], str)


def test_local_openai_non_vision_error_still_falls_back_to_rules(
    scene, library, monkeypatch
):
    # A connect error (not an image rejection) must NOT trigger the text retry;
    # it falls all the way back to the rule-based provider.
    def _raise(*args, **kwargs):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(httpx, "post", _raise)
    response = LocalOpenAIProvider().suggest(
        scene, library, [WINDOW_ID], screenshot=_TINY_PNG_DATA_URL
    )
    assert response.provider == "rule_based"
    assert response.warnings[0].startswith("local_openai failed:")


def test_ollama_provider_sends_images_and_vision_model(scene, library, monkeypatch):
    capture = _CapturingPost({"message": {"content": json.dumps(_valid_payload())}})
    monkeypatch.setattr(httpx, "post", capture)
    settings = get_settings().ai
    response = OllamaTextProvider().suggest(
        scene, library, [WINDOW_ID], screenshot=_TINY_PNG_DATA_URL
    )
    assert response.provider == "ollama_text"
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"
    body = capture.calls[0]["json"]
    # Vision model selected, base64 image attached WITHOUT the data: prefix.
    assert body["model"] == settings.vision_model
    user_msg = body["messages"][-1]
    assert "images" in user_msg
    assert len(user_msg["images"]) == 1
    assert not user_msg["images"][0].startswith("data:")
    assert "base64," not in user_msg["images"][0]
    assert "current 3D viewport" in user_msg["content"]


def test_ollama_provider_text_model_without_screenshot(scene, library, monkeypatch):
    capture = _CapturingPost({"message": {"content": json.dumps(_valid_payload())}})
    monkeypatch.setattr(httpx, "post", capture)
    settings = get_settings().ai
    OllamaTextProvider().suggest(scene, library, [WINDOW_ID])
    body = capture.calls[0]["json"]
    assert body["model"] == settings.text_model
    assert "images" not in body["messages"][-1]


def test_rule_based_ignores_screenshot(scene, library):
    # Backward-compatible signature: RuleBased accepts but ignores the image.
    response = RuleBasedProvider().suggest(
        scene, library, [WINDOW_ID], screenshot=_TINY_PNG_DATA_URL
    )
    assert response.provider == "rule_based"
    assert response.suggestions[0].recommended_rf_material_id == "itu_glass"


def test_api_suggest_records_screenshot_attached_flag(client):
    http, _store, project_dir = client
    response = http.post(
        "/api/projects/ai_test/ai/suggest-materials",
        json={
            "prim_ids": [WINDOW_ID],
            "provider": "rule_based",
            "screenshot_data_url": _TINY_PNG_DATA_URL,
        },
    )
    assert response.status_code == 200
    records = _read_log(project_dir)
    suggested = [r for r in records if r["event"] == "suggested"]
    assert len(suggested) == 1
    # Provenance flag present and true; the image itself is never stored.
    assert suggested[0]["screenshot_attached"] is True
    assert _TINY_PNG_DATA_URL not in json.dumps(suggested[0])


def test_api_suggest_screenshot_attached_false_without_image(client):
    http, _store, project_dir = client
    http.post(
        "/api/projects/ai_test/ai/suggest-materials",
        json={"prim_ids": [WINDOW_ID], "provider": "rule_based"},
    )
    suggested = [r for r in _read_log(project_dir) if r["event"] == "suggested"]
    assert suggested[0]["screenshot_attached"] is False
