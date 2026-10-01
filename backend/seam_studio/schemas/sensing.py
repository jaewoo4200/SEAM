"""Sensing-target binding (RCS) and the sensing solve request."""

from typing import Literal, Optional

from pydantic import Field, model_validator

from .common import StrictModel, Vec3
from .simulation import SimulationConfig

SensingModel = Literal["tr38901", "constant"]
TR38901ObjectType = Literal[
    "uav-small-size", "uav-large-size", "human",
    "vehicle-single-sp", "vehicle-multi-sp", "agv-single-sp", "agv-multi-sp",
]

# Mirrors sionna-rt 2.2 sionna/rt/rcs/tr38901/parameters.py (OBJECT_TYPES /
# TARGET_PARAMETERS): the model types TR 38.901 clause 7.9.2.1 defines per
# object type, and 10lg(sigma_M), the mean monostatic RCS [dBsm].
TR38901_OBJECT_TYPES: tuple[str, ...] = (
    "uav-small-size", "uav-large-size", "human",
    "vehicle-single-sp", "vehicle-multi-sp", "agv-single-sp", "agv-multi-sp",
)
TR38901_MODEL_TYPES: dict[str, tuple[int, ...]] = {
    "uav-small-size": (1,), "uav-large-size": (2,), "human": (1, 2),
    "vehicle-single-sp": (2,), "vehicle-multi-sp": (2,),
    "agv-single-sp": (2,), "agv-multi-sp": (2,),
}
TR38901_NOMINAL_RCS_DBSM: dict[str, float] = {
    "uav-small-size": -12.81, "uav-large-size": -5.85, "human": -1.37,
    "vehicle-single-sp": 11.25, "vehicle-multi-sp": 11.25,
    "agv-single-sp": -4.25, "agv-multi-sp": -4.25,
}
# Actor kind -> TR 38.901 object type used when sensing.object_type is None.
DEFAULT_OBJECT_TYPE_BY_KIND: dict[str, Optional[str]] = {
    "car": "vehicle-multi-sp", "human": "human", "uav": "uav-small-size", "custom": None,
}


def model_type_error(object_type: str, model_type: int) -> str:
    return (
        f"TR 38.901 does not define RCS model {model_type} for '{object_type}'; "
        f"available: {list(TR38901_MODEL_TYPES[object_type])}"
    )


class SensingTargetSpec(StrictModel):
    """Binds an actor as a radar sensing target (sionna.rt.rcs)."""

    model: SensingModel = "tr38901"
    # tr38901 only. None = derived from the actor kind (custom: required).
    object_type: Optional[TR38901ObjectType] = None
    # tr38901 only. None = the highest model type the object type defines.
    model_type: Optional[Literal[1, 2]] = None
    # constant only. None = 0 dBsm (1 m^2).
    rcs_dbsm: Optional[float] = Field(default=None, ge=-80.0, le=80.0)
    # constant only. None = the scattering point does not depolarize.
    xpr_db: Optional[float] = None
    # tr38901 only: draw sigma_S, XPR and initial phases per direction pair.
    random_components: bool = False
    # Target cuboid (length, width, height) [m]; None = actor.shape.size_m.
    size_m: Optional[Vec3] = None
    # World-frame m/s; None = the actor trajectory tangent at t=0 (static: 0).
    velocity_m_s: Optional[Vec3] = None
    enabled: bool = True

    @model_validator(mode="after")
    def _fields_match_model(self) -> "SensingTargetSpec":
        if self.size_m is not None and any(v <= 0 for v in self.size_m):
            raise ValueError("sensing.size_m entries must be > 0")
        if self.model == "constant":
            offending = [
                name
                for name, set_ in (
                    ("object_type", self.object_type is not None),
                    ("model_type", self.model_type is not None),
                    ("random_components", self.random_components),
                )
                if set_
            ]
            if offending:
                raise ValueError(
                    f"sensing.{', sensing.'.join(offending)} applies to model "
                    "'tr38901' only"
                )
        else:
            offending = [
                name
                for name, value in (("rcs_dbsm", self.rcs_dbsm), ("xpr_db", self.xpr_db))
                if value is not None
            ]
            if offending:
                raise ValueError(
                    f"sensing.{', sensing.'.join(offending)} applies to model "
                    "'constant' only; TR 38.901 derives RCS/XPR from the spec tables"
                )
            if (
                self.object_type is not None
                and self.model_type is not None
                and self.model_type not in TR38901_MODEL_TYPES[self.object_type]
            ):
                raise ValueError(model_type_error(self.object_type, self.model_type))
        return self


class SensingSimulateRequest(StrictModel):
    """Body for POST /projects/{id}/simulate/sensing."""

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    # Override config.tx_ids / config.rx_ids (None = keep the config's).
    tx_ids: Optional[list[str]] = None
    rx_ids: Optional[list[str]] = None
    # None = every actor whose sensing binding is enabled.
    target_actor_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # Also run the normal paths solve and append its RayPaths to the result.
    include_comm_paths: bool = False
    samples_per_sp: int = Field(default=1_000_000, ge=1, le=100_000_000)
    # RCSSolver depth (counts the scattering event). None = max(1, config.max_depth).
    max_depth: Optional[int] = Field(default=None, ge=1, le=12)


def resolve_object_type(kind: str, spec: SensingTargetSpec) -> Optional[str]:
    """TR 38.901 object type of a binding (None for constant / unresolved)."""
    if spec.model != "tr38901":
        return None
    return spec.object_type or DEFAULT_OBJECT_TYPE_BY_KIND.get(kind)


def resolve_model_type(object_type: str, spec: SensingTargetSpec) -> int:
    if spec.model_type is not None:
        return int(spec.model_type)
    return max(TR38901_MODEL_TYPES[object_type])
