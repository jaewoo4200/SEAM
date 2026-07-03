"""SionnaTwin Studio backend entry point.

Run locally:
    uvicorn app.main:app --reload --port 8000  (from backend/)
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api import (
    ai,
    calibrate,
    channel,
    compile as compile_api,
    datasets,
    engines,
    export,
    health,
    import_scene,
    materials,
    projects,
    render,
    scenario,
    scene,
    simulate,
)
from app.core.config import APP_VERSION


def create_app() -> FastAPI:
    app = FastAPI(
        title="SionnaTwin Studio",
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
        health, projects, import_scene, scene, materials, ai, compile_api,
        simulate, export, calibrate, channel, scenario, engines, datasets,
        render,
    ):
        app.include_router(module.router, prefix="/api")
    # Load user plugins (plugins/<name>/plugin.py). Failures are contained in
    # PluginInfo records, never raised - a bad plugin cannot break startup.
    from app.services.plugins import load_plugins

    load_plugins(app)
    return app


app = create_app()
