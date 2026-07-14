"""Export a project into the AODT-like viewer's RFData contract.

Produces the exact file set the FTC AODT viewer guide specifies, under
``export/rfdata/`` inside the project folder:

    scenario_meta.json     units/frequency/coordinate transform/time window
    devices.json           transmitters + receivers (positions in meters)
    paths.json             time-indexed ray paths (schema_version "1.0")
    trajectory.csv         per-waypoint UE metrics
    radio_map.csv          plane heatmap samples
    calibration_points.json 3 coordinate-check reference points

All positions are meters, Z-up (our canonical frame). The viewer converts to
Unreal centimeters via the coordinate_transform (scale 100).
"""

import csv
import io
import json
from pathlib import Path
from typing import Optional

from seam_studio.schemas.results import (
    PathResultSet,
    RadioMapResultSet,
    TrajectoryResultSet,
)
from seam_studio.schemas.scene import Scene
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.simulation_backends.sionna_backend import noise_floor_dbm

EXPORT_DIR_REL = "export/rfdata"

# our RayPath.path_type -> the guide's path type enum
_PATH_TYPE_MAP = {
    "los": "LOS",
    "reflection": "REFLECTION",
    "diffraction": "DIFFRACTION",
    "scattering": "SCATTERING",
    "transmission": "TRANSMISSION",
    "mixed": "REFLECTION",
}


def _scenario_meta(scene: Scene, config: SimulationConfig, created_at: str) -> dict:
    txs = [d for d in scene.devices if d.kind == "tx"]
    end_s = 0.0
    for ref in scene.result_sets:
        if ref.kind == "trajectory":
            end_s = max(end_s, 1.0)  # placeholder; refined from trajectory below
    return {
        "scenario_name": scene.name or scene.scene_id,
        "description": f"SEAM Studio export of {scene.scene_id}",
        "unit": "meter",
        "unreal_unit": "centimeter",
        "frequency_hz": config.frequency_hz,
        "tx_power_dbm": txs[0].power_dbm if txs else 0.0,
        "coordinate_transform": {
            "scale": 100.0,
            "axis_mapping": "XYZ_TO_XYZ",
            "offset_m": [0.0, 0.0, 0.0],
            "rotation_degrees": [0.0, 0.0, 0.0],
        },
        "time": {"start_s": 0.0, "end_s": end_s, "dt_s": 0.1},
        "created_at": created_at,
    }


def _devices(scene: Scene, config: SimulationConfig) -> dict:
    return {
        "transmitters": [
            {
                "id": d.id,
                "name": d.name or d.id,
                "position_m": list(d.position),
                "frequency_hz": config.frequency_hz,
                "power_dbm": d.power_dbm,
            }
            for d in scene.devices
            if d.kind == "tx"
        ],
        "receivers": [
            {
                "id": d.id,
                "name": d.name or d.id,
                "initial_position_m": list(d.position),
            }
            for d in scene.devices
            if d.kind == "rx"
        ],
    }


def _paths_json(paths: Optional[PathResultSet]) -> dict:
    frames: list[dict] = []
    if paths and paths.paths:
        # A single snapshot at t=0, grouped per receiver (ue).
        by_rx: dict[str, list] = {}
        for p in paths.paths:
            by_rx.setdefault(p.rx_id, []).append(p)
        for ue_id, plist in by_rx.items():
            frames.append(
                {
                    "time_s": 0.0,
                    "ue_id": ue_id,
                    "paths": [
                        {
                            "path_id": idx,
                            "type": _PATH_TYPE_MAP.get(p.path_type, "UNKNOWN"),
                            "power_db": round(p.power_dbm, 3),
                            "delay_ns": round(p.delay_ns, 4),
                            "points_m": [list(v) for v in p.vertices],
                            "object_ids": [
                                i.prim_id for i in p.interactions if i.prim_id
                            ],
                        }
                        for idx, p in enumerate(plist)
                    ],
                }
            )
    return {"schema_version": "1.0", "paths_by_time": frames}


def _trajectory_csv(
    trajectory: Optional[TrajectoryResultSet],
    scene: Scene,
    paths: Optional[PathResultSet],
    config: SimulationConfig,
) -> str:
    """Per-waypoint UE metrics, one row per trajectory sample.

    Multi-UE contract: the ``ue_id`` column is ALWAYS present (a fixed column
    in the AODT-viewer trajectory schema, not a conditional one), and rows are
    emitted in the result's native order — STEP-MAJOR for multi-UE routes
    (all UEs at step 0, then all UEs at step 1, ...), so the viewer can split
    the file by ``ue_id`` into per-UE sequences. A single-UE run is just the
    degenerate case: every row shares one ue_id, order == waypoint order.
    """
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["time_s", "ue_id", "x_m", "y_m", "z_m", "rss_dbm", "sinr_db", "path_gain_db"])

    def fmt(v):
        return "" if v is None else round(v, 3)

    # SNR reference (thermal + NF; no interference model, so sinr == snr).
    noise_floor = noise_floor_dbm(config)

    if trajectory and trajectory.samples:
        for s in trajectory.samples:
            w.writerow(
                [round(s.time_s, 4), s.ue_id, *[round(c, 4) for c in s.position],
                 fmt(s.rss_dbm), fmt(s.sinr_db), fmt(s.path_gain_db)]
            )
    elif paths and paths.paths:
        # No trajectory result: one row per (tx, rx) LINK from the path snapshot.
        # Grouping by (tx_id, rx_id) - not just rx_id - keeps multi-TX scenes
        # honest: each row is a single link's signal, never a blend of the
        # serving TX with interferers, and its path gain is referenced to that
        # link's own TX power (not an arbitrary txs[0]). A single-TX scene has
        # exactly one link per RX, so this stays identical to the old output.
        import math

        tx_power_by_id = {d.id: d.power_dbm for d in scene.devices if d.kind == "tx"}
        by_pair: dict[tuple[str, str], list] = {}
        for p in paths.paths:
            by_pair.setdefault((p.tx_id, p.rx_id), []).append(p)
        for (tx_id, ue_id), plist in by_pair.items():
            dev = scene.device_by_id(ue_id)
            pos = dev.position if dev else [0.0, 0.0, 0.0]
            lin = sum(10.0 ** (p.power_dbm / 10.0) for p in plist)
            rss = 10.0 * math.log10(lin) if lin > 0 else None
            # Path gain from the per-path gains when the backend supplied them
            # for every path in the link (linear-sum, same domain as rss);
            # otherwise fall back to rss minus THIS link's TX power.
            if plist and all(p.path_gain_db is not None for p in plist):
                gain_lin = sum(10.0 ** (p.path_gain_db / 10.0) for p in plist)
                gain = 10.0 * math.log10(gain_lin) if gain_lin > 0 else None
            elif rss is not None and tx_id in tx_power_by_id:
                gain = rss - tx_power_by_id[tx_id]
            else:
                gain = None
            sinr = (rss - noise_floor) if rss is not None else None
            w.writerow(
                [0.0, ue_id, *[round(c, 4) for c in pos], fmt(rss), fmt(sinr), fmt(gain)]
            )
    return buf.getvalue()


def _radio_map_csv(rm: Optional[RadioMapResultSet], config: SimulationConfig) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["x_m", "y_m", "z_m", "rss_dbm", "sinr_db", "path_gain_db"])
    if rm:
        ox, oy = rm.grid.origin[0], rm.grid.origin[1]
        z = rm.grid.height_m
        cs = rm.grid.cell_size_m
        # SINR is only meaningful for an absolute rss_dbm map (sinr = rss -
        # noise_floor); a path_gain_db map has no absolute power so it stays
        # blank. No interference model, so this is SNR.
        noise_floor = noise_floor_dbm(config)
        for j, row in enumerate(rm.values):
            for i, v in enumerate(row):
                if v is None:
                    continue
                x = ox + (i + 0.5) * cs
                y = oy + (j + 0.5) * cs
                cells = ["", "", ""]
                if rm.metric == "rss_dbm":
                    cells[0] = round(v, 3)
                    cells[1] = round(v - noise_floor, 3)  # sinr_db (SNR)
                elif rm.metric == "sinr_db":
                    cells[1] = round(v, 3)  # value is already dB; no noise math
                elif rm.metric == "path_gain_db":
                    cells[2] = round(v, 3)
                w.writerow([round(x, 3), round(y, 3), round(z, 3), *cells])
    return buf.getvalue()


def _calibration_points(scene: Scene) -> dict:
    """At least 3 reference points (the viewer guide's minimum for its
    coordinate check), padded with axis references when devices are scarce."""
    pts = [
        {
            "name": d.id,
            "sionna_m": list(d.position),
            "unreal_expected_cm": [c * 100.0 for c in d.position],
        }
        for d in scene.devices[:2]
    ]
    for name, p in (
        ("origin", [0.0, 0.0, 0.0]),
        ("x_axis_10m", [10.0, 0.0, 0.0]),
        ("y_axis_10m", [0.0, 10.0, 0.0]),
    ):
        if len(pts) >= 3 and name != "origin":
            break
        pts.append(
            {"name": name, "sionna_m": p, "unreal_expected_cm": [c * 100.0 for c in p]}
        )
    return {"points": pts}


def export_rfdata(
    project_dir: Path,
    scene: Scene,
    config: SimulationConfig,
    created_at: str,
    paths: Optional[PathResultSet] = None,
    radio_map: Optional[RadioMapResultSet] = None,
    trajectory: Optional[TrajectoryResultSet] = None,
) -> dict:
    out = project_dir / EXPORT_DIR_REL
    out.mkdir(parents=True, exist_ok=True)

    written: list[str] = []

    def write_json(name: str, obj: dict) -> None:
        (out / name).write_text(json.dumps(obj, indent=2), encoding="utf-8")
        written.append(f"{EXPORT_DIR_REL}/{name}")

    def write_text(name: str, text: str) -> None:
        (out / name).write_text(text, encoding="utf-8", newline="")
        written.append(f"{EXPORT_DIR_REL}/{name}")

    meta = _scenario_meta(scene, config, created_at)
    if trajectory and trajectory.samples:
        meta["time"]["end_s"] = round(trajectory.samples[-1].time_s, 4)
        if len(trajectory.samples) >= 2:
            # Actual sampling interval, so viewer playback timing stays in sync.
            meta["time"]["dt_s"] = round(
                trajectory.samples[1].time_s - trajectory.samples[0].time_s, 6
            )
    write_json("scenario_meta.json", meta)
    write_json("devices.json", _devices(scene, config))
    write_json("paths.json", _paths_json(paths))
    write_text("trajectory.csv", _trajectory_csv(trajectory, scene, paths, config))
    write_text("radio_map.csv", _radio_map_csv(radio_map, config))
    write_json("calibration_points.json", _calibration_points(scene))

    return {
        "export_dir": EXPORT_DIR_REL,
        "files": written,
        "has_paths": bool(paths and paths.paths),
        "has_radio_map": radio_map is not None,
        "has_trajectory": bool(trajectory and trajectory.samples),
    }
