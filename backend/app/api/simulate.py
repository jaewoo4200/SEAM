"""Simulation and result endpoints.

POST /projects/{project_id}/simulate/paths      -> PathResultSet
POST /projects/{project_id}/simulate/radio-map  -> RadioMapResultSet
GET  /projects/{project_id}/results/paths       -> stored PathResultSet
GET  /projects/{project_id}/results/radio-map   -> stored RadioMapResultSet

Storage convention: results/<result_id>.json inside the project folder, with
result_id = "<backend>_<kind>_<nnn>" (nnn = highest existing suffix + 1,
bumped past id/file collisions so pruned refs never cause an overwrite).
A ResultSetRef is appended to scene.result_sets; "latest" is the last ref of
the requested kind. Backends never persist anything - this layer owns ids,
files, and provenance.
"""

from datetime import datetime, timezone
from typing import Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query

from app.api.deps import get_store, load_scene_or_404
from app.schemas.results import (
    BeamformingResult,
    PathResultSet,
    RadioMapResultSet,
    TrajectoryResultSet,
)
from app.schemas.scene import ResultSetRef, Scene
from app.schemas.simulation import (
    BeamformingRequest,
    SimulateRequest,
    SimulationConfig,
    TrajectorySimulateRequest,
)
from app.services.simulation_backends import BackendUnavailableError, resolve_backend

router = APIRouter(tags=["simulate"])

ResultKind = Literal["paths", "radio_map", "trajectory"]
AnyResult = Union[PathResultSet, RadioMapResultSet, TrajectoryResultSet]


def _persist_result(
    project_id: str,
    scene: Scene,
    project_dir,
    kind: ResultKind,
    backend_name: str,
    config_id: str,
    result: AnyResult,
) -> AnyResult:
    """Allocate a collision-free result id, save it, append a ResultSetRef,
    and log provenance. Shared by every simulate endpoint."""
    prefix = f"{backend_name}_{kind}_"
    existing_ids = {ref.result_id for ref in scene.result_sets}
    n = 1 + max(
        (
            int(ref.result_id[len(prefix):])
            for ref in scene.result_sets
            if ref.kind == kind
            and ref.result_id.startswith(prefix)
            and ref.result_id[len(prefix):].isdigit()
        ),
        default=0,
    )
    store = get_store()
    while (
        f"{prefix}{n:03d}" in existing_ids
        or (project_dir / "results" / f"{prefix}{n:03d}.json").exists()
    ):
        n += 1
    result.result_id = f"{prefix}{n:03d}"
    result.backend = backend_name
    result.created_at = datetime.now(timezone.utc).isoformat()

    uri = f"results/{result.result_id}.json"
    store.save_json(project_id, uri, result.model_dump(mode="json"))
    scene.result_sets.append(
        ResultSetRef(
            result_id=result.result_id,
            kind=kind,
            backend=backend_name,
            simulation_config_id=config_id,
            uri=uri,
            created_at=result.created_at,
        )
    )
    store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "simulate",
            "kind": kind,
            "backend": backend_name,
            "result_id": result.result_id,
            "simulation_config_id": config_id,
            "uri": uri,
        },
    )
    return result


def _resolve_config(scene: Scene, request: SimulateRequest) -> SimulationConfig:
    if request.config is not None:
        return request.config
    if request.config_id is not None:
        for cfg in scene.simulation_configs:
            if cfg.id == request.config_id:
                return cfg
        raise HTTPException(
            status_code=404,
            detail=f"simulation config not found: {request.config_id}",
        )
    if scene.simulation_configs:
        return scene.simulation_configs[0]
    return SimulationConfig()


def _run_simulation(
    project_id: str,
    request: Optional[SimulateRequest],
    kind: ResultKind,
) -> Union[PathResultSet, RadioMapResultSet]:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(scene, request or SimulateRequest())

    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    project_dir = store.resolve(project_id)
    if kind == "paths":
        result = backend.simulate_paths(project_dir, scene, library, config)
    else:
        result = backend.simulate_radio_map(project_dir, scene, library, config)

    return _persist_result(
        project_id, scene, project_dir, kind, backend.name, config.id, result
    )


def _load_result(project_id: str, kind: ResultKind, result_id: Optional[str]) -> dict:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    refs = [ref for ref in scene.result_sets if ref.kind == kind]
    if result_id is None:
        if not refs:
            raise HTTPException(
                status_code=404, detail=f"no {kind} results in project {project_id}"
            )
        ref = refs[-1]  # latest = last appended ref of this kind
    else:
        ref = next((r for r in refs if r.result_id == result_id), None)
        if ref is None:
            raise HTTPException(
                status_code=404, detail=f"unknown {kind} result: {result_id}"
            )
    try:
        return store.load_json(project_id, ref.uri)
    except (OSError, ValueError):
        raise HTTPException(
            status_code=404,
            detail=f"result file missing or unreadable: {ref.uri}",
        )


@router.post("/projects/{project_id}/simulate/paths", response_model=PathResultSet)
def simulate_paths(
    project_id: str, request: Optional[SimulateRequest] = None
) -> PathResultSet:
    return _run_simulation(project_id, request, "paths")


@router.post(
    "/projects/{project_id}/simulate/radio-map", response_model=RadioMapResultSet
)
def simulate_radio_map(
    project_id: str, request: Optional[SimulateRequest] = None
) -> RadioMapResultSet:
    return _run_simulation(project_id, request, "radio_map")


@router.get("/projects/{project_id}/results/paths", response_model=PathResultSet)
def get_paths_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> PathResultSet:
    return PathResultSet.model_validate(_load_result(project_id, "paths", result_id))


@router.get(
    "/projects/{project_id}/results/radio-map", response_model=RadioMapResultSet
)
def get_radio_map_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> RadioMapResultSet:
    return RadioMapResultSet.model_validate(
        _load_result(project_id, "radio_map", result_id)
    )


@router.post(
    "/projects/{project_id}/simulate/trajectory", response_model=TrajectoryResultSet
)
def simulate_trajectory(
    project_id: str, request: TrajectorySimulateRequest
) -> TrajectoryResultSet:
    from app.services.trajectory import run_trajectory

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
    try:
        result = run_trajectory(backend, project_dir, scene, library, config, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return _persist_result(
        project_id, scene, project_dir, "trajectory", backend.name, config.id, result
    )


@router.get(
    "/projects/{project_id}/results/trajectory", response_model=TrajectoryResultSet
)
def get_trajectory_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> TrajectoryResultSet:
    return TrajectoryResultSet.model_validate(
        _load_result(project_id, "trajectory", result_id)
    )


@router.post(
    "/projects/{project_id}/simulate/beamforming", response_model=BeamformingResult
)
def simulate_beamforming(
    project_id: str, request: Optional[BeamformingRequest] = None
) -> BeamformingResult:
    """MIMO beamforming gain (MRT / SVD) over one TX->RX link. Computed on
    demand and returned directly (not stored as a result set)."""
    request = request or BeamformingRequest()
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
    return backend.simulate_beamforming(project_dir, scene, library, config, request)
