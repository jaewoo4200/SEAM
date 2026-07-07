"""Tests for the event hub, the events WebSocket, and AODT parquet import.

The AODT round-trip test SKIPS cleanly when pyarrow is absent; when present it
writes a tiny parquet with pyarrow and round-trips it back through the import
service and the /results/import-aodt route.
"""

import asyncio
import threading

import pytest

from app.services import aodt_import
from app.services.events import EventHub, get_hub, publish_event

try:  # pyarrow is optional; the round-trip tests skip without it.
    import pyarrow as pa  # noqa: F401
    import pyarrow.parquet as pq  # noqa: F401

    HAS_PYARROW = True
except ImportError:  # pragma: no cover - env-dependent
    HAS_PYARROW = False


# --------------------------------------------------------------- event hub


def test_hub_subscribe_publish_roundtrip():
    async def scenario():
        hub = EventHub()
        q = hub.subscribe("p1")
        assert hub.subscriber_count("p1") == 1
        hub.publish("p1", {"type": "simulation_started", "kind": "paths"})
        evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt == {"type": "simulation_started", "kind": "paths"}
        hub.unsubscribe("p1", q)
        assert hub.subscriber_count("p1") == 0

    asyncio.run(scenario())


def test_hub_is_project_scoped():
    async def scenario():
        hub = EventHub()
        qa = hub.subscribe("a")
        qb = hub.subscribe("b")
        hub.publish("a", {"type": "compile_started"})
        evt = await asyncio.wait_for(qa.get(), timeout=1.0)
        assert evt["type"] == "compile_started"
        # b never received a's event.
        assert qb.empty()

    asyncio.run(scenario())


def test_hub_publish_from_worker_thread():
    """publish() called from another thread (the threadpool case) is delivered
    to the loop-owned queue via call_soon_threadsafe."""

    async def scenario():
        hub = EventHub()
        q = hub.subscribe("p")

        def worker():
            hub.publish("p", {"type": "simulation_finished", "result_id": "x"})

        t = threading.Thread(target=worker)
        t.start()
        t.join()
        evt = await asyncio.wait_for(q.get(), timeout=1.0)
        assert evt["result_id"] == "x"

    asyncio.run(scenario())


def test_hub_drops_events_when_queue_full():
    async def scenario():
        hub = EventHub()
        q = hub.subscribe("p")
        # Fill past the bounded queue; extras are dropped, never raised.
        for i in range(300):
            hub.publish("p", {"n": i})
        assert q.qsize() <= 100
        # The queue still yields a valid earliest event.
        first = await asyncio.wait_for(q.get(), timeout=1.0)
        assert first["n"] == 0

    asyncio.run(scenario())


def test_publish_event_never_raises_and_uses_singleton():
    # No subscribers: publish_event is a no-op that must not raise.
    publish_event("nobody", {"type": "simulation_started", "kind": "paths"})
    assert get_hub() is get_hub()


# ------------------------------------------------------- events websocket


def test_events_websocket_streams_simulation_events(api_client):
    """A live WS receives the hello frame, then simulation_started/finished
    around a paths solve driven through the real API."""
    api_client.post(
        "/api/projects", json={"name": "WS Proj", "project_id": "ws_proj"}
    )
    with api_client.websocket_connect("/ws/projects/ws_proj/events") as ws:
        hello = ws.receive_json()
        assert hello == {"type": "connected", "project_id": "ws_proj"}
        # Drive a solve; the sync route publishes started + finished.
        resp = api_client.post("/api/projects/ws_proj/simulate/paths")
        assert resp.status_code == 200, resp.text
        types = []
        for _ in range(2):
            types.append(ws.receive_json()["type"])
        assert "simulation_started" in types
        assert "simulation_finished" in types


# ----------------------------------------------------------- AODT import


def test_import_unavailable_when_pyarrow_missing(monkeypatch, tmp_path):
    """When pyarrow is absent, the reader raises the typed unavailable error
    regardless of on-disk files (simulated by forcing the import to fail)."""

    def boom():
        raise aodt_import.AodtImportUnavailable("pyarrow missing (forced)")

    monkeypatch.setattr(aodt_import, "_require_pyarrow", boom)
    (tmp_path / "paths.parquet").write_bytes(b"not really parquet")
    with pytest.raises(aodt_import.AodtImportUnavailable):
        aodt_import.import_aodt_results(tmp_path, "paths")


def test_require_pyarrow_guard_actionable_message(monkeypatch):
    """The real _require_pyarrow guard (import forced to fail) raises the typed
    unavailable error with an ACTIONABLE message naming the [results] extra -
    not a bare ImportError. Monkeypatches the import itself, not the guard."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pyarrow.parquet" or name.startswith("pyarrow"):
            raise ImportError("No module named 'pyarrow'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(aodt_import.AodtImportUnavailable) as excinfo:
        aodt_import._require_pyarrow()
    msg = str(excinfo.value)
    assert "seam-backend[results]" in msg
    assert "pyarrow" in msg


def test_import_bad_kind_and_missing_dir():
    with pytest.raises(aodt_import.AodtImportError):
        aodt_import.import_aodt_results("/no/such/dir/exists", "paths")


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
def test_aodt_paths_roundtrip(tmp_path):
    # A tiny paths.parquet with a points polyline + AODT-ish column names.
    table = pa.table(
        {
            "ray_id": ["r0", "r1"],
            "ru_id": ["tx_a", "tx_a"],
            "ue_id": ["ue_0", "ue_0"],
            "type": ["los", "reflection"],
            "power_dB": [-60.0, -75.5],
            "cir_delay": [3.3e-8, 5.0e-8],  # seconds -> should scale to ns
            "points": [
                [[0.0, 0.0, 10.0], [10.0, 0.0, 1.5]],
                [[0.0, 0.0, 10.0], [5.0, 5.0, 3.0], [10.0, 0.0, 1.5]],
            ],
        }
    )
    pq.write_table(table, tmp_path / "paths.parquet")

    result = aodt_import.import_aodt_results(tmp_path, "paths")
    assert result.backend == "aodt_import"
    assert len(result.paths) == 2
    p0 = result.paths[0]
    assert p0.tx_id == "tx_a" and p0.rx_id == "ue_0"
    assert p0.path_type == "los"
    assert p0.power_dbm == pytest.approx(-60.0)
    # 3.3e-8 s -> 33.0 ns.
    assert p0.delay_ns == pytest.approx(33.0, rel=1e-6)
    assert len(result.paths[1].vertices) == 3


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
def test_aodt_paths_straight_line_fallback(tmp_path):
    # No points column: reconstruct a straight tx->rx line from positions.
    table = pa.table(
        {
            "path_id": ["p0"],
            "tx_id": ["t"],
            "rx_id": ["r"],
            "power_dbm": [-70.0],
            "delay_ns": [12.0],  # already ns
            "tx_position": [[0.0, 0.0, 5.0]],
            "rx_position": [[20.0, 0.0, 1.5]],
        }
    )
    pq.write_table(table, tmp_path / "paths.parquet")
    result = aodt_import.import_aodt_results(tmp_path, "paths")
    assert len(result.paths) == 1
    assert result.paths[0].vertices == [[0.0, 0.0, 5.0], [20.0, 0.0, 1.5]]
    assert result.paths[0].delay_ns == pytest.approx(12.0)


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
def test_aodt_radio_map_per_cell_roundtrip(tmp_path):
    # 3x2 grid of per-cell rows at 2 m spacing; grid + cell size are inferred.
    xs = [0.0, 2.0, 4.0]
    ys = [0.0, 2.0]
    rows_x, rows_y, vals, tx = [], [], [], []
    for iy, y in enumerate(ys):
        for ix, x in enumerate(xs):
            rows_x.append(x)
            rows_y.append(y)
            vals.append(-50.0 - ix - 10 * iy)
            tx.append("tx_a")
    table = pa.table(
        {"x": rows_x, "y": rows_y, "path_gain_db": vals, "tx_id": tx}
    )
    pq.write_table(table, tmp_path / "radio_map.parquet")

    result = aodt_import.import_aodt_results(tmp_path, "radio_map")
    assert result.metric == "path_gain_db"
    assert result.grid.cell_size_m == pytest.approx(2.0)
    assert result.grid.nx == 3 and result.grid.ny == 2
    assert result.grid.origin[0] == pytest.approx(0.0)
    assert result.tx_id == "tx_a"
    # Row-major [ny][nx]; the (0,0) cell is the strongest.
    assert result.values[0][0] == pytest.approx(-50.0)
    assert result.values[1][2] == pytest.approx(-50.0 - 2 - 10)


@pytest.mark.skipif(not HAS_PYARROW, reason="pyarrow not installed")
def test_import_aodt_route_persists_result(api_client, tmp_path):
    api_client.post(
        "/api/projects", json={"name": "AODT Proj", "project_id": "aodt_proj"}
    )
    table = pa.table(
        {
            "path_id": ["p0"],
            "tx_id": ["t"],
            "rx_id": ["r"],
            "power_dbm": [-70.0],
            "delay_ns": [12.0],
            "points": [[[0.0, 0.0, 5.0], [20.0, 0.0, 1.5]]],
        }
    )
    pq.write_table(table, tmp_path / "paths.parquet")

    resp = api_client.post(
        "/api/projects/aodt_proj/results/import-aodt",
        json={"source_dir": str(tmp_path), "kinds": ["paths"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["imported"]) == 1
    assert body["imported"][0]["kind"] == "paths"
    rid = body["imported"][0]["result_id"]
    assert rid.startswith("aodt_import_paths_")

    # The imported result is retrievable via the normal results route.
    got = api_client.get(
        f"/api/projects/aodt_proj/results/paths?result_id={rid}"
    )
    assert got.status_code == 200, got.text
    assert got.json()["backend"] == "aodt_import"
    assert len(got.json()["paths"]) == 1


def test_import_aodt_route_409_without_pyarrow(api_client, tmp_path, monkeypatch):
    """The route answers 409 (not 500) when pyarrow is missing."""
    api_client.post(
        "/api/projects", json={"name": "No PA", "project_id": "nopa_proj"}
    )

    def boom():
        raise aodt_import.AodtImportUnavailable("pyarrow missing (forced)")

    monkeypatch.setattr(aodt_import, "_require_pyarrow", boom)
    (tmp_path / "paths.parquet").write_bytes(b"x")
    resp = api_client.post(
        "/api/projects/nopa_proj/results/import-aodt",
        json={"source_dir": str(tmp_path), "kinds": ["paths"]},
    )
    assert resp.status_code == 409, resp.text
