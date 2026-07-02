"""GET /api/health - app, backend, and AI provider availability."""

from fastapi import APIRouter

from app.core.config import APP_VERSION, get_settings
from app.schemas.common import SCHEMA_VERSION
from app.schemas.projects import HealthBackendStatus, HealthResponse
from app.services.availability import sionna_available

router = APIRouter(tags=["health"])


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    sionna_ok = sionna_available()

    ai_statuses: list[dict] = []
    try:
        # Lazy: the AI provider module may probe a local Ollama server.
        from app.services.ai_provider import get_provider_statuses

        ai_statuses = [s.model_dump(mode="json") for s in get_provider_statuses()]
    except Exception as exc:  # AI must never break the app (HANDOFF rule 6)
        ai_statuses = [
            {"name": "unknown", "available": False, "model": None, "detail": str(exc)}
        ]

    return HealthResponse(
        version=APP_VERSION,
        schema_version=SCHEMA_VERSION,
        sionna_available=sionna_ok,
        backends=[
            HealthBackendStatus(name="mock", available=True, detail="always available"),
            HealthBackendStatus(
                name="sionna",
                available=sionna_ok,
                detail="" if sionna_ok else "sionna-rt not installed (optional)",
            ),
        ],
        ai_providers=ai_statuses,
        project_roots=[str(r) for r in settings.project_roots],
    )
