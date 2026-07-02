"""POST /projects/{project_id}/compile/sionna - compile the RF projection.

A failed compile (validation errors, missing meshes) is a domain result, not
a transport failure: the endpoint returns 200 with ok=False and the errors in
the CompileResult body. Only an unknown project is a 404.
"""

from fastapi import APIRouter

from app.api.deps import get_store, load_scene_or_404
from app.schemas.compile import CompileResult
from app.services.rf_compiler import compile_project

router = APIRouter(tags=["compile"])


@router.post("/projects/{project_id}/compile/sionna", response_model=CompileResult)
def compile_sionna(project_id: str) -> CompileResult:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    project_dir = store.resolve(project_id)

    result = compile_project(project_dir, scene, library)

    store.append_provenance(
        project_id,
        {"type": "compile", "ok": result.ok, "groups": len(result.material_groups)},
    )
    return result
