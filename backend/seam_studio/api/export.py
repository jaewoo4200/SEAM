"""Export endpoints.

POST /projects/{project_id}/export/rfdata  -> writes the AODT viewer contract
(scenario_meta/devices/paths/trajectory/radio_map/calibration_points) under
export/rfdata/ and returns a summary of what was written.

POST /projects/{project_id}/export/aodt    -> writes NVIDIA AODT's OFFICIAL
results-schema parquet tables under export/aodt/ (409 when pyarrow is missing,
404 when the requested source result is absent).
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from seam_studio.api.deps import get_store, load_scene_or_404
from seam_studio.schemas.results import (
    AodtExportRequest,
    AodtExportSummary,
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
