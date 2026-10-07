#!/usr/bin/env bash
#
# SEAM Studio one-command installer (Linux / macOS).
#
# Idempotent: re-running is safe. Creates backend/.venv if missing, installs the
# backend (editable, with the dev and parquet extras), installs the frontend,
# regenerates the demo projects, checks which Dr.Jit compute backend works, and
# prints next steps.
#
# The real ray-tracing engine (sionna-rt) is a base backend dependency, so it
# installs here too (Dr.Jit/Mitsuba, no GPU needed to install). Running it needs
# an NVIDIA GPU (CUDA) or LLVM-C for the CPU backend (LLVM 18+, see
# INSTALL.md); with neither, 'auto' solves use the Mock
# backend. See INSTALL.md for alternate engine venvs.
#
# Usage:  bash scripts/install.sh
set -euo pipefail

fail() { printf '\n[ERROR] %s\n' "$1" >&2; exit 1; }
step() { printf '\n==> %s\n' "$1"; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

VENV_DIR="$REPO_ROOT/backend/.venv"
VENV_PYTHON="$VENV_DIR/bin/python"

# ------------------------------------------------------------ prerequisites
step "Checking prerequisites"

PY_BIN=""
if command -v python3 >/dev/null 2>&1; then PY_BIN="python3"
elif command -v python >/dev/null 2>&1; then PY_BIN="python"
else fail "Python not found. Install Python 3.11+ (https://www.python.org/downloads/)."; fi
echo "  python: $(command -v "$PY_BIN")"

command -v npm >/dev/null 2>&1 || fail "npm not found. Install Node.js 20+ and re-run: Ubuntu/Debian 'sudo apt install nodejs npm' (20+ via nodesource), macOS 'brew install node@20', or https://nodejs.org/."
echo "  npm:    $(command -v npm)"

# ------------------------------------------------------------ backend venv
step "Backend virtual environment (backend/.venv)"
if [ ! -x "$VENV_PYTHON" ]; then
    echo "  creating venv..."
    "$PY_BIN" -m venv "$VENV_DIR" || fail "python -m venv failed."
else
    echo "  venv already exists, reusing."
fi
[ -x "$VENV_PYTHON" ] || fail "venv python missing after creation: $VENV_PYTHON"

step "Installing backend (editable + dev and parquet extras)"
"$VENV_PYTHON" -m pip install --upgrade pip || fail "pip upgrade failed."
"$VENV_PYTHON" -m pip install -e "backend[dev,parquet]" || fail "backend install failed (pip install -e 'backend[dev,parquet]')."

# ------------------------------------------------------------ compute backend
step "Checking the Sionna RT compute backend (Dr.Jit CUDA / LLVM)"
# stderr dropped: drjit prints a harmless 'jitc_llvm_init(): LLVM API
# initialization failed' line when LLVM-C is absent.
PROBE="$("$VENV_PYTHON" -c 'import drjit as dr; print(int(dr.has_backend(dr.JitBackend.CUDA)), int(dr.has_backend(dr.JitBackend.LLVM)))' 2>/dev/null | grep -E '^[01] [01]$' | tail -n 1 || true)"
case "$PROBE" in
    "1 "*) echo "  CUDA backend available (GPU solves)." ;;
    "0 1") echo "  LLVM CPU backend available (no CUDA device; solves run on the CPU)." ;;
    "0 0")
        echo "  No CUDA device and no LLVM-C found: Sionna RT cannot run here, so 'auto' solves use the Mock backend."
        if [ "$(uname -s)" = "Darwin" ]; then
            echo "  For the CPU backend: brew install llvm, then"
            echo "     export DRJIT_LIBLLVM_PATH=\"\$(brew --prefix llvm)/lib/libLLVM.dylib\""
            echo "  (add it to your shell profile)."
        else
            echo "  For the CPU backend install LLVM 18+: sudo apt install libllvm18 (Ubuntu 24.04;"
            echo "  Ubuntu 22.04 / Debian 12 default to LLVM 14: use libllvm18 from https://apt.llvm.org)."
            echo "  Dr.Jit finds /usr/lib/<arch>-linux-gnu/libLLVM*.so.* itself: leave DRJIT_LIBLLVM_PATH"
            echo "  unset unless the library is elsewhere (a wrong path disables the CPU backend)."
        fi
        echo "  See INSTALL.md (CPU-only machines)." ;;
    *) echo "  could not probe Dr.Jit (import drjit failed); solves will use the Mock backend. See INSTALL.md." ;;
esac

# ------------------------------------------------------------ frontend
step "Installing frontend (npm install in frontend/)"
( cd "$REPO_ROOT/frontend" && npm install ) || fail "npm install failed."

# ------------------------------------------------------------ demo projects
# Demo projects (sample_demo + lab_room + ftc_outdoor) are COMMITTED under
# examples/demo_project/. Regeneration from the reference bundle is optional:
# a fresh clone without reference-bundle/ must still install cleanly.
step "Regenerating demo projects (sample_demo + lab_room + ftc_outdoor)"
"$VENV_PYTHON" "$REPO_ROOT/examples/scripts/create_demo_project.py" || fail "create_demo_project.py failed."
if [ -f "$REPO_ROOT/reference-bundle/indoor/lab_room.xml" ]; then
    "$VENV_PYTHON" "$REPO_ROOT/examples/scripts/import_bundle_scene.py" || fail "import_bundle_scene.py failed."
else
    echo " reference-bundle/ not present - keeping the committed lab_room demo."
fi
if [ -f "$REPO_ROOT/reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" ]; then
    "$VENV_PYTHON" "$REPO_ROOT/examples/scripts/import_bundle_scene.py" \
        --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" \
        --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor \
        --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb" \
        || fail "import_bundle_scene.py (ftc_outdoor) failed."
else
    echo " reference-bundle/ not present - keeping the committed ftc_outdoor demo."
fi

# ------------------------------------------------------------ done
cat <<'EOF'

==================================================================
 SEAM Studio install complete.
==================================================================

 Next steps:
   1. Start both servers:   bash scripts/start.sh
   2. Open the app:         http://localhost:5173
      (the Sample Demo project loads automatically)

 Walkthrough: TUTORIAL.md    Install details/troubleshooting: INSTALL.md
EOF
