"""Import externally-solved results into a project.

    POST /projects/{project_id}/results/import-aodt

Reads NVIDIA AODT parquet exports from a server-local directory, normalizes
them into our result schemas, and persists each via the shared
``simulate._persist_result`` helper (so imported results get canonical ids,
provenance, and a ResultSetRef exactly like a locally-solved result). The
imported sets are stamped with backend "aodt_import".

Status codes:
- 409 when pyarrow is not installed (AODT parquet cannot be read);
- 400 on a bad source directory or malformed/columnless parquet;
- 404 on an unknown project.
"""

from __future__ import annotations

from typing import List

from fastapi import APIRouter, HTTPException
from pydantic import Field

from app.api.deps import get_store, load_scene_or_404
from app.api.simulate import _persist_result
from app.schemas.common import StrictModel
from app.services.aodt_import import (
    AodtImportError,
    AodtImportUnavailable,
    import_aodt_results,
)

router = APIRouter(tags=["import"])

# AODT import kind -> our ResultKind (both parquet names map straight through).
_KIND_MAP = {"paths": "paths", "radio_map": "radio_map"}


class ImportAodtRequest(StrictModel):
    source_dir: str
    kinds: List[str] = Field(default_factory=lambda: ["paths"])


class ImportedResult(StrictModel):
    kind: str
    result_id: str


class ImportAodtResponse(StrictModel):
    imported: List[ImportedResult] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


@router.post(
    "/projects/{project_id}/results/import-aodt", response_model=ImportAodtResponse
)
def import_aodt(project_id: str, request: ImportAodtRequest) -> ImportAodtResponse:
    from pathlib import Path

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    source = Path(request.source_dir)

    imported: list[ImportedResult] = []
    warnings: list[str] = []
    for kind in request.kinds:
        our_kind = _KIND_MAP.get(kind)
        if our_kind is None:
            raise HTTPException(
                status_code=400, detail=f"unsupported import kind: {kind!r}"
            )
        try:
            result = import_aodt_results(source, kind)
        except AodtImportUnavailable as exc:
            raise HTTPException(status_code=409, detail=str(exc))
        except AodtImportError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        warnings.extend(result.warnings)
        persisted = _persist_result(
            project_id, scene, project_dir, our_kind, result.backend,
            result.simulation_config_id, result,
        )
        imported.append(ImportedResult(kind=our_kind, result_id=persisted.result_id))

    return ImportAodtResponse(imported=imported, warnings=warnings)
