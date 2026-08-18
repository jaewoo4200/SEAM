"""Export endpoints.

POST /projects/{project_id}/export/rfdata  -> writes the AODT viewer contract
(scenario_meta/devices/paths/trajectory/radio_map/calibration_points) under
export/rfdata/ and returns a summary of what was written.

POST /projects/{project_id}/export/aodt    -> writes NVIDIA AODT's OFFICIAL
results-schema parquet tables under export/aodt/ (409 when pyarrow is missing,
404 when the requested source result is absent).

POST /projects/{project_id}/export/channel-npz -> solves one paths run per UE
position and writes a per-link channel dataset npz in the AODT/HYRAY layout
under export/channel_npz/.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from seam_studio.api.deps import get_store, load_scene_or_404
from seam_studio.schemas.results import (
    MAX_CHANNEL_NPZ_UES,
    AodtExportRequest,
    AodtExportSummary,
    ChannelNpzExportRequest,
    ChannelNpzExportSummary,
    PathResultSet,
    PlaybackResultSet,
    RadioMapResultSet,
    RFDataExportSummary,
    TrajectoryResultSet,
)
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.simulation import SimulateRequest, SimulationConfig

router = APIRouter(tags=["export"])


def _resolve_config(scene: Scene, config_id: Optional[str]) -> SimulationConfig:
    if config_id is not None:
        for cfg in scene.simulation_configs:
            if cfg.id == config_id:
                return cfg
        raise HTTPException(status_code=404, detail=f"simulation config not found: {config_id}")
    if scene.simulation_configs:
        return scene.simulation_configs[0]
    return SimulationConfig()


def _latest(store, project_id: str, scene: Scene, kind: str):
    refs = [r for r in scene.result_sets if r.kind == kind]
    if not refs:
        return None
    try:
        return store.load_json(project_id, refs[-1].uri)
    except (OSError, ValueError):
        return None


@router.post(
    "/projects/{project_id}/export/rfdata", response_model=RFDataExportSummary
)
def export_rfdata_endpoint(
    project_id: str, request: Optional[SimulateRequest] = None
) -> RFDataExportSummary:
    from seam_studio.services.rfdata_export import export_rfdata

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    config = _resolve_config(scene, (request or SimulateRequest()).config_id)
    if request and request.config is not None:
        config = request.config
    project_dir = store.resolve(project_id)

    paths_raw = _latest(store, project_id, scene, "paths")
    rm_raw = _latest(store, project_id, scene, "radio_map")
    traj_raw = _latest(store, project_id, scene, "trajectory")

    paths = PathResultSet.model_validate(paths_raw) if paths_raw else None
    radio_map = RadioMapResultSet.model_validate(rm_raw) if rm_raw else None
    trajectory = TrajectoryResultSet.model_validate(traj_raw) if traj_raw else None

    summary = export_rfdata(
        project_dir,
        scene,
        config,
        created_at=datetime.now(timezone.utc).isoformat(),
        paths=paths,
        radio_map=radio_map,
        trajectory=trajectory,
    )
    store.append_provenance(
        project_id,
        {"type": "export_rfdata", "files": summary["files"]},
    )
    return RFDataExportSummary(**summary)


def _load_result_of_kind(store, project_id: str, scene: Scene, kind: str,
                         result_id: Optional[str]) -> dict:
    """Stored result of ``kind`` (explicit id, else latest) or 404."""
    refs = [r for r in scene.result_sets if r.kind == kind]
    if result_id is not None:
        refs = [r for r in refs if r.result_id == result_id]
        if not refs:
            raise HTTPException(
                status_code=404, detail=f"unknown {kind} result: {result_id}"
            )
    if not refs:
        raise HTTPException(
            status_code=404, detail=f"no {kind} results in project {project_id}"
        )
    try:
        return store.load_json(project_id, refs[-1].uri)
    except (OSError, ValueError):
        raise HTTPException(
            status_code=404, detail=f"result file missing or unreadable: {refs[-1].uri}"
        )


@router.post("/projects/{project_id}/export/aodt", response_model=AodtExportSummary)
def export_aodt_endpoint(
    project_id: str, request: Optional[AodtExportRequest] = None
) -> AodtExportSummary:
    from seam_studio.services.aodt_export import (
        AodtExportError,
        AodtExportUnavailable,
        export_aodt,
    )
    from seam_studio.services.events import publish_event

    req = request or AodtExportRequest()
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    config = _resolve_config(scene, req.config_id)
    project_dir = store.resolve(project_id)
    library = store.load_materials(project_id)

    paths = playback = None
    if req.source == "playback":
        playback = PlaybackResultSet.model_validate(
            _load_result_of_kind(store, project_id, scene, "playback", req.result_id)
        )
    else:
        paths = PathResultSet.model_validate(
            _load_result_of_kind(store, project_id, scene, "paths", req.result_id)
        )

    try:
        summary = export_aodt(
            project_dir, scene, library, config, req, paths=paths, playback=playback
        )
    except AodtExportUnavailable as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except AodtExportError as exc:
        raise HTTPException(status_code=404, detail=str(exc))

    store.append_provenance(
        project_id,
        {"type": "export_aodt", "source": req.source, "files": summary["files"]},
    )
    publish_event(
        project_id,
        {
            "type": "export_finished",
            "kind": "aodt",
            "export_dir": summary["export_dir"],
            "tables": summary["tables"],
        },
    )
    return AodtExportSummary(**summary)


def _resolve_ue_positions(
    store, project_id: str, scene: Scene, req: ChannelNpzExportRequest
) -> list[list[float]]:
    """UE grid for the channel-dataset export, per ``req.ue_source``."""
    if req.ue_source == "explicit":
        positions = [[float(c) for c in p] for p in (req.ue_positions or [])]
    elif req.ue_source == "devices":
        # Ordered by id so the exported UE axis is stable across runs (the
        # same convention aodt_export uses for its ues table).
        rxs = sorted((d for d in scene.devices if d.kind == "rx"), key=lambda d: d.id)
        if not rxs:
            raise HTTPException(
                status_code=404,
                detail=(
                    'ue_source="devices" but the scene has no rx device; import '
                    "a UE list via POST /import/devices or pass ue_positions"
                ),
            )
        positions = [[float(c) for c in d.position] for d in rxs]
    else:  # "trajectory"
        raw = _load_result_of_kind(
            store, project_id, scene, "trajectory", req.ue_result_id
        )
        result = TrajectoryResultSet.model_validate(raw)
        positions = [[float(c) for c in s.position] for s in result.samples]
        if not positions:
            raise HTTPException(
                status_code=404,
                detail=f"trajectory result {result.result_id} has no samples",
            )
    if len(positions) > MAX_CHANNEL_NPZ_UES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{len(positions)} UE positions exceeds the per-export cap of "
                f"{MAX_CHANNEL_NPZ_UES}; narrow the source or export in batches"
            ),
        )
    for name, column in (("ue_ids", req.ue_ids), ("time_idx", req.time_idx)):
        if column is not None and len(column) != len(positions):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"{name} has {len(column)} entries but the export has "
                    f"{len(positions)} UE positions"
                ),
            )
    return positions


@router.post(
    "/projects/{project_id}/export/channel-npz",
    response_model=ChannelNpzExportSummary,
)
def export_channel_npz_endpoint(
    project_id: str, request: ChannelNpzExportRequest
) -> ChannelNpzExportSummary:
    """Per-link channel dataset npz in the lab's AODT/HYRAY layout.

    One paths solve per UE position: the UE is an EPHEMERAL rx probe added to a
    deep copy of the scene (the mesh-radio-map probe pattern), so the stored
    scene is never edited and no result set is persisted — only
    ``export/channel_npz/`` is written.

    Departure angles are reported in each transmitter's LOCAL array frame,
    built from that TX Device's own ``orientation_deg`` ([yaw, pitch, roll]
    degrees). This is the SAME frame the solver orients the array with:
    ``sionna_backend`` passes ``orientation=radians(orientation_deg)`` to
    Sionna's ``Transmitter``, and sionna-rt composes ``R = Rz(yaw) Ry(pitch)
    Rx(roll)``; the exporter rotates world departure directions by ``R.T``.

    Progress is reported over the project's event stream
    (``simulation_started`` / ``simulation_progress`` / ``simulation_finished``
    with ``kind="channel_npz_export"``) and the run is cancellable via
    ``POST /simulate/cancel``.
    """
    from seam_studio.api.simulate import _solve_guard
    from seam_studio.services import solve_ctx
    from seam_studio.services.channel_npz_export import (
        ChannelNpzExportError,
        export_channel_npz,
    )
    from seam_studio.services.events import publish_event
    from seam_studio.services.simulation_backends import (
        BackendUnavailableError,
        resolve_backend,
    )

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    config = request.config or _resolve_config(scene, request.config_id)
    ue_positions = _resolve_ue_positions(store, project_id, scene, request)
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    project_dir = store.resolve(project_id)

    # Validation above stays outside the guard so a 400/404/409 never announces
    # a solve to the progress card.
    with _solve_guard(project_id, "channel_npz_export"):
        try:
            summary = export_channel_npz(
                backend,
                project_dir,
                scene,
                store.load_materials(project_id),
                config,
                request,
                ue_positions,
                tick=solve_ctx.tick,
            )
        except ChannelNpzExportError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

        store.append_provenance(
            project_id,
            {
                "type": "export_channel_npz",
                "ue_source": request.ue_source,
                "num_ue": summary["num_ue"],
                "num_tx": summary["num_tx"],
                "files": summary["files"],
            },
        )
        # This export runs under the solve guard but persists no result set, so
        # the terminal event _persist_result would normally publish is manual
        # (same shape as simulate/beamforming).
        publish_event(
            project_id,
            {
                "type": "simulation_finished",
                "kind": "channel_npz_export",
                "backend": backend.name,
            },
        )
        publish_event(
            project_id,
            {
                "type": "export_finished",
                "kind": "channel_npz",
                "export_dir": summary["export_dir"],
                "files": summary["files"],
            },
        )
        return ChannelNpzExportSummary(**summary)
