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
    export,
    health,
    materials,
    projects,
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
        health, projects, scene, materials, ai, compile_api, simulate, export,
        calibrate, channel, scenario,
    ):
        app.include_router(module.router, prefix="/api")
    return app


app = create_app()
