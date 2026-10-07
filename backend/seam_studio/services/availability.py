"""Optional-dependency probes. Import-light: never import heavy packages here."""

import json
import os
import pkgutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import lru_cache
from importlib import util
from pathlib import Path
from typing import Optional


@lru_cache(maxsize=1)
def sionna_installed() -> bool:
    """True when a Sionna RT implementation is on disk (file check only).

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


@dataclass(frozen=True)
class SionnaRuntime:
    """What the installed Dr.Jit/Mitsuba/Sionna stack can actually run."""

    cuda: bool
    llvm: bool
    rt_import: bool
    error: Optional[str]
    timed_out: bool = False


# Runs in a child interpreter: importing drjit/mitsuba/sionna.rt here would pin
# a Dr.Jit variant in the server process before _ensure_sionna_variant picks
# one, and a missing LLVM-C library only shows up at import time.
_PROBE_SRC = r"""
import json
out = {"cuda": False, "llvm": False, "rt": False, "error": None}
try:
    import drjit as dr
    out["cuda"] = bool(dr.has_backend(dr.JitBackend.CUDA))
    out["llvm"] = bool(dr.has_backend(dr.JitBackend.LLVM))
    import mitsuba  # noqa: F401
    if out["cuda"] or out["llvm"]:
        import sionna.rt  # noqa: F401
        out["rt"] = True
except BaseException as exc:
    out["error"] = (type(exc).__name__ + ": " + str(exc)).strip()
print(json.dumps(out))
"""

_PROBE_TIMEOUT_S = 60


def _last_line(text: Optional[str]) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _run_runtime_probe() -> SionnaRuntime:
    """One child-interpreter probe (uncached; see :func:`sionna_runtime`)."""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", _PROBE_SRC],
            capture_output=True,
            text=True,
            timeout=_PROBE_TIMEOUT_S,
            env=os.environ.copy(),
        )
    except subprocess.TimeoutExpired:
        return SionnaRuntime(False, False, False, "runtime probe timed out", timed_out=True)
    except Exception as exc:  # noqa: BLE001 - probing must never raise
        return SionnaRuntime(False, False, False, f"runtime probe failed: {exc}")
    # The JSON line wins even on a non-zero exit: a child that finished the
    # probe and then crashed in interpreter teardown still measured the runtime.
    try:
        data = json.loads(_last_line(proc.stdout))
        return SionnaRuntime(
            cuda=bool(data.get("cuda")),
            llvm=bool(data.get("llvm")),
            rt_import=bool(data.get("rt")),
            error=data.get("error") or None,
        )
    except Exception:  # noqa: BLE001 - unparsable output reads as unusable
        reason = _last_line(proc.stderr) or f"runtime probe exited with {proc.returncode}"
        return SionnaRuntime(False, False, False, reason)


_runtime_cached = lru_cache(maxsize=1)(_run_runtime_probe)
# lru_cache alone lets concurrent cold callers (/health and /backends on page
# load) each start a ~1 s child; the lock makes them share one.
_runtime_lock = threading.Lock()


def sionna_runtime() -> SionnaRuntime:
    """Probe the Dr.Jit backends and ``import sionna.rt`` once per process.

    Only meaningful when :func:`sionna_installed` is True. Never raises: any
    failure of the child reads as "nothing works" with the reason in ``error``.
    """
    with _runtime_lock:
        return _runtime_cached()


sionna_runtime.cache_clear = _runtime_cached.cache_clear
sionna_runtime.__wrapped__ = _run_runtime_probe


@lru_cache(maxsize=1)
def sionna_available() -> bool:
    """True when Sionna RT is installed AND can run on this machine.

    Installed alone is not enough: without a CUDA device Dr.Jit needs the
    LLVM-C shared library (not shipped by pip), and ``import sionna.rt`` then
    fails, so every solve would come back empty. A probe that times out never
    hides an installed engine (a slow first import on a busy box)."""
    if not sionna_installed():
        return False
    probe = sionna_runtime()
    return probe.timed_out or ((probe.cuda or probe.llvm) and probe.rt_import)


def sionna_unavailable_reason() -> Optional[str]:
    """Why an installed Sionna cannot run here; None when it is available or
    simply not installed."""
    if not sionna_installed() or sionna_available():
        return None
    probe = sionna_runtime()
    if probe.timed_out:
        return None
    if not probe.cuda and not probe.llvm:
        if probe.error is not None:
            # drjit/mitsuba failed to import or the probe child crashed:
            # installing LLVM would not help, so name the actual failure.
            return (
                "sionna-rt installed but its Dr.Jit/Mitsuba runtime failed to load: "
                f"{probe.error}"
            )
        return (
            "sionna-rt installed but no Dr.Jit backend works (no CUDA device and "
            "LLVM-C not found); set DRJIT_LIBLLVM_PATH to LLVM-C.dll/libLLVM"
        )
    if not probe.rt_import:
        return f"sionna-rt installed but sionna.rt failed to import: {probe.error}"
    return None


def auto_fallback_note(config, backend_name: str) -> Optional[str]:
    """The warning every output of an ``auto`` request that ran the mock
    because an installed Sionna cannot run here carries (None otherwise; an
    explicit ``mock`` request is the user's choice and gets no note)."""
    if config is None or getattr(config, "backend", None) != "auto" or backend_name != "mock":
        return None
    reason = sionna_unavailable_reason()
    return None if reason is None else f"auto backend: {reason}; ran the mock backend"


def note_auto_fallback(warnings: list[str], config, backend_name: str) -> None:
    """Append :func:`auto_fallback_note` to ``warnings`` (once) when it applies."""
    note = auto_fallback_note(config, backend_name)
    if note is not None and note not in warnings:
        warnings.append(note)


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
    /api/health, /api/backends and simulation_backends.available_backends)."""
    if not available:
        return sionna_unavailable_reason() or "sionna-rt not installed (optional)"
    detail = (
        "sensing (RCS) available"
        if sionna_rcs_available()
        else "sensing needs sionna-rt>=2.2"
    )
    if sionna_installed() and sionna_runtime().timed_out:
        detail += " (runtime probe timed out; assuming usable)"
    return detail
