"""RF material library models and assignment requests.

RF materials are electromagnetic descriptions (permittivity, conductivity,
scattering) consumed by the RF projection / Sionna RT. They are deliberately
disjoint from visual/PBR materials, which live in the GLB and in
``Prim.visual``. The only shared field is ``preview_color``, used by the
frontend RF overlay mode.
"""

from typing import Literal, Optional

from pydantic import Field

from .common import ActiveAssignmentStatus, StrictModel, UnitFloat


class RFMaterial(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_]+$")
    display_name: str
    category: str = "generic"
    # itu_frequency_dependent: parameters derived from the ITU-R P.2040 model
    # at simulation time (Sionna's built-in itu_* materials). constant: use
    # relative_permittivity / conductivity_s_per_m as given.
    model: Literal["itu_frequency_dependent", "constant"] = "itu_frequency_dependent"
    # Name of the corresponding Sionna RT built-in material (e.g. "itu_concrete").
    # None for fully custom materials.
    itu_name: Optional[str] = None
    relative_permittivity: Optional[float] = Field(default=None, ge=1.0)
    conductivity_s_per_m: Optional[float] = Field(default=None, ge=0.0)
    thickness_m: Optional[float] = Field(default=None, gt=0.0)
    scattering_coefficient: UnitFloat = 0.0
    xpd_coefficient: UnitFloat = 0.0
    # Whether radio waves can pass through (glass, brick, ...). Transmissive
    # materials without a thickness raise a validation warning.
    transmissive: bool = True
    preview_color: str = Field(default="#888888", pattern=r"^#[0-9a-fA-F]{6}$")
    notes: str = ""
    # True for materials shipped with the app; custom/edited materials are False.
    builtin: bool = False


class RFMaterialLibrary(StrictModel):
    materials: list[RFMaterial] = Field(default_factory=list)

    def get(self, material_id: str) -> Optional[RFMaterial]:
        for mat in self.materials:
            if mat.id == material_id:
                return mat
        return None

    def ids(self) -> set[str]:
        return {mat.id for mat in self.materials}


class RFOverrides(StrictModel):
    """Per-prim overrides applied on top of the material's defaults."""

    thickness_m: Optional[float] = Field(default=None, gt=0.0)
    scattering_coefficient: Optional[UnitFloat] = None
    xpd_coefficient: Optional[UnitFloat] = None


class AssignRequest(StrictModel):
    prim_ids: list[str] = Field(min_length=1)
    rf_material_id: str
    assignment_status: ActiveAssignmentStatus = "user_confirmed"
    sources: list[str] = Field(default_factory=lambda: ["user"])
    confidence: Optional[UnitFloat] = None
    overrides: Optional[RFOverrides] = None


class BatchAssignRequest(StrictModel):
    assignments: list[AssignRequest] = Field(min_length=1)


class AssignResponse(StrictModel):
    updated_prim_ids: list[str]
    # Prim ids that were requested but not found / not assignable.
    skipped_prim_ids: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
