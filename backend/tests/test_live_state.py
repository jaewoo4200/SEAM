"""Ephemeral live-state overlay (POST /live/state persist=false).

A persist=false push must still be visible to GET /scene polling and periodic
re-solves WITHOUT writing to disk; an authoritative save must clear it so it
can never resurrect stale positions. These tests exercise the overlay service
directly plus its integration through the store's load/save.
"""

from pathlib import Path

from app.schemas.scene import Scene
from app.services import live_state
from app.services.project_store import ProjectStore


def _scene() -> Scene:
    from app.schemas.devices import Device

    return Scene(
        scene_id="live_test",
        name="Live",
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 10.0]),
            Device(id="rx_001", name="UE", kind="rx", position=[1.0, 2.0, 1.5]),
        ],
    )


def test_overlay_applies_and_clears(tmp_path: Path) -> None:
    live_state.clear("live_test")  # isolate from other tests / prior runs
    scene = _scene()
    assert not live_state.has_overlay("live_test")

    # A non-persisted push records device positions into the overlay.
    live_state.record("live_test", {"rx_001": [10.0, 5.0, 1.5]})
    assert live_state.has_overlay("live_test")

    # Applying the overlay onto a freshly-loaded scene moves the device.
    fresh = _scene()
    live_state.apply_overlay("live_test", fresh)
    rx = next(d for d in fresh.devices if d.id == "rx_001")
    assert rx.position == [10.0, 5.0, 1.5]
    # An un-pushed device is untouched.
    tx = next(d for d in fresh.devices if d.id == "tx_001")
    assert tx.position == [0.0, 0.0, 10.0]

    # Clearing removes the overlay.
    live_state.clear("live_test")
    fresh2 = _scene()
    live_state.apply_overlay("live_test", fresh2)
    assert next(d for d in fresh2.devices if d.id == "rx_001").position == [1.0, 2.0, 1.5]


def test_save_scene_clears_overlay(tmp_path: Path) -> None:
    """An authoritative save drops the overlay so it can't override the save."""
    root = tmp_path / "projects"
    root.mkdir()
    store = ProjectStore(roots=[root])
    info = store.create_project(name="Live", project_id="live_save")
    scene = store.load_scene("live_save")

    live_state.record("live_save", {"any_device": [9.0, 9.0, 9.0]})
    assert live_state.has_overlay("live_save")

    store.save_scene("live_save", scene)
    assert not live_state.has_overlay("live_save")
    _ = info


def test_load_scene_reflects_overlay(tmp_path: Path) -> None:
    """GET /scene path (store.load_scene + deps overlay) follows a persist=false
    push without the scene file changing on disk."""
    from app.api import deps

    root = tmp_path / "projects"
    root.mkdir()
    store = ProjectStore(roots=[root])
    store.create_project(name="Live", project_id="live_load")
    # Seed one device so there is something to move.
    scene = store.load_scene("live_load")
    from app.schemas.devices import Device

    scene.devices.append(Device(id="rx_001", kind="rx", position=[0.0, 0.0, 1.5]))
    store.save_scene("live_load", scene)

    # Non-persisted push: overlay only, no disk write.
    live_state.record("live_load", {"rx_001": [42.0, 0.0, 1.5]})

    # load_scene_live (used by GET /scene + simulate reads) applies the overlay.
    seen = deps.load_scene_live(store, "live_load")
    assert next(d for d in seen.devices if d.id == "rx_001").position == [42.0, 0.0, 1.5]

    # load_scene_or_404 (write-path loads) stays CLEAN so a save can't persist
    # the overlay — this is the safeguard against write endpoints leaking it.
    clean = deps.load_scene_or_404(store, "live_load")
    assert next(d for d in clean.devices if d.id == "rx_001").position == [0.0, 0.0, 1.5]

    # The scene FILE on disk is untouched (still the saved 0,0,1.5).
    raw = store.load_scene("live_load")
    assert next(d for d in raw.devices if d.id == "rx_001").position == [0.0, 0.0, 1.5]
    live_state.clear("live_load")
