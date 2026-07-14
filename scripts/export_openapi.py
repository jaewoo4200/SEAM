#!/usr/bin/env python
"""Export the FastAPI OpenAPI schema to ``backend/openapi.json``.

This file is the drift reference for the hand-written frontend types
(``frontend/src/types/api.ts``): the generated ``openapi.gen.d.ts`` is produced
from it (``npm run gen:api-types``), and tsc + code review then catch any
contract drift between the two.

Run it from ``backend/`` (the documented spot) ::

    ./.venv/Scripts/python.exe ../scripts/export_openapi.py

or from the repo root ::

    python scripts/export_openapi.py

Either way it writes ``<repo>/backend/openapi.json`` with sorted keys so the
diff is stable across runs. It imports ``app.main`` from the backend package,
adding ``backend/`` to ``sys.path`` when needed so the invocation directory
does not matter.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Repo layout: <repo>/scripts/export_openapi.py, backend package under <repo>/backend.
REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND_DIR = REPO_ROOT / "backend"
OUTPUT_PATH = BACKEND_DIR / "openapi.json"


def _ensure_backend_importable() -> None:
    """Make ``import seam_studio.main`` work regardless of the current directory."""
    backend = str(BACKEND_DIR)
    if backend not in sys.path:
        # Prepend so the backend's ``app`` package wins over any same-named module.
        sys.path.insert(0, backend)


def export(output_path: Path = OUTPUT_PATH) -> Path:
    _ensure_backend_importable()

    # Imported after sys.path is patched so this works from the repo root too.
    from seam_studio.main import app

    schema = app.openapi()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(schema, fh, indent=2, sort_keys=True, ensure_ascii=False)
        fh.write("\n")
    return output_path


def main() -> int:
    written = export()
    # Relative-to-repo path when possible, else absolute - just for a tidy log line.
    try:
        shown = written.relative_to(REPO_ROOT)
    except ValueError:
        shown = written
    print(f"Wrote OpenAPI schema to {shown}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
