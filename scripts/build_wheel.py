"""Build the pip-installable seam-studio wheel (frontend bundled).

Steps:
  1. `npm run build` in frontend/  (tsc -b + vite build -> frontend/dist)
  2. copy frontend/dist -> backend/seam_studio/static   (gitignored; wheel-only data)
  3. `python -m build` in backend/              (wheel + sdist -> backend/dist)

Run from anywhere:

    python scripts/build_wheel.py [--skip-frontend]

`--skip-frontend` reuses an existing frontend/dist (CI builds it in its own
step). Requires `pip install build` in the running interpreter.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTEND = REPO_ROOT / "frontend"
FRONTEND_DIST = FRONTEND / "dist"
BACKEND = REPO_ROOT / "backend"
STATIC_DEST = BACKEND / "seam_studio" / "static"


def run(cmd: list[str], cwd: Path) -> None:
    print(f"$ {' '.join(cmd)}  (cwd={cwd})")
    subprocess.run(cmd, cwd=cwd, check=True, shell=(cmd[0] == "npm"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-frontend",
        action="store_true",
        help="reuse an existing frontend/dist instead of rebuilding",
    )
    args = parser.parse_args()

    if not args.skip_frontend:
        run(["npm", "run", "build"], cwd=FRONTEND)
    if not (FRONTEND_DIST / "index.html").is_file():
        sys.exit("frontend/dist/index.html missing — frontend build failed?")

    if STATIC_DEST.exists():
        shutil.rmtree(STATIC_DEST)
    shutil.copytree(FRONTEND_DIST, STATIC_DEST)
    print(f"bundled SPA -> {STATIC_DEST}")

    # Clean previous artifacts so the upload step never grabs a stale wheel.
    dist = BACKEND / "dist"
    if dist.exists():
        shutil.rmtree(dist)
    run([sys.executable, "-m", "build"], cwd=BACKEND)

    for artifact in sorted(dist.iterdir()):
        print(f"built: {artifact.name}  ({artifact.stat().st_size / 1e6:.1f} MB)")


if __name__ == "__main__":
    main()
