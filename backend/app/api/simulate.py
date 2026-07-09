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

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from app.api.deps import get_store, load_scene_live, load_scene_or_404
from app.schemas.actors import ScenarioResultSet
from app.schemas.common import StrictModel
from app.schemas.results import (
    BeamformingResult,
    MeshRadioMapResultSet,
    PathResultSet,
    RadioMapResultSet,
    TrajectoryResultSet,
)
from app.schemas.scene import ResultSetRef, Scene
from app.schemas.simulation import (
    BeamformingRequest,
    MeshRadioMapRequest,
    SimulateRequest,
    SimulationConfig,
    TrajectorySimulateRequest,
)
from app.services.simulation_backends import BackendUnavailableError, resolve_backend

router = APIRouter(tags=["simulate"])

logger = logging.getLogger(__name__)

ResultKind = Literal["paths", "radio_map", "mesh_radio_map", "trajectory", "scenario"]
RESULT_KINDS: tuple[str, ...] = (
    "paths",
    "radio_map",
    "mesh_radio_map",
    "trajectory",
    "scenario",
)
AnyResult = Union[
    PathResultSet,
    RadioMapResultSet,
    MeshRadioMapResultSet,
    TrajectoryResultSet,
    ScenarioResultSet,
]


class ResultsPruneRequest(StrictModel):
    """Body for POST /projects/{id}/results/prune.

    ``keep_latest`` newest refs per kind survive (0 = drop every ref of the
    kind); ``kinds`` restricts the operation to the named result kinds, or
    None to sweep every kind.
    """

    keep_latest: int = Field(default=0, ge=0)
    kinds: Optional[list[ResultKind]] = None


def _sha256(payload) -> str:
    """Stable content hash of a JSON-serializable payload."""
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _provenance_hashes(scene: Scene, config: Optional[SimulationConfig]) -> dict:
    """Reproducibility hashes stamped into every persisted result's metadata.

    scene_hash covers the canonical scene MINUS result_sets (results must not
    churn the hash of the scene that produced them); rf_assignment_hash covers
    just (prim id, material, status) so a pure material edit is detectable on
    its own; sim_config_hash pins the exact solver knobs.
    """
    scene_payload = scene.model_dump(mode="json")
    scene_payload.pop("result_sets", None)
    assignment = sorted(
        (p.id, p.rf.material_id or "", p.rf.assignment_status) for p in scene.prims
    )
    hashes = {
        "scene_hash": _sha256(scene_payload),
        "rf_assignment_hash": _sha256(assignment),
    }
    if config is not None:
        hashes["sim_config_hash"] = _sha256(config.model_dump(mode="json"))
    return hashes


def _persist_result(
    project_id: str,
    scene: Scene,
    project_dir,
    kind: ResultKind,
    backend_name: str,
    config_id: str,
    result: AnyResult,
    config: Optional[SimulationConfig] = None,
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
    # Reproducibility stamp: content hashes + the exact solver knobs used.
    result.metadata.update(_provenance_hashes(scene, config))
    if config is not None:
        result.metadata.setdefault("config_snapshot", config.model_dump(mode="json"))

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
    # Lazy import to avoid an import cycle (events -> nothing here, but keep the
    # hook self-contained and never fatal to a solve).
    from app.services.events import publish_event

    publish_event(
        project_id,
        {
            "type": "simulation_finished",
            "kind": kind,
            "result_id": result.result_id,
            "backend": backend_name,
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
    from app.services.events import publish_event

    publish_event(project_id, {"type": "simulation_started", "kind": kind})
    store = get_store()
    scene = load_scene_live(store, project_id)
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
        project_id, scene, project_dir, kind, backend.name, config.id, result,
        config=config,
    )


def _load_result(project_id: str, kind: ResultKind, result_id: Optional[str]) -> dict:
    store = get_store()
    scene = load_scene_live(store, project_id)
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


@router.post(
    "/projects/{project_id}/simulate/mesh-radio-map",
    response_model=MeshRadioMapResultSet,
)
def simulate_mesh_radio_map(
    project_id: str, request: MeshRadioMapRequest
) -> MeshRadioMapResultSet:
    """Per-triangle coverage on the requested prims' surfaces (facades,
    roads, floors) - probe receivers at triangle centers, chunk-solved with
    the active backend."""
    from app.services.events import publish_event
    from app.services.mesh_radio_map import mesh_radio_map

    publish_event(project_id, {"type": "simulation_started", "kind": "mesh_radio_map"})
    store = get_store()
    scene = load_scene_live(store, project_id)
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
        result = mesh_radio_map(backend, project_dir, scene, library, config, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return _persist_result(
        project_id, scene, project_dir, "mesh_radio_map", backend.name, config.id,
        result, config=config,
    )


@router.get(
    "/projects/{project_id}/results/mesh-radio-map",
    response_model=MeshRadioMapResultSet,
)
def get_mesh_radio_map_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> MeshRadioMapResultSet:
    return MeshRadioMapResultSet(**_load_result(project_id, "mesh_radio_map", result_id))


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


@router.post("/projects/{project_id}/results/prune")
def prune_results(project_id: str, request: Optional[ResultsPruneRequest] = None) -> dict:
    """Delete older result files and their ResultSetRef entries.

    For each result kind in scope (all kinds, or those named in
    ``request.kinds``), the newest ``keep_latest`` refs are retained and the
    older ones dropped: their result files are removed (a file already gone is
    still dropped from the scene, logged as a warning) and their refs deleted
    from ``scene.result_sets``. Surviving refs keep their original order. One
    ``results_pruned`` provenance event is appended with the removed count.
    """
    request = request or ResultsPruneRequest()
    store = get_store()
    scene = load_scene_live(store, project_id)
    project_dir = store.resolve(project_id)

    scope = set(request.kinds) if request.kinds is not None else set(RESULT_KINDS)

    removed_ids: list[str] = []
    kept_ids: list[str] = []
    survivors: list[ResultSetRef] = []
    # Refs are appended chronologically; the last N of a kind are the newest.
    kept_per_kind: dict[str, int] = {}
    for ref in reversed(scene.result_sets):
        if ref.kind not in scope:
            survivors.append(ref)
            continue
        seen = kept_per_kind.get(ref.kind, 0)
        if seen < request.keep_latest:
            kept_per_kind[ref.kind] = seen + 1
            survivors.append(ref)
            continue
        # Older than the keep window -> remove file + ref.
        try:
            file_path = store.asset_path(project_id, ref.uri)
            if file_path.exists():
                file_path.unlink()
            else:
                logger.warning(
                    "prune: result file already missing for %s: %s",
                    ref.result_id,
                    ref.uri,
                )
        except (OSError, ValueError) as exc:
            # Never let a stray/unreadable path block the ref cleanup.
            logger.warning(
                "prune: could not delete result file for %s (%s): %s",
                ref.result_id,
                ref.uri,
                exc,
            )
        removed_ids.append(ref.result_id)

    survivors.reverse()  # restore chronological order
    kept_ids = [ref.result_id for ref in survivors]
    scene.result_sets = survivors
    store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {"type": "results_pruned", "removed_count": len(removed_ids)},
    )
    # removed_ids was built newest-first; present it oldest-first for symmetry
    # with kept_ids (both chronological).
    removed_ids.reverse()
    return {"removed": removed_ids, "kept": kept_ids}


@router.post(
    "/projects/{project_id}/simulate/trajectory", response_model=TrajectoryResultSet
)
def simulate_trajectory(
    project_id: str, request: TrajectorySimulateRequest
) -> TrajectoryResultSet:
    from app.services.events import publish_event
    from app.services.trajectory import run_trajectory

    publish_event(project_id, {"type": "simulation_started", "kind": "trajectory"})
    store = get_store()
    scene = load_scene_live(store, project_id)
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
        project_id, scene, project_dir, "trajectory", backend.name, config.id, result,
        config=config,
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
    scene = load_scene_live(store, project_id)
    # Explicit ids must resolve (audit M3): silently falling back to the first
    # device returned plausible-but-wrong numbers for a typo. Matches the
    # 400 contract of analyze/channel and simulate/trajectory.
    for wanted, kind in ((request.tx_id, "tx"), (request.rx_id, "rx")):
        if wanted is not None and not any(
            d.id == wanted and d.kind == kind for d in scene.devices
        ):
            raise HTTPException(
                status_code=400, detail=f"{kind} device not found: {wanted}"
            )
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
