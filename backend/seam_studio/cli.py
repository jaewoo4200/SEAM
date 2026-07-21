"""``seam-studio`` console entry point.

Launches the FastAPI backend (which also serves the bundled frontend in a
pip install) and opens the browser. First run bootstraps the Sample Demo
project into the default project root (``~/.seam/projects`` when installed,
the repo roots in a source checkout) so the app never starts empty.

    seam-studio                     # start on 127.0.0.1:8000 + open browser
    seam-studio --port 9000
    seam-studio --project-root D:\\twins   # equivalent to SEAM_PROJECT_ROOTS
    seam-studio --no-browser
"""

from __future__ import annotations

import argparse
import os
import threading
import webbrowser


def _bootstrap_demo() -> None:
    """Create the Sample Demo project when no project exists anywhere.

    Import happens here (after --project-root landed in the environment) so
    the settings cache resolves the roots the user asked for. Never fatal:
    a bootstrap failure still leaves a working (empty) app.
    """
    from seam_studio.api.deps import get_store
    from seam_studio.services.demo_project import create_demo_project

    store = get_store()
    try:
        if not store.list_projects():
            project_dir = create_demo_project(store)
            print(f"first run: created Sample Demo project at {project_dir}")
    except Exception as exc:  # noqa: BLE001 - never block startup on the demo
        print(f"warning: demo project bootstrap failed: {exc}")


def main() -> None:
    # ASCII only: argparse writes help straight to stdout, and on Windows a
    # legacy-codepage console (e.g. Korean cp949) raises UnicodeEncodeError on
    # characters like an em dash, crashing `seam-studio --help`.
    parser = argparse.ArgumentParser(
        prog="seam-studio",
        description=(
            "SEAM Studio - unified RF-visual scene authoring, validation and "
            "Sionna RT simulation workbench."
        ),
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8000, help="port (default: 8000)")
    parser.add_argument(
        "--project-root",
        default=None,
        help="projects directory (default: ~/.seam/projects when installed; "
        "the repo's projects/ + examples in a source checkout). Equivalent to "
        "the SEAM_PROJECT_ROOTS environment variable.",
    )
    parser.add_argument(
        "--no-browser", action="store_true", help="do not open the browser on start"
    )
    args = parser.parse_args()

    # Must land in the environment BEFORE seam_studio.core.config's lru_cached
    # get_settings() is first evaluated (hence the local imports below).
    if args.project_root:
        os.environ["SEAM_PROJECT_ROOTS"] = args.project_root

    import uvicorn

    from seam_studio.core.config import get_settings
    from seam_studio.main import app

    # Ensure the primary root exists so first project creation never 500s.
    roots = get_settings().project_roots
    if roots:
        roots[0].mkdir(parents=True, exist_ok=True)
    _bootstrap_demo()

    url = f"http://{args.host}:{args.port}"
    print(f"SEAM Studio on {url}  (projects: {', '.join(str(r) for r in roots)})")
    if not args.no_browser:
        # Open after a short delay so the first paint hits a live server.
        threading.Timer(1.2, webbrowser.open, args=(url,)).start()

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
