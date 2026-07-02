"""Scene validation report models.

Issue codes (stable identifiers used by tests and the frontend):
- DUPLICATE_PRIM_ID        two prims share an id (also rejected at parse time)
- MISSING_RF_MATERIAL      mesh prim has no RF material assigned
- UNKNOWN_RF_MATERIAL      rf.material_id not present in the project library
- VISUAL_RF_MISMATCH       visual evidence contradicts RF material (glass vs concrete)
- MISSING_THICKNESS        transmissive RF material with no thickness anywhere
- MISSING_MESH_REF         mesh_primitive prim without a mesh_ref
- UNSUPPORTED_MESH_REF     mesh_ref points at a missing asset/mesh name
- UNKNOWN_PARENT           parent_id references a prim that does not exist
- UNCONFIRMED_SUGGESTION   rf material is only rule/ai suggested, not confirmed
- NO_DEVICES               scene has no tx or no rx (simulation would be empty)
- UNKNOWN_MATERIAL_CATEGORY material category is not recognized
"""

from typing import Literal, Optional

from pydantic import Field

from .common import StrictModel

Severity = Literal["error", "warning", "info"]


class ValidationIssue(StrictModel):
    severity: Severity
    code: str
    message: str
    prim_id: Optional[str] = None
    device_id: Optional[str] = None


class ValidationReport(StrictModel):
    # ok means "no error-severity issues"; warnings do not block compilation.
    ok: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    error_count: int = 0
    warning_count: int = 0
    info_count: int = 0

    @classmethod
    def from_issues(cls, issues: list[ValidationIssue]) -> "ValidationReport":
        errors = sum(1 for i in issues if i.severity == "error")
        warnings = sum(1 for i in issues if i.severity == "warning")
        infos = sum(1 for i in issues if i.severity == "info")
        return cls(
            ok=errors == 0,
            issues=issues,
            error_count=errors,
            warning_count=warnings,
            info_count=infos,
        )
