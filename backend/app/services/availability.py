"""Optional-dependency probes. Import-light: never import heavy packages here."""

from functools import lru_cache
from importlib import util


@lru_cache(maxsize=1)
def sionna_available() -> bool:
    """True when a Sionna RT implementation is importable.

    Sionna 1.x ships ray tracing as the standalone ``sionna-rt`` package
    (module ``sionna.rt``); Sionna 0.x bundled it inside ``sionna``. We only
    probe for module specs - no heavy import happens here.
    """
    try:
        if util.find_spec("sionna") is None:
            return False
        return util.find_spec("sionna.rt") is not None
    except (ImportError, ModuleNotFoundError, ValueError):
        return False
