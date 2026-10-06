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

Phase C options (each off by default, leaving the v0.1.12 numbers as they
were): an elevation sweep turns the codebook into an azimuth x elevation
grid (codebook_weights_2d), ``interference`` adds the other txs' comm /
sensing beams to each served UE's SINR (synchronized slots), and
``detector`` picks the Pd model and a Monte Carlo of it (services/detector).
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
    DetectorModel,
    DetectorOptions,
    ISACRequest,
    SensingSimulateRequest,
    SensingTrackOptions,
    metadata_dump,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.channel_npz_export import local_frame_matrix
from seam_studio.services.detector import (
    NoiseReference,
    detector_metadata,
    monte_carlo_pd,
    noise_reference,
    noise_seed,
    pd_swerling,
    snr_for_pd,
)
from seam_studio.services.sensing import COLOCATED_SENSING_RX_M, ResolvedSensingTarget
from seam_studio.services.sensing_track import detection_constants
from seam_studio.services.simulation_backends.base import (
    UNSAVED_RESULT_ID,
    RayTracingBackend,
)
from seam_studio.services.simulation_backends.sionna_backend import (
    _steering_from_positions,
    noise_floor_dbm,
)

# |h| below this is no channel (log of zero).
_MIN_AMPLITUDE = 1e-30
# Two trade-off points closer than this on both axes are one point.
_PARETO_TOL = 1e-12
# TX beams x RX beams above this: the echo matrix is built in row chunks.
_ECHO_MATRIX_CHUNK = 1 << 20
# Gauss-Seidel sweeps of the interference-aware comm beam choice.
MAX_INTERFERENCE_ROUNDS = 10
# Pareto points get MC streams [seed, tx, offset + point]; beams [seed, tx, beam].
_POINT_STREAM_OFFSET = 1_000_000
ISAC_MODEL = "path-synthesized azimuth DFT codebook (sionna synthetic-array convention)"
ISAC_MODEL_2D = (
    "path-synthesized azimuth x elevation DFT codebook (sionna synthetic-array convention)"
)
INTERFERENCE_MODEL = (
    "synchronized slots: comm slots see the other TXs' comm beams, sensing "
    "slots their sensing beams"
)


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


def codebook_weights_2d(
    pos: np.ndarray, az_list: Sequence[float], el_list: Sequence[float]
) -> np.ndarray:
    """[n_el * n_az, N] unit-norm beams, elevation outer (k = i_el * n_az +
    i_az): w_n = exp(+j 2 pi p_n . k) / sqrt(N) toward the panel-local
    direction k = [cos el cos az, cos el sin az, sin el] (array_response's
    convention). On a planar_positions grid (x = 0) this is
    kron(w_z(el), w_y(az, el)); at el = 0 a row is codebook_weights' beam."""
    n = pos.shape[1]
    if not az_list or not el_list:
        return np.zeros((0, n), dtype=complex)
    az = np.radians(np.asarray(az_list, dtype=float))[None, :, None]
    el = np.radians(np.asarray(el_list, dtype=float))[:, None, None]
    phase = 2.0 * np.pi * (
        pos[0] * (np.cos(el) * np.cos(az))
        + pos[1] * (np.cos(el) * np.sin(az))
        + pos[2] * np.sin(el)
    )
    return (np.exp(1j * phase) / math.sqrt(n)).reshape(-1, n)


@dataclass(frozen=True)
class Codebook:
    """Per-beam local azimuth / elevation (elevation None: azimuth-only)."""

    az_axis: list[float]
    el_axis: Optional[list[float]]

    @property
    def az(self) -> list[float]:
        n_el = len(self.el_axis) if self.el_axis is not None else 1
        return [a for _ in range(n_el) for a in self.az_axis]

    @property
    def el(self) -> Optional[list[float]]:
        if self.el_axis is None:
            return None
        return [e for e in self.el_axis for _ in self.az_axis]

    def __len__(self) -> int:
        return len(self.az_axis) * (len(self.el_axis) if self.el_axis is not None else 1)

    def weights(self, pos: np.ndarray) -> np.ndarray:
        if self.el_axis is None:
            return codebook_weights(pos, self.az_axis)
        return codebook_weights_2d(pos, self.az_axis, self.el_axis)


def request_codebook(request: ISACRequest) -> Codebook:
    az = codebook_angles(request.sweep_start_deg, request.sweep_stop_deg, request.sweep_step_deg)
    if not request.elevation_sweep():
        return Codebook(az, None)
    el = codebook_angles(
        float(request.elevation_start_deg),  # type: ignore[arg-type]
        float(request.elevation_stop_deg),  # type: ignore[arg-type]
        float(request.elevation_step_deg),  # type: ignore[arg-type]
    )
    return Codebook(az, el)


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


def echo_best_rx(
    echoes: Sequence[RayPath],
    tx: Device,
    rx: Device,
    pos_tx: np.ndarray,
    pos_rx: np.ndarray,
    w_tx: np.ndarray,
    w_rx: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """(max_m |E[k, m]|, argmax_m) per TX beam k of echo_beam_matrix. Above
    _ECHO_MATRIX_CHUNK beam pairs (2-D codebooks) the matrix is built in row
    chunks of the same per-element sums instead of all at once."""
    k_tx, k_rx = w_tx.shape[0], w_rx.shape[0]
    if k_tx * k_rx <= _ECHO_MATRIX_CHUNK:
        mag = np.abs(echo_beam_matrix(echoes, tx, rx, pos_tx, pos_rx, w_tx, w_rx))
        idx = np.argmax(mag, axis=1)
        return mag[np.arange(k_tx), idx], idx
    conj_t, conj_r = np.conj(w_tx), np.conj(w_rx)
    terms = [
        (
            path_alpha(e, tx.power_dbm),
            conj_t @ array_response(pos_tx, tx.orientation_deg, e.aod_deg),
            conj_r @ array_response(pos_rx, rx.orientation_deg, e.aoa_deg),
        )
        for e in echoes
    ]
    best = np.empty(k_tx)
    idx = np.empty(k_tx, dtype=np.int64)
    rows = max(1, _ECHO_MATRIX_CHUNK // k_rx)
    for start in range(0, k_tx, rows):
        stop = min(start + rows, k_tx)
        block = np.zeros((stop - start, k_rx), dtype=complex)
        for alpha, g_tx, g_rx in terms:
            block += alpha * np.outer(g_tx[start:stop], g_rx)
        mag = np.abs(block)
        idx[start:stop] = np.argmax(mag, axis=1)
        best[start:stop] = mag[np.arange(stop - start), idx[start:stop]]
    return best, idx


def _db20(x: complex | float) -> Optional[float]:
    mag = abs(x)
    return 20.0 * math.log10(mag) if mag >= _MIN_AMPLITUDE else None


def _rate(sinr_db: Optional[float]) -> float:
    return math.log2(1.0 + 10.0 ** (sinr_db / 10.0)) if sinr_db is not None else 0.0


def _sinr_db(
    rss_dbm: Optional[float], noise_dbm: float, interference_mw: Optional[float]
) -> Optional[float]:
    """SINR [dB]; no interferer term (None) is the plain SNR, rss - N."""
    if rss_dbm is None:
        return None
    if interference_mw is None:
        return rss_dbm - noise_dbm
    return rss_dbm - 10.0 * math.log10(10.0 ** (noise_dbm / 10.0) + interference_mw)


def _dbm(mw: Optional[float]) -> Optional[float]:
    return 10.0 * math.log10(mw) if mw is not None and mw > 0.0 else None


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
    *,
    model: DetectorModel = "swerling1",
    sensing_slot_rates: Optional[Sequence[dict[str, float]]] = None,
) -> list[ISACPoint]:
    """Operating points of one tx, Pareto-flagged: rho ascending, and within
    rho > 0 the sensing beam k ascending. A fraction rho of the slots (or
    pulses) senses with beam k, so the echo integrates rho * cpi_pulses
    (``beam_sensing_snr_db`` is the full-CPI SNR). time_sharing: the UEs get
    the comm beam in the other 1 - rho; dual_function: they are also served
    by beam k while it senses, at ``sensing_slot_rates[k]`` (default
    ``beam_ue_rates[k]``; differs under interference, where the other txs
    radiate their sensing beams in those slots)."""
    comm_rates = {
        u: (beam_ue_rates[comm_beam_idx][u] if comm_beam_idx is not None else 0.0)
        for u in ue_ids
    }
    slot_rates = beam_ue_rates if sensing_slot_rates is None else sensing_slot_rates
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
                    u: (1.0 - rho) * comm_rates[u] + rho * slot_rates[k][u]
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
                    pd=pd_swerling(snr, pfa, model),
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
    return snr_for_pd(pd, pfa, "swerling1")


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
    # Target id -> [K] full-CPI echo SNR and best RX beam (azimuth, and
    # elevation on a 2-D codebook) per TX beam.
    target_snr: dict[str, list[Optional[float]]]
    target_rx_angle: dict[str, list[Optional[float]]]
    target_rx_elevation: dict[str, list[Optional[float]]]
    target_single: dict[str, Optional[float]]


def _tx_channels(
    tx: Device,
    rx: Device,
    request: ISACRequest,
    codebook: Codebook,
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
    w_tx = codebook.weights(pos_tx)
    w_rx = codebook.weights(pos_rx)
    az, el = codebook.az, codebook.el
    n_beams = len(codebook)
    integration_db = 10.0 * math.log10(request.cpi_pulses)

    rss: dict[str, list[Optional[float]]] = {}
    ue_single: dict[str, Optional[float]] = {}
    for ue in ues:
        paths = comm.get((tx.id, ue.id), [])
        if not paths:
            rss[ue.id] = [None] * n_beams
            ue_single[ue.id] = None
            continue
        gains = [_db20(v) for v in comm_beam_channel(paths, tx, pos_tx, w_tx)]
        rss[ue.id] = [tx.power_dbm + g if g is not None else None for g in gains]
        single = _db20(sum((path_alpha(p, tx.power_dbm) for p in paths), 0j))
        ue_single[ue.id] = tx.power_dbm + single if single is not None else None

    target_snr: dict[str, list[Optional[float]]] = {}
    target_rx_angle: dict[str, list[Optional[float]]] = {}
    target_rx_elevation: dict[str, list[Optional[float]]] = {}
    target_single: dict[str, Optional[float]] = {}
    for t in targets:
        mine = echoes.get((tx.id, rx.id, t.actor_id), [])
        snr: list[Optional[float]] = [None] * n_beams
        rx_angle: list[Optional[float]] = [None] * n_beams
        rx_elevation: list[Optional[float]] = [None] * n_beams
        if mine:
            best, best_rx = echo_best_rx(mine, tx, rx, pos_tx, pos_rx, w_tx, w_rx)
            for k in range(n_beams):
                m = int(best_rx[k])
                db = _db20(float(best[k]))
                if db is not None:
                    snr[k] = tx.power_dbm + db - noise_dbm + integration_db
                    rx_angle[k] = az[m]
                    if el is not None:
                        rx_elevation[k] = el[m]
        target_snr[t.actor_id] = snr
        target_rx_angle[t.actor_id] = rx_angle
        target_rx_elevation[t.actor_id] = rx_elevation
        single = _db20(sum((path_alpha(e, tx.power_dbm) for e in mine), 0j))
        target_single[t.actor_id] = (
            tx.power_dbm + single - noise_dbm + integration_db
            if mine and single is not None
            else None
        )
    return _TxChannels(
        tx=tx, rx=rx, rss=rss, ue_single=ue_single,
        target_snr=target_snr, target_rx_angle=target_rx_angle,
        target_rx_elevation=target_rx_elevation, target_single=target_single,
    )


def _target_snrs(
    ch: _TxChannels, target_ids: list[str], k: int
) -> tuple[dict[str, Optional[float]], Optional[float]]:
    """Per-target echo SNR of TX beam k and the weakest (None if any target
    has no echo)."""
    per_target = {q: ch.target_snr[q][k] for q in target_ids}
    weakest = (
        min(per_target.values())  # type: ignore[type-var]
        if per_target and all(v is not None for v in per_target.values())
        else None
    )
    return per_target, weakest


# --------------------------------------------------------- interference


@dataclass(frozen=True)
class _Interference:
    """One tx's view under request.interference (synchronized slots)."""

    # Per served UE: interference [mW] while the other txs radiate their comm
    # beams (comm slots) / sensing beams (sensing slots); None = no term.
    comm_mw: dict[str, Optional[float]]
    sens_mw: dict[str, Optional[float]]
    # The tx's comm beam from the best-response iteration.
    comm_beam_idx: Optional[int]


def _interference_mw(
    ue_id: str, tx_id: str, channels: list[_TxChannels], beam_of: dict[str, Optional[int]]
) -> Optional[float]:
    """Sum over the other txs of their RSS at the UE with the beam they
    radiate (None beam = silent)."""
    terms = [
        10.0 ** (v / 10.0)
        for ch in channels
        if ch.tx.id != tx_id
        and (k := beam_of.get(ch.tx.id)) is not None
        and (v := ch.rss[ue_id][k]) is not None
    ]
    return sum(terms) if terms else None


def _best_comm_beam(
    ch: _TxChannels,
    served: list[str],
    n_beams: int,
    noise_dbm: float,
    interference_mw: dict[str, Optional[float]],
) -> Optional[int]:
    if not served or not any(v is not None for u in served for v in ch.rss[u]):
        return None
    sums = [
        sum(_rate(_sinr_db(ch.rss[u][k], noise_dbm, interference_mw.get(u))) for u in served)
        for k in range(n_beams)
    ]
    return int(np.argmax(sums))


def _interference_plan(
    channels: list[_TxChannels],
    served: dict[str, list[str]],
    sensing_beam: dict[str, Optional[int]],
    n_beams: int,
    noise_dbm: float,
) -> tuple[dict[str, _Interference], int, bool]:
    """Comm beams under mutual interference: round 0 is the interference-free
    choice, then Gauss-Seidel best response in tx order (each tx maximizes
    its own sum SINR rate given the others' current comm beams) until a sweep
    changes nothing, at most MAX_INTERFERENCE_ROUNDS sweeps. Sensing beams
    do not depend on interference."""
    comm = {
        ch.tx.id: _best_comm_beam(ch, served[ch.tx.id], n_beams, noise_dbm, {})
        for ch in channels
    }
    rounds, converged = 0, False
    while rounds < MAX_INTERFERENCE_ROUNDS:
        rounds += 1
        changed = False
        for ch in channels:
            t = ch.tx.id
            i_comm = {u: _interference_mw(u, t, channels, comm) for u in served[t]}
            new = _best_comm_beam(ch, served[t], n_beams, noise_dbm, i_comm)
            if new != comm[t]:
                comm[t] = new
                changed = True
        if not changed:
            converged = True
            break
    plan = {
        ch.tx.id: _Interference(
            comm_mw={u: _interference_mw(u, ch.tx.id, channels, comm) for u in served[ch.tx.id]},
            sens_mw={
                u: _interference_mw(u, ch.tx.id, channels, sensing_beam)
                for u in served[ch.tx.id]
            },
            comm_beam_idx=comm[ch.tx.id],
        )
        for ch in channels
    }
    return plan, rounds, converged


# ---------------------------------------------------------- per-tx result


def _monte_carlo(
    beams: list[ISACBeam],
    points: list[ISACPoint],
    request: ISACRequest,
    tx_index: int,
    ref: NoiseReference,
) -> None:
    """pd_mc of every beam with an echo and of every Pareto point with rho > 0
    (streams [seed, tx, beam] / [seed, tx, 1e6 + point]) against the
    request's shared threshold."""
    det = request.detector

    def mc(snr_db: float, pulses: int, stream: int) -> float:
        return monte_carlo_pd(
            snr_db, request.pfa, det.monte_carlo_trials, det.model,
            seed=[det.seed, tx_index, stream], cpi_pulses=pulses,
            empirical_threshold=det.empirical_threshold,
            threshold=ref.threshold, measure_pfa=False,
        ).pd

    for k, beam in enumerate(beams):
        if beam.sensing_snr_db is not None:
            beam.pd_mc = mc(beam.sensing_snr_db, request.cpi_pulses, k)
    for i, p in enumerate(points):
        if p.pareto and p.rho > 0.0 and p.sensing_snr_db is not None:
            pulses = max(1, round(p.rho * request.cpi_pulses))
            p.pd_mc = mc(p.sensing_snr_db, pulses, _POINT_STREAM_OFFSET + i)


def _tx_result(
    ch: _TxChannels,
    served: list[str],
    targets: list[ResolvedSensingTarget],
    request: ISACRequest,
    codebook: Codebook,
    ratios: list[float],
    noise_dbm: float,
    *,
    tx_index: int = 0,
    interference: Optional[_Interference] = None,
    mc_ref: Optional[NoiseReference] = None,
) -> ISACTxResult:
    target_ids = [t.actor_id for t in targets]
    rx_rows = request.rx_rows if request.rx_rows is not None else request.tx_rows
    rx_cols = request.rx_cols if request.rx_cols is not None else request.tx_cols
    az, el = codebook.az, codebook.el
    model = request.detector.model
    beams: list[ISACBeam] = []
    beam_rates: list[dict[str, float]] = []
    beam_snr: list[Optional[float]] = []
    slot_rates: Optional[list[dict[str, float]]] = [] if interference is not None else None
    for k, angle in enumerate(az):
        snr_u = {
            u: (ch.rss[u][k] - noise_dbm) if ch.rss[u][k] is not None else None
            for u in served
        }
        if interference is None:
            sinr = snr_u
        else:
            sinr = {
                u: _sinr_db(ch.rss[u][k], noise_dbm, interference.comm_mw[u]) for u in served
            }
            slot_rates.append(  # type: ignore[union-attr]
                {
                    u: _rate(_sinr_db(ch.rss[u][k], noise_dbm, interference.sens_mw[u]))
                    for u in served
                }
            )
        rates = {u: _rate(sinr[u]) for u in served}
        per_target, weakest = _target_snrs(ch, target_ids, k)
        beam_rates.append(rates)
        beam_snr.append(weakest)
        beams.append(
            ISACBeam(
                angle_deg=angle,
                elevation_deg=el[k] if el is not None else None,
                ue_sinr_db=sinr,
                ue_snr_db=snr_u if interference is not None else None,
                ue_interference_dbm=(
                    {u: _dbm(interference.comm_mw[u]) for u in served}
                    if interference is not None
                    else None
                ),
                sum_rate_bps_hz=sum(rates[u] for u in served),
                target_snr_db=per_target,
                target_best_rx_angle_deg={q: ch.target_rx_angle[q][k] for q in target_ids},
                target_best_rx_elevation_deg=(
                    {q: ch.target_rx_elevation[q][k] for q in target_ids}
                    if el is not None
                    else None
                ),
                sensing_snr_db=weakest,
                pd=pd_swerling(weakest, request.pfa, model),
                detected=weakest is not None and weakest >= request.threshold_db,
            )
        )

    warnings: list[str] = []
    if interference is not None:
        comm_idx = interference.comm_beam_idx
    else:
        has_path = any(v is not None for u in served for v in ch.rss[u])
        comm_idx = (
            int(np.argmax([b.sum_rate_bps_hz for b in beams])) if served and has_path else None
        )
    if not served:
        warnings.append(f"tx {ch.tx.id} serves no UE: sensing-only trade-off")
    sensing_idx = _argmax_optional(beam_snr)
    comm_angle = az[comm_idx] if comm_idx is not None else None
    sensing_angle = az[sensing_idx] if sensing_idx is not None else None
    comm_el = el[comm_idx] if el is not None and comm_idx is not None else None
    sensing_el = el[sensing_idx] if el is not None and sensing_idx is not None else None
    points = tradeoff_points(
        served, beam_rates, comm_idx, beam_snr, ratios, request.sharing_mode, request.pfa,
        model=model, sensing_slot_rates=slot_rates,
    )
    if mc_ref is not None:
        _monte_carlo(beams, points, request, tx_index, mc_ref)
    comm_only = beams[comm_idx].sum_rate_bps_hz if comm_idx is not None else 0.0
    return ISACTxResult(
        tx_id=ch.tx.id,
        sensing_rx_id=ch.rx.id,
        sensing_baseline_m=math.dist(ch.tx.position, ch.rx.position),
        ue_ids=list(served),
        target_ids=target_ids,
        tx_array=[request.tx_rows, request.tx_cols],
        rx_array=[rx_rows, rx_cols],
        angles_deg=list(az),
        elevations_deg=list(el) if el is not None else None,
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
        comm_beam_elevation_deg=comm_el,
        sensing_beam_elevation_deg=sensing_el,
        elevation_gap_deg=(
            abs(comm_el - sensing_el) if comm_el is not None and sensing_el is not None else None
        ),
        ue_interference_sensing_dbm=(
            {u: _dbm(interference.sens_mw[u]) for u in served}
            if interference is not None
            else None
        ),
        ue_single_element_rss_dbm={u: ch.ue_single[u] for u in served},
        target_single_element_snr_db=dict(ch.target_single),
        points=points,
        pareto=pareto_summary(points, comm_only, request.pd_target, request.pfa),
        warnings=warnings,
    )


def isac_mc_estimates(request: ISACRequest, n_tx: int) -> int:
    """Upper bound on the Pd Monte Carlo estimates of a run: every beam and
    every (rho > 0, beam) point of every tx (only Pareto points run)."""
    n_az, n_el = request.codebook_size()
    n_ratios = len({r for r in request.slot_ratios if r > 0.0})
    return n_tx * n_az * n_el * (1 + n_ratios)


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
    codebook = request_codebook(request)
    ratios = sorted(set(float(r) for r in request.slot_ratios))
    channels = [
        _tx_channels(
            t, plan.sensing_rx[t.id], request, codebook, plan.ues, targets,
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
    served = {
        ch.tx.id: (
            list(ue_ids)
            if request.ue_association == "all"
            else [u for u in ue_ids if ue_serving_tx[u] == ch.tx.id]
        )
        for ch in channels
    }

    target_ids = [t.actor_id for t in targets]
    interference: dict[str, _Interference] = {}
    extra_metadata: dict = {}
    if request.interference:
        sensing_beam = {
            ch.tx.id: _argmax_optional(
                [_target_snrs(ch, target_ids, k)[1] for k in range(len(codebook))]
            )
            for ch in channels
        }
        interference, rounds, converged = _interference_plan(
            channels, served, sensing_beam, len(codebook), noise_dbm
        )
        if not converged:
            warnings.append(
                f"interference: the comm beam best response did not settle in "
                f"{MAX_INTERFERENCE_ROUNDS} rounds; the last round's beams are reported"
            )
        extra_metadata.update(
            interference=True,
            ue_interferers={
                u: [
                    ch.tx.id for ch in channels
                    if ch.tx.id != ue_serving_tx[u]
                    and ue_serving_tx[u] is not None
                    and any(v is not None for v in ch.rss[u])
                ]
                for u in ue_ids
            },
            interference_rounds=rounds,
            interference_converged=converged,
            interference_model=INTERFERENCE_MODEL,
        )

    det = request.detector
    mc_ref: Optional[NoiseReference] = None
    if det.monte_carlo_trials > 0:
        mc_ref = noise_reference(
            request.pfa, det.monte_carlo_trials, noise_seed(det.seed),
            request.cpi_pulses, det.empirical_threshold,
        )
    if det != DetectorOptions():
        extra_metadata["detector"] = detector_metadata(
            det.model, request.pfa, det.monte_carlo_trials, mc_ref
        )

    txs = [
        _tx_result(
            ch, served[ch.tx.id], targets, request, codebook, ratios, noise_dbm,
            tx_index=i, interference=interference.get(ch.tx.id), mc_ref=mc_ref,
        )
        for i, ch in enumerate(channels)
    ]

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
        "request": metadata_dump(request, exclude=("config",)),
        **consts,
        "codebook_size": len(codebook),
        "echo_path_count": len(echo_paths),
        "comm_path_count": len(comm_paths),
        "snr_for_pd_target_db": snr_for_pd(request.pd_target, request.pfa, det.model),
        "pd_at_threshold": pd_swerling(request.threshold_db, request.pfa, det.model),
        "model": ISAC_MODEL if codebook.el_axis is None else ISAC_MODEL_2D,
    }
    if codebook.el_axis is not None:
        # Beams enumerate elevation outer, azimuth inner.
        metadata["codebook_shape"] = [len(codebook.el_axis), len(codebook.az_axis)]
    metadata.update(extra_metadata)
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
