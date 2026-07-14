"""Material-segmentation endpoints (multi-material building split).

POST /projects/{id}/segmentation/preview      compute mask + face assignment
POST /projects/{id}/segmentation/upload-mask  stage an external id-mask PNG
GET  /projects/{id}/segmentation/jobs/{jid}   poll a VLM tile-vote job
POST /projects/{id}/segmentation/apply        physically split (GLB backup)
POST /projects/{id}/segmentation/undo         restore the pre-split state

Preview never mutates the scene; apply is the explicit user decision (same
philosophy as AI suggestions) and is recorded in provenance.
"""

from fastapi import APIRouter, File, HTTPException, UploadFile

from seam_studio.api.deps import get_store, load_scene_or_404
from seam_studio.schemas.segmentation import (
    MaskUploadResponse,
    SegmentationApplyRequest,
    SegmentationApplyResponse,
    SegmentationJobStart,
    SegmentationJobStatus,
    SegmentationPreviewRequest,
    SegmentationPreviewResponse,
    SegmentationUndoRequest,
    SegmentationUndoResponse,
    SplitPartsRequest,
    SplitPartsResponse,
)
from seam_studio.services import material_segmentation as seg

router = APIRouter(tags=["segmentation"])


@router.post(
    "/projects/{project_id}/segmentation/preview",
    response_model=SegmentationPreviewResponse | SegmentationJobStart,
)
def segmentation_preview(project_id: str, request: SegmentationPreviewRequest):
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    try:
        if request.mask_source == "vlm_tile_vote":
            job_id = seg.start_vlm_job(
                project_dir,
                scene,
                request.prim_id,
                tile_px=request.tile_px,
                max_tiles=request.max_tiles,
                model=request.model,
                flip_v=request.flip_v,
            )
            return SegmentationJobStart(job_id=job_id)

        _prim, _geom, texture = seg._resolve_textured_prim(
            project_dir, scene, request.prim_id
        )
        if request.mask_source == "user_png":
            if not request.mask_asset_path:
                raise seg.SegmentationError(
                    "mask_asset_path is required for user_png (upload it first)"
                )
            mask_path = (project_dir / request.mask_asset_path).resolve()
            if not mask_path.is_file() or not mask_path.is_relative_to(
                project_dir.resolve()
            ):
                raise seg.SegmentationError(
                    f"mask not found: {request.mask_asset_path}"
                )
            labels = seg.load_user_mask(
                mask_path.read_bytes(), (texture.height, texture.width)
            )
        else:  # color_heuristic
            labels = seg.build_color_heuristic_mask(texture)
        result = seg.segment_preview(
            project_dir,
            scene,
            request.prim_id,
            labels,
            texture,
            source=request.mask_source,
            flip_v=request.flip_v,
        )
    except seg.SegmentationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SegmentationPreviewResponse(**result)


@router.post(
    "/projects/{project_id}/segmentation/upload-mask",
    response_model=MaskUploadResponse,
)
def upload_mask(project_id: str, file: UploadFile = File(...)) -> MaskUploadResponse:
    """Stage an externally produced id-mask PNG under the project.

    Size/id validation happens at preview time against the target prim's
    texture; here we only confirm it decodes as an image and persist it.
    Sync on purpose: image decode runs in the threadpool, not the event loop.
    """
    from PIL import Image
    import io

    store = get_store()
    load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    data = file.file.read()
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"not a readable image: {exc}")
    out_dir = project_dir / "ai" / "segmentation" / "uploads"
    out_dir.mkdir(parents=True, exist_ok=True)
    import uuid

    name = f"mask-{uuid.uuid4().hex[:8]}.png"
    img.save(out_dir / name)
    return MaskUploadResponse(
        mask_asset_path=f"ai/segmentation/uploads/{name}",
        width=img.width,
        height=img.height,
    )


@router.get(
    "/projects/{project_id}/segmentation/jobs/{job_id}",
    response_model=SegmentationJobStatus,
)
def segmentation_job(project_id: str, job_id: str) -> SegmentationJobStatus:
    store = get_store()
    load_scene_or_404(store, project_id)
    job = seg.get_vlm_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    if job["status"] == "done":
        return SegmentationJobStatus(
            status="done", result=SegmentationPreviewResponse(**job["result"])
        )
    if job["status"] == "error":
        return SegmentationJobStatus(status="error", detail=job.get("detail", ""))
    return SegmentationJobStatus(
        status="running",
        progress=int(job.get("progress", 0)),
        total=int(job.get("total", 0)),
    )


@router.post(
    "/projects/{project_id}/segmentation/apply",
    response_model=SegmentationApplyResponse,
)
def segmentation_apply(
    project_id: str, request: SegmentationApplyRequest
) -> SegmentationApplyResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    # GLB + scene JSON must move together (crash between the two writes would
    # silently desync the project); one write lock per project covers both.
    with seg.project_write_lock(project_dir):
        try:
            scene, info = seg.apply_split(
                project_dir, scene, request.prim_id, request.mask_ref,
                flip_v=request.flip_v,
            )
        except seg.SegmentationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "segmentation_split",
            "prim_id": request.prim_id,
            "mask_ref": request.mask_ref,
            "added_prim_ids": info["added_prim_ids"],
            "backup_glb": info["backup_glb"],
        },
    )
    return SegmentationApplyResponse(
        added_prim_ids=info["added_prim_ids"],
        removed_prim_id=info["removed_prim_id"],
        backup_glb=info["backup_glb"],
        batch_id=info["batch_id"],
    )


@router.post(
    "/projects/{project_id}/segmentation/split-parts",
    response_model=SplitPartsResponse,
)
def segmentation_split_parts(
    project_id: str, request: SplitPartsRequest
) -> SplitPartsResponse:
    """Split a merged mesh into connected components (per-building prims)."""
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    with seg.project_write_lock(project_dir):
        try:
            scene, info = seg.split_connected_parts(
                project_dir,
                scene,
                request.prim_id,
                min_faces=request.min_faces,
                max_parts=request.max_parts,
            )
        except seg.SegmentationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "segmentation_split_parts",
            "prim_id": request.prim_id,
            "added_prim_ids": info["added_prim_ids"],
            "backup_glb": info["backup_glb"],
        },
    )
    return SplitPartsResponse(**info)


@router.post(
    "/projects/{project_id}/segmentation/undo",
    response_model=SegmentationUndoResponse,
)
def segmentation_undo(
    project_id: str, request: SegmentationUndoRequest
) -> SegmentationUndoResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    with seg.project_write_lock(project_dir):
        try:
            scene, info = seg.undo_split(project_dir, scene, request.batch_id)
        except seg.SegmentationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "segmentation_undo",
            "batch_id": request.batch_id,
            "restored_prim_id": info["restored_prim_id"],
        },
    )
    return SegmentationUndoResponse(
        restored_prim_id=info["restored_prim_id"],
        removed_prim_ids=info["removed_prim_ids"],
    )
