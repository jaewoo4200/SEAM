#!/usr/bin/env bash
#
# Start the SEAM Studio backend (uvicorn :8000) and frontend (vite :5173)
# together, print the URLs, and shut both down cleanly on Ctrl+C.
#
# Usage:  bash scripts/start.sh
set -euo pipefail

fail() { printf '[ERROR] %s\n' "$1" >&2; exit 1; }

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

VENV_PYTHON="$REPO_ROOT/backend/.venv/bin/python"
[ -x "$VENV_PYTHON" ] || fail "backend venv not found. Run scripts/install.sh first."
[ -d "$REPO_ROOT/frontend/node_modules" ] || fail "frontend/node_modules not found. Run scripts/install.sh first."

# Kill both child processes when this script exits (Ctrl+C, error, or normal).
pids=()
cleanup() {
    trap - INT TERM EXIT
    for pid in "${pids[@]:-}"; do
        [ -n "$pid" ] && kill "$pid" 2>/dev/null || true
    done
}
trap cleanup INT TERM EXIT

echo "Starting backend  -> http://127.0.0.1:8000  (uvicorn)"
( cd "$REPO_ROOT" && exec "$VENV_PYTHON" -m uvicorn --app-dir backend seam_studio.main:app --port 8000 ) &
pids+=($!)

echo "Starting frontend -> http://localhost:5173  (vite dev, proxies /api to :8000)"
( cd "$REPO_ROOT/frontend" && exec npm run dev ) &
pids+=($!)

cat <<'EOF'

==================================================================
 SEAM Studio is running.
   Backend  : http://127.0.0.1:8000   (API + /api/health)
   Frontend : http://localhost:5173    <- open this
==================================================================
 Press Ctrl+C to stop both servers.
EOF

# Wait for either process to exit; cleanup() then stops the other.
wait -n 2>/dev/null || wait
