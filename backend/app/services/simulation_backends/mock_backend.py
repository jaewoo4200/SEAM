"""Deterministic mock ray-tracing backend.

Always available: no Sionna, no GPU, no RNG, no wall clock inside payloads.
Outputs are physics-flavored (Friis free-space loss, image-method ground
bounce) but deliberately fake - they exist so the frontend, result explorer,
and tests work on any machine. Running the same scene/config twice yields
identical results.
"""

import math
from pathlib import Path
from typing import Optional

from app.schemas.devices import Device
from app.schemas.materials import RFMaterialLibrary
from app.schemas.results import (
    BeamformingResult,
    PathInteraction,
    PathResultSet,
    RadioMapGrid,
    RadioMapResultSet,
    RayPath,
)
from app.schemas.scene import Prim, Scene
from app.schemas.simulation import BeamformingRequest, SimulationConfig

from .base import UNSAVED_RESULT_ID, RayTracingBackend

SPEED_OF_LIGHT = 299_792_458.0
ENGINE = "mock-deterministic-v2"
# Consumer-level guardrail: never build a radio map bigger than this.
MAX_RADIO_MAP_CELLS = 40_000
GROUND_REFLECTION_LOSS_DB = 10.0
WALL_REFLECTION_LOSS_DB = 18.0


def friis_dbm(p_tx_dbm: float, freq_hz: float, dist_m: float) -> float:
    """Friis free-space received power; FSPL = 20log10(d) + 20log10(f) - 147.55."""
    d = max(dist_m, 0.1)
    return p_tx_dbm - (20.0 * math.log10(d) + 20.0 * math.log10(freq_hz) - 147.55)


def reflection_loss_db(material, base_db: float) -> float:
    """Deterministic, physics-flavored reflection loss for a bounce.

    Depends monotonically on the material's parameters so material edits (and
    calibration grid sweeps) visibly change mock results:
    - diffuse scattering drains the specular bounce: +10*S dB;
    - constant-model materials add a normal-incidence Fresnel term,
      -10*log10(R) with R = ((sqrt(eps)-1)/(sqrt(eps)+1))^2.
    ITU frequency-dependent materials contribute only the scattering term
    (their eps/sigma live inside Sionna, not in the library entry).
    """
    loss = base_db
    if material is None:
        return loss
    loss += 10.0 * float(material.scattering_coefficient or 0.0)
    eps = material.relative_permittivity
    if material.model == "constant" and eps and eps > 1.0:
        sq = math.sqrt(eps)
        reflectance = ((sq - 1.0) / (sq + 1.0)) ** 2
        loss += -10.0 * math.log10(max(reflectance, 1e-6))
    return loss


def _dist(a: list[float], b: list[float]) -> float:
    return math.sqrt(sum((ai - bi) ** 2 for ai, bi in zip(a, b)))


def _delay_ns(dist_m: float) -> float:
    return dist_m / SPEED_OF_LIGHT * 1e9


def _phase_rad(dist_m: float, freq_hz: float) -> float:
    # Electrical length modulo 2*pi: deterministic, no RNG.
    return (2.0 * math.pi * dist_m * freq_hz / SPEED_OF_LIGHT) % (2.0 * math.pi)


def _select_devices(
    scene: Scene, kind: str, ids: Optional[list[str]]
) -> list[Device]:
    return [
        d
        for d in scene.devices
        if d.kind == kind and (ids is None or d.id in ids)
    ]


def _find_ground_prim(scene: Scene, library: RFMaterialLibrary) -> Optional[Prim]:
    """First mesh prim whose tags or RF material category mention ground/asphalt."""
    for prim in scene.prims:
        if prim.type != "mesh_primitive":
            continue
        if any("ground" in t or "asphalt" in t for t in prim.semantic_tags):
            return prim
        if prim.rf.material_id:
            mat = library.get(prim.rf.material_id)
            if mat and ("ground" in mat.category or "asphalt" in mat.category):
                return prim
    return None


def _find_wall_prim(scene: Scene) -> Optional[Prim]:
    """Prefer an RF-assigned mesh prim tagged building/wall; groups carry no
    geometry and make poor bounce anchors, so they are never picked."""
    fallback: Optional[Prim] = None
    for prim in scene.prims:
        if prim.type != "mesh_primitive":
            continue
        if not any(t in ("building", "wall") for t in prim.semantic_tags):
            continue
        if prim.rf.material_id is not None:
            return prim
        if fallback is None:
            fallback = prim
    return fallback


class MockBackend(RayTracingBackend):
    name = "mock"

    def is_available(self) -> bool:
        return True

    # ------------------------------------------------------------- paths

    def simulate_paths(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> PathResultSet:
        txs = _select_devices(scene, "tx", config.tx_ids)
        rxs = _select_devices(scene, "rx", config.rx_ids)
        ground_prim = _find_ground_prim(scene, library)
        wall_prim = _find_wall_prim(scene)
        wall_anchor_xy = (
            self._wall_anchor_xy(project_dir, wall_prim) if wall_prim else None
        )
        reflections_on = config.reflection and config.max_depth >= 1

        paths: list[RayPath] = []
        warnings: list[str] = []
        if not txs or not rxs:
            warnings.append(
                "scene has no matching tx/rx devices; no paths computed"
            )

        counter = 0

        def next_id() -> str:
            nonlocal counter
            counter += 1
            return f"path_{counter:04d}"

        for tx in txs:
            for rx in rxs:
                if config.los:
                    paths.append(self._los_path(next_id(), tx, rx, config))
                if reflections_on:
                    ground = self._ground_bounce_path(
                        next_id, tx, rx, config, ground_prim, library
                    )
                    if ground is not None:
                        paths.append(ground)
                    if wall_prim is not None:
                        paths.append(
                            self._wall_bounce_path(
                                next_id(), tx, rx, config, wall_prim, wall_anchor_xy, library
                            )
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
                "engine": ENGINE,
            },
        )

    def _los_path(
        self, path_id: str, tx: Device, rx: Device, config: SimulationConfig
    ) -> RayPath:
        dist = _dist(tx.position, rx.position)
        return RayPath(
            path_id=path_id,
            tx_id=tx.id,
            rx_id=rx.id,
            path_type="los",
            vertices=[list(tx.position), list(rx.position)],
            power_dbm=friis_dbm(tx.power_dbm, config.frequency_hz, dist),
            delay_ns=_delay_ns(dist),
            phase_rad=_phase_rad(dist, config.frequency_hz),
            interactions=[],
        )

    def _ground_bounce_path(
        self,
        next_id,
        tx: Device,
        rx: Device,
        config: SimulationConfig,
        ground_prim: Optional[Prim],
        library: Optional[RFMaterialLibrary] = None,
    ) -> Optional[RayPath]:
        # Image method across the z=0 plane: reflect tx to (x, y, -z); the
        # straight line image->rx crosses z=0 at the specular point.
        tz, rz = tx.position[2], rx.position[2]
        if tz < 0.0 or rz < 0.0 or (tz + rz) <= 1e-9:
            return None  # degenerate: at/below ground, no specular point
        t = tz / (tz + rz)
        point = [
            tx.position[0] + t * (rx.position[0] - tx.position[0]),
            tx.position[1] + t * (rx.position[1] - tx.position[1]),
            0.0,
        ]
        total = _dist(tx.position, point) + _dist(point, rx.position)
        interaction = PathInteraction(
            type="reflection",
            prim_id=ground_prim.id if ground_prim else None,
            rf_material_id=ground_prim.rf.material_id if ground_prim else None,
            point=point,
        )
        ground_mat = (
            library.get(ground_prim.rf.material_id)
            if library and ground_prim and ground_prim.rf.material_id
            else None
        )
        return RayPath(
            path_id=next_id(),
            tx_id=tx.id,
            rx_id=rx.id,
            path_type="reflection",
            vertices=[list(tx.position), point, list(rx.position)],
            power_dbm=friis_dbm(tx.power_dbm, config.frequency_hz, total)
            - reflection_loss_db(ground_mat, GROUND_REFLECTION_LOSS_DB),
            delay_ns=_delay_ns(total),
            phase_rad=_phase_rad(total, config.frequency_hz),
            interactions=[interaction],
        )

    def _wall_anchor_xy(
        self, project_dir: Path, prim: Prim
    ) -> Optional[list[float]]:
        """Horizontal anchor for the fake wall bounce.

        Scenes with baked geometry keep prim transforms at identity, so the
        translation is only trusted when non-zero; otherwise fall back to the
        mesh bbox center when the visual asset is loadable.
        """
        t = prim.transform.translation
        if abs(t[0]) > 1e-9 or abs(t[1]) > 1e-9:
            return [t[0], t[1]]
        if prim.mesh_ref is not None:
            try:
                from app.services import mesh_tools

                tm_scene = mesh_tools.load_visual_scene(
                    project_dir, prim.mesh_ref.asset_uri
                )
                if tm_scene is not None:
                    mesh = mesh_tools.extract_prim_mesh(tm_scene, prim.mesh_ref)
                    if mesh is not None:
                        center = mesh.bounds.mean(axis=0)
                        return [float(center[0]), float(center[1])]
            except Exception:
                pass  # anchor is cosmetic; never fail a mock simulation over it
        return None

    def _wall_bounce_path(
        self,
        path_id: str,
        tx: Device,
        rx: Device,
        config: SimulationConfig,
        wall_prim: Prim,
        anchor_xy: Optional[list[float]],
        library: Optional[RFMaterialLibrary] = None,
    ) -> RayPath:
        if anchor_xy is None:
            # Deterministic last resort: offset perpendicular to the tx-rx
            # segment at its midpoint so the polyline still looks like a bounce.
            dx = rx.position[0] - tx.position[0]
            dy = rx.position[1] - tx.position[1]
            norm = math.hypot(dx, dy) or 1.0
            anchor_xy = [
                (tx.position[0] + rx.position[0]) / 2.0 - dy / norm * 8.0,
                (tx.position[1] + rx.position[1]) / 2.0 + dx / norm * 8.0,
            ]
        bounce = [anchor_xy[0], anchor_xy[1], (tx.position[2] + rx.position[2]) / 2.0]
        total = _dist(tx.position, bounce) + _dist(bounce, rx.position)
        interaction = PathInteraction(
            type="reflection",
            prim_id=wall_prim.id,
            rf_material_id=wall_prim.rf.material_id,
            point=bounce,
        )
        wall_mat = (
            library.get(wall_prim.rf.material_id)
            if library and wall_prim.rf.material_id
            else None
        )
        return RayPath(
            path_id=path_id,
            tx_id=tx.id,
            rx_id=rx.id,
            path_type="reflection",
            vertices=[list(tx.position), bounce, list(rx.position)],
            power_dbm=friis_dbm(tx.power_dbm, config.frequency_hz, total)
            - reflection_loss_db(wall_mat, WALL_REFLECTION_LOSS_DB),
            delay_ns=_delay_ns(total),
            phase_rad=_phase_rad(total, config.frequency_hz),
            interactions=[interaction],
        )

    # --------------------------------------------------------- radio map

    def simulate_radio_map(
        self,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
    ) -> RadioMapResultSet:
        txs = _select_devices(scene, "tx", config.tx_ids)
        warnings: list[str] = []

        # Grid extent: union bbox of all device positions and prim anchors,
        # padded 20 m so coverage extends beyond the built geometry.
        points = [d.position for d in scene.devices]
        points += [p.transform.translation for p in scene.prims]
        if not points:
            points = [[0.0, 0.0, 0.0]]
        pad = 20.0
        xmin = min(p[0] for p in points) - pad
        xmax = max(p[0] for p in points) + pad
        ymin = min(p[1] for p in points) - pad
        ymax = max(p[1] for p in points) + pad

        cell = config.radio_map.cell_size_m
        height = config.radio_map.height_m
        nx = max(1, math.ceil((xmax - xmin) / cell))
        ny = max(1, math.ceil((ymax - ymin) / cell))
        if nx * ny > MAX_RADIO_MAP_CELLS:
            # Coarsen instead of exploding memory on consumer machines.
            requested = cell
            while nx * ny > MAX_RADIO_MAP_CELLS:
                cell *= math.sqrt(nx * ny / MAX_RADIO_MAP_CELLS) * 1.001
                nx = max(1, math.ceil((xmax - xmin) / cell))
                ny = max(1, math.ceil((ymax - ymin) / cell))
            warnings.append(
                f"radio map capped at {MAX_RADIO_MAP_CELLS} cells: cell size "
                f"coarsened from {requested} m to {cell:.3f} m"
            )

        grid = RadioMapGrid(
            origin=[xmin, ymin, height],
            cell_size_m=cell,
            nx=nx,
            ny=ny,
            height_m=height,
        )
        metric = config.radio_map.metric

        if not txs:
            warnings.append("no transmitters in scene; radio map is empty")
            return RadioMapResultSet(
                result_id=UNSAVED_RESULT_ID,
                backend=self.name,
                simulation_config_id=config.id,
                tx_id="",
                metric=metric,
                grid=grid,
                values=[[None] * nx for _ in range(ny)],
                warnings=warnings,
                metadata={"frequency_hz": config.frequency_hz, "engine": ENGINE},
            )

        values: list[list[Optional[float]]] = []
        for j in range(ny):
            row: list[Optional[float]] = []
            cy = ymin + (j + 0.5) * cell
            for i in range(nx):
                cx = xmin + (i + 0.5) * cell
                best: Optional[float] = None
                for tx in txs:
                    d = _dist([cx, cy, height], tx.position)
                    v = friis_dbm(tx.power_dbm, config.frequency_hz, d)
                    if metric == "path_gain_db":
                        v -= tx.power_dbm
                    if best is None or v > best:
                        best = v
                # Deterministic ripple stands in for multipath fading.
                row.append(best + 6.0 * math.sin(0.35 * i) * math.cos(0.35 * j))
            values.append(row)

        metadata = {
            "frequency_hz": config.frequency_hz,
            "num_tx": len(txs),
            "engine": ENGINE,
        }
        if len(txs) > 1:
            metadata["multi_tx"] = "cell values are the max over all transmitters"

        return RadioMapResultSet(
            result_id=UNSAVED_RESULT_ID,
            backend=self.name,
            simulation_config_id=config.id,
            tx_id=txs[0].id,
            metric=metric,
            grid=grid,
            values=values,
            warnings=warnings,
            metadata=metadata,
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
        """Analytic array-gain stub (deterministic). Real per-antenna channel
        beamforming requires the Sionna backend; this gives the frontend/tests
        a plausible readout without a solver."""
        txs = _select_devices(scene, "tx", [request.tx_id] if request.tx_id else None)
        rxs = _select_devices(scene, "rx", [request.rx_id] if request.rx_id else None)
        n_tx = request.tx_rows * request.tx_cols
        n_rx = request.rx_rows * request.rx_cols
        result = BeamformingResult(
            backend=self.name,
            simulation_config_id=config.id,
            tx_id=txs[0].id if txs else "",
            rx_id=rxs[0].id if rxs else "",
            frequency_hz=config.frequency_hz,
            tx_array=[request.tx_rows, request.tx_cols],
            rx_array=[request.rx_rows, request.rx_cols],
            warnings=["mock beamforming is an analytic array-gain stub; "
                      "use the sionna backend for channel-based MRT/SVD"],
            metadata={"engine": ENGINE},
        )
        if not txs or not rxs:
            result.warnings.append("scene has no matching tx/rx device")
            return result
        dist = _dist(txs[0].position, rxs[0].position)
        result.single_element_dbm = friis_dbm(txs[0].power_dbm, config.frequency_hz, dist)
        # Idealized array gains: TX-MRT ~ 10log10(N_tx); both-ends adds RX gain.
        result.tx_mrt_gain_db = 10.0 * math.log10(n_tx)
        result.svd_gain_db = 10.0 * math.log10(n_tx) + 10.0 * math.log10(n_rx)
        result.num_paths = 1  # analytic LoS only
        return result
