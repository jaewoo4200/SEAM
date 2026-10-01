"""Optional-dependency probes. Import-light: never import heavy packages here."""

import pkgutil
from functools import lru_cache
from importlib import util
from pathlib import Path


@lru_cache(maxsize=1)
def sionna_available() -> bool:
    """True when a Sionna RT implementation is importable.

    Sionna 1.x ships ray tracing as the standalone ``sionna-rt`` package
    (module ``sionna.rt``); Sionna 0.x bundled it inside ``sionna``.

    Deliberately does NOT call find_spec("sionna.rt"): that imports the
    ``sionna`` parent package (pulling TensorFlow), which is slow on every
    /health call and, on a broken install (e.g. the classic Windows TF DLL
    failure), raises OSError/RuntimeError instead of ImportError. We only
    inspect the package's search path for an ``rt`` submodule, and any
    failure whatsoever reads as "unavailable" - a broken Sionna must never
    turn health or auto-backend resolution into a 500.
    """
    try:
        spec = util.find_spec("sionna")
        if spec is None or not spec.submodule_search_locations:
            return False
        return any(
            module.name == "rt"
            for module in pkgutil.iter_modules(list(spec.submodule_search_locations))
        )
    except Exception:
        return False


@lru_cache(maxsize=1)
def sionna_rcs_available() -> bool:
    """True when the installed sionna-rt ships the RCS module (sionna.rt.rcs,
    sionna-rt >= 2.2). Import-light: inspects the package tree only.

    Importing sionna here would pin a Dr.Jit variant before
    ``_ensure_sionna_variant`` gets to choose it, so this never imports."""
    if not sionna_available():
        return False
    try:
        spec = util.find_spec("sionna")
        if spec is None or not spec.submodule_search_locations:
            return False
        return any(
            (Path(location) / "rt" / "rcs" / "solver.py").is_file()
            for location in spec.submodule_search_locations
        )
    except Exception:
        return False


def sionna_backend_detail(available: bool) -> str:
    """Health/backends detail line for the sionna backend (one wording for
    /api/health and simulation_backends.available_backends)."""
    if not available:
        return "sionna-rt not installed (optional)"
    return (
        "sensing (RCS) available"
        if sionna_rcs_available()
        else "sensing needs sionna-rt>=2.2"
    )
