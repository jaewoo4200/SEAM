"""ISAC beam trade-off (POST /projects/{id}/simulate/isac): per TRP, which
codebook beam serves its UEs best, which one sees the targets best, and what
sharing the slots / pulses between the two costs in rate and buys in Pd.

One t = 0 solve pair on single-element antennas: the echo solve (TX ->
target -> sensing RX, the RCS solver) and the comm solve (TX -> UE, targets
as absorbers, as in ``include_comm_paths``). Everything after that is pure
synthesis on the stored RayPaths. Each path's world-frame departure/arrival
direction gives the planar-array response in the sionna synthetic-array
convention (paths.py, antenna_array.py)

    a_n = exp(+j 2 pi p_n . k_local),  k_local = R(orientation)^T k_world,

with p_n the PlanarArray element position in wavelengths. A codebook beam w
(the azimuth DFT beams of /simulate/beamforming, sionna_backend
._steering_from_positions) scores conj(w) . a on both ends: the matched
filtering of ``_codebook_sweep`` (H @ conj(w_t), vdot(w_r, .)). The solve
uses 1x1 antennas so every path is referenced to the panel center, which
makes the synthesis equal Sionna's own synthetic array.
"""

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

import numpy as np

from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import (
    ISACBeam,
    ISACParetoSummary,
    ISACPoint,
    ISACResultSet,
    ISACTxResult,
    RayPath,
)
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.sensing import (
    ISACRequest,
    SensingSimulateRequest,
    SensingTrackOptions,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.channel_npz_export import local_frame_matrix
from seam_studio.services.sensing import ResolvedSensingTarget
from seam_studio.services.sensing_track import detection_constants, swerling1_pd
from seam_studio.services.simulation_backends.base import (
    UNSAVED_RESULT_ID,
    RayTracingBackend,
)
from seam_studio.services.simulation_backends.sionna_backend import (
    _steering_from_positions,
    noise_floor_dbm,
)

COLOCATED_SENSING_RX_M = 1.0
# |h| below this is no channel (log of zero).
_MIN_AMPLITUDE = 1e-30
# Two trade-off points closer than this on both axes are one point.
_PARETO_TOL = 1e-12
ISAC_MODEL = "path-synthesized azimuth DFT codebook (sionna synthetic-array convention)"


class ISACRequestError(ValueError):
    """The request cannot be planned (API -> 400)."""


@dataclass(frozen=True)
class ISACPlan:
    txs: list[Device]
    # tx id -> its sensing rx.
    sensing_rx: dict[str, Device]
    ues: list[Device]


def _rx_devices(by_id: dict[str, Device], ids: Sequence[str]) -> list[Device]:
    out: list[Device] = []
    seen: set[str] = set()
    for device_id in ids:
        device = by_id.get(device_id)
        if device is None or device.kind != "rx":
            raise ISACRequestError(f"rx device not found: {device_id}")
        if device_id not in seen:
            seen.add(device_id)
            out.append(device)
    return out


def plan_isac_roles(
    scene: Scene,
    tx_ids: Optional[Sequence[str]],
    ue_rx_ids: Optional[Sequence[str]],
    sensing_rx_ids: Optional[Sequence[str]],
    *,
    require_ues: bool = True,
) -> ISACPlan:
    """Split the scene's devices into ISAC roles.

    Sensing receivers: ``sensing_rx_ids``, else every rx (not a requested UE)
    within ``COLOCATED_SENSING_RX_M`` of a selected tx. Each tx pairs with the
    nearest one (ties: pool order; auto mode also needs it within 1 m), so
    several txs may share one (bistatic). UEs: ``ue_rx_ids``, else every rx
    outside the sensing pool that is not within 1 m of ANY tx of the scene
    (an unselected TRP's radar receiver is not a served user)."""
    by_id = {d.id: d for d in scene.devices}
    for device_id in tx_ids or []:
        device = by_id.get(device_id)
        if device is None or device.kind != "tx":
            raise ISACRequestError(f"tx device not found: {device_id}")
    txs = [
        d for d in scene.devices
        if d.kind == "tx" and (tx_ids is None or d.id in tx_ids)
    ]
    if not txs:
        raise ISACRequestError("ISAC needs at least one tx device")

    explicit_ues = _rx_devices(by_id, ue_rx_ids) if ue_rx_ids is not None else None
    explicit_sensing = (
        _rx_devices(by_id, sensing_rx_ids) if sensing_rx_ids is not None else None
    )
    ue_set = {d.id for d in explicit_ues or []}
    if explicit_sensing is not None:
        for d in explicit_sensing:
            if d.id in ue_set:
                raise ISACRequestError(
                    f"rx {d.id} cannot be both a UE and a sensing receiver"
                )
        pool = explicit_sensing
    else:
        pool = [
            d for d in scene.devices
            if d.kind == "rx"
            and d.id not in ue_set
            and any(
                math.dist(d.position, t.position) <= COLOCATED_SENSING_RX_M for t in txs
            )
        ]

    sensing_rx: dict[str, Device] = {}
    for t in txs:
        best: Optional[tuple[float, Device]] = None
        for d in pool:
            dist = math.dist(t.position, d.position)
            if best is None or dist < best[0]:
                best = (dist, d)
        if best is None or (
            explicit_sensing is None and best[0] > COLOCATED_SENSING_RX_M
        ):
            raise ISACRequestError(
                f"tx {t.id} has no sensing receiver: place an rx within 1 m of it "
                "(monostatic) or pass sensing_rx_ids"
            )
        sensing_rx[t.id] = best[1]

    pool_ids = {d.id for d in pool}
    if explicit_ues is not None:
        ues = explicit_ues
    else:
        all_txs = [d for d in scene.devices if d.kind == "tx"]
        ues = [
            d for d in scene.devices
            if d.kind == "rx"
            and d.id not in pool_ids
            and not any(
                math.dist(d.position, t.position) <= COLOCATED_SENSING_RX_M
                for t in all_txs
            )
        ]
    if require_ues and not ues:
        raise ISACRequestError(
            "ISAC needs at least one UE: an rx device not co-located with a tx "
            "(or pass ue_rx_ids)"
        )
    return ISACPlan(txs=txs, sensing_rx=sensing_rx, ues=ues)


# ------------------------------------------------------------ beam math


def codebook_angles(start: float, stop: float, step: float) -> list[float]:
    """The sweep of sionna_backend._codebook_sweep, beam for beam."""
    out: list[float] = []
    a = start
    while a <= stop + 1e-9:
        out.append(round(a, 6))
        a += step
    return out


def planar_positions(
    rows: int, cols: int, vertical_spacing: float, horizontal_spacing: float
) -> np.ndarray:
    """[3, rows*cols] element positions in wavelengths, panel-centered, x = 0
    (sionna PlanarArray.normalized_positions)."""
    i, j = np.meshgrid(np.arange(rows), np.arange(cols), indexing="ij")
    y = horizontal_spacing * j - (cols - 1) * horizontal_spacing / 2.0
    z = -vertical_spacing * i + (rows - 1) * vertical_spacing / 2.0
    return np.stack([np.zeros(rows * cols), y.ravel(), z.ravel()])


def codebook_weights(pos: np.ndarray, angles: Sequence[float]) -> np.ndarray:
    """[K, N] unit-norm azimuth beams; row k = _steering_from_positions(y, angle_k)."""
    if not angles:
        return np.zeros((0, pos.shape[1]), dtype=complex)
    return np.stack([_steering_from_positions(pos[1], a, np) for a in angles])


def world_unit(az_el_deg: Sequence[float]) -> np.ndarray:
    a, e = math.radians(float(az_el_deg[0])), math.radians(float(az_el_deg[1]))
    return np.array([math.cos(e) * math.cos(a), math.cos(e) * math.sin(a), math.sin(e)])


def array_response(
    pos: np.ndarray, orientation_deg: Sequence[float], az_el_deg: Sequence[float]
) -> np.ndarray:
    """[N] response of a panel with ``orientation_deg`` to a plane wave along
    world direction ``az_el_deg`` (departure for a TX, arrival-from for an RX)."""
    rot = np.asarray(local_frame_matrix(orientation_deg))
    return np.exp(1j * 2.0 * np.pi * (pos.T @ (rot.T @ world_unit(az_el_deg))))


def path_alpha(p: RayPath, tx_power_dbm: float) -> complex:
    g = p.path_gain_db if p.path_gain_db is not None else p.power_dbm - tx_power_dbm
    return 10.0 ** (g / 20.0) * complex(math.cos(p.phase_rad), math.sin(p.phase_rad))


def comm_beam_channel(
    paths: Sequence[RayPath], tx: Device, pos: np.ndarray, weights: np.ndarray
) -> np.ndarray:
    """[K] complex channel to a single-element UE with each TX beam:
    h_k = sum_l alpha_l conj(w_k) . a_tx(l)."""
    h = np.zeros(weights.shape[0], dtype=complex)
    conj_w = np.conj(weights)
    for p in paths:
        a = array_response(pos, tx.orientation_deg, p.aod_deg)
        h += path_alpha(p, tx.power_dbm) * (conj_w @ a)
    return h


def echo_beam_matrix(
    echoes: Sequence[RayPath],
    tx: Device,
    rx: Device,
    pos_tx: np.ndarray,
    pos_rx: np.ndarray,
    w_tx: np.ndarray,
    w_rx: np.ndarray,
) -> np.ndarray:
    """[K, M] complex echo with TX beam k and RX beam m, every echo summed
    coherently: E = sum_e alpha_e (conj(w_k) . a_tx(e)) (w_m^H a_rx(e))."""
    out = np.zeros((w_tx.shape[0], w_rx.shape[0]), dtype=complex)
    conj_t, conj_r = np.conj(w_tx), np.conj(w_rx)
    for e in echoes:
        g_tx = conj_t @ array_response(pos_tx, tx.orientation_deg, e.aod_deg)
        g_rx = conj_r @ array_response(pos_rx, rx.orientation_deg, e.aoa_deg)
        out += path_alpha(e, tx.power_dbm) * np.outer(g_tx, g_rx)
    return out


def _db20(x: complex | float) -> Optional[float]:
    mag = abs(x)
    return 20.0 * math.log10(mag) if mag >= _MIN_AMPLITUDE else None


def _rate(sinr_db: Optional[float]) -> float:
    return math.log2(1.0 + 10.0 ** (sinr_db / 10.0)) if sinr_db is not None else 0.0


# ------------------------------------------------------- trade-off curve


def pareto_flags(pairs: Sequence[tuple[float, float]]) -> list[bool]:
    """(rate, pd) pairs -> on the Pareto front: no other point is at least as
    good on both axes and better on one. Duplicates (within 1e-12 on both)
    keep only the first."""
    if not pairs:
        return []
    r = np.array([p[0] for p in pairs], dtype=float)
    d = np.array([p[1] for p in pairs], dtype=float)
    # Rate descending + running max of pd: O(n log n) time, O(n) memory (a
    # pairwise test needs n x n float64 temporaries). j dominates i iff
    # r_j > r_i + tol with d_j >= d_i - tol, or r_j >= r_i - tol with
    # d_j > d_i + tol: two prefix-max queries over the sorted rates.
    order = np.argsort(-r, kind="stable")
    neg_sorted = -r[order]  # ascending
    pd_prefix_max = np.maximum.accumulate(d[order])
    n_above = np.searchsorted(neg_sorted, -(r + _PARETO_TOL), side="left")
    n_at_least = np.searchsorted(neg_sorted, -(r - _PARETO_TOL), side="right")
    max_above = np.where(n_above > 0, pd_prefix_max[np.maximum(n_above - 1, 0)], -np.inf)
    max_at_least = pd_prefix_max[n_at_least - 1]  # >= 1: i itself
    dominated = (max_above >= d - _PARETO_TOL) | (max_at_least > d + _PARETO_TOL)
    flags: list[bool] = []
    for i in range(len(r)):
        if dominated[i]:
            flags.append(False)
            continue
        # Duplicates (within tol on both) keep the first in emission order;
        # the rate window is widened so the exact test below decides.
        lo = np.searchsorted(neg_sorted, -(r[i] + 2.0 * _PARETO_TOL), side="left")
        hi = np.searchsorted(neg_sorted, -(r[i] - 2.0 * _PARETO_TOL), side="right")
        js = order[lo:hi]
        earlier = js[js < i]
        flags.append(
            not bool(
                np.any(
                    (np.abs(r[earlier] - r[i]) <= _PARETO_TOL)
                    & (np.abs(d[earlier] - d[i]) <= _PARETO_TOL)
                )
            )
        )
    return flags


def tradeoff_points(
    ue_ids: Sequence[str],
    beam_ue_rates: Sequence[dict[str, float]],
    comm_beam_idx: Optional[int],
    beam_sensing_snr_db: Sequence[Optional[float]],
    slot_ratios: Sequence[float],
    sharing_mode: str,
    pfa: float,
) -> list[ISACPoint]:
    """Operating points of one tx, Pareto-flagged: rho ascending, and within
    rho > 0 the sensing beam k ascending. A fraction rho of the slots (or
    pulses) senses with beam k, so the echo integrates rho * cpi_pulses
    (``beam_sensing_snr_db`` is the full-CPI SNR). time_sharing: the UEs get
    the comm beam in the other 1 - rho; dual_function: they are also served
    by beam k while it senses."""
    comm_rates = {
        u: (beam_ue_rates[comm_beam_idx][u] if comm_beam_idx is not None else 0.0)
        for u in ue_ids
    }
    points: list[ISACPoint] = []
    for rho in slot_ratios:
        if rho <= 0.0:
            points.append(
                ISACPoint(
                    rho=0.0,
                    beam_idx=None,
                    sum_rate_bps_hz=sum(comm_rates[u] for u in ue_ids),
                    ue_rates_bps_hz=dict(comm_rates),
                    sensing_snr_db=None,
                    pd=pfa,
                )
            )
            continue
        gain = 10.0 * math.log10(rho)
        for k, snr_full in enumerate(beam_sensing_snr_db):
            if sharing_mode == "time_sharing":
                rates = {u: (1.0 - rho) * comm_rates[u] for u in ue_ids}
            else:
                rates = {
                    u: (1.0 - rho) * comm_rates[u] + rho * beam_ue_rates[k][u]
                    for u in ue_ids
                }
            snr = snr_full + gain if snr_full is not None else None
            points.append(
                ISACPoint(
                    rho=float(rho),
                    beam_idx=k,
                    sum_rate_bps_hz=sum(rates[u] for u in ue_ids),
                    ue_rates_bps_hz=rates,
                    sensing_snr_db=snr,
                    pd=swerling1_pd(snr, pfa),
                )
            )
    for point, flag in zip(
        points, pareto_flags([(p.sum_rate_bps_hz, p.pd) for p in points])
    ):
        point.pareto = flag
    return points


def pareto_summary(
    points: Sequence[ISACPoint], comm_only_rate: float, pd_target: float, pfa: float
) -> ISACParetoSummary:
    """Operating point at ``pd_target``: the max rate among points with
    pd >= pd_target. Rates within _PARETO_TOL tie (float noise: the comm beam
    gives (1 - rho) R + rho R at every rho) and go to the higher pd, so the
    named point is on the front; full ties go to the first point."""
    max_pd = max((p.pd for p in points), default=pfa)
    best: Optional[ISACPoint] = None
    for p in points:
        if p.pd < pd_target:
            continue
        if (
            best is None
            or p.sum_rate_bps_hz > best.sum_rate_bps_hz + _PARETO_TOL
            or (
                abs(p.sum_rate_bps_hz - best.sum_rate_bps_hz) <= _PARETO_TOL
                and p.pd > best.pd + _PARETO_TOL
            )
        ):
            best = p
    floor = 0.95 * comm_only_rate - _PARETO_TOL
    # No point keeps 95 % of the rate (e.g. only time-sharing points with
    # rho near 1): that operating point is comm only, sensing at the Pfa.
    pd_95 = max((p.pd for p in points if p.sum_rate_bps_hz >= floor), default=pfa)
    return ISACParetoSummary(
        comm_only_rate_bps_hz=comm_only_rate,
        max_pd=max_pd,
        pd_target=pd_target,
        rate_at_pd_target_bps_hz=best.sum_rate_bps_hz if best else None,
        rho_at_pd_target=best.rho if best else None,
        beam_idx_at_pd_target=best.beam_idx if best else None,
        rate_loss_at_pd_target_bps_hz=(comm_only_rate - best.sum_rate_bps_hz) if best else None,
        pd_at_95pct_rate=pd_95,
        num_pareto_points=sum(1 for p in points if p.pareto),
    )


def snr_for_pd_db(pd: float, pfa: float) -> Optional[float]:
    """Swerling-1 SNR [dB] that reaches ``pd`` at ``pfa`` (None: any SNR does)."""
    lin = math.log(pfa) / math.log(pd) - 1.0
    return 10.0 * math.log10(lin) if lin > 0.0 else None


# ---------------------------------------------------------------- solve


def _single_element_scene(scene: Scene) -> Scene:
    devices = [
        d.model_copy(
            update={"antenna": d.antenna.model_copy(update={"num_rows": 1, "num_cols": 1})}
        )
        for d in scene.devices
    ]
    return scene.model_copy(update={"devices": devices})


def _argmax_optional(values: Sequence[Optional[float]]) -> Optional[int]:
    best: Optional[int] = None
    for i, v in enumerate(values):
        if v is not None and (best is None or v > values[best]):  # type: ignore[operator]
            best = i
    return best


@dataclass
class _TxChannels:
    tx: Device
    rx: Device
    # UE id -> [K] RSS dBm (None = no path); every planned UE.
    rss: dict[str, list[Optional[float]]]
    ue_single: dict[str, Optional[float]]
    # Target id -> [K] full-CPI echo SNR and best RX angle per TX beam.
    target_snr: dict[str, list[Optional[float]]]
    target_rx_angle: dict[str, list[Optional[float]]]
    target_single: dict[str, Optional[float]]


def _tx_channels(
    tx: Device,
    rx: Device,
    request: ISACRequest,
    angles: list[float],
    ues: list[Device],
    targets: list[ResolvedSensingTarget],
    comm: dict[tuple[str, str], list[RayPath]],
    echoes: dict[tuple[str, str, str], list[RayPath]],
    noise_dbm: float,
) -> _TxChannels:
    rx_rows = request.rx_rows if request.rx_rows is not None else request.tx_rows
    rx_cols = request.rx_cols if request.rx_cols is not None else request.tx_cols
    pos_tx = planar_positions(
        request.tx_rows, request.tx_cols,
        tx.antenna.vertical_spacing, tx.antenna.horizontal_spacing,
    )
    pos_rx = planar_positions(
        rx_rows, rx_cols, rx.antenna.vertical_spacing, rx.antenna.horizontal_spacing
    )
    w_tx = codebook_weights(pos_tx, angles)
    w_rx = codebook_weights(pos_rx, angles)
    integration_db = 10.0 * math.log10(request.cpi_pulses)

    rss: dict[str, list[Optional[float]]] = {}
    ue_single: dict[str, Optional[float]] = {}
    for ue in ues:
        paths = comm.get((tx.id, ue.id), [])
        if not paths:
            rss[ue.id] = [None] * len(angles)
            ue_single[ue.id] = None
            continue
        gains = [_db20(v) for v in comm_beam_channel(paths, tx, pos_tx, w_tx)]
        rss[ue.id] = [tx.power_dbm + g if g is not None else None for g in gains]
        single = _db20(sum((path_alpha(p, tx.power_dbm) for p in paths), 0j))
        ue_single[ue.id] = tx.power_dbm + single if single is not None else None

    target_snr: dict[str, list[Optional[float]]] = {}
    target_rx_angle: dict[str, list[Optional[float]]] = {}
    target_single: dict[str, Optional[float]] = {}
    for t in targets:
        mine = echoes.get((tx.id, rx.id, t.actor_id), [])
        snr: list[Optional[float]] = [None] * len(angles)
        rx_angle: list[Optional[float]] = [None] * len(angles)
        if mine:
            mag = np.abs(echo_beam_matrix(mine, tx, rx, pos_tx, pos_rx, w_tx, w_rx))
            for k in range(len(angles)):
                m = int(np.argmax(mag[k]))
                db = _db20(float(mag[k, m]))
                if db is not None:
                    snr[k] = tx.power_dbm + db - noise_dbm + integration_db
                    rx_angle[k] = angles[m]
        target_snr[t.actor_id] = snr
        target_rx_angle[t.actor_id] = rx_angle
        single = _db20(sum((path_alpha(e, tx.power_dbm) for e in mine), 0j))
        target_single[t.actor_id] = (
            tx.power_dbm + single - noise_dbm + integration_db
            if mine and single is not None
            else None
        )
    return _TxChannels(
        tx=tx, rx=rx, rss=rss, ue_single=ue_single,
        target_snr=target_snr, target_rx_angle=target_rx_angle,
        target_single=target_single,
    )


def _tx_result(
    ch: _TxChannels,
    served: list[str],
    targets: list[ResolvedSensingTarget],
    request: ISACRequest,
    angles: list[float],
    ratios: list[float],
    noise_dbm: float,
) -> ISACTxResult:
    target_ids = [t.actor_id for t in targets]
    rx_rows = request.rx_rows if request.rx_rows is not None else request.tx_rows
    rx_cols = request.rx_cols if request.rx_cols is not None else request.tx_cols
    beams: list[ISACBeam] = []
    beam_rates: list[dict[str, float]] = []
    beam_snr: list[Optional[float]] = []
    for k, angle in enumerate(angles):
        sinr = {
            u: (ch.rss[u][k] - noise_dbm) if ch.rss[u][k] is not None else None
            for u in served
        }
        rates = {u: _rate(sinr[u]) for u in served}
        per_target = {q: ch.target_snr[q][k] for q in target_ids}
        weakest = (
            min(per_target.values())  # type: ignore[type-var]
            if per_target and all(v is not None for v in per_target.values())
            else None
        )
        beam_rates.append(rates)
        beam_snr.append(weakest)
        beams.append(
            ISACBeam(
                angle_deg=angle,
                ue_sinr_db=sinr,
                sum_rate_bps_hz=sum(rates[u] for u in served),
                target_snr_db=per_target,
                target_best_rx_angle_deg={q: ch.target_rx_angle[q][k] for q in target_ids},
                sensing_snr_db=weakest,
                pd=swerling1_pd(weakest, request.pfa),
                detected=weakest is not None and weakest >= request.threshold_db,
            )
        )

    warnings: list[str] = []
    has_path = any(v is not None for u in served for v in ch.rss[u])
    comm_idx = (
        int(np.argmax([b.sum_rate_bps_hz for b in beams])) if served and has_path else None
    )
    if not served:
        warnings.append(f"tx {ch.tx.id} serves no UE: sensing-only trade-off")
    sensing_idx = _argmax_optional(beam_snr)
    comm_angle = angles[comm_idx] if comm_idx is not None else None
    sensing_angle = angles[sensing_idx] if sensing_idx is not None else None
    points = tradeoff_points(
        served, beam_rates, comm_idx, beam_snr, ratios, request.sharing_mode, request.pfa
    )
    comm_only = beams[comm_idx].sum_rate_bps_hz if comm_idx is not None else 0.0
    return ISACTxResult(
        tx_id=ch.tx.id,
        sensing_rx_id=ch.rx.id,
        sensing_baseline_m=math.dist(ch.tx.position, ch.rx.position),
        ue_ids=list(served),
        target_ids=target_ids,
        tx_array=[request.tx_rows, request.tx_cols],
        rx_array=[rx_rows, rx_cols],
        angles_deg=list(angles),
        beams=beams,
        comm_beam_idx=comm_idx,
        sensing_beam_idx=sensing_idx,
        comm_beam_angle_deg=comm_angle,
        sensing_beam_angle_deg=sensing_angle,
        angle_gap_deg=(
            abs(comm_angle - sensing_angle)
            if comm_angle is not None and sensing_angle is not None
            else None
        ),
        ue_single_element_rss_dbm={u: ch.ue_single[u] for u in served},
        target_single_element_snr_db=dict(ch.target_single),
        points=points,
        pareto=pareto_summary(points, comm_only, request.pd_target, request.pfa),
        warnings=warnings,
    )


def run_isac(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: ISACRequest,
    plan: ISACPlan,
    targets: list[ResolvedSensingTarget],
    tick: Optional[Callable[[int, int], None]] = None,
    extra_warnings: tuple[str, ...] | list[str] = (),
) -> ISACResultSet:
    """Echo solve + comm solve + synthesis; never persists."""
    if tick is not None:
        tick(0, 2)
    tx_ids = [t.id for t in plan.txs]
    sensing_ids = list(dict.fromkeys(plan.sensing_rx[t].id for t in tx_ids))
    ue_ids = [u.id for u in plan.ues]
    involved = set(tx_ids) | set(sensing_ids) | set(ue_ids)
    solve_scene = _single_element_scene(scene)

    echo = backend.simulate_sensing(
        project_dir,
        solve_scene,
        library,
        config.model_copy(update={"tx_ids": tx_ids, "rx_ids": sensing_ids}),
        SensingSimulateRequest(
            target_actor_ids=[t.actor_id for t in targets],
            samples_per_sp=request.samples_per_sp,
            max_depth=request.max_depth,
        ),
        targets,
    )
    if tick is not None:
        tick(1, 2)
    echo_paths = [p for p in echo.paths if p.target_id is not None]
    comm_paths: list[RayPath] = []
    comm_warnings: list[str] = []
    if ue_ids:
        comm = backend.simulate_paths_with_targets(
            project_dir,
            solve_scene,
            library,
            config.model_copy(update={"tx_ids": tx_ids, "rx_ids": ue_ids}),
            targets,
        )
        dop = comm.metadata.get("doppler_hz")
        aligned = isinstance(dop, list) and len(dop) == len(comm.paths)
        comm_paths = [
            p.model_copy(update={"doppler_hz": float(dop[i])}) if aligned else p
            for i, p in enumerate(comm.paths)
        ]
        comm_warnings = [f"comm paths: {w}" for w in comm.warnings]

    warnings = list(extra_warnings) + list(echo.warnings) + comm_warnings
    if any(
        d.id in involved and d.antenna.polarization in ("VH", "cross")
        for d in scene.devices
    ):
        warnings.append(
            "dual-polarized antenna: ISAC synthesizes the first polarization port only"
        )
    usable_echoes = [p for p in echo_paths if p.aod_deg is not None and p.aoa_deg is not None]
    usable_comm = [p for p in comm_paths if p.aod_deg is not None]
    skipped = len(echo_paths) - len(usable_echoes) + len(comm_paths) - len(usable_comm)
    if skipped:
        warnings.append(f"{skipped} path(s) without departure/arrival angles skipped")
    comm_by_link: dict[tuple[str, str], list[RayPath]] = {}
    for p in usable_comm:
        comm_by_link.setdefault((p.tx_id, p.rx_id), []).append(p)
    echoes_by_link: dict[tuple[str, str, str], list[RayPath]] = {}
    for p in usable_echoes:
        echoes_by_link.setdefault((p.tx_id, p.rx_id, p.target_id or ""), []).append(p)

    noise_dbm = noise_floor_dbm(config)
    angles = codebook_angles(
        request.sweep_start_deg, request.sweep_stop_deg, request.sweep_step_deg
    )
    ratios = sorted(set(float(r) for r in request.slot_ratios))
    channels = [
        _tx_channels(
            t, plan.sensing_rx[t.id], request, angles, plan.ues, targets,
            comm_by_link, echoes_by_link, noise_dbm,
        )
        for t in plan.txs
    ]

    # Serving rule: strongest best-beam RSS (ties: tx order).
    ue_serving_tx: dict[str, Optional[str]] = {}
    for u in ue_ids:
        best: Optional[tuple[float, str]] = None
        for ch in channels:
            peak = max((v for v in ch.rss[u] if v is not None), default=None)
            if peak is not None and (best is None or peak > best[0]):
                best = (peak, ch.tx.id)
        ue_serving_tx[u] = best[1] if best else None
        if best is None:
            warnings.append(
                f"UE {u} has no path to any selected tx; left unserved"
                if request.ue_association == "serving"
                else f"UE {u} has no path to any selected tx"
            )

    txs = []
    for ch in channels:
        served = (
            list(ue_ids)
            if request.ue_association == "all"
            else [u for u in ue_ids if ue_serving_tx[u] == ch.tx.id]
        )
        txs.append(_tx_result(ch, served, targets, request, angles, ratios, noise_dbm))

    consts = detection_constants(
        config,
        SensingTrackOptions(
            threshold_db=request.threshold_db,
            cpi_s=request.cpi_s,
            cpi_pulses=request.cpi_pulses,
        ),
    )
    for key in ("mti_min_doppler_hz", "mti_blind_speed_m_s"):
        consts.pop(key, None)
    metadata = {
        "request": request.model_dump(mode="json", exclude={"config"}),
        **consts,
        "codebook_size": len(angles),
        "echo_path_count": len(echo_paths),
        "comm_path_count": len(comm_paths),
        "snr_for_pd_target_db": snr_for_pd_db(request.pd_target, request.pfa),
        "pd_at_threshold": swerling1_pd(request.threshold_db, request.pfa),
        "model": ISAC_MODEL,
    }
    if tick is not None:
        tick(2, 2)
    return ISACResultSet(
        result_id=UNSAVED_RESULT_ID,
        backend=backend.name,
        simulation_config_id=config.id,
        frequency_hz=float(config.frequency_hz),
        sharing_mode=request.sharing_mode,
        ue_association=request.ue_association,
        slot_ratios=ratios,
        noise_floor_dbm=noise_dbm,
        ue_serving_tx=ue_serving_tx,
        txs=txs,
        paths=(echo_paths + comm_paths) if request.include_paths else None,
        warnings=warnings,
        metadata=metadata,
    )
