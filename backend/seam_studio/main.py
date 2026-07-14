"""SEAM Studio backend entry point.

Run locally:
    uvicorn seam_studio.main:app --reload --port 8000  (from backend/)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from seam_studio.api import (
    agent,
    ai,
    calibrate,
    channel,
    compile as compile_api,
    datasets,
    engines,
    events,
    export,
    health,
    import_osm,
    import_results,
    import_scene,
    materials,
    point_import,
    projects,
    render,
    scenario,
    scene,
    segmentation,
    simulate,
)
from seam_studio.core.config import APP_VERSION


def create_app() -> FastAPI:
    app = FastAPI(
        title="SEAM Studio",
        version=APP_VERSION,
        description=(
            "Unified RF-visual scene authoring, RF material assignment, "
            "Sionna RT projection compilation, and simulation result APIs."
        ),
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    for module in (
        health, projects, import_scene, import_osm, scene, materials, ai,
        compile_api, simulate, export, calibrate, channel, scenario, engines,
        datasets, render, import_results, segmentation, agent, point_import,
    ):
        app.include_router(module.router, prefix="/api")
    # WebSocket event stream is mounted WITHOUT the /api prefix so the path is
    # exactly /ws/projects/{id}/events (frontend contract).
    app.include_router(events.router)
    # Load user plugins (plugins/<name>/plugin.py). Failures are contained in
    # PluginInfo records, never raised - a bad plugin cannot break startup.
    from seam_studio.services.plugins import load_plugins

    load_plugins(app)

    # Bundled frontend (pip install): scripts/build_wheel.py copies the built
    # SPA into app/static, and this serves it at "/". Mounted LAST so /api and
    # /ws always win; absent in dev, where the Vite dev server owns the SPA.
    from seam_studio.core.paths import FRONTEND_DIST

    if (FRONTEND_DIST / "index.html").is_file():
        from fastapi.staticfiles import StaticFiles

        app.mount(
            "/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="spa"
        )
    return app


app = create_app()
