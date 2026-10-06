"""Radar sensing (RCS) solve: target resolution and the orchestration shared by
every backend (POST /projects/{id}/simulate/sensing).

An actor becomes a sensing target through ``Actor.sensing``. This module turns
each binding into a backend-neutral :class:`ResolvedSensingTarget` (cuboid,
world center, velocity, RCS model) and runs the solve: the optional comm
paths solve first, then ``backend.simulate_sensing``.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional, Sequence

import numpy as np

from seam_studio.schemas.actors import ActorState
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import SensingResultSet, SensingTargetSummary
from seam_studio.schemas.scene import Actor, Scene
from seam_studio.schemas.sensing import (
    TR38901_NOMINAL_RCS_DBSM,
    SensingSimulateRequest,
    resolve_model_type,
    resolve_object_type,
)
from seam_studio.schemas.simulation import SimulationConfig

if TYPE_CHECKING:
    from seam_studio.services.simulation_backends.base import RayTracingBackend

SENSING_TARGET_PREFIX = "seam-st-"  # sionna SensingTarget name prefix
SENSING_PATH_PREFIX = "sensing_"  # RayPath.path_id prefix
# Positions this close are one site: an RX within it of a TX is that TX's
# co-located sensing receiver, and links between the same two sites are one
# bistatic ellipsoid (ISAC roles, coverage fusion count, scenario fusion).
COLOCATED_SENSING_RX_M = 1.0


class SensingRequestError(ValueError):
    """The request names no usable sensing target (API -> 400)."""


@dataclass(frozen=True)
class ResolvedSensingTarget:
    actor_id: str
    model: str  # "tr38901" | "constant"
    object_type: Optional[str]
    model_type: Optional[int]
    rcs_dbsm: float  # constant: bound or 0.0; tr38901: nominal sigma_M
    xpr_db: Optional[float]
    random_components: bool
    size_m: tuple[float, float, float]  # (length, width, height)
    center: tuple[float, float, float]  # world; actor base + (0, 0, h/2)
    orientation_deg: tuple[float, float, float]  # [yaw, pitch, roll]
    velocity_m_s: tuple[float, float, float]


def dbsm_to_m2(rcs_dbsm: float) -> float:
    return 10.0 ** (rcs_dbsm / 10.0)


def bistatic_radar_gain_db(wavelength_m, rcs_dbsm, r_tx_m, r_rx_m):
    """10log10(lambda^2 sigma / ((4 pi)^3 R_t^2 R_r^2)): the TX -> point target
    -> RX channel gain with isotropic antennas. Legs clamp at 0.1 m like
    friis_dbm. Scalars in -> float out (math, so the mock's numbers do not
    move); any array in -> numpy array out."""
    if not any(isinstance(v, np.ndarray) for v in (wavelength_m, rcs_dbsm, r_tx_m, r_rx_m)):
        d1, d2 = max(float(r_tx_m), 0.1), max(float(r_rx_m), 0.1)
        return 10.0 * math.log10(
            wavelength_m**2 * dbsm_to_m2(rcs_dbsm) / ((4.0 * math.pi) ** 3 * d1**2 * d2**2)
        )
    d1 = np.maximum(r_tx_m, 0.1)
    d2 = np.maximum(r_rx_m, 0.1)
    return 10.0 * np.log10(
        wavelength_m**2 * 10.0 ** (np.asarray(rcs_dbsm) / 10.0)
        / ((4.0 * np.pi) ** 3 * d1**2 * d2**2)
    )


def site_index(
    points: Sequence[Sequence[float]], tol_m: float = COLOCATED_SENSING_RX_M
) -> list[int]:
    """Site of each point: points chained within ``tol_m`` (<=) form one site
    (union-find, so A-B and B-C within tol make A, B, C one site even when
    A-C is not). The value is the index of the site's first point."""
    pts = [tuple(float(c) for c in p) for p in points]
    parent = list(range(len(pts)))

    def root(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(pts)):
        for j in range(i):
            if math.dist(pts[i], pts[j]) <= tol_m:
                parent[root(i)] = root(j)
    first: dict[int, int] = {}
    return [first.setdefault(root(i), i) for i in range(len(pts))]


def geometry_groups(
    links: Sequence[tuple[Sequence[float], Sequence[float]]],
    tol_m: float = COLOCATED_SENSING_RX_M,
) -> list[int]:
    """Group index per (tx position, rx position) link: links between the same
    two sites (``site_index``), in either order, share one (one bistatic
    ellipsoid), numbered by first appearance. A TRP whose sensing panel sits
    up to ``tol_m`` from its TX is one focus, so A -> B_rx and B -> A_rx stay
    one geometry."""
    points: list[tuple[float, ...]] = []
    for t, r in links:
        for p in (t, r):
            key = tuple(float(c) for c in p)
            if key not in points:
                points.append(key)
    site = dict(zip(points, site_index(points, tol_m)))
    numbering: dict[frozenset[int], int] = {}
    groups: list[int] = []
    for t, r in links:
        key = frozenset(
            (site[tuple(float(c) for c in t)], site[tuple(float(c) for c in r)])
        )
        groups.append(numbering.setdefault(key, len(numbering)))
    return groups


def resolve_target(actor: Actor) -> ResolvedSensingTarget:
    """Backend-neutral target for an actor with a sensing binding.

    An explicit ``velocity_m_s`` keeps the authored pose. Otherwise the whole
    kinematic state comes from the trajectory at t = 0 (base position, heading
    and velocity, i.e. playback's first frame), so the scattering lobes are
    evaluated at the aspect the Doppler assumes; a static actor keeps its
    authored pose and is at rest."""
    # scenario imports the backends, which import this module.
    from seam_studio.services.scenario import (
        actor_heading_at,
        actor_position_at,
        actor_velocity_at,
    )

    spec = actor.sensing
    if spec is None:
        raise SensingRequestError(f"actor {actor.id} has no sensing binding")
    orientation = [float(v) for v in actor.orientation_deg]
    if spec.velocity_m_s is not None:
        base = [float(v) for v in actor.position]
        velocity = spec.velocity_m_s
    else:
        base = actor_position_at(actor, 0.0)
        orientation[0] = actor_heading_at(actor, 0.0)  # pitch/roll stay authored
        velocity = actor_velocity_at(actor, 0.0)
    return _target_from_pose(actor, base, orientation, velocity)


def resolve_target_state(
    actor: Actor, state: ActorState, velocity: Sequence[float]
) -> ResolvedSensingTarget:
    """The target at one scenario frame: the frame's base pose (travel yaw
    included) and trajectory velocity. An explicit sensing.velocity_m_s still
    overrides the velocity; the pose always follows the frame."""
    spec = actor.sensing
    if spec is None:
        raise SensingRequestError(f"actor {actor.id} has no sensing binding")
    if spec.velocity_m_s is not None:
        velocity = spec.velocity_m_s
    return _target_from_pose(actor, state.position, state.orientation_deg, velocity)


def _target_from_pose(
    actor: Actor,
    base: Sequence[float],
    orientation: Sequence[float],
    velocity: Sequence[float],
) -> ResolvedSensingTarget:
    """Pose-independent part of a target: cuboid, center = base + (0, 0, h/2),
    RCS model fields."""
    spec = actor.sensing
    if spec is None:
        raise SensingRequestError(f"actor {actor.id} has no sensing binding")
    size = tuple(float(v) for v in (spec.size_m or actor.shape.size_m))
    base = [float(v) for v in base]
    # Same base-center convention as rf_compiler._actor_box_mesh.
    center = (base[0], base[1], base[2] + size[2] / 2.0)
    if spec.model == "tr38901":
        object_type = resolve_object_type(actor.kind, spec)
        if object_type is None:
            raise SensingRequestError(
                f"actor {actor.id} needs sensing.object_type for model 'tr38901'"
            )
        model_type: Optional[int] = resolve_model_type(object_type, spec)
        rcs_dbsm = TR38901_NOMINAL_RCS_DBSM[object_type]
        xpr_db = None
    else:
        object_type = None
        model_type = None
        rcs_dbsm = float(spec.rcs_dbsm) if spec.rcs_dbsm is not None else 0.0
        xpr_db = spec.xpr_db
    return ResolvedSensingTarget(
        actor_id=actor.id,
        model=spec.model,
        object_type=object_type,
        model_type=model_type,
        rcs_dbsm=rcs_dbsm,
        xpr_db=xpr_db,
        random_components=bool(spec.random_components),
        size_m=size,  # type: ignore[arg-type]
        center=center,
        orientation_deg=tuple(float(v) for v in orientation),  # type: ignore[arg-type]
        velocity_m_s=tuple(float(v) for v in velocity),  # type: ignore[arg-type]
    )


def target_from_summary(s: SensingTargetSummary) -> ResolvedSensingTarget:
    """The solved cuboid, pose and RCS model of a stored result's target, e.g.
    to stand it in for its actor in a later paths solve (channel npz)."""
    return ResolvedSensingTarget(
        actor_id=s.actor_id,
        model=s.model,
        object_type=s.object_type,
        model_type=s.model_type,
        rcs_dbsm=s.rcs_dbsm if s.rcs_dbsm is not None else 0.0,
        xpr_db=None,
        random_components=False,
        size_m=tuple(float(v) for v in s.size_m),  # type: ignore[arg-type]
        center=tuple(float(v) for v in s.position),  # type: ignore[arg-type]
        orientation_deg=tuple(float(v) for v in s.orientation_deg),  # type: ignore[arg-type]
        velocity_m_s=tuple(float(v) for v in s.velocity_m_s),  # type: ignore[arg-type]
    )


def actor_velocities_t0(scene: Scene) -> dict[str, list[float]]:
    """Trajectory velocity at t = 0 of every moving actor (static ones omitted).

    A sensing solve, and a paths solve without per-frame actor states, is a
    t = 0 snapshot: the actor meshes in the scene move like this (and the
    devices riding them, ``with_rider_velocities``), so legs reflecting off
    them carry the Doppler of playback's first frame rather than whatever the
    cached scene last held."""
    from seam_studio.services.scenario import actor_velocity_at

    out: dict[str, list[float]] = {}
    for actor in scene.actors:
        v = actor_velocity_at(actor, 0.0)
        if any(abs(c) >= 1e-9 for c in v):
            out[actor.id] = [float(c) for c in v]
    return out


def with_rider_velocities(scene: Scene, actor_velocities: dict[str, list[float]]) -> Scene:
    """``scene`` with every device attached to a moving actor given that
    actor's velocity, as in a scenario frame, unless the device sets its own
    ``velocity_m_s``. Returns ``scene`` itself when no device needs one."""
    rider_v = {
        dev_id: v
        for actor in scene.actors
        if (v := actor_velocities.get(actor.id)) is not None
        for dev_id in actor.attached_device_ids
    }
    if not any(d.id in rider_v and d.velocity_m_s is None for d in scene.devices):
        return scene
    devices = [
        d.model_copy(update={"velocity_m_s": [float(c) for c in rider_v[d.id]]})
        if d.id in rider_v and d.velocity_m_s is None
        else d
        for d in scene.devices
    ]
    return scene.model_copy(update={"devices": devices})


def select_targets(
    scene: Scene, target_actor_ids: Optional[list[str]]
) -> list[ResolvedSensingTarget]:
    """Resolve the request's targets; SensingRequestError when none qualify."""
    if target_actor_ids is None:
        bound = [a for a in scene.actors if a.sensing is not None and a.sensing.enabled]
        if not bound:
            raise SensingRequestError(
                "no sensing targets: bind an actor first "
                "(actor.sensing, Inspector → Sensing target)"
            )
        return [resolve_target(a) for a in bound]
    by_id = {a.id: a for a in scene.actors}
    out: list[ResolvedSensingTarget] = []
    seen: set[str] = set()
    for aid in target_actor_ids:
        if aid in seen:
            continue
        seen.add(aid)
        actor = by_id.get(aid)
        if actor is None:
            raise SensingRequestError(f"actor not found: {aid}")
        if actor.sensing is None or not actor.sensing.enabled:
            raise SensingRequestError(f"actor {aid} has no enabled sensing binding")
        out.append(resolve_target(actor))
    return out


def target_summary(
    t: ResolvedSensingTarget, scattering_points: list[list[float]], path_count: int
) -> SensingTargetSummary:
    sps = [[float(c) for c in sp] for sp in scattering_points]
    return SensingTargetSummary(
        actor_id=t.actor_id,
        model=t.model,  # type: ignore[arg-type]
        object_type=t.object_type,
        model_type=t.model_type,
        num_scattering_points=len(sps),
        scattering_points=sps,
        position=list(t.center),
        orientation_deg=list(t.orientation_deg),
        velocity_m_s=list(t.velocity_m_s),
        size_m=list(t.size_m),
        rcs_dbsm=t.rcs_dbsm,
        path_count=path_count,
    )


def run_sensing(
    backend: "RayTracingBackend",
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: SensingSimulateRequest,
    targets: list[ResolvedSensingTarget],
    tick: Optional[Callable[[int, int], None]] = None,
    extra_warnings: tuple[str, ...] | list[str] = (),
) -> SensingResultSet:
    """Sensing solve (+ optional comm paths) with progress ticks; never persists."""
    total = 2 if request.include_comm_paths else 1
    if tick is not None:
        tick(0, total)
    comm_paths = []
    comm_warnings: list[str] = []
    if request.include_comm_paths:
        # A separate solve (Paths.concat needs identical scene configuration)
        # with the targets standing in for their actors as absorbers, the way
        # Sionna's PathSolver sees sensing targets: the RCS model alone
        # describes each target, so no comm path may also reflect off its mesh.
        comm = backend.simulate_paths_with_targets(
            project_dir, scene, library, config, targets
        )
        dop = comm.metadata.get("doppler_hz")
        aligned = isinstance(dop, list) and len(dop) == len(comm.paths)
        comm_paths = [
            p.model_copy(update={"doppler_hz": float(dop[i])}) if aligned else p
            for i, p in enumerate(comm.paths)
        ]
        comm_warnings = [f"comm paths: {w}" for w in comm.warnings]
        if tick is not None:
            tick(1, total)
    result = backend.simulate_sensing(project_dir, scene, library, config, request, targets)
    result.paths = list(result.paths) + comm_paths
    result.warnings = list(extra_warnings) + list(result.warnings) + comm_warnings
    result.metadata["sensing_path_count"] = sum(
        1 for p in result.paths if p.target_id is not None
    )
    if request.include_comm_paths:
        result.metadata["comm_path_count"] = len(comm_paths)
    result.metadata["sensing_request"] = request.model_dump(mode="json", exclude={"config"})
    if tick is not None:
        tick(total, total)
    return result
