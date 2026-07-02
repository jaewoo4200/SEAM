"""Optional Sionna RT backend.

Contract (HANDOFF.md sections 7.2, 14): Sionna is imported lazily inside
methods, availability is probed without heavy imports, and ANY failure -
missing install, version API drift, solver errors, incompatible scene -
degrades to an empty result set with a warning instead of a 500. The app must
never break because Sionna is absent or its API moved.
"""

import json
import math
from pathlib import Path

from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import PathResultSet, RadioMapGrid, RadioMapResultSet, RayPath
from app.schemas.scene import Scene
from app.schemas.simulation import SimulationConfig

from .base import UNSAVED_RESULT_ID, RayTracingBackend

INTERACTION_WARNING = "sionna interaction->prim mapping not implemented (future)"


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

        warnings: list[str] = [INTERACTION_WARNING]

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

        solver = PathSolver()
        # Sionna 1.x PathSolver signature; refraction disabled because our
        # compiled projection does not model interior volumes yet.
        solved = solver(
            rt_scene,
            max_depth=config.max_depth,
            los=config.los,
            specular_reflection=config.reflection,
            diffuse_reflection=config.scattering,
            refraction=False,
            samples_per_src=config.num_samples or 100_000,
        )

        paths = self._convert_paths(solved, txs, rxs, config, warnings, np)
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

        # Manifest layout is owned by the compiler module; accept both a
        # top-level "materials" list and a mapping keyed by material id.
        raw = manifest.get("materials", [])
        entries = list(raw.values()) if isinstance(raw, dict) else raw
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            if entry.get("model") != "constant":
                continue
            mat_id = entry.get("id") or entry.get("rf_material_id")
            eps = entry.get("relative_permittivity")
            sigma = entry.get("conductivity_s_per_m")
            if not mat_id or eps is None:
                continue
            try:
                # Sionna 1.x: scene.radio_materials is a dict name->RadioMaterial
                # with settable relative_permittivity / conductivity.
                rt_mat = rt_scene.radio_materials.get(mat_id)
                if rt_mat is None:
                    continue
                rt_mat.relative_permittivity = float(eps)
                if sigma is not None:
                    rt_mat.conductivity = float(sigma)
            except Exception as exc:  # noqa: BLE001 - per-material best effort
                warnings.append(f"could not apply material {mat_id!r}: {exc}")

    @staticmethod
    def _convert_paths(
        solved, txs, rxs, config: SimulationConfig, warnings: list[str], np
    ) -> list[RayPath]:
        """Normalize a Sionna Paths object into schema RayPath entries.

        Sionna 1.x shape assumptions (synthetic 1x1 arrays):
        - solved.vertices: [max_depth, num_rx, num_tx, max_paths, 3]
        - solved.tau:      [num_rx, num_tx, max_paths] (antenna dims squeezed)
        - solved.a:        complex, [num_rx, .., num_tx, .., max_paths] or a
                           (real, imag) tuple in some point releases
        - solved.valid:    bool mask [num_rx, num_tx, max_paths]
        Invalid entries carry tau < 0. Anything that does not fit is skipped
        with a warning rather than raised.
        """
        def to_np(x):
            return np.asarray(x.numpy() if hasattr(x, "numpy") else x)

        tau = to_np(solved.tau)
        a = solved.a
        if isinstance(a, (tuple, list)) and len(a) == 2:
            a = to_np(a[0]) + 1j * to_np(a[1])
        else:
            a = to_np(a)
        vertices = to_np(solved.vertices) if hasattr(solved, "vertices") else None
        valid = to_np(solved.valid) if hasattr(solved, "valid") else None

        # Squeeze any singleton antenna/pol dims so indexing is [rx, tx, path].
        def squeeze_to_3d(arr):
            while arr.ndim > 3:
                singles = [i for i, s in enumerate(arr.shape) if s == 1]
                if not singles:
                    raise ValueError(f"unexpected solver output shape {arr.shape}")
                arr = arr.squeeze(axis=singles[0])
            return arr

        tau = squeeze_to_3d(tau)
        a = squeeze_to_3d(a)

        paths: list[RayPath] = []
        counter = 0
        num_rx, num_tx, max_paths = tau.shape[0], tau.shape[1], tau.shape[-1]
        for r in range(min(num_rx, len(rxs))):
            for t in range(min(num_tx, len(txs))):
                for p in range(max_paths):
                    tau_s = float(tau[r, t, p])
                    if tau_s < 0:
                        continue  # Sionna marks invalid paths with tau=-1
                    if valid is not None and valid.ndim == 3 and not bool(valid[r, t, p]):
                        continue
                    amp = complex(a[r, t, p])
                    mag = abs(amp)
                    if mag <= 0:
                        continue
                    power_dbm = 20.0 * math.log10(max(mag, 1e-30)) + txs[t].power_dbm

                    # Bounce points: depth-major vertex tensor; unused depth
                    # slots are zero/invalid, so keep only finite non-zero rows.
                    bounce: list[list[float]] = []
                    if vertices is not None and vertices.ndim == 5:
                        for d in range(vertices.shape[0]):
                            v = vertices[d, r, t, p]
                            if np.all(np.isfinite(v)) and not np.allclose(v, 0.0):
                                bounce.append([float(x) for x in v])
                    verts = (
                        [list(txs[t].position)] + bounce + [list(rxs[r].position)]
                    )
                    counter += 1
                    paths.append(
                        RayPath(
                            path_id=f"path_{counter:04d}",
                            tx_id=txs[t].id,
                            rx_id=rxs[r].id,
                            path_type="los" if not bounce else "reflection",
                            vertices=verts,
                            power_dbm=power_dbm,
                            delay_ns=tau_s * 1e9,
                            phase_rad=math.atan2(amp.imag, amp.real),
                            # Prim mapping needs the compiler's shape->prim
                            # index; deferred (see INTERACTION_WARNING).
                            interactions=[],
                        )
                    )
        return paths

    # --------------------------------------------------------- radio map

    def simulate_radio_map(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> RadioMapResultSet:
        # MVP: Sionna's RadioMapSolver integration is deferred; return an
        # empty grid with a warning so callers keep the same result shape.
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
            warnings=["sionna radio map not implemented; use mock"],
            metadata={"frequency_hz": config.frequency_hz, "engine": "sionna"},
        )
