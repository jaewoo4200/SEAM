"""ML ground-truth dataset generation schemas.

The dataset pipeline is the AODT-style research loop: sweep a UE over
positions in the scene, solve ray-traced ground truth per position, and export
arrays (complex CFR/CIR + labels) that train and validate communication
algorithms (channel estimation, beam prediction, localization, LOS
classification). See docs/ml_datasets.md for the array layout and a training
example.
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel, Vec3
from .simulation import SimulationConfig


class DatasetSampling(StrictModel):
    mode: Literal["random", "grid", "trajectory"] = "random"
    # Sampling region (axis-aligned, meters, Z-up world). When omitted the
    # generator falls back to the bounding box of the scene's devices padded
    # by 25 m - the UI prefills these from the visual scene bounds instead.
    region_min: Optional[Vec3] = None
    region_max: Optional[Vec3] = None
    # UE height above z=0 for random/grid sampling (positions are placed at
    # this z; region z bounds are ignored for those modes).
    height_m: float = 1.5
    # random: number of uniform samples. grid: cap on grid points (the grid is
    # truncated row-major if spacing yields more). trajectory: waypoint count.
    num_samples: int = Field(default=256, ge=1, le=20000)
    grid_spacing_m: float = Field(default=2.0, gt=0.0)
    # trajectory mode: straight line from start to end (inclusive).
    start_m: Optional[Vec3] = None
    end_m: Optional[Vec3] = None
    seed: int = Field(default=0, ge=0)
    # Snap each sampled position's z to the scene surface underneath it
    # (raycast down onto the visual mesh) + height_m. Meant for outdoor
    # scenes with sloped terrain; indoor scenes should leave this off.
    follow_terrain: bool = False


class DatasetGenerateRequest(StrictModel):
    name: str = "dataset"
    # Solver config: stored config id or inline (inline wins).
    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    tx_id: Optional[str] = None  # None = first tx in the scene
    # Antenna/geometry of the swept UE: None = first rx device's antenna.
    rx_id: Optional[str] = None
    sampling: DatasetSampling = Field(default_factory=DatasetSampling)
    num_cfr_points: int = Field(default=128, ge=2, le=4096)
    # Also dump per-path vertices/interactions as JSONL (large; off by default).
    include_paths: bool = False


class DatasetInfo(StrictModel):
    dataset_id: str
    name: str
    num_samples: int
    num_cfr_points: int
    created_at: Optional[str] = None
    files: list[str] = Field(default_factory=list)
    size_bytes: int = 0
    warnings: list[str] = Field(default_factory=list)
    metadata: dict = Field(default_factory=dict)


class DatasetListResponse(StrictModel):
    datasets: list[DatasetInfo] = Field(default_factory=list)


class DatasetDeleteResponse(StrictModel):
    deleted: bool
    dataset_id: str
