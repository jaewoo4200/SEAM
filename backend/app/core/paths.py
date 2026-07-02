"""Repository-relative path anchors."""

from pathlib import Path

# backend/app/core/paths.py -> repo root is three levels above "core".
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_ROOT = REPO_ROOT / "backend"
APP_ROOT = BACKEND_ROOT / "app"
DATA_DIR = APP_ROOT / "data"
DEFAULT_RF_MATERIALS_FILE = DATA_DIR / "default_rf_materials.yaml"

# Where projects are looked up, in order. The first root is where new
# projects are created.
DEFAULT_PROJECT_ROOTS = [
    REPO_ROOT / "projects",
    REPO_ROOT / "examples" / "demo_project",
]
