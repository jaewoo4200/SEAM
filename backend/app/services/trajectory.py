"""Moving-RX (UE) trajectory RF metrics.

Solves ray paths with one RX stepped along a set of waypoints and aggregates
per-waypoint metrics (RSS, path gain, RMS delay spread, path count) — the
per-trajectory-point metrics the FTC repro/eval workflow reports. Backend
agnostic: any RayTracingBackend that produces PathResultSet works.
"""

import math
from pathlib import Path
from typing import Optional

from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import TrajectoryResultSet, TrajectorySample
from app.schemas.scene import Scene
from app.schemas.simulation import SimulationConfig, TrajectorySimulateRequest
from app.services.simulation_backends.base import UNSAVED_RESULT_ID, RayTracingBackend
from app.services.simulation_backends.sionna_backend import noise_floor_dbm


def resolve_waypoints(request: TrajectorySimulateRequest) -> list[list[float]]:
    if request.waypoints:
        return [[float(c) for c in wp] for wp in request.waypoints]
    if request.start_m and request.end_m:
        n = request.num_points
        start, end = request.start_m, request.end_m
        return [
            [start[a] + (end[a] - start[a]) * i / (n - 1) for a in range(3)]
            for i in range(n)
        ]
    raise ValueError("trajectory needs either 'waypoints' or 'start_m'+'end_m'")


def _aggregate(powers_dbm: list[float], delays_ns: list[float], tx_power_dbm: float):
    """RSS, path gain, RMS delay spread from per-path power/delay."""
    if not powers_dbm:
        return None, None, None, None
    lin = [10.0 ** (p / 10.0) for p in powers_dbm]
    total = sum(lin)
    rss_dbm = 10.0 * math.log10(total) if total > 0 else None
    path_gain_db = (rss_dbm - tx_power_dbm) if rss_dbm is not None else None
    # Power-weighted mean and RMS delay spread.
    mean_tau = sum(w * t for w, t in zip(lin, delays_ns)) / total
    var = sum(w * (t - mean_tau) ** 2 for w, t in zip(lin, delays_ns)) / total
    rms = math.sqrt(max(var, 0.0))
    strongest_delay = delays_ns[max(range(len(lin)), key=lambda i: lin[i])]
    return rss_dbm, path_gain_db, rms, strongest_delay


def _waypoint_velocity(
    waypoints: list[list[float]], i: int, dt_s: float
) -> list[float]:
    """UE velocity [m/s] at waypoint ``i`` from a finite difference of adjacent
    waypoints over dt. Forward difference in the interior/start, backward at the
    last point; zero when there is only one waypoint or dt is degenerate."""
    n = len(waypoints)
    if n < 2 or dt_s <= 0.0:
        return [0.0, 0.0, 0.0]
    j = i + 1 if i + 1 < n else i  # backward diff at the last point
    k = j - 1
    return [(waypoints[j][a] - waypoints[k][a]) / dt_s for a in range(3)]


def _doppler_spread_hz(powers_dbm: list[float], doppler_hz: list[float]):
    """Power-weighted std of per-path Doppler [Hz]; None if misaligned/empty."""
    if not powers_dbm or len(powers_dbm) != len(doppler_hz):
        return None
    lin = [10.0 ** (p / 10.0) for p in powers_dbm]
    total = sum(lin)
    if total <= 0.0:
        return None
    mean = sum(w * d for w, d in zip(lin, doppler_hz)) / total
    var = sum(w * (d - mean) ** 2 for w, d in zip(lin, doppler_hz)) / total
    return math.sqrt(max(var, 0.0))


def run_trajectory(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: TrajectorySimulateRequest,
) -> TrajectoryResultSet:
    rxs = [d for d in scene.devices if d.kind == "rx"]
    if not rxs:
        raise ValueError("scene has no receiver to move along a trajectory")
    ue_id = request.ue_id or rxs[0].id
    if not any(d.id == ue_id for d in rxs):
        raise ValueError(f"unknown rx device: {ue_id}")

    waypoints = resolve_waypoints(request)
    txs = [d for d in scene.devices if d.kind == "tx"]
    tx_power = txs[0].power_dbm if txs else 0.0

    # SNR reference floor (thermal + NF). No interference model yet, so the
    # reported sinr_db is really an SNR = rss_dbm - noise_floor.
    noise_floor = noise_floor_dbm(config)

    warnings: list[str] = []
    if request.follow_terrain:
        from app.services.terrain import snap_to_terrain

        waypoints = snap_to_terrain(
            project_dir, scene, waypoints, request.follow_height_m, warnings
        )
    samples: list[TrajectorySample] = []
    doppler_spreads: list[Optional[float]] = []
    for i, wp in enumerate(waypoints):
        # Solve with the UE parked at this waypoint; only this RX is active. Its
        # velocity is the finite difference of adjacent waypoints over dt, so
        # the solved paths carry the moving-UE Doppler (effect #2 in
        # docs/dynamic_scattering.md). Velocity does not move the RX geometry.
        ue_velocity = _waypoint_velocity(waypoints, i, request.dt_s)
        step_scene = scene.model_copy(deep=True)
        for dev in step_scene.devices:
            if dev.id == ue_id:
                dev.position = [float(c) for c in wp]
                dev.velocity_m_s = ue_velocity
        step_cfg = config.model_copy(update={"rx_ids": [ue_id]})
        result = backend.simulate_paths(project_dir, step_scene, library, step_cfg)
        if i == 0:
            warnings.extend(result.warnings)
        powers = [p.power_dbm for p in result.paths]
        delays = [p.delay_ns for p in result.paths]
        rss, gain, rms, strongest = _aggregate(powers, delays, tx_power)
        # Per-waypoint Doppler spread (power-weighted std of per-path Doppler),
        # from the backend's per-path doppler_hz (aligned to result.paths).
        raw_doppler = result.metadata.get("doppler_hz")
        spread = (
            _doppler_spread_hz(powers, raw_doppler)
            if isinstance(raw_doppler, list)
            else None
        )
        doppler_spreads.append(spread)
        sinr = (rss - noise_floor) if rss is not None else None
        frame_paths = None
        if request.include_paths:
            # Strongest-first, capped so playback payloads stay bounded even
            # on long trajectories (the viewer filters further client-side).
            frame_paths = sorted(
                result.paths, key=lambda p: p.power_dbm, reverse=True
            )[:100]
        samples.append(
            TrajectorySample(
                time_s=i * request.dt_s,
                ue_id=ue_id,
                position=[float(c) for c in wp],
                rss_dbm=rss,
                path_gain_db=gain,
                sinr_db=sinr,  # SNR (no interference model); rss - noise_floor
                rms_delay_spread_ns=rms,
                path_count=len(result.paths),
                strongest_delay_ns=strongest,
                paths=frame_paths,
            )
        )

    # A trajectory that mostly walks out of the scene looks like a successful
    # run with NaN/0 samples; make that loud (mirrors the dataset guard).
    zero_count = sum(1 for s in samples if s.path_count == 0)
    if zero_count == len(samples) and samples:
        warnings.append(
            f"ALL {len(samples)} waypoints produced zero paths — the trajectory "
            "is almost certainly outside the scene geometry. Pick the endpoints "
            "in the viewport or use scene bounds."
        )
    elif zero_count > len(samples) / 2:
        warnings.append(
            f"{zero_count}/{len(samples)} waypoints produced zero paths (UE "
            "outside the scene or fully occluded)"
        )

    return TrajectoryResultSet(
        result_id=UNSAVED_RESULT_ID,
        backend=backend.name,
        simulation_config_id=config.id,
        ue_id=ue_id,
        samples=samples,
        warnings=warnings,
        metadata={
            "frequency_hz": config.frequency_hz,
            "num_waypoints": len(waypoints),
            "engine": backend.name,
            # Per-waypoint Doppler spread [Hz] (power-weighted std of per-path
            # Doppler), aligned to ``samples``. None where the backend does not
            # model Doppler or the UE is momentarily stationary. Omitted when no
            # waypoint produced a Doppler value (keeps mock output unchanged).
            **(
                {"doppler_spread_hz": doppler_spreads}
                if any(s is not None for s in doppler_spreads)
                else {}
            ),
        },
    )
