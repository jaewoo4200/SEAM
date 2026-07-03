"""Optional Sionna RT backend (targets sionna-rt 2.x, Dr.Jit/Mitsuba 3).

Contract (HANDOFF.md sections 7.2, 14): Sionna is imported lazily inside
methods, availability is probed without heavy imports, and ANY failure -
missing install, version API drift, solver errors, incompatible scene -
degrades to an empty result set with a warning instead of a 500. The app must
never break because Sionna is absent or its API moved.

Verified against sionna-rt 2.0.1 on this machine's Quadro RTX 8000 (Dr.Jit
CUDA backend). Our compiled rf/generated_scene.xml loads directly: ITU
materials resolve from the "mat-itu_*" bsdf ids and constant materials load
from the "radio-material" bsdf plugin the compiler emits.
"""

import json
import math
from pathlib import Path
from typing import Optional

from app.schemas.devices import Antenna, Device
from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import (
    BeamformingResult,
    PathInteraction,
    PathResultSet,
    RadioMapGrid,
    RadioMapResultSet,
    RayPath,
)
from app.schemas.scene import Scene
from app.schemas.simulation import BeamformingRequest, SimulationConfig

from .base import UNSAVED_RESULT_ID, RayTracingBackend

# paths.objects sentinel for "no interaction at this depth" (uint32 max).
_NO_OBJECT = 0xFFFFFFFF


def _steering_from_positions(y_norm, angle_deg: float, np):
    """Azimuth steering vector built from the array's ACTUAL element
    positions (y = horizontal offset in wavelengths, from
    PlanarArray.normalized_positions) - immune to element-ordering
    assumptions. Vertical stays broadside: this is the azimuth-only DFT
    codebook real mmWave beam training sweeps (ICC'26 paper setup).
    Phase sign verified against a 1x4 ULA probe: element phase grows as
    +2*pi*y*sin(azimuth)."""
    import math

    phase = 2.0 * math.pi * np.asarray(y_norm) * math.sin(math.radians(angle_deg))
    w = np.exp(1j * phase)
    return w / np.linalg.norm(w)


def _codebook_sweep(base, H, h00: float, request, np, tx_y_norm, rx_y_norm) -> None:
    """Hardware-style beam training: scan an azimuth DFT codebook on both
    ends, record the full [rx_beam][tx_beam] gain map, select the best pair."""
    import math

    angles = []
    a = request.sweep_start_deg
    while a <= request.sweep_stop_deg + 1e-9:
        angles.append(round(a, 6))
        a += request.sweep_step_deg
    tx_beams = [_steering_from_positions(tx_y_norm, ang, np) for ang in angles]
    rx_beams = [_steering_from_positions(rx_y_norm, ang, np) for ang in angles]

    sweep: list[list[float]] = []
    best = (-1.0, 0, 0)
    for i, w_r in enumerate(rx_beams):
        row: list[float] = []
        for j, w_t in enumerate(tx_beams):
            power = abs(np.vdot(w_r, H @ w_t)) ** 2
            gain_db = 10.0 * math.log10(max(power / h00, 1e-30))
            row.append(round(gain_db, 3))
            if power > best[0]:
                best = (power, i, j)
        sweep.append(row)

    base.sweep_angles_deg = angles
    base.sweep_gain_db = sweep
    if best[0] > 0:
        base.codebook_gain_db = 10.0 * math.log10(best[0] / h00)
        base.best_rx_angle_deg = angles[best[1]]
        base.best_tx_angle_deg = angles[best[2]]
    base.metadata["beam_pairs_scanned"] = len(angles) ** 2

# Sionna RT interaction-type codes -> our schema interaction type. Code 0 is
# "none"; the rest are mapped defensively (unknown codes fall back to
# reflection) so a version bump cannot crash conversion.
_INTERACTION_TYPES = {1: "reflection", 2: "scattering", 3: "transmission", 4: "diffraction"}

# sionna.rt.PlanarArray patterns/polarizations we validate against (verified via
# the antenna_pattern / polarization registries in sionna-rt 2.0.1). Unknown
# pattern -> iso fallback with a warning so a bad device config never raises.
_VALID_PATTERNS = ("iso", "dipole", "hw_dipole", "tr38901")
_VALID_POLARIZATIONS = ("V", "H", "VH", "cross")


# --------------------------------------------------------------- scene cache
#
# Loading generated_scene.xml is the dominant cost of a trajectory solve (one
# Mitsuba scene compile per waypoint). We cache the loaded rt_scene keyed by
# (xml path, mtime_ns) so N waypoints reuse a single load. Before each use the
# caller strips the previously added transmitters/receivers and reapplies
# frequency + arrays, so a cached scene is functionally identical to a fresh
# load. A mtime change (the compiler rewriting the file, e.g. per calibration
# trial) invalidates the entry automatically.
_SCENE_CACHE: dict[str, tuple[int, object]] = {}
# Test-visible counters: hits/misses/loads. Reset via clear_scene_cache().
_CACHE_STATS = {"hits": 0, "misses": 0, "loads": 0}


def clear_scene_cache() -> None:
    """Drop all cached rt_scenes. Called defensively on any solver exception so
    a scene left in a bad state is never reused. Hit/miss/load counters are
    left intact (they are cumulative diagnostics, not cache state); a fresh
    load after this will simply register as another miss."""
    _SCENE_CACHE.clear()


def cache_stats() -> dict:
    """Hit/miss/load counters, exposed for tests to assert cache behaviour."""
    return dict(_CACHE_STATS)


def _load_scene_cached(xml_path: Path, warnings: list[str]):
    """Return a loaded rt_scene for ``xml_path``, reusing the cached instance
    when the file's mtime is unchanged. On a hit the returned scene still has
    whatever tx/rx the previous caller added; ``_reset_scene_devices`` clears
    them. On a miss (or mtime change) the file is (re)loaded."""
    from sionna.rt import load_scene  # type: ignore[import-not-found]

    key = str(xml_path)
    try:
        mtime = xml_path.stat().st_mtime_ns
    except OSError:
        mtime = -1
    cached = _SCENE_CACHE.get(key)
    if cached is not None and cached[0] == mtime:
        _CACHE_STATS["hits"] += 1
        return cached[1]
    _CACHE_STATS["misses"] += 1
    _CACHE_STATS["loads"] += 1
    rt_scene = load_scene(str(xml_path))
    _SCENE_CACHE[key] = (mtime, rt_scene)
    return rt_scene


def _reset_scene_devices(rt_scene) -> None:
    """Remove every transmitter/receiver from a (possibly cached) scene so it
    starts empty for the next solve. scene.transmitters/.receivers are dict
    copies, so removing by key while iterating is safe."""
    for name in list(rt_scene.transmitters.keys()):
        rt_scene.remove(name)
    for name in list(rt_scene.receivers.keys()):
        rt_scene.remove(name)


def _make_planar_array(antenna: Antenna, warnings: list[str], *, num_rows=None, num_cols=None):
    """Build a sionna.rt.PlanarArray from a Device.antenna, validating the
    pattern/polarization against the installed registries. Unknown pattern ->
    iso with a warning; unknown polarization -> V with a warning. num_rows /
    num_cols override the device geometry (used by beamforming's explicit
    array request while keeping the device's pattern/polarization)."""
    from sionna.rt import PlanarArray  # type: ignore[import-not-found]

    pattern = antenna.pattern
    if pattern not in _VALID_PATTERNS:
        warnings.append(
            f"unknown antenna pattern {pattern!r}; falling back to 'iso' "
            f"(valid: {', '.join(_VALID_PATTERNS)})"
        )
        pattern = "iso"
    polarization = antenna.polarization
    if polarization not in _VALID_POLARIZATIONS:
        warnings.append(
            f"unknown antenna polarization {polarization!r}; falling back to 'V'"
        )
        polarization = "V"
    rows = int(num_rows if num_rows is not None else antenna.num_rows)
    cols = int(num_cols if num_cols is not None else antenna.num_cols)
    return PlanarArray(
        num_rows=max(1, rows),
        num_cols=max(1, cols),
        vertical_spacing=antenna.vertical_spacing,
        horizontal_spacing=antenna.horizontal_spacing,
        pattern=pattern,
        polarization=polarization,
    )


def _apply_arrays(
    rt_scene,
    txs: list[Device],
    rxs: list[Device],
    warnings: list[str],
) -> None:
    """Set rt_scene.tx_array / rx_array from the first selected TX/RX device's
    antenna. Falls back to an isotropic 1x1 array when a side has no device."""
    default = Antenna()
    tx_antenna = txs[0].antenna if txs else default
    rx_antenna = rxs[0].antenna if rxs else default
    rt_scene.tx_array = _make_planar_array(tx_antenna, warnings)
    rt_scene.rx_array = _make_planar_array(rx_antenna, warnings)


def _actor_object_key(rt_scene, actor_id: str) -> Optional[str]:
    """Resolve the SceneObject key for an actor's shape.

    Sionna's XML shape id is ``shape-actor-<id>``, but the loader may keep or
    strip the ``shape-`` prefix, and it MERGES every shape sharing one radio
    material into a single ``merged-shapes`` object (verified on sionna-rt
    2.0.1). When that happens the individual actor shape is no longer
    addressable, so we return None and the caller warns instead of crashing.
    The compiler gives each material its own bsdf and the demo actors use
    materials (metal, human_body) not shared with static geometry, so they
    stay individually movable there.
    """
    objs = rt_scene.objects
    for candidate in (f"shape-actor-{actor_id}", f"actor-{actor_id}"):
        if candidate in objs:
            return candidate
    return None


def apply_actor_states(rt_scene, states, base_actors: dict) -> list[str]:
    """Move each actor's SceneObject to the pose in ``states`` for one frame.

    ``states`` is a list of ActorState (id/position/orientation_deg);
    ``base_actors`` maps actor id -> the authored Actor (its scene pose). The
    actor mesh is baked at the authored pose, and Sionna reports/accepts
    ``SceneObject.position`` as the mesh's ABSOLUTE world centroid (verified by
    probing: reading gives the baked centroid, writing replaces it). So to move
    an actor whose authored base is ``p0`` to a new base ``p1`` we shift the
    baked centroid by the delta ``p1 - p0`` - this is correct for box and mesh
    actors alike without needing the centroid offset.

    Orientation: only yaw (Z, radians) is set. Returns per-actor warnings for
    unknown / unaddressable (merged) actors so the caller can surface them."""
    import mitsuba as mi  # type: ignore[import-not-found]
    import numpy as np

    def vec3(dr_point) -> list[float]:
        # SceneObject.position/orientation are Mitsuba Point3f; numpy yields a
        # (3,1) array of drjit scalars. Flatten to three plain floats.
        return [float(v) for v in np.array(dr_point).reshape(-1)[:3]]

    # Setting SceneObject.position is ABSOLUTE (it replaces the mesh centroid),
    # so a delta must always be measured from the AUTHORED baked pose, not the
    # scene's current (already-moved) pose. We capture each actor's authored
    # centroid/orientation the first time we see it and cache it on the scene,
    # so repeated per-frame calls on a cached rt_scene stay correct.
    authored = getattr(rt_scene, "_actor_authored_pose", None)
    if authored is None:
        authored = {}
        setattr(rt_scene, "_actor_authored_pose", authored)

    warnings: list[str] = []
    for state in states:
        base = base_actors.get(state.id)
        if base is None:
            warnings.append(f"actor state for unknown actor {state.id!r}; ignored")
            continue
        key = _actor_object_key(rt_scene, state.id)
        if key is None:
            warnings.append(
                f"actor {state.id!r} shape not individually addressable in the "
                "loaded scene (Sionna merged it with same-material geometry); "
                "its per-frame movement was not applied"
            )
            continue
        obj = rt_scene.objects[key]
        if state.id not in authored:
            authored[state.id] = (vec3(obj.position), vec3(obj.orientation))
        baked_centroid, baked_orient = authored[state.id]
        # Absolute target centroid = authored centroid + (target base - authored
        # base). Correct for box and mesh actors without knowing the offset.
        p0 = [float(v) for v in base.position]
        p1 = [float(v) for v in state.position]
        new_centroid = [baked_centroid[i] + (p1[i] - p0[i]) for i in range(3)]
        obj.position = mi.Point3f(new_centroid[0], new_centroid[1], new_centroid[2])
        # Yaw only. Sionna orientation is [alpha(Z-yaw), beta(Y), gamma(X)] rad;
        # index 0 is the yaw. Our orientation_deg is [yaw, pitch, roll], so
        # index 0 on both sides. Preserve beta/gamma, set the delta yaw.
        try:
            yaw = baked_orient[0] + math.radians(
                float(state.orientation_deg[0]) - float(base.orientation_deg[0])
            )
            obj.orientation = mi.Point3f(yaw, baked_orient[1], baked_orient[2])
        except Exception as exc:  # noqa: BLE001 - orientation is best-effort
            warnings.append(f"could not set actor {state.id!r} orientation: {exc}")
    return warnings


def noise_floor_dbm(config: SimulationConfig) -> float:
    """Thermal noise floor + receiver noise figure, in dBm.

    kTB at 290 K is -174 dBm/Hz; add 10log10(bandwidth) and the NF. This is an
    SNR reference (no interference term), so downstream SINR == SNR = signal -
    noise_floor.
    """
    return -174.0 + 10.0 * math.log10(config.bandwidth_hz) + config.noise_figure_db


class SionnaBackend(RayTracingBackend):
    name = "sionna"

    def is_available(self) -> bool:
        from app.services.availability import sionna_available

        return sionna_available()

    # ------------------------------------------------------------- paths

    def simulate_paths(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
        actor_states: Optional[list] = None,
    ) -> PathResultSet:
        try:
            # Alternate engine venvs (config.engine) run through the subprocess
            # worker; the builtin engine solves in-process.
            if config.engine and config.engine != "builtin":
                return self._simulate_paths_engine(
                    project_dir, scene, library, config, actor_states
                )
            return self._simulate_paths_impl(
                project_dir, scene, library, config, actor_states
            )
        except Exception as exc:  # noqa: BLE001 - graceful degradation contract
            # A scene left half-mutated in the cache must not be reused.
            clear_scene_cache()
            # Keep the actionable frequency hints even on the failure path.
            return PathResultSet(
                result_id=UNSAVED_RESULT_ID,
                backend=self.name,
                simulation_config_id=config.id,
                paths=[],
                warnings=self._frequency_warnings(scene, library, config)
                + [f"sionna backend failed: {exc}; see logs"],
                metadata={
                    "frequency_hz": config.frequency_hz,
                    "engine": config.engine or "builtin",
                },
            )

    def _simulate_paths_engine(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
        actor_states: Optional[list] = None,
    ) -> PathResultSet:
        """Solve paths in an alternate sionna-rt venv (subprocess worker)."""
        from .. import engines as engine_registry

        warnings: list[str] = self._frequency_warnings(scene, library, config)
        engine = engine_registry.get_engine(config.engine)  # type: ignore[arg-type]
        if engine is None or not engine.available:
            detail = engine.detail if engine else "unknown engine id"
            raise RuntimeError(f"engine '{config.engine}' unavailable: {detail}")

        xml_path = project_dir / "rf" / "generated_scene.xml"
        if not xml_path.is_file():
            compile_result = self.compile(project_dir, scene, library)
            if not compile_result.ok or not xml_path.is_file():
                raise RuntimeError("rf projection missing and compile failed")
            warnings.append("rf projection was missing; compiled on demand")

        if actor_states:
            # Actor states mutate the in-process cached scene; the subprocess
            # worker loads the XML fresh, where actors sit at authored poses.
            warnings.append(
                f"engine '{engine.id}': per-frame actor states are not applied "
                "(actors solve at authored poses); use the builtin engine for "
                "scenario playback"
            )

        txs = [d for d in scene.devices
               if d.kind == "tx" and (config.tx_ids is None or d.id in config.tx_ids)]
        rxs = [d for d in scene.devices
               if d.kind == "rx" and (config.rx_ids is None or d.id in config.rx_ids)]
        if not txs or not rxs:
            return PathResultSet(
                result_id=UNSAVED_RESULT_ID, backend=self.name,
                simulation_config_id=config.id, paths=[],
                warnings=warnings + ["scene has no matching tx/rx devices"],
                metadata={"frequency_hz": config.frequency_hz, "engine": engine.id},
            )

        material_to_prims: dict[str, list[str]] = {}
        for prim in scene.prims:
            if prim.rf.material_id:
                material_to_prims.setdefault(prim.rf.material_id, []).append(prim.id)

        def dev_json(d):
            return {
                "id": d.id, "position": list(d.position),
                "orientation_deg": list(d.orientation_deg),
                "power_dbm": d.power_dbm,
                "antenna": {
                    "pattern": d.antenna.pattern,
                    "polarization": d.antenna.polarization,
                    "num_rows": d.antenna.num_rows,
                    "num_cols": d.antenna.num_cols,
                    "vertical_spacing": d.antenna.vertical_spacing,
                    "horizontal_spacing": d.antenna.horizontal_spacing,
                },
            }

        manifest_path = project_dir / "rf" / "compile_manifest.json"
        job = {
            "kind": "paths",
            "xml_path": str(xml_path),
            "manifest_path": str(manifest_path) if manifest_path.is_file() else None,
            "frequency_hz": config.frequency_hz,
            "max_depth": config.max_depth,
            "seed": config.seed,
            "num_samples": config.num_samples,
            "synthetic_array": config.synthetic_array,
            "flags": {
                "los": config.los, "reflection": config.reflection,
                "scattering": config.scattering, "refraction": config.refraction,
                "diffraction": config.diffraction,
                "edge_diffraction": config.edge_diffraction,
                "diffraction_lit_region": config.diffraction_lit_region,
            },
            "txs": [dev_json(d) for d in txs],
            "rxs": [dev_json(d) for d in rxs],
            "material_to_prims": material_to_prims,
        }
        result = engine_registry.run_paths_job(engine, job)
        warnings.extend(result.get("warnings", []))
        paths = [RayPath(**p) for p in result.get("paths", [])]
        return PathResultSet(
            result_id=UNSAVED_RESULT_ID,
            backend=self.name,
            simulation_config_id=config.id,
            paths=paths,
            warnings=warnings,
            metadata={
                "frequency_hz": config.frequency_hz,
                "num_tx": len(txs), "num_rx": len(rxs),
                "engine": engine.id,
                "engine_version": result.get("engine_version"),
            },
        )

    def _simulate_paths_impl(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
        actor_states: Optional[list] = None,
    ) -> PathResultSet:
        import numpy as np

        # Sionna 1.x exposes RT as the standalone sionna-rt package under
        # sionna.rt with these names; 0.x had solver methods on the scene
        # object instead of PathSolver. We target 1.x and let the outer
        # try/except absorb anything older/newer.
        from sionna.rt import (  # type: ignore[import-not-found]
            PathSolver,
            Receiver,
            Transmitter,
        )

        warnings: list[str] = self._frequency_warnings(scene, library, config)

        # Ensure the compiled RF projection exists; compile on demand.
        xml_path = project_dir / "rf" / "generated_scene.xml"
        if not xml_path.is_file():
            compile_result = self.compile(project_dir, scene, library)
            if not compile_result.ok or not xml_path.is_file():
                raise RuntimeError(
                    "rf/generated_scene.xml missing and compile did not produce it: "
                    + "; ".join(compile_result.errors or ["unknown compile error"])
                )
            warnings.append("rf projection was missing; compiled on demand")

        # Cached scene load: reuse across waypoints/grid solves keyed by mtime.
        rt_scene = _load_scene_cached(xml_path, warnings)
        _reset_scene_devices(rt_scene)
        rt_scene.frequency = config.frequency_hz

        txs = [
            d for d in scene.devices
            if d.kind == "tx" and (config.tx_ids is None or d.id in config.tx_ids)
        ]
        rxs = [
            d for d in scene.devices
            if d.kind == "rx" and (config.rx_ids is None or d.id in config.rx_ids)
        ]
        if not txs or not rxs:
            return PathResultSet(
                result_id=UNSAVED_RESULT_ID,
                backend=self.name,
                simulation_config_id=config.id,
                paths=[],
                warnings=warnings
                + ["scene has no matching tx/rx devices; no paths computed"],
                metadata={"frequency_hz": config.frequency_hz, "engine": "sionna"},
            )

        # Per-device antenna arrays: first selected TX/RX device drives the
        # scene's tx_array/rx_array (pattern/polarization/geometry).
        _apply_arrays(rt_scene, txs, rxs, warnings)

        for dev in txs:
            rt_scene.add(
                Transmitter(
                    name=dev.id,
                    position=list(dev.position),
                    orientation=[math.radians(a) for a in dev.orientation_deg],
                    power_dbm=dev.power_dbm,
                )
            )
        for dev in rxs:
            rt_scene.add(
                Receiver(
                    name=dev.id,
                    position=list(dev.position),
                    orientation=[math.radians(a) for a in dev.orientation_deg],
                )
            )

        self._apply_custom_materials(project_dir, rt_scene, warnings)

        # Per-frame actor movement: move each actor's SceneObject to its state
        # for this frame before solving (scenario / live-sync flows). The scene
        # is cached across frames, so apply_actor_states measures deltas from
        # the captured authored pose to keep repeated calls correct.
        if actor_states:
            base_actors = {a.id: a for a in scene.actors}
            warnings.extend(apply_actor_states(rt_scene, actor_states, base_actors))

        # Map Sionna's per-interaction object ids back to canonical prims.
        # Shape names are "shape-<rf_material_id>" (the compiler's convention),
        # so the object id -> rf material id, and (when a material group holds
        # exactly one prim) -> a single canonical prim id.
        objid_to_material: dict[int, str] = {}
        for name, obj in rt_scene.objects.items():
            mat_id = name[len("shape-"):] if name.startswith("shape-") else name
            objid_to_material[int(obj.object_id)] = mat_id
        material_to_prims: dict[str, list[str]] = {}
        for prim in scene.prims:
            if prim.rf.material_id:
                material_to_prims.setdefault(prim.rf.material_id, []).append(prim.id)

        solver = PathSolver()
        # Full solver passthrough: every SimulationConfig interaction/mechanics
        # flag maps to the matching PathSolver kwarg (verified against
        # sionna-rt 2.0.1 PathSolver.__call__).
        solved = solver(
            rt_scene,
            max_depth=config.max_depth,
            los=config.los,
            specular_reflection=config.reflection,
            diffuse_reflection=config.scattering,
            refraction=config.refraction,
            diffraction=config.diffraction,
            edge_diffraction=config.edge_diffraction,
            diffraction_lit_region=config.diffraction_lit_region,
            synthetic_array=config.synthetic_array,
            seed=config.seed,
            samples_per_src=config.num_samples or 1_000_000,
        )

        paths = self._convert_paths(
            solved, txs, rxs, objid_to_material, material_to_prims, warnings, np
        )
        return PathResultSet(
            result_id=UNSAVED_RESULT_ID,
            backend=self.name,
            simulation_config_id=config.id,
            paths=paths,
            warnings=warnings,
            metadata={
                "frequency_hz": config.frequency_hz,
                "num_tx": len(txs),
                "num_rx": len(rxs),
                "engine": "sionna",
            },
        )

    # ------------------------------------------------------ beamforming

    def simulate_beamforming(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
        request: BeamformingRequest,
    ) -> BeamformingResult:
        txs = [d for d in scene.devices if d.kind == "tx"]
        rxs = [d for d in scene.devices if d.kind == "rx"]
        tx = next((d for d in txs if d.id == request.tx_id), txs[0] if txs else None)
        rx = next((d for d in rxs if d.id == request.rx_id), rxs[0] if rxs else None)
        base = BeamformingResult(
            backend=self.name,
            simulation_config_id=config.id,
            tx_id=tx.id if tx else "",
            rx_id=rx.id if rx else "",
            frequency_hz=config.frequency_hz,
            tx_array=[request.tx_rows, request.tx_cols],
            rx_array=[request.rx_rows, request.rx_cols],
            metadata={"engine": "sionna"},
        )
        if tx is None or rx is None:
            base.warnings.append("scene needs at least one tx and one rx")
            return base
        try:
            return self._beamforming_impl(project_dir, scene, library, config, request, tx, rx, base)
        except Exception as exc:  # noqa: BLE001 - graceful degradation contract
            clear_scene_cache()
            base.warnings.append(f"sionna beamforming failed: {exc}; see logs")
            return base

    def _beamforming_impl(
        self, project_dir, scene, library, config, request, tx, rx, base
    ) -> BeamformingResult:
        import math

        import numpy as np
        from sionna.rt import (  # type: ignore[import-not-found]
            PathSolver,
            Receiver,
            Transmitter,
        )

        base.warnings.extend(self._frequency_warnings(scene, library, config))
        xml_path = project_dir / "rf" / "generated_scene.xml"
        if not xml_path.is_file():
            self.compile(project_dir, scene, library)
        rt_scene = _load_scene_cached(xml_path, base.warnings)
        _reset_scene_devices(rt_scene)
        rt_scene.frequency = config.frequency_hz
        # Beamforming keeps the request's explicit array geometry but adopts the
        # device's pattern/polarization.
        rt_scene.tx_array = _make_planar_array(
            tx.antenna, base.warnings, num_rows=request.tx_rows, num_cols=request.tx_cols
        )
        rt_scene.rx_array = _make_planar_array(
            rx.antenna, base.warnings, num_rows=request.rx_rows, num_cols=request.rx_cols
        )
        self._apply_custom_materials(project_dir, rt_scene, base.warnings)
        # Panels face each other (look_at), like the lab presets' explicit
        # boresights: without this, a steep link loses its vertical array gain
        # to broadside mismatch and the azimuth-only codebook can't recover it
        # (verified: -23 deg elevation costs ~5.7 dB per end at 4 rows).
        rt_scene.add(
            Transmitter(
                name=tx.id,
                position=list(tx.position),
                power_dbm=tx.power_dbm,
                look_at=list(rx.position),
            )
        )
        rt_scene.add(
            Receiver(name=rx.id, position=list(rx.position), look_at=list(tx.position))
        )

        # synthetic_array=True keeps the per-antenna channel tensor dense so the
        # MRT/SVD math below has a full [rx_ant, tx_ant] matrix per path.
        paths = PathSolver()(
            rt_scene, max_depth=config.max_depth, los=config.los,
            specular_reflection=config.reflection, diffuse_reflection=config.scattering,
            refraction=config.refraction, diffraction=config.diffraction,
            edge_diffraction=config.edge_diffraction,
            diffraction_lit_region=config.diffraction_lit_region, synthetic_array=True,
            seed=config.seed, samples_per_src=config.num_samples or 1_000_000,
        )
        a_raw = paths.a
        a = (np.asarray(a_raw[0]) + 1j * np.asarray(a_raw[1])) if isinstance(a_raw, (tuple, list)) else np.asarray(a_raw)
        # a: [num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths]; sum over paths
        # to a per-antenna-pair channel H for the first tx/rx device.
        if a.ndim != 5 or a.shape[-1] == 0:
            base.warnings.append(f"unexpected/empty path coefficients {a.shape}; no beamforming")
            return base
        H = a[0, :, 0, :, :].sum(axis=-1)  # [num_rx_ant, num_tx_ant]
        base.num_paths = int(a.shape[-1])
        h00 = abs(H[0, 0]) ** 2
        if h00 <= 0:
            base.warnings.append("degenerate channel (zero reference element); no gain computed")
            return base
        base.single_element_dbm = 10.0 * math.log10(h00) + tx.power_dbm
        # TX-MRT toward the first RX antenna: power = ||H[0, :]||^2.
        h0 = H[0, :]
        tx_mrt = float(np.vdot(h0, h0).real)
        base.tx_mrt_gain_db = 10.0 * math.log10(max(tx_mrt / h00, 1e-30))
        # Both-ends SVD: largest singular value squared.
        sigma_max = float(np.linalg.svd(H, compute_uv=False)[0])
        base.svd_gain_db = 10.0 * math.log10(max(sigma_max ** 2 / h00, 1e-30))

        base.mode = request.mode
        if request.mode == "codebook_sweep":
            # Horizontal element offsets (in wavelengths) straight from the
            # arrays: makes the codebook independent of element ordering.
            # Dual-polarized arrays expose num_pol x elements ports in H,
            # ordered polarization-major (all elements pol A, then pol B -
            # verified via a 1x4 cross-pol phase probe), so tile the element
            # offsets per polarization to get per-port offsets.
            tx_y = np.asarray(rt_scene.tx_array.normalized_positions)[1]
            rx_y = np.asarray(rt_scene.rx_array.normalized_positions)[1]
            if H.shape[1] % len(tx_y) == 0 and H.shape[0] % len(rx_y) == 0:
                tx_y = np.tile(tx_y, H.shape[1] // len(tx_y))
                rx_y = np.tile(rx_y, H.shape[0] // len(rx_y))
                _codebook_sweep(base, H, h00, request, np, tx_y, rx_y)
            else:
                base.warnings.append(
                    f"channel ports {H.shape} not a polarization multiple of "
                    f"element counts ({len(rx_y)}x{len(tx_y)}); codebook sweep skipped"
                )
        return base

    @staticmethod
    def _frequency_warnings(
        scene: Scene, library: RFMaterialLibrary, config: SimulationConfig
    ) -> list[str]:
        """ITU ground models (very_dry/medium_dry/wet) are only defined up to
        ~10 GHz. Above that, warn and point at the constant ground material."""
        if config.frequency_hz <= 10e9:
            return []
        flagged: set[str] = set()
        for prim in scene.prims:
            mat = library.get(prim.rf.material_id) if prim.rf.material_id else None
            if (
                mat
                and mat.model == "itu_frequency_dependent"
                and mat.category == "ground"
            ):
                flagged.add(mat.id)
        if not flagged:
            return []
        return [
            f"frequency {config.frequency_hz/1e9:.1f} GHz exceeds ~10 GHz: ITU "
            f"ground material(s) {sorted(flagged)} are outside their valid band; "
            "consider the 'ground_28ghz' constant material for mmWave scenes"
        ]

    @staticmethod
    def _apply_custom_materials(project_dir: Path, rt_scene, warnings: list[str]) -> None:
        """Push constant-model material parameters onto loaded RadioMaterials.

        The compiler writes rf/compile_manifest.json describing the material
        groups it exported. For custom constant materials we override the
        loaded scene's RadioMaterial parameters when a material of the same
        name exists; ITU materials are left to Sionna's built-in tables.
        """
        manifest_path = project_dir / "rf" / "compile_manifest.json"
        if not manifest_path.is_file():
            return
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            warnings.append(f"could not read compile manifest: {exc}")
            return

        # Manifest layout (written by rf_compiler._manifest): {"groups":
        # [{"rf_material_id", "itu_name", "custom_material": {...}|null, ...}]}
        # where custom_material carries the constant-model parameters. The XML
        # already embeds these via the radio-material plugin; this pass is a
        # defensive re-sync that also surfaces missing materials as warnings.
        for entry in manifest.get("groups", []):
            if not isinstance(entry, dict):
                continue
            custom = entry.get("custom_material")
            mat_id = entry.get("rf_material_id")
            if not mat_id or not isinstance(custom, dict):
                continue
            try:
                # Sionna 1.x: scene.radio_materials is a dict name->RadioMaterial
                # with settable relative_permittivity / conductivity. The name
                # may or may not keep the XML's "mat-" prefix depending on
                # loader version, so try both.
                materials = rt_scene.radio_materials
                rt_mat = materials.get(mat_id) or materials.get(f"mat-{mat_id}")
                if rt_mat is None:
                    warnings.append(
                        f"custom material {mat_id!r} from the compile manifest "
                        "was not found in the loaded Sionna scene; its "
                        "parameters were not applied"
                    )
                    continue
                eps = custom.get("relative_permittivity")
                sigma = custom.get("conductivity_s_per_m")
                if eps is not None:
                    rt_mat.relative_permittivity = float(eps)
                if sigma is not None:
                    rt_mat.conductivity = float(sigma)
                scattering = custom.get("scattering_coefficient")
                if scattering is not None:
                    try:
                        rt_mat.scattering_coefficient = float(scattering)
                    except Exception:  # noqa: BLE001 - optional across versions
                        pass
            except Exception as exc:  # noqa: BLE001 - per-material best effort
                warnings.append(f"could not apply material {mat_id!r}: {exc}")

    @staticmethod
    def _convert_paths(
        solved,
        txs,
        rxs,
        objid_to_material: dict[int, str],
        material_to_prims: dict[str, list[str]],
        warnings: list[str],
        np,
    ) -> list[RayPath]:
        """Normalize a sionna-rt 2.x Paths object into schema RayPath entries.

        Verified tensor layout (synthetic 1x1 arrays, synthetic_array=True):
        - solved.a:            tuple(real, imag), each
                               [num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths]
        - solved.tau:          [num_rx, num_tx, num_paths]
        - solved.vertices:     [max_depth, num_rx, num_tx, num_paths, 3]
        - solved.valid:        bool [num_rx, num_tx, num_paths]
        - solved.interactions: uint [max_depth, num_rx, num_tx, num_paths]
        - solved.objects:      uint [max_depth, num_rx, num_tx, num_paths],
                               _NO_OBJECT where a depth slot is unused
        Anything that does not fit is skipped with a warning, not raised.
        """
        def to_np(x):
            return np.asarray(x.numpy() if hasattr(x, "numpy") else x)

        tau = to_np(solved.tau)
        a_raw = solved.a
        if isinstance(a_raw, (tuple, list)) and len(a_raw) == 2:
            a = to_np(a_raw[0]) + 1j * to_np(a_raw[1])
        else:
            a = to_np(a_raw)
        # a is [num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths]; collapse the
        # antenna axes (1 and 3) to [num_rx, num_tx, num_paths]. Singleton axes
        # (synthetic_array / 1x1 arrays) squeeze cleanly; multi-antenna arrays
        # (per-device tr38901 etc.) are reduced to a per-link amplitude by
        # summing element power, so a real array still yields one entry per path.
        if a.ndim == 5:
            if a.shape[1] == 1 and a.shape[3] == 1:
                a = a[:, 0, :, 0, :]
            else:
                power = (np.abs(a) ** 2).sum(axis=(1, 3))  # [num_rx, num_tx, num_paths]
                a = np.sqrt(power).astype(complex)
        else:
            while a.ndim > 3:
                axes = tuple(i for i, s in enumerate(a.shape) if s == 1)
                if not axes:
                    warnings.append(f"unexpected path-coefficient shape {a.shape}")
                    return []
                a = a.squeeze(axis=axes[0])

        vertices = to_np(solved.vertices) if hasattr(solved, "vertices") else None
        valid = to_np(solved.valid) if hasattr(solved, "valid") else None
        objects = to_np(solved.objects) if hasattr(solved, "objects") else None
        itypes = to_np(solved.interactions) if hasattr(solved, "interactions") else None

        num_rx = min(tau.shape[0], len(rxs))
        num_tx = min(tau.shape[1], len(txs))
        max_paths = tau.shape[-1]

        paths: list[RayPath] = []
        counter = 0
        for r in range(num_rx):
            for t in range(num_tx):
                for p in range(max_paths):
                    if valid is not None and valid.ndim == 3 and not bool(valid[r, t, p]):
                        continue
                    tau_s = float(tau[r, t, p])
                    if tau_s < 0:
                        continue
                    amp = complex(a[r, t, p]) if a.ndim == 3 else 0j
                    mag = abs(amp)
                    if mag <= 0:
                        continue
                    # |a| is the free-space/interaction channel gain; add the
                    # transmit power to get received power in dBm.
                    power_dbm = 20.0 * math.log10(max(mag, 1e-30)) + txs[t].power_dbm

                    bounce, interactions = SionnaBackend._path_interactions(
                        r, t, p, vertices, objects, itypes,
                        objid_to_material, material_to_prims, np,
                    )
                    verts = [list(txs[t].position)] + bounce + [list(rxs[r].position)]
                    counter += 1
                    paths.append(
                        RayPath(
                            path_id=f"path_{counter:04d}",
                            tx_id=txs[t].id,
                            rx_id=rxs[r].id,
                            path_type=SionnaBackend._path_type(interactions),
                            vertices=verts,
                            power_dbm=power_dbm,
                            delay_ns=tau_s * 1e9,
                            phase_rad=math.atan2(amp.imag, amp.real),
                            interactions=interactions,
                        )
                    )
        return paths

    @staticmethod
    def _path_interactions(
        r, t, p, vertices, objects, itypes, objid_to_material, material_to_prims, np
    ) -> tuple[list[list[float]], list[PathInteraction]]:
        """Extract a path's bounce points and per-interaction prim/material."""
        bounce: list[list[float]] = []
        interactions: list[PathInteraction] = []
        if vertices is None or vertices.ndim != 5:
            return bounce, interactions
        for d in range(vertices.shape[0]):
            obj_id = int(objects[d, r, t, p]) if objects is not None else _NO_OBJECT
            if obj_id == _NO_OBJECT:
                continue  # unused depth slot: no interaction here
            v = vertices[d, r, t, p]
            if not np.all(np.isfinite(v)):
                continue
            point = [float(x) for x in v]
            bounce.append(point)
            code = int(itypes[d, r, t, p]) if itypes is not None else 1
            mat_id = objid_to_material.get(obj_id)
            prims = material_to_prims.get(mat_id, []) if mat_id else []
            interactions.append(
                PathInteraction(
                    # Only name a prim when the material group is a single prim;
                    # otherwise the merged geometry is genuinely ambiguous.
                    prim_id=prims[0] if len(prims) == 1 else None,
                    rf_material_id=mat_id,
                    type=_INTERACTION_TYPES.get(code, "reflection"),
                    point=point,
                )
            )
        return bounce, interactions

    @staticmethod
    def _path_type(interactions: list[PathInteraction]) -> str:
        if not interactions:
            return "los"
        kinds = {i.type for i in interactions}
        return next(iter(kinds)) if len(kinds) == 1 else "mixed"

    # --------------------------------------------------------- radio map

    def simulate_radio_map(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> RadioMapResultSet:
        try:
            return self._simulate_radio_map_impl(project_dir, scene, library, config)
        except Exception as exc:  # noqa: BLE001 - graceful degradation contract
            clear_scene_cache()
            txs = [d for d in scene.devices if d.kind == "tx"]
            return RadioMapResultSet(
                result_id=UNSAVED_RESULT_ID,
                backend=self.name,
                simulation_config_id=config.id,
                tx_id=txs[0].id if txs else "",
                metric=config.radio_map.metric,
                grid=RadioMapGrid(
                    origin=[0.0, 0.0, config.radio_map.height_m],
                    cell_size_m=config.radio_map.cell_size_m,
                    nx=1,
                    ny=1,
                    height_m=config.radio_map.height_m,
                ),
                values=[[None]],
                warnings=self._frequency_warnings(scene, library, config)
                + [f"sionna radio map failed: {exc}; see logs"],
                metadata={"frequency_hz": config.frequency_hz, "engine": "sionna"},
            )

    def _simulate_radio_map_impl(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> RadioMapResultSet:
        import numpy as np

        from sionna.rt import (  # type: ignore[import-not-found]
            RadioMapSolver,
            Transmitter,
        )

        warnings: list[str] = self._frequency_warnings(scene, library, config)
        xml_path = project_dir / "rf" / "generated_scene.xml"
        if not xml_path.is_file():
            compile_result = self.compile(project_dir, scene, library)
            if not compile_result.ok or not xml_path.is_file():
                raise RuntimeError(
                    "rf/generated_scene.xml missing and compile did not produce it: "
                    + "; ".join(compile_result.errors or ["unknown compile error"])
                )
            warnings.append("rf projection was missing; compiled on demand")

        txs = [d for d in scene.devices if d.kind == "tx"]
        if not txs:
            raise RuntimeError("scene has no transmitters; cannot compute a radio map")

        rxs = [d for d in scene.devices if d.kind == "rx"]
        rt_scene = _load_scene_cached(xml_path, warnings)
        _reset_scene_devices(rt_scene)
        rt_scene.frequency = config.frequency_hz
        # First TX/RX device antenna drives the arrays (matches simulate_paths).
        _apply_arrays(rt_scene, txs, rxs, warnings)
        self._apply_custom_materials(project_dir, rt_scene, warnings)
        for dev in txs:
            rt_scene.add(
                Transmitter(name=dev.id, position=list(dev.position), power_dbm=dev.power_dbm)
            )

        # Horizontal measurement plane sized to the scene geometry (padded),
        # at the configured height. Falls back to the tx extent if the mitsuba
        # bbox is unavailable.
        cell = float(config.radio_map.cell_size_m)
        height = float(config.radio_map.height_m)
        cx, cy, ext_x, ext_y = self._measurement_extent(rt_scene, txs, np)
        # Plain Python floats: mitsuba Point3f/Point2f reject numpy scalars.
        center = [float(cx), float(cy), float(height)]
        size = [float(max(ext_x, cell * 2)), float(max(ext_y, cell * 2))]

        solver = RadioMapSolver()
        # Full passthrough (RadioMapSolver has no synthetic_array kwarg; the
        # rest match PathSolver). Verified against sionna-rt 2.0.1.
        rm = solver(
            rt_scene,
            center=center,
            # Horizontal plane (Z-up): zero orientation. Required to be
            # non-None whenever center/size are given.
            orientation=[0.0, 0.0, 0.0],
            size=size,
            cell_size=[cell, cell],
            max_depth=config.max_depth,
            los=config.los,
            specular_reflection=config.reflection,
            diffuse_reflection=config.scattering,
            refraction=config.refraction,
            diffraction=config.diffraction,
            edge_diffraction=config.edge_diffraction,
            diffraction_lit_region=config.diffraction_lit_region,
            seed=config.seed,
            samples_per_tx=config.num_samples or 1_000_000,
        )

        metric = config.radio_map.metric
        raw = np.array(rm.rss if metric == "rss_dbm" else rm.path_gain)  # [num_tx, ny, nx]
        agg = raw.max(axis=0)  # combine transmitters by strongest coverage
        ny, nx = agg.shape
        with np.errstate(divide="ignore"):
            db = 10.0 * np.log10(np.where(agg > 0, agg, np.nan))
        if metric == "rss_dbm":
            db = db + 30.0  # Sionna rss is in Watts -> dBm
        values = [
            [None if not np.isfinite(db[j, i]) else float(db[j, i]) for i in range(nx)]
            for j in range(ny)
        ]

        centers = np.array(rm.cell_centers)  # [ny, nx, 3]
        origin = [
            float(centers[0, 0, 0] - cell / 2.0),
            float(centers[0, 0, 1] - cell / 2.0),
            height,
        ]
        return RadioMapResultSet(
            result_id=UNSAVED_RESULT_ID,
            backend=self.name,
            simulation_config_id=config.id,
            tx_id=txs[0].id,
            metric=metric,
            grid=RadioMapGrid(
                origin=origin, cell_size_m=cell, nx=nx, ny=ny, height_m=height
            ),
            values=values,
            warnings=warnings + (["multiple tx aggregated by max"] if len(txs) > 1 else []),
            metadata={
                "frequency_hz": config.frequency_hz,
                "num_tx": len(txs),
                "engine": "sionna",
            },
        )

    @staticmethod
    def _measurement_extent(rt_scene, txs, np) -> tuple[float, float, float, float]:
        """(center_x, center_y, size_x, size_y) covering the scene.

        Padding adapts to scene size: a 7 m lab room gets ~3 m of margin
        instead of the 15 m appropriate for a campus, so indoor radio maps
        don't waste most of their cells outside the room."""
        try:
            bbox = rt_scene.mi_scene.bbox()
            lo, hi = np.array(bbox.min), np.array(bbox.max)
            ext = max(hi[0] - lo[0], hi[1] - lo[1])
            pad = min(15.0, max(3.0, 0.15 * float(ext)))
            cx, cy = (lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0
            return cx, cy, (hi[0] - lo[0]) + 2 * pad, (hi[1] - lo[1]) + 2 * pad
        except Exception:  # noqa: BLE001 - fall back to transmitter extent
            xs = [d.position[0] for d in txs]
            ys = [d.position[1] for d in txs]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            return cx, cy, 60.0, 60.0
