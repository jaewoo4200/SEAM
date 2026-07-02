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


def actor_position_at(actor: Actor, time_s: float) -> list[float]:
    """Actor base position at ``time_s`` (MVP: nearest waypoint, no interp).

    Pinned rule: waypoint index = floor(t / trajectory.dt_s); clamp to the last
    waypoint, or wrap when ``loop`` is set. Actors without a trajectory (or with
    an empty one) stay at their authored position.
    """
    traj = actor.trajectory
    if traj is None or not traj.waypoints:
        return [float(c) for c in actor.position]
    idx = int(math.floor(time_s / traj.dt_s))
    n = len(traj.waypoints)
    if traj.loop:
        idx %= n
    else:
        idx = max(0, min(idx, n - 1))
    return [float(c) for c in traj.waypoints[idx]]


def _actor_states_at(scene: Scene, time_s: float) -> list[ActorState]:
    """ActorState for every actor at ``time_s`` (moving and static alike)."""
    states: list[ActorState] = []
    for actor in scene.actors:
        states.append(
            ActorState(
                id=actor.id,
                position=actor_position_at(actor, time_s),
                orientation_deg=[float(a) for a in actor.orientation_deg],
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


def _pair_metrics(
    paths: list[RayPath],
    tx_id: str,
    rx_id: str,
    tx_power_dbm: float,
    noise_floor: float,
) -> LinkMetrics:
    """Aggregate a tx->rx link's paths into one LinkMetrics row (RSS/PG/SINR/
    RMS delay spread/path count), mirroring the trajectory aggregation."""
    pair = [p for p in paths if p.tx_id == tx_id and p.rx_id == rx_id]
    if not pair:
        return LinkMetrics(tx_id=tx_id, rx_id=rx_id, path_count=0)
    lin = [10.0 ** (p.power_dbm / 10.0) for p in pair]
    total = sum(lin)
    rss = 10.0 * math.log10(total) if total > 0 else None
    pg = (rss - tx_power_dbm) if rss is not None else None
    sinr = (rss - noise_floor) if rss is not None else None
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
):
    """Solve one frame's paths. Sionna moves the cached scene's actor objects;
    every other backend gets a deep-copied scene with actor (and attached
    device) positions overwritten."""
    if backend.name == "sionna":
        # Attached devices still need to move: splice their positions into a
        # light scene copy (cheap vs. the Mitsuba solve) so the transmitters/
        # receivers are placed correctly, while actors move via SceneObjects.
        frame_scene = scene
        if device_positions:
            frame_scene = scene.model_copy(deep=True)
            for dev in frame_scene.devices:
                if dev.id in device_positions:
                    dev.position = [float(c) for c in device_positions[dev.id]]
        return backend.simulate_paths(
            project_dir, frame_scene, library, config, actor_states=actor_states
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
    return backend.simulate_paths(project_dir, frame_scene, library, config)


def run_scenario(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: ScenarioSimulateRequest,
) -> ScenarioResultSet:
    txs = [d for d in scene.devices if d.kind == "tx"]
    rxs = [d for d in scene.devices if d.kind == "rx"]
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
    for i in range(request.num_frames):
        t = i * request.dt_s
        actor_states = _actor_states_at(scene, t)
        device_states, device_positions = _device_states_at(scene, actor_states)

        result = _solve_frame_paths(
            backend, project_dir, scene, library, config,
            actor_states, device_positions,
        )
        if i == 0:
            warnings.extend(result.warnings)

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
        },
    )
