"""Scene render contract: camera parameters for a Mitsuba path-traced still.

The compiled RF projection (``rf/generated_scene.xml``) is rendered directly by
Mitsuba into a PNG. This is the file-export counterpart to Sionna RT's
interactive preview (which renders to screen but has no still-image export).

World is Z-up (HANDOFF.md invariant); ``camera_position``/``look_at`` are in
scene meters, ``up`` is fixed to +Z in the service.
"""

from typing import Annotated

from pydantic import Field

from .common import StrictModel, Vec3


class RenderRequest(StrictModel):
    # Camera eye and target in world meters (Z-up). No sensible defaults exist
    # for an arbitrary scene, so both are required.
    camera_position: Vec3
    look_at: Vec3
    # Horizontal field of view in degrees. Mitsuba's perspective sensor maps
    # ``fov`` to the larger film axis by default; kept in the usual 1-179 range.
    fov_deg: Annotated[float, Field(default=45.0, gt=0.0, lt=180.0)]
    # Resolution. Width capped at 1920 and spp at 256 (service enforces the same
    # caps defensively) to bound render time / VRAM.
    width: Annotated[int, Field(default=1280, gt=0, le=1920)]
    height: Annotated[int, Field(default=720, gt=0, le=1080)]
    # Samples per pixel. Higher = less noise, more time.
    spp: Annotated[int, Field(default=64, gt=0, le=256)]
