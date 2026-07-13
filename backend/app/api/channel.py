"""Channel-analysis endpoints.

POST /projects/{project_id}/analyze/channel       -> ChannelAnalysisResult
POST /projects/{project_id}/analyze/channel-sweep -> ChannelSweepResult
POST /projects/{project_id}/analyze/spectrogram   -> SpectrogramResult

Solve a single TX->RX link and return the CIR/CFR, dispersion metrics, and an
empirical path-loss model comparison; sweep one link knob across values; or
STFT the coherent h(t) into a Doppler-time spectrogram. All computed on demand
and returned directly (not persisted as result sets — these are interactive
readouts, like beamforming).
"""

from fastapi import APIRouter, HTTPException

from app.api.deps import get_store, load_scene_or_404
from app.schemas.channel import (
    ChannelAnalysisRequest,
    ChannelAnalysisResult,
    ChannelSweepRequest,
    ChannelSweepResult,
    SpectrogramRequest,
    SpectrogramResult,
)
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
        result = channel_analysis.analyze_channel(project_dir, scene, library, request)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:  # unknown config id / missing device
        raise HTTPException(status_code=400, detail=str(exc))
    if request.persist:
        # Store the analysis like any other run (kind "channel") so the
        # Metrics dashboard survives a reload and the run joins the history
        # browser / prune lifecycle. Reuses the simulate module's persist
        # helper for id allocation, size stamping and refs-lock semantics.
        from app.api.simulate import _persist_result

        config_id = (
            request.config.id
            if request.config is not None
            else request.config_id
            or (
                scene.simulation_configs[0].id
                if scene.simulation_configs
                else "default"
            )
        )
        result = _persist_result(
            project_id,
            scene,
            project_dir,
            "channel",
            result.backend,
            config_id,
            result,
        )
    return result


@router.post(
    "/projects/{project_id}/analyze/channel-sweep",
    response_model=ChannelSweepResult,
)
def analyze_channel_sweep(
    project_id: str, request: ChannelSweepRequest
) -> ChannelSweepResult:
    """Link parameter sweep: the single-link analysis re-run per sweep value
    with the swept field patched (nothing persisted)."""
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    project_dir = store.resolve(project_id)
    try:
        return channel_analysis.sweep_channel(project_dir, scene, library, request)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:  # unknown config id / missing device / bad value
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/projects/{project_id}/analyze/spectrogram",
    response_model=SpectrogramResult,
)
def analyze_spectrogram(
    project_id: str, request: SpectrogramRequest | None = None
) -> SpectrogramResult:
    """Doppler-time spectrogram of the coherent channel h(t) (ISAC sensing
    readout): STFT over the per-path Doppler superposition of one link."""
    request = request or SpectrogramRequest()
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    project_dir = store.resolve(project_id)
    try:
        return channel_analysis.analyze_spectrogram(project_dir, scene, library, request)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:  # unknown config id / missing device / bad grid
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
