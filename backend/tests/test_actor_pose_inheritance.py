"""Radio map and beamforming solve against the authored actor poses (F03).

A scenario moves actor SceneObjects on the cached Mitsuba scene and leaves
them at the last frame's pose. Paths and sensing solves re-place every actor;
radio map and beamforming must too, or they silently trace a car or drone
where the last scenario frame left it.

Only the live test guards F03: the mock has no cached scene, so the mock test
is an API smoke test (the routes run after a scenario and never write the
frame poses back), not coverage of the stale-pose bug.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import trimesh

from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Actor, ActorTrajectory, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import (
    BeamformingRequest,
    RadioMapGridConfig,
    SimulationConfig,
)

from .conftest import requires_sionna

FREQ = 3.5e9
AUTHORED = [10.0, 0.0, 0.0]
# 10 m/s along +x: frame 2 of a dt = 1 s scenario parks the car at x = 30.
DRIVE = ActorTrajectory(waypoints=[[10.0, 0.0, 0.0], [30.0, 0.0, 0.0]], dt_s=2.0)


def _scene() -> Scene:
    """TX and RX 1 m up at y = 3 with the car between them: at its authored
    pose the car's +y side mirrors TX -> car -> RX and shadows part of the map
    grid; parked at x = 30 it does neither."""
    return Scene(
        scene_id="pose_inherit",
        prims=[
            Prim(
                id="/ground",
                name="ground",
                semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            )
        ],
        devices=[
            Device(id="tx_001", kind="tx", position=[0.0, 3.0, 1.0], power_dbm=30.0),
            Device(id="rx_001", kind="rx", position=[20.0, 3.0, 1.0]),
        ],
        actors=[
            Actor(
                id="car_01", kind="car", position=list(AUTHORED),
                trajectory=DRIVE.model_copy(deep=True),
            )
        ],
    )


def _write_ground(project_dir: Path) -> None:
    (project_dir / "visual").mkdir(parents=True, exist_ok=True)
    (project_dir / "rf").mkdir(exist_ok=True)
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(120.0, 120.0, 0.2))
    ground.apply_translation((0.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    (project_dir / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))


def _config(backend: str) -> SimulationConfig:
    return SimulationConfig(
        backend=backend, frequency_hz=FREQ, max_depth=1, reflection=True,
        scattering=False, diffraction=False, num_samples=200_000,
        radio_map=RadioMapGridConfig(
            cell_size_m=1.0, height_m=1.0, center_xy=[20.0, 0.0], size_xy=[30.0, 10.0]
        ),
    )


# ------------------------------------------------------------------ mock


def test_mock_scenario_then_radio_map_and_beamforming_smoke(api_client):
    from seam_studio.api import deps

    store = deps.get_store()
    store.create_project("Pose", project_id="pose_inherit")
    store.save_scene("pose_inherit", _scene())
    cfg = _config("mock").model_dump(mode="json")
    base = "/api/projects/pose_inherit"

    scenario = api_client.post(
        f"{base}/simulate/scenario", json={"config": cfg, "num_frames": 3, "dt_s": 1.0}
    )
    assert scenario.status_code == 200
    assert scenario.json()["frames"][2]["actor_states"][0]["position"] == [30.0, 0.0, 0.0]
    assert api_client.post(f"{base}/simulate/radio-map", json={"config": cfg}).status_code == 200
    assert api_client.post(f"{base}/simulate/beamforming", json={"config": cfg}).status_code == 200
    assert store.load_scene("pose_inherit").actors[0].position == AUTHORED


# ------------------------------------------------------------------ live


@requires_sionna
def test_sionna_radio_map_and_beamforming_ignore_the_last_scenario_frame(
    tmp_path: Path, monkeypatch
):
    from seam_studio.schemas.actors import ScenarioSimulateRequest
    from seam_studio.services.project_store import load_default_library
    from seam_studio.services.scenario import run_scenario
    from seam_studio.services.simulation_backends import sionna_backend as sb

    # No periodic cache flush inside this test (it would hide the stale pose),
    # and the counter is back where other tests expect it afterwards.
    monkeypatch.setattr(sb, "_solves_since_full_flush", 0)

    project = tmp_path / "pose_inherit.seam"
    _write_ground(project)
    scene, library, cfg = _scene(), load_default_library(), _config("sionna")
    backend = sb.SionnaBackend()
    bf_request = BeamformingRequest(tx_rows=2, tx_cols=2, rx_rows=2, rx_cols=2, mode="svd")

    def car_centroid() -> np.ndarray:
        rt_scene = sb._load_scene_cached(project / "rf" / "generated_scene.xml", [])
        key = sb._actor_object_key(rt_scene, "car_01")
        assert key is not None
        return np.array(rt_scene.objects[key].position).reshape(-1)[:3].astype(float)

    def radio_map() -> np.ndarray:
        result = backend.simulate_radio_map(project, scene, library, cfg)
        assert not any("failed" in w for w in result.warnings), result.warnings
        return np.array(
            [[np.nan if v is None else v for v in row] for row in result.values], dtype=float
        )

    def beam() -> tuple[int, float, float]:
        result = backend.simulate_beamforming(project, scene, library, cfg, bf_request)
        assert result.svd_gain_db is not None, result.warnings
        return result.num_paths, result.single_element_dbm, result.svd_gain_db

    # Compile once so the scenario's up-front compile writes the same XML;
    # then a fresh cache gives the reference at the authored pose.
    backend.compile(project, scene, library)
    sb.clear_scene_cache()
    ref_map = radio_map()
    ref_beam = beam()
    assert ref_beam[0] == 3  # LoS, ground, and the car's side
    authored_centroid = car_centroid()

    def after_scenario() -> None:
        result = run_scenario(
            backend, project, scene, library, cfg,
            ScenarioSimulateRequest(config=cfg, num_frames=3, dt_s=1.0, include_paths=False),
        )
        assert result.frames[2].actor_states[0].position == [30.0, 0.0, 0.0]
        moved = car_centroid()
        assert moved[0] == pytest.approx(authored_centroid[0] + 20.0, abs=1e-3)

    after_scenario()
    after_map = radio_map()
    assert car_centroid() == pytest.approx(authored_centroid, abs=1e-4)
    finite = np.isfinite(ref_map) & np.isfinite(after_map)
    assert np.array_equal(np.isfinite(ref_map), np.isfinite(after_map))
    assert finite.any()
    assert np.max(np.abs(after_map[finite] - ref_map[finite])) <= 1e-3

    after_scenario()
    after_beam = beam()
    assert after_beam[0] == ref_beam[0]
    assert after_beam[1:] == pytest.approx(ref_beam[1:], abs=1e-3)
    assert car_centroid() == pytest.approx(authored_centroid, abs=1e-4)
