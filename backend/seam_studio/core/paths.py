"""Path anchors that work both from a source checkout and a pip install.

Two run modes share this module:

- **Source checkout** (git clone): the package lives at ``<repo>/backend/seam_studio``.
  Project roots default to the repo's ``projects/`` + the committed example,
  and the engines/plugins registries sit at the repo root — the historical
  developer layout.
- **Installed package** (``pip install seam-studio``): the package lives in
  site-packages, so nothing repo-relative exists. Everything user-writable
  moves under ``SEAM_HOME`` (default ``~/.seam``): projects, plugins, and the
  optional multi-venv engine registry. Package data (default RF materials,
  the bundled frontend) is resolved relative to the ``app`` package itself,
  which is valid in both modes.

``SEAM_PROJECT_ROOTS`` / ``SEAM_HOME`` env vars override either mode.
"""

import os
from pathlib import Path
from typing import Optional

# .../app — the package directory itself. Valid in a checkout AND in
# site-packages, unlike any repo-root-relative anchor.
APP_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = APP_ROOT / "data"
DEFAULT_RF_MATERIALS_FILE = DATA_DIR / "default_rf_materials.yaml"

# The production frontend bundle (copied in by scripts/build_wheel.py).
# Absent in dev, where the Vite dev server serves the SPA instead.
FRONTEND_DIST = APP_ROOT / "static"

# Source-checkout marker: in the repo, app's parent is backend/ and carries
# the pyproject; in site-packages the parent is site-packages (no pyproject).
_BACKEND_DIR = APP_ROOT.parent
IS_SOURCE_CHECKOUT = (
    _BACKEND_DIR.name == "backend" and (_BACKEND_DIR / "pyproject.toml").is_file()
)

# Repo root is only meaningful in a source checkout; installed runs must not
# derive paths from it (site-packages' grandparent is a venv, not the repo).
REPO_ROOT: Optional[Path] = _BACKEND_DIR.parent if IS_SOURCE_CHECKOUT else None

# User-writable home for installed runs: projects/, plugins/, engines.json.
SEAM_HOME = Path(
    os.environ.get("SEAM_HOME") or (Path.home() / ".seam")
).expanduser()

# Where projects are looked up, in order. The first root is where new
# projects are created. (SEAM_PROJECT_ROOTS env overrides this entirely —
# see seam_studio.core.config.get_settings.)
if REPO_ROOT is not None:
    DEFAULT_PROJECT_ROOTS = [
        REPO_ROOT / "projects",
        REPO_ROOT / "examples" / "demo_project",
    ]
else:
    DEFAULT_PROJECT_ROOTS = [SEAM_HOME / "projects"]
