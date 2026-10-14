"""Signal-level link detection of a sensing scenario (#24,
``sensing.detection.mode "cfar"``).

Per frame and TX -> sensing-RX link the detector synthesizes the pilot
channel of the frame's echoes plus the background (H1 when solved, else H0),
adds thermal noise per resource element from the run's noise seed, builds
the delay-Doppler map and runs 2-D CA/OS-CFAR (services/channel_synthesis).
Every detection is stored without truth fields (SensingFrame.detections);
the link reports keep their truth-side fields and take the detection
decision and the measured range sum / Doppler from the strongest detection
inside an oracle gate around the reported echo. Detections outside every
gate are false alarms (thermal noise, clutter, multipath ghosts); their
achieved rate per cell, map and frame goes into metadata.sensing.detection.

The threshold is either analytic (CFAR alpha from cfar.pfa_cell) or the
empirical quantile of the CFAR statistic over the H0 maps of a stored
sensing_background_ensemble or scenario result (detection.h0_result_id).
Calibration noise comes from its own stream (H0_NOISE_STREAM), never the
noise of the run it calibrates.
"""

import json
import math
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, Sequence

import numpy as np
from pydantic import ValidationError

from seam_studio.schemas.actors import ScenarioFrame, ScenarioResultSet
from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import (
    RayPath,
    SensingBackgroundEnsembleResultSet,
    SensingDetection,
    SensingLinkReport,
)
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.sensing import DetectionOptions, SensingTrackOptions
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.channel_synthesis import (
    H0_NOISE_STREAM,
    SPEED_OF_LIGHT,
    ThresholdAccumulator,
    analytic_alpha,
    cfar,
    cfar_statistic,
    check_cfar_fits,
    dd_map,
    dispersion,
    effective_notch_bins,
    interpolate_peak,
    noise_frame,
    noise_key,
    notch_rows,
    os_rank,
    rate_block,
    snr_est_db,
    synthesize_cfr,
    training_counts,
    training_rows,
)
from seam_studio.services.sensing import SensingRequestError

# Total H0 cells one calibration may evaluate.
MAX_CALIBRATION_CELLS = 500_000_000
# Cells of one zero-padded delay-Doppler map (complex128: 256 MiB).
MAX_MAP_CELLS = 2**24
QUANTILE_RULE = "sorted[n - floor(n*p)]"
MEASUREMENT_NOISE_NOTE = (
    "sensing.measurement_noise is ignored with detection.mode 'cfar' (the map supplies "
    "the measurement noise)"
)
FALSE_ALARM_NOTE = (
    "false alarms: detections outside every oracle gate of the link (thermal noise, "
    "clutter and multipath ghosts alike)"
)


# ------------------------------------------------------------------ H0 sources


def sensing_link_pairs(result: ScenarioResultSet, frame: ScenarioFrame) -> list[tuple[str, str]]:
    """TX -> sensing-RX pairs of one frame, tx-major: RX = metadata.sensing
    .sensing_rx_ids; TX = sensing_tx_ids, else the frame's nodes minus the
    RX, else the frame's unique report pairs."""
    meta = result.metadata.get("sensing") or {}
    rx_ids = meta.get("sensing_rx_ids")
    tx_ids = meta.get("sensing_tx_ids")
    sensing = frame.sensing
    if rx_ids:
        if tx_ids is None and sensing is not None and sensing.nodes is not None:
            tx_ids = [n.id for n in sensing.nodes if n.id not in rx_ids]
        if tx_ids is not None:
            return [(t, r) for t in tx_ids for r in rx_ids]
    if sensing is None:
        return []
    return list(dict.fromkeys((r.tx_id, r.rx_id) for r in sensing.links))


@dataclass
class H0Link:
    tx_id: str
    rx_id: str
    paths: list[RayPath]
    cfo_hz: float = 0.0


@dataclass
class H0Group:
    """H0 frames sharing one background (their noise differs per frame)."""

    seed: int
    realization: int
    frame_indices: Sequence[int]
    links: list[H0Link]


@dataclass
class H0Source:
    result_id: str
    kind: str
    groups: list[H0Group] = field(default_factory=list)


def _link_paths(paths: list[RayPath], tx_id: str, rx_id: str) -> list[RayPath]:
    return [p for p in paths if p.tx_id == tx_id and p.rx_id == rx_id]


@dataclass(frozen=True)
class MapLayout:
    """Row layout of a run's delay-Doppler maps: notched rows (never a CUT)
    and training rows (cells that may train the CFAR)."""

    n_pad: int
    k_pad: int
    notch_rows: np.ndarray  # bool [N']
    train_rows: np.ndarray  # bool [N']


def check_detection_map(numerology: Any, det: DetectionOptions) -> MapLayout:
    """The map of ``det`` on ``numerology``'s pilot grid, or
    SensingRequestError (400): more than MAX_MAP_CELLS cells, a CFAR window
    that does not fit, a Hann axis of one sample, a notch over every row, or
    a tested row without a training cell."""
    zp = det.map.zero_pad
    grid_n, grid_k = int(numerology.grid_n), int(numerology.grid_k)
    n_pad, k_pad = zp * grid_n, zp * grid_k
    if n_pad * k_pad > MAX_MAP_CELLS:
        raise SensingRequestError(
            f"the delay-Doppler map has {k_pad} x {n_pad} = {n_pad * k_pad} cells (zero_pad "
            f"{zp} x {grid_k} pilot subcarriers x {grid_n} pilot symbols); at most "
            f"{MAX_MAP_CELLS} (shorten cpi_s, thin the pilot lattice or lower "
            "detection.map.zero_pad)"
        )
    check_cfar_fits(det.cfar, n_pad, k_pad)
    if det.map.window == "hann" and min(grid_n, grid_k) < 2:
        raise SensingRequestError(
            f"a {grid_k} x {grid_n} pilot grid is too small for the Hann window (it needs at "
            "least 2 pilot subcarriers and 2 pilot symbols; use detection.map.window 'rect')"
        )
    notch = notch_rows(n_pad, zp, effective_notch_bins(det.map))
    if notch.all():
        raise SensingRequestError(
            f"detection.map.zero_doppler_notch_bins {effective_notch_bins(det.map)} notches "
            f"every one of the {n_pad} Doppler rows: no cell is left to test"
        )
    train = training_rows(grid_n, det.map)
    counts = training_counts(train, det.cfar)
    empty = np.flatnonzero((counts == 0) & ~notch)
    if empty.size:
        raise SensingRequestError(
            f"{empty.size} tested Doppler rows have no CFAR training cell (the notch and "
            "the mean_subtract rows cover the whole training window); lower the notch or "
            "raise detection.cfar.train"
        )
    return MapLayout(n_pad=n_pad, k_pad=k_pad, notch_rows=notch, train_rows=train)


def load_h0_source(project_dir: Path, scene: Scene, result_id: str) -> H0Source:
    """The H0 maps of a stored sensing_background_ensemble (every realization
    x noise frame) or scenario result (every frame with an H0 background).
    SensingRequestError (400) for an unknown / unreadable / unsuitable one."""
    ref = next((r for r in scene.result_sets if r.result_id == result_id), None)
    if ref is None:
        raise SensingRequestError(f"detection.h0_result_id: unknown result {result_id}")
    if ref.kind not in ("sensing_background_ensemble", "scenario"):
        raise SensingRequestError(
            f"detection.h0_result_id: result {result_id} is a {ref.kind} result; use a "
            "sensing_background_ensemble or a scenario result with H0 background"
        )
    try:
        root = Path(project_dir).resolve()
        path = (root / ref.uri).resolve()
        if not path.is_relative_to(root):
            raise ValueError("result uri escapes the project")
        data = json.loads(path.read_text(encoding="utf-8"))
        if ref.kind == "sensing_background_ensemble":
            ens = SensingBackgroundEnsembleResultSet.model_validate(data)
        else:
            res = ScenarioResultSet.model_validate(data)
    except (OSError, ValueError, ValidationError) as exc:
        raise SensingRequestError(
            f"detection.h0_result_id: result file missing or unreadable: {ref.uri}"
        ) from exc

    source = H0Source(result_id=result_id, kind=ref.kind)
    if ref.kind == "sensing_background_ensemble":
        by_index = {r.index: r for r in ens.realizations}
        for real in ens.realizations:
            holder = real if real.background_h0 is not None else by_index.get(
                real.source_realization
            )
            if holder is None or holder.background_h0 is None:
                raise SensingRequestError(
                    f"detection.h0_result_id: realization {real.index} of {result_id} "
                    f"names source realization {real.source_realization} without paths"
                )
            source.groups.append(H0Group(
                seed=ens.seed,
                realization=real.index,
                frame_indices=range(ens.noise_frames_per_realization),
                links=[
                    H0Link(
                        link.tx_id, link.rx_id,
                        _link_paths(holder.background_h0, link.tx_id, link.rx_id),
                        float(real.cfo_hz.get(link.rx_id, 0.0)),
                    )
                    for link in ens.links
                ],
            ))
    else:
        from seam_studio.services.sensing_background import resolve_background

        options = (res.metadata.get("sensing") or {}).get("options") or {}
        seed = int(options.get("noise_seed", 0))
        # Frames sharing one stored H0 list (static_nodes cache hits) and link
        # set form one group: the background is synthesized once.
        by_key: dict[tuple, H0Group] = {}
        for i, frame in enumerate(res.frames):
            if frame.sensing is None:
                continue
            paths = resolve_background(res.frames, i, "h0")
            if paths is None:
                continue
            pairs = sensing_link_pairs(res, frame)
            key = (id(paths), tuple(pairs))
            group = by_key.get(key)
            if group is None:
                group = by_key[key] = H0Group(
                    seed=seed,
                    realization=0,
                    frame_indices=[],
                    links=[H0Link(tx, rx, _link_paths(paths, tx, rx)) for tx, rx in pairs],
                )
                source.groups.append(group)
            group.frame_indices.append(i)  # type: ignore[attr-defined]
    if not source.groups:
        raise SensingRequestError(
            f"detection.h0_result_id: result {result_id} has no H0 background frames "
            "(run the scenario with sensing.background mode 'h0_removed' or 'both' and "
            "store 'h0' or 'both')"
        )
    return source


# ------------------------------------------------------------------ detector


class SignalDetector:
    """cfar-mode detection of one scenario run (see module docstring)."""

    def __init__(
        self,
        *,
        options: SensingTrackOptions,
        consts: dict,
        numerology: Any,
        config: SimulationConfig,
        txs: list[Device],
        rxs: list[Device],
        project_dir: Path,
        scene: Scene,
    ) -> None:
        det = options.detection
        if det is None or det.mode != "cfar":
            raise SensingRequestError("SignalDetector needs sensing.detection.mode 'cfar'")
        self.options = options
        self.det = det
        self.consts = consts
        self.num = numerology
        self.txs = list(txs)
        self.rxs = list(rxs)
        self.project_dir = project_dir
        self.scene = scene
        self.noise_seed = int(options.noise_seed)
        self.nf_db = float(config.noise_figure_db)
        self.warnings: list[str] = []
        if options.measurement_noise:
            self.warnings.append(MEASUREMENT_NOISE_NOTE)

        zp = det.map.zero_pad
        layout = check_detection_map(numerology, det)
        self.shape = (int(numerology.grid_n), int(numerology.grid_k))
        self.n_pad, self.k_pad = layout.n_pad, layout.k_pad
        self.f_hz, self.t_s = numerology.axes("pilot")
        self.sigma2 = {
            tx.id: float(numerology.sigma_h2(float(tx.power_dbm), self.nf_db)) for tx in self.txs
        }
        self.tx_power = {tx.id: float(tx.power_dbm) for tx in self.txs}
        self.gate = det.oracle_gate_cells * zp
        self._notch_rows = layout.notch_rows
        self._train_rows = layout.train_rows
        self._notch = np.repeat(layout.notch_rows[:, None], self.k_pad, axis=1)

        self.alpha: Optional[float] = None
        self.source_meta: Optional[dict] = None
        if det.threshold == "analytic":
            self.alpha = analytic_alpha(det.cfar)
            self.threshold = self.alpha
        else:
            self.threshold, self.source_meta = self._calibrate(det.h0_result_id or "")

        self._bg_ref: Optional[list[RayPath]] = None
        self._bg_h: dict[tuple[str, str], np.ndarray] = {}
        self.frames = 0
        self.cell_fa = self.cell_trials = 0
        # Per map: false-alarm cells and tested cells (overdispersion of the
        # cell interval: windowing and zero padding correlate cells).
        self._map_cell_fa: list[int] = []
        self._map_cell_trials: list[int] = []
        self.map_fa = self.maps = 0
        self.frame_fa = 0
        self.detections_total = self.detections_stored = 0

    # ------------------------------------------------------------ calibration

    def _calibrate(self, result_id: str) -> tuple[float, dict]:
        source = load_h0_source(self.project_dir, self.scene, result_id)
        tx_ids = {t.id for t in self.txs}
        rx_ids = {r.id for r in self.rxs}
        groups = [
            (g, [lk for lk in g.links if lk.tx_id in tx_ids and lk.rx_id in rx_ids])
            for g in source.groups
        ]
        groups = [(g, links) for g, links in groups if links]
        if not groups:
            raise SensingRequestError(
                f"detection.h0_result_id: no link of {result_id} matches this run's "
                f"TX -> sensing-RX links ({sorted(tx_ids)} -> {sorted(rx_ids)})"
            )
        run_links = {(t.id, r.id) for t in self.txs for r in self.rxs}
        missing = sorted(
            {f"{tx}>{rx}" for tx, rx in run_links}
            - {f"{lk.tx_id}>{lk.rx_id}" for _, links in groups for lk in links}
        )
        if missing and self.det.system_pfa is not None:
            # The system statistic is the max over every link of a frame: a
            # threshold over fewer links lets the run's frames alarm more.
            raise SensingRequestError(
                f"detection.system_pfa: {result_id} has no H0 maps for this run's links "
                f"{', '.join(missing)}; the per-frame maximum must cover every link (use an "
                "H0 source with the same TX and sensing RX, or drop system_pfa)"
            )
        if missing:
            self.warnings.append(
                f"detection.h0_result_id: {result_id} has no H0 maps for the links "
                f"{', '.join(missing)}; their cells use the threshold calibrated on the "
                "other links"
            )
        n_frames = sum(len(g.frame_indices) for g, _ in groups)
        n_maps = sum(len(g.frame_indices) * len(links) for g, links in groups)
        cells = n_maps * self.n_pad * self.k_pad
        if cells > MAX_CALIBRATION_CELLS:
            raise SensingRequestError(
                f"detection.h0_result_id: {n_maps} H0 maps x {self.n_pad * self.k_pad} cells "
                f"= {cells} cells; at most {MAX_CALIBRATION_CELLS} per calibration"
            )
        tested = int(np.count_nonzero(~self._notch))
        system = self.det.system_pfa is not None
        pfa = self.det.system_pfa if system else self.det.cfar.pfa_cell
        n_values = n_frames if system else n_maps * tested
        acc = ThresholdAccumulator(n_values, float(pfa))
        for group, links in groups:
            h_bg = {
                (lk.tx_id, lk.rx_id): synthesize_cfr(
                    lk.paths, self.f_hz, self.t_s, self.tx_power[lk.tx_id], cfo_hz=lk.cfo_hz
                )
                for lk in links
            }
            for m in group.frame_indices:
                frame_max = -math.inf
                for lk in links:
                    sigma2 = self.sigma2[lk.tx_id]
                    Y = h_bg[(lk.tx_id, lk.rx_id)] + noise_frame(
                        group.seed, m, lk.tx_id, lk.rx_id, self.shape, sigma2,
                        realization=group.realization, stream=H0_NOISE_STREAM,
                    )
                    stat, _ = cfar_statistic(
                        dd_map(Y, self.num, self.det.map, sigma2).P, self.det.cfar,
                        self._train_rows,
                    )
                    values = stat[~self._notch]
                    if system:
                        frame_max = max(frame_max, float(values.max()))
                    else:
                        acc.add(values)
                if system:
                    acc.add(np.array([frame_max]))
        threshold = acc.threshold()
        return threshold, {
            "result_id": source.result_id,
            "kind": source.kind,
            "statistic": "system_frame_max" if system else "cell",
            "n_values": acc.n_total,
            "m": acc.m,
            "pfa": float(pfa),
            "quantile_rule": QUANTILE_RULE,
            "frames": n_frames,
            "maps": n_maps,
            "links": sorted({f"{lk.tx_id}>{lk.rx_id}" for _, links in groups for lk in links}),
            "uncalibrated_links": missing,
            "noise_key": noise_key("seed", "realization", H0_NOISE_STREAM),
        }

    # ------------------------------------------------------------ per frame

    def _background_h(self, background: Optional[list[RayPath]], tx_id: str, rx_id: str):
        if not background:
            return None
        if background is not self._bg_ref:
            # A static_nodes cache hit hands over the same list: synthesize once.
            self._bg_ref = background
            self._bg_h = {}
        key = (tx_id, rx_id)
        if key not in self._bg_h:
            self._bg_h[key] = synthesize_cfr(
                _link_paths(background, tx_id, rx_id), self.f_hz, self.t_s, self.tx_power[tx_id]
            )
        return self._bg_h[key]

    def _circ_mask(self, length: int, centre: float, values: np.ndarray) -> np.ndarray:
        # Measured from the map cell nearest the true (fractional) cell: the
        # gate is 2G + 1 cells, gate 0 the echo's own cell.
        d = np.mod(values - math.floor(centre + 0.5), length)
        return np.minimum(d, length - d) <= self.gate

    def frame(
        self,
        frame_index: int,
        echoes: list[RayPath],
        background: Optional[list[RayPath]],
        reports: list[SensingLinkReport],
    ) -> tuple[list[SensingDetection], list[SensingLinkReport]]:
        reports = list(reports)
        by_link: dict[tuple[str, str], list[int]] = defaultdict(list)
        for j, r in enumerate(reports):
            by_link[(r.tx_id, r.rx_id)].append(j)
        echo_by_link: dict[tuple[str, str], list[RayPath]] = defaultdict(list)
        for p in echoes:
            if p.target_id is not None:
                echo_by_link[(p.tx_id, p.rx_id)].append(p)
        dfp = float(self.num.pilot_spacing_hz)
        tp = float(self.num.pilot_period_s)
        half = self.n_pad // 2
        u_axis = np.arange(self.k_pad, dtype=np.float64)
        v_axis = np.arange(self.n_pad, dtype=np.float64) - half
        res_m = float(self.consts["bistatic_range_resolution_m"])
        cpi_s = float(self.consts["cpi_s"])

        stored: list[SensingDetection] = []
        frame_has_fa = False
        for tx in self.txs:
            for rx in self.rxs:
                key = (tx.id, rx.id)
                sigma2 = self.sigma2[tx.id]
                H = synthesize_cfr(echo_by_link.get(key, []), self.f_hz, self.t_s, self.tx_power[tx.id])
                h_bg = self._background_h(background, tx.id, rx.id)
                if h_bg is not None:
                    H = H + h_bg
                Y = H + noise_frame(self.noise_seed, frame_index, tx.id, rx.id, self.shape, sigma2)
                ddm = dd_map(Y, self.num, self.det.map, sigma2)
                res = cfar(ddm.P, self._notch, self.det.cfar, self.threshold, self._train_rows)
                r_idx, u_idx = np.nonzero(res.detected)
                stats = res.stat[r_idx, u_idx]
                order = np.lexsort((r_idx, u_idx, -stats))
                r_idx, u_idx, stats = r_idx[order], u_idx[order], stats[order]

                # Oracle gates of this link's reports with an echo.
                gates: list[tuple[int, np.ndarray]] = []
                gate_mask = np.zeros((self.n_pad, self.k_pad), dtype=bool)
                for j in by_link.get(key, []):
                    r = reports[j]
                    if r.path_id is None or r.bistatic_range_m is None:
                        continue
                    u_star = (r.bistatic_range_m / SPEED_OF_LIGHT) * self.k_pad * dfp
                    v_star = (r.doppler_hz or 0.0) * self.n_pad * tp
                    in_u = self._circ_mask(self.k_pad, u_star, u_axis)
                    in_v = self._circ_mask(self.n_pad, v_star, v_axis)
                    gate_mask |= in_v[:, None] & in_u[None, :]
                    hit = in_v[r_idx] & in_u[u_idx]
                    gates.append((j, hit))
                in_any_gate = gate_mask[r_idx, u_idx]
                fa = int(np.count_nonzero(~in_any_gate))
                trials = int(np.count_nonzero(~self._notch & ~gate_mask))
                self.cell_fa += fa
                self.cell_trials += trials
                self._map_cell_fa.append(fa)
                self._map_cell_trials.append(trials)
                self.maps += 1
                self.map_fa += fa > 0
                frame_has_fa |= fa > 0
                self.detections_total += int(r_idx.size)

                cap = self.det.max_detections_per_link
                keep = list(range(min(cap, r_idx.size)))
                keep += [i for i in range(cap, r_idx.size) if in_any_gate[i]]
                position = {i: len(stored) + n for n, i in enumerate(keep)}
                for i in keep:
                    stored.append(self._detection(ddm, res, tx.id, rx.id, int(r_idx[i]), int(u_idx[i])))

                for j, hit in gates:
                    r = reports[j]
                    cand = np.flatnonzero(hit)
                    if cand.size == 0:
                        reports[j] = r.model_copy(update={
                            "detected": False, "reason": "cfar_miss",
                            "measured_range_m": None, "measured_doppler_hz": None,
                            "range_bin": None, "doppler_bin": None, "detection_index": None,
                        })
                        continue
                    best = int(cand[0])  # sorted by statistic: the first is the strongest
                    d = stored[position[best]]
                    reports[j] = r.model_copy(update={
                        "detected": True, "reason": "detected",
                        "measured_range_m": d.range_sum_m,
                        "measured_doppler_hz": d.doppler_hz,
                        "range_bin": math.floor(d.range_sum_m / res_m),
                        "doppler_bin": round(d.doppler_hz * cpi_s),
                        "detection_index": position[best],
                    })
                for j in by_link.get(key, []):
                    r = reports[j]
                    if r.path_id is None:
                        reports[j] = r.model_copy(update={
                            "measured_range_m": None, "measured_doppler_hz": None,
                            "range_bin": None, "doppler_bin": None,
                        })
        self.frames += 1
        self.frame_fa += frame_has_fa
        self.detections_stored += len(stored)
        return stored, reports

    def _detection(self, ddm, res, tx_id: str, rx_id: str, r: int, u: int) -> SensingDetection:
        du, dv = interpolate_peak(ddm.P, r, u)
        v = r - self.n_pad // 2
        delay_s = ddm.delay_s(u + du)
        power = float(ddm.P[r, u])
        return SensingDetection(
            tx_id=tx_id,
            rx_id=rx_id,
            cell_tau=u,
            cell_nu=v,
            delay_ns=delay_s * 1e9,
            range_sum_m=SPEED_OF_LIGHT * delay_s,
            doppler_hz=ddm.doppler_hz(v + dv),
            statistic=float(res.stat[r, u]),
            threshold=float(self.threshold),
            power_db=10.0 * math.log10(power) if power > 0.0 else -math.inf,
            snr_est_db=snr_est_db(power, float(res.noise_mean[r, u])),
        )

    # ------------------------------------------------------------ summary

    def summary(self) -> dict:
        det = self.det
        cfar_meta: dict = {**det.cfar.model_dump(mode="json"), "n_train": det.cfar.n_train()}
        if det.cfar.type == "os":
            cfar_meta["os_rank"] = os_rank(det.cfar)
        cfar_meta["alpha"] = self.alpha
        v = np.arange(self.n_pad) - self.n_pad // 2
        return {
            "mode": "cfar",
            "map": {
                **det.map.model_dump(mode="json"),
                "shape": [self.n_pad, self.k_pad],
                "notch_bins": effective_notch_bins(det.map),
                "notched_rows": [int(x) for x in v[self._notch_rows]],
                "non_training_rows": [int(x) for x in v[~self._train_rows]],
            },
            "cfar": cfar_meta,
            "threshold": {
                "kind": det.threshold,
                "value": float(self.threshold),
                "pfa_cell": det.cfar.pfa_cell,
                "system_pfa": det.system_pfa,
                "source": self.source_meta,
            },
            "oracle_gate_cells": det.oracle_gate_cells,
            "noise": {
                "seed": self.noise_seed,
                "n0_dbm_per_re": float(self.num.n0_dbm_per_re(self.nf_db)),
                "sigma_h2_db": {k: 10.0 * math.log10(v) for k, v in self.sigma2.items()},
                "key": noise_key(self.noise_seed, 0),
            },
            "achieved": {
                # Interval at the effective sample n / phi, phi = Pearson
                # dispersion of the per-map counts (neighbouring cells of a
                # windowed / zero-padded map are correlated).
                "cell": rate_block(
                    self.cell_fa, self.cell_trials,
                    dispersion(self._map_cell_fa, self._map_cell_trials),
                ),
                "link_frame": rate_block(self.map_fa, self.maps),
                "system": rate_block(self.frame_fa, self.frames),
            },
            "false_alarm_note": FALSE_ALARM_NOTE,
            "detections_total": self.detections_total,
            "detections_stored": self.detections_stored,
            "frames": self.frames,
        }
