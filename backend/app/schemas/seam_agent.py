"""SEAM-Agent contracts (retrieval-augmented material authoring).

The FE captures multi-view renders of ONE building prim (RGB + triangle-id
buffers) and starts a bounded agent job; the job exposes an observable
activity trace (steps + evidence cards) and, when done, segment-level RF
material candidates the user reviews and applies.
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel


class AgentView(StrictModel):
    view_id: str
    # JPEG data URL of the rendered view (what the VLM sees).
    rgb_data_url: str
    # PNG data URL; RGB encodes faceIndex as uint24 (r<<16|g<<8|b),
    # background = 0xFFFFFF. Same pixel grid as the RGB render.
    tri_id_png_data_url: str
    width: int
    height: int


class AgentBudgetRequest(StrictModel):
    max_web_searches: int = Field(default=6, ge=0, le=20)
    max_image_searches: int = Field(default=4, ge=0, le=20)
    max_vlm_calls: int = Field(default=40, ge=1, le=200)
    max_runtime_sec: int = Field(default=600, ge=30, le=3600)


class AgentStartRequest(StrictModel):
    prim_id: str
    # Site/building hint driving retrieval, e.g. "한양대학교 퓨전테크센터 FTC".
    user_hint: Optional[str] = None
    # Online evidence is OPT-IN (local-first); off = renders only.
    allow_web: bool = False
    # VLM model override (None = provider default; see the AI model picker).
    model: Optional[str] = None
    views: list[AgentView] = Field(min_length=1, max_length=12)
    budget: Optional[AgentBudgetRequest] = None


class AgentStartResponse(StrictModel):
    job_id: str


class AgentTraceStep(StrictModel):
    step_id: str
    status: Literal["running", "done", "error"]
    summary: str
    queries: Optional[list[str]] = None


class AgentEvidence(StrictModel):
    evidence_id: str
    # web_page | web_image | vlm_claim | mesh_render | rule
    type: str
    claim: str
    source_url: Optional[str] = None
    page_url: Optional[str] = None
    # Servable via GET /projects/{id}/assets/{path} when a thumbnail exists.
    thumb_asset_path: Optional[str] = None
    query: Optional[str] = None


class AgentAlternative(StrictModel):
    rf_material_id: str
    confidence: float


class AgentSegment(StrictModel):
    segment_id: str
    semantic_label: str
    face_count: int
    rf_material_id: str
    confidence: float
    alternatives: list[AgentAlternative] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)


class AgentTrace(StrictModel):
    status: Literal["running", "needs_review", "done", "error"]
    detail: Optional[str] = None
    steps: list[AgentTraceStep] = Field(default_factory=list)
    evidence: list[AgentEvidence] = Field(default_factory=list)
    segments: Optional[list[AgentSegment]] = None


class AgentApplyRequest(StrictModel):
    segment_ids: list[str] = Field(min_length=1)


class AgentApplyResponse(StrictModel):
    added_prim_ids: list[str]
    removed_prim_id: str
    backup_glb: str
    batch_id: str
