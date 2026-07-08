"""Time-stepped multi-actor scenario simulation.

A scenario advances every trajectory-carrying actor frame by frame, moves any
devices attached to those actors, re-solves the RF paths, and records
per-frame LinkMetrics for every tx->rx pair (plus the actor/device states and,
optionally, the full ray paths).

Backend handling (pinned):
- Sionna: the RF projection is compiled ONCE up front; each frame reuses the
  cached scene and moves the actor SceneObjects via apply_actor_states before
  solving (fast: one Mitsuba load for the whole scenario).
- Mock: there is no persistent scene, so each frame gets a deep-copied scene
  with the actors' authored positions overwritten and is solved normally. The
  mock backend only uses actor/prim anchors loosely, which is acceptable.

Metrics mirror the moving-RX trajectory service: RSS/path gain/RMS delay
spread from the per-pair paths, and SINR == SNR = RSS - noise_floor (no
interference model yet).
"""

import math
from pathlib import Path
from typing import Optional

from app.schemas.actors import (
    ActorState,
    DeviceState,
    LinkMetrics,
    ScenarioFrame,
    ScenarioResultSet,
    ScenarioSimulateRequest,
)
from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import RayPath
from app.schemas.scene import Actor, Scene
from app.schemas.simulation import SimulationConfig
from app.services.simulation_backends.base import RayTracingBackend
from app.services.simulation_backends.sionna_backend import noise_floor_dbm


def _trajectory_param(traj, time_s: float) -> float:
    """Continuous waypoint parameter s in [0, n-1] at ``time_s``.

    s advances by 1 per trajectory dt. Modes:
    - once:     clamp at the ends;
    - loop:     wrap (…, n-2, n-1, 0, 1, …) - traversal jumps back to start;
    - pingpong: reflect at both ends (0..n-1..0..).
    """
    n = len(traj.waypoints)
    if n <= 1:
        return 0.0
    s = time_s / traj.dt_s
    mode = traj.resolved_mode()
    span = float(n - 1)
    if mode == "once":
        return max(0.0, min(s, span))
    if mode == "loop":
        return s % span
    # pingpong: triangle wave with period 2*span.
    period = 2.0 * span
    phase = s % period
    return phase if phase <= span else period - phase


def actor_position_at(actor: Actor, time_s: float) -> list[float]:
    """Actor base position at ``time_s``: LINEAR INTERPOLATION between the
    trajectory waypoints (smooth motion), honoring once/loop/pingpong modes.
    Actors without a trajectory stay at their authored position."""
    traj = actor.trajectory
    if traj is None or not traj.waypoints:
        return [float(c) for c in actor.position]
    if len(traj.waypoints) == 1:
        return [float(c) for c in traj.waypoints[0]]
    s = _trajectory_param(traj, time_s)
    i = int(math.floor(s))
    i = max(0, min(i, len(traj.waypoints) - 2))
    frac = s - i
    a, b = traj.waypoints[i], traj.waypoints[i + 1]
    return [float(a[k]) + (float(b[k]) - float(a[k])) * frac for k in range(3)]


def actor_heading_at(actor: Actor, time_s: float, eps_s: float = 1e-3) -> float:
    """Yaw (deg) facing the direction of travel; authored yaw when static."""
    p0 = actor_position_at(actor, time_s)
    p1 = actor_position_at(actor, time_s + eps_s)
    dx, dy = p1[0] - p0[0], p1[1] - p0[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return float(actor.orientation_deg[0])  # [yaw, pitch, roll]
    return math.degrees(math.atan2(dy, dx))


def actor_velocity_at(actor: Actor, time_s: float, eps_s: float = 1e-3) -> list[float]:
    """Actor velocity [m/s] (world frame, Z-up) at ``time_s``, from a central
    finite difference of the interpolated trajectory position (tangent x speed).
    All-zero for a static actor (no trajectory) or a degenerate step."""
    if actor.trajectory is None or not actor.trajectory.waypoints:
        return [0.0, 0.0, 0.0]
    p_before = actor_position_at(actor, max(0.0, time_s - eps_s))
    p_after = actor_position_at(actor, time_s + eps_s)
    span = (time_s + eps_s) - max(0.0, time_s - eps_s)
    if span <= 0.0:
        return [0.0, 0.0, 0.0]
    return [(p_after[a] - p_before[a]) / span for a in range(3)]


def _actor_states_at(scene: Scene, time_s: float) -> list[ActorState]:
    """ActorState for every actor at ``time_s`` (moving and static alike).

    Moving actors face their direction of travel (yaw from the trajectory
    tangent), like vehicles/pedestrians do."""
    states: list[ActorState] = []
    for actor in scene.actors:
        # orientation_deg convention is [yaw, pitch, roll]; only yaw follows
        # the travel direction, pitch/roll keep the authored values.
        yaw = actor_heading_at(actor, time_s)
        pitch = float(actor.orientation_deg[1])
        roll = float(actor.orientation_deg[2])
        states.append(
            ActorState(
                id=actor.id,
                position=actor_position_at(actor, time_s),
                orientation_deg=[yaw, pitch, roll],
            )
        )
    return states


def _device_states_at(
    scene: Scene, actor_states: list[ActorState]
) -> tuple[list[DeviceState], dict[str, list[float]]]:
    """Positions of devices attached to actors, translated by the actor's delta
    from its authored position. Returns the DeviceState list plus an id->pos map
    the mock path can splice into its scene copy."""
    state_by_actor = {s.id: s for s in actor_states}
    device_positions: dict[str, list[float]] = {}
    states: list[DeviceState] = []
    for actor in scene.actors:
        if not actor.attached_device_ids:
            continue
        state = state_by_actor.get(actor.id)
        if state is None:
            continue
        delta = [state.position[i] - float(actor.position[i]) for i in range(3)]
        for dev_id in actor.attached_device_ids:
            dev = scene.device_by_id(dev_id)
            if dev is None:
                continue
            moved = [float(dev.position[i]) + delta[i] for i in range(3)]
            device_positions[dev_id] = moved
            states.append(DeviceState(id=dev_id, position=moved))
    return states, device_positions


def _velocities_at(
    scene: Scene, time_s: float
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """(actor_velocities, device_velocities) at ``time_s`` [m/s], for every
    MOVING actor and any device attached to one. A device rides with its
    actor, so it inherits the actor's velocity. Static actors/devices are
    omitted (no key) so the caller only sets velocity where something moves."""
    actor_velocities: dict[str, list[float]] = {}
    device_velocities: dict[str, list[float]] = {}
    for actor in scene.actors:
        v = actor_velocity_at(actor, time_s)
        # Epsilon, not exact-float: a pingpong turnaround frame's central
        # difference can be ~0 without being bitwise zero (audit polish).
        if all(abs(c) < 1e-9 for c in v):
            continue
        actor_velocities[actor.id] = v
        for dev_id in actor.attached_device_ids:
            device_velocities[dev_id] = v
    return actor_velocities, device_velocities


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


def _pair_metrics(
    paths: list[RayPath],
    tx_id: str,
    rx_id: str,
    tx_power_dbm: float,
    noise_floor: float,
) -> LinkMetrics:
    """Aggregate a tx->rx link's paths into one LinkMetrics row.

    The serving link is (tx_id -> rx_id); every OTHER TX's power arriving at
    the same RX in this frame counts as co-channel interference (full-buffer),
    so sinr_db is a true S/(I+N) - same convention as channel analysis and
    trajectories."""
    pair = [p for p in paths if p.tx_id == tx_id and p.rx_id == rx_id]
    intf_lin = sum(
        10.0 ** (p.power_dbm / 10.0)
        for p in paths
        if p.rx_id == rx_id and p.tx_id != tx_id
    )
    interference = 10.0 * math.log10(intf_lin) if intf_lin > 0.0 else None
    if not pair:
        return LinkMetrics(
            tx_id=tx_id, rx_id=rx_id, interference_dbm=interference, path_count=0
        )
    lin = [10.0 ** (p.power_dbm / 10.0) for p in pair]
    total = sum(lin)
    rss = 10.0 * math.log10(total) if total > 0 else None
    pg = (rss - tx_power_dbm) if rss is not None else None
    intf_plus_noise = 10.0 ** (noise_floor / 10.0) + intf_lin
    sinr = (
        rss - 10.0 * math.log10(intf_plus_noise) if rss is not None else None
    )
    rms: Optional[float] = None
    if total > 0:
        delays = [p.delay_ns for p in pair]
        mean_tau = sum(w * t for w, t in zip(lin, delays)) / total
        var = sum(w * (t - mean_tau) ** 2 for w, t in zip(lin, delays)) / total
        rms = math.sqrt(max(var, 0.0))
    return LinkMetrics(
        tx_id=tx_id,
        rx_id=rx_id,
        rss_dbm=rss,
        path_gain_db=pg,
        interference_dbm=interference,
        sinr_db=sinr,
        rms_delay_spread_ns=rms,
        path_count=len(pair),
    )


def _solve_frame_paths(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    actor_states: list[ActorState],
    device_positions: dict[str, list[float]],
    actor_velocities: dict[str, list[float]],
    device_velocities: dict[str, list[float]],
):
    """Solve one frame's paths. Sionna moves the cached scene's actor objects;
    every other backend gets a deep-copied scene with actor (and attached
    device) positions overwritten. Actor/device velocities (from the trajectory
    tangent) are applied so moving actors and attached devices carry Doppler."""
    if backend.name == "sionna":
        # Attached devices still need to move: splice their positions into a
        # light scene copy (cheap vs. the Mitsuba solve) so the transmitters/
        # receivers are placed correctly, while actors move via SceneObjects.
        # Attached-device velocity rides on the same copy (RadioDevice.velocity).
        frame_scene = scene
        if device_positions or device_velocities:
            frame_scene = scene.model_copy(deep=True)
            for dev in frame_scene.devices:
                if dev.id in device_positions:
                    dev.position = [float(c) for c in device_positions[dev.id]]
                if dev.id in device_velocities:
                    dev.velocity_m_s = [float(c) for c in device_velocities[dev.id]]
        return backend.simulate_paths(
            project_dir, frame_scene, library, config,
            actor_states=actor_states, actor_velocities=actor_velocities or None,
        )
    # Mock / other backends: move actor authored positions in a scene copy.
    frame_scene = scene.model_copy(deep=True)
    state_by_id = {s.id: s for s in actor_states}
    for actor in frame_scene.actors:
        st = state_by_id.get(actor.id)
        if st is not None:
            actor.position = [float(c) for c in st.position]
            actor.orientation_deg = [float(a) for a in st.orientation_deg]
    for dev in frame_scene.devices:
        if dev.id in device_positions:
            dev.position = [float(c) for c in device_positions[dev.id]]
        if dev.id in device_velocities:
            dev.velocity_m_s = [float(c) for c in device_velocities[dev.id]]
    return backend.simulate_paths(project_dir, frame_scene, library, config)


def run_scenario(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: ScenarioSimulateRequest,
) -> ScenarioResultSet:
    # Honor the config's device filters so the frame link tables cover exactly
    # the solved tx->rx pairs (the backend already filters by tx_ids/rx_ids).
    txs = [
        d for d in scene.devices
        if d.kind == "tx" and (config.tx_ids is None or d.id in config.tx_ids)
    ]
    rxs = [
        d for d in scene.devices
        if d.kind == "rx" and (config.rx_ids is None or d.id in config.rx_ids)
    ]
    tx_power = {d.id: d.power_dbm for d in txs}
    noise_floor = noise_floor_dbm(config)

    # Sionna: compile the RF projection ONCE up front so every frame reuses the
    # cached Mitsuba scene (the per-frame apply_actor_states just nudges objects).
    warnings: list[str] = []
    if backend.name == "sionna":
        compile_result = backend.compile(project_dir, scene, library)
        if not compile_result.ok:
            warnings.append(
                "scenario compile failed: "
                + "; ".join(compile_result.errors or ["unknown compile error"])
            )
        else:
            warnings.extend(compile_result.warnings)

    if not scene.actors:
        warnings.append("scene has no actors; scenario frames are static")

    frames: list[ScenarioFrame] = []
    frame_doppler_spread: list[Optional[float]] = []
    for i in range(request.num_frames):
        t = i * request.dt_s
        actor_states = _actor_states_at(scene, t)
        device_states, device_positions = _device_states_at(scene, actor_states)
        actor_velocities, device_velocities = _velocities_at(scene, t)

        result = _solve_frame_paths(
            backend, project_dir, scene, library, config,
            actor_states, device_positions,
            actor_velocities, device_velocities,
        )
        if i == 0:
            warnings.extend(result.warnings)
        # Per-frame Doppler spread [Hz]: power-weighted std of per-path Doppler
        # across every path in the frame (backend supplies doppler_hz aligned to
        # result.paths). None when nothing moves or the backend has no Doppler.
        raw_doppler = result.metadata.get("doppler_hz")
        if isinstance(raw_doppler, list) and len(raw_doppler) == len(result.paths):
            frame_doppler_spread.append(
                _doppler_spread_hz(
                    [p.power_dbm for p in result.paths], raw_doppler
                )
            )
        else:
            frame_doppler_spread.append(None)

        links = [
            _pair_metrics(result.paths, tx.id, rx.id, tx_power.get(tx.id, 0.0), noise_floor)
            for tx in txs
            for rx in rxs
        ]
        frames.append(
            ScenarioFrame(
                time_s=t,
                actor_states=actor_states,
                device_states=device_states,
                links=links,
                paths=list(result.paths) if request.include_paths else None,
            )
        )

    return ScenarioResultSet(
        result_id="unsaved",
        backend=backend.name,
        simulation_config_id=config.id,
        frames=frames,
        warnings=warnings,
        metadata={
            "frequency_hz": config.frequency_hz,
            "num_frames": request.num_frames,
            "dt_s": request.dt_s,
            "num_actors": len(scene.actors),
            "engine": backend.name,
            # Per-frame Doppler spread [Hz] aligned to ``frames``. Omitted when
            # no frame produced a Doppler value (mock output stays unchanged).
            **(
                {"doppler_spread_hz": frame_doppler_spread}
                if any(s is not None for s in frame_doppler_spread)
                else {}
            ),
        },
    )
