"""Result store growth (F44): compact result files, a corrupt provenance log
kept aside instead of silently replaced, and opt-in automatic pruning
(SEAM_AUTO_PRUNE_KEEP)."""

from __future__ import annotations

import json
import logging

import pytest

from seam_studio.core import config

from .conftest import make_demo_scene

PID = "store_test"
MOCK = {"config": {"backend": "mock"}}


@pytest.fixture()
def client(api_client):
    from seam_studio.api import deps

    store = deps.get_store()
    store.create_project("Store", project_id=PID)
    store.save_scene(PID, make_demo_scene(PID))
    return api_client


def _solve(client) -> dict:
    resp = client.post(f"/api/projects/{PID}/simulate/paths", json=MOCK)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _project_dir():
    from seam_studio.api import deps

    return deps.get_store().resolve(PID)


def _refs(client) -> list[dict]:
    return client.get(f"/api/projects/{PID}/scene").json()["result_sets"]


def test_result_files_are_compact(client):
    body = _solve(client)
    path = _project_dir() / "results" / f"{body['result_id']}.json"
    text = path.read_text(encoding="utf-8")
    assert "\n" not in text and ": " not in text[:200]
    assert json.loads(text) == body
    ref = _refs(client)[-1]
    assert ref["size_bytes"] == path.stat().st_size
    # The scene and provenance stay human-readable.
    assert "\n" in (_project_dir() / "scene.seam.json").read_text(encoding="utf-8")
    assert "\n" in (_project_dir() / "provenance.json").read_text(encoding="utf-8")


def test_corrupt_provenance_is_kept_aside(client, caplog):
    prov = _project_dir() / "provenance.json"
    prov.write_bytes(b'{"created_at": "x", "events": [trunc')
    with caplog.at_level(logging.WARNING):
        body = _solve(client)
    backups = sorted(_project_dir().glob("provenance.corrupt-*.json"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == b'{"created_at": "x", "events": [trunc'
    data = json.loads(prov.read_text(encoding="utf-8"))
    recovered, simulated = data["events"]
    assert recovered["type"] == "provenance_recovered"
    assert recovered["corrupt_backup"] == backups[0].name and recovered["reason"]
    assert simulated["type"] == "simulate" and simulated["result_id"] == body["result_id"]
    assert any("could not be parsed" in r.getMessage() for r in caplog.records)

    # A second corrupt log in the same second gets its own backup name.
    prov.write_text("[]", encoding="utf-8")  # valid JSON, wrong shape
    _solve(client)
    assert len(list(_project_dir().glob("provenance.corrupt-*.json"))) == 2


@pytest.mark.parametrize(
    "raw",
    [
        # A Korean label re-saved as ANSI (cp949) by Notepad, and binary junk:
        # not UTF-8, which used to escape the recovery and 500 every solve.
        '{"created_at": "x", "events": [{"label": "기준"}]}'.encode("cp949"),
        b"\xff\xfe{",
    ],
)
def test_non_utf8_provenance_is_kept_aside(client, raw):
    prov = _project_dir() / "provenance.json"
    prov.write_bytes(raw)
    body = _solve(client)
    backups = list(_project_dir().glob("provenance.corrupt-*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == raw
    events = json.loads(prov.read_text(encoding="utf-8"))["events"]
    assert [e["type"] for e in events] == ["provenance_recovered", "simulate"]
    assert events[1]["result_id"] == body["result_id"]


def test_utf8_bom_provenance_is_read_not_replaced(client):
    prov = _project_dir() / "provenance.json"
    prov.write_bytes(b"\xef\xbb\xbf" + json.dumps(
        {"created_at": "x", "events": [{"type": "note", "label": "기준"}]}, ensure_ascii=False
    ).encode("utf-8"))
    _solve(client)
    assert not list(_project_dir().glob("provenance.corrupt-*.json"))
    events = json.loads(prov.read_text(encoding="utf-8"))["events"]
    assert [e["type"] for e in events] == ["note", "simulate"] and events[0]["label"] == "기준"


@pytest.fixture()
def prune_keep(monkeypatch):
    def set_keep(value):
        if value is None:
            monkeypatch.delenv("SEAM_AUTO_PRUNE_KEEP", raising=False)
        else:
            monkeypatch.setenv("SEAM_AUTO_PRUNE_KEEP", value)
        config.get_settings.cache_clear()

    yield set_keep
    monkeypatch.delenv("SEAM_AUTO_PRUNE_KEEP", raising=False)
    config.get_settings.cache_clear()


def test_auto_prune_keeps_the_newest_unlabeled_runs(client, prune_keep):
    prune_keep("2")
    first = _solve(client)["result_id"]
    labeled = client.patch(
        f"/api/projects/{PID}/results/{first}/label", json={"label": "baseline"}
    )
    assert labeled.status_code == 200
    ids = [_solve(client)["result_id"] for _ in range(4)]
    refs = _refs(client)
    assert [r["result_id"] for r in refs] == [first, *ids[-2:]]
    results = _project_dir() / "results"
    assert sorted(p.stem for p in results.glob("*.json")) == sorted([first, *ids[-2:]])
    prov = json.loads((_project_dir() / "provenance.json").read_text(encoding="utf-8"))
    pruned = [e for e in prov["events"] if e["type"] == "results_pruned"]
    assert [e["removed_count"] for e in pruned] == [1, 1]
    assert all(e["auto"] is True and e["kind"] == "paths" for e in pruned)
    # Other kinds are untouched by a paths solve.
    radio = client.post(f"/api/projects/{PID}/simulate/radio-map", json=MOCK)
    assert radio.status_code == 200
    assert len(_refs(client)) == 4


@pytest.mark.parametrize("value", [None, "0", "-3", "two", ""])
def test_auto_prune_is_off_unless_a_positive_integer(client, prune_keep, value, caplog):
    with caplog.at_level(logging.WARNING):
        prune_keep(value)
        assert config.get_settings().auto_prune_keep is None
    if value not in (None, ""):
        assert any("SEAM_AUTO_PRUNE_KEEP" in r.getMessage() for r in caplog.records)
    for _ in range(4):
        _solve(client)
    assert len(_refs(client)) == 4
