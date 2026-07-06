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


def resample_polyline(waypoints: list[list[float]], n: int) -> list[list[float]]:
    """Resample a waypoint polyline to exactly ``n`` positions, equally spaced
    by arc length (linear interpolation along the segments). A 2-point polyline
    yields the same straight line the single-UE start/end path produces; a
    zero-length polyline (all waypoints coincident) collapses to n copies of
    the first point."""
    wps = [[float(c) for c in wp] for wp in waypoints]
    if n <= 1:
        return [list(wps[0])]
    # Cumulative arc length at each waypoint.
    cum = [0.0]
    for a, b in zip(wps, wps[1:]):
        cum.append(cum[-1] + math.dist(a, b))
    total = cum[-1]
    if total <= 0.0:
        return [list(wps[0]) for _ in range(n)]
    out: list[list[float]] = []
    seg = 0
    for i in range(n):
        target = total * i / (n - 1)
        # Advance to the segment [cum[seg], cum[seg+1]] containing target.
        while seg < len(cum) - 2 and cum[seg + 1] < target:
            seg += 1
        span = cum[seg + 1] - cum[seg]
        t = (target - cum[seg]) / span if span > 0.0 else 0.0
        a, b = wps[seg], wps[seg + 1]
        out.append([a[k] + (b[k] - a[k]) * t for k in range(3)])
    return out


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


def _sample_from_result(
    result,
    *,
    time_s: float,
    ue_id: str,
    position: list[float],
    rx_id: Optional[str],
    serving_tx,
    tx_power: float,
    noise_floor: float,
    include_paths: bool,
) -> tuple[TrajectorySample, Optional[float]]:
    """Build one TrajectorySample (and its Doppler spread) for ``ue_id`` from a
    solved PathResultSet. When ``rx_id`` is given the paths are first filtered
    to that receiver (multi-UE: one solve carries every routed UE's paths); the
    serving/interference split and metric math are identical to the single-UE
    path. Returns (sample, doppler_spread)."""
    ue_paths = [p for p in result.paths if rx_id is None or p.rx_id == rx_id]
    # Serving-link metrics come from the serving TX's paths only; the other
    # TXs' received power is co-channel interference (full-buffer).
    serving_paths = [
        p for p in ue_paths if serving_tx is None or p.tx_id == serving_tx.id
    ]
    intf_lin = sum(
        10.0 ** (p.power_dbm / 10.0)
        for p in ue_paths
        if serving_tx is not None and p.tx_id != serving_tx.id
    )
    interference = 10.0 * math.log10(intf_lin) if intf_lin > 0.0 else None
    powers = [p.power_dbm for p in serving_paths]
    delays = [p.delay_ns for p in serving_paths]
    rss, gain, rms, strongest = _aggregate(powers, delays, tx_power)
    # Per-waypoint Doppler spread (power-weighted std of per-path Doppler), from
    # the backend's per-path doppler_hz (aligned to result.paths). Only defined
    # for the single-UE path where result.paths carries exactly this UE.
    spread: Optional[float] = None
    if rx_id is None:
        raw_doppler = result.metadata.get("doppler_hz")
        spread = (
            _doppler_spread_hz([p.power_dbm for p in result.paths], raw_doppler)
            if isinstance(raw_doppler, list)
            else None
        )
    # SINR over interference + noise; equals SNR when nothing interferes.
    intf_plus_noise = 10.0 ** (noise_floor / 10.0) + intf_lin
    sinr = rss - 10.0 * math.log10(intf_plus_noise) if rss is not None else None
    frame_paths = None
    if include_paths:
        # Strongest-first, capped so playback payloads stay bounded even on long
        # trajectories (the viewer filters further client-side).
        frame_paths = sorted(ue_paths, key=lambda p: p.power_dbm, reverse=True)[:100]
    sample = TrajectorySample(
        time_s=time_s,
        ue_id=ue_id,
        position=[float(c) for c in position],
        rss_dbm=rss,
        path_gain_db=gain,
        interference_dbm=interference,
        sinr_db=sinr,  # S/(I+N); equals SNR when nothing interferes
        rms_delay_spread_ns=rms,
        # Serving-link path count (interferer paths still render via
        # frame_paths, which keeps every TX's rays for playback).
        path_count=len(serving_paths),
        strongest_delay_ns=strongest,
        paths=frame_paths,
    )
    return sample, spread


def _run_trajectory_routes(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: TrajectorySimulateRequest,
) -> TrajectoryResultSet:
    """Multi-UE trajectory: every route's polyline is resampled to num_points
    steps by arc length; per step all routed UEs move to their step positions
    and ONE paths solve (all TXs, rx_ids = routed UEs) yields each UE's metrics.
    Samples are STEP-MAJOR, UE in routes order."""
    routes = request.routes or []
    if not routes:
        raise ValueError("routes must contain at least one UE route")
    rxs = [d for d in scene.devices if d.kind == "rx"]
    rx_ids = {d.id for d in rxs}
    ue_ids = [r.ue_id for r in routes]
    if len(set(ue_ids)) != len(ue_ids):
        raise ValueError("duplicate ue_id in routes")
    for uid in ue_ids:
        if uid not in rx_ids:
            raise ValueError(f"unknown rx device: {uid}")

    txs = [d for d in scene.devices if d.kind == "tx"]
    serving_tx = next(
        (d for d in txs if d.id == request.serving_tx_id),
        txs[0] if txs else None,
    )
    if request.serving_tx_id and (
        serving_tx is None or serving_tx.id != request.serving_tx_id
    ):
        raise ValueError(f"unknown tx device: {request.serving_tx_id}")
    tx_power = serving_tx.power_dbm if serving_tx else 0.0
    noise_floor = noise_floor_dbm(config)

    n = request.num_points
    # Resample each route to exactly num_points positions by arc length.
    positions: list[list[list[float]]] = [
        resample_polyline(r.waypoints, n) for r in routes
    ]
    warnings: list[str] = []
    # Sionna applies ONE scene-level rx_array, taken from the FIRST selected RX
    # device's antenna (sionna_backend._apply_arrays: rt_scene.rx_array =
    # rxs[0].antenna, where rxs is filtered from scene.devices in SCENE order,
    # not routes order). So when routed UEs carry non-identical antenna configs,
    # only that first UE's antenna is honored for every UE. The mock backend is
    # per-device isotropic and unaffected; warn regardless so the result is
    # honest about the sionna solve it stands in for. Name the applied UE by
    # scene order so the message matches what sionna actually does.
    routed = set(ue_ids)
    ue_devices = {d.id: d for d in rxs}  # rxs preserves scene device order
    applied_uid = next(d.id for d in rxs if d.id in routed)
    applied_antenna = ue_devices[applied_uid].antenna
    differing = [
        d.id
        for d in rxs
        if d.id in routed
        and d.id != applied_uid
        and d.antenna != applied_antenna
    ]
    if differing:
        warnings.append(
            f"sionna applies rx antenna of '{applied_uid}' to all routed UEs "
            f"(scene-level array); differing antennas on "
            f"{', '.join(differing)} are not individually honored"
        )
    if request.follow_terrain:
        from app.services.terrain import snap_to_terrain

        positions = [
            snap_to_terrain(
                project_dir, scene, pos, request.follow_height_m, warnings
            )
            for pos in positions
        ]

    samples: list[TrajectorySample] = []
    for step in range(n):
        # Move ALL routed UEs to their step positions in one scene, with each
        # UE's finite-difference velocity so solved paths carry moving-UE
        # Doppler (velocity does not move the RX geometry).
        step_scene = scene.model_copy(deep=True)
        step_positions = {ue_ids[u]: positions[u][step] for u in range(len(routes))}
        for u in range(len(routes)):
            uid = ue_ids[u]
            vel = _waypoint_velocity(positions[u], step, request.dt_s)
            for dev in step_scene.devices:
                if dev.id == uid:
                    dev.position = [float(c) for c in positions[u][step]]
                    dev.velocity_m_s = vel
        step_cfg = config.model_copy(update={"rx_ids": ue_ids})
        result = backend.simulate_paths(project_dir, step_scene, library, step_cfg)
        if step == 0:
            warnings.extend(result.warnings)
        for u in range(len(routes)):
            uid = ue_ids[u]
            sample, _ = _sample_from_result(
                result,
                time_s=step * request.dt_s,
                ue_id=uid,
                position=step_positions[uid],
                rx_id=uid,
                serving_tx=serving_tx,
                tx_power=tx_power,
                noise_floor=noise_floor,
                include_paths=request.include_paths,
            )
            samples.append(sample)

    zero_count = sum(1 for s in samples if s.path_count == 0)
    if zero_count == len(samples) and samples:
        warnings.append(
            f"ALL {len(samples)} samples produced zero paths — the routes are "
            "almost certainly outside the scene geometry. Pick the endpoints "
            "in the viewport or use scene bounds."
        )
    elif zero_count > len(samples) / 2:
        warnings.append(
            f"{zero_count}/{len(samples)} samples produced zero paths (UE "
            "outside the scene or fully occluded)"
        )

    return TrajectoryResultSet(
        result_id=UNSAVED_RESULT_ID,
        backend=backend.name,
        simulation_config_id=config.id,
        ue_id=ue_ids[0],
        samples=samples,
        warnings=warnings,
        metadata={
            "frequency_hz": config.frequency_hz,
            "num_waypoints": n,
            "engine": backend.name,
            "ue_ids": list(ue_ids),
            "num_steps": n,
        },
    )


def run_trajectory(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: TrajectorySimulateRequest,
) -> TrajectoryResultSet:
    if request.routes is not None:
        return _run_trajectory_routes(
            backend, project_dir, scene, library, config, request
        )
    rxs = [d for d in scene.devices if d.kind == "rx"]
    if not rxs:
        raise ValueError("scene has no receiver to move along a trajectory")
    ue_id = request.ue_id or rxs[0].id
    if not any(d.id == ue_id for d in rxs):
        raise ValueError(f"unknown rx device: {ue_id}")

    waypoints = resolve_waypoints(request)
    txs = [d for d in scene.devices if d.kind == "tx"]
    # Serving TX: explicit id or the first TX. Every OTHER TX's power at the
    # UE counts as co-channel interference in the per-sample SINR.
    serving_tx = next(
        (d for d in txs if d.id == request.serving_tx_id),
        txs[0] if txs else None,
    )
    if request.serving_tx_id and (serving_tx is None or serving_tx.id != request.serving_tx_id):
        raise ValueError(f"unknown tx device: {request.serving_tx_id}")
    tx_power = serving_tx.power_dbm if serving_tx else 0.0

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
        # Single-UE: rx_id=None keeps the legacy behavior (metrics over the whole
        # solve, per-path Doppler spread from result.metadata).
        sample, spread = _sample_from_result(
            result,
            time_s=i * request.dt_s,
            ue_id=ue_id,
            position=wp,
            rx_id=None,
            serving_tx=serving_tx,
            tx_power=tx_power,
            noise_floor=noise_floor,
            include_paths=request.include_paths,
        )
        doppler_spreads.append(spread)
        samples.append(sample)

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
