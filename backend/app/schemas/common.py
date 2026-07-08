"""Shared primitive types used across all SEAM schemas."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

SCHEMA_VERSION = "0.1.0"

Vec3 = Annotated[list[float], Field(min_length=3, max_length=3)]
Vec4 = Annotated[list[float], Field(min_length=4, max_length=4)]
RGBA = Annotated[list[float], Field(min_length=4, max_length=4)]
UnitFloat = Annotated[float, Field(ge=0.0, le=1.0)]

# Provenance states for an RF material binding. Ordering reflects increasing
# trust: unassigned < rule_suggested/ai_suggested < user_confirmed <
# measurement_calibrated. A visual/PBR material is never, by itself, an RF
# material - suggestions derived from visual evidence stay in *_suggested
# until a user confirms or a calibration run promotes them.
# "rule_assigned" is a deterministic rule-based assignment (no human/AI in the
# loop) that carries a material; "rejected" records that a suggestion was
# declined and the prim intentionally has NO material (material_id stays None).
AssignmentStatus = Literal[
    "unassigned",
    "rule_suggested",
    "rule_assigned",
    "ai_suggested",
    "user_confirmed",
    "measurement_calibrated",
    "rejected",
]

# Statuses a material assignment may carry - "unassigned" is only ever the
# absence of a binding, and "rejected" is a deliberate no-material decision;
# neither is something a request can set alongside a material.
ActiveAssignmentStatus = Literal[
    "rule_suggested",
    "rule_assigned",
    "ai_suggested",
    "user_confirmed",
    "measurement_calibrated",
]

ASSIGNMENT_STATUSES: tuple[str, ...] = (
    "unassigned",
    "rule_suggested",
    "rule_assigned",
    "ai_suggested",
    "user_confirmed",
    "measurement_calibrated",
    "rejected",
)

# Statuses that legitimately carry NO material_id: "unassigned" is the absence
# of a binding, "rejected" is an explicit no-material decision. Every other
# status requires a material_id.
NO_MATERIAL_STATUSES: tuple[str, ...] = ("unassigned", "rejected")


class StrictModel(BaseModel):
    """Base for all schema models: reject unknown keys so schema drift is loud."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Transform(StrictModel):
    translation: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation_quat_xyzw: Vec4 = Field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])
    scale: Vec3 = Field(default_factory=lambda: [1.0, 1.0, 1.0])
