"""In-memory ephemeral live-state overlay.

``POST /live/state`` with ``persist=false`` pushes external real-world
positions that must still be visible to the UI's *Live sync* polling
(``GET /scene``) and to periodic re-solves, WITHOUT permanently writing them
into the scene file. Without this overlay a ``persist=false`` push mutated a
throwaway in-request Scene object, so the very next ``load_scene`` (disk read)
lost it and the viewer never followed — contradicting the documented
"the viewer follows in real time" behavior.

This module holds the latest non-persisted live positions per project and
applies them on top of the loaded scene. Any AUTHORITATIVE scene save clears
the overlay for that project (the saved scene becomes the truth), so the
overlay only ever holds *unsaved live deltas since the last save* — it can
never resurrect stale positions over a real edit.

Thread-safe: FastAPI runs sync endpoints in a threadpool, so concurrent
pushes/reads are guarded by a lock. Purely in-memory (no persistence): a
backend restart drops the overlay, which is correct — non-persisted live
state is ephemeral by definition.
"""

from __future__ import annotations

import threading
from typing import Optional

from ..schemas.scene import Scene

_lock = threading.Lock()
# project_id -> {"devices": {device_id: [x, y, z]},
#                "actors":  {actor_id: {"position": [x,y,z],
#                                       "orientation_deg": [yaw,pitch,roll] | None}}}
_overlay: dict[str, dict] = {}


def record(
    project_id: str,
    device_positions: dict[str, list[float]],
    actor_states: Optional[dict[str, dict]] = None,
) -> None:
    """Merge the latest non-persisted live positions for a project.

    ``device_positions`` maps device id -> [x, y, z]; ``actor_states`` maps
    actor id -> {"position": [...], "orientation_deg": [...] | None}. Later
    pushes overwrite earlier ones per id (last write wins).
    """
    with _lock:
        entry = _overlay.setdefault(project_id, {"devices": {}, "actors": {}})
        for did, pos in device_positions.items():
            entry["devices"][did] = [float(c) for c in pos]
        for aid, st in (actor_states or {}).items():
            entry["actors"][aid] = {
                "position": [float(c) for c in st["position"]],
                "orientation_deg": (
                    [float(a) for a in st["orientation_deg"]]
                    if st.get("orientation_deg") is not None
                    else None
                ),
            }


def clear(project_id: str) -> None:
    """Drop a project's overlay (called on any authoritative scene save)."""
    with _lock:
        _overlay.pop(project_id, None)


def has_overlay(project_id: str) -> bool:
    with _lock:
        entry = _overlay.get(project_id)
        return bool(entry and (entry["devices"] or entry["actors"]))


def apply_overlay(project_id: str, scene: Scene) -> Scene:
    """Overlay the latest live positions onto a freshly-loaded ``scene``.

    Mutates and returns the passed scene (``load_scene`` hands out a fresh
    object each call, so this never touches shared state). A no-op when the
    project has no live overlay.
    """
    with _lock:
        entry = _overlay.get(project_id)
        if not entry:
            return scene
        dev_ov = dict(entry["devices"])
        act_ov = {k: dict(v) for k, v in entry["actors"].items()}
    if not dev_ov and not act_ov:
        return scene
    for d in scene.devices:
        pos = dev_ov.get(d.id)
        if pos is not None:
            d.position = list(pos)
    for a in scene.actors:
        st = act_ov.get(a.id)
        if st is not None:
            a.position = list(st["position"])
            if st.get("orientation_deg") is not None:
                a.orientation_deg = list(st["orientation_deg"])
    return scene
