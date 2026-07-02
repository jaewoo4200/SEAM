"""Shared primitive types used across all SionnaTwin schemas."""

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
AssignmentStatus = Literal[
    "unassigned",
    "rule_suggested",
    "ai_suggested",
    "user_confirmed",
    "measurement_calibrated",
]

ASSIGNMENT_STATUSES: tuple[str, ...] = (
    "unassigned",
    "rule_suggested",
    "ai_suggested",
    "user_confirmed",
    "measurement_calibrated",
)


class StrictModel(BaseModel):
    """Base for all schema models: reject unknown keys so schema drift is loud."""

    model_config = ConfigDict(extra="forbid", validate_assignment=True)


class Transform(StrictModel):
    translation: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    rotation_quat_xyzw: Vec4 = Field(default_factory=lambda: [0.0, 0.0, 0.0, 1.0])
    scale: Vec3 = Field(default_factory=lambda: [1.0, 1.0, 1.0])
