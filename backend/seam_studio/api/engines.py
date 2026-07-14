"""Compute-engine registry endpoint."""

from fastapi import APIRouter

from ..schemas.engines import EngineListResponse
from ..services import engines as engine_registry

router = APIRouter(tags=["engines"])


@router.get("/engines", response_model=EngineListResponse)
def list_engines(refresh: bool = False) -> EngineListResponse:
    """Installed compute engines. Availability probes are cached per process;
    pass refresh=true after installing a new engine venv."""
    return EngineListResponse(engines=engine_registry.list_engines(refresh=refresh))
