"""GT-vs-DT playback pack builder (issue #3).

Replays a project's sensor manifest through the active backend: for each
selected frame the RX device is moved to the frame pose IN MEMORY (the stored
scene is never edited) and one paths solve plus one codebook beam sweep run.
Output frames align 1:1 with manifest frames by ``index``, so the dashboard
pairs the GT side (manifest files + inline gt) with the DT side without
re-keying anything.

Only solver output lands in the pack - GT scalars/curves stay in the manifest,
never duplicated here.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Optional

from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import PlaybackFrame, PlaybackResultSet
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.sensors import SensorFrame, SensorManifest
from seam_studio.schemas.simulation import (
    BeamformingRequest,
    PlaybackBuildRequest,
    SimulationConfig,
)
from seam_studio.services.measurement_validation import _subsample_evenly
from seam_studio.services.simulation_backends.base import (
    UNSAVED_RESULT_ID,
    RayTracingBackend,
)
from seam_studio.services.trajectory import _aggregate

# Emitted once per run when the backend gave no absolute reference power, so
# the beam curve carries only Nones instead of silently plotting bare gains.
_NO_REFERENCE_POWER = (
    "beam sweep returned no single-element reference power; beam_power_dbm is "
    "null for every frame (relative gains are not absolute dBm)"
)


def _wrap180(deg: float) -> float:
    """World azimuth into atan2's canonical (-180, 180] range, so pack angles
    are directly comparable with manifest GT (atan2(y, x) by contract)."""
    return -((-deg + 180.0) % 360.0 - 180.0)


def _resolve_tx(scene: Scene, request: PlaybackBuildRequest) -> Device:
    txs = [d for d in scene.devices if d.kind == "tx"]
    if request.tx_id is not None:
        tx = next((d for d in txs if d.id == request.tx_id), None)
        if tx is None:
            raise ValueError(f"unknown tx device: {request.tx_id}")
        return tx
    if not txs:
        raise ValueError("scene has no transmitter to replay the manifest against")
    return txs[0]


def _resolve_rx(
    scene: Scene, request: PlaybackBuildRequest, manifest: SensorManifest
) -> Device:
    """The device the per-frame pose moves: the request's rx_id, else the
    manifest's tracked entity, else the first rx. An id that names no rx device
    is an error - falling back would silently replay the wrong receiver."""
    rxs = [d for d in scene.devices if d.kind == "rx"]
    wanted = request.rx_id or manifest.entity_id
    if wanted is None:
        if not rxs:
            raise ValueError("scene has no receiver to move along the manifest frames")
        return rxs[0]
    rx = next((d for d in rxs if d.id == wanted), None)
    if rx is None:
        raise ValueError(f"unknown rx device: {wanted}")
    return rx


def _select_frames(
    manifest: SensorManifest,
    request: PlaybackBuildRequest,
    warnings: list[str],
) -> list[SensorFrame]:
    """Manifest frames by index, poseless ones dropped, strided, then capped at
    max_frames evenly with the first and last kept (so the pack still spans the
    whole drive at any budget)."""
    frames = sorted(manifest.frames, key=lambda f: f.index)
    posed = [f for f in frames if f.pose is not None]
    skipped = len(frames) - len(posed)
    if skipped:
        warnings.append(f"{skipped} frame(s) without a pose were skipped")
    return _subsample_evenly(posed[:: request.frame_stride], request.max_frames)


def _best_rx_row(
    sweep: Optional[list[list[Optional[float]]]],
) -> list[Optional[float]]:
    """The [rx][tx] sweep row holding the global peak, i.e. the TX-beam curve at
    the RX beam a real receiver would have selected. Row 0 when no cell is
    finite; empty when the backend produced no grid at all."""
    grid = sweep or []
    best_row, best = 0, None
    for r, row in enumerate(grid):
        for value in row:
            if value is None or not math.isfinite(value):
                continue
            if best is None or value > best:
                best, best_row = value, r
    return list(grid[best_row]) if grid else []


def build_playback(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: PlaybackBuildRequest,
    manifest: SensorManifest,
    tick: Optional[Callable[[int, int], None]] = None,
) -> PlaybackResultSet:
    """One paths solve + one codebook sweep per selected manifest frame."""
    tx = _resolve_tx(scene, request)
    rx = _resolve_rx(scene, request, manifest)

    warnings: list[str] = []
    frames = _select_frames(manifest, request, warnings)
    if not frames:
        raise ValueError(
            "no manifest frame carries a pose to replay (the playback pack moves "
            "the rx device to frames[].pose)"
        )

    out: list[PlaybackFrame] = []
    backend_name = backend.name
    axis_mode = "look_at"
    total = len(frames)
    for i, frame in enumerate(frames):
        pose = frame.pose
        # Per-frame scene copy: the moved RX must never reach the stored scene.
        scene_f = scene.model_copy(deep=True)
        for dev in scene_f.devices:
            if dev.id != rx.id:
                continue
            dev.position = [float(c) for c in pose.position]
            # An all-zero pose orientation is the schema default (pose carries
            # no heading), not an authored one - keep the device's own value.
            if any(float(a) != 0.0 for a in pose.orientation_deg):
                dev.orientation_deg = [float(a) for a in pose.orientation_deg]
        rx_position = [float(c) for c in pose.position]

        pr = backend.simulate_paths(project_dir, scene_f, library, config)
        backend_name = pr.backend
        # Serving link only: other TX/RX pairs in the same solve are neither
        # this link's power nor its delay spread.
        serving = [p for p in pr.paths if p.tx_id == tx.id and p.rx_id == rx.id]
        rss, _gain, tau_rms, _strongest, rss_coherent = _aggregate(
            [p.power_dbm for p in serving],
            [p.delay_ns for p in serving],
            tx.power_dbm,
            [p.phase_rad for p in serving],
        )

        bf_req = BeamformingRequest(
            config=config,
            tx_id=tx.id,
            rx_id=rx.id,
            tx_rows=request.tx_rows,
            tx_cols=request.tx_cols,
            rx_rows=request.rx_rows,
            rx_cols=request.rx_cols,
            mode="codebook_sweep",
            use_device_orientation=request.use_device_orientation,
            sweep_start_deg=request.sweep_start_deg,
            sweep_stop_deg=request.sweep_stop_deg,
            sweep_step_deg=request.sweep_step_deg,
        )
        bf = backend.simulate_beamforming(project_dir, scene_f, library, config, bf_req)

        row = _best_rx_row(bf.sweep_gain_db)
        reference = bf.single_element_dbm
        if reference is None and _NO_REFERENCE_POWER not in warnings:
            warnings.append(_NO_REFERENCE_POWER)
        curve: list[Optional[float]] = []
        for k in range(len(bf.sweep_angles_deg)):
            gain = row[k] if k < len(row) else None
            usable = (
                reference is not None
                and math.isfinite(reference)
                and gain is not None
                and math.isfinite(gain)
            )
            curve.append(reference + gain if usable else None)

        # WORLD azimuth (atan2(y, x) about +Z, positive CCW) of every swept
        # beam: axis_deg + the backend's sweep angle. The axis depends on what
        # the sweep was anchored to:
        #   mock  - its analytic lobe is centered on the geometric TX->RX
        #           bearing, so the angles are already world-referenced;
        #   device- angles are relative to the TX array broadside, which for
        #           orientation [0, 0, 0] is +x (pinned by
        #           tests/test_sionna_backend.py::
        #           test_codebook_sweep_sign_convention_and_orientation);
        #   look_at- the panels re-aim at each other, so the axis is the
        #           TX->RX bearing of THIS frame (the RX has moved; the scene's
        #           authored rx position is stale).
        if bf.backend == "mock":
            axis_mode, axis_deg = "mock0", 0.0
        elif request.use_device_orientation:
            axis_mode, axis_deg = "device", float(tx.orientation_deg[0])
        else:
            axis_mode = "look_at"
            axis_deg = math.degrees(
                math.atan2(
                    rx_position[1] - tx.position[1],
                    rx_position[0] - tx.position[0],
                )
            )

        # First frame's solver warnings verbatim; later frames contribute only
        # strings not already reported (a per-frame failure must still surface,
        # but a constant caveat must not repeat 400 times).
        for w in list(pr.warnings) + list(bf.warnings):
            if i == 0 or w not in warnings:
                warnings.append(w)

        # A peak sitting AT the sweep edge usually means the link bearing lies
        # outside the swept sector — the reported "best beam" is then just the
        # least-bad endpoint, not a tracked beam. Say so once (frame-agnostic
        # text so the dedup above collapses repeats).
        if bf.best_tx_angle_deg is not None and (
            abs(bf.best_tx_angle_deg - request.sweep_start_deg) < 1e-6
            or abs(bf.best_tx_angle_deg - request.sweep_stop_deg) < 1e-6
        ):
            edge_warning = (
                "beam peak sits at the sweep edge on some frames — the swept "
                "sector may not cover the link bearing; widen sweep_start_deg/"
                "sweep_stop_deg or re-aim the array orientation"
            )
            if edge_warning not in warnings:
                warnings.append(edge_warning)

        out.append(
            PlaybackFrame(
                index=frame.index,
                time_s=frame.time_s,
                rx_position=rx_position,
                # Wrapped into atan2's canonical range so the pack is directly
                # comparable with the manifest's GT convention; the chart and
                # lobe consumers handle the +/-180 seam.
                beam_azimuth_deg=[
                    _wrap180(axis_deg + a) for a in bf.sweep_angles_deg
                ],
                beam_power_dbm=curve,
                best_beam_deg=(
                    _wrap180(axis_deg + bf.best_tx_angle_deg)
                    if bf.best_tx_angle_deg is not None
                    else None
                ),
                rss_dbm=rss,
                rss_coherent_dbm=rss_coherent,
                tau_rms_ns=tau_rms,
                n_paths=len(serving),
                paths=sorted(serving, key=lambda p: p.power_dbm, reverse=True)[
                    : request.max_paths_per_frame
                ],
            )
        )
        if tick is not None:
            tick(i + 1, total)

    return PlaybackResultSet(
        result_id=UNSAVED_RESULT_ID,
        backend=backend_name,
        simulation_config_id=config.id,
        tx_id=tx.id,
        rx_id=rx.id,
        frames=out,
        warnings=warnings,
        metadata={
            "frame_count": len(out),
            "use_device_orientation": request.use_device_orientation,
            # How beam_azimuth_deg was anchored to world azimuth.
            "axis_mode": axis_mode,
        },
    )
