"""Channel-analysis endpoint.

POST /projects/{project_id}/analyze/channel -> ChannelAnalysisResult

Solves a single TX->RX link and returns the CIR/CFR, dispersion metrics, and
an empirical path-loss model comparison. Computed on demand and returned
directly (not persisted as a result set — this is an interactive readout, like
beamforming).
"""

from fastapi import APIRouter, HTTPException

from app.api.deps import get_store, load_scene_or_404
from app.schemas.channel import ChannelAnalysisRequest, ChannelAnalysisResult
from app.schemas.material_impact import MaterialImpactReport, MaterialImpactRequest
from app.services import channel_analysis
from app.services.simulation_backends import BackendUnavailableError, resolve_backend

router = APIRouter(tags=["channel"])


@router.post(
    "/projects/{project_id}/analyze/channel",
    response_model=ChannelAnalysisResult,
)
def analyze_channel(
    project_id: str, request: ChannelAnalysisRequest | None = None
) -> ChannelAnalysisResult:
    request = request or ChannelAnalysisRequest()
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    project_dir = store.resolve(project_id)
    try:
        return channel_analysis.analyze_channel(project_dir, scene, library, request)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:  # unknown config id / missing device
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/projects/{project_id}/analyze/material-impact",
    response_model=MaterialImpactReport,
)
def analyze_material_impact(
    project_id: str, request: MaterialImpactRequest
) -> MaterialImpactReport:
    """Material-aware vs single-material-baseline CFR comparison (NMSE,
    cosine similarity, dRSS, capacity proxy) - the KICS 2026 evaluation."""
    from app.services.material_impact import material_impact

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config_impact(scene, request)
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        return material_impact(
            backend, store.resolve(project_id), scene, library, config, request
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _resolve_config_impact(scene, request: MaterialImpactRequest):
    if request.config is not None:
        return request.config
    if request.config_id is not None:
        for cfg in scene.simulation_configs:
            if cfg.id == request.config_id:
                return cfg
        raise HTTPException(
            status_code=404, detail=f"config not found: {request.config_id}"
        )
    from app.schemas.simulation import SimulationConfig

    return scene.simulation_configs[0] if scene.simulation_configs else SimulationConfig()
