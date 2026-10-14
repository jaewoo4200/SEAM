"""H0 calibration ensemble (#30): background-only frames for CFAR threshold
calibration and digital-twin mismatch studies
(POST /projects/{id}/simulate/sensing-background-ensemble).

One request re-solves the H0 background of one stored scenario frame (the
target actors removed, ``sensing_background.solve_background``) at that
frame's node poses, once per realization. Realization 0 is the nominal twin;
every later one draws a perturbation from the request seed: radio-material
eps_r / sigma scales, one position offset per node site, and a CFO per
bistatic sensing RX. A realization with no material or position draw has the
nominal solve inputs and reuses realization 0's paths (no solve).

An H0 frame is (realization r, noise frame m): r's paths synthesized on the
consumer's pilot grid (CFO as a Doppler offset) plus the thermal noise of
``channel_synthesis.noise_frame(seed, m, tx, rx, realization=r,
stream=H0_NOISE_STREAM)``, a stream no scenario run draws its in-run noise
from. Noise is never stored: the seed redraws it bit for bit (``h0_frame``).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

import numpy as np
from pydantic import ValidationError

from seam_studio.schemas.actors import ActorState, ScenarioResultSet
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import (
    BackgroundRealization,
    EnsembleLink,
    EnsembleMaterialDraw,
    RayPath,
    SensingBackgroundEnsembleResultSet,
    SensingNodeState,
)
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.sensing import (
    BackgroundEnsembleRequest,
    BackgroundOptions,
    EnsemblePerturbation,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.channel_synthesis import (
    H0_NOISE_STREAM,
    noise_frame,
    noise_key,
    synthesize_cfr,
)
from seam_studio.services.scenario import _frame_scene, _velocities_at
from seam_studio.services.sensing import (
    COLOCATED_SENSING_RX_M,
    ResolvedSensingTarget,
    SensingRequestError,
    resolve_target_state,
    site_index,
)
from seam_studio.services.sensing_background import (
    BackgroundSolveError,
    link_power_db,
    solve_background,
)

if TYPE_CHECKING:
    from seam_studio.services.numerology import Numerology
    from seam_studio.services.simulation_backends.base import RayTracingBackend

ENSEMBLE_STREAM = 0x5EA030
# Paths stored over every solved realization (about 1 KB of JSON each).
MAX_ENSEMBLE_STORED_PATHS = 2_000_000

DRAW_ORDER = (
    "per radio material (sorted) normal(0, eps_r_pct/100) then normal(0, sigma_pct/100); "
    "per node site (site order) normal(0, node_pos_sigma_m, 3); "
    "per bistatic sensing RX (sorted ids) normal(0, cfo_hz)"
)
N0_NOTE = (
    "the ensemble stores no noise level: the consumer sets it (sigma_h2 = N0 per RE / Es per "
    "RE from its waveform numerology, noise figure and TX power)"
)


class EnsembleError(ValueError):
    """The ensemble request cannot run on this source (API -> 400)."""


@dataclass
class EnsembleSetup:
    """Everything a run needs from the source frame, checked before any solve."""

    config: SimulationConfig
    options: BackgroundOptions
    frame_index: int
    time_s: float
    tx_ids: list[str]
    rx_ids: list[str]
    links: list[tuple[str, str]]
    nodes: dict[str, tuple[list[float], list[float]]]
    actor_states: list[ActorState]
    actor_velocities: dict[str, list[float]]
    targets: list[ResolvedSensingTarget]
    sites: list[list[str]]
    bistatic_rx_ids: list[str]
    warnings: list[str] = field(default_factory=list)


def _source_config(
    scene: Scene, source: ScenarioResultSet, warnings: list[str]
) -> SimulationConfig:
    snapshot = source.metadata.get("config_snapshot")
    if isinstance(snapshot, dict):
        try:
            return SimulationConfig.model_validate(snapshot)
        except ValidationError:
            pass
    for cfg in scene.simulation_configs:
        if cfg.id == source.simulation_config_id:
            warnings.append(
                f"source {source.result_id} has no usable config_snapshot: using the scene's "
                f"simulation config {cfg.id!r}"
            )
            return cfg
    raise EnsembleError(
        f"source {source.result_id} has no config_snapshot and the scene has no simulation "
        f"config {source.simulation_config_id!r}"
    )


def _background_options(
    request: BackgroundEnsembleRequest, sensing_meta: dict
) -> BackgroundOptions:
    if request.background is not None:
        return request.background
    raw = (sensing_meta.get("options") or {}).get("background")
    if isinstance(raw, dict):
        try:
            return BackgroundOptions.model_validate({**raw, "mode": "h0_removed"})
        except ValidationError:
            pass
    return BackgroundOptions(mode="h0_removed")


def prepare_ensemble(
    scene: Scene,
    request: BackgroundEnsembleRequest,
    source: ScenarioResultSet,
    *,
    scene_hash: Optional[str] = None,
) -> EnsembleSetup:
    """Resolve the source frame (links, node kinematics, actor poses, targets)
    without solving; EnsembleError for anything a solve could not fix."""
    if request.perturb.material.scope == "prim":
        raise EnsembleError(
            "perturb.material.scope 'prim' is not supported: material groups are merged "
            "meshes, so per-prim draws need per-prim shapes (use scope 'material')"
        )
    rid = source.result_id
    index = request.source.frame_index
    if index >= len(source.frames):
        raise EnsembleError(
            f"source.frame_index {index} is out of range: {rid} has {len(source.frames)} frames"
        )
    frame = source.frames[index]
    sensing = frame.sensing
    if sensing is None:
        raise EnsembleError(f"frame {index} of {rid} has no sensing (run the scenario with sensing on)")
    warnings: list[str] = []
    config = _source_config(scene, source, warnings)
    meta = source.metadata.get("sensing") or {}

    pairs = list(dict.fromkeys((r.tx_id, r.rx_id) for r in sensing.links))
    rx_ids = list(meta.get("sensing_rx_ids") or dict.fromkeys(rx for _, rx in pairs))
    node_ids = [n.id for n in sensing.nodes] if sensing.nodes is not None else []
    tx_ids = list(
        meta.get("sensing_tx_ids")
        or [i for i in node_ids if i not in rx_ids]
        or dict.fromkeys(tx for tx, _ in pairs)
    )
    if not tx_ids or not rx_ids:
        raise EnsembleError(f"frame {index} of {rid} names no sensing TX / RX")

    devices = {d.id: d for d in scene.devices}
    for kind, ids in (("tx", tx_ids), ("rx", rx_ids)):
        for dev_id in ids:
            dev = devices.get(dev_id)
            if dev is None or dev.kind != kind:
                raise EnsembleError(f"{kind} {dev_id} of {rid} is not a {kind} device of the scene")

    stored = {n.id: n for n in sensing.nodes or []}
    nodes: dict[str, tuple[list[float], list[float]]] = {}
    missing: list[str] = []
    for dev_id in [*tx_ids, *rx_ids]:
        node = stored.get(dev_id)
        if node is None:
            dev = devices[dev_id]
            missing.append(dev_id)
            nodes[dev_id] = (
                [float(c) for c in dev.position],
                [float(c) for c in (dev.velocity_m_s or [0.0, 0.0, 0.0])],
            )
        else:
            nodes[dev_id] = ([float(c) for c in node.position], [float(c) for c in node.velocity])
    if missing:
        warnings.append(
            f"frame {index} of {rid} stores no kinematics for {missing} (a result before "
            "v0.1.13): their current scene position and velocity are used"
        )
    if scene_hash is not None and source.metadata.get("scene_hash") not in (None, scene_hash):
        warnings.append(
            f"the scene changed since {rid} (scene_hash differs): the ensemble solves the "
            "current scene at the source frame's poses"
        )

    actor_velocities, _device_velocities = _velocities_at(scene, frame.time_s)
    actors = {a.id: a for a in scene.actors}
    state_by_id = {s.id: s for s in frame.actor_states}
    targets: list[ResolvedSensingTarget] = []
    for aid in meta.get("target_ids") or []:
        actor = actors.get(aid)
        state = state_by_id.get(aid)
        if actor is None or state is None:
            raise EnsembleError(f"target actor {aid} of {rid} is not in the scene or the frame")
        try:
            targets.append(
                resolve_target_state(actor, state, actor_velocities.get(aid, [0.0, 0.0, 0.0]))
            )
        except SensingRequestError as exc:
            raise EnsembleError(str(exc)) from exc

    ids = [*tx_ids, *rx_ids]
    site_of = site_index([nodes[i][0] for i in ids])
    groups: dict[int, list[str]] = {}
    for dev_id, site in zip(ids, site_of):
        groups.setdefault(site, []).append(dev_id)
    bistatic = sorted(
        rx for rx in rx_ids
        if all(math.dist(nodes[rx][0], nodes[tx][0]) > COLOCATED_SENSING_RX_M for tx in tx_ids)
    )
    options = _background_options(request, meta)
    if not options.deterministic:
        warnings.append(
            "background.deterministic is false: realizations also differ by RT sampling "
            "noise (Monte Carlo ray resampling), which is not a clutter realization"
        )
    return EnsembleSetup(
        config=config.model_copy(update={"tx_ids": tx_ids, "rx_ids": rx_ids}),
        options=options,
        frame_index=index,
        time_s=float(frame.time_s),
        tx_ids=tx_ids,
        rx_ids=rx_ids,
        links=[(tx, rx) for tx in tx_ids for rx in rx_ids],
        nodes=nodes,
        actor_states=list(frame.actor_states),
        actor_velocities=actor_velocities,
        targets=targets,
        sites=[groups[s] for s in sorted(groups)],
        bistatic_rx_ids=bistatic,
        warnings=warnings,
    )


def draw_realization(
    seed: int,
    index: int,
    material_names: list[str],
    sites: list[list[str]],
    bistatic_rx_ids: list[str],
    perturb: EnsemblePerturbation,
) -> tuple[dict[str, EnsembleMaterialDraw], dict[str, list[float]], dict[str, float]]:
    """Realization ``index``'s draws (``draw_key``). Every draw is taken even at
    sigma 0, so one knob never shifts another's stream; identity entries are
    left out of the returned dicts."""
    rng = np.random.default_rng(
        np.random.SeedSequence(entropy=[int(seed), ENSEMBLE_STREAM], spawn_key=(int(index),))
    )
    materials: dict[str, EnsembleMaterialDraw] = {}
    for name in sorted(material_names):
        d_eps = float(rng.normal(0.0, perturb.material.eps_r_pct / 100.0))
        d_sig = float(rng.normal(0.0, perturb.material.sigma_pct / 100.0))
        if d_eps != 0.0 or d_sig != 0.0:
            materials[name] = EnsembleMaterialDraw(eps_r_scale=1.0 + d_eps, sigma_scale=1.0 + d_sig)
    offsets: dict[str, list[float]] = {}
    for site in sites:
        offset = [float(c) for c in rng.normal(0.0, perturb.node_pos_sigma_m, 3)]
        if any(c != 0.0 for c in offset):
            for dev_id in site:
                offsets[dev_id] = list(offset)
    cfo: dict[str, float] = {}
    for rx in bistatic_rx_ids:
        value = float(rng.normal(0.0, perturb.cfo_hz))
        if value != 0.0:
            cfo[rx] = value
    return materials, offsets, cfo


def _solve(
    backend: "RayTracingBackend",
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    setup: EnsembleSetup,
    offsets: dict[str, list[float]],
    materials: dict[str, EnsembleMaterialDraw],
) -> tuple[list[RayPath], list[str], float, int]:
    devices = {d.id: d for d in scene.devices}
    positions: dict[str, list[float]] = {}
    velocities: dict[str, list[float]] = {}
    for dev_id, (pos, vel) in setup.nodes.items():
        off = offsets.get(dev_id, [0.0, 0.0, 0.0])
        positions[dev_id] = [p + o for p, o in zip(pos, off)]
        # Like the tracker's frame scene: a device at rest without its own
        # velocity_m_s keeps None.
        if devices[dev_id].velocity_m_s is not None or any(c != 0.0 for c in vel):
            velocities[dev_id] = list(vel)
    frame_scene = _frame_scene(
        scene, setup.actor_states, positions, velocities,
        move_actors=backend.name != "sionna",
    )
    scales = {
        name: (draw.eps_r_scale, draw.sigma_scale) for name, draw in materials.items()
    } or None
    return solve_background(
        backend, project_dir, frame_scene, library, setup.config, setup.options,
        setup.targets, "h0",
        actor_states=setup.actor_states,
        actor_velocities=setup.actor_velocities,
        material_scales=scales,
    )


def run_background_ensemble(
    backend: "RayTracingBackend",
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    request: BackgroundEnsembleRequest,
    source: ScenarioResultSet,
    tick: Optional[Callable[[int, int], None]] = None,
    *,
    setup: Optional[EnsembleSetup] = None,
    scene_hash: Optional[str] = None,
) -> SensingBackgroundEnsembleResultSet:
    """Solve the H0 background of every realization, serially."""
    if setup is None:
        setup = prepare_ensemble(scene, request, source, scene_hash=scene_hash)
    n = request.n_realizations
    material_names = sorted(backend.radio_material_names(project_dir, scene, library))
    draws = [({}, {}, {})] + [
        draw_realization(
            request.seed, r, material_names, setup.sites, setup.bistatic_rx_ids, request.perturb
        )
        for r in range(1, n)
    ]
    solving = [r for r in range(n) if r == 0 or draws[r][0] or draws[r][1]]

    warnings = list(setup.warnings)
    seen: set[str] = set()
    realizations: list[BackgroundRealization] = []
    total_solve_s = 0.0
    for r in range(n):
        if tick is not None:
            tick(r, n)
        materials, offsets, cfo = draws[r]
        label = "perturbed" if (materials or offsets or cfo) else "nominal"
        if r not in solving:
            base = realizations[0]
            realizations.append(
                BackgroundRealization(
                    index=r, label=label, material_draws=materials, node_offsets_m=offsets,
                    cfo_hz=cfo, background_h0=None, source_realization=0,
                    num_paths=base.num_paths, dropped_paths=base.dropped_paths,
                    total_power_db=dict(base.total_power_db),
                )
            )
            continue
        try:
            paths, solve_warnings, solve_s, dropped = _solve(
                backend, project_dir, scene, library, setup, offsets, materials
            )
        except BackgroundSolveError as exc:
            # A failed solve would store an empty "H0" that calibrates
            # thresholds on noise alone.
            raise EnsembleError(f"realization {r}: background solve failed: {exc}") from exc
        total_solve_s += solve_s
        if r == 0 and len(paths) * len(solving) > MAX_ENSEMBLE_STORED_PATHS:
            raise EnsembleError(
                f"{len(paths)} background paths x {len(solving)} solved realizations = "
                f"{len(paths) * len(solving)} stored paths; at most {MAX_ENSEMBLE_STORED_PATHS} "
                "(set background.max_paths_per_link or lower n_realizations)"
            )
        for w in solve_warnings:
            if w not in seen:
                seen.add(w)
                warnings.append(f"realization {r}: {w}")
        realizations.append(
            BackgroundRealization(
                index=r, label=label, material_draws=materials, node_offsets_m=offsets,
                cfo_hz=cfo, background_h0=paths, source_realization=r, solve_s=solve_s,
                num_paths=len(paths), dropped_paths=dropped,
                total_power_db={
                    f"{tx}>{rx}": link_power_db(paths, tx, rx) for tx, rx in setup.links
                },
                warnings=list(solve_warnings),
            )
        )
    if tick is not None:
        tick(n, n)

    return SensingBackgroundEnsembleResultSet(
        result_id="unsaved",
        backend=backend.name,
        simulation_config_id=setup.config.id,
        source=request.source,
        seed=request.seed,
        noise_frames_per_realization=request.noise_frames_per_realization,
        background=setup.options,
        links=[EnsembleLink(tx_id=tx, rx_id=rx) for tx, rx in setup.links],
        nodes=[
            SensingNodeState(id=dev_id, position=pos, velocity=vel)
            for dev_id, (pos, vel) in setup.nodes.items()
        ],
        realizations=realizations,
        warnings=warnings,
        metadata={
            "request": request.model_dump(mode="json"),
            "source_kind": "scenario",
            "source_backend": source.backend,
            "source_time_s": setup.time_s,
            "target_ids": [t.actor_id for t in setup.targets],
            "material_names": material_names,
            "sites": setup.sites,
            "bistatic_rx_ids": setup.bistatic_rx_ids,
            "rt_seed": setup.config.seed,
            "solves": len(solving),
            "total_solve_s": total_solve_s,
            "draw_key": draw_key(request.seed),
            "noise_key": noise_key(request.seed, "realization", H0_NOISE_STREAM),
            "n0_note": N0_NOTE,
        },
    )


# ------------------------------------------------------------ H0 frames


def draw_key(seed: int) -> str:
    return (
        f"default_rng(SeedSequence(entropy=[{int(seed)}, {ENSEMBLE_STREAM:#x}], "
        f"spawn_key=(realization,))): {DRAW_ORDER}"
    )


def _check(
    ens: SensingBackgroundEnsembleResultSet, realization: int, frame: int, tx_id: str, rx_id: str
) -> BackgroundRealization:
    if not 0 <= realization < len(ens.realizations):
        raise EnsembleError(
            f"realization {realization} out of range: {ens.result_id} has "
            f"{len(ens.realizations)}"
        )
    if not 0 <= frame < ens.noise_frames_per_realization:
        raise EnsembleError(
            f"noise frame {frame} out of range: {ens.result_id} defines "
            f"{ens.noise_frames_per_realization} per realization"
        )
    if not any(link.tx_id == tx_id and link.rx_id == rx_id for link in ens.links):
        raise EnsembleError(f"link {tx_id} -> {rx_id} is not in {ens.result_id}")
    return ens.realizations[realization]


def realization_paths(
    ens: SensingBackgroundEnsembleResultSet, realization: int
) -> list[RayPath]:
    """The H0 paths of ``realization`` (those of its source_realization)."""
    rz = ens.realizations[realization]
    return list(ens.realizations[rz.source_realization].background_h0 or [])


def h0_noise(
    ens: SensingBackgroundEnsembleResultSet,
    realization: int,
    frame: int,
    tx_id: str,
    rx_id: str,
    shape: tuple[int, int],
    sigma2: float,
) -> np.ndarray:
    """Thermal noise of H0 frame (realization, frame) on link (tx, rx)."""
    _check(ens, realization, frame, tx_id, rx_id)
    return noise_frame(
        ens.seed, frame, tx_id, rx_id, shape, sigma2,
        realization=realization, stream=H0_NOISE_STREAM,
    )


def h0_frame(
    ens: SensingBackgroundEnsembleResultSet,
    realization: int,
    frame: int,
    tx_id: str,
    rx_id: str,
    num: "Numerology",
    tx_power_dbm: float,
    noise_figure_db: float,
    n0_dbm_per_re: Optional[float] = None,
) -> np.ndarray:
    """H0 pilot channel Y[n, k] (complex128, [grid_n, grid_k]) of one link:
    the realization's background plus its noise frame."""
    rz = _check(ens, realization, frame, tx_id, rx_id)
    f_hz, t_s = num.axes("pilot")
    paths = [
        p for p in realization_paths(ens, realization) if p.tx_id == tx_id and p.rx_id == rx_id
    ]
    h = synthesize_cfr(paths, f_hz, t_s, tx_power_dbm, cfo_hz=rz.cfo_hz.get(rx_id, 0.0))
    sigma2 = num.sigma_h2(tx_power_dbm, noise_figure_db, n0_dbm_per_re)
    return h + noise_frame(
        ens.seed, frame, tx_id, rx_id, h.shape, sigma2,
        realization=realization, stream=H0_NOISE_STREAM,
    )
