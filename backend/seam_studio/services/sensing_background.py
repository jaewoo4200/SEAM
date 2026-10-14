"""TX -> sensing-RX background (clutter) paths of a sensing scenario (#21).

Two hypotheses per frame, both a PathSolver solve over the sensing links:

- H1 (``h1_absorber``): the targets stand in for their actors as absorbers
  (the actor meshes hidden), the background a receiver sees while the
  target is there; the comm-path mechanism of ``include_comm_paths``.
- H0 (``h0_removed``): the target actors removed (meshes hidden, no
  absorber), the target-free background a false-alarm calibration needs.

The background solve uses config's los / reflection / refraction plus the
mechanisms of ``BackgroundOptions`` (diffuse reflection on by default: the
RCSSolver traces no diffuse clutter), keeping the ``max_paths_per_link``
strongest paths per link (256 by default). With ``cache: "static_nodes"`` a
hypothesis is solved once while its key (nodes, non-target actors, H1
targets, solver options, RF projection) repeats, and later frames point at
the frame holding the paths (``background_meta.h0/h1.source_frame``).
``store`` picks which hypotheses the result keeps: by default H0 only; H1
(solved every frame for a moving target) feeds the CFAR map in memory.
A failed solve is never cached: that frame has no background for the
hypothesis (``meta.error``) and the next frame solves again.
"""

import hashlib
import json
import math
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal, Optional, Sequence

from seam_studio.schemas.actors import ActorState, ScenarioFrame
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import BackgroundHypothesisMeta, BackgroundMeta, RayPath
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.sensing import BackgroundOptions
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.sensing import ResolvedSensingTarget

if TYPE_CHECKING:
    from seam_studio.services.simulation_backends.base import RayTracingBackend

Hypothesis = Literal["h0", "h1"]
# Node id -> (position, velocity), tx first, then the sensing rx.
Nodes = dict[str, tuple[list[float], list[float]]]

CACHE_KEY_VERSION = 1
_HYPOTHESES: dict[str, tuple[Hypothesis, ...]] = {
    "h0_removed": ("h0",),
    "h1_absorber": ("h1",),
    "both": ("h0", "h1"),
}
_STORED: dict[str, tuple[Hypothesis, ...]] = {"h0": ("h0",), "both": ("h0", "h1"), "none": ()}
# Solved keys a run keeps in memory per hypothesis, least recently used out
# first: a moving target makes a new H1 key every frame.
CACHE_ENTRIES_PER_HYPOTHESIS = 4
# (source frame, paths, meta) of one solved key.
CacheEntry = tuple[int, list[RayPath], BackgroundHypothesisMeta]


class BackgroundSolveError(RuntimeError):
    """The backend could not solve a background hypothesis (its warning)."""


def background_config(config: SimulationConfig, options: BackgroundOptions) -> SimulationConfig:
    """The sensing config with the background mechanisms switched in; always
    the builtin engine (the subprocess worker cannot hide actors)."""
    return config.model_copy(
        update={
            "scattering": options.diffuse_reflection,
            "diffraction": options.diffraction,
            "edge_diffraction": options.edge_diffraction,
            "max_depth": options.max_depth if options.max_depth is not None else config.max_depth,
            "num_samples": (
                options.num_samples if options.num_samples is not None else config.num_samples
            ),
            "engine": None,
        }
    )


def mechanisms(cfg: SimulationConfig) -> list[str]:
    flags = (
        ("los", cfg.los),
        ("specular_reflection", cfg.reflection),
        ("diffuse_reflection", cfg.scattering),
        ("refraction", cfg.refraction),
        ("diffraction", cfg.diffraction),
        ("edge_diffraction", cfg.edge_diffraction),
    )
    return [name for name, on in flags if on]


def path_gain_lin(p: RayPath) -> Optional[float]:
    """|a|^2 of a path (path_gain_db), None when the backend left it unset."""
    if p.path_gain_db is None:
        return None
    return 10.0 ** (p.path_gain_db / 10.0)


def link_power_db(paths: Sequence[RayPath], tx_id: str, rx_id: str) -> Optional[float]:
    """10 log10(sum |a|^2) of the link's paths; None without a path."""
    total = 0.0
    found = False
    for p in paths:
        if p.tx_id != tx_id or p.rx_id != rx_id:
            continue
        g = path_gain_lin(p)
        if g is None:
            continue
        total += g
        found = True
    if not found or total <= 0.0:
        return None
    return 10.0 * math.log10(total)


def _canonical_order(paths: list[RayPath]) -> list[RayPath]:
    """Links in solver order, each link's paths by (delay, interactions,
    vertices), renumbered path_0001...: the GPU solver returns the same path
    set in a run-dependent order (sionna-rt 2.2, deterministic=True too)."""
    link_rank: dict[tuple[str, str], int] = {}
    for p in paths:
        link_rank.setdefault((p.tx_id, p.rx_id), len(link_rank))

    def key(p: RayPath) -> tuple:
        return (
            link_rank[(p.tx_id, p.rx_id)],
            p.delay_ns,
            tuple(i.type for i in p.interactions),
            tuple(c for v in p.vertices for c in v),
            p.power_dbm,
        )

    ordered = sorted(paths, key=key)
    return [p.model_copy(update={"path_id": f"path_{k:04d}"}) for k, p in enumerate(ordered, 1)]


def _keep_strongest(
    paths: list[RayPath], max_per_link: Optional[int]
) -> tuple[list[RayPath], int]:
    """The ``max_per_link`` strongest paths per (tx, rx) in solver order, and
    how many were dropped."""
    if max_per_link is None:
        return paths, 0
    by_link: dict[tuple[str, str], list[int]] = {}
    for i, p in enumerate(paths):
        by_link.setdefault((p.tx_id, p.rx_id), []).append(i)
    keep: set[int] = set()
    for idx in by_link.values():
        ranked = sorted(idx, key=lambda i: (-paths[i].power_dbm, i))
        keep.update(ranked[:max_per_link])
    kept = [p for i, p in enumerate(paths) if i in keep]
    return kept, len(paths) - len(kept)


def solve_background(
    backend: "RayTracingBackend",
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    options: BackgroundOptions,
    targets: list[ResolvedSensingTarget],
    hypothesis: Hypothesis,
    *,
    actor_states: Optional[list] = None,
    actor_velocities: Optional[dict[str, list[float]]] = None,
    material_scales: Optional[dict[str, tuple[float, float]]] = None,
) -> tuple[list[RayPath], list[str], float, int]:
    """One background solve: (paths, warnings, solve_s, dropped). ``config``
    is the sensing config (TX -> sensing RX); the mechanisms come from
    ``options``. Every path gets target_id None and a float doppler_hz (the
    solver's Doppler, else 0.0); paths come in canonical order."""
    cfg = background_config(config, options)
    t0 = time.perf_counter()
    result = backend.simulate_background(
        project_dir, scene, library, cfg, targets,
        hypothesis=hypothesis,
        deterministic=options.deterministic,
        actor_states=actor_states,
        actor_velocities=actor_velocities,
        material_scales=material_scales,
        max_paths_per_link=options.max_paths_per_link,
    )
    solve_s = time.perf_counter() - t0
    if result.metadata.get("solve_failed"):
        raise BackgroundSolveError("; ".join(result.warnings) or "background solve failed")
    # Paths the backend dropped before converting them (sionna).
    pre_dropped = int(result.metadata.get("background_dropped_paths", 0))
    dop = result.metadata.get("doppler_hz")
    aligned = isinstance(dop, list) and len(dop) == len(result.paths)
    paths: list[RayPath] = []
    for i, p in enumerate(result.paths):
        if aligned:
            doppler = float(dop[i])
        elif p.doppler_hz is not None:
            doppler = float(p.doppler_hz)
        else:
            doppler = 0.0
        paths.append(p.model_copy(update={"doppler_hz": doppler, "target_id": None}))
    paths, dropped = _keep_strongest(_canonical_order(paths), options.max_paths_per_link)
    return paths, list(result.warnings), solve_s, dropped + pre_dropped


def _floats(v: Sequence[float]) -> list[float]:
    return [float(c) for c in v]


def background_cache_key(
    hypothesis: Hypothesis,
    *,
    backend_name: str,
    rf_fp: str,
    cfg: SimulationConfig,
    options: BackgroundOptions,
    nodes: Nodes,
    actors: list[tuple[str, Sequence[float], Sequence[float], Sequence[float]]],
    targets: list[ResolvedSensingTarget],
) -> str:
    """sha256 of everything one background solve depends on within a run.
    ``actors``: (id, position, orientation_deg, velocity) of every
    NON-target actor at the frame."""
    if hypothesis == "h0":
        target_part: list = sorted(t.actor_id for t in targets)
    else:
        target_part = sorted(
            (
                [
                    t.actor_id,
                    _floats(t.center),
                    _floats(t.orientation_deg),
                    _floats(t.size_m),
                    _floats(t.velocity_m_s),
                    t.model,
                    t.object_type,
                    t.model_type,
                    float(t.rcs_dbsm),
                ]
                for t in targets
            ),
            key=lambda row: row[0],
        )
    payload = {
        "v": CACHE_KEY_VERSION,
        "hypothesis": hypothesis,
        "backend": backend_name,
        "rf_fingerprint": rf_fp,
        "config": {
            "frequency_hz": float(cfg.frequency_hz),
            "max_depth": cfg.max_depth,
            "num_samples": cfg.num_samples,
            "seed": cfg.seed,
            "los": cfg.los,
            "reflection": cfg.reflection,
            "scattering": cfg.scattering,
            "refraction": cfg.refraction,
            "diffraction": cfg.diffraction,
            "edge_diffraction": cfg.edge_diffraction,
            "diffraction_lit_region": cfg.diffraction_lit_region,
            "synthetic_array": cfg.synthetic_array,
            "max_num_paths_per_src": cfg.max_num_paths_per_src,
            "tx_ids": cfg.tx_ids,
            "rx_ids": cfg.rx_ids,
        },
        "background": options.model_dump(mode="json", exclude={"mode", "cache", "store"}),
        "nodes": [[nid, _floats(pos), _floats(vel)] for nid, (pos, vel) in nodes.items()],
        "actors": sorted(
            ([aid, _floats(pos), _floats(orient), _floats(vel)] for aid, pos, orient, vel in actors),
            key=lambda row: row[0],
        ),
        "targets": target_part,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


def resolve_background(
    frames: list[ScenarioFrame], frame_index: int, which: Hypothesis
) -> Optional[list[RayPath]]:
    """The H0 / H1 background paths of a stored frame, following a cache hit
    to the frame that holds them; None when that hypothesis was not solved."""
    sensing = frames[frame_index].sensing
    if sensing is None or sensing.background_meta is None:
        return None
    meta = sensing.background_meta.h0 if which == "h0" else sensing.background_meta.h1
    if meta is None or not meta.stored or meta.error is not None:
        return None
    own = sensing.background_h0 if which == "h0" else sensing.background_h1
    if own is not None:
        return own
    if not 0 <= meta.source_frame < len(frames):
        return None
    src = frames[meta.source_frame].sensing
    if src is None:
        return None
    return src.background_h0 if which == "h0" else src.background_h1


@dataclass
class BackgroundFrame:
    # Full path lists in memory (cache hits included); None when the
    # hypothesis is not requested.
    h0: Optional[list[RayPath]]
    h1: Optional[list[RayPath]]
    # What SensingFrame stores: None on a cache hit and for a hypothesis
    # options.store leaves out.
    stored_h0: Optional[list[RayPath]]
    stored_h1: Optional[list[RayPath]]
    meta: BackgroundMeta
    warnings: list[str] = field(default_factory=list)


class BackgroundRunner:
    """Per-run background solver with the static_nodes cache."""

    def __init__(
        self,
        backend: "RayTracingBackend",
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        sensing_config: SimulationConfig,
        options: BackgroundOptions,
        target_ids: list[str],
    ):
        from seam_studio.services.rf_compiler import rf_fingerprint

        if options.mode == "off":
            raise ValueError("BackgroundRunner needs background.mode != 'off'")
        self.backend = backend
        self.project_dir = project_dir
        self.scene = scene
        self.library = library
        self.config = sensing_config
        self.options = options
        self.target_ids = list(target_ids)
        self.cfg = background_config(sensing_config, options)
        self.hypotheses = _HYPOTHESES[options.mode]
        self.stored = _STORED[options.store]
        self.rf_fp = rf_fingerprint(scene, library)
        self.links = [
            (t.id, r.id)
            for t in scene.devices
            if t.kind == "tx" and (sensing_config.tx_ids is None or t.id in sensing_config.tx_ids)
            for r in scene.devices
            if r.kind == "rx" and (sensing_config.rx_ids is None or r.id in sensing_config.rx_ids)
        ]
        self._cache: dict[str, OrderedDict[str, CacheEntry]] = {
            "h0": OrderedDict(), "h1": OrderedDict(),
        }
        self.solves = {"h0": 0, "h1": 0}
        self.cache_hits = {"h0": 0, "h1": 0}
        self.failures = {"h0": 0, "h1": 0}
        self.solve_s_total = 0.0
        self.paths_stored = 0
        self.paths_solved = 0
        self.paths_dropped = 0

    def _totals(self, paths: list[RayPath]) -> dict[str, Optional[float]]:
        return {f"{tx}>{rx}": link_power_db(paths, tx, rx) for tx, rx in self.links}

    def frame(
        self,
        frame_index: int,
        frame_scene: Scene,
        targets: list[ResolvedSensingTarget],
        actor_states: list[ActorState],
        actor_velocities: dict[str, list[float]],
        nodes: Nodes,
    ) -> BackgroundFrame:
        target_set = set(self.target_ids)
        actors = [
            (
                s.id,
                s.position,
                s.orientation_deg,
                actor_velocities.get(s.id, [0.0, 0.0, 0.0]),
            )
            for s in actor_states
            if s.id not in target_set
        ]
        full: dict[str, Optional[list[RayPath]]] = {"h0": None, "h1": None}
        stored: dict[str, Optional[list[RayPath]]] = {"h0": None, "h1": None}
        metas: dict[str, Optional[BackgroundHypothesisMeta]] = {"h0": None, "h1": None}
        warnings: list[str] = []
        static = self.options.cache == "static_nodes"
        for hyp in self.hypotheses:
            key = background_cache_key(
                hyp,
                backend_name=self.backend.name,
                rf_fp=self.rf_fp,
                cfg=self.cfg,
                options=self.options,
                nodes=nodes,
                actors=actors,
                targets=targets,
            )
            cache = self._cache[hyp]
            cached = cache.get(key) if static else None
            if cached is not None:
                cache.move_to_end(key)
                source, paths, src_meta = cached
                self.cache_hits[hyp] += 1
                full[hyp] = paths
                metas[hyp] = src_meta.model_copy(
                    update={"solved": False, "cache_hit": True, "source_frame": source,
                            "solve_s": 0.0}
                )
                continue
            store = hyp in self.stored
            t0 = time.perf_counter()
            try:
                paths, warns, solve_s, dropped = solve_background(
                    self.backend, self.project_dir, frame_scene, self.library, self.config,
                    self.options, targets, hyp,
                    actor_states=actor_states, actor_velocities=actor_velocities,
                )
            except BackgroundSolveError as exc:
                # Not cached: a transient failure must not empty every later frame.
                self.failures[hyp] += 1
                warnings.append(f"{hyp.upper()} solve failed at frame {frame_index}: {exc}")
                # The map then falls back to H0 / no background, like the export.
                full[hyp] = None
                metas[hyp] = BackgroundHypothesisMeta(
                    solved=False, cache_hit=False, source_frame=frame_index, cache_key=key,
                    solve_s=time.perf_counter() - t0, stored=False, error=str(exc),
                )
                continue
            self.solves[hyp] += 1
            self.solve_s_total += solve_s
            self.paths_solved += len(paths) + dropped
            self.paths_dropped += dropped
            if store:
                self.paths_stored += len(paths)
            warnings += warns
            meta = BackgroundHypothesisMeta(
                solved=True,
                cache_hit=False,
                source_frame=frame_index,
                cache_key=key,
                solve_s=solve_s,
                num_paths=len(paths),
                dropped_paths=dropped,
                total_power_db=self._totals(paths),
                stored=store,
            )
            if static:
                cache[key] = (frame_index, paths, meta)
                while len(cache) > CACHE_ENTRIES_PER_HYPOTHESIS:
                    cache.popitem(last=False)
            full[hyp] = paths
            stored[hyp] = paths if store else None
            metas[hyp] = meta
        bg_meta = BackgroundMeta(
            mode=self.options.mode,  # type: ignore[arg-type]
            mechanisms=mechanisms(self.cfg),
            max_depth=self.cfg.max_depth,
            num_samples=self.cfg.num_samples,
            seed=self.cfg.seed,
            deterministic=self.options.deterministic,
            h0=metas["h0"],
            h1=metas["h1"],
        )
        return BackgroundFrame(
            h0=full["h0"],
            h1=full["h1"],
            stored_h0=stored["h0"],
            stored_h1=stored["h1"],
            meta=bg_meta,
            warnings=warnings,
        )

    def run_warnings(self) -> list[str]:
        """Run-wide notes, once per run (after the last frame)."""
        cap = self.options.max_paths_per_link
        if cap is None or self.paths_dropped == 0:
            return []
        return [
            f"background: max_paths_per_link {cap} kept the strongest {cap} paths per link: "
            f"{self.paths_dropped} of {self.paths_solved} solved background paths dropped "
            "over the run (set sensing.background.max_paths_per_link null to keep every path)"
        ]

    def summary(self) -> dict:
        return {
            "mode": self.options.mode,
            "mechanisms": mechanisms(self.cfg),
            "max_depth": self.cfg.max_depth,
            "num_samples": self.cfg.num_samples,
            "seed": self.cfg.seed,
            "deterministic": self.options.deterministic,
            "cache": self.options.cache,
            "store": self.options.store,
            "max_paths_per_link": self.options.max_paths_per_link,
            "solves_h0": self.solves["h0"],
            "solves_h1": self.solves["h1"],
            "cache_hits_h0": self.cache_hits["h0"],
            "cache_hits_h1": self.cache_hits["h1"],
            "failed_solves_h0": self.failures["h0"],
            "failed_solves_h1": self.failures["h1"],
            "solve_s_total": self.solve_s_total,
            "paths_stored": self.paths_stored,
            "paths_dropped": self.paths_dropped,
        }
