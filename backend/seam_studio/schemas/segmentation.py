"""Material-segmentation contracts (multi-material building split).

Preview computes a mask + per-face assignment and persists reviewable
artifacts under ai/segmentation/<batch>/; apply physically splits the prim's
mesh into per-material sub-prims (with a GLB backup for undo). The mask
sources mirror the FTC pipeline tiers: instant color heuristic, local VLM
tile vote, or an externally produced id-mask PNG (SAM2/DINOv2 grade).
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel

MaskSource = Literal["color_heuristic", "vlm_tile_vote", "user_png"]


class SegmentationPreviewRequest(StrictModel):
    prim_id: str
    mask_source: MaskSource = "color_heuristic"
    # Mask images are top-left-origin; GLB UVs are bottom-left. Leave True
    # unless the atlas was authored with an already-flipped V.
    flip_v: bool = True
    # vlm_tile_vote tuning (ignored otherwise).
    tile_px: int = Field(default=512, ge=64, le=2048)
    max_tiles: int = Field(default=64, ge=4, le=256)
    model: Optional[str] = None
    # user_png: project-relative path of a previously uploaded mask (the
    # upload endpoint returns it). Never an absolute filesystem path.
    mask_asset_path: Optional[str] = None


class SegmentationRegion(StrictModel):
    material_id: int
    name: str
    rf_material_id: str
    face_count: int


class SegmentationPreviewResponse(StrictModel):
    batch_id: str
    # Project-relative refs servable via GET /projects/{id}/assets/{path}.
    mask_ref: str
    overlay_asset_path: str
    manifest: list[SegmentationRegion]
    # Per-face material ids for the viewer's region tint (mesh face order).
    face_materials: list[int]
    total_faces: int


class SegmentationJobStart(StrictModel):
    job_id: str


class SegmentationJobStatus(StrictModel):
    status: Literal["running", "done", "error"]
    progress: int = 0
    total: int = 0
    detail: str = ""
    result: Optional[SegmentationPreviewResponse] = None


class SegmentationApplyRequest(StrictModel):
    prim_id: str
    # The preview's mask_ref (ai/segmentation/<batch>/material_mask_ids.png).
    mask_ref: str
    flip_v: bool = True


class SegmentationApplyResponse(StrictModel):
    added_prim_ids: list[str]
    removed_prim_id: str
    backup_glb: str
    batch_id: str


class SplitPartsRequest(StrictModel):
    """Split a merged multi-building mesh into its connected components.

    Parts below ``min_faces`` (and beyond the ``max_parts`` largest) pool into
    one ``rest`` sub-mesh. New prims inherit the source prim's RF binding and
    texture verbatim; undo works via the returned batch_id like any split.
    """

    prim_id: str
    min_faces: int = Field(default=200, ge=1, le=1_000_000)
    max_parts: int = Field(default=64, ge=2, le=256)


class SplitPartsResponse(StrictModel):
    added_prim_ids: list[str]
    removed_prim_id: str
    backup_glb: str
    batch_id: str
    part_face_counts: dict[str, int]


class SegmentationUndoRequest(StrictModel):
    batch_id: str


class SegmentationUndoResponse(StrictModel):
    restored_prim_id: str
    removed_prim_ids: list[str]


class MaskUploadResponse(StrictModel):
    # Project-relative path to pass back as mask_asset_path.
    mask_asset_path: str
    width: int
    height: int
