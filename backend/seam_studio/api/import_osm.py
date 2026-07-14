"""One-shot OpenStreetMap import route.

POST /api/projects/import-osm -> a ready-to-simulate project built from OSM
building footprints (extruded) plus a ground plane, with RF materials
pre-assigned. The heavy lifting lives in ``app.services.osm_import``; this
module is a thin request/response + error-mapping shell.

Error contract:
- 400 invalid arguments, unknown material id, or an id that already exists;
- 502 the Overpass API was unreachable or returned garbage;
- 504 the Overpass API timed out.
"""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import Field

from seam_studio.api import deps
from seam_studio.schemas.common import StrictModel
from seam_studio.services.osm_import import (
    OverpassError,
    OverpassTimeout,
    import_osm_project,
)
from seam_studio.services.project_store import load_default_library

router = APIRouter(tags=["projects"])


class ImportOSMRequest(StrictModel):
    # Slug; derived from name when null. Same charset as ProjectCreateRequest.
    project_id: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_\-]+$")
    name: str = Field(min_length=1)
    lat: float = Field(ge=-90.0, le=90.0)
    lon: float = Field(ge=-180.0, le=180.0)
    # E-W span (width) and N-S span (height) of the rectangle, in meters.
    width_m: float = Field(default=500.0, ge=50.0, le=3000.0)
    height_m: float = Field(default=500.0, ge=50.0, le=3000.0)
    default_building_material: str = "itu_concrete"
    ground_material: str = "ground_28ghz"
    # Used when a footprint has no height / building:levels tag.
    default_building_height_m: float = Field(default=10.0, gt=0.0)


class ImportOSMResponse(StrictModel):
    project_id: str
    num_buildings: int
    num_skipped: int
    warnings: list[str] = Field(default_factory=list)


@router.post("/projects/import-osm", response_model=ImportOSMResponse)
def import_osm(req: ImportOSMRequest) -> ImportOSMResponse:
    store = deps.get_store()
    library = load_default_library()
    try:
        result = import_osm_project(
            store,
            library,
            name=req.name,
            lat=req.lat,
            lon=req.lon,
            width_m=req.width_m,
            height_m=req.height_m,
            project_id=req.project_id,
            default_building_material=req.default_building_material,
            ground_material=req.ground_material,
            default_building_height_m=req.default_building_height_m,
        )
    except OverpassTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc))
    except OverpassError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    except ValueError as exc:
        # invalid args, unknown material, or "project already exists".
        raise HTTPException(status_code=400, detail=str(exc))
    return ImportOSMResponse(**result)
