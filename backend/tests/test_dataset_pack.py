"""Volumetric / flight-path / velocity+Doppler dataset generation tests.

Covers the UAV-research extensions on top of test_datasets.py: volumetric z
sampling (region z-span), waypoint/actor trajectory sources, and the
ue_velocity / doppler_spread_hz labels. Mock backend + stubs; no GPU needed.
"""

import json

import numpy as np
import pytest
from pydantic import ValidationError

from seam_studio.schemas.datasets import DatasetGenerateRequest, DatasetSampling
from seam_studio.schemas.results import PathResultSet, RayPath
from seam_studio.schemas.scene import Actor, ActorTrajectory
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import dataset as ds
from seam_studio.services.simulation_backends import get_backend
from seam_studio.services.simulation_backends.base import UNSAVED_RESULT_ID

from .conftest import make_demo_scene


@pytest.fixture()
def project_dir(store, demo_scene):
    info = store.create_project(name="DSP", project_id="dsp_proj")
    store.save_scene("dsp_proj", demo_scene)
    return store.resolve("dsp_proj")


def _scene_with_uav(scene_id: str = "dsp_proj"):
    """Demo scene plus a UAV actor flying a 20 m straight leg at z=10."""
    scene = make_demo_scene(scene_id)
    scene.actors.append(Actor(
        id="uav_1", kind="uav", position=[0.0, 0.0, 10.0],
        trajectory=ActorTrajectory(
            waypoints=[[0.0, 0.0, 10.0], [20.0, 0.0, 10.0]], dt_s=0.5,
        ),
    ))
    scene.actors.append(Actor(id="uav_hover", kind="uav", position=[5.0, 5.0, 10.0]))
    return scene


def _gen(project_dir, library, sampling: DatasetSampling, *, scene=None,
         backend=None, **kw):
    scene = scene if scene is not None else make_demo_scene("dsp_proj")
    config = SimulationConfig(backend="mock")
    request = DatasetGenerateRequest(name="pack", sampling=sampling, **kw)
    return ds.generate_dataset(
        project_dir, scene, library, config, request,
        backend if backend is not None else get_backend("mock"),
    )


def _arrays(project_dir, info) -> dict:
    out = project_dir / "export" / "datasets" / info.dataset_id
    with np.load(out / "dataset.npz") as z:
        return {k: z[k] for k in z.files}


def _meta(project_dir, info) -> dict:
    out = project_dir / "export" / "datasets" / info.dataset_id
    return json.loads((out / "metadata.json").read_text(encoding="utf-8"))


# ---------------------------------------------------- volumetric sampling


def test_random_volumetric_z_spans_range(project_dir, library):
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=64, seed=3,
                        region_min=[0, 0, 1], region_max=[30, 30, 20]),
        num_cfr_points=8,
    )
    z = _arrays(project_dir, info)["positions_m"][:, 2]
    assert z.min() >= 1.0 and z.max() <= 20.0
    # Uniform draws actually spread over the volume, not one plane.
    assert z.max() - z.min() > 5.0
    assert len(np.unique(z)) > 1
    # Deterministic seeding: the same request reproduces the same positions.
    again = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=64, seed=3,
                        region_min=[0, 0, 1], region_max=[30, 30, 20]),
        num_cfr_points=8,
    )
    assert np.array_equal(
        _arrays(project_dir, info)["positions_m"],
        _arrays(project_dir, again)["positions_m"],
    )


def test_random_zero_z_span_keeps_height_plane(project_dir, library):
    planar = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=16, seed=7, height_m=1.5,
                        region_min=[0, 0, 1], region_max=[30, 30, 1]),
        num_cfr_points=8,
    )
    p = _arrays(project_dir, planar)["positions_m"]
    # z-span == 0: legacy behavior, every sample on the height_m plane.
    assert np.allclose(p[:, 2], 1.5)
    # The xy stream for a given seed is unchanged by volumetric z (z draws
    # come after xy), so planar and volumetric requests share their footprint.
    volumetric = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=16, seed=7,
                        region_min=[0, 0, 1], region_max=[30, 30, 20]),
        num_cfr_points=8,
    )
    v = _arrays(project_dir, volumetric)["positions_m"]
    assert np.array_equal(p[:, :2], v[:, :2])


def test_explicit_height_pins_plane_despite_z_span(project_dir, library):
    # An explicitly supplied height_m keeps the legacy plane even when the
    # region z bounds differ (back-compat for callers that always send both).
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=8, seed=2, height_m=1.5,
                        region_min=[0, 0, 0], region_max=[30, 30, 3]),
        num_cfr_points=8,
    )
    assert np.allclose(_arrays(project_dir, info)["positions_m"][:, 2], 1.5)


def test_grid_volumetric_adds_z_levels(project_dir, library):
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="grid", grid_spacing_m=10.0, num_samples=100,
                        region_min=[0, 0, 0], region_max=[20, 20, 10]),
        num_cfr_points=8,
    )
    pts = _arrays(project_dir, info)["positions_m"]
    # 3x3 xy grid x 2 z levels (0 and 10 m at 10 m spacing) within the budget.
    assert info.num_samples == 18
    assert np.allclose(np.unique(pts[:, 2]), [0.0, 10.0])
    # Every xy point appears once per z level.
    xy = {tuple(p) for p in pts[:, :2].tolist()}
    assert len(xy) == 9
    for level in (0.0, 10.0):
        assert np.count_nonzero(pts[:, 2] == level) == 9


def test_grid_zero_z_span_keeps_height_plane(project_dir, library):
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="grid", grid_spacing_m=10.0, num_samples=100,
                        region_min=[0, 0, 3], region_max=[20, 20, 3]),
        num_cfr_points=8,
    )
    pts = _arrays(project_dir, info)["positions_m"]
    assert info.num_samples == 9  # legacy 3x3 plane
    assert np.allclose(pts[:, 2], 1.5)  # height_m, not the region z


def test_grid_volumetric_budget_caps_z_levels(project_dir, library):
    # num_samples only affords the 3x3 xy grid: one z level, at the lower
    # z bound of the volume.
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="grid", grid_spacing_m=10.0, num_samples=9,
                        region_min=[0, 0, 0], region_max=[20, 20, 10]),
        num_cfr_points=8,
    )
    pts = _arrays(project_dir, info)["positions_m"]
    assert info.num_samples == 9
    assert np.allclose(pts[:, 2], 0.0)


# ---------------------------------------------------- flight-path trajectories


def test_waypoint_trajectory_follows_polyline(project_dir, library):
    # start/end and actor_id are also supplied: explicit waypoints win.
    scene = _scene_with_uav()
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="trajectory", num_samples=9,
                        waypoints=[[0, 0, 1], [10, 0, 2], [10, 10, 3]],
                        actor_id="uav_1",
                        start_m=[50, 50, 5], end_m=[60, 60, 5]),
        scene=scene, num_cfr_points=8,
    )
    pts = _arrays(project_dir, info)["positions_m"]
    assert pts.shape == (9, 3)
    assert np.allclose(pts[0], [0, 0, 1], atol=1e-5)
    assert np.allclose(pts[-1], [10, 10, 3], atol=1e-5)
    # Equal-arc-length resampling puts the middle sample on the corner (the
    # two legs have equal length), and z interpolates within the polyline.
    assert np.allclose(pts[4], [10, 0, 2], atol=1e-4)
    assert pts[:, 2].min() >= 1.0 - 1e-5 and pts[:, 2].max() <= 3.0 + 1e-5
    vel = _arrays(project_dir, info)["ue_velocity"]
    assert vel.shape == (9, 3) and (np.linalg.norm(vel, axis=1) > 0).all()


def test_actor_trajectory_resolution(project_dir, library):
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="trajectory", num_samples=5, actor_id="uav_1"),
        scene=_scene_with_uav(), num_cfr_points=8,
    )
    z = _arrays(project_dir, info)
    assert np.allclose(z["positions_m"][:, 0], [0, 5, 10, 15, 20], atol=1e-4)
    assert np.allclose(z["positions_m"][:, 2], 10.0)
    # Authored speed preserved: 20 m over dt_s*(2-1)=0.5 s -> 40 m/s along +x,
    # via the derived per-sample step 0.5/4 = 0.125 s.
    assert _meta(project_dir, info)["sample_dt_s"] == pytest.approx(0.125)
    assert np.allclose(z["ue_velocity"], [[40.0, 0.0, 0.0]] * 5, atol=1e-3)


def test_actor_trajectory_errors(project_dir, library):
    scene = _scene_with_uav()
    with pytest.raises(ValueError, match="unknown actor"):
        _gen(project_dir, library,
             DatasetSampling(mode="trajectory", num_samples=3, actor_id="nope"),
             scene=scene, num_cfr_points=8)
    with pytest.raises(ValueError, match="no trajectory"):
        _gen(project_dir, library,
             DatasetSampling(mode="trajectory", num_samples=3,
                             actor_id="uav_hover"),
             scene=scene, num_cfr_points=8)


def test_waypoints_require_at_least_two():
    with pytest.raises(ValidationError):
        DatasetSampling(mode="trajectory", waypoints=[[0, 0, 1]])


# ---------------------------------------------------- velocity + Doppler labels


class _DopplerBackend:
    """Backend stub returning two equal-power paths plus per-path Doppler
    metadata — the shape sionna emits when something in the link moves. Also
    records the swept UE's stamped velocity per solve so tests can assert the
    trajectory finite differences actually reach the solver."""

    name = "mock"

    def __init__(self):
        self.ue_velocities: list = []

    def simulate_paths(self, project_dir, scene, library, config) -> PathResultSet:
        ue = next(d for d in scene.devices if d.id == "ue_dataset")
        self.ue_velocities.append(ue.velocity_m_s)
        mk = lambda pid, ptype, delay, phase: RayPath(  # noqa: E731
            path_id=pid, tx_id="tx_001", rx_id="ue_dataset", path_type=ptype,
            vertices=[[0.0, 0.0, 10.0], [float(c) for c in ue.position]],
            power_dbm=-60.0, delay_ns=delay, phase_rad=phase,
        )
        return PathResultSet(
            result_id=UNSAVED_RESULT_ID,
            backend=self.name,
            simulation_config_id=config.id,
            paths=[mk("p0", "los", 100.0, 0.0), mk("p1", "reflection", 150.0, 0.5)],
            metadata={"doppler_hz": [10.0, 20.0]},
        )


def test_ue_velocity_along_trajectory_line(project_dir, library):
    backend = _DopplerBackend()
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="trajectory", num_samples=5,
                        start_m=[0, 0, 1.5], end_m=[8, 0, 1.5]),
        backend=backend, num_cfr_points=8,
    )
    z = _arrays(project_dir, info)
    # 2 m per 0.1 s (default dt_s) -> 20 m/s along +x at every sample (the
    # last sample's backward difference matches the constant-speed line).
    assert z["ue_velocity"].shape == (5, 3)
    assert z["ue_velocity"].dtype == np.float32
    assert np.allclose(z["ue_velocity"], [[20.0, 0.0, 0.0]] * 5, atol=1e-4)
    # ...and the same velocity was stamped on the UE for every solve.
    assert all(v == pytest.approx([20.0, 0.0, 0.0]) for v in backend.ue_velocities)
    assert _meta(project_dir, info)["sample_dt_s"] == pytest.approx(0.1)


def test_ue_velocity_zeros_and_no_doppler_for_random(project_dir, library):
    backend = _DopplerBackend()
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=4, seed=1,
                        region_min=[0, 0, 0], region_max=[10, 10, 0]),
        backend=get_backend("mock"), num_cfr_points=8,
    )
    z = _arrays(project_dir, info)
    assert z["ue_velocity"].shape == (4, 3)
    assert not z["ue_velocity"].any()  # zeros for random/grid
    # The mock backend reports no Doppler: the optional array is absent and
    # the metadata says so.
    assert "doppler_spread_hz" not in z
    meta = _meta(project_dir, info)
    assert meta["sample_dt_s"] is None
    assert meta["has_doppler_spread"] is False
    # Random sampling never stamps a velocity on the solved UE (byte-compat
    # with the pre-velocity solve inputs).
    info2 = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=4, seed=1,
                        region_min=[0, 0, 0], region_max=[10, 10, 0]),
        backend=backend, num_cfr_points=8,
    )
    assert info2.num_samples == 4
    assert all(v is None for v in backend.ue_velocities)


def test_doppler_spread_from_solver_metadata(project_dir, library):
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="trajectory", num_samples=5,
                        start_m=[0, 0, 1.5], end_m=[8, 0, 1.5]),
        backend=_DopplerBackend(), num_cfr_points=8,
    )
    z = _arrays(project_dir, info)
    # Equal powers, per-path Doppler 10/20 Hz -> power-weighted std = 5 Hz.
    assert z["doppler_spread_hz"].shape == (5,)
    assert z["doppler_spread_hz"].dtype == np.float32
    assert np.allclose(z["doppler_spread_hz"], 5.0, atol=1e-4)
    assert _meta(project_dir, info)["has_doppler_spread"] is True
