"""Measured-vs-predicted trajectory validation (measurement round-trip).

Orders a measurement log by capture time, replays its RX positions as the
waypoints of the EXISTING trajectory solver (services/trajectory.py, one solve
per point so samples align 1:1), and scores per-point predicted path gain
against the measured values with the SAME level-offset alignment the material
calibration uses (services/calibration._stats). Closes the loop: import a
drive/flight log, predict along it, and see exactly where the twin diverges.

Nothing here persists a result set - the report is computed and returned.
"""

from pathlib import Path
from typing import TypeVar

from seam_studio.schemas.calibration import (
    MeasurementSample,
    TrajectoryValidationPoint,
    TrajectoryValidationReport,
    TrajectoryValidationRequest,
    TrajectoryValidationStats,
)
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.simulation import SimulationConfig, TrajectorySimulateRequest
from seam_studio.services.calibration import _stats
from seam_studio.services.simulation_backends.base import RayTracingBackend
from seam_studio.services.trajectory import run_trajectory

T = TypeVar("T")


def order_measurements(
    measurements: list[MeasurementSample],
) -> list[MeasurementSample]:
    """Time-order a measurement log.

    Samples carrying ``time_s`` sort ascending (stable: equal times keep file
    order); samples without a time keep their file order AFTER the timed ones.
    When no sample has a time, file order is preserved untouched - the sort is
    a no-op for today's time-less logs.
    """
    if not any(m.time_s is not None for m in measurements):
        return list(measurements)
    return sorted(
        measurements,
        key=lambda m: (m.time_s is None, m.time_s if m.time_s is not None else 0.0),
    )


def _subsample_evenly(items: list[T], max_points: int) -> list[T]:
    """At most ``max_points`` items, evenly spaced, first and last kept."""
    n = len(items)
    if n <= max_points:
        return list(items)
    if max_points == 1:
        return [items[0]]
    # Spacing > 1 whenever n > max_points, so the rounded indices are strictly
    # increasing - no duplicates.
    return [
        items[round(i * (n - 1) / (max_points - 1))] for i in range(max_points)
    ]


def validate_trajectory(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: TrajectoryValidationRequest,
    measurements: list[MeasurementSample],
) -> TrajectoryValidationReport:
    """Measured-vs-predicted path gain along the measurement log.

    ``measurements`` is the resolved sample list (the API layer substitutes
    the project's stored measurements when the request carries none);
    ``request`` contributes tx_id and max_points.
    """
    if not measurements:
        raise ValueError("measurements must not be empty")
    txs = [d for d in scene.devices if d.kind == "tx"]
    if not txs:
        raise ValueError("scene has no transmitter to validate against")
    tx = next((d for d in txs if d.id == request.tx_id), None) if request.tx_id else txs[0]
    if tx is None:
        raise ValueError(f"unknown tx device: {request.tx_id}")

    warnings: list[str] = []
    ordered = order_measurements(measurements)
    untimed = sum(1 for m in ordered if m.time_s is None)
    if 0 < untimed < len(ordered):
        warnings.append(
            f"{untimed} measurement(s) carry no time_s; they are ordered after "
            "the timed ones in file order"
        )
    mismatched = sum(1 for m in ordered if m.tx_id is not None and m.tx_id != tx.id)
    if mismatched:
        warnings.append(
            f"{mismatched} measurement(s) name a tx_id other than {tx.id!r}; "
            f"the whole log is validated against {tx.id!r}"
        )
    if len(ordered) > request.max_points:
        warnings.append(
            f"subsampled {len(ordered)} measurements evenly to "
            f"max_points={request.max_points}"
        )
        ordered = _subsample_evenly(ordered, request.max_points)

    # Replay the measured RX positions as explicit trajectory waypoints on the
    # existing solver: one solve per point, so result.samples aligns 1:1 with
    # ``ordered``. The config is pinned to the validated TX (the same
    # single-link framing as calibration's _simulate_path_gains) so no other
    # TX leaks into the prediction. num_points is ignored for explicit
    # waypoints but must still satisfy the request schema's bounds.
    traj_request = TrajectorySimulateRequest(
        waypoints=[[float(c) for c in m.rx_position] for m in ordered],
        serving_tx_id=tx.id,
        num_points=min(max(len(ordered), 2), 200),
    )
    cfg = config.model_copy(update={"tx_ids": [tx.id]})
    result = run_trajectory(backend, project_dir, scene, library, cfg, traj_request)
    warnings.extend(result.warnings)

    measured = [m.measured_path_gain_db for m in ordered]
    predicted = [s.path_gain_db for s in result.samples]
    # Level-offset alignment + RMSE/MAE: the exact math the material
    # calibration reports, so both loops read the same error metric.
    stats, _, idx = _stats(measured, predicted)
    excluded = len(ordered) - len(idx)
    if excluded:
        warnings.append(
            f"{excluded} point(s) produced zero paths and were excluded from "
            "the stats"
        )
    if not idx and ordered:
        # An all-excluded run must not read as a clean pass ("Validated 0
        # point(s); RMSE 0.00 dB" is what turned a path-dead scene into a
        # misfiled bug during verification).
        warnings.append(
            "no measurement point produced any ray path — the stats are "
            "empty, not a pass; check the per-point path counts and the "
            "trajectory warnings above"
        )

    solved = set(idx)
    points = [
        TrajectoryValidationPoint(
            index=i,
            time_s=ordered[i].time_s,
            position=ordered[i].rx_position,
            measured_db=measured[i],
            predicted_db=predicted[i] if i in solved else None,
            aligned_predicted_db=(
                predicted[i] + stats.level_offset_db if i in solved else None  # type: ignore[operator]
            ),
            error_db=(
                (predicted[i] + stats.level_offset_db) - measured[i]  # type: ignore[operator]
                if i in solved
                else None
            ),
            path_count=result.samples[i].path_count if i < len(result.samples) else 0,
        )
        for i in range(len(ordered))
    ]
    return TrajectoryValidationReport(
        tx_id=tx.id,
        points=points,
        stats=TrajectoryValidationStats(
            level_offset_db=stats.level_offset_db,
            rmse_db=stats.rmse_db,
            mean_abs_error_db=stats.mean_abs_error_db,
            n=stats.n_links,
        ),
        backend=backend.name,
        warnings=warnings,
    )
