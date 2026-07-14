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
    NO_MATERIAL_STATUSES,
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
    # Optional textured backdrop GLB rendered alongside (non-pickable): lets a
    # photogrammetry/textured map coexist with the pickable RF-derived meshes.
    visual_overlay_uri: Optional[str] = None
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
        # "unassigned" and "rejected" are the only statuses that legitimately
        # carry no material: unassigned is the absence of a binding, rejected
        # is a deliberate decline that keeps material_id None on purpose.
        if self.material_id is None and self.assignment_status not in NO_MATERIAL_STATUSES:
            raise ValueError(
                "rf.assignment_status must be 'unassigned' or 'rejected' when "
                "rf.material_id is None"
            )
        if self.material_id is not None and self.assignment_status in NO_MATERIAL_STATUSES:
            raise ValueError(
                f"rf.material_id is set but assignment_status is "
                f"{self.assignment_status!r}; use rule_suggested/rule_assigned/"
                "ai_suggested/user_confirmed/measurement_calibrated"
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


ActorKind = Literal["car", "human", "custom", "uav"]

# Default RF material, box size (l, w, h meters) and color per actor kind.
# UAV: a small quadrotor-class body (predominantly metal for RF purposes).
# Its position.z is the airframe's flight altitude — actors are never
# ground-clamped, so hovering (no trajectory) and waypoint flight (trajectory
# with per-waypoint z) both work like any other actor.
ACTOR_DEFAULTS: dict[str, dict] = {
    "car": {"rf_material_id": "metal", "size_m": [4.5, 1.8, 1.5], "color": "#ffd166"},
    "human": {"rf_material_id": "human_body", "size_m": [0.5, 0.35, 1.7], "color": "#06d6a0"},
    "custom": {"rf_material_id": "unknown_rf", "size_m": [1.0, 1.0, 1.0], "color": "#a78bfa"},
    "uav": {"rf_material_id": "metal", "size_m": [0.6, 0.6, 0.25], "color": "#38bdf8"},
}


class ActorShape(StrictModel):
    type: Literal["box", "mesh"] = "box"
    # Box extents (length x width x height); the actor position is the base
    # center (z = ground contact), matching vehicle/pedestrian placement.
    size_m: Vec3 = Field(default_factory=lambda: [1.0, 1.0, 1.0])
    # For type "mesh": named mesh in the project's visual asset.
    mesh_ref: Optional[MeshRef] = None


class ActorTrajectory(StrictModel):
    waypoints: list[Vec3] = Field(default_factory=list)
    dt_s: float = Field(default=0.1, gt=0.0)
    # Deprecated boolean kept for older scenes; superseded by mode.
    loop: bool = False
    # once: clamp at the last waypoint; loop: wrap to the start; pingpong:
    # reverse direction at each end. None derives from the legacy loop flag.
    mode: Optional[Literal["once", "loop", "pingpong"]] = None

    def resolved_mode(self) -> str:
        if self.mode is not None:
            return self.mode
        return "loop" if self.loop else "once"


class Actor(StrictModel):
    """Movable RF-relevant object (car, human, UAV, custom scatterer).

    Compiled as its OWN Sionna shape (never merged into material groups) so
    the backend can move it per frame and re-solve; may carry devices (V2X)."""

    id: str = Field(pattern=r"^[a-z0-9_\-]+$")
    name: str = ""
    kind: ActorKind = "custom"
    shape: ActorShape = Field(default_factory=ActorShape)
    rf_material_id: Optional[str] = None
    # Base position (z = ground contact plane of the shape).
    position: Vec3
    # [yaw, pitch, roll] degrees - same convention as Device.orientation_deg.
    orientation_deg: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])
    trajectory: Optional[ActorTrajectory] = None
    # Devices that move with this actor (offsets preserved from scene pose).
    attached_device_ids: list[str] = Field(default_factory=list)
    color: Optional[str] = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")

    @model_validator(mode="after")
    def _kind_defaults(self) -> "Actor":
        defaults = ACTOR_DEFAULTS[self.kind]
        if self.rf_material_id is None:
            object.__setattr__(self, "rf_material_id", defaults["rf_material_id"])
        if self.color is None:
            object.__setattr__(self, "color", defaults["color"])
        # The generic 1x1x1 box default means "unspecified": adopt the kind's
        # physical size (car 4.5x1.8x1.5, human 0.5x0.35x1.7).
        if self.shape.type == "box" and self.shape.size_m == [1.0, 1.0, 1.0]:
            object.__setattr__(
                self.shape, "size_m", [float(v) for v in defaults["size_m"]]
            )
        return self


class SceneBounds(StrictModel):
    """World-space AABB of the visual scene (Z-up meters).

    Served by GET /projects/{id}/scene/bounds so the UI can seed sampling
    regions, trajectory endpoints, and camera framing from real geometry
    instead of guessed constants.
    """

    min: Vec3
    max: Vec3

    @model_validator(mode="after")
    def _ordered(self) -> "SceneBounds":
        if any(self.min[i] > self.max[i] for i in range(3)):
            raise ValueError("bounds min must be <= max on every axis")
        return self

    def center(self) -> list[float]:
        return [(self.min[i] + self.max[i]) / 2.0 for i in range(3)]

    def size(self) -> list[float]:
        return [self.max[i] - self.min[i] for i in range(3)]


class ResultSetRef(StrictModel):
    """Pointer from the scene to a stored result artifact."""

    result_id: str
    kind: Literal["paths", "radio_map", "mesh_radio_map", "trajectory", "scenario", "channel"]
    backend: str
    simulation_config_id: str
    # Relative to the project folder, e.g. "results/paths.json".
    uri: str
    created_at: Optional[str] = None
    # User-facing run name ("before-glass-facade"). Labeled runs are spared by
    # results pruning so named baselines survive cleanup.
    label: Optional[str] = None
    # Stamped at persist time so the UI can show what each run costs on disk.
    size_bytes: Optional[int] = None


class Scene(StrictModel):
    schema_version: str = SCHEMA_VERSION
    scene_id: str
    name: str = ""
    # Drives UI/solver presets (marker scale, camera, cell size, max depth):
    # "auto" infers from geometry extent.
    environment: Literal["auto", "indoor", "outdoor"] = "auto"
    coordinate_system: CoordinateSystem = Field(default_factory=CoordinateSystem)
    assets: SceneAssets = Field(default_factory=SceneAssets)
    prims: list[Prim] = Field(default_factory=list)
    devices: list[Device] = Field(default_factory=list)
    # Movable RF-relevant objects (cars, humans, ...).
    actors: list[Actor] = Field(default_factory=list)
    simulation_configs: list[SimulationConfig] = Field(default_factory=list)
    result_sets: list[ResultSetRef] = Field(default_factory=list)
    # Monotonic save counter for optimistic concurrency: PUT /scene carries the
    # revision it was based on and gets 409 on mismatch. None means "no check"
    # (fresh in-memory scenes and pre-revision files), keeping old clients and
    # tests working.
    revision: Optional[int] = None

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
        actor_seen: set[str] = set()
        for actor in self.actors:
            if actor.id in actor_seen:
                raise ValueError(f"duplicate actor id: {actor.id!r}")
            actor_seen.add(actor.id)
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
