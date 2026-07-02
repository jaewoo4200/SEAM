"""RF projection compile results."""

from typing import Optional

from pydantic import Field

from .common import StrictModel
from .validation import ValidationReport


class MaterialGroup(StrictModel):
    """Geometry grouped by RF material for export (HANDOFF.md Mode 2)."""

    rf_material_id: str
    prim_ids: list[str] = Field(default_factory=list)
    # Path relative to project folder, e.g. "rf/meshes/itu_concrete.ply".
    # None when mesh extraction was not possible (placeholder compile).
    mesh_file: Optional[str] = None
    face_count: Optional[int] = None


class CompileResult(StrictModel):
    ok: bool
    backend_format: str = "mitsuba_xml"
    # All paths relative to the project folder.
    scene_xml: Optional[str] = None
    manifest: Optional[str] = None
    mesh_dir: Optional[str] = None
    material_groups: list[MaterialGroup] = Field(default_factory=list)
    generated_files: list[str] = Field(default_factory=list)
    # Prims skipped because they had no RF material or no extractable mesh.
    skipped_prim_ids: list[str] = Field(default_factory=list)
    validation: Optional[ValidationReport] = None
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)
