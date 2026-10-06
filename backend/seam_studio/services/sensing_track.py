"""Per-frame sensing for scenarios: echo -> link detection (SNR, MTI) ->
multistatic fusion (bistatic ranges, Doppler).

Every scenario frame with ``ScenarioSimulateRequest.sensing.enabled`` runs one
sensing solve at the frame's actor poses/velocities and rider positions. For
each TX -> RX link and target the strongest direct echo (interactions ==
["sensing"]) is tested against a noise-limited threshold

    snr_db = echo_power_dbm - noise_floor_dbm + 10*log10(cpi_pulses)

and an MTI notch (|f_D - f_nodes| >= mti_min_doppler_hz, where f_nodes is the
Doppler TX/RX motion alone gives the echo's path: 0 for static nodes). The
detected, geometrically
distinct direct echoes of a target are fused: Gauss-Newton on the bistatic
range sums |p - t_i| + |p - s_i| = R_i gives the position, then a linear
least-squares fit of the Doppler gives the velocity, with Sionna's sign
convention (positive = closing):

    lambda * f_D = v_tx . k_ts - v_rx . k_sr + v_t . (k_sr - k_ts)

where k_ts = unit(TX -> target) and k_sr = unit(target -> RX).

With ``sensing.tracking.enabled`` a constant-velocity EKF per target runs on
top: it starts (and, after a manoeuvre its prediction cannot explain,
restarts) from an ok fusion, predicts over each frame's dt, and updates
with every detected direct link's range sum and Doppler one scalar at a time
(chi-square gated), so frames with one or two links still refine the track.
A track is dropped after too many frames without an accepted scalar or once
its position sigma exceeds ``max_position_std_m``.

Association is an oracle: the ray tracer labels each echo with its target and
direct/multipath type, which a real receiver would have to infer.
"""

import math
import random
import statistics
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from seam_studio.schemas.actors import ActorState, ScenarioFrame
from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import (
    RayPath,
    SensingFrame,
    SensingLinkReport,
    SensingNodeState,
    TargetEstimate,
)
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.sensing import (
    SensingSimulateRequest,
    SensingTrackOptions,
    TrackingOptions,
    metadata_dump,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.detector import monte_carlo_pd, pd_swerling
from seam_studio.services.scenario import _frame_scene
from seam_studio.services.sensing import (
    ResolvedSensingTarget,
    geometry_groups,
    resolve_target_state,
)
from seam_studio.services.simulation_backends.base import RayTracingBackend
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm

SPEED_OF_LIGHT = 299_792_458.0
GN_MAX_ITER = 50
GN_STEP_TOL_M = 1e-7
GN_MIN_STEP_SCALE = 1.0 / 1024.0
COND_MAX = 1e8  # J or B condition number above this: degenerate geometry
ROOT_SEPARATION_M = 1.0  # converged candidates closer than this are one root
GROUND_TOL_M = 2.0  # a 3-link root more than this below z = 0 is underground
GROUND_KINDS = ("car", "human")  # actor kinds whose fusion tie goes toward z = 0

# Floor for the linear SNR in the measurement-noise sigma (-300 dB), so an
# absurdly weak echo cannot divide by zero.
_MIN_SNR_LIN = 1e-30
# Floor of the EKF measurement sigmas (m, Hz): a very strong echo must not
# make the innovation variance vanish.
_MIN_TRACK_SIGMA = 1e-3

TRACKING_MODEL = (
    "CV EKF, DWNA process noise, sequential scalar range/Doppler updates, chi2(1) gate"
)
TRACKED_STATUSES = ("init", "tracking", "coasting")

# Node id -> (position, velocity) in one frame.
Nodes = dict[str, tuple[list[float], list[float]]]
# (position_est, velocity_est or None, time_s) of a target's last ok frame.
Prior = tuple[list[float], Optional[list[float]], float]


@dataclass(frozen=True)
class FusionResult:
    position: Optional[list[float]]
    status: str  # "ok" | "insufficient_links" | "diverged"
    iterations: int
    gdop: Optional[float]
    rms_residual_m: Optional[float]


def is_direct_echo(path: RayPath) -> bool:
    """TX -> scattering point -> RX with no other interaction."""
    return [i.type for i in path.interactions] == ["sensing"]


def detection_constants(config: SimulationConfig, options: SensingTrackOptions) -> dict:
    """Run-wide detection constants (also the head of metadata["sensing"])."""
    bandwidth = float(config.bandwidth_hz)
    wavelength = SPEED_OF_LIGHT / float(config.frequency_hz)
    f_min = float(options.resolved_mti_min_doppler_hz())
    return {
        "frequency_hz": float(config.frequency_hz),
        "wavelength_m": wavelength,
        "bandwidth_hz": bandwidth,
        "noise_figure_db": float(config.noise_figure_db),
        "noise_floor_dbm": noise_floor_dbm(config),
        "integration_gain_db": 10.0 * math.log10(options.cpi_pulses),
        "threshold_db": float(options.threshold_db),
        "cpi_s": float(options.cpi_s),
        "mti_min_doppler_hz": f_min,
        "range_resolution_m": SPEED_OF_LIGHT / (2.0 * bandwidth),
        "bistatic_range_resolution_m": SPEED_OF_LIGHT / bandwidth,
        "doppler_resolution_hz": 1.0 / options.cpi_s,
        "velocity_resolution_m_s": wavelength / (2.0 * options.cpi_s),
        "mti_blind_speed_m_s": wavelength * f_min / 2.0,
    }


def swerling1_pd(snr_db: Optional[float], pfa: float) -> float:
    """Detection probability of a Swerling-1 target with a square-law detector
    on the coherently integrated sample: Pfa = e^-T and Pd = e^(-T / (1 + SNR))
    give Pd = Pfa^(1 / (1 + SNR)). No echo (None) detects at the false-alarm
    rate."""
    if snr_db is None:
        return pfa
    return pfa ** (1.0 / (1.0 + 10.0 ** (snr_db / 10.0)))


def _node_doppler_hz(
    path: RayPath,
    tx: tuple[list[float], list[float]],
    rx: tuple[list[float], list[float]],
    wavelength_m: float,
) -> float:
    """Doppler [Hz] of ``path`` from the TX/RX motion alone (every scatterer at
    rest): (v_tx . k_dep - v_rx . k_arr) / lambda, with k_dep = unit(TX -> first
    interaction) and k_arr = unit(last interaction -> RX)."""
    (t_pos, t_vel), (r_pos, r_vel) = tx, rx
    k_dep = _unit(np.asarray(path.interactions[0].point, dtype=float) - np.asarray(t_pos))
    k_arr = _unit(np.asarray(r_pos, dtype=float) - np.asarray(path.interactions[-1].point))
    return (float(np.dot(t_vel, k_dep)) - float(np.dot(r_vel, k_arr))) / wavelength_m


def link_report(
    tx_id: str,
    rx_id: str,
    target_id: str,
    echoes: list[RayPath],
    consts: dict,
    options: SensingTrackOptions,
    frame_index: int,
    nodes: Optional[Nodes] = None,
) -> SensingLinkReport:
    """Detection of ``target_id`` on one link from the frame's echoes.

    ``nodes`` (the frame's TX/RX positions and velocities) moves the MTI
    notch onto the static scene's Doppler: with a TX or RX riding a moving
    actor a static scatterer is not at 0 Hz, so the notch tests the echo's
    Doppler minus the node-motion Doppler of its own path."""
    mine = [
        p for p in echoes
        if p.tx_id == tx_id and p.rx_id == rx_id and p.target_id == target_id
    ]
    direct = [p for p in mine if is_direct_echo(p)]
    pool = direct or mine
    if not pool:
        return SensingLinkReport(tx_id=tx_id, rx_id=rx_id, target_id=target_id)
    pick = max(pool, key=lambda p: p.power_dbm)

    bistatic_range = SPEED_OF_LIGHT * pick.delay_ns * 1e-9
    doppler = pick.doppler_hz
    snr_db = pick.power_dbm - consts["noise_floor_dbm"] + consts["integration_gain_db"]
    target_doppler = doppler or 0.0
    if doppler is not None and nodes is not None and pick.interactions:
        tx, rx = nodes[tx_id], nodes[rx_id]
        if any(tx[1]) or any(rx[1]):  # static nodes: static scene sits at 0 Hz
            target_doppler = doppler - _node_doppler_hz(pick, tx, rx, consts["wavelength_m"])
    # Decisions use the exact solver values, never the noisy measurements.
    if snr_db < options.threshold_db:
        reason = "below_threshold"
    elif abs(target_doppler) < consts["mti_min_doppler_hz"]:
        reason = "mti_rejected"
    else:
        reason = "detected"

    measured_range = bistatic_range
    measured_doppler = doppler
    if options.measurement_noise:
        # A str seed hashes deterministically (unlike hash()) across processes.
        rng = random.Random(
            f"{options.noise_seed}:{frame_index}:{tx_id}:{rx_id}:{target_id}"
        )
        snr_lin = max(10.0 ** (snr_db / 10.0), _MIN_SNR_LIN)
        sigma_r = consts["bistatic_range_resolution_m"] / math.sqrt(2.0 * snr_lin)
        sigma_f = consts["doppler_resolution_hz"] / math.sqrt(2.0 * snr_lin)
        measured_range = bistatic_range + rng.gauss(0.0, sigma_r)  # range draw first
        doppler_noise = rng.gauss(0.0, sigma_f)
        if doppler is not None:
            measured_doppler = doppler + doppler_noise

    return SensingLinkReport(
        tx_id=tx_id,
        rx_id=rx_id,
        target_id=target_id,
        path_id=pick.path_id,
        num_echoes=len(mine),
        multipath=not direct,
        bistatic_range_m=bistatic_range,
        doppler_hz=doppler,
        echo_power_dbm=pick.power_dbm,
        snr_db=snr_db,
        measured_range_m=measured_range,
        measured_doppler_hz=measured_doppler,
        range_bin=math.floor(measured_range / consts["bistatic_range_resolution_m"]),
        doppler_bin=(
            round(measured_doppler * consts["cpi_s"]) if measured_doppler is not None else None
        ),
        detected=reason == "detected",
        reason=reason,
    )


def with_link_pd(
    report: SensingLinkReport,
    options: SensingTrackOptions,
    frame_index: int,
    link_index: int,
) -> SensingLinkReport:
    """``report`` with its Pd at ``options.pfa`` under the detector model, and
    a Monte Carlo of it (seeded by frame and link) when trials > 0 and there
    is an echo. Detection itself stays the snr_db >= threshold_db test."""
    if options.pfa is None:
        return report
    det = options.detector
    pd = float(pd_swerling(report.snr_db, options.pfa, det.model))
    pd_mc: Optional[float] = None
    if det.monte_carlo_trials > 0 and report.snr_db is not None:
        pd_mc = float(
            monte_carlo_pd(
                report.snr_db,
                options.pfa,
                det.monte_carlo_trials,
                det.model,
                seed=[det.seed, frame_index, link_index],
                cpi_pulses=options.cpi_pulses,
                empirical_threshold=det.empirical_threshold,
                measure_pfa=False,
            ).pd
        )
    return report.model_copy(update={"pd": pd, "pd_mc": pd_mc})


# ------------------------------------------------------------------ fusion


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.linalg.norm(v))
    return v / n if n > 1e-12 else np.zeros(3)


def _cond(m: np.ndarray) -> float:
    try:
        c = float(np.linalg.cond(m))
    except np.linalg.LinAlgError:
        return math.inf
    return c if math.isfinite(c) else math.inf


class _Ellipsoids:
    """Bistatic range-sum residuals r_i(p) = |p - t_i| + |p - s_i| - R_i."""

    def __init__(self, tx: np.ndarray, rx: np.ndarray, ranges: np.ndarray):
        self.tx, self.rx, self.ranges = tx, rx, ranges

    def residuals(self, p: np.ndarray) -> np.ndarray:
        return (
            np.linalg.norm(p - self.tx, axis=1)
            + np.linalg.norm(p - self.rx, axis=1)
            - self.ranges
        )

    def cost(self, p: np.ndarray) -> float:
        r = self.residuals(p)
        return 0.5 * float(r @ r)

    def jacobian(self, p: np.ndarray) -> np.ndarray:
        jac = np.zeros((len(self.ranges), 3))
        for i in range(len(self.ranges)):
            for focus in (self.tx[i], self.rx[i]):
                d = p - focus
                n = float(np.linalg.norm(d))
                if n >= 1e-9:
                    jac[i] += d / n
        return jac

    def gauss_newton(self, p0: np.ndarray) -> tuple[np.ndarray, float, int, bool]:
        """(p, F, iterations, converged) with a halving line search."""
        p = np.array(p0, dtype=float)
        f = self.cost(p)
        for it in range(1, GN_MAX_ITER + 1):
            r = self.residuals(p)
            jac = self.jacobian(p)
            try:
                delta = np.linalg.lstsq(jac, -r, rcond=None)[0]
            except np.linalg.LinAlgError:
                return p, f, it, False
            alpha = 1.0
            f_new = self.cost(p + alpha * delta)
            while f_new > f and alpha >= GN_MIN_STEP_SCALE:
                alpha /= 2.0
                f_new = self.cost(p + alpha * delta)
            if not f_new <= f:  # no descent (or NaN): stationary iff the gradient vanished
                return p, f, it, float(np.linalg.norm(jac.T @ r)) < 1e-6
            step = alpha * delta
            p = p + step
            f = f_new
            if float(np.linalg.norm(step)) < GN_STEP_TOL_M or f < 1e-20:
                return p, f, it, True
        return p, f, GN_MAX_ITER, False


def solve_bistatic_position(
    tx_pos: Sequence[Sequence[float]],
    rx_pos: Sequence[Sequence[float]],
    ranges_m: Sequence[float],
    *,
    prior: Optional[Sequence[float]] = None,
    max_rms_residual_m: float = math.inf,
    range_sigma_m: float = 0.0,
    ground_target: bool = False,
) -> FusionResult:
    """Target position from >= 3 bistatic range sums (Gauss-Newton).

    Multi-start: ``prior`` (the predicted position, when known), then the
    centroid of the link midpoints shifted up and down by h = max(median R / 4,
    1 m). Rooftop TRPs are nearly coplanar, so the centroid sits where the
    Jacobian's z column vanishes and the ellipsoids have a mirror solution
    about that plane; the off-plane starts land on both. Without a prior,
    every link midpoint + h and the centroid + 2h are tried too: three range
    sums have several exact roots, and two starts often miss the true one.

    Tied set: candidates whose cost is within 1e-9 + 1e-6 F_min of the
    minimum, plus 4.5 ``range_sigma_m``^2 (delta chi^2 <= 9 for that 1-sigma
    range-sum noise): with noisy ranges the mirror often fits slightly better
    by chance. Among them the one nearest ``prior`` wins; else, for a
    ``ground_target``, the one closest to z = 0, otherwise the highest.
    With exactly 3 links and no prior every root fits exactly, so more than
    one distinct tied root not below ground (z >= -GROUND_TOL_M) is
    ambiguous and reported "diverged"."""
    tx = np.asarray(tx_pos, dtype=float).reshape(-1, 3)
    rx = np.asarray(rx_pos, dtype=float).reshape(-1, 3)
    ranges = np.asarray(ranges_m, dtype=float).reshape(-1)
    if len(ranges) < 3:
        return FusionResult(None, "insufficient_links", 0, None, None)
    model = _Ellipsoids(tx, rx, ranges)

    center = ((tx + rx) / 2.0).mean(axis=0)
    h = max(float(np.median(ranges)) / 4.0, 1.0)
    up = np.array([0.0, 0.0, h])
    prior_arr = np.asarray(prior, dtype=float) if prior is not None else None
    starts = [] if prior_arr is None else [prior_arr]
    starts += [center + up, center - up]
    if prior_arr is None:
        starts += [(t + s) / 2.0 + up for t, s in zip(tx, rx)] + [center + 2.0 * up]

    candidates: list[tuple[float, np.ndarray, int]] = []
    for p0 in starts:
        p, f, it, converged = model.gauss_newton(p0)
        if converged and np.all(np.isfinite(p)) and math.isfinite(f):
            candidates.append((f, p, it))
    if not candidates:
        return FusionResult(None, "diverged", 0, None, None)

    f_min = min(c[0] for c in candidates)
    tol = 1e-9 + 1e-6 * f_min + 4.5 * range_sigma_m * range_sigma_m
    tied = [c for c in candidates if c[0] - f_min <= tol]
    ambiguous = False
    if prior_arr is not None:
        f, p, it = min(tied, key=lambda c: float(np.linalg.norm(c[1] - prior_arr)))
    else:
        if len(ranges) == 3:
            tied = [c for c in tied if c[1][2] >= -GROUND_TOL_M] or tied
            roots: list[np.ndarray] = []
            for c in tied:
                if all(float(np.linalg.norm(c[1] - q)) > ROOT_SEPARATION_M for q in roots):
                    roots.append(c[1])
            ambiguous = len(roots) > 1
        if ground_target:
            f, p, it = min(tied, key=lambda c: abs(float(c[1][2])))
        else:
            f, p, it = max(tied, key=lambda c: float(c[1][2]))

    jac = model.jacobian(p)
    gdop: Optional[float] = None
    try:
        g = float(np.trace(np.linalg.inv(jac.T @ jac)))
        if math.isfinite(g) and g >= 0.0:
            gdop = math.sqrt(g)
    except np.linalg.LinAlgError:
        pass
    rms = math.sqrt(2.0 * f / len(ranges))
    position = [float(c) for c in p]
    status = "ok"
    if ambiguous or _cond(jac) > COND_MAX or rms > max_rms_residual_m:
        status = "diverged"
    return FusionResult(position, status, it, gdop, rms)


def solve_doppler_velocity(
    position: Sequence[float],
    tx_pos: Sequence[Sequence[float]],
    rx_pos: Sequence[Sequence[float]],
    doppler_hz: Sequence[Optional[float]],
    wavelength_m: float,
    tx_vel: Optional[Sequence[Sequence[float]]] = None,
    rx_vel: Optional[Sequence[Sequence[float]]] = None,
) -> Optional[list[float]]:
    """Target velocity from the links' Doppler at a known position (linear LS).

    Row i: (k_sr - k_ts) . v = lambda f_D - v_tx . k_ts + v_rx . k_sr. Never
    k_ts + k_sr: that flips the TX leg's sign. None when the rows do not span
    3-D (rank < 3 or ill-conditioned)."""
    p = np.asarray(position, dtype=float)
    rows: list[np.ndarray] = []
    rhs: list[float] = []
    for i, fd in enumerate(doppler_hz):
        if fd is None:
            continue
        k_ts = _unit(p - np.asarray(tx_pos[i], dtype=float))
        k_sr = _unit(np.asarray(rx_pos[i], dtype=float) - p)
        y = wavelength_m * float(fd)
        if tx_vel is not None:
            y -= float(np.dot(np.asarray(tx_vel[i], dtype=float), k_ts))
        if rx_vel is not None:
            y += float(np.dot(np.asarray(rx_vel[i], dtype=float), k_sr))
        rows.append(k_sr - k_ts)
        rhs.append(y)
    if len(rows) < 3:
        return None
    b = np.array(rows)
    if np.linalg.matrix_rank(b) < 3 or _cond(b) > COND_MAX:
        return None
    v = np.linalg.lstsq(b, np.asarray(rhs), rcond=None)[0]
    if not np.all(np.isfinite(v)):
        return None
    return [float(c) for c in v]


def estimate_target(
    target: ResolvedSensingTarget,
    reports: list[SensingLinkReport],
    nodes: Nodes,
    consts: dict,
    prior: Optional[Prior],
    time_s: float,
    *,
    measurement_noise: bool = False,
    ground_target: bool = False,
) -> TargetEstimate:
    """Fuse one target's detected direct echoes into a position/velocity
    estimate scored against the frame's truth. ``measurement_noise`` widens
    the fusion's tie to the expected range noise (exact ranges keep the exact
    root); ``ground_target`` (car, human) breaks a tie toward the ground."""
    mine = [r for r in reports if r.target_id == target.actor_id]
    detected = [r for r in mine if r.detected]
    candidates = sorted(
        (r for r in detected if not r.multipath),
        key=lambda r: (-(r.snr_db or 0.0), r.tx_id, r.rx_id),
    )
    # One link per bistatic ellipsoid (sites within COLOCATED_SENSING_RX_M,
    # either order), the highest-SNR one.
    groups = geometry_groups([(nodes[r.tx_id][0], nodes[r.rx_id][0]) for r in candidates])
    seen: set[int] = set()
    kept: list[SensingLinkReport] = []
    for r, g in zip(candidates, groups):
        if g not in seen:
            seen.add(g)
            kept.append(r)

    position_true = [float(c) for c in target.center]
    velocity_true = [float(c) for c in target.velocity_m_s]
    base = dict(
        target_id=target.actor_id,
        n_links_detected=len(detected),
        n_links_used=len(kept),
        links_used=[f"{r.tx_id}>{r.rx_id}" for r in kept],
        position_true=position_true,
        velocity_true=velocity_true,
    )
    if len(kept) < 3:
        return TargetEstimate(status="insufficient_links", **base)

    predicted: Optional[list[float]] = None
    if prior is not None:
        prior_pos, prior_vel, prior_t = prior
        dt = time_s - prior_t
        predicted = [
            prior_pos[k] + (prior_vel[k] * dt if prior_vel is not None else 0.0)
            for k in range(3)
        ]
    tx_pos = [nodes[r.tx_id][0] for r in kept]
    rx_pos = [nodes[r.rx_id][0] for r in kept]
    # With noisy ranges, fits within delta chi^2 <= 9 of the best under the
    # expected range accuracy (the same cell / sqrt(2 SNR) rule as the
    # measurement noise) are indistinguishable. Exact ranges: no such tie.
    cell = consts["bistatic_range_resolution_m"]
    range_sigma = 0.0
    if measurement_noise:
        sigmas = [
            cell / math.sqrt(2.0 * max(10.0 ** ((r.snr_db or 0.0) / 10.0), _MIN_SNR_LIN))
            for r in kept
        ]
        range_sigma = min(math.sqrt(sum(s * s for s in sigmas) / len(sigmas)), cell / 3.0)
    fusion = solve_bistatic_position(
        tx_pos,
        rx_pos,
        [float(r.measured_range_m) for r in kept],  # type: ignore[arg-type]
        prior=predicted,
        max_rms_residual_m=cell,
        range_sigma_m=range_sigma,
        ground_target=ground_target,
    )
    fused = dict(
        position_est=fusion.position,
        gdop=fusion.gdop,
        rms_residual_m=fusion.rms_residual_m,
        iterations=fusion.iterations,
    )
    if fusion.status != "ok" or fusion.position is None:
        return TargetEstimate(status=fusion.status, **base, **fused)

    velocity = solve_doppler_velocity(
        fusion.position,
        tx_pos,
        rx_pos,
        [r.measured_doppler_hz for r in kept],
        consts["wavelength_m"],
        tx_vel=[nodes[r.tx_id][1] for r in kept],
        rx_vel=[nodes[r.rx_id][1] for r in kept],
    )
    return TargetEstimate(
        status="ok",
        **base,
        **fused,
        velocity_est=velocity,
        position_error_m=math.dist(fusion.position, position_true),
        velocity_error_m_s=(
            math.dist(velocity, velocity_true) if velocity is not None else None
        ),
    )


# ------------------------------------------------------------------ EKF
#
# State x = [p (3), v (3)] in the world frame, constant velocity between
# frames. Measurements are scalars, one range sum and one Doppler per detected
# direct link, applied one at a time and re-linearized at the current state.


@dataclass(frozen=True)
class TrackMeasurement:
    kind: str  # "range" | "doppler"
    value: float
    sigma: float
    tx: tuple[list[float], list[float]]  # (position, velocity)
    rx: tuple[list[float], list[float]]
    link: str  # "tx_id>rx_id"


@dataclass
class TargetTrack:
    x: np.ndarray  # [6]
    P: np.ndarray  # [6, 6]
    time_s: float
    coast: int = 0  # consecutive frames without an accepted measurement


def measurement_sigmas(snr_db: Optional[float], consts: dict) -> tuple[float, float]:
    """(range-sum sigma [m], Doppler sigma [Hz]) of one link: cell / sqrt(2 SNR),
    cell = c/B and 1/CPI, the measurement-noise rule, floored at 1e-3."""
    snr_lin = max(10.0 ** ((snr_db or 0.0) / 10.0), _MIN_SNR_LIN)
    root = math.sqrt(2.0 * snr_lin)
    return (
        max(consts["bistatic_range_resolution_m"] / root, _MIN_TRACK_SIGMA),
        max(consts["doppler_resolution_hz"] / root, _MIN_TRACK_SIGMA),
    )


def cv_predict(
    x: np.ndarray, P: np.ndarray, dt: float, accel_sigma: float
) -> tuple[np.ndarray, np.ndarray]:
    """Constant-velocity prediction over ``dt`` with discrete white noise
    acceleration (piecewise constant over the step, std ``accel_sigma``):
    Q = sigma_a^2 [[dt^4/4 I, dt^3/2 I], [dt^3/2 I, dt^2 I]]."""
    eye = np.eye(3)
    F = np.eye(6)
    F[:3, 3:] = dt * eye
    q = accel_sigma * accel_sigma
    Q = np.zeros((6, 6))
    Q[:3, :3] = q * dt**4 / 4.0 * eye
    Q[:3, 3:] = Q[3:, :3] = q * dt**3 / 2.0 * eye
    Q[3:, 3:] = q * dt * dt * eye
    return F @ x, F @ P @ F.T + Q


def _safe_unit(v: np.ndarray) -> tuple[np.ndarray, float]:
    n = float(np.linalg.norm(v))
    return (v / n, n) if n > 1e-9 else (np.zeros(3), n)


def range_model(
    x: np.ndarray, tx_pos: Sequence[float], rx_pos: Sequence[float]
) -> tuple[float, np.ndarray]:
    """h_R = |p - t| + |p - s| and its Jacobian [u(p - t) + u(p - s), 0]."""
    p = x[:3]
    u_t, n_t = _safe_unit(p - np.asarray(tx_pos, dtype=float))
    u_s, n_s = _safe_unit(p - np.asarray(rx_pos, dtype=float))
    H = np.zeros(6)
    H[:3] = u_t + u_s
    return n_t + n_s, H


def doppler_model(
    x: np.ndarray,
    tx: tuple[Sequence[float], Sequence[float]],
    rx: tuple[Sequence[float], Sequence[float]],
    wavelength_m: float,
) -> tuple[float, np.ndarray]:
    """h_f = (v_t . k_ts - v_s . k_sr + v . (k_sr - k_ts)) / lambda (positive =
    closing, the solve_doppler_velocity row model) and its Jacobian:
    dh/dp = [(I - k_ts k_ts^T)(v_t - v)/|p - t| + (I - k_sr k_sr^T)(v_s - v)/|s - p|] / lambda,
    dh/dv = (k_sr - k_ts) / lambda."""
    p, v = x[:3], x[3:]
    t, v_t = np.asarray(tx[0], dtype=float), np.asarray(tx[1], dtype=float)
    s, v_s = np.asarray(rx[0], dtype=float), np.asarray(rx[1], dtype=float)
    k_ts, r_t = _safe_unit(p - t)
    k_sr, r_s = _safe_unit(s - p)
    h = (float(v_t @ k_ts) - float(v_s @ k_sr) + float(v @ (k_sr - k_ts))) / wavelength_m
    eye = np.eye(3)
    dp = np.zeros(3)
    if r_t > 1e-9:
        dp += (eye - np.outer(k_ts, k_ts)) @ (v_t - v) / r_t
    if r_s > 1e-9:
        dp += (eye - np.outer(k_sr, k_sr)) @ (v_s - v) / r_s
    H = np.zeros(6)
    H[:3] = dp / wavelength_m
    H[3:] = (k_sr - k_ts) / wavelength_m
    return h, H


def scalar_update(
    x: np.ndarray,
    P: np.ndarray,
    z: float,
    h: float,
    H: np.ndarray,
    sigma: float,
    gate_chi2: float,
) -> tuple[np.ndarray, np.ndarray, bool]:
    """One gated scalar EKF update (Joseph form). Rejected (state unchanged)
    when nu^2 / S > gate_chi2, S = H P H^T + sigma^2."""
    nu = float(z) - float(h)
    PHt = P @ H
    S = float(H @ PHt) + sigma * sigma
    if not (math.isfinite(nu) and math.isfinite(S) and S > 0.0) or nu * nu / S > gate_chi2:
        return x, P, False
    K = PHt / S
    A = np.eye(len(x)) - np.outer(K, H)
    P_new = A @ P @ A.T + sigma * sigma * np.outer(K, K)
    return x + K * nu, 0.5 * (P_new + P_new.T), True


def track_measurements(
    reports: list[SensingLinkReport], target_id: str, nodes: Nodes, consts: dict
) -> list[TrackMeasurement]:
    """The target's detected direct links, highest SNR first, as scalars: the
    range sum, then the Doppler (when the echo has one). No geometry dedup:
    two links of one ellipsoid are independent receiver draws."""
    links = sorted(
        (
            r for r in reports
            if r.target_id == target_id and r.detected and not r.multipath
            and r.measured_range_m is not None
        ),
        key=lambda r: (-(r.snr_db or 0.0), r.tx_id, r.rx_id),
    )
    out: list[TrackMeasurement] = []
    for r in links:
        sigma_r, sigma_f = measurement_sigmas(r.snr_db, consts)
        tx, rx = nodes[r.tx_id], nodes[r.rx_id]
        name = f"{r.tx_id}>{r.rx_id}"
        out.append(TrackMeasurement("range", float(r.measured_range_m), sigma_r, tx, rx, name))  # type: ignore[arg-type]
        if r.measured_doppler_hz is not None:
            out.append(
                TrackMeasurement("doppler", float(r.measured_doppler_hz), sigma_f, tx, rx, name)
            )
    return out


def ekf_update(
    x: np.ndarray,
    P: np.ndarray,
    measurements: list[TrackMeasurement],
    wavelength_m: float,
    gate_chi2: float,
) -> tuple[np.ndarray, np.ndarray, int, int]:
    """Sequential gated scalar updates: (x, P, accepted, gated)."""
    accepted = gated = 0
    for m in measurements:
        if m.kind == "range":
            h, H = range_model(x, m.tx[0], m.rx[0])
        else:
            h, H = doppler_model(x, m.tx, m.rx, wavelength_m)
        x, P, ok = scalar_update(x, P, m.value, h, H, m.sigma, gate_chi2)
        if ok:
            accepted += 1
        else:
            gated += 1
    return x, P, accepted, gated


def chi2_3dof_gate(gate_1dof: float) -> float:
    """The chi-square (3 dof) value whose tail probability equals that of a
    1-dof ``gate_1dof`` (16 -> 22.06, both 6.3e-5). Tails: 1 dof erfc(z),
    3 dof erfc(z) + sqrt(2x/pi) e^-x/2, z = sqrt(x/2)."""

    def log_tail(x: float, three: bool) -> float:
        z = math.sqrt(x / 2.0)
        extra = math.sqrt(2.0 * x / math.pi) if three else 0.0
        if z < 25.0:
            return math.log(math.erfc(z) + extra * math.exp(-z * z))
        # erfc(z) ~ e^-z^2 / (z sqrt(pi)): keeps the log finite past underflow.
        return -z * z + math.log(1.0 / (z * math.sqrt(math.pi)) + extra)

    target = log_tail(gate_1dof, False)
    lo, hi = gate_1dof, 2.0 * gate_1dof + 10.0
    while log_tail(hi, True) > target:
        lo, hi = hi, 2.0 * hi
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if log_tail(mid, True) > target:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


def init_track(
    est: TargetEstimate, reports: list[SensingLinkReport], consts: dict, time_s: float
) -> TargetTrack:
    """A track from an ok fusion: x0 = [position_est, velocity_est or 0],
    P0 = diag(max(GDOP^2 sigma_R^2 / 3, 0.25) per position axis (100 m^2
    without a GDOP), 25 (m/s)^2 per velocity axis with a Doppler velocity,
    400 without), sigma_R^2 = the mean range-sum variance of links_used."""
    used = set(est.links_used)
    variances = [
        measurement_sigmas(r.snr_db, consts)[0] ** 2
        for r in reports
        if r.target_id == est.target_id and f"{r.tx_id}>{r.rx_id}" in used
    ]
    pos_var = 100.0
    if est.gdop is not None and variances:
        pos_var = max(est.gdop**2 * (sum(variances) / len(variances)) / 3.0, 0.25)
    vel_var = 25.0 if est.velocity_est is not None else 400.0
    x0 = np.array(
        [*est.position_est, *(est.velocity_est or [0.0, 0.0, 0.0])],  # type: ignore[misc]
        dtype=float,
    )
    return TargetTrack(x0, np.diag([pos_var] * 3 + [vel_var] * 3), time_s)


# ------------------------------------------------------------------ tracker


def _position_std(P: np.ndarray) -> float:
    """sqrt(trace(P_pos)): the 3-D RMS position error the filter expects."""
    return math.sqrt(max(float(np.trace(P[:3, :3])), 0.0))


def _median(values: list[float]) -> Optional[float]:
    return float(statistics.median(values)) if values else None


def _p90(values: list[float]) -> Optional[float]:
    return float(np.percentile(values, 90)) if values else None


class SensingTracker:
    """Runs the per-frame sensing solve of a scenario and keeps, per target,
    the last ok estimate (the next frame's Gauss-Newton start) and, with
    tracking enabled, the EKF track."""

    def __init__(
        self,
        backend: RayTracingBackend,
        project_dir: Path,
        scene: Scene,
        library: RFMaterialLibrary,
        config: SimulationConfig,
        options: SensingTrackOptions,
        txs: list[Device],
        rxs: list[Device],
        target_ids: list[str],
    ):
        self.backend = backend
        self.project_dir = project_dir
        self.scene = scene
        self.library = library
        self.config = config
        self.options = options
        self.txs = txs
        self.rxs = rxs
        self.target_ids = target_ids
        self.consts = detection_constants(config, options)
        self._actor_by_id = {a.id: a for a in scene.actors}
        self._request = SensingSimulateRequest(
            target_actor_ids=target_ids,
            include_comm_paths=False,
            samples_per_sp=options.samples_per_sp,
            max_depth=options.max_depth,
        )
        self._prior: dict[str, Prior] = {}
        self._tracking: Optional[TrackingOptions] = (
            options.tracking if options.tracking_enabled() else None
        )
        self._tracks: dict[str, TargetTrack] = {}
        self._track_started: set[str] = set()
        # Frames whose track the max_position_std_m cap dropped, per target.
        self._lost_by_std: dict[str, int] = {}
        self._restart_gate = (
            chi2_3dof_gate(self._tracking.gate_chi2) if self._tracking is not None else math.inf
        )

    def _nodes(
        self,
        device_positions: dict[str, list[float]],
        device_velocities: dict[str, list[float]],
    ) -> Nodes:
        nodes: Nodes = {}
        for dev in [*self.txs, *self.rxs]:
            pos = device_positions.get(dev.id, dev.position)
            vel = device_velocities.get(dev.id, dev.velocity_m_s or [0.0, 0.0, 0.0])
            nodes[dev.id] = ([float(c) for c in pos], [float(c) for c in vel])
        return nodes

    def frame(
        self,
        frame_index: int,
        time_s: float,
        actor_states: list[ActorState],
        actor_velocities: dict[str, list[float]],
        device_positions: dict[str, list[float]],
        device_velocities: dict[str, list[float]],
    ) -> tuple[SensingFrame, list[str]]:
        frame_scene = _frame_scene(
            self.scene, actor_states, device_positions, device_velocities,
            move_actors=self.backend.name != "sionna",
        )
        state_by_id = {s.id: s for s in actor_states}
        targets = [
            resolve_target_state(
                self._actor_by_id[aid],
                state_by_id[aid],
                actor_velocities.get(aid, [0.0, 0.0, 0.0]),
            )
            for aid in self.target_ids
        ]
        result = self.backend.simulate_sensing(
            self.project_dir, frame_scene, self.library, self.config, self._request,
            targets, actor_states=actor_states, actor_velocities=actor_velocities,
        )
        warnings = list(result.warnings)
        comm_paths: list[RayPath] = []
        if self.options.include_comm_paths:
            comm = self.backend.simulate_paths_with_targets(
                self.project_dir, frame_scene, self.library, self.config, targets,
                actor_states=actor_states, actor_velocities=actor_velocities,
            )
            dop = comm.metadata.get("doppler_hz")
            aligned = isinstance(dop, list) and len(dop) == len(comm.paths)
            comm_paths = [
                p.model_copy(update={"doppler_hz": float(dop[i])}) if aligned else p
                for i, p in enumerate(comm.paths)
            ]
            warnings += [f"comm paths: {w}" for w in comm.warnings]
        echoes = [p for p in result.paths if p.target_id is not None] + comm_paths

        nodes = self._nodes(device_positions, device_velocities)
        links = [
            link_report(
                tx.id, rx.id, aid, echoes, self.consts, self.options, frame_index, nodes
            )
            for tx in self.txs
            for rx in self.rxs
            for aid in self.target_ids
        ]
        if self.options.pfa is not None:
            links = [
                with_link_pd(r, self.options, frame_index, k) for k, r in enumerate(links)
            ]
        estimates: list[TargetEstimate] = []
        for target in targets:
            aid = target.actor_id
            prior = self._prior.get(aid)
            predicted: Optional[tuple[np.ndarray, np.ndarray]] = None
            tracking = self._tracking
            if tracking is not None and aid in self._tracks:
                track = self._tracks[aid]
                predicted = cv_predict(
                    track.x, track.P, time_s - track.time_s, tracking.process_accel_sigma_m_s2
                )
                if tracking.use_as_prior:
                    # dt = 0: the Gauss-Newton start is exactly the prediction.
                    prior = ([float(c) for c in predicted[0][:3]], None, time_s)
            est = estimate_target(
                target, links, nodes, self.consts, prior, time_s,
                measurement_noise=self.options.measurement_noise,
                ground_target=self._actor_by_id[aid].kind in GROUND_KINDS,
            )
            if est.status == "ok" and est.position_est is not None:
                self._prior[aid] = (est.position_est, est.velocity_est, time_s)
            if tracking is not None:
                est = self._track_step(est, links, nodes, time_s, predicted, tracking)
            estimates.append(est)
        node_states = [
            SensingNodeState(id=dev_id, position=pos, velocity=vel)
            for dev_id, (pos, vel) in nodes.items()
        ]
        frame = SensingFrame(echoes=echoes, links=links, estimates=estimates, nodes=node_states)
        return frame, warnings

    def _track_step(
        self,
        est: TargetEstimate,
        links: list[SensingLinkReport],
        nodes: Nodes,
        time_s: float,
        predicted: Optional[tuple[np.ndarray, np.ndarray]],
        tracking: TrackingOptions,
    ) -> TargetEstimate:
        """Update (or start, or drop) the target's track with this frame and
        return ``est`` carrying the track fields. Order: predict (done by the
        caller, before the fusion) -> fusion -> update / init."""
        aid = est.target_id
        track = self._tracks.get(aid)
        status = "lost" if aid in self._track_started else "none"
        updates: Optional[int] = None
        gated: Optional[int] = None
        fresh: Optional[TargetTrack] = None
        if est.status == "ok" and est.position_est is not None:
            fresh = init_track(est, links, self.consts, time_s)
        update: Optional[tuple[np.ndarray, np.ndarray, int, int]] = None
        if track is not None and predicted is not None:
            update = ekf_update(
                predicted[0], predicted[1],
                track_measurements(links, aid, nodes, self.consts),
                self.consts["wavelength_m"], tracking.gate_chi2,
            )
        if predicted is not None and update is not None and fresh is not None and update[3] > 0:
            # The target manoeuvred (a waypoint corner) when the gate rejects
            # some of the frame's scalars (the Doppler that would correct the
            # velocity) AND the ok fusion lies outside the predicted track's
            # 3-D gate (same tail as gate_chi2): restart from the fusion. The
            # fusion test alone also fires on the fusion's heavy-tailed
            # outliers while every raw scalar still agrees with the track.
            e = fresh.x[:3] - predicted[0][:3]
            cov = predicted[1][:3, :3] + fresh.P[:3, :3]
            try:
                d2 = float(e @ np.linalg.solve(cov, e))
            except np.linalg.LinAlgError:
                d2 = 0.0
            if d2 > self._restart_gate:
                del self._tracks[aid]
                track = None
        if track is not None and update is not None:
            x, P, updates, gated = update
            track.x, track.P, track.time_s = x, P, time_s
            track.coast = 0 if updates > 0 else track.coast + 1
            status = "tracking" if updates > 0 else "coasting"
            # A posterior wider than max_position_std_m (long stretches on one
            # or two links) no longer locates the target: drop it like a coast.
            too_wide = _position_std(P) > tracking.max_position_std_m
            if too_wide or track.coast > tracking.coast_max_frames:
                if too_wide:
                    self._lost_by_std[aid] = self._lost_by_std.get(aid, 0) + 1
                del self._tracks[aid]
                track = None
                status = "lost"
        if track is None and fresh is not None:
            # Started from this frame's fusion: no update with the same data.
            track = self._tracks[aid] = fresh
            self._track_started.add(aid)
            status, updates, gated = "init", 0, 0
        fields: dict = {"track_status": status, "track_updates": updates, "track_gated": gated}
        if track is not None:
            pos = [float(c) for c in track.x[:3]]
            vel = [float(c) for c in track.x[3:]]
            fields.update(
                track_position=pos,
                track_velocity=vel,
                track_position_error_m=math.dist(pos, est.position_true),
                track_velocity_error_m_s=math.dist(vel, est.velocity_true),
                track_position_std_m=_position_std(track.P),
            )
        return est.model_copy(update=fields)

    def summary(self, frames: list[ScenarioFrame]) -> dict:
        """Run summary for metadata["sensing"]: constants, options, and per
        target detection / fusion statistics (plain floats, JSON-ready)."""
        num_links = len(self.txs) * len(self.rxs)
        n = len(frames)
        targets: dict[str, dict] = {}
        all_pos: list[float] = []
        all_vel: list[float] = []
        all_track_pos: list[float] = []
        for aid in self.target_ids:
            detected_frames = detected_links = ge3 = ok = 0
            pos_err: list[float] = []
            vel_err: list[float] = []
            gdops: list[float] = []
            for frame in frames:
                if frame.sensing is None:
                    continue
                hits = sum(
                    1 for r in frame.sensing.links if r.target_id == aid and r.detected
                )
                detected_links += hits
                detected_frames += hits > 0
                est = next((e for e in frame.sensing.estimates if e.target_id == aid), None)
                if est is None:
                    continue
                ge3 += est.n_links_used >= 3
                if est.status != "ok":
                    continue
                ok += 1
                if est.position_error_m is not None:
                    pos_err.append(est.position_error_m)
                if est.velocity_error_m_s is not None:
                    vel_err.append(est.velocity_error_m_s)
                if est.gdop is not None:
                    gdops.append(est.gdop)
            all_pos += pos_err
            all_vel += vel_err
            targets[aid] = {
                "frames": n,
                "detected_frames": detected_frames,
                "detection_rate": detected_frames / n if n else 0.0,
                "link_detection_rate": (
                    detected_links / (n * num_links) if n * num_links else 0.0
                ),
                "frames_ge3_links": ge3,
                "frames_ge3_links_rate": ge3 / n if n else 0.0,
                "ok_frames": ok,
                "median_position_error_m": _median(pos_err),
                "p90_position_error_m": (
                    float(np.percentile(pos_err, 90)) if pos_err else None
                ),
                "median_velocity_error_m_s": _median(vel_err),
                "median_gdop": _median(gdops),
            }
            if self._tracking is not None:
                stats, track_pos_err = self._track_summary(
                    frames, aid, self._lost_by_std.get(aid, 0)
                )
                all_track_pos += track_pos_err
                targets[aid].update(stats)
        per_target = list(targets.values())
        # tracking {enabled: false} is the same run as no tracking block.
        options = self.options
        if options.tracking is not None and not options.tracking_enabled():
            options = options.model_copy(update={"tracking": None})
        tracking_keys = (
            {
                "median_track_position_error_m": _median(all_track_pos),
                "tracking_model": TRACKING_MODEL,
            }
            if self._tracking is not None
            else {}
        )
        return {
            "options": metadata_dump(options),
            **self.consts,
            "num_links": num_links,
            "target_ids": list(self.target_ids),
            "targets": targets,
            "detection_rate": (
                float(np.mean([t["detection_rate"] for t in per_target])) if per_target else 0.0
            ),
            "frames_ge3_links_rate": (
                float(np.mean([t["frames_ge3_links_rate"] for t in per_target]))
                if per_target
                else 0.0
            ),
            "median_position_error_m": _median(all_pos),
            "median_velocity_error_m_s": _median(all_vel),
            **tracking_keys,
        }

    @staticmethod
    def _track_summary(
        frames: list[ScenarioFrame], aid: str, lost_by_std: int
    ) -> tuple[dict, list[float]]:
        """Per-target EKF statistics, and the track position errors.
        ``lost_by_std``: frames whose track the max_position_std_m cap dropped
        (the frame does not record why its track was dropped)."""
        tracked = coasting = lost = improved = without_fusion = 0
        pos_err: list[float] = []
        vel_err: list[float] = []
        for frame in frames:
            if frame.sensing is None:
                continue
            est = next((e for e in frame.sensing.estimates if e.target_id == aid), None)
            if est is None:
                continue
            tracked += est.track_status in TRACKED_STATUSES
            coasting += est.track_status == "coasting"
            lost += est.track_status == "lost"
            if est.track_position_error_m is not None:
                pos_err.append(est.track_position_error_m)
                if est.status != "ok":
                    without_fusion += 1
                elif (
                    est.position_error_m is not None
                    and est.track_position_error_m < est.position_error_m
                ):
                    improved += 1
            if est.track_velocity_error_m_s is not None:
                vel_err.append(est.track_velocity_error_m_s)
        return {
            "tracked_frames": tracked,
            "coasting_frames": coasting,
            "lost_frames": lost,
            "lost_by_std_frames": lost_by_std,
            "median_track_position_error_m": _median(pos_err),
            "p90_track_position_error_m": _p90(pos_err),
            "median_track_velocity_error_m_s": _median(vel_err),
            "frames_improved_over_fusion": improved,
            "frames_track_without_fusion": without_fusion,
        }, pos_err
