"""Ground-truth ML dataset generation.

Sweeps a UE (rx) over sampled positions, solves ray-traced paths per position
through the normal backend stack (mock or Sionna, including alternate engine
venvs via SimulationConfig.engine), and writes a NumPy .npz + metadata.json
under <project>/export/datasets/<dataset_id>/.

Array layout (documented for consumers in docs/ml_datasets.md):
    positions_m          float32 [N, 3]   UE positions (Z-up meters)
    tx_position_m        float32 [3]
    cfr                  complex64 [N, K] channel frequency response
    cfr_freq_offset_hz   float64 [K]      offsets across [-B/2, +B/2]
    cir_gain             complex64 [N, P] per-path complex gain (0-padded)
    cir_delay_ns         float32 [N, P]   per-path delay (NaN-padded)
    num_paths            int32 [N]
    los                  bool [N]         any line-of-sight path present
    rss_dbm              float32 [N]      total received power
    mean_delay_ns        float32 [N]      power-weighted mean delay (NaN if none)
    rms_delay_spread_ns  float32 [N]      (NaN if undefined)
    k_factor_db          float32 [N]      Rician K (NaN if undefined)

The CFR uses the same tap model as the channel-analysis panel:
H(f_k) = sum_l a_l exp(-j 2 pi f_k tau_l), a_l from path power+phase - so a
dataset sample and the interactive panel agree by construction.
"""

from __future__ import annotations

import json
import math
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..schemas.datasets import DatasetGenerateRequest, DatasetInfo, DatasetSampling
from ..schemas.devices import Antenna, Device
from ..schemas.materials import RFMaterialLibrary
from ..schemas.results import PathResultSet
from ..schemas.scene import Scene
from ..schemas.simulation import SimulationConfig
from .channel_analysis import delay_metrics, k_factor_db

DATASETS_SUBDIR = Path("export") / "datasets"
SCHEMA_VERSION = "1.0"


def _sample_positions(
    sampling: DatasetSampling, scene: Scene, warnings: list[str], project_dir: Path
):
    import numpy as np

    def _snap(pts):
        if not sampling.follow_terrain:
            return pts
        from .terrain import snap_to_terrain

        snapped = snap_to_terrain(
            project_dir, scene, [list(map(float, p)) for p in pts],
            sampling.height_m, warnings,
        )
        return np.asarray(snapped, dtype=np.float64)

    if sampling.mode == "trajectory":
        if not sampling.start_m or not sampling.end_m:
            raise ValueError("trajectory sampling needs start_m and end_m")
        t = np.linspace(0.0, 1.0, sampling.num_samples)[:, None]
        a = np.asarray(sampling.start_m, dtype=np.float64)
        b = np.asarray(sampling.end_m, dtype=np.float64)
        return _snap(a[None, :] * (1 - t) + b[None, :] * t)

    if sampling.region_min and sampling.region_max:
        lo = np.asarray(sampling.region_min, dtype=np.float64)
        hi = np.asarray(sampling.region_max, dtype=np.float64)
    else:
        # Fallback region: the actual scene AABB (the old device-bbox ±25 m
        # guess sampled mostly outside small indoor scenes — audit F3).
        from .scene_bounds import compute_scene_bounds

        bounds = compute_scene_bounds(project_dir, scene)
        if bounds is not None:
            lo = np.asarray(bounds.min, dtype=np.float64)
            hi = np.asarray(bounds.max, dtype=np.float64)
            warnings.append(
                "sampling region omitted; using the scene bounds "
                f"({lo[:2].round(1).tolist()}..{hi[:2].round(1).tolist()})"
            )
        else:
            pts = np.asarray([d.position for d in scene.devices] or [[0, 0, 0]])
            lo, hi = pts.min(axis=0) - 25.0, pts.max(axis=0) + 25.0
            warnings.append(
                "sampling region omitted and the scene has no visual mesh; "
                "using device bounding box +/-25 m "
                f"({lo[:2].round(1).tolist()}..{hi[:2].round(1).tolist()})"
            )

    if sampling.mode == "grid":
        xs = np.arange(lo[0], hi[0] + 1e-9, sampling.grid_spacing_m)
        ys = np.arange(lo[1], hi[1] + 1e-9, sampling.grid_spacing_m)
        gx, gy = np.meshgrid(xs, ys, indexing="xy")
        pts = np.stack([gx.ravel(), gy.ravel(), np.full(gx.size, sampling.height_m)], axis=1)
        if len(pts) > sampling.num_samples:
            warnings.append(
                f"grid has {len(pts)} points; truncated to num_samples={sampling.num_samples}"
            )
            pts = pts[: sampling.num_samples]
        return _snap(pts)

    rng = np.random.default_rng(sampling.seed)
    xy = rng.uniform(lo[:2], hi[:2], size=(sampling.num_samples, 2))
    z = np.full((sampling.num_samples, 1), sampling.height_m)
    return _snap(np.concatenate([xy, z], axis=1))


def _complex_gains(result: PathResultSet):
    """Per-path complex voltage gains + delays from a paths result."""
    import numpy as np

    amps = np.asarray(
        [math.sqrt(10.0 ** (p.power_dbm / 10.0)) for p in result.paths], dtype=np.float64
    )
    phases = np.asarray([p.phase_rad for p in result.paths], dtype=np.float64)
    delays_ns = np.asarray([p.delay_ns for p in result.paths], dtype=np.float64)
    return amps * np.exp(1j * phases), delays_ns


def generate_dataset(
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: DatasetGenerateRequest,
    backend,
) -> DatasetInfo:
    import numpy as np

    warnings: list[str] = []
    txs = [d for d in scene.devices if d.kind == "tx"]
    rxs = [d for d in scene.devices if d.kind == "rx"]
    tx = next((d for d in txs if d.id == request.tx_id), txs[0] if txs else None)
    if tx is None:
        raise ValueError("scene has no transmitter")
    rx_proto = next((d for d in rxs if d.id == request.rx_id), rxs[0] if rxs else None)
    # Each dataset is ONE UE's sequence: a single synthetic receiver
    # ("ue_dataset") swept over positions_m. The dataset sampler is single-UE
    # by construction — it does not consume multi-UE TrajectoryResultSets — so
    # there is no per-sample ue_id to group by; every row of every array
    # belongs to this one UE. We record which scene RX its antenna/identity was
    # cloned from (source_rx_id) so a consumer can associate the sequence with
    # a device; None when the scene has no RX and we fall back to a default
    # isotropic antenna.
    source_rx_id = rx_proto.id if rx_proto else None
    ue = Device(
        id="ue_dataset",
        name="Dataset UE",
        kind="rx",
        position=[0.0, 0.0, 1.5],
        antenna=rx_proto.antenna if rx_proto else Antenna(),
    )

    positions = _sample_positions(request.sampling, scene, warnings, project_dir)
    n = len(positions)
    k = request.num_cfr_points
    freqs = (
        np.linspace(-config.bandwidth_hz / 2.0, config.bandwidth_hz / 2.0, k)
        if k > 1
        else np.zeros(1)
    )

    cfr = np.zeros((n, k), dtype=np.complex64)
    num_paths = np.zeros(n, dtype=np.int32)
    los = np.zeros(n, dtype=bool)
    rss_dbm = np.full(n, np.nan, dtype=np.float32)
    mean_delay = np.full(n, np.nan, dtype=np.float32)
    rms_ds = np.full(n, np.nan, dtype=np.float32)
    kfac = np.full(n, np.nan, dtype=np.float32)
    gains_per_sample: list = []
    delays_per_sample: list = []
    paths_dump = [] if request.include_paths else None

    started = time.monotonic()
    for i, pos in enumerate(positions):
        ue_i = ue.model_copy(update={"position": [float(x) for x in pos]})
        # Only the fixed TX and the swept UE take part in the solve; the rest
        # of the scene (prims, actors) is untouched and the compiled XML is
        # reused via the backend's scene cache.
        scene_i = scene.model_copy(update={"devices": [tx, ue_i]})
        result = backend.simulate_paths(project_dir, scene_i, library, config)
        if i == 0:
            warnings.extend(result.warnings)
        elif any("failed" in w for w in result.warnings):
            warnings.append(f"sample {i}: " + "; ".join(result.warnings[-1:]))

        gains, delays_ns = _complex_gains(result)
        gains_per_sample.append(gains.astype(np.complex64))
        delays_per_sample.append(delays_ns.astype(np.float32))
        num_paths[i] = len(result.paths)
        if result.paths:
            los[i] = any(p.path_type == "los" for p in result.paths)
            total_mw = float(np.sum(np.abs(gains) ** 2))
            rss_dbm[i] = 10.0 * math.log10(total_mw) if total_mw > 0 else np.nan
            md, rms = delay_metrics(result.paths)
            mean_delay[i] = md if md is not None else np.nan
            rms_ds[i] = rms if rms is not None else np.nan
            kf = k_factor_db(result.paths)
            kfac[i] = kf if kf is not None else np.nan
            # Vectorized H(f) = sum_l a_l exp(-j 2 pi f tau_l).
            cfr[i] = (gains[None, :] * np.exp(
                -2j * np.pi * freqs[:, None] * (delays_ns[None, :] * 1e-9)
            )).sum(axis=1).astype(np.complex64)
        if paths_dump is not None:
            paths_dump.append([p.model_dump() for p in result.paths])

    # A dataset full of zero-path samples is garbage that *looks* like a
    # success (200 + files on disk). Detect and warn loudly — the usual cause
    # is a sampling region that extends outside the actual scene geometry.
    zero_count = int(np.count_nonzero(num_paths == 0))
    if zero_count == n:
        warnings.append(
            f"ALL {n} samples produced zero paths — the sampling region is "
            "almost certainly outside the scene geometry. Use the scene "
            "bounds (GET /scene/bounds or the UI's 'Fit to scene' button) "
            "to place the region, then regenerate."
        )
    elif zero_count > 0:
        warnings.append(
            f"{zero_count}/{n} samples produced zero paths (UE outside the "
            "scene or fully occluded); their cfr/labels are zero/NaN."
        )

    max_p = max((len(g) for g in gains_per_sample), default=0) or 1
    cir_gain = np.zeros((n, max_p), dtype=np.complex64)
    cir_delay = np.full((n, max_p), np.nan, dtype=np.float32)
    for i, (g, d) in enumerate(zip(gains_per_sample, delays_per_sample)):
        cir_gain[i, : len(g)] = g
        cir_delay[i, : len(d)] = d

    dataset_id = f"{request.name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    out_dir = project_dir / DATASETS_SUBDIR / dataset_id
    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = out_dir / "dataset.npz"
    np.savez_compressed(
        npz_path,
        positions_m=positions.astype(np.float32),
        tx_position_m=np.asarray(tx.position, dtype=np.float32),
        cfr=cfr,
        cfr_freq_offset_hz=freqs.astype(np.float64),
        cir_gain=cir_gain,
        cir_delay_ns=cir_delay,
        num_paths=num_paths,
        los=los,
        rss_dbm=rss_dbm,
        mean_delay_ns=mean_delay,
        rms_delay_spread_ns=rms_ds,
        k_factor_db=kfac,
    )
    metadata = {
        "schema_version": SCHEMA_VERSION,
        "name": request.name,
        "scene_id": scene.scene_id,
        "tx_id": tx.id,
        # Single-UE dataset: the whole .npz is one UE's sequence (id below).
        # source_rx_id is the scene RX whose antenna/identity was cloned into
        # it (None when the scene had no RX -> default isotropic antenna).
        "ue_id": ue.id,
        "source_rx_id": source_rx_id,
        "ue_antenna": ue.antenna.model_dump(),
        "frequency_hz": config.frequency_hz,
        "bandwidth_hz": config.bandwidth_hz,
        "num_cfr_points": k,
        "sampling": request.sampling.model_dump(),
        "config": config.model_dump(),
        "backend": backend.name,
        "engine": config.engine or "builtin",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "duration_s": round(time.monotonic() - started, 2),
        "num_zero_path_samples": zero_count,
        "conventions": {
            "coordinates": "Z-up ENU meters",
            "power": "dBm; cir_gain is linear voltage gain (|g|^2 = mW at 0 dBm tx ref)",
            "cfr": "H(f_k) = sum_l g_l exp(-j 2 pi f_k tau_l), offsets across [-B/2,+B/2]",
        },
        # Field mapping to NVIDIA AODT's ClickHouse/Parquet ground-truth schema
        # (cfrs/cirs/raypaths tables) for cross-tool pipelines. Our arrays are
        # per-link (antenna axes collapsed); a per-antenna-element export
        # matching AODT's ru_ant_el/ue_ant_el structs is a documented roadmap.
        "aodt_field_map": {
            "sample index": "time_idx",
            "tx": "ru_id",
            # Single UE per dataset: every positions_m row is the same UE
            # (metadata ue_id); AODT's ue_id is constant across the sequence.
            "ue (constant, = metadata ue_id)": "ue_id",
            "cfr (complex64)": "cfr_re + j*cfr_im",
            "cir_gain (complex64)": "cir_re + j*cir_im",
            "cir_delay_ns": "cir_delay",
            "rss_dbm / per-path power": "power_dB",
        },
        "warnings": warnings,
    }
    (out_dir / "metadata.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    if paths_dump is not None:
        with (out_dir / "paths.jsonl").open("w", encoding="utf-8") as f:
            for sample_paths in paths_dump:
                f.write(json.dumps(sample_paths) + "\n")

    files = sorted(p.name for p in out_dir.iterdir() if p.is_file())
    size = sum((out_dir / f).stat().st_size for f in files)
    return DatasetInfo(
        dataset_id=dataset_id, name=request.name, num_samples=n, num_cfr_points=k,
        created_at=metadata["created_at"], files=files, size_bytes=size,
        warnings=warnings, metadata={"duration_s": metadata["duration_s"],
                                      "backend": backend.name,
                                      "engine": metadata["engine"],
                                      "num_zero_path_samples": zero_count},
    )


def list_datasets(project_dir: Path) -> list[DatasetInfo]:
    root = project_dir / DATASETS_SUBDIR
    if not root.is_dir():
        return []
    out: list[DatasetInfo] = []
    for d in sorted(root.iterdir()):
        meta_path = d / "metadata.json"
        if not d.is_dir() or not meta_path.is_file():
            continue
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        files = sorted(p.name for p in d.iterdir() if p.is_file())
        npz = d / "dataset.npz"
        num = 0
        if npz.is_file():
            try:
                import numpy as np

                with np.load(npz) as z:
                    num = int(z["positions_m"].shape[0])
            except Exception:  # noqa: BLE001 - a corrupt npz must not 500 the list
                num = -1
        out.append(DatasetInfo(
            dataset_id=d.name, name=meta.get("name", d.name), num_samples=num,
            num_cfr_points=meta.get("num_cfr_points", 0),
            created_at=meta.get("created_at"), files=files,
            size_bytes=sum((d / f).stat().st_size for f in files),
            metadata={"duration_s": meta.get("duration_s"),
                      "backend": meta.get("backend"),
                      "engine": meta.get("engine")},
        ))
    return out


def dataset_file(project_dir: Path, dataset_id: str, filename: str) -> Optional[Path]:
    """Resolve a dataset file for download, refusing path escapes."""
    root = (project_dir / DATASETS_SUBDIR).resolve()
    target = (root / dataset_id / filename).resolve()
    # is_relative_to (not startswith) so a sibling dir sharing the prefix
    # (e.g. export/datasets_evil) can never be reached (audit minor).
    if not target.is_relative_to(root) or not target.is_file():
        return None
    return target
