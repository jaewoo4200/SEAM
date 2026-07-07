#!/usr/bin/env bash
#
# SionnaTwin Studio one-command installer (Linux / macOS).
#
# Idempotent: re-running is safe. Creates backend/.venv if missing, installs the
# backend (editable, with dev extras), installs the frontend, regenerates the
# demo projects, and prints next steps.
#
# The real ray-tracing engine (sionna-rt) is NOT installed here: the Mock
# backend always works and the whole app runs without a GPU. See INSTALL.md for
# the optional `sionna` extra and alternate engine venvs.
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

command -v npm >/dev/null 2>&1 || fail "npm not found. Install Node.js 20+ (https://nodejs.org/)."
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

step "Installing backend (editable + dev extras)"
"$VENV_PYTHON" -m pip install --upgrade pip || fail "pip upgrade failed."
"$VENV_PYTHON" -m pip install -e "backend[dev]" || fail "backend install failed (pip install -e 'backend[dev]')."

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
 SionnaTwin Studio install complete.
==================================================================

 Next steps:
   1. Start both servers:   bash scripts/start.sh
   2. Open the app:         http://localhost:5173
      (the Sample Demo project loads automatically)

 Walkthrough: TUTORIAL.md    Install details/troubleshooting: INSTALL.md
EOF
