"""Generate the sample_demo example project.

Thin wrapper: the actual generator lives in the shipped package at
``seam_studio.services.demo_project`` (so ``pip install seam-studio`` can create the
same demo on first run without bundled binary assets). This script just calls
it against a chosen output root.

Run from the repo root:

    backend\\.venv\\Scripts\\python.exe examples/scripts/create_demo_project.py [--out DIR]

Note: output follows the store's canonical layout (``<id>.seam`` folder +
``scene.seam.json``); the committed examples use the same names. Legacy
``.sionnatwin`` projects still load identically.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
sys.path.insert(0, str(BACKEND_DIR))

from seam_studio.services.demo_project import (  # noqa: E402
    GEOMETRY_NAMES,
    PROJECT_ID,
    build_scene,
    create_demo_project,
)
from seam_studio.services.project_store import ProjectStore  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "examples" / "demo_project"


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the sample_demo example project.")
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        help=f"output root directory (default: {DEFAULT_OUT})",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="delete an existing sample_demo and regenerate it",
    )
    args = parser.parse_args()
    out_root = args.out.resolve()
    out_root.mkdir(parents=True, exist_ok=True)

    store = ProjectStore(roots=[out_root])
    # Idempotent by default: a fresh clone already carries the committed demo,
    # and the installers re-run this script on every invocation. The generator
    # itself refuses duplicate ids, so skip (or, with --force, replace) here.
    existing = next((p for p in store.list_projects() if p.project_id == PROJECT_ID), None)
    if existing is not None:
        if not args.force:
            print(f"sample_demo already exists at {existing.path} - keeping it (use --force to regenerate).")
            return
        shutil.rmtree(existing.path)
        store = ProjectStore(roots=[out_root])
    project_dir = create_demo_project(store)

    scene = store.load_scene(PROJECT_ID)
    n_prims, n_devices, n_actors = len(scene.prims), len(scene.devices), len(scene.actors)
    config_ids = [c.id for c in scene.simulation_configs]
    # v0.1.14: + the co-located sensing RX and the drone sensing target;
    # v0.1.15: + TX 2 / TX 3 with their sensing RXs (7 devices) and the
    # sensing_fr1 config. The device count follows the generator.
    expected_devices = len(build_scene().devices)
    expected_configs = ["default", "sensing_fr1"]
    if (
        n_prims != 13
        or n_devices != expected_devices
        or n_actors != 3
        or config_ids != expected_configs
    ):
        raise RuntimeError(
            f"expected 13 prims, {expected_devices} devices, 3 actors and configs "
            f"{expected_configs}, got {n_prims} prims / {n_devices} devices / "
            f"{n_actors} actors / configs {config_ids}"
        )
    print(f"project: {project_dir}")
    print(
        f"prims: {n_prims} (8 mesh + 5 group), devices: {n_devices}, actors: {n_actors}, "
        f"configs: {', '.join(config_ids)}"
    )
    print("GLB mesh names verified:", ", ".join(GEOMETRY_NAMES))


if __name__ == "__main__":
    main()
