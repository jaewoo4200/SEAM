"""Mesh radio map: the per-chunk solver warnings reach the result.

The first chunk of a stale project carries the auto-recompile warnings
(material itu_name collisions, "rf projection was stale ... recompiled"); the
chunk loop used to read only ``result.paths`` and drop them (F21 follow-up).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Prim, Scene
from seam_studio.schemas.simulation import MeshRadioMapRequest, SimulationConfig
from seam_studio.services import mesh_radio_map as mrm

SOLVER_WARNINGS = [
    "materials 'a' and 'b' share itu_name='itu_concrete'",
    "rf projection was stale (material assignments changed); recompiled",
]


class _WarningBackend:
    name = "fake"

    def __init__(self) -> None:
        self.calls = 0

    def simulate_paths(self, project_dir, scene, library, config):
        self.calls += 1
        return SimpleNamespace(paths=[], warnings=list(SOLVER_WARNINGS))


def test_chunk_solver_warnings_are_kept_once(monkeypatch):
    n = mrm.CHUNK_SIZE + 1  # two chunks, both repeating the same warnings
    monkeypatch.setattr(
        mrm, "_sample_surface",
        lambda *a, **k: ([[float(i), 0.0, 0.0] for i in range(n)], [[0.0, 0.0, 1.0]] * n, 1),
    )
    scene = Scene(
        scene_id="s",
        prims=[Prim(id="/wall", name="wall", type="mesh_primitive")],
        devices=[Device(id="tx1", name="tx", kind="tx", position=[0.0, 0.0, 10.0])],
    )
    backend = _WarningBackend()
    result = mrm.mesh_radio_map(
        backend, Path("."), scene, None, SimulationConfig(),
        MeshRadioMapRequest(prim_ids=["/wall"]),
    )
    assert backend.calls == 2
    assert result.warnings == SOLVER_WARNINGS
