"""Simulation and result endpoints.

POST /projects/{project_id}/simulate/paths      -> PathResultSet
POST /projects/{project_id}/simulate/radio-map  -> RadioMapResultSet
POST /projects/{project_id}/simulate/sensing    -> SensingResultSet
POST /projects/{project_id}/simulate/isac       -> ISACResultSet
POST /projects/{project_id}/simulate/sensing-coverage -> SensingCoverageResultSet
POST /projects/{project_id}/analysis/pd-curve   -> PdCurveResult (not stored)
GET  /projects/{project_id}/results/paths       -> stored PathResultSet
GET  /projects/{project_id}/results/radio-map   -> stored RadioMapResultSet
GET  /projects/{project_id}/results/sensing     -> stored SensingResultSet
GET  /projects/{project_id}/results/isac        -> stored ISACResultSet
GET  /projects/{project_id}/results/sensing-coverage -> stored SensingCoverageResultSet

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
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Iterator, Literal, Optional, Union

from fastapi import APIRouter, HTTPException, Query
from pydantic import Field

from seam_studio.api.deps import get_store, load_scene_live, load_scene_or_404
from seam_studio.schemas.actors import ScenarioResultSet
from seam_studio.schemas.channel import ChannelAnalysisResult
from seam_studio.schemas.common import StrictModel
from seam_studio.schemas.results import (
    BeamformingResult,
    ISACResultSet,
    MeshRadioMapResultSet,
    PathResultSet,
    PdCurveResult,
    PlaybackResultSet,
    RadioMapResultSet,
    SensingCoverageResultSet,
    SensingResultSet,
    TrajectoryResultSet,
)
from seam_studio.schemas.scene import ResultSetRef, Scene
from seam_studio.schemas.sensing import (
    MAX_MC_TOTAL_TRIALS,
    ISACRequest,
    PdCurveRequest,
    SensingCoverageRequest,
    SensingSimulateRequest,
)
from seam_studio.schemas.simulation import (
    BeamformingRequest,
    MeshRadioMapRequest,
    PlaybackBuildRequest,
    SimulateRequest,
    SimulationConfig,
    TrajectorySimulateRequest,
)
from seam_studio.services import solve_ctx
from seam_studio.services.simulation_backends import (
    BackendUnavailableError,
    RayTracingBackend,
    get_backend,
    resolve_backend,
)

router = APIRouter(tags=["simulate"])

logger = logging.getLogger(__name__)

# Solves mutate the (cached, shared) compiled scene in place — concurrent
# solves on one project would interleave device setup and corrupt results, so
# each project gets one solve at a time. Separate small lock for the
# read-modify-write of scene.result_sets in _persist_result (two solves on
# DIFFERENT projects finishing together must not drop each other's ref).
_project_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_refs_lock = threading.Lock()


def _project_lock(project_id: str) -> threading.Lock:
    with _locks_guard:
        return _project_locks.setdefault(project_id, threading.Lock())


@contextmanager
def _solve_guard(project_id: str, kind: str) -> Iterator[None]:
    """Serialize per project, arm cancel/progress, and always emit a terminal
    event (finished comes from _persist_result; failure/cancel from here)."""
    from seam_studio.services.events import publish_event

    publish_event(project_id, {"type": "simulation_started", "kind": kind})
    with _project_lock(project_id):
        with solve_ctx.solve_context(project_id, kind):
            try:
                yield
            except solve_ctx.SolveCancelled as exc:
                publish_event(
                    project_id,
                    {"type": "simulation_failed", "kind": kind, "cancelled": True},
                )
                raise HTTPException(status_code=409, detail="solve cancelled") from exc
            except HTTPException as exc:
                # A mid-solve 404/409 must still emit the terminal event, or
                # the client's progress card (armed by started above) sticks.
                publish_event(
                    project_id,
                    {
                        "type": "simulation_failed",
                        "kind": kind,
                        "error": str(exc.detail),
                    },
                )
                raise
            except Exception as exc:
                publish_event(
                    project_id,
                    {"type": "simulation_failed", "kind": kind, "error": str(exc)},
                )
                raise

ResultKind = Literal[
    "paths", "radio_map", "mesh_radio_map", "trajectory", "scenario",
    "channel", "playback", "sensing", "isac", "sensing_coverage",
]
RESULT_KINDS: tuple[str, ...] = (
    "paths",
    "radio_map",
    "mesh_radio_map",
    "trajectory",
    "scenario",
    "channel",
    "playback",
    "sensing",
    "isac",
    "sensing_coverage",
)
AnyResult = Union[
    PathResultSet,
    RadioMapResultSet,
    MeshRadioMapResultSet,
    TrajectoryResultSet,
    ScenarioResultSet,
    ChannelAnalysisResult,
    PlaybackResultSet,
    SensingResultSet,
    ISACResultSet,
    SensingCoverageResultSet,
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

    scene_hash covers the canonical scene MINUS result_sets AND revision:
    both are bookkeeping the *act of solving* mutates (persisting a result
    appends a ref and bumps the save counter), so leaving either in would make
    two identical solves record different hashes and break the provenance
    claim "same scene_hash => same input scene". rf_assignment_hash covers just
    (prim id, material, status) so a pure material edit is detectable on its
    own; sim_config_hash pins the exact solver knobs.
    """
    scene_payload = scene.model_dump(mode="json")
    scene_payload.pop("result_sets", None)
    scene_payload.pop("revision", None)
    # An unbound actor (and a dt-paced trajectory) hashes exactly as it did
    # before Actor.sensing / ActorTrajectory.speed_m_s existed, so results
    # solved before those fields landed stay comparable.
    for actor in scene_payload.get("actors", []):
        if actor.get("sensing") is None:
            actor.pop("sensing", None)
        traj = actor.get("trajectory")
        if traj is not None and traj.get("speed_m_s") is None:
            traj.pop("speed_m_s", None)
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
    label: Optional[str] = None,
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
    # Sensing-only knobs (targets, max_depth, samples_per_sp, comm paths,
    # codebook, slot ratios, grid) live in the request, not the config:
    # without this two different runs would share every hash.
    req = result.metadata.get("sensing_request" if kind == "sensing" else "request")
    if kind in ("sensing", "isac", "sensing_coverage") and req is not None:
        result.metadata["request_hash"] = _sha256(req)
    if config is not None:
        result.metadata.setdefault("config_snapshot", config.model_dump(mode="json"))

    uri = f"results/{result.result_id}.json"
    saved_path = store.save_json(project_id, uri, result.model_dump(mode="json"))
    ref = ResultSetRef(
        result_id=result.result_id,
        kind=kind,
        backend=backend_name,
        simulation_config_id=config_id,
        uri=uri,
        created_at=result.created_at,
        label=label,
        size_bytes=saved_path.stat().st_size,
    )
    # The solved `scene` may carry an ephemeral live-state overlay (this solve
    # loaded it via load_scene_live so the ray tracing followed external
    # positions). Persisting the result ref must NOT write those overlaid
    # positions to disk, so append the ref onto a freshly-loaded CLEAN scene
    # and save that — and keep the overlay (clear_live_overlay=False) so a
    # periodic/live re-solve keeps following the live feed.
    scene.result_sets.append(ref)  # keep the in-memory scene consistent
    with _refs_lock:
        clean = load_scene_or_404(store, project_id)
        clean.result_sets.append(ref)
        store.save_scene(project_id, clean, clear_live_overlay=False)
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
    from seam_studio.services.events import publish_event

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
    with _solve_guard(project_id, kind):
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
    from seam_studio.services.mesh_radio_map import mesh_radio_map

    with _solve_guard(project_id, "mesh_radio_map"):
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
    "/projects/{project_id}/results/channel", response_model=ChannelAnalysisResult
)
def get_channel_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> ChannelAnalysisResult:
    """Latest (or a specific) persisted channel analysis — lets the Metrics
    dashboard reload what the user last analyzed instead of starting empty."""
    return ChannelAnalysisResult.model_validate(
        _load_result(project_id, "channel", result_id)
    )


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
    # Pruning only edits result_sets — never device/actor positions — so load
    # the CLEAN scene (no live overlay) and keep any overlay intact on save.
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)

    scope = set(request.kinds) if request.kinds is not None else set(RESULT_KINDS)

    removed_ids: list[str] = []
    kept_ids: list[str] = []
    survivors: list[ResultSetRef] = []
    # Refs are appended chronologically; the last N of a kind are the newest.
    kept_per_kind: dict[str, int] = {}
    freed_bytes = 0
    for ref in reversed(scene.result_sets):
        if ref.kind not in scope:
            survivors.append(ref)
            continue
        # Labeled runs are named baselines — housekeeping never deletes them
        # (and they do not consume the keep window).
        if ref.label:
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
                freed_bytes += file_path.stat().st_size
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
    # Not a position edit: keep any live overlay so a running live session
    # is not ended by a housekeeping prune.
    store.save_scene(project_id, scene, clear_live_overlay=False)
    store.append_provenance(
        project_id,
        {"type": "results_pruned", "removed_count": len(removed_ids)},
    )
    # removed_ids was built newest-first; present it oldest-first for symmetry
    # with kept_ids (both chronological).
    removed_ids.reverse()
    return {"removed": removed_ids, "kept": kept_ids, "freed_bytes": freed_bytes}


@router.post(
    "/projects/{project_id}/simulate/trajectory", response_model=TrajectoryResultSet
)
def simulate_trajectory(
    project_id: str, request: TrajectorySimulateRequest
) -> TrajectoryResultSet:
    from seam_studio.services.trajectory import run_trajectory

    with _solve_guard(project_id, "trajectory"):
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
    "/projects/{project_id}/simulate/playback", response_model=PlaybackResultSet
)
def simulate_playback(
    project_id: str, request: PlaybackBuildRequest
) -> PlaybackResultSet:
    """GT-vs-DT playback pack (issue #3): replay the project's sensor manifest,
    one paths solve + one codebook sweep per selected frame."""
    from seam_studio.services.playback import build_playback
    from seam_studio.services.sensor_store import MANIFEST_REL, load_manifest

    with _solve_guard(project_id, "playback"):
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
            manifest = load_manifest(project_dir)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc))
        if manifest is None:
            raise HTTPException(
                status_code=404, detail=f"no sensor data: {MANIFEST_REL} not found"
            )
        try:
            result = build_playback(
                backend, project_dir, scene, library, config, request, manifest,
                tick=solve_ctx.tick,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        return _persist_result(
            project_id, scene, project_dir, "playback", backend.name, config.id,
            result, config=config,
        )


@router.get(
    "/projects/{project_id}/results/playback", response_model=PlaybackResultSet
)
def get_playback_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> PlaybackResultSet:
    return PlaybackResultSet.model_validate(
        _load_result(project_id, "playback", result_id)
    )


def _resolve_sensing_backend(
    config: SimulationConfig,
) -> tuple[RayTracingBackend, list[str]]:
    """Backend for a sensing solve plus warnings to prepend to the result.

    "auto" keeps its "sionna when usable, else mock" meaning: a sionna-rt
    without the RCS module (< 2.2) falls back to the mock with a note instead
    of blocking the solve. An explicit backend without sensing raises
    BackendUnavailableError (the route answers 409)."""
    if config.backend == "auto":
        sionna = get_backend("sionna")
        if sionna.is_available() and sionna.capabilities().get("sensing"):
            return sionna, []
        note = (
            ["sionna-rt has no RCS solver (needs >=2.2); the sensing solve used the mock backend"]
            if sionna.is_available()
            else []
        )
        return get_backend("mock"), note
    backend = resolve_backend(config)
    if not backend.capabilities().get("sensing", False):
        raise BackendUnavailableError(
            "sensing requires sionna-rt>=2.2 (sionna.rt.rcs not found)"
        )
    return backend, []


@router.post(
    "/projects/{project_id}/simulate/sensing", response_model=SensingResultSet
)
def simulate_sensing(
    project_id: str, request: Optional[SensingSimulateRequest] = None
) -> SensingResultSet:
    """Radar (RCS) solve over every actor bound as a sensing target: echo paths
    TX -> target scattering point -> RX with per-path Doppler. Place an RX at
    the TX for monostatic sensing."""
    from seam_studio.services.sensing import SensingRequestError, run_sensing, select_targets

    request = request or SensingSimulateRequest()
    store = get_store()
    scene = load_scene_live(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(
        scene, SimulateRequest(config_id=request.config_id, config=request.config)
    )
    overrides = {
        key: value
        for key, value in (("tx_ids", request.tx_ids), ("rx_ids", request.rx_ids))
        if value is not None
    }
    if overrides:
        config = config.model_copy(update=overrides)
    # Validation stays outside the guard so a 4xx never announces a solve.
    for kind, wanted in (("tx", request.tx_ids), ("rx", request.rx_ids)):
        for device_id in wanted or []:
            if not any(d.id == device_id and d.kind == kind for d in scene.devices):
                raise HTTPException(
                    status_code=400, detail=f"{kind} device not found: {device_id}"
                )
    selected = {
        kind: [
            d
            for d in scene.devices
            if d.kind == kind and (ids is None or d.id in ids)
        ]
        for kind, ids in (("tx", config.tx_ids), ("rx", config.rx_ids))
    }
    if not selected["tx"] or not selected["rx"]:
        raise HTTPException(
            status_code=400,
            detail=(
                "sensing needs at least one tx and one rx device (place an rx at "
                "the tx for monostatic sensing)"
            ),
        )
    try:
        targets = select_targets(scene, request.target_actor_ids)
    except SensingRequestError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        backend, notes = _resolve_sensing_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    project_dir = store.resolve(project_id)

    with _solve_guard(project_id, "sensing"):
        try:
            result = run_sensing(
                backend, project_dir, scene, library, config, request, targets,
                tick=solve_ctx.tick, extra_warnings=notes,
            )
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _persist_result(
            project_id, scene, project_dir, "sensing", backend.name, config.id,
            result, config=config,
        )


@router.get(
    "/projects/{project_id}/results/sensing", response_model=SensingResultSet
)
def get_sensing_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> SensingResultSet:
    return SensingResultSet.model_validate(
        _load_result(project_id, "sensing", result_id)
    )


@router.post("/projects/{project_id}/simulate/isac", response_model=ISACResultSet)
def simulate_isac(
    project_id: str, request: Optional[ISACRequest] = None
) -> ISACResultSet:
    """ISAC beam trade-off: per TX, the azimuth (or azimuth x elevation)
    codebook scored for its UEs' rate and its targets' echo SNR from one
    t = 0 echo + comm solve, and the Pd-rate Pareto front of sharing slots
    between the comm and sensing beams. The Monte Carlo budget is checked
    here, before the solve guard (400)."""
    from seam_studio.services.isac import (
        ISACRequestError,
        isac_mc_estimates,
        plan_isac_roles,
        run_isac,
    )
    from seam_studio.services.sensing import SensingRequestError, select_targets

    request = request or ISACRequest()
    store = get_store()
    scene = load_scene_live(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(
        scene, SimulateRequest(config_id=request.config_id, config=request.config)
    )
    # Validation stays outside the guard so a 4xx never announces a solve.
    if not request.use_device_orientation:
        raise HTTPException(
            status_code=400,
            detail=(
                "ISAC needs fixed-bearing arrays (use_device_orientation=true): "
                "look_at cannot aim one panel at both its UEs and its targets"
            ),
        )
    try:
        plan = plan_isac_roles(
            scene, request.tx_ids, request.ue_rx_ids, request.sensing_rx_ids
        )
        targets = select_targets(scene, request.target_actor_ids)
    except (ISACRequestError, SensingRequestError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    trials = request.detector.monte_carlo_trials
    estimates = isac_mc_estimates(request, len(plan.txs))
    if trials * estimates > MAX_MC_TOTAL_TRIALS:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{trials} Monte Carlo trials x {estimates} Pd estimates ({len(plan.txs)} tx "
                f"x beams x (1 + nonzero slot ratios)) = {trials * estimates} trials; at most "
                f"{MAX_MC_TOTAL_TRIALS}"
            ),
        )
    try:
        backend, notes = _resolve_sensing_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    project_dir = store.resolve(project_id)

    with _solve_guard(project_id, "isac"):
        try:
            result = run_isac(
                backend, project_dir, scene, library, config, request, plan, targets,
                tick=solve_ctx.tick, extra_warnings=notes,
            )
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        return _persist_result(
            project_id, scene, project_dir, "isac", backend.name, config.id,
            result, config=config,
        )


@router.get("/projects/{project_id}/results/isac", response_model=ISACResultSet)
def get_isac_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> ISACResultSet:
    return ISACResultSet.model_validate(_load_result(project_id, "isac", result_id))


@router.post(
    "/projects/{project_id}/simulate/sensing-coverage",
    response_model=SensingCoverageResultSet,
)
def simulate_sensing_coverage(
    project_id: str, request: Optional[SensingCoverageRequest] = None
) -> SensingCoverageResultSet:
    """Sensing coverage map: bistatic radar equation of a virtual point target
    over a grid at one height, per TX x sensing-RX link with two-leg LOS, plus
    the links detected and the multistatic fusion feasibility per cell."""
    from seam_studio.services.isac import ISACRequestError, plan_isac_roles
    from seam_studio.services.scene_bounds import compute_scene_bounds
    from seam_studio.services.sensing_coverage import (
        SensingCoverageError,
        coverage_grid,
        run_sensing_coverage,
    )

    request = request or SensingCoverageRequest()
    store = get_store()
    scene = load_scene_live(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(
        scene, SimulateRequest(config_id=request.config_id, config=request.config)
    )
    project_dir = store.resolve(project_id)
    try:
        plan = plan_isac_roles(
            scene, request.tx_ids, None, request.sensing_rx_ids, require_ues=False
        )
        bounds = (
            compute_scene_bounds(project_dir, scene) if request.center_xy is None else None
        )
        grid = coverage_grid(request, bounds)
    except (ISACRequestError, SensingCoverageError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    with _solve_guard(project_id, "sensing_coverage"):
        result = run_sensing_coverage(
            backend, project_dir, scene, library, config, request,
            plan=plan, grid=grid, tick=solve_ctx.tick,
        )
        return _persist_result(
            project_id, scene, project_dir, "sensing_coverage", backend.name,
            config.id, result, config=config,
        )


@router.get(
    "/projects/{project_id}/results/sensing-coverage",
    response_model=SensingCoverageResultSet,
)
def get_sensing_coverage_result(
    project_id: str, result_id: Optional[str] = Query(default=None)
) -> SensingCoverageResultSet:
    return SensingCoverageResultSet.model_validate(
        _load_result(project_id, "sensing_coverage", result_id)
    )


@router.post("/projects/{project_id}/analysis/pd-curve", response_model=PdCurveResult)
def analysis_pd_curve(
    project_id: str, request: Optional[PdCurveRequest] = None
) -> PdCurveResult:
    """Pd vs post-integration SNR per detector model (Swerling 0/1/3,
    square-law), with a Monte Carlo per point when monte_carlo_trials > 0.
    Pure math: no solve, no solve guard, not persisted."""
    from seam_studio.services.detector import DetectorError, pd_curve

    request = request or PdCurveRequest()
    load_scene_or_404(get_store(), project_id)
    try:
        return pd_curve(request)
    except DetectorError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


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
    # Validation stays outside the guard so a 400/409 never announces a solve.
    # The guard matters beyond UX here: the backend swaps devices on the shared
    # cached rt_scene, so an unguarded beamforming racing another solve on the
    # same project could silently corrupt both results.
    with _solve_guard(project_id, "beamforming"):
        result = backend.simulate_beamforming(
            project_dir, scene, library, config, request
        )
        # Beamforming is the one solve that does not persist a result set, so
        # the finished event (elsewhere published by _persist_result) is manual.
        from seam_studio.services.events import publish_event

        publish_event(
            project_id,
            {
                "type": "simulation_finished",
                "kind": "beamforming",
                "backend": backend.name,
            },
        )
        return result


@router.post("/projects/{project_id}/simulate/cancel")
def cancel_solve(project_id: str) -> dict:
    """Request cooperative cancellation of the project's running solve.

    Loop-based solves (trajectory, dataset, mesh radio map, scenario, height
    sweeps) stop at their next checkpoint; a single-shot solver call finishes
    its current invocation first.
    """
    solve_ctx.request_cancel(project_id)
    return {"requested": True}


class ResultLabelRequest(StrictModel):
    label: Optional[str] = Field(default=None, max_length=80)


@router.patch("/projects/{project_id}/results/{result_id}/label")
def label_result(
    project_id: str, result_id: str, request: ResultLabelRequest
) -> ResultSetRef:
    """Name a stored run ("before-glass-facade"); labeled runs survive prune.

    Passing label=null clears the name.
    """
    store = get_store()
    with _refs_lock:
        scene = load_scene_or_404(store, project_id)
        ref = next(
            (r for r in scene.result_sets if r.result_id == result_id), None
        )
        if ref is None:
            raise HTTPException(
                status_code=404, detail=f"unknown result: {result_id}"
            )
        label = (request.label or "").strip() or None
        scene.result_sets = [
            r.model_copy(update={"label": label}) if r.result_id == result_id else r
            for r in scene.result_sets
        ]
        store.save_scene(project_id, scene, clear_live_overlay=False)
    return next(r for r in scene.result_sets if r.result_id == result_id)


class RadioMapSweepRequest(StrictModel):
    """Coverage-vs-altitude: one planar radio map per requested height."""

    heights_m: list[float] = Field(min_length=1, max_length=12)
    # Coverage = fraction of solved cells at or above this metric threshold
    # (same metric the grid config selects); omit to skip the summary.
    threshold_db: Optional[float] = None
    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None


@router.post("/projects/{project_id}/simulate/radio-map-sweep")
def simulate_radio_map_sweep(
    project_id: str, request: RadioMapSweepRequest
) -> dict:
    """Solve the planar radio map at each height and persist every run
    (auto-labeled "h=<H> m" so the sweep survives pruning). Returns the run
    ids plus a per-height coverage summary for the altitude curve."""
    with _solve_guard(project_id, "radio_map_sweep"):
        store = get_store()
        scene = load_scene_live(store, project_id)
        library = store.load_materials(project_id)
        base = _resolve_config(
            scene, SimulateRequest(config_id=request.config_id, config=request.config)
        )
        try:
            backend = resolve_backend(base)
        except BackendUnavailableError as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        project_dir = store.resolve(project_id)

        runs: list[dict] = []
        coverage: list[dict] = []
        total = len(request.heights_m)
        for i, height in enumerate(request.heights_m):
            solve_ctx.tick(i, total)
            grid = base.radio_map.model_copy(update={"height_m": float(height)})
            config = base.model_copy(update={"radio_map": grid})
            result = backend.simulate_radio_map(project_dir, scene, library, config)
            persisted = _persist_result(
                project_id, scene, project_dir, "radio_map", backend.name,
                config.id, result, config=config, label=f"h={height:g} m",
            )
            runs.append({"height_m": height, "result_id": persisted.result_id})
            frac = None
            if request.threshold_db is not None:
                solved = [
                    v
                    for row in persisted.values
                    for v in row
                    if v is not None
                ]
                if solved:
                    frac = sum(
                        1 for v in solved if v >= request.threshold_db
                    ) / len(solved)
            coverage.append({"height_m": height, "coverage": frac})
        solve_ctx.tick(total, total)
        return {"runs": runs, "coverage": coverage}
