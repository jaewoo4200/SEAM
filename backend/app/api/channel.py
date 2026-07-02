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
from app.services import channel_analysis
from app.services.simulation_backends import BackendUnavailableError

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
