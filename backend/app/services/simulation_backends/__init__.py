"""Ray-tracing backend registry.

Backends are constructed on demand and are stateless, so instances are cheap.
"auto" resolves to the real Sionna backend when installed, else the mock -
the app must work with no Sionna and no GPU (HANDOFF.md section 7).
"""

from app.schemas.projects import HealthBackendStatus
from app.schemas.simulation import SimulationConfig

from .base import BackendUnavailableError, RayTracingBackend
from .mock_backend import MockBackend
from .sionna_backend import SionnaBackend

_BACKENDS: dict[str, type[RayTracingBackend]] = {
    "mock": MockBackend,
    "sionna": SionnaBackend,
}

__all__ = [
    "BackendUnavailableError",
    "RayTracingBackend",
    "MockBackend",
    "SionnaBackend",
    "get_backend",
    "resolve_backend",
    "available_backends",
]


def get_backend(name: str) -> RayTracingBackend:
    try:
        return _BACKENDS[name]()
    except KeyError:
        raise ValueError(
            f"unknown backend: {name!r} (expected one of {sorted(_BACKENDS)})"
        ) from None


def resolve_backend(config: SimulationConfig) -> RayTracingBackend:
    if config.backend == "auto":
        sionna = get_backend("sionna")
        return sionna if sionna.is_available() else get_backend("mock")
    backend = get_backend(config.backend)
    if not backend.is_available():
        raise BackendUnavailableError(
            f"backend {backend.name!r} is not available on this machine"
        )
    return backend


def available_backends() -> list[HealthBackendStatus]:
    mock = MockBackend()
    sionna = SionnaBackend()
    sionna_ok = sionna.is_available()
    return [
        HealthBackendStatus(name=mock.name, available=True, detail="always available"),
        HealthBackendStatus(
            name=sionna.name,
            available=sionna_ok,
            detail="" if sionna_ok else "sionna-rt not installed (optional)",
        ),
    ]
