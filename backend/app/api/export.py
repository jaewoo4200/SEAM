"""RFData export endpoint.

POST /projects/{project_id}/export/rfdata  -> writes the AODT viewer contract
(scenario_meta/devices/paths/trajectory/radio_map/calibration_points) under
export/rfdata/ and returns a summary of what was written.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException

from app.api.deps import get_store, load_scene_or_404
from app.schemas.results import PathResultSet, RadioMapResultSet, TrajectoryResultSet
from app.schemas.scene import Scene
from app.schemas.simulation import SimulateRequest, SimulationConfig

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


@router.post("/projects/{project_id}/export/rfdata")
def export_rfdata_endpoint(project_id: str, request: Optional[SimulateRequest] = None) -> dict:
    from app.services.rfdata_export import export_rfdata

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
    return summary
