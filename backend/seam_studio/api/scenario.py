"""Multi-actor scenario simulation and live state-sync endpoints.

POST /projects/{pid}/simulate/scenario  -> ScenarioResultSet (persisted)
GET  /projects/{pid}/results/scenario   -> stored ScenarioResultSet
POST /projects/{pid}/live/state         -> LiveStateResponse

The scenario endpoint runs the time-stepped actor simulation and stores the
result with the same "<backend>_scenario_<nnn>" convention every other result
uses (via api.simulate._persist_result). The live/state endpoint applies an
external real-world push (GPS/mocap positions) to the loaded scene, optionally
persisting it and/or re-solving to return fresh link metrics - the closed-loop
digital-twin primitive.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from seam_studio.api.deps import get_store, load_scene_or_404
from seam_studio.api.simulate import _load_result, _persist_result, _resolve_config
from seam_studio.schemas.actors import (
    DeviceState,
    LinkMetrics,
    LiveStateResponse,
    LiveStateUpdate,
    ScenarioResultSet,
    ScenarioSimulateRequest,
)
from seam_studio.schemas.simulation import SimulateRequest, SimulationConfig
from seam_studio.services.scenario import _pair_metrics, run_scenario
from seam_studio.services.simulation_backends import BackendUnavailableError, resolve_backend
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm

router = APIRouter(tags=["scenario"])


@router.post(
    "/projects/{project_id}/simulate/scenario", response_model=ScenarioResultSet
)
def simulate_scenario(
    project_id: str, request: Optional[ScenarioSimulateRequest] = None
) -> ScenarioResultSet:
    from seam_studio.services.events import publish_event

    request = request or ScenarioSimulateRequest()
    publish_event(project_id, {"type": "simulation_started", "kind": "scenario"})
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(
        scene, SimulateRequest(config_id=request.config_id, config=request.config)
    )
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    project_dir = store.resolve(project_id)
    result = run_scenario(backend, project_dir, scene, library, config, request)
    return _persist_result(
        project_id, scene, project_dir, "scenario", backend.name, config.id, result,
        config=config,
    )


@router.get(
    "/projects/{project_id}/results/scenario", response_model=ScenarioResultSet
)
def get_scenario_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> ScenarioResultSet:
    return ScenarioResultSet.model_validate(
        _load_result(project_id, "scenario", result_id)
    )


@router.post("/projects/{project_id}/live/state", response_model=LiveStateResponse)
def apply_live_state(project_id: str, update: LiveStateUpdate) -> LiveStateResponse:
    """Apply an external real-world state push to the scene.

    Matching devices/actors are moved; unknown ids are reported (never fatal).
    With persist=True the new positions are written into the stored scene.
    With persist=False they are recorded in an in-memory live overlay
    (services/live_state.py) that GET /scene and periodic re-solves apply on
    read, so the viewer's Live sync polling follows the external positions in
    real time WITHOUT permanently writing them; any authoritative save clears
    the overlay. With resimulate=True a quick path solve runs on the just-moved
    positions and fresh LinkMetrics are returned so a measure -> sync -> predict
    loop can continue.
    """
    store = get_store()
    scene = load_scene_or_404(store, project_id)

    device_by_id = {d.id: d for d in scene.devices}
    actor_by_id = {a.id: a for a in scene.actors}

    applied_devices: list[str] = []
    applied_actors: list[str] = []
    unknown_ids: list[str] = []
    warnings: list[str] = []

    for dev_state in update.devices:
        dev = device_by_id.get(dev_state.id)
        if dev is None:
            unknown_ids.append(dev_state.id)
            continue
        dev.position = [float(c) for c in dev_state.position]
        applied_devices.append(dev_state.id)

    # Actor state carries the movable actors; attached devices follow the actor
    # by its delta from the authored position (same rule as scenario frames).
    actor_states = []
    for actor_state in update.actors:
        actor = actor_by_id.get(actor_state.id)
        if actor is None:
            unknown_ids.append(actor_state.id)
            continue
        delta = [
            float(actor_state.position[i]) - float(actor.position[i]) for i in range(3)
        ]
        actor.position = [float(c) for c in actor_state.position]
        actor.orientation_deg = [float(a) for a in actor_state.orientation_deg]
        applied_actors.append(actor_state.id)
        actor_states.append(actor_state)
        # Move attached devices by the actor delta (unless the push already set
        # them explicitly above).
        for dev_id in actor.attached_device_ids:
            if dev_id in applied_devices:
                continue
            dev = device_by_id.get(dev_id)
            if dev is None:
                continue
            dev.position = [float(dev.position[i]) + delta[i] for i in range(3)]

    if update.persist:
        # Authoritative write (also clears any prior live overlay for this id set).
        store.save_scene(project_id, scene)
    else:
        # Ephemeral: record the moved positions so GET /scene polling and
        # periodic re-solves follow them without writing to disk (the README
        # "viewer follows in real time" example uses persist=false).
        from seam_studio.services import live_state

        dev_positions = {
            dev_id: device_by_id[dev_id].position
            for dev_id in applied_devices
        }
        # Attached devices moved by the actor delta also need the overlay.
        for actor_state in actor_states:
            actor = actor_by_id[actor_state.id]
            for dev_id in actor.attached_device_ids:
                if dev_id in device_by_id and dev_id not in dev_positions:
                    dev_positions[dev_id] = device_by_id[dev_id].position
        act_overlay = {
            a_state.id: {
                "position": actor_by_id[a_state.id].position,
                "orientation_deg": actor_by_id[a_state.id].orientation_deg,
            }
            for a_state in actor_states
        }
        if dev_positions or act_overlay:
            live_state.record(project_id, dev_positions, act_overlay)

    links: list[LinkMetrics] = []
    if update.resimulate:
        library = store.load_materials(project_id)
        config = _resolve_config(scene, SimulateRequest())
        try:
            backend = resolve_backend(config)
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        project_dir = store.resolve(project_id)
        result = _quick_solve(
            backend, project_dir, scene, library, config, actor_states
        )
        warnings.extend(result.warnings)
        txs = [d for d in scene.devices if d.kind == "tx"]
        rxs = [d for d in scene.devices if d.kind == "rx"]
        tx_power = {d.id: d.power_dbm for d in txs}
        noise_floor = noise_floor_dbm(config)
        links = [
            _pair_metrics(
                result.paths, tx.id, rx.id, tx_power.get(tx.id, 0.0), noise_floor
            )
            for tx in txs
            for rx in rxs
        ]

    return LiveStateResponse(
        applied_devices=applied_devices,
        applied_actors=applied_actors,
        unknown_ids=unknown_ids,
        links=links,
        warnings=warnings,
    )


def _quick_solve(backend, project_dir, scene, library, config: SimulationConfig, actor_states):
    """One path solve with the current (just-synced) positions. Sionna moves
    the cached actor objects; other backends use the mutated scene directly."""
    if backend.name == "sionna" and actor_states:
        return backend.simulate_paths(
            project_dir, scene, library, config, actor_states=actor_states
        )
    return backend.simulate_paths(project_dir, scene, library, config)
