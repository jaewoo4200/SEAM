"""ML dataset generation tests (mock backend; no GPU needed)."""

import json

import numpy as np
import pytest

from app.schemas.datasets import DatasetGenerateRequest, DatasetSampling
from app.schemas.simulation import SimulationConfig
from app.services import dataset as ds
from app.services.simulation_backends import get_backend

from .conftest import make_demo_scene


@pytest.fixture()
def project_dir(store, demo_scene):
    info = store.create_project(name="DS", project_id="ds_proj")
    store.save_scene("ds_proj", demo_scene)
    return store.resolve("ds_proj")


def _gen(project_dir, library, sampling: DatasetSampling, **kw):
    scene = make_demo_scene("ds_proj")
    config = SimulationConfig(backend="mock")
    request = DatasetGenerateRequest(name="t", sampling=sampling, **kw)
    return ds.generate_dataset(
        project_dir, scene, library, config, request, get_backend("mock")
    )


def test_random_dataset_arrays_and_labels(project_dir, library):
    info = _gen(
        project_dir, library,
        DatasetSampling(mode="random", num_samples=12, seed=3,
                        region_min=[0, 0, 0], region_max=[30, 30, 3], height_m=1.5),
        num_cfr_points=32,
    )
    assert info.num_samples == 12
    out = project_dir / "export" / "datasets" / info.dataset_id
    with np.load(out / "dataset.npz") as z:
        assert z["positions_m"].shape == (12, 3)
        assert np.allclose(z["positions_m"][:, 2], 1.5)
        assert z["cfr"].shape == (12, 32) and z["cfr"].dtype == np.complex64
        assert z["cir_gain"].shape[0] == 12
        assert z["num_paths"].min() >= 0
        assert z["los"].dtype == bool
        # CFR is consistent with the stored taps: H(f_k) = sum_l g_l e^{-j2pi f_k tau_l}.
        freqs = z["cfr_freq_offset_hz"]
        k0 = int(np.argmin(np.abs(freqs)))
        tau_s = np.nan_to_num(z["cir_delay_ns"], nan=0.0) * 1e-9
        expected = (z["cir_gain"] * np.exp(-2j * np.pi * freqs[k0] * tau_s)).sum(axis=1)
        row = z["num_paths"] > 0
        assert np.allclose(z["cfr"][row, k0], expected[row], rtol=1e-3, atol=1e-6)
    meta = json.loads((out / "metadata.json").read_text(encoding="utf-8"))
    assert meta["schema_version"] == ds.SCHEMA_VERSION
    assert meta["backend"] == "mock"
    assert meta["sampling"]["mode"] == "random"


def test_grid_and_trajectory_sampling(project_dir, library):
    grid = _gen(
        project_dir, library,
        DatasetSampling(mode="grid", grid_spacing_m=10.0, num_samples=100,
                        region_min=[0, 0, 0], region_max=[20, 20, 3]),
        num_cfr_points=8,
    )
    assert grid.num_samples == 9  # 3x3 grid at 10 m spacing over 20 m
    traj = _gen(
        project_dir, library,
        DatasetSampling(mode="trajectory", num_samples=5,
                        start_m=[0, 0, 1.5], end_m=[8, 0, 1.5]),
        num_cfr_points=8,
    )
    out = project_dir / "export" / "datasets" / traj.dataset_id
    with np.load(out / "dataset.npz") as z:
        assert np.allclose(z["positions_m"][:, 0], [0, 2, 4, 6, 8])


def test_trajectory_requires_endpoints(project_dir, library):
    with pytest.raises(ValueError):
        _gen(project_dir, library, DatasetSampling(mode="trajectory", num_samples=3))


def test_list_and_download_roundtrip(project_dir, library):
    info = _gen(project_dir, library,
                DatasetSampling(mode="random", num_samples=3,
                                region_min=[0, 0, 0], region_max=[5, 5, 3]))
    listed = ds.list_datasets(project_dir)
    assert any(d.dataset_id == info.dataset_id and d.num_samples == 3 for d in listed)
    f = ds.dataset_file(project_dir, info.dataset_id, "dataset.npz")
    assert f is not None and f.is_file()
    # Path escape refused.
    assert ds.dataset_file(project_dir, "..", "secrets.txt") is None


def test_include_paths_writes_jsonl(project_dir, library):
    info = _gen(project_dir, library,
                DatasetSampling(mode="random", num_samples=2,
                                region_min=[0, 0, 0], region_max=[5, 5, 3]),
                include_paths=True)
    out = project_dir / "export" / "datasets" / info.dataset_id / "paths.jsonl"
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    json.loads(lines[0])  # valid JSON per line
