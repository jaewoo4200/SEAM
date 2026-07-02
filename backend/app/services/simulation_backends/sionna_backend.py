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

# Sionna RT interaction-type codes -> our schema interaction type. Code 0 is
# "none"; the rest are mapped defensively (unknown codes fall back to
# reflection) so a version bump cannot crash conversion.
_INTERACTION_TYPES = {1: "reflection", 2: "scattering", 3: "transmission", 4: "diffraction"}


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
    ) -> PathResultSet:
        try:
            return self._simulate_paths_impl(project_dir, scene, library, config)
        except Exception as exc:  # noqa: BLE001 - graceful degradation contract
            return PathResultSet(
                result_id=UNSAVED_RESULT_ID,
                backend=self.name,
                simulation_config_id=config.id,
                paths=[],
                warnings=[f"sionna backend failed: {exc}; see logs"],
                metadata={"frequency_hz": config.frequency_hz, "engine": "sionna"},
            )

    def _simulate_paths_impl(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> PathResultSet:
        import numpy as np

        # Sionna 1.x exposes RT as the standalone sionna-rt package under
        # sionna.rt with these names; 0.x had solver methods on the scene
        # object instead of PathSolver. We target 1.x and let the outer
        # try/except absorb anything older/newer.
        from sionna.rt import (  # type: ignore[import-not-found]
            PathSolver,
            PlanarArray,
            Receiver,
            Transmitter,
            load_scene,
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

        rt_scene = load_scene(str(xml_path))
        rt_scene.frequency = config.frequency_hz

        # Single isotropic antenna per device keeps solver output shapes
        # small and predictable across Sionna versions (synthetic array).
        array = PlanarArray(
            num_rows=1,
            num_cols=1,
            vertical_spacing=0.5,
            horizontal_spacing=0.5,
            pattern="iso",
            polarization="V",
        )
        rt_scene.tx_array = array
        rt_scene.rx_array = array

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
        # refraction disabled: our compiled projection is single-sided surface
        # geometry, not closed interior volumes. diffraction follows the config.
        solved = solver(
            rt_scene,
            max_depth=config.max_depth,
            los=config.los,
            specular_reflection=config.reflection,
            diffuse_reflection=config.scattering,
            diffraction=config.diffraction,
            refraction=False,
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
            base.warnings.append(f"sionna beamforming failed: {exc}; see logs")
            return base

    def _beamforming_impl(
        self, project_dir, scene, library, config, request, tx, rx, base
    ) -> BeamformingResult:
        import math

        import numpy as np
        from sionna.rt import (  # type: ignore[import-not-found]
            PathSolver,
            PlanarArray,
            Receiver,
            Transmitter,
            load_scene,
        )

        base.warnings.extend(self._frequency_warnings(scene, library, config))
        xml_path = project_dir / "rf" / "generated_scene.xml"
        if not xml_path.is_file():
            self.compile(project_dir, scene, library)
        rt_scene = load_scene(str(xml_path))
        rt_scene.frequency = config.frequency_hz
        rt_scene.tx_array = PlanarArray(
            num_rows=request.tx_rows, num_cols=request.tx_cols, pattern="iso", polarization="V"
        )
        rt_scene.rx_array = PlanarArray(
            num_rows=request.rx_rows, num_cols=request.rx_cols, pattern="iso", polarization="V"
        )
        self._apply_custom_materials(project_dir, rt_scene, base.warnings)
        rt_scene.add(Transmitter(name=tx.id, position=list(tx.position), power_dbm=tx.power_dbm))
        rt_scene.add(Receiver(name=rx.id, position=list(rx.position)))

        paths = PathSolver()(
            rt_scene, max_depth=config.max_depth, los=config.los,
            specular_reflection=config.reflection, diffuse_reflection=config.scattering,
            refraction=False, synthetic_array=True, samples_per_src=config.num_samples or 1_000_000,
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
        # Drop the singleton antenna axes (1 and 3) so a aligns with tau.
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
                warnings=[f"sionna radio map failed: {exc}; see logs"],
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
            PlanarArray,
            RadioMapSolver,
            Transmitter,
            load_scene,
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

        rt_scene = load_scene(str(xml_path))
        rt_scene.frequency = config.frequency_hz
        array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization="V")
        rt_scene.tx_array = array
        rt_scene.rx_array = array
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
            refraction=False,
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
        """(center_x, center_y, size_x, size_y) covering the scene, padded 15 m."""
        pad = 15.0
        try:
            bbox = rt_scene.mi_scene.bbox()
            lo, hi = np.array(bbox.min), np.array(bbox.max)
            cx, cy = (lo[0] + hi[0]) / 2.0, (lo[1] + hi[1]) / 2.0
            return cx, cy, (hi[0] - lo[0]) + 2 * pad, (hi[1] - lo[1]) + 2 * pad
        except Exception:  # noqa: BLE001 - fall back to transmitter extent
            xs = [d.position[0] for d in txs]
            ys = [d.position[1] for d in txs]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            return cx, cy, 60.0, 60.0
