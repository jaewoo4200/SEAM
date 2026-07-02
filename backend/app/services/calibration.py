"""Measurement-based material calibration (grid search).

Given measured per-link path gain, this:
1. simulates path gain at each measured RX position with the current material;
2. computes a level offset (absorbs unknown absolute TX power / cable loss) and
   the residual RMSE / MAE — the shape error;
3. sweeps one material parameter over a grid, re-simulating, and picks the
   value that minimizes the level-aligned RMSE;
4. reports before/after and optionally writes the fitted value back.

Grid search is backend-agnostic and robust (no gradients). The differentiable
Adam fit over Sionna's material parameters is the documented next step for
finer, multi-parameter calibration — see docs/roadmap.md.
"""

import math
from pathlib import Path
from typing import Optional

from app.schemas.calibration import (
    CalibrationReport,
    CalibrationRequest,
    CalibrationStats,
    LinkError,
)
from app.schemas.materials import RFMaterialLibrary
from app.schemas.scene import Scene
from app.schemas.simulation import SimulationConfig
from app.services.simulation_backends.base import RayTracingBackend

_DEFAULT_GRIDS: dict[str, list[float]] = {
    "scattering_coefficient": [0.0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.7],
    "relative_permittivity": [2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0],
    "conductivity_s_per_m": [0.001, 0.005, 0.01, 0.05, 0.1, 0.5, 1.0],
}


def _simulate_path_gains(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: CalibrationRequest,
) -> list[Optional[float]]:
    """Path gain (dB) at each measured RX position: PG = RSS_dbm - tx_power."""
    rxs = [d for d in scene.devices if d.kind == "rx"]
    txs = [d for d in scene.devices if d.kind == "tx"]
    if not rxs or not txs:
        return [None] * len(request.measurements)
    gains: list[Optional[float]] = []
    for m in request.measurements:
        tx = next((d for d in txs if d.id == m.tx_id), txs[0])
        step = scene.model_copy(deep=True)
        # Park the first RX at the measured position; solve that single link.
        rx_id = rxs[0].id
        for d in step.devices:
            if d.id == rx_id:
                d.position = [float(c) for c in m.rx_position]
        cfg = config.model_copy(update={"rx_ids": [rx_id], "tx_ids": [tx.id]})
        result = backend.simulate_paths(project_dir, step, library, cfg)
        if not result.paths:
            gains.append(None)
            continue
        lin = sum(10.0 ** (p.power_dbm / 10.0) for p in result.paths)
        rss = 10.0 * math.log10(lin) if lin > 0 else None
        gains.append(None if rss is None else rss - tx.power_dbm)
    return gains


def _stats(measured: list[float], simulated: list[Optional[float]]) -> tuple[CalibrationStats, list[LinkError], list[int]]:
    pairs = [(i, mm, ss) for i, (mm, ss) in enumerate(zip(measured, simulated)) if ss is not None]
    idx = [i for i, _, _ in pairs]
    if not pairs:
        return CalibrationStats(n_links=0, level_offset_db=0.0, rmse_db=0.0, mean_abs_error_db=0.0), [], idx
    offset = sum(mm - ss for _, mm, ss in pairs) / len(pairs)  # align sim -> meas
    residuals = [(ss + offset) - mm for _, mm, ss in pairs]
    rmse = math.sqrt(sum(r * r for r in residuals) / len(residuals))
    mae = sum(abs(r) for r in residuals) / len(residuals)
    return (
        CalibrationStats(
            n_links=len(pairs),
            level_offset_db=offset,
            rmse_db=rmse,
            mean_abs_error_db=mae,
        ),
        [],  # per-link filled by the caller for the chosen (after) run
        idx,
    )


def calibrate_material(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: CalibrationRequest,
) -> CalibrationReport:
    mat = library.get(request.target_material_id)
    if mat is None:
        raise ValueError(f"unknown target material: {request.target_material_id}")
    measured = [m.measured_path_gain_db for m in request.measurements]
    warnings: list[str] = []

    baseline_value = getattr(mat, request.param, None)
    base_sim = _simulate_path_gains(backend, project_dir, scene, library, config, request)
    before, _, _ = _stats(measured, base_sim)

    grid = request.grid or _DEFAULT_GRIDS[request.param]
    grid_rmse: list[float] = []
    best_value: Optional[float] = None
    best_rmse = float("inf")
    best_sim = base_sim
    for value in grid:
        trial = library.model_copy(deep=True)
        tmat = trial.get(request.target_material_id)
        setattr(tmat, request.param, value)
        sim = _simulate_path_gains(backend, project_dir, scene, library=trial, config=config, request=request)
        stats, _, _ = _stats(measured, sim)
        grid_rmse.append(stats.rmse_db)
        if stats.n_links > 0 and stats.rmse_db < best_rmse:
            best_rmse, best_value, best_sim = stats.rmse_db, value, sim

    after, _, idx = _stats(measured, best_sim)
    per_link = [
        LinkError(
            rx_position=request.measurements[i].rx_position,
            measured_path_gain_db=measured[i],
            simulated_path_gain_db=best_sim[i],  # type: ignore[arg-type]
            error_db=(best_sim[i] + after.level_offset_db) - measured[i],  # type: ignore[operator]
        )
        for i in idx
    ]
    if before.n_links == 0:
        warnings.append("no links produced paths; check device/geometry setup")
    if best_value is not None and abs(best_rmse - before.rmse_db) < 1e-9:
        warnings.append(
            "grid search found no material sensitivity (the mock backend ignores "
            "material EM params — use the sionna backend for a real fit)"
        )

    applied = False
    if request.apply and best_value is not None:
        applied = True  # caller persists; report signals intent

    return CalibrationReport(
        target_material_id=request.target_material_id,
        param=request.param,
        baseline_value=baseline_value,
        fitted_value=best_value,
        before=before,
        after=after,
        grid_values=list(grid),
        grid_rmse_db=grid_rmse,
        per_link_after=per_link,
        applied=applied,
        backend=backend.name,
        warnings=warnings,
    )
