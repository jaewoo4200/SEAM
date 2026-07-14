"""Material-aware vs single-material-baseline channel impact.

Implements the CFR evaluation framework of Lee et al. (KICS 2026): solve the
same TX->RX link(s) twice - once with the scene's assigned materials and once
with EVERY prim rebound to a baseline material (default itu_concrete) - and
quantify the difference per position:

    NMSE      = sum |H_mat - H_base|^2 / sum |H_mat|^2      (dB)
    cos_sim   = |H_mat^H H_base| / (||H_mat|| ||H_base||)
    dRSS      = RSS_mat - RSS_base                            (signed dB)
    T_proxy   = B * mean_f log2(1 + P_tx |h(f)|^2 / N)        (Mbps)

H(f_k) = sum_l g_l exp(-j 2 pi f_k tau_l) is the same tap model as the
channel-analysis panel, so the numbers agree with what the UI shows.

CFR convention: g_l (hence H(f)) is a DIMENSIONLESS per-path CHANNEL GAIN
(path_gain_db = power_dbm - tx_power_dbm), NOT the received amplitude. The
transmit power P_tx therefore enters exactly once, in the capacity proxy
above; it must not also be baked into H. NMSE / cos_sim / dRSS / global NMSE
are ratio metrics in which the constant P_tx factor cancels, so their values
are independent of this convention.
"""

from __future__ import annotations

import math
from pathlib import Path

from seam_studio.schemas.material_impact import (
    MaterialImpactReport,
    MaterialImpactRequest,
    PositionImpact,
)
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import RayPath
from seam_studio.schemas.scene import RFBinding, Scene
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.simulation_backends.base import RayTracingBackend
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm


def _cfr(paths: list[RayPath], bandwidth_hz: float, k: int, tx_power_dbm: float):
    """Complex H(f) on k offsets across [-B/2, +B/2] from per-path CHANNEL GAIN.

    Each tap amplitude is sqrt(10^(gain_db/10)) where gain_db is the path's
    dimensionless channel gain (path_gain_db when the backend supplies it,
    else power_dbm - tx_power_dbm). Using the gain rather than the received
    power keeps H(f) free of the transmit power, so capacity applies P_tx
    exactly once (see module docstring)."""
    import numpy as np

    freqs = np.linspace(-bandwidth_hz / 2.0, bandwidth_hz / 2.0, k)
    if not paths:
        return freqs, np.zeros(k, dtype=np.complex128)
    gains_db = [
        p.path_gain_db if p.path_gain_db is not None else p.power_dbm - tx_power_dbm
        for p in paths
    ]
    amps = np.asarray([math.sqrt(10.0 ** (g / 10.0)) for g in gains_db])
    phases = np.asarray([p.phase_rad for p in paths])
    taus = np.asarray([p.delay_ns * 1e-9 for p in paths])
    gains = amps * np.exp(1j * phases)
    return freqs, (gains[None, :] * np.exp(-2j * np.pi * freqs[:, None] * taus[None, :])).sum(axis=1)


def _capacity_mbps(h, tx_power_dbm: float, noise_dbm: float, bandwidth_hz: float):
    """Shannon proxy T = B * mean_f log2(1 + P|h|^2/N), in Mbps.

    ``h`` is the dimensionless channel gain from ``_cfr`` (TX power NOT baked
    in), so multiplying by P here applies the transmit power exactly once:
    P * |h|^2 recovers the received power over the noise N."""
    import numpy as np

    p_lin = 10.0 ** (tx_power_dbm / 10.0)
    n_lin = 10.0 ** (noise_dbm / 10.0)
    snr = p_lin * np.abs(h) ** 2 / n_lin
    return float(bandwidth_hz * np.mean(np.log2(1.0 + snr)) / 1e6)


def material_impact(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: MaterialImpactRequest,
) -> MaterialImpactReport:
    import numpy as np

    if library.get(request.baseline_material_id) is None:
        raise ValueError(f"unknown baseline material: {request.baseline_material_id}")
    txs = [d for d in scene.devices if d.kind == "tx"]
    rxs = [d for d in scene.devices if d.kind == "rx"]
    if not txs or not rxs:
        raise ValueError("scene needs at least one tx and one rx")
    tx = next((d for d in txs if d.id == request.tx_id), txs[0])
    rx = next((d for d in rxs if d.id == request.rx_id), rxs[0])
    if request.tx_id and tx.id != request.tx_id:
        raise ValueError(f"unknown tx device: {request.tx_id}")
    if request.rx_id and rx.id != request.rx_id:
        raise ValueError(f"unknown rx device: {request.rx_id}")

    warnings: list[str] = []
    positions = request.waypoints or [list(rx.position)]
    noise = noise_floor_dbm(config)

    # Baseline scene: every prim rebound to the single baseline material.
    baseline_scene = scene.model_copy(deep=True)
    for prim in baseline_scene.prims:
        # Replace the whole binding (field-by-field mutation trips the
        # material-without-status validator mid-assignment). Synthetic
        # baseline scene - never persisted.
        prim.rf = RFBinding(
            material_id=request.baseline_material_id,
            assignment_status="user_confirmed",
            assignment_sources=["baseline"],
        )

    def solve(step_scene: Scene, pos) -> list[RayPath]:
        step = step_scene.model_copy(deep=True)
        for d in step.devices:
            if d.id == rx.id:
                d.position = [float(c) for c in pos]
        cfg = config.model_copy(update={"tx_ids": [tx.id], "rx_ids": [rx.id]})
        result = backend.simulate_paths(project_dir, step, library, cfg)
        return [p for p in result.paths if p.tx_id == tx.id and p.rx_id == rx.id]

    per_pos: list[PositionImpact] = []
    num_lin_sum = 0.0
    den_lin_sum = 0.0
    cos_vals: list[float] = []
    drss_vals: list[float] = []
    cap_mat: list[float] = []
    cap_base: list[float] = []
    sensitive = 0
    try:
        # The Sionna backend reads materials from the on-disk projection:
        # compile each variant before its solves (same discipline as
        # calibration - an in-memory rebind alone never reaches the solver).
        backend.compile(project_dir, scene, library)
        mat_paths = [solve(scene, pos) for pos in positions]
        backend.compile(project_dir, baseline_scene, library)
        base_paths = [solve(baseline_scene, pos) for pos in positions]
    finally:
        backend.compile(project_dir, scene, library)

    k = request.num_cfr_points
    for pos, pm, pb in zip(positions, mat_paths, base_paths):
        _, h_mat = _cfr(pm, config.bandwidth_hz, k, tx.power_dbm)
        _, h_base = _cfr(pb, config.bandwidth_hz, k, tx.power_dbm)
        e_mat = float(np.sum(np.abs(h_mat) ** 2))
        e_base = float(np.sum(np.abs(h_base) ** 2))
        row = PositionImpact(position=[float(c) for c in pos])
        if e_mat > 0.0:
            err = float(np.sum(np.abs(h_mat - h_base) ** 2))
            row.nmse_db = 10.0 * math.log10(err / e_mat) if err > 0 else -300.0
            num_lin_sum += err
            den_lin_sum += e_mat
            row.material_sensitive = row.nmse_db > request.sensitive_nmse_db
            sensitive += int(row.material_sensitive)
        if e_mat > 0.0 and e_base > 0.0:
            num = abs(complex(np.vdot(h_mat, h_base)))
            row.cosine_similarity = float(
                num / (math.sqrt(e_mat) * math.sqrt(e_base))
            )
            cos_vals.append(row.cosine_similarity)
        if pm:
            row.rss_material_dbm = 10.0 * math.log10(
                sum(10.0 ** (p.power_dbm / 10.0) for p in pm)
            )
            cap_mat.append(_capacity_mbps(h_mat, tx.power_dbm, noise, config.bandwidth_hz))
        if pb:
            row.rss_baseline_dbm = 10.0 * math.log10(
                sum(10.0 ** (p.power_dbm / 10.0) for p in pb)
            )
            cap_base.append(_capacity_mbps(h_base, tx.power_dbm, noise, config.bandwidth_hz))
        if row.rss_material_dbm is not None and row.rss_baseline_dbm is not None:
            row.delta_rss_db = row.rss_material_dbm - row.rss_baseline_dbm
            drss_vals.append(row.delta_rss_db)
        per_pos.append(row)

    active = sum(1 for r in per_pos if r.nmse_db is not None)
    if active == 0:
        warnings.append(
            "no position produced a material-aware channel; check TX/RX placement"
        )
    global_nmse = (
        10.0 * math.log10(num_lin_sum / den_lin_sum)
        if den_lin_sum > 0 and num_lin_sum > 0
        else None
    )
    return MaterialImpactReport(
        baseline_material_id=request.baseline_material_id,
        tx_id=tx.id,
        rx_id=rx.id,
        global_nmse_db=global_nmse,
        mean_cosine_similarity=(sum(cos_vals) / len(cos_vals)) if cos_vals else None,
        mean_delta_rss_db=(sum(drss_vals) / len(drss_vals)) if drss_vals else None,
        mean_capacity_material_mbps=(sum(cap_mat) / len(cap_mat)) if cap_mat else None,
        mean_capacity_baseline_mbps=(sum(cap_base) / len(cap_base)) if cap_base else None,
        material_sensitive_count=sensitive,
        positions=per_pos,
        backend=backend.name,
        warnings=warnings,
    )
