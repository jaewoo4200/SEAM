"""Optional-dependency probes. Import-light: never import heavy packages here."""

import pkgutil
from functools import lru_cache
from importlib import util


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
