"""Concurrency regression for provenance.json appends.

``append_provenance`` does a read-modify-write of provenance.json. FastAPI
sync endpoints run in a threadpool, so several requests can append at once;
without a per-project lock the read-modify-write races and events are lost to
a classic lost update (and ``_atomic_write_text`` writers would collide on a
shared tmp path). This test hammers the store from many threads and asserts
every single event survives.
"""

import json
import threading
from pathlib import Path

from seam_studio.services.project_store import ProjectStore

THREADS = 8
EVENTS_PER_THREAD = 25
TOTAL = THREADS * EVENTS_PER_THREAD


def test_concurrent_append_provenance_loses_no_events(tmp_path: Path) -> None:
    root = tmp_path / "projects"
    root.mkdir()
    store = ProjectStore(roots=[root])
    info = store.create_project(name="Race Test", project_id="race_test")
    project_dir = Path(info.path)

    start = threading.Barrier(THREADS)

    def worker(worker_id: int) -> None:
        # Line every thread up at the same instant to maximize contention.
        start.wait()
        for i in range(EVENTS_PER_THREAD):
            store.append_provenance(
                "race_test",
                {"type": "race", "worker": worker_id, "seq": i},
            )

    threads = [
        threading.Thread(target=worker, args=(w,)) for w in range(THREADS)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    data = json.loads((project_dir / "provenance.json").read_text(encoding="utf-8"))
    events = [e for e in data.get("events", []) if e.get("type") == "race"]

    # Exactly every appended event survived - no lost updates.
    assert len(events) == TOTAL, f"expected {TOTAL} events, got {len(events)}"

    # And every (worker, seq) pair is present exactly once.
    pairs = {(e["worker"], e["seq"]) for e in events}
    assert pairs == {
        (w, i) for w in range(THREADS) for i in range(EVENTS_PER_THREAD)
    }
    assert len(pairs) == TOTAL
