"""SEAM-Agent endpoints (retrieval-augmented RF material authoring).

POST /projects/{id}/agent/material-assignment/start          spawn a job
                                                             (409 while one is
                                                             already running)
GET  /projects/{id}/agent/material-assignment/{jid}/trace    activity trace
                                                             (memory, or disk
                                                             after a restart)
POST /projects/{id}/agent/material-assignment/{jid}/cancel   cooperative stop
POST /projects/{id}/agent/material-assignment/{jid}/apply    bake accepted
                                                             segments (undo
                                                             via segmentation
                                                             /undo)

The trace is the user-visible "thinking": bounded steps, search queries,
evidence cards, live progress/ETA and segment proposals - never raw
chain-of-thought.
"""

from fastapi import APIRouter, HTTPException

from seam_studio.api.deps import get_store, load_scene_or_404
from seam_studio.schemas.seam_agent import (
    AgentApplyRequest,
    AgentApplyResponse,
    AgentCancelResponse,
    AgentStartRequest,
    AgentStartResponse,
    AgentTrace,
)
from seam_studio.services import seam_agent
from seam_studio.services.material_segmentation import SegmentationError

router = APIRouter(tags=["agent"])


@router.post(
    "/projects/{project_id}/agent/material-assignment/start",
    response_model=AgentStartResponse,
)
def agent_start(project_id: str, request: AgentStartRequest) -> AgentStartResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    if scene.prim_by_id(request.prim_id) is None:
        raise HTTPException(status_code=404, detail=f"prim not found: {request.prim_id}")
    b = request.budget
    budget = seam_agent.AgentBudget(
        max_web_searches=b.max_web_searches if b else 6,
        max_image_searches=b.max_image_searches if b else 4,
        max_vlm_calls=b.max_vlm_calls if b else 40,
        max_refine_calls=b.max_refine_calls if b else 3,
        max_runtime_sec=b.max_runtime_sec if b else 600,
    )
    try:
        job_id = seam_agent.start_job(
            project_dir,
            scene,
            request.prim_id,
            [v.model_dump() for v in request.views],
            request.user_hint,
            request.allow_web,
            request.model,
            budget,
        )
    except seam_agent.AgentBusyError as exc:
        # One job per project (and a small global cap): multi-building runs
        # are sequential by design — the FE queues and retries.
        raise HTTPException(status_code=409, detail=str(exc))
    return AgentStartResponse(job_id=job_id)


@router.get(
    "/projects/{project_id}/agent/material-assignment/{job_id}/trace",
    response_model=AgentTrace,
)
def agent_trace(project_id: str, job_id: str) -> AgentTrace:
    store = get_store()
    load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    # Live jobs from memory; finished jobs rehydrate from ai/agent/<id>/ so
    # traces stay reviewable after a backend restart.
    job = seam_agent.resolve_job(project_dir, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    return AgentTrace(
        status=job.status,  # type: ignore[arg-type]
        detail=job.detail or None,
        steps=job.steps,  # type: ignore[arg-type]
        evidence=job.evidence,  # type: ignore[arg-type]
        segments=job.segments,  # type: ignore[arg-type]
        progress=(job.progress or None),  # type: ignore[arg-type]
        views=job.view_assets,  # type: ignore[arg-type]
    )


@router.post(
    "/projects/{project_id}/agent/material-assignment/{job_id}/cancel",
    response_model=AgentCancelResponse,
)
def agent_cancel(project_id: str, job_id: str) -> AgentCancelResponse:
    store = get_store()
    load_scene_or_404(store, project_id)
    accepted = seam_agent.cancel_job(job_id)
    return AgentCancelResponse(status="accepted" if accepted else "not_running")


@router.post(
    "/projects/{project_id}/agent/material-assignment/{job_id}/apply",
    response_model=AgentApplyResponse,
)
def agent_apply(
    project_id: str, job_id: str, request: AgentApplyRequest
) -> AgentApplyResponse:
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    project_dir = store.resolve(project_id)
    job = seam_agent.resolve_job(project_dir, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown job: {job_id}")
    # GLB + scene JSON must move together; same per-project write lock as the
    # segmentation apply/undo routes.
    from seam_studio.services.material_segmentation import project_write_lock

    with project_write_lock(project_dir):
        try:
            scene, info = seam_agent.apply_segments(
                project_dir, scene, job, request.segment_ids
            )
        except SegmentationError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        store.save_scene(project_id, scene)
    store.append_provenance(
        project_id,
        {
            "type": "seam_agent_apply",
            "job_id": job_id,
            "prim_id": job.prim_id,
            "segment_ids": request.segment_ids,
            "added_prim_ids": info["added_prim_ids"],
            "backup_glb": info["backup_glb"],
        },
    )
    return AgentApplyResponse(
        added_prim_ids=info["added_prim_ids"],
        removed_prim_id=info["removed_prim_id"],
        backup_glb=info["backup_glb"],
        batch_id=info["batch_id"],
    )
