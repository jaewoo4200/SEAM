"""Canonical unified RF-visual scene graph.

This is the single source of truth (HANDOFF.md section 1). The visual
projection (GLB rendered by the frontend) and the RF projection (meshes + XML
compiled for Sionna RT) are both derived from this model and always map back
to canonical prim ids.

Key invariants enforced here:
- prim ids are unique, absolute, path-like ("/buildings/b07/wall_03");
- every prim carries an ``rf`` binding (possibly ``unassigned``) so RF state
  is never implicit;
- visual bindings and RF bindings are separate objects that only meet at the
  prim level.
"""

from typing import Literal, Optional

from pydantic import Field, field_validator, model_validator

from .common import (
    SCHEMA_VERSION,
    AssignmentStatus,
    RGBA,
    StrictModel,
    Transform,
    UnitFloat,
    Vec3,
)
from .devices import Device
from .simulation import SimulationConfig


class CoordinateSystem(StrictModel):
    type: Literal["local_enu"] = "local_enu"
    # [lat_deg, lon_deg, alt_m] geodetic anchor of the local ENU origin, when
    # the scene is georeferenced (future CesiumJS / 3D Tiles path).
    origin_lat_lon_alt: Optional[Vec3] = None
    units: Literal["meters"] = "meters"


class SceneAssets(StrictModel):
    # URI of the visual projection source, relative to the project folder.
    visual_scene_uri: Optional[str] = "visual/scene.glb"
    # Optional future 3D Tiles tileset for city-scale scenes.
    tileset_uri: Optional[str] = None


class MeshRef(StrictModel):
    """Reference from a prim into the visual asset's geometry.

    MVP supports Mode 1 (whole named mesh, ``face_group=None``) and Mode 2
    (named face group split). Mode 3 (separate simplified RF proxy mesh) is a
    future optimization and would add an ``rf_proxy_uri`` here.
    """

    asset_uri: str = "visual/scene.glb"
    mesh_name: str
    primitive_index: int = Field(default=0, ge=0)
    face_group: Optional[str] = None


class VisualBinding(StrictModel):
    """Visual/PBR side of the dual binding. Never used for RF simulation."""

    material_id: Optional[str] = None
    # Material name as authored in the GLB (useful evidence for suggestions).
    material_name: Optional[str] = None
    base_color_texture: Optional[str] = None
    base_color_rgba: Optional[RGBA] = None


class RFBinding(StrictModel):
    """RF side of the dual binding, with mandatory provenance tracking."""

    material_id: Optional[str] = None
    # Per-prim overrides of the material's defaults.
    thickness_m: Optional[float] = Field(default=None, gt=0.0)
    scattering_coefficient: Optional[UnitFloat] = None
    xpd_coefficient: Optional[UnitFloat] = None
    assignment_status: AssignmentStatus = "unassigned"
    # Where this assignment came from, in order: e.g. ["rule_based"],
    # ["ai:ollama/qwen3:8b", "user"], ["visual_material_name", "user"].
    assignment_sources: list[str] = Field(default_factory=list)
    confidence: Optional[UnitFloat] = None

    @model_validator(mode="after")
    def _status_consistent(self) -> "RFBinding":
        if self.material_id is None and self.assignment_status != "unassigned":
            raise ValueError(
                "rf.assignment_status must be 'unassigned' when rf.material_id is None"
            )
        if self.material_id is not None and self.assignment_status == "unassigned":
            raise ValueError(
                "rf.material_id is set but assignment_status is 'unassigned'; "
                "use rule_suggested/ai_suggested/user_confirmed/measurement_calibrated"
            )
        return self


class Prim(StrictModel):
    id: str
    name: str
    type: Literal["mesh_primitive", "group"] = "mesh_primitive"
    parent_id: Optional[str] = None
    semantic_tags: list[str] = Field(default_factory=list)
    # Required for mesh_primitive prims; groups have no geometry of their own.
    mesh_ref: Optional[MeshRef] = None
    transform: Transform = Field(default_factory=Transform)
    visual: Optional[VisualBinding] = None
    rf: RFBinding = Field(default_factory=RFBinding)

    @field_validator("id", "parent_id")
    @classmethod
    def _path_like(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not v.startswith("/") or v.endswith("/") or "//" in v:
            raise ValueError(
                f"prim id {v!r} must be an absolute path-like id such as "
                "'/buildings/b07/wall_03'"
            )
        return v


class ResultSetRef(StrictModel):
    """Pointer from the scene to a stored result artifact."""

    result_id: str
    kind: Literal["paths", "radio_map", "trajectory"]
    backend: str
    simulation_config_id: str
    # Relative to the project folder, e.g. "results/paths.json".
    uri: str
    created_at: Optional[str] = None


class Scene(StrictModel):
    schema_version: str = SCHEMA_VERSION
    scene_id: str
    name: str = ""
    coordinate_system: CoordinateSystem = Field(default_factory=CoordinateSystem)
    assets: SceneAssets = Field(default_factory=SceneAssets)
    prims: list[Prim] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=list)
    simulation_configs: list[SimulationConfig] = Field(default_factory=list)
    result_sets: list[ResultSetRef] = Field(default_factory=list)

    @model_validator(mode="after")
    def _unique_ids(self) -> "Scene":
        seen: set[str] = set()
        for prim in self.prims:
            if prim.id in seen:
                raise ValueError(f"duplicate prim id: {prim.id!r}")
            seen.add(prim.id)
        dev_seen: set[str] = set()
        for dev in self.devices:
            if dev.id in dev_seen:
                raise ValueError(f"duplicate device id: {dev.id!r}")
            dev_seen.add(dev.id)
        return self

    def prim_by_id(self, prim_id: str) -> Optional[Prim]:
        for prim in self.prims:
            if prim.id == prim_id:
                return prim
        return None

    def device_by_id(self, device_id: str) -> Optional[Device]:
        for dev in self.devices:
            if dev.id == device_id:
                return dev
        return None
