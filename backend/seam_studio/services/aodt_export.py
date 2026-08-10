"""Export SEAM results as NVIDIA AODT results-schema parquet tables.

Companion to :mod:`rfdata_export` (the AODT *viewer* JSON/CSV contract): this
writer targets AODT's OFFICIAL result tables
(docs.nvidia.com/aerial/aodt/results-schemas), so a SEAM solve can be consumed
by tooling built against AODT ground truth. One ``<table>.parquet`` per table
lands in ``export/aodt/`` inside the project:

    ues, scatterers, rus, dus, panels, patterns, time_info, cfrs, cirs, raypaths

plus ``id_map.json``, which records the string id <-> AODT integer id mapping
(devices, prims, actors, panels) that the parquet rows can no longer carry.

NOT written: ``telemetry`` and ``ran_config``. Both are RAN-simulation outputs
(scheduler/PHY KPIs, gNB configuration) that a ray-tracing pipeline never
produces; emitting empty ones would claim coverage SEAM does not have.

Fidelity caveats, all deliberate:
- ``normals`` are zeros - SEAM records the interaction point and the prim it
  belongs to, never the surface normal there;
- ``object_ids`` repeat ``prim_ids``: SEAM has no separate USD object table;
- one antenna element per end - ``ru_ant_el``/``ue_ant_el`` are (0, 0[, 0]) and
  every amplitude list holds a single entry, because the path solver resolves
  per-LINK coefficients, not per-element ones;
- ``patterns`` is a single isotropic placeholder row.

``raypaths`` list alignment: ``points`` is the whole polyline (TX, every
interaction, RX), so ``normals``/``prim_ids``/``object_ids``/
``vegetation_depths`` are parallel to it, while ``interaction_types`` is
"emission" plus one entry per bounce (``len(points) - 1``) - the arrival at the
RX is not an interaction and AODT's vocabulary has no token for it. Endpoints
carry prim/object id -1.

Amplitudes are CHANNEL coefficients (not received power): magnitude
10^(path_gain_db/20), phase from the path's carrier phase. cirs/cfrs are built
from the same taps, so |H|^2 is a linear channel gain.

pyarrow is OPTIONAL, exactly as in :mod:`aodt_import`: when missing we raise
the typed :class:`AodtExportUnavailable` so the API layer answers 409, not 500.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Optional

from ..schemas.results import (
    AodtExportRequest,
    PathResultSet,
    PlaybackResultSet,
    RayPath,
)
from ..schemas.scene import Scene
from ..schemas.simulation import SimulationConfig

EXPORT_DIR_REL = "export/aodt"

C_M_S = 299_792_458.0

# The AODT tables this exporter produces (telemetry/ran_config are RAN-only).
TABLES: tuple[str, ...] = (
    "ues",
    "scatterers",
    "rus",
    "dus",
    "panels",
    "patterns",
    "time_info",
    "cfrs",
    "cirs",
    "raypaths",
)


class AodtExportUnavailable(RuntimeError):
    """pyarrow is not installed, so AODT parquet cannot be written."""


class AodtExportError(ValueError):
    """The requested source result is missing or carries nothing to export."""


def _require_pyarrow():
    try:
        import pyarrow as pa
        import pyarrow.parquet as pq

        return pa, pq
    except ImportError as exc:  # pragma: no cover - exercised via the 409 path
        raise AodtExportUnavailable(
            "pyarrow is required for AODT export; run "
            "\"pip install 'seam-backend[results]'\" (which pulls pyarrow) in "
            "the backend venv."
        ) from exc


# ------------------------------------------------------------------ schemas


def _schemas(pa) -> dict:
    """Explicit AODT column names/types, built once per export.

    Tuple-typed AODT columns split two ways: coordinate triples become
    ``fixed_size_list<float32>[3]`` (parquet round-trips them as plain lists,
    which is what our own reader consumes), while the antenna-element
    (h, v[, p]) tuples become named structs - nothing reads them back, and the
    field names document the ordering.
    """
    f32 = pa.float32()
    u32 = pa.uint32()
    i32 = pa.int32()
    vec3 = pa.list_(f32, 3)
    list_vec3 = pa.list_(vec3)
    list2_vec3 = pa.list_(pa.list_(vec3))
    list_f32 = pa.list_(f32)
    list2_f32 = pa.list_(pa.list_(f32))
    list_u32 = pa.list_(u32)
    list2_u32 = pa.list_(pa.list_(u32))
    ant3 = pa.struct([("h", u32), ("v", u32), ("p", u32)])
    ant2 = pa.struct([("h", u32), ("v", u32)])

    return {
        "ues": pa.schema(
            [
                ("ID", u32),
                ("is_manual", pa.bool_()),
                ("is_manual_mobility", pa.bool_()),
                ("radiated_power", f32),
                ("height", f32),
                ("mech_tilt", f32),
                ("panel", list_u32),
                ("batch_indices", list_u32),
                ("waypoint_ids", list2_u32),
                ("waypoint_points", list2_vec3),
                ("waypoint_stops", list2_f32),
                ("waypoint_speeds", list2_f32),
                ("trajectory_ids", list2_u32),
                ("trajectory_points", list2_vec3),
                ("trajectory_stops", list2_f32),
                ("trajectory_speeds", list2_f32),
                ("route_positions", list2_vec3),
                ("route_orientations", list2_vec3),
                ("route_speeds", list2_f32),
                ("route_times", list2_f32),
                ("bler_target", f32),
                ("is_indoor_mobility", pa.bool_()),
            ]
        ),
        "scatterers": pa.schema(
            [
                ("ID", u32),
                ("is_indoor_mobility", pa.bool_()),
                ("is_3d", pa.bool_()),
                ("is_manual", pa.bool_()),
                ("batch_indices", list_u32),
                ("route_positions", list2_vec3),
                ("route_orientations", list2_vec3),
                ("route_speeds", list2_f32),
                ("route_times", list2_f32),
            ]
        ),
        "rus": pa.schema(
            [
                ("ID", u32),
                ("subcarrier_spacing", f32),
                ("fft_size", u32),
                ("radiated_power", f32),
                ("height", f32),
                ("mech_azimuth", f32),
                ("mech_tilt", f32),
                ("panel", list_u32),
                ("position", list_f32),
                ("du_id", u32),
                ("du_manual_assign", pa.bool_()),
            ]
        ),
        "dus": pa.schema(
            [
                ("ID", u32),
                ("subcarrier_spacing", f32),
                ("fft_size", u32),
                ("num_antennas", u32),
                ("reference_freq", f32),
                ("max_channel_bandwidth", f32),
                ("position", list_f32),
            ]
        ),
        "panels": pa.schema(
            [
                ("panel_id", u32),
                ("panel_name", pa.string()),
                ("antenna_names", pa.list_(pa.string())),
                ("antenna_pattern_indices", list_u32),
                ("frequencies", list_f32),
                ("thetas", list_f32),
                ("phis", list_f32),
                ("reference_freq", f32),
                ("dual_polarized", pa.uint8()),
                ("num_loc_antenna_horz", u32),
                ("num_loc_antenna_vert", u32),
                ("antenna_spacing_horz", f32),
                ("antenna_spacing_vert", f32),
                ("antenna_roll_angle_first_polz", f32),
                ("antenna_roll_angle_second_polz", f32),
            ]
        ),
        "patterns": pa.schema(
            [
                ("pattern_id", u32),
                ("pattern_type", u32),
                ("e_theta_re", list2_f32),
                ("e_theta_im", list2_f32),
                ("e_phi_re", list2_f32),
                ("e_phi_im", list2_f32),
            ]
        ),
        "time_info": pa.schema(
            [
                ("time_idx", u32),
                ("batch_idx", u32),
                ("slot_idx", u32),
                ("symbol_idx", u32),
            ]
        ),
        "cfrs": pa.schema(
            [
                ("time_idx", u32),
                ("ru_id", u32),
                ("ue_id", u32),
                ("ru_ant_el", ant3),
                ("ue_ant_el", ant3),
                ("cfr_re", list_f32),
                ("cfr_im", list_f32),
            ]
        ),
        "cirs": pa.schema(
            [
                ("time_idx", u32),
                ("ru_id", u32),
                ("ue_id", u32),
                ("ru_ant_el", ant3),
                ("ue_ant_el", ant3),
                ("cir_re", list_f32),
                ("cir_im", list_f32),
                ("cir_delay", list_f32),
            ]
        ),
        "raypaths": pa.schema(
            [
                ("time_idx", u32),
                ("ru_id", u32),
                ("ue_id", u32),
                ("ru_ant_el", ant2),
                ("ue_ant_el", ant2),
                ("interaction_types", pa.list_(pa.string())),
                ("points", list_vec3),
                ("normals", list_vec3),
                ("ampl_re", list_f32),
                ("ampl_im", list_f32),
                ("prim_ids", pa.list_(i32)),
                ("object_ids", pa.list_(i32)),
                ("vegetation_depths", list_f32),
            ]
        ),
    }


# ------------------------------------------------------------------ helpers

_ANT_EL3 = {"h": 0, "v": 0, "p": 0}
_ANT_EL2 = {"h": 0, "v": 0}


def _vec3(v) -> list[float]:
    return [float(v[0]), float(v[1]), float(v[2])]


def _amplitude(path: RayPath, tx_power_dbm: Optional[float]) -> tuple[float, float]:
    """Complex channel coefficient (re, im) of one path.

    Magnitude is 10^(gain_dB/20) so |a|^2 is the linear channel gain; the phase
    is the path's total carrier phase. Backends that leave path_gain_db unset
    fall back to power_dbm referenced to that link's TX power.
    """
    gain_db = path.path_gain_db
    if gain_db is None:
        gain_db = path.power_dbm - (tx_power_dbm if tx_power_dbm is not None else 0.0)
    mag = 10.0 ** (gain_db / 20.0)
    return mag * math.cos(path.phase_rad), mag * math.sin(path.phase_rad)


def _cfr(taps: list[tuple[float, float, float]], bandwidth_hz: float, fft_size: int):
    """H(f_k) = sum_l a_l exp(-j 2 pi f_k tau_l) on an FFT bin grid.

    ``fft_size`` tones step by bandwidth/fft_size across [-B/2, +B/2), the
    baseband grid AODT's cfrs rows describe. Not shared with
    channel_analysis.compute_cfr: that helper returns magnitudes in dB and
    drops the phase these rows must carry.
    """
    step = bandwidth_hz / float(fft_size)
    re_out: list[float] = []
    im_out: list[float] = []
    for k in range(fft_size):
        f = (k - fft_size / 2.0) * step
        acc_re = 0.0
        acc_im = 0.0
        two_pi_f = 2.0 * math.pi * f
        for a_re, a_im, tau in taps:
            angle = -two_pi_f * tau
            c = math.cos(angle)
            s = math.sin(angle)
            acc_re += a_re * c - a_im * s
            acc_im += a_re * s + a_im * c
        re_out.append(acc_re)
        im_out.append(acc_im)
    return re_out, im_out


def _panel_key(device) -> tuple:
    a = device.antenna
    return (
        a.num_rows,
        a.num_cols,
        a.vertical_spacing,
        a.horizontal_spacing,
        a.polarization,
    )


def _snapshots(
    request: AodtExportRequest,
    paths: Optional[PathResultSet],
    playback: Optional[PlaybackResultSet],
) -> list[tuple[int, float, list[RayPath]]]:
    """(time_idx, time_s, paths) per exported time step.

    source="paths" is a single t=0 snapshot; source="playback" yields one time
    index per pack frame, each carrying that frame's (capped) ray list.
    """
    if request.source == "playback":
        if playback is None:
            raise AodtExportError(
                "AODT export source='playback' needs a stored playback result"
            )
        return [
            (
                i,
                float(f.time_s) if f.time_s is not None else float(f.index),
                list(f.paths),
            )
            for i, f in enumerate(playback.frames)
        ]
    if paths is None:
        raise AodtExportError("AODT export source='paths' needs a stored paths result")
    return [(0, 0.0, list(paths.paths))]


# ------------------------------------------------------------- table builders


def _panel_rows(scene: Scene, config: SimulationConfig) -> tuple[list[dict], dict]:
    """One panel row per DISTINCT antenna configuration, plus device -> index."""
    wavelength_m = C_M_S / config.frequency_hz
    devices = sorted(
        [d for d in scene.devices if d.kind == "tx"], key=lambda d: d.id
    ) + sorted([d for d in scene.devices if d.kind == "rx"], key=lambda d: d.id)

    rows: list[dict] = []
    by_key: dict[tuple, int] = {}
    panel_by_device: dict[str, int] = {}
    for dev in devices:
        key = _panel_key(dev)
        if key not in by_key:
            idx = len(rows)
            by_key[key] = idx
            a = dev.antenna
            rows.append(
                {
                    "panel_id": idx,
                    "panel_name": f"seam_panel_{idx}",
                    "antenna_names": [
                        f"el_{i}" for i in range(a.num_rows * a.num_cols)
                    ],
                    "antenna_pattern_indices": [0],
                    "frequencies": [config.frequency_hz],
                    "thetas": [],
                    "phis": [],
                    "reference_freq": config.frequency_hz,
                    "dual_polarized": 1 if a.polarization in ("VH", "cross") else 0,
                    "num_loc_antenna_horz": a.num_cols,
                    "num_loc_antenna_vert": a.num_rows,
                    # Spacing is authored in wavelengths (sionna PlanarArray).
                    "antenna_spacing_horz": a.horizontal_spacing * wavelength_m * 100.0,
                    "antenna_spacing_vert": a.vertical_spacing * wavelength_m * 100.0,
                    "antenna_roll_angle_first_polz": 0.0,
                    "antenna_roll_angle_second_polz": 0.0,
                }
            )
        panel_by_device[dev.id] = by_key[key]
    return rows, panel_by_device


def _ru_rows(
    scene: Scene,
    request: AodtExportRequest,
    panel_by_device: dict[str, int],
    ru_ids: dict[str, int],
) -> list[dict]:
    rows: list[dict] = []
    for dev in sorted([d for d in scene.devices if d.kind == "tx"], key=lambda d: d.id):
        rows.append(
            {
                "ID": ru_ids[dev.id],
                "subcarrier_spacing": request.subcarrier_spacing_hz,
                "fft_size": request.fft_size,
                # dBm -> W.
                "radiated_power": 10.0 ** ((dev.power_dbm - 30.0) / 10.0),
                "height": dev.position[2],
                "mech_azimuth": dev.orientation_deg[0],
                "mech_tilt": dev.orientation_deg[1],
                "panel": [panel_by_device[dev.id]],
                "position": _vec3(dev.position),
                "du_id": 0,
                "du_manual_assign": False,
            }
        )
    return rows


def _du_rows(
    scene: Scene, config: SimulationConfig, request: AodtExportRequest
) -> list[dict]:
    """A single DU: SEAM has no RAN topology, so every RU hangs off DU 0."""
    num_antennas = sum(
        d.antenna.num_rows * d.antenna.num_cols
        for d in scene.devices
        if d.kind == "tx"
    )
    return [
        {
            "ID": 0,
            "subcarrier_spacing": request.subcarrier_spacing_hz,
            "fft_size": request.fft_size,
            "num_antennas": num_antennas,
            "reference_freq": config.frequency_hz / 1e6,
            "max_channel_bandwidth": config.bandwidth_hz / 1e6,
            "position": [0.0, 0.0, 0.0],
        }
    ]


def _ue_rows(
    scene: Scene,
    panel_by_device: dict[str, int],
    ue_ids: dict[str, int],
    playback: Optional[PlaybackResultSet],
    request: AodtExportRequest,
) -> list[dict]:
    routes: dict[str, dict] = {}
    if request.source == "playback" and playback is not None and playback.frames:
        positions = [_vec3(f.rx_position) for f in playback.frames]
        times = [
            float(f.time_s) if f.time_s is not None else float(f.index)
            for f in playback.frames
        ]
        routes[playback.rx_id] = {
            "route_positions": [positions],
            "route_orientations": [[[0.0, 0.0, 0.0] for _ in positions]],
            "route_speeds": [[0.0 for _ in positions]],
            "route_times": [times],
        }

    rows: list[dict] = []
    for dev in sorted([d for d in scene.devices if d.kind == "rx"], key=lambda d: d.id):
        route = routes.get(
            dev.id,
            {
                "route_positions": [],
                "route_orientations": [],
                "route_speeds": [],
                "route_times": [],
            },
        )
        rows.append(
            {
                "ID": ue_ids[dev.id],
                "is_manual": False,
                "is_manual_mobility": False,
                "radiated_power": 10.0 ** ((dev.power_dbm - 30.0) / 10.0),
                "height": dev.position[2],
                "mech_tilt": dev.orientation_deg[1],
                "panel": [panel_by_device[dev.id]],
                "batch_indices": [0],
                # Waypoint/trajectory authoring is an AODT UI concept; SEAM
                # routes arrive as solved frames, so these stay empty.
                "waypoint_ids": [],
                "waypoint_points": [],
                "waypoint_stops": [],
                "waypoint_speeds": [],
                "trajectory_ids": [],
                "trajectory_points": [],
                "trajectory_stops": [],
                "trajectory_speeds": [],
                "bler_target": 0.1,
                "is_indoor_mobility": False,
                **route,
            }
        )
    return rows


def _scatterer_rows(scene: Scene, scatterer_ids: dict[str, int]) -> list[dict]:
    """One row per actor WITH a trajectory (a static actor is scene geometry)."""
    rows: list[dict] = []
    for actor in sorted(scene.actors, key=lambda a: a.id):
        traj = actor.trajectory
        if traj is None or not traj.waypoints:
            continue
        pts = [_vec3(p) for p in traj.waypoints]
        dt = traj.dt_s
        times = [i * dt for i in range(len(pts))]
        speeds: list[float] = []
        for i in range(len(pts)):
            j = min(i + 1, len(pts) - 1)
            if j == i:
                speeds.append(speeds[-1] if speeds else 0.0)
                continue
            d = math.dist(pts[i], pts[j])
            speeds.append(d / dt)
        rows.append(
            {
                "ID": scatterer_ids[actor.id],
                "is_indoor_mobility": False,
                "is_3d": True,
                "is_manual": True,
                "batch_indices": [0],
                "route_positions": [pts],
                "route_orientations": [[[0.0, 0.0, 0.0] for _ in pts]],
                "route_speeds": [speeds],
                "route_times": [times],
            }
        )
    return rows


def _pattern_rows() -> list[dict]:
    """Isotropic placeholder: unit E_theta, no E_phi, one (theta, phi) sample.

    SEAM stores the antenna pattern by NAME ("iso", "tr38901", ...) and leaves
    the field values to the solver, so there is no sampled pattern to export.
    """
    return [
        {
            "pattern_id": 0,
            "pattern_type": 0,
            "e_theta_re": [[1.0]],
            "e_theta_im": [[0.0]],
            "e_phi_re": [[0.0]],
            "e_phi_im": [[0.0]],
        }
    ]


def _raypath_row(
    path: RayPath,
    time_idx: int,
    ru_id: int,
    ue_id: int,
    prim_index: dict[str, int],
    tx_power_dbm: Optional[float],
) -> dict:
    points = [_vec3(v) for v in path.vertices]
    types = ["emission"] + [i.type for i in path.interactions]
    # prim/object ids are parallel to points; the emission and arrival
    # endpoints belong to no surface (-1).
    prim_ids = [-1] * len(points)
    for k, inter in enumerate(path.interactions, start=1):
        if k >= len(points) - 1:
            break
        prim_ids[k] = (
            prim_index.get(inter.prim_id, -1) if inter.prim_id is not None else -1
        )
    a_re, a_im = _amplitude(path, tx_power_dbm)
    return {
        "time_idx": time_idx,
        "ru_id": ru_id,
        "ue_id": ue_id,
        "ru_ant_el": dict(_ANT_EL2),
        "ue_ant_el": dict(_ANT_EL2),
        "interaction_types": types,
        "points": points,
        "normals": [[0.0, 0.0, 0.0] for _ in points],
        "ampl_re": [a_re],
        "ampl_im": [a_im],
        "prim_ids": prim_ids,
        "object_ids": list(prim_ids),
        "vegetation_depths": [0.0 for _ in points],
    }


# --------------------------------------------------------------------- export


def export_aodt(
    project_dir: Path,
    scene: Scene,
    library,
    config: SimulationConfig,
    request: AodtExportRequest,
    paths: Optional[PathResultSet] = None,
    playback: Optional[PlaybackResultSet] = None,
) -> dict:
    """Write the AODT results-schema parquet set; return a summary dict.

    ``library`` is accepted for signature parity with the solve/export services
    (AODT's results schemas carry no material tables, so it is not read).
    Results are passed in already resolved, exactly like
    :func:`rfdata_export.export_rfdata` - the route owns store lookups.
    """
    pa, pq = _require_pyarrow()

    warnings: list[str] = []
    snapshots = _snapshots(request, paths, playback)

    tx_devices = sorted([d for d in scene.devices if d.kind == "tx"], key=lambda d: d.id)
    rx_devices = sorted([d for d in scene.devices if d.kind == "rx"], key=lambda d: d.id)
    ru_ids = {d.id: i for i, d in enumerate(tx_devices)}
    ue_ids = {d.id: i for i, d in enumerate(rx_devices)}
    tx_power = {d.id: d.power_dbm for d in tx_devices}
    prim_index = {pid: i for i, pid in enumerate(sorted(p.id for p in scene.prims))}
    scatterer_ids = {
        a.id: i
        for i, a in enumerate(
            sorted([a for a in scene.actors if a.trajectory], key=lambda a: a.id)
        )
    }

    panel_rows, panel_by_device = _panel_rows(scene, config)

    # Rows keyed by (time_idx, ru, ue) so cirs/cfrs stay one row per link.
    raypath_rows: list[dict] = []
    time_rows: list[dict] = []
    cir_rows: list[dict] = []
    cfr_rows: list[dict] = []
    unmapped: set[str] = set()

    for time_idx, _time_s, snapshot_paths in snapshots:
        time_rows.append(
            {
                "time_idx": time_idx,
                "batch_idx": 0,
                "slot_idx": time_idx,
                "symbol_idx": 0,
            }
        )
        taps_by_link: dict[tuple[int, int], list[tuple[float, float, float]]] = {}
        for path in snapshot_paths:
            ru = ru_ids.get(path.tx_id)
            ue = ue_ids.get(path.rx_id)
            if ru is None or ue is None:
                unmapped.add(path.tx_id if ru is None else path.rx_id)
                continue
            raypath_rows.append(
                _raypath_row(
                    path, time_idx, ru, ue, prim_index, tx_power.get(path.tx_id)
                )
            )
            a_re, a_im = _amplitude(path, tx_power.get(path.tx_id))
            taps_by_link.setdefault((ru, ue), []).append(
                (a_re, a_im, path.delay_ns * 1e-9)
            )

        for (ru, ue), taps in sorted(taps_by_link.items()):
            taps.sort(key=lambda t: t[2])  # AODT orders CIR taps by delay
            cir_rows.append(
                {
                    "time_idx": time_idx,
                    "ru_id": ru,
                    "ue_id": ue,
                    "ru_ant_el": dict(_ANT_EL3),
                    "ue_ant_el": dict(_ANT_EL3),
                    "cir_re": [t[0] for t in taps],
                    "cir_im": [t[1] for t in taps],
                    "cir_delay": [t[2] for t in taps],  # SECONDS
                }
            )
            cfr_re, cfr_im = _cfr(taps, config.bandwidth_hz, request.fft_size)
            cfr_rows.append(
                {
                    "time_idx": time_idx,
                    "ru_id": ru,
                    "ue_id": ue,
                    "ru_ant_el": dict(_ANT_EL3),
                    "ue_ant_el": dict(_ANT_EL3),
                    "cfr_re": cfr_re,
                    "cfr_im": cfr_im,
                }
            )

    if unmapped:
        warnings.append(
            "paths reference device ids that are not in the scene "
            f"({', '.join(sorted(unmapped))}); those rows were skipped"
        )
    if not raypath_rows:
        warnings.append("no ray paths matched a scene tx/rx pair; raypaths is empty")

    rows_by_table = {
        "ues": _ue_rows(scene, panel_by_device, ue_ids, playback, request),
        "scatterers": _scatterer_rows(scene, scatterer_ids),
        "rus": _ru_rows(scene, request, panel_by_device, ru_ids),
        "dus": _du_rows(scene, config, request),
        "panels": panel_rows,
        "patterns": _pattern_rows(),
        "time_info": time_rows,
        "cfrs": cfr_rows,
        "cirs": cir_rows,
        "raypaths": raypath_rows,
    }

    out = project_dir / EXPORT_DIR_REL
    out.mkdir(parents=True, exist_ok=True)
    # A previous run's tables must never survive into this one (a renamed or
    # dropped table would otherwise read as current data).
    for stale in out.glob("*.parquet"):
        stale.unlink()

    schemas = _schemas(pa)
    written: list[str] = []
    tables: dict[str, int] = {}
    for name in TABLES:
        rows = rows_by_table[name]
        pq.write_table(
            pa.Table.from_pylist(rows, schema=schemas[name]), out / f"{name}.parquet"
        )
        written.append(f"{EXPORT_DIR_REL}/{name}.parquet")
        tables[name] = len(rows)

    id_map = {
        "rus": ru_ids,
        "ues": ue_ids,
        "prims": prim_index,
        "scatterers": scatterer_ids,
        "panels": panel_by_device,
        "source": request.source,
        "omitted_tables": ["telemetry", "ran_config"],
    }
    (out / "id_map.json").write_text(json.dumps(id_map, indent=2), encoding="utf-8")
    written.append(f"{EXPORT_DIR_REL}/id_map.json")

    return {
        "export_dir": EXPORT_DIR_REL,
        "files": written,
        "tables": tables,
        "warnings": warnings,
    }
