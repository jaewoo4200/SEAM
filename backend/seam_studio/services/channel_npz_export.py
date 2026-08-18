"""Per-link channel dataset export in the AODT/HYRAY npz layout.

Writes ``export/channel_npz/channel_dataset.npz`` with the 13 canonical keys a
lab AODT/HYRAY reference file carries, so a consumer written against those
datasets can load a SEAM export without a single rename:

===========================  ===============  =========================
key                          shape            dtype
===========================  ===============  =========================
num_TRP                      ()               int64
num_trajectory               ()               int64
batch                        ()               int64
dataset_TRP_pos              (T, 3)           float32
dataset_TRP_ori_angle        (T, 2)           float32
dataset_UE_pos               (U, 3)           float32
sorted_dataset_ampl          (U, T, P)        complex128
sorted_dataset_azimuth       (U, T, P)        float32
sorted_dataset_elevation     (U, T, P)        float32
sorted_dataset_toa           (U, T, P)        float32
is_nlos                      (U, T)           bool
ue_id                        (U,)             int32
time_idx                     (U,)             int32
===========================  ===============  =========================

T = transmitters (TRPs), U = UE positions, P = ``max_paths``. Per-link path
axes are sorted strongest-first by |amplitude| and zero-padded to P; links the
solver found no path for stay all-zero with ``is_nlos = True``.

Conventions (also written verbatim into the sibling ``metadata.json``):

- ``sorted_dataset_ampl`` is the LINEAR complex channel amplitude
  ``10 ** ((path_gain_db + normalization_db) / 20) * exp(1j * phase_rad)``.
  ``normalization_db`` defaults to 0 (SEAM's own physical gain); the lab's
  AODT files carry a fixed -5.06 dB offset, so pass ``normalization_db=-5.06``
  to match them numerically.
- ``sorted_dataset_azimuth`` / ``_elevation`` are DEPARTURE angles in the
  emitting TX's LOCAL array frame, in RADIANS: azimuth is
  ``atan2(y_local, x_local)`` in (-pi, pi], elevation is the ZENITH angle
  ``arccos(z_local)`` in [0, pi] (NOT the elevation-above-horizon that
  ``RayPath.aod_deg`` reports).
- ``sorted_dataset_toa`` is absolute propagation delay in SECONDS.
- ``dataset_TRP_ori_angle`` is ``[yaw_deg, pitch_deg]`` of each TX device, in
  DEGREES (the reference files store degree-valued orientation pairs while the
  per-path angle arrays are radians).

The local array frame comes from each TX Device's own ``orientation_deg``
([yaw, pitch, roll] degrees), rotated exactly the way the solver orients the
array: ``sionna_backend`` hands Sionna ``orientation=radians(orientation_deg)``
and sionna-rt builds ``R = Rz(yaw) @ Ry(pitch) @ Rx(roll)`` (verified against
``sionna.rt.utils.rotation_matrix`` in this repo's venv). World -> local is
therefore ``R.T @ v_world``, which is what :func:`local_frame_matrix` returns
the ``R`` for. Getting this wrong silently rotates every departure angle, so
the convention is pinned by tests.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Callable, Optional

from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterialLibrary
from seam_studio.schemas.results import ChannelNpzExportRequest
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.simulation_backends.base import RayTracingBackend

EXPORT_DIR_REL = "export/channel_npz"
NPZ_NAME = "channel_dataset.npz"
METADATA_NAME = "metadata.json"

# The exact key set (and order) a HYRAY/AODT reference npz carries. Kept as a
# module constant so the export test can assert parity without restating it.
NPZ_KEYS: tuple[str, ...] = (
    "num_TRP",
    "num_trajectory",
    "batch",
    "dataset_TRP_pos",
    "dataset_TRP_ori_angle",
    "dataset_UE_pos",
    "sorted_dataset_ampl",
    "sorted_dataset_azimuth",
    "sorted_dataset_elevation",
    "sorted_dataset_toa",
    "is_nlos",
    "ue_id",
    "time_idx",
)

# Base id of the ephemeral receiver walked over the UE positions. It only ever
# exists inside a per-UE scene COPY, so the stored scene never sees it.
_PROBE_ID = "seam_npz_ue"


class ChannelNpzExportError(ValueError):
    """Export cannot run as requested (no TX, unknown id, empty UE list)."""


def local_frame_matrix(orientation_deg) -> "list[list[float]]":
    """``R`` (local -> world) for a device orientation ``[yaw, pitch, roll]`` deg.

    Mirrors sionna-rt's ``rotation_matrix``: ``R = Rz(yaw) Ry(pitch) Rx(roll)``
    with the angles taken in radians. The inverse (world -> local, what the
    departure-angle conversion needs) is ``R.T`` because ``R`` is orthonormal.
    """
    a, b, g = (math.radians(float(v)) for v in orientation_deg)
    ca, sa = math.cos(a), math.sin(a)
    cb, sb = math.cos(b), math.sin(b)
    cg, sg = math.cos(g), math.sin(g)
    return [
        [ca * cb, ca * sb * sg - sa * cg, ca * sb * cg + sa * sg],
        [sa * cb, sa * sb * sg + ca * cg, sa * sb * cg - ca * sg],
        [-sb, cb * sg, cb * cg],
    ]


def _resolve_txs(scene: Scene, tx_ids: Optional[list[str]]) -> list[Device]:
    txs = [d for d in scene.devices if d.kind == "tx"]
    if tx_ids is None:
        if not txs:
            raise ChannelNpzExportError(
                "scene has no transmitter to build a channel dataset from"
            )
        return txs
    by_id = {d.id: d for d in txs}
    resolved: list[Device] = []
    for wanted in tx_ids:
        dev = by_id.get(wanted)
        if dev is None:
            raise ChannelNpzExportError(f"unknown tx device: {wanted}")
        resolved.append(dev)
    if not resolved:
        raise ChannelNpzExportError("tx_ids selected no transmitter")
    return resolved


def _probe_device(scene: Scene) -> Device:
    """Ephemeral RX cloned from the project's first receiver (antenna and
    orientation included, so the export sees the UE the project authored) with
    a non-colliding id."""
    taken = {d.id for d in scene.devices}
    probe_id = _PROBE_ID
    n = 1
    while probe_id in taken:
        probe_id = f"{_PROBE_ID}_{n}"
        n += 1
    source = next((d for d in scene.devices if d.kind == "rx"), None)
    if source is None:
        return Device(id=probe_id, name="npz ue probe", kind="rx", position=[0.0, 0.0, 0.0])
    return source.model_copy(deep=True, update={"id": probe_id, "name": "npz ue probe"})


def export_channel_npz(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: ChannelNpzExportRequest,
    ue_positions: list[list[float]],
    tick: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Solve every (UE, TX) link and write the npz. Returns a summary dict.

    Nothing is persisted beyond the export directory: each UE is solved on a
    DEEP COPY of the scene whose devices are the selected TXs plus one
    ephemeral RX probe, exactly like ``mesh_radio_map``'s probe receivers, so
    ``scene.seam.json`` and ``results/`` are never touched.
    """
    import numpy as np

    if not ue_positions:
        raise ChannelNpzExportError("no UE positions to export")

    txs = _resolve_txs(scene, request.tx_ids)
    probe = _probe_device(scene)

    n_ue = len(ue_positions)
    n_tx = len(txs)
    n_path = request.max_paths

    ampl = np.zeros((n_ue, n_tx, n_path), dtype=np.complex128)
    azimuth = np.zeros((n_ue, n_tx, n_path), dtype=np.float32)
    elevation = np.zeros((n_ue, n_tx, n_path), dtype=np.float32)
    toa = np.zeros((n_ue, n_tx, n_path), dtype=np.float32)
    # Default True: a link with no solved path is not a line-of-sight link.
    is_nlos = np.ones((n_ue, n_tx), dtype=bool)

    # World -> local rotation per TX, from the device's own orientation.
    world_to_local = [np.asarray(local_frame_matrix(t.orientation_deg)).T for t in txs]
    # Amplitude scale for the requested normalization (voltage, hence /20).
    norm_scale = 10.0 ** (float(request.normalization_db) / 20.0)

    warnings: list[str] = []
    truncated_links = 0
    empty_links = 0
    total_paths = 0
    started = time.monotonic()

    for u, position in enumerate(ue_positions):
        step = scene.model_copy(deep=True)
        step.devices = [t.model_copy(deep=True) for t in txs] + [
            probe.model_copy(deep=True, update={"position": [float(c) for c in position]})
        ]
        cfg = config.model_copy(
            update={"tx_ids": [t.id for t in txs], "rx_ids": [probe.id]}
        )
        result = backend.simulate_paths(project_dir, step, library, cfg)
        for w in result.warnings:
            if u == 0 or w not in warnings:
                warnings.append(w)

        per_tx: dict[str, list] = {}
        for p in result.paths:
            if p.rx_id != probe.id:
                continue
            per_tx.setdefault(p.tx_id, []).append(p)

        for t, tx in enumerate(txs):
            link = per_tx.get(tx.id, [])
            if not link:
                empty_links += 1
                continue
            rot = world_to_local[t]
            amps: list[complex] = []
            azs: list[float] = []
            els: list[float] = []
            tas: list[float] = []
            for p in link:
                gain_db = (
                    p.path_gain_db
                    if p.path_gain_db is not None
                    else p.power_dbm - tx.power_dbm
                )
                amps.append(
                    10.0 ** (float(gain_db) / 20.0)
                    * norm_scale
                    * complex(math.cos(p.phase_rad), math.sin(p.phase_rad))
                )
                # RayPath.aod_deg is [azimuth, elevation-above-XY] in WORLD
                # degrees; rotate the unit direction into the TX array frame
                # and re-read it as (azimuth, ZENITH) radians.
                az_w, el_w = (
                    math.radians(p.aod_deg[0]),
                    math.radians(p.aod_deg[1]),
                ) if p.aod_deg else (0.0, 0.0)
                v_world = np.array(
                    [
                        math.cos(el_w) * math.cos(az_w),
                        math.cos(el_w) * math.sin(az_w),
                        math.sin(el_w),
                    ]
                )
                v_local = rot @ v_world
                azs.append(math.atan2(float(v_local[1]), float(v_local[0])))
                els.append(math.acos(max(-1.0, min(1.0, float(v_local[2])))))
                tas.append(float(p.delay_ns) * 1e-9)

            order = np.argsort(-np.abs(np.asarray(amps, dtype=np.complex128)))
            if len(order) > n_path:
                truncated_links += 1
                order = order[:n_path]
            k = len(order)
            total_paths += k
            ampl[u, t, :k] = np.asarray(amps, dtype=np.complex128)[order]
            azimuth[u, t, :k] = np.asarray(azs, dtype=np.float32)[order]
            elevation[u, t, :k] = np.asarray(els, dtype=np.float32)[order]
            toa[u, t, :k] = np.asarray(tas, dtype=np.float32)[order]
            is_nlos[u, t] = not any(p.path_type == "los" for p in link)

        if tick is not None:
            tick(u + 1, n_ue)

    elapsed_s = time.monotonic() - started

    ue_id = (
        np.asarray(request.ue_ids, dtype=np.int32)
        if request.ue_ids is not None
        else np.arange(n_ue, dtype=np.int32)
    )
    time_idx = (
        np.asarray(request.time_idx, dtype=np.int32)
        if request.time_idx is not None
        else np.zeros(n_ue, dtype=np.int32)
    )

    if truncated_links:
        warnings.append(
            f"{truncated_links} link(s) had more than max_paths={n_path} paths; "
            "the weakest were dropped (arrays keep the strongest max_paths)"
        )
    if empty_links:
        warnings.append(
            f"{empty_links} of {n_ue * n_tx} link(s) produced no path; their "
            "rows are zero-padded and flagged is_nlos=True"
        )

    out_dir = project_dir / EXPORT_DIR_REL
    out_dir.mkdir(parents=True, exist_ok=True)
    npz_path = out_dir / NPZ_NAME
    np.savez_compressed(
        npz_path,
        num_TRP=np.int64(n_tx),
        num_trajectory=np.int64(len(set(int(v) for v in ue_id.tolist()))),
        batch=np.int64(request.batch),
        dataset_TRP_pos=np.asarray([list(t.position) for t in txs], dtype=np.float32),
        dataset_TRP_ori_angle=np.asarray(
            [[float(t.orientation_deg[0]), float(t.orientation_deg[1])] for t in txs],
            dtype=np.float32,
        ),
        dataset_UE_pos=np.asarray(
            [[float(c) for c in p] for p in ue_positions], dtype=np.float32
        ),
        sorted_dataset_ampl=ampl,
        sorted_dataset_azimuth=azimuth,
        sorted_dataset_elevation=elevation,
        sorted_dataset_toa=toa,
        is_nlos=is_nlos,
        ue_id=ue_id,
        time_idx=time_idx,
    )

    metadata = {
        "generator": "seam-studio",
        "layout": "aodt_hyray_npz_v1",
        "scene_id": scene.scene_id,
        "backend": backend.name,
        "tx_ids": [t.id for t in txs],
        "num_tx": n_tx,
        "num_ue": n_ue,
        "max_paths": n_path,
        "normalization_db": float(request.normalization_db),
        "conventions": {
            "sorted_dataset_ampl": (
                "linear complex amplitude 10**((path_gain_db + "
                "normalization_db)/20) * exp(1j*phase_rad), strongest-first, "
                "zero-padded to max_paths"
            ),
            "sorted_dataset_azimuth": (
                "departure azimuth in the TX device's local array frame "
                "(radians, atan2(y,x) in (-pi, pi])"
            ),
            "sorted_dataset_elevation": (
                "departure ZENITH angle in the TX device's local array frame "
                "(radians, 0 = +z_local)"
            ),
            "sorted_dataset_toa": "absolute propagation delay in seconds",
            "dataset_TRP_ori_angle": "[yaw_deg, pitch_deg] of each TX device",
            "local_frame": (
                "R = Rz(yaw) Ry(pitch) Rx(roll) from Device.orientation_deg, "
                "matching sionna-rt's rotation_matrix; world -> local uses R.T"
            ),
        },
        "config": config.model_dump(mode="json"),
        "elapsed_s": round(elapsed_s, 3),
        "warnings": warnings,
    }
    (out_dir / METADATA_NAME).write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )

    return {
        "export_dir": EXPORT_DIR_REL,
        "files": [f"{EXPORT_DIR_REL}/{NPZ_NAME}", f"{EXPORT_DIR_REL}/{METADATA_NAME}"],
        "shapes": {
            "dataset_TRP_pos": [n_tx, 3],
            "dataset_TRP_ori_angle": [n_tx, 2],
            "dataset_UE_pos": [n_ue, 3],
            "sorted_dataset_ampl": [n_ue, n_tx, n_path],
            "is_nlos": [n_ue, n_tx],
        },
        "num_tx": n_tx,
        "num_ue": n_ue,
        "link_count": n_ue * n_tx,
        "path_count": total_paths,
        "max_paths": n_path,
        "size_bytes": npz_path.stat().st_size,
        "elapsed_s": round(elapsed_s, 3),
        "warnings": warnings,
    }
