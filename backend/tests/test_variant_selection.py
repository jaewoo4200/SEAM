"""Unit tests for Dr.Jit/Mitsuba compute-variant selection (`_pick_variant`).

Pure-Python: exercises the CUDA->LLVM preference and the single CPU-fallback
warning WITHOUT sionna-rt, mitsuba, or a GPU installed, so this runs on any
machine (including CI without a GPU). The real solver integration tests live in
test_sionna_backend.py and skip when sionna is absent.

`_pick_variant` takes a plain list of variant names, so importing the backend
module here does not pull in sionna/mitsuba (all heavy imports are lazy inside
the solve methods).
"""

import pytest

from app.services.simulation_backends.sionna_backend import (
    _LLVM_FALLBACK_WARNING,
    _SIONNA_CUDA_VARIANT,
    _SIONNA_LLVM_VARIANT,
    _pick_variant,
)


def test_prefers_cuda_when_available():
    # A CUDA-capable Linux/Windows+NVIDIA build offers both; CUDA wins, silently.
    available = [_SIONNA_CUDA_VARIANT, _SIONNA_LLVM_VARIANT, "scalar_rgb"]
    variant, warnings = _pick_variant(available)
    assert variant == _SIONNA_CUDA_VARIANT
    assert warnings == []  # GPU path is the happy path: no warning


def test_falls_back_to_llvm_with_single_warning():
    # No CUDA variant: an Apple Silicon macOS mitsuba wheel ships only
    # scalar_*/llvm_* (Dr.Jit has no Metal/MPS backend).
    available = ["scalar_rgb", "llvm_ad_rgb", _SIONNA_LLVM_VARIANT]
    variant, warnings = _pick_variant(available)
    assert variant == _SIONNA_LLVM_VARIANT
    # Exactly ONE warning, and it names the CPU cost + the macOS Metal/MPS gap.
    assert warnings == [_LLVM_FALLBACK_WARNING]
    msg = warnings[0]
    assert "CUDA unavailable" in msg
    assert "LLVM" in msg and "CPU" in msg
    assert "macOS" in msg


def test_empty_available_raises_clear_error():
    with pytest.raises(RuntimeError) as excinfo:
        _pick_variant([])
    msg = str(excinfo.value)
    # Error must be actionable and mention the macOS/CPU (LLVM) situation.
    assert "macOS" in msg
    assert "CPU" in msg or "LLVM" in msg
    assert _SIONNA_LLVM_VARIANT in msg


def test_only_scalar_variant_raises_clear_error():
    # scalar_rgb exists but Sionna cannot run on it: still a clear error, not a
    # silent wrong pick.
    with pytest.raises(RuntimeError) as excinfo:
        _pick_variant(["scalar_rgb"])
    msg = str(excinfo.value)
    assert "macOS" in msg
    assert _SIONNA_CUDA_VARIANT in msg and _SIONNA_LLVM_VARIANT in msg
