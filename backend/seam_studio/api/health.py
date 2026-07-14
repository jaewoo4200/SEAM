"""GET /api/health - app, backend, and AI provider availability.

GET /api/backends adds the per-backend capability map (what each solver can
actually do on this machine) for capability-aware frontends.
"""

from fastapi import APIRouter

from seam_studio.core.config import APP_VERSION, get_settings
from seam_studio.schemas.ai import AIProviderStatus
from seam_studio.schemas.common import SCHEMA_VERSION, StrictModel
from seam_studio.schemas.projects import HealthBackendStatus, HealthResponse
from seam_studio.services.availability import sionna_available

router = APIRouter(tags=["health"])


class BackendCapabilities(StrictModel):
    name: str
    available: bool
    detail: str = ""
    capabilities: dict = {}


@router.get("/backends", response_model=list[BackendCapabilities])
def list_backends() -> list[BackendCapabilities]:
    from seam_studio.services.simulation_backends import get_backend

    out: list[BackendCapabilities] = []
    for name in ("mock", "sionna"):
        try:
            backend = get_backend(name)
            available = backend.is_available()
            out.append(
                BackendCapabilities(
                    name=name,
                    available=available,
                    detail="" if available else "not installed (optional)",
                    capabilities=backend.capabilities(),
                )
            )
        except Exception as exc:  # noqa: BLE001 - listing must never 500
            out.append(
                BackendCapabilities(name=name, available=False, detail=str(exc))
            )
    return out


@router.get("/health", response_model=HealthResponse)
def health() -> HealthResponse:
    settings = get_settings()
    sionna_ok = sionna_available()

    try:
        # Lazy: the AI provider module may probe a local Ollama server.
        from seam_studio.services.ai_provider import get_provider_statuses

        ai_statuses = get_provider_statuses()
    except Exception as exc:  # AI must never break the app (HANDOFF rule 6)
        ai_statuses = [
            AIProviderStatus(name="unknown", available=False, detail=str(exc))
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
