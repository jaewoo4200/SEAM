"""ML ground-truth dataset endpoints.

POST /projects/{id}/datasets/generate  -> DatasetInfo (synchronous sweep)
GET  /projects/{id}/datasets           -> DatasetListResponse
GET  /projects/{id}/datasets/{did}/files/{name} -> file download

See docs/ml_datasets.md for the array layout and a training example.
"""

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from seam_studio.api.deps import get_store, load_scene_or_404
from seam_studio.schemas.datasets import (
    DatasetDeleteResponse,
    DatasetGenerateRequest,
    DatasetInfo,
    DatasetListResponse,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import dataset as dataset_service
from seam_studio.services.simulation_backends import BackendUnavailableError, resolve_backend

router = APIRouter(tags=["datasets"])


def _resolve_config(scene, request: DatasetGenerateRequest) -> SimulationConfig:
    if request.config is not None:
        return request.config
    if request.config_id is not None:
        for cfg in scene.simulation_configs:
            if cfg.id == request.config_id:
                return cfg
        raise ValueError(f"unknown config_id {request.config_id!r}")
    return scene.simulation_configs[0] if scene.simulation_configs else SimulationConfig()


@router.post("/projects/{project_id}/datasets/generate", response_model=DatasetInfo)
def generate_dataset(project_id: str, request: DatasetGenerateRequest) -> DatasetInfo:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    project_dir = store.resolve(project_id)
    try:
        config = _resolve_config(scene, request)
        backend = resolve_backend(config)
        return dataset_service.generate_dataset(
            project_dir, scene, library, config, request, backend
        )
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.get("/projects/{project_id}/datasets", response_model=DatasetListResponse)
def list_datasets(project_id: str) -> DatasetListResponse:
    store = get_store()
    load_scene_or_404(store, project_id)  # 404 on unknown project
    return DatasetListResponse(
        datasets=dataset_service.list_datasets(store.resolve(project_id))
    )


@router.delete(
    "/projects/{project_id}/datasets/{dataset_id}",
    response_model=DatasetDeleteResponse,
)
def delete_dataset(project_id: str, dataset_id: str) -> DatasetDeleteResponse:
    """Permanently remove a generated dataset directory.

    404 on an unknown project (via load_scene_or_404) or an unknown dataset;
    ``dataset_id`` is validated against path traversal the same way the file
    download route validates it, so it can never point removal outside the
    project's datasets root.
    """
    store = get_store()
    load_scene_or_404(store, project_id)  # 404 on unknown project
    if not dataset_service.delete_dataset(store.resolve(project_id), dataset_id):
        raise HTTPException(status_code=404, detail=f"dataset not found: {dataset_id}")
    return DatasetDeleteResponse(deleted=True, dataset_id=dataset_id)


@router.get("/projects/{project_id}/datasets/{dataset_id}/files/{filename}")
def download_dataset_file(project_id: str, dataset_id: str, filename: str):
    store = get_store()
    load_scene_or_404(store, project_id)
    path = dataset_service.dataset_file(store.resolve(project_id), dataset_id, filename)
    if path is None:
        raise HTTPException(status_code=404, detail="dataset file not found")
    return FileResponse(path, filename=path.name)
