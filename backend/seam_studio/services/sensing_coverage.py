"""Sensing coverage map (POST /projects/{id}/simulate/sensing-coverage): where
a virtual point target at one height would be detected, by how many links,
and where multistatic fusion has enough distinct geometries.

For every grid cell and every TX x sensing-RX link (monostatic and bistatic)
the echo SNR is the bistatic radar equation

    SNR = P_tx + G + G_e,tx + G_e,rx + 10log10(lambda^2 sigma / ((4 pi)^3 R_t^2 R_r^2))
          - A(R_t + R_r) - N0 + 10log10(cpi_pulses)

when both legs are line-of-sight, and no echo otherwise. G_e is each
device's element gain toward the cell in its local frame (``element_gain_db``,
the sionna-rt pattern closed forms; 0 dB for iso). The LOS test is the
one backend-specific step (``RayTracingBackend.segment_los``: a Mitsuba
shadow-ray test on sionna, none on the mock); grid, radar equation and fusion
count are backend-neutral, so mock and sionna maps share their grid.
"""

import math
import time
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import (
    PdMcSpotCheck,
    RadioMapGrid,
    SensingCoverageLink,
    SensingCoverageResultSet,
    SensingCoverageSummary,
)
from seam_studio.schemas.scene import Scene, SceneBounds
from seam_studio.schemas.sensing import DetectorOptions, SensingCoverageRequest, metadata_dump
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import atmosphere
from seam_studio.services.channel_npz_export import local_frame_matrix
from seam_studio.services.detector import (
    NoiseReference,
    detector_metadata,
    monte_carlo_pd,
    noise_reference,
    noise_seed,
    pd_swerling,
)
from seam_studio.services.isac import ISACPlan, plan_isac_roles
from seam_studio.services.scene_bounds import compute_scene_bounds
from seam_studio.services.sensing import (
    bistatic_radar_gain_db,
    dbsm_to_m2,
    geometry_groups,
)
from seam_studio.services.simulation_backends.base import (
    UNSAVED_RESULT_ID,
    RayTracingBackend,
)
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm

SPEED_OF_LIGHT = 299_792_458.0
MAX_COVERAGE_CELLS = 40_000
OCCLUSION_LOS_MODEL = "mitsuba ray_test (static scene, actor meshes hidden)"

GridSpec = tuple[RadioMapGrid, list[str]]


class SensingCoverageError(ValueError):
    """The coverage grid cannot be sized (API -> 400)."""


def coverage_grid(
    request: SensingCoverageRequest, bounds: Optional[SceneBounds]
) -> GridSpec:
    """Grid of the request: its explicit extent, else the scene bounds padded
    like the sionna radio map (min(15, max(3, 15 % of the larger side))).
    More than MAX_COVERAGE_CELLS cells coarsens the cell size."""
    cell = float(request.cell_size_m)
    if request.center_xy is not None and request.size_xy is not None:
        width, depth = float(request.size_xy[0]), float(request.size_xy[1])
        x0 = float(request.center_xy[0]) - width / 2.0
        y0 = float(request.center_xy[1]) - depth / 2.0
    else:
        if bounds is None:
            raise SensingCoverageError(
                "scene has no geometry or devices to size the coverage grid"
            )
        ext_x = bounds.max[0] - bounds.min[0]
        ext_y = bounds.max[1] - bounds.min[1]
        pad = min(15.0, max(3.0, 0.15 * max(ext_x, ext_y)))
        x0, y0 = bounds.min[0] - pad, bounds.min[1] - pad
        width, depth = ext_x + 2.0 * pad, ext_y + 2.0 * pad
    warnings: list[str] = []
    try:
        nx = max(1, math.ceil(width / cell))
        ny = max(1, math.ceil(depth / cell))
        if nx * ny > MAX_COVERAGE_CELLS:
            requested = cell
            while nx * ny > MAX_COVERAGE_CELLS:
                cell *= math.sqrt(nx * ny / MAX_COVERAGE_CELLS) * 1.001
                nx = max(1, math.ceil(width / cell))
                ny = max(1, math.ceil(depth / cell))
            warnings.append(
                f"coverage grid capped at {MAX_COVERAGE_CELLS} cells: cell size "
                f"coarsened from {requested:g} m to {cell:.3f} m"
            )
    except (OverflowError, ValueError) as exc:
        # A subnormal cell size: width / cell overflows.
        raise SensingCoverageError(
            f"coverage grid cannot be sized ({width:g} x {depth:g} m at "
            f"{float(request.cell_size_m):g} m cells): {exc}"
        ) from exc
    grid = RadioMapGrid(
        origin=[x0, y0, float(request.height_m)],
        cell_size_m=cell,
        nx=nx,
        ny=ny,
        height_m=float(request.height_m),
    )
    return grid, warnings


def cell_centers(grid: RadioMapGrid) -> np.ndarray:
    """[ny * nx, 3] cell centers, row-major (index j * nx + i)."""
    xs = grid.origin[0] + (np.arange(grid.nx) + 0.5) * grid.cell_size_m
    ys = grid.origin[1] + (np.arange(grid.ny) + 0.5) * grid.cell_size_m
    x, y = np.meshgrid(xs, ys)
    return np.stack([x.ravel(), y.ravel(), np.full(x.size, grid.height_m)], axis=1)


def mc_spot_cells(best_snr_db: np.ndarray, has_echo: np.ndarray) -> list[int]:
    """Up to 5 distinct cell indices at the 0/25/50/75/100 % quantiles
    (method "nearest") of best_snr_db over the cells with an echo; a value
    shared by several cells goes to the lowest index not taken yet."""
    echo_idx = np.flatnonzero(has_echo)
    if echo_idx.size == 0:
        return []
    values = best_snr_db[echo_idx]
    chosen: list[int] = []
    for q in (0.0, 0.25, 0.5, 0.75, 1.0):
        v = np.quantile(values, q, method="nearest")
        for cell in echo_idx[values == v]:
            if int(cell) not in chosen:
                chosen.append(int(cell))
                break
    return chosen


def _mc_spot_check(
    best: np.ndarray,
    has_echo: np.ndarray,
    pd_best: np.ndarray,
    grid: RadioMapGrid,
    request: SensingCoverageRequest,
    ref: NoiseReference,
) -> list[PdMcSpotCheck]:
    det = request.detector
    out: list[PdMcSpotCheck] = []
    for cell in mc_spot_cells(best, has_echo):
        mc = monte_carlo_pd(
            float(best[cell]), request.pfa, det.monte_carlo_trials, det.model,
            seed=[det.seed, cell], cpi_pulses=request.cpi_pulses,
            empirical_threshold=det.empirical_threshold,
            threshold=ref.threshold, measure_pfa=False,
        )
        out.append(
            PdMcSpotCheck(
                cell=[cell % grid.nx, cell // grid.nx],
                snr_db=float(best[cell]),
                pd=float(pd_best[cell]),
                pd_mc=mc.pd,
                ci_low=mc.ci_low,
                ci_high=mc.ci_high,
            )
        )
    return out


def element_gain_db(device: Device, directions: np.ndarray) -> np.ndarray:
    """[n] power gain [dBi] of the device's antenna element toward the world
    directions [n, 3] (any length), in its local frame (R(orientation)^T k:
    zenith theta from local z, azimuth phi from local x). Closed forms of
    sionna-rt's v_*_pattern; a single-port polarization only rotates the
    field, so |C|^2 is the gain. iso and unknown names (the sionna backend's
    iso fallback) are 0 dB."""
    pattern = device.antenna.pattern
    n = len(directions)
    if pattern not in ("dipole", "hw_dipole", "tr38901"):
        return np.zeros(n)
    rot = np.asarray(local_frame_matrix(device.orientation_deg))
    local = np.asarray(directions, dtype=float) @ rot  # rows: R^T k
    norm = np.linalg.norm(local, axis=1)
    cos_theta = np.divide(local[:, 2], norm, out=np.zeros(n), where=norm > 0.0)
    theta = np.arccos(np.clip(cos_theta, -1.0, 1.0))
    if pattern == "tr38901":
        # TR 38.901 Table 7.3-1: 65 deg 3 dB beamwidths, 30 dB floor, 8 dBi.
        phi = np.arctan2(local[:, 1], local[:, 0])
        hpbw = math.radians(65.0)
        a_v = -np.minimum(12.0 * ((theta - math.pi / 2.0) / hpbw) ** 2, 30.0)
        a_h = -np.minimum(12.0 * (phi / hpbw) ** 2, 30.0)
        return -np.minimum(-(a_v + a_h), 30.0) + 8.0
    sin_theta = np.sin(theta)
    if pattern == "dipole":
        gain = 1.5 * sin_theta**2
    else:
        inv = np.divide(
            1.0, sin_theta, out=np.ones(n), where=~np.isclose(sin_theta, 0.0, atol=1e-5)
        )
        gain = 1.643 * (np.cos(math.pi / 2.0 * np.cos(theta)) * inv) ** 2
    return 10.0 * np.log10(np.maximum(gain, 1e-30))


def _rows(
    values: np.ndarray, grid: RadioMapGrid, *, none_below: bool
) -> list[list[Optional[float]]]:
    """[ny][nx] lists; ``none_below`` turns -inf (no echo) into None."""
    return [
        [None if none_below and not np.isfinite(v) else float(v) for v in row]
        for row in values.reshape(grid.ny, grid.nx)
    ]


def run_sensing_coverage(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: SensingCoverageRequest,
    *,
    plan: Optional[ISACPlan] = None,
    grid: Optional[GridSpec] = None,
    tick: Optional[Callable[[int, int], None]] = None,
    extra_warnings: tuple[str, ...] | list[str] = (),
) -> SensingCoverageResultSet:
    """Coverage map of a virtual point target; never persists. ``plan`` and
    ``grid`` default to plan_isac_roles(require_ues=False) and coverage_grid
    (the API validates them before the solve guard and passes them in)."""
    t_start = time.perf_counter()
    if tick is not None:
        tick(0, 1)
    if plan is None:
        plan = plan_isac_roles(
            scene, request.tx_ids, None, request.sensing_rx_ids, require_ues=False
        )
    if grid is None:
        bounds = (
            compute_scene_bounds(project_dir, scene) if request.center_xy is None else None
        )
        grid = coverage_grid(request, bounds)
    rm_grid, grid_warnings = grid
    txs = plan.txs
    if request.sensing_rx_ids is not None:
        by_id = {d.id: d for d in scene.devices}
        rxs = [by_id[i] for i in dict.fromkeys(request.sensing_rx_ids)]
    else:
        paired = {d.id for d in plan.sensing_rx.values()}
        rxs = [d for d in scene.devices if d.id in paired]

    warnings = list(extra_warnings) + list(grid_warnings)
    points = cell_centers(rm_grid)
    n_cells = len(points)
    devices = list({d.id: d for d in [*txs, *rxs]}.values())
    dev_index = {d.id: i for i, d in enumerate(devices)}

    los_failed = False
    occluded = False
    los_mask: Optional[np.ndarray] = None
    try:
        starts = np.repeat(
            np.array([d.position for d in devices], dtype=float), n_cells, axis=0
        )
        ends = np.tile(points, (len(devices), 1))
        mask, los_warnings = backend.segment_los(
            project_dir, scene, library, config, starts, ends
        )
        warnings.extend(los_warnings)
        if mask is not None:
            los_mask = np.asarray(mask, dtype=bool).reshape(len(devices), n_cells)
            occluded = True
    except Exception as exc:  # noqa: BLE001 - graceful degradation contract
        los_failed = True
        warnings.append(f"sensing coverage LOS test failed: {exc}; see logs")
    if los_mask is None:
        los_mask = np.full((len(devices), n_cells), not los_failed, dtype=bool)

    wavelength = SPEED_OF_LIGHT / float(config.frequency_hz)
    rcs_dbsm = request.resolved_rcs_dbsm()
    rx_rows = request.rx_rows if request.rx_rows is not None else request.tx_rows
    rx_cols = request.rx_cols if request.rx_cols is not None else request.tx_cols
    array_gain_db = (
        10.0 * math.log10(request.tx_rows * request.tx_cols)
        + 10.0 * math.log10(rx_rows * rx_cols)
        if request.array_gain == "steered"
        else 0.0
    )
    noise_dbm = noise_floor_dbm(config)
    integration_db = 10.0 * math.log10(request.cpi_pulses)
    alpha, absorption_warning = atmosphere.absorption_db_per_km(config)
    if absorption_warning:
        warnings.append(absorption_warning)

    # Element gain of every device toward every cell (its own pattern and
    # orientation, as the sionna echo sees it; iso = 0 dB).
    element_db = np.stack(
        [element_gain_db(d, points - np.asarray(d.position, dtype=float)) for d in devices]
    )

    links = [(t, r) for t in txs for r in rxs]
    snr = np.full((len(links), n_cells), -np.inf)
    link_los = np.zeros((len(links), n_cells), dtype=bool)
    for li, (t, r) in enumerate(links):
        both = los_mask[dev_index[t.id]] & los_mask[dev_index[r.id]]
        link_los[li] = both
        r_t = np.linalg.norm(points - np.asarray(t.position, dtype=float), axis=1)
        r_r = np.linalg.norm(points - np.asarray(r.position, dtype=float), axis=1)
        # atmosphere.path_attenuation_db over the echo's delay, vectorized.
        delay_s = (r_t + r_r) / SPEED_OF_LIGHT
        gas_db = alpha * (delay_s * atmosphere.SPEED_OF_LIGHT / 1000.0) if alpha > 0.0 else 0.0
        value = (
            float(t.power_dbm)
            + array_gain_db
            + element_db[dev_index[t.id]]
            + element_db[dev_index[r.id]]
            + bistatic_radar_gain_db(wavelength, rcs_dbsm, r_t, r_r)
            - gas_db
            - noise_dbm
            + integration_db
        )
        snr[li] = np.where(both, value, -np.inf)

    detected = snr >= request.threshold_db
    best = snr.max(axis=0) if links else np.full(n_cells, -np.inf)
    has_echo = np.isfinite(best)
    n_detected = detected.sum(axis=0)
    groups = geometry_groups([(t.position, r.position) for t, r in links])
    n_geom = np.zeros(n_cells, dtype=int)
    for g in sorted(set(groups)):
        members = [i for i, gi in enumerate(groups) if gi == g]
        n_geom += detected[members].any(axis=0)
    fusion = n_geom >= request.min_links_for_fusion
    det = request.detector
    pd_best = np.where(
        has_echo, pd_swerling(np.where(has_echo, best, 0.0), request.pfa, det.model), -np.inf
    )
    mc_ref: Optional[NoiseReference] = None
    spot_check: Optional[list[PdMcSpotCheck]] = None
    if det.monte_carlo_trials > 0:
        mc_ref = noise_reference(
            request.pfa, det.monte_carlo_trials, noise_seed(det.seed),
            request.cpi_pulses, det.empirical_threshold,
        )
        spot_check = _mc_spot_check(best, has_echo, pd_best, rm_grid, request, mc_ref)

    def pct(mask: np.ndarray) -> float:
        return 100.0 * float(mask.mean()) if n_cells else 0.0

    summary = SensingCoverageSummary(
        num_cells=n_cells,
        num_links=len(links),
        num_geometries=len(set(groups)),
        pct_cells_los=pct(link_los.any(axis=0)) if links else 0.0,
        pct_cells_detected=pct(detected.any(axis=0)) if links else 0.0,
        pct_cells_fusion_feasible=pct(fusion),
        median_best_snr_db=float(np.median(best[has_echo])) if has_echo.any() else None,
        mc_spot_check=spot_check,
    )
    coverage_links = [
        SensingCoverageLink(
            tx_id=t.id,
            rx_id=r.id,
            geometry_group=groups[li],
            baseline_m=math.dist(t.position, r.position),
            los_fraction=float(link_los[li].mean()) if n_cells else 0.0,
            detected_fraction=float(detected[li].mean()) if n_cells else 0.0,
        )
        for li, (t, r) in enumerate(links)
    ]
    if los_failed:
        los_model = "failed (no cell has an echo)"
    elif occluded:
        los_model = OCCLUSION_LOS_MODEL
    else:
        los_model = f"none ({backend.name}: no geometry occlusion)"
    metadata = {
        "request": metadata_dump(request, exclude=("config",)),
        "wavelength_m": wavelength,
        "noise_floor_dbm": noise_dbm,
        "integration_gain_db": integration_db,
        "threshold_db": float(request.threshold_db),
        "pfa": float(request.pfa),
        "rcs_dbsm": rcs_dbsm,
        "rcs_m2": dbsm_to_m2(rcs_dbsm),
        "array_gain_db": array_gain_db,
        "element_patterns": {d.id: d.antenna.pattern for d in devices},
        "num_rays": n_cells * len(devices),
        "los_model": los_model,
        "elapsed_s": time.perf_counter() - t_start,
    }
    if det != DetectorOptions():
        metadata["detector"] = detector_metadata(
            det.model, request.pfa, det.monte_carlo_trials, mc_ref
        )
    if tick is not None:
        tick(1, 1)
    return SensingCoverageResultSet(
        result_id=UNSAVED_RESULT_ID,
        backend=backend.name,
        simulation_config_id=config.id,
        frequency_hz=float(config.frequency_hz),
        rcs_dbsm=rcs_dbsm,
        array_gain_db=array_gain_db,
        tx_ids=[t.id for t in txs],
        sensing_rx_ids=[r.id for r in rxs],
        grid=rm_grid,
        values={
            "best_snr_db": _rows(best, rm_grid, none_below=True),
            "n_links_detected": _rows(n_detected.astype(float), rm_grid, none_below=False),
            "pd_best": _rows(pd_best, rm_grid, none_below=True),
            "fusion_feasible": _rows(fusion.astype(float), rm_grid, none_below=False),
        },
        links=coverage_links,
        summary=summary,
        warnings=warnings,
        metadata=metadata,
    )
