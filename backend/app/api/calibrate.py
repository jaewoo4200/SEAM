"""Measurement-based calibration endpoint (HANDOFF Milestone 11).

POST /projects/{project_id}/calibrate/materials
Import measured per-link path gain, fit one RF material parameter by grid
search, and return a before/after report. With apply=true, the fitted value is
written into the project material library and prims using that material are
promoted to assignment_status "measurement_calibrated".
"""

from typing import Optional

from fastapi import APIRouter, HTTPException

from app.api.deps import get_store, load_scene_or_404
from app.schemas.calibration import (
    CalibrationReport,
    CalibrationRequest,
    DisambiguationReport,
    DisambiguationRequest,
)
from app.schemas.scene import RFBinding, Scene
from app.schemas.simulation import SimulateRequest, SimulationConfig
from app.services.simulation_backends import BackendUnavailableError, resolve_backend

router = APIRouter(tags=["calibrate"])


def _resolve_config(scene: Scene, request: CalibrationRequest) -> SimulationConfig:
    if request.config is not None:
        return request.config
    if request.config_id is not None:
        for cfg in scene.simulation_configs:
            if cfg.id == request.config_id:
                return cfg
        raise HTTPException(status_code=404, detail=f"config not found: {request.config_id}")
    return scene.simulation_configs[0] if scene.simulation_configs else SimulationConfig()


@router.post(
    "/projects/{project_id}/calibrate/materials", response_model=CalibrationReport
)
def calibrate_materials(project_id: str, request: CalibrationRequest) -> CalibrationReport:
    from app.services.calibration import calibrate_material

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(scene, request)
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    project_dir = store.resolve(project_id)

    try:
        report = calibrate_material(backend, project_dir, scene, library, config, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if report.applied and report.fitted_value is not None:
        # Persist the fitted parameter and promote prims using this material.
        mat = library.get(request.target_material_id)
        setattr(mat, request.param, report.fitted_value)
        mat.builtin = False
        mat.notes = (mat.notes + " ").strip() + "[measurement-calibrated]"
        store.save_materials(project_id, library)
        promoted = 0
        for prim in scene.prims:
            if prim.rf.material_id == request.target_material_id:
                prim.rf = RFBinding(
                    material_id=prim.rf.material_id,
                    thickness_m=prim.rf.thickness_m,
                    scattering_coefficient=prim.rf.scattering_coefficient,
                    xpd_coefficient=prim.rf.xpd_coefficient,
                    assignment_status="measurement_calibrated",
                    assignment_sources=list(prim.rf.assignment_sources) + ["calibration"],
                    confidence=prim.rf.confidence,
                )
                promoted += 1
        store.save_scene(project_id, scene)
        store.append_provenance(
            project_id,
            {
                "type": "calibrate",
                "material": request.target_material_id,
                "param": request.param,
                "fitted_value": report.fitted_value,
                "rmse_before_db": report.before.rmse_db,
                "rmse_after_db": report.after.rmse_db,
                "prims_promoted": promoted,
            },
        )
    return report


@router.post(
    "/projects/{project_id}/calibrate/disambiguate",
    response_model=DisambiguationReport,
)
def disambiguate(project_id: str, request: DisambiguationRequest) -> DisambiguationReport:
    """Rank candidate RF materials for a prim by measurement fit (the
    RF-sensing disambiguation companion to the AI suggestion flow)."""
    from app.services.calibration import disambiguate_materials

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(scene, request)
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        return disambiguate_materials(
            backend, store.resolve(project_id), scene, library, config, request
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
