"""Compute-engine registry, worker protocol, and paths dispatch tests.

The subprocess protocol is exercised with a FAKE worker running under
sys.executable, so these tests need no alternate sionna-rt venv installed.
"""

import json
import sys
import textwrap
from pathlib import Path

import pytest

from seam_studio.schemas.engines import EngineInfo
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import engines as reg
from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend

from .conftest import make_demo_scene


# ------------------------------------------------------------------ registry


def test_builtin_engine_always_listed_first():
    engines = reg.list_engines()
    assert engines[0].id == "builtin"
    assert engines[0].kind == "builtin"


def test_manifest_missing_interpreter_marks_unavailable(tmp_path, monkeypatch):
    manifest = tmp_path / "engines.json"
    manifest.write_text(json.dumps({
        "engines": [{"id": "ghost", "label": "Ghost", "python": str(tmp_path / "nope.exe")}]
    }), encoding="utf-8")
    monkeypatch.setattr(reg, "ENGINES_FILE", manifest)
    reg._probe_cache.clear()
    engines = {e.id: e for e in reg.list_engines()}
    assert "ghost" in engines
    assert engines["ghost"].available is False
    assert "not found" in engines["ghost"].detail


def test_malformed_manifest_entry_does_not_hide_others(tmp_path, monkeypatch):
    manifest = tmp_path / "engines.json"
    manifest.write_text(json.dumps({
        "engines": [{"label": "no id or python"},
                     {"id": "ok", "label": "OK", "python": str(tmp_path / "nope.exe")}]
    }), encoding="utf-8")
    monkeypatch.setattr(reg, "ENGINES_FILE", manifest)
    reg._probe_cache.clear()
    ids = [e.id for e in reg.list_engines()]
    assert "ok" in ids  # the bad entry didn't abort parsing


# ------------------------------------------------------- worker protocol

FAKE_WORKER = textwrap.dedent(
    """
    import json, sys
    job = json.load(open(sys.argv[1], encoding="utf-8"))
    tx, rx = job["txs"][0], job["rxs"][0]
    result = {
        "ok": True,
        "engine_version": "9.9-test",
        "warnings": ["fake worker ran"],
        "error": None,
        "paths": [{
            "path_id": "path_0001",
            "tx_id": tx["id"], "rx_id": rx["id"],
            "path_type": "los",
            "vertices": [tx["position"], rx["position"]],
            "power_dbm": -60.0, "delay_ns": 12.5, "phase_rad": 0.0,
            "interactions": [],
        }],
    }
    json.dump(result, open(sys.argv[2], "w", encoding="utf-8"))
    """
)


@pytest.fixture()
def fake_engine(tmp_path, monkeypatch) -> EngineInfo:
    workers = tmp_path / "workers"
    workers.mkdir()
    (workers / "sionna_rt_worker.py").write_text(FAKE_WORKER, encoding="utf-8")
    monkeypatch.setattr(reg, "WORKERS_DIR", workers)
    return EngineInfo(
        id="fake", label="Fake Engine", kind="subprocess", adapter="sionna_rt",
        python=sys.executable, available=True, version="9.9-test",
    )


def _paths_job() -> dict:
    return {
        "kind": "paths", "xml_path": "x", "manifest_path": None,
        "frequency_hz": 28e9, "max_depth": 3, "seed": 1, "num_samples": 10,
        "synthetic_array": True, "flags": {},
        "txs": [{"id": "tx_001", "position": [0, 0, 10], "power_dbm": 30.0}],
        "rxs": [{"id": "rx_001", "position": [25, 5, 1.5]}],
        "material_to_prims": {},
    }


def test_run_paths_job_round_trip(fake_engine):
    result = reg.run_paths_job(fake_engine, _paths_job(), timeout_s=60)
    assert result["ok"] is True
    assert result["engine_version"] == "9.9-test"
    assert result["paths"][0]["tx_id"] == "tx_001"


def test_run_paths_job_worker_failure_raises(tmp_path, monkeypatch, fake_engine):
    (Path(reg.WORKERS_DIR) / "sionna_rt_worker.py").write_text(
        "import sys; sys.exit(3)", encoding="utf-8"
    )
    with pytest.raises(reg.EngineError):
        reg.run_paths_job(fake_engine, _paths_job(), timeout_s=60)


# ------------------------------------------------------------- dispatch


def test_simulate_paths_dispatches_to_engine(tmp_path, monkeypatch, fake_engine, library):
    monkeypatch.setattr(reg, "get_engine", lambda engine_id, refresh=False: fake_engine)
    project = tmp_path / "proj"
    (project / "rf").mkdir(parents=True)
    # An existing XML skips compile-on-demand; the fake worker never reads it.
    (project / "rf" / "generated_scene.xml").write_text("<scene/>", encoding="utf-8")

    scene = make_demo_scene()
    config = SimulationConfig(backend="sionna", engine="fake", num_samples=10)
    result = SionnaBackend().simulate_paths(project, scene, library, config)

    assert result.metadata["engine"] == "fake"
    assert result.metadata["engine_version"] == "9.9-test"
    assert "fake worker ran" in result.warnings
    assert len(result.paths) == 1
    assert result.paths[0].tx_id == "tx_001"
    assert result.paths[0].path_type == "los"


def test_simulate_paths_unknown_engine_degrades_gracefully(tmp_path, library, monkeypatch):
    monkeypatch.setattr(reg, "get_engine", lambda engine_id, refresh=False: None)
    project = tmp_path / "proj"
    (project / "rf").mkdir(parents=True)
    (project / "rf" / "generated_scene.xml").write_text("<scene/>", encoding="utf-8")

    scene = make_demo_scene()
    config = SimulationConfig(backend="sionna", engine="does-not-exist", num_samples=10)
    result = SionnaBackend().simulate_paths(project, scene, library, config)
    assert result.paths == []
    assert any("does-not-exist" in w for w in result.warnings)


def test_engines_api_lists_builtin(api_client):
    resp = api_client.get("/api/engines")
    assert resp.status_code == 200
    ids = [e["id"] for e in resp.json()["engines"]]
    assert "builtin" in ids
