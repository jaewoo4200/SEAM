"""Sensing dataset export (POST /projects/{id}/export/sensing-dataset).

Turns the per-frame sensing of stored scenario results into flat, ML-ready
tables: one ``links`` row per (result, frame, TX -> RX link, target) with the
link's features (node kinematics, range, Doppler, SNR, angles, Pd), its
detection labels, the target truth, the fusion estimate and the EKF track;
optionally one ``echoes`` row per echo path. Both go into one zip under
``export/sensing_dataset/`` with a manifest (column units and roles, the
run constants, split, label balance) and a README, in npz, csv and/or
parquet. Pure post-processing: no solve.

Every column is always present (a stable schema): a missing value is NaN in
float columns and "" in string columns.
"""

import csv
import io
import json
import math
import time
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import numpy as np

from seam_studio.core.config import APP_VERSION
from seam_studio.schemas.actors import ScenarioFrame, ScenarioResultSet
from seam_studio.schemas.results import RayPath, SensingLinkReport, TargetEstimate
from seam_studio.schemas.scene import ResultSetRef, Scene
from seam_studio.schemas.sensing import (
    MAX_SENSING_DATASET_ROWS,
    SensingDatasetExportRequest,
)
from seam_studio.services.scenario import _velocities_at

EXPORT_DIR = "export/sensing_dataset"
SPEED_OF_LIGHT = 299_792_458.0
SCHEMA = "seam.sensing_dataset"
SCHEMA_VERSION = 1


class SensingDatasetError(ValueError):
    """The export cannot run as requested (API -> 400)."""


class SensingDatasetNotFound(LookupError):
    """A requested result does not exist or cannot be read (API -> 404)."""


@dataclass(frozen=True)
class Column:
    name: str
    dtype: str  # "float64" | "int64" | "bool" | "str"
    unit: str
    role: str  # "meta" | "feature" | "label" | "estimate" | "track"
    description: str


@dataclass(frozen=True)
class DatasetSource:
    result: ScenarioResultSet
    label: Optional[str] = None


def _xyz(prefix: str, dtype: str, unit: str, role: str, what: str) -> list[Column]:
    return [Column(f"{prefix}_{a}", dtype, unit, role, f"{what}, {a}") for a in "xyz"]


LINK_COLUMNS: list[Column] = [
    Column("result_id", "str", "", "meta", "Scenario result the row comes from"),
    Column("split", "str", "", "meta", "train / val / test (frame-level), or all"),
    Column("frame_index", "int64", "", "meta", "Index of the frame in the result's frames"),
    Column("time_s", "float64", "s", "meta", "Frame time"),
    Column("tx_id", "str", "", "meta", "Transmitter id"),
    Column("rx_id", "str", "", "meta", "Receiver id"),
    Column("target_id", "str", "", "meta", "Target actor id"),
    *_xyz("tx_pos", "float64", "m", "feature", "TX position (world, Z-up)"),
    *_xyz("rx_pos", "float64", "m", "feature", "RX position (world, Z-up)"),
    *_xyz("tx_vel", "float64", "m/s", "feature", "TX velocity"),
    *_xyz("rx_vel", "float64", "m/s", "feature", "RX velocity"),
    Column("bistatic_range_m", "float64", "m", "feature",
           "c * delay of the reported echo (|TX - p| + |p - RX| for a direct echo)"),
    Column("doppler_hz", "float64", "Hz", "feature",
           "Doppler of the reported echo (positive = closing)"),
    Column("echo_power_dbm", "float64", "dBm", "feature", "Received power of the reported echo"),
    Column("snr_db", "float64", "dB", "feature",
           "Post-integration SNR: echo power - noise floor + 10 log10(cpi_pulses)"),
    Column("measured_range_m", "float64", "m", "feature",
           "Range sum the fusion used (bistatic_range_m + noise with measurement_noise)"),
    Column("measured_doppler_hz", "float64", "Hz", "feature",
           "Doppler the fusion used (doppler_hz + noise with measurement_noise)"),
    Column("range_bin", "float64", "cell", "feature",
           "floor(measured_range_m / (c / B)); NaN without an echo"),
    Column("doppler_bin", "float64", "cell", "feature",
           "round(measured_doppler_hz * cpi_s); NaN without an echo or Doppler"),
    Column("num_echoes", "int64", "", "feature", "Echoes of this target on this link"),
    Column("multipath", "bool", "", "feature",
           "The reported echo is not direct (no direct echo on the link)"),
    Column("aod_az_deg", "float64", "deg", "feature", "Departure azimuth of the reported echo"),
    Column("aod_el_deg", "float64", "deg", "feature", "Departure elevation of the reported echo"),
    Column("aoa_az_deg", "float64", "deg", "feature", "Arrival azimuth of the reported echo"),
    Column("aoa_el_deg", "float64", "deg", "feature", "Arrival elevation of the reported echo"),
    Column("pd", "float64", "", "feature",
           "Analytic Pd of snr_db at the run's pfa (NaN unless the run set sensing.pfa)"),
    Column("pd_mc", "float64", "", "feature",
           "Monte Carlo Pd (NaN unless sensing.detector.monte_carlo_trials > 0)"),
    Column("detected", "bool", "", "label",
           "snr_db >= threshold_db and the echo passed the MTI notch"),
    Column("reason", "str", "", "label",
           "detected / no_echo / below_threshold / mti_rejected"),
    *_xyz("position_true", "float64", "m", "label", "Target cuboid center (truth)"),
    *_xyz("velocity_true", "float64", "m/s", "label", "Target velocity (truth)"),
    Column("est_status", "str", "", "estimate",
           "Fusion status of the target in this frame: ok / insufficient_links / diverged"),
    Column("n_links_detected", "int64", "", "estimate", "Detected links of the target"),
    Column("n_links_used", "int64", "", "estimate",
           "Geometrically distinct direct links the fusion used"),
    *_xyz("position_est", "float64", "m", "estimate", "Fused position"),
    *_xyz("velocity_est", "float64", "m/s", "estimate", "Fused velocity (Doppler least squares)"),
    Column("position_error_m", "float64", "m", "estimate", "|position_est - position_true|"),
    Column("velocity_error_m_s", "float64", "m/s", "estimate", "|velocity_est - velocity_true|"),
    Column("gdop", "float64", "", "estimate", "sqrt(trace((J^T J)^-1)) of the fusion"),
    Column("track_status", "str", "", "track",
           "EKF track: none / init / tracking / coasting / lost; \"\" when tracking was off"),
    *_xyz("track_position", "float64", "m", "track", "EKF posterior position"),
    *_xyz("track_velocity", "float64", "m/s", "track", "EKF posterior velocity"),
    Column("track_position_error_m", "float64", "m", "track", "|track_position - position_true|"),
    Column("track_velocity_error_m_s", "float64", "m/s", "track",
           "|track_velocity - velocity_true|"),
    Column("track_position_std_m", "float64", "m", "track", "sqrt(trace(P_pos)) of the EKF"),
    Column("track_updates", "float64", "", "track", "Scalar measurements accepted this frame"),
    Column("track_gated", "float64", "", "track", "Scalar measurements gated out this frame"),
]

ECHO_COLUMNS: list[Column] = [
    Column("result_id", "str", "", "meta", "Scenario result the row comes from"),
    Column("split", "str", "", "meta", "Split of the echo's frame"),
    Column("frame_index", "int64", "", "meta", "Index of the frame in the result's frames"),
    Column("time_s", "float64", "s", "meta", "Frame time"),
    Column("path_id", "str", "", "meta", "Path id within the frame"),
    Column("tx_id", "str", "", "meta", "Transmitter id"),
    Column("rx_id", "str", "", "meta", "Receiver id"),
    Column("target_id", "str", "", "label",
           "Target the echo scatters off (\"\" = a comm path, include_comm_paths)"),
    Column("path_type", "str", "", "label", "Path type (sensing for echoes)"),
    Column("delay_ns", "float64", "ns", "feature", "Propagation delay"),
    Column("bistatic_range_m", "float64", "m", "feature", "c * delay"),
    Column("power_dbm", "float64", "dBm", "feature", "Received power"),
    Column("path_gain_db", "float64", "dB", "feature", "Path gain (power - TX power)"),
    Column("phase_rad", "float64", "rad", "feature", "Total passband phase at the carrier"),
    Column("doppler_hz", "float64", "Hz", "feature", "Doppler (positive = closing)"),
    Column("aod_az_deg", "float64", "deg", "feature", "Departure azimuth"),
    Column("aod_el_deg", "float64", "deg", "feature", "Departure elevation"),
    Column("aoa_az_deg", "float64", "deg", "feature", "Arrival azimuth"),
    Column("aoa_el_deg", "float64", "deg", "feature", "Arrival elevation"),
    Column("num_interactions", "int64", "", "feature", "Interactions along the path"),
    Column("direct", "bool", "", "label",
           "TX -> scattering point -> RX with no other interaction"),
    Column("reported", "bool", "", "meta", "The echo its link report was built from"),
]

CONVENTIONS = {
    "frame": "world coordinates, Z-up, metres",
    "angles": "degrees",
    "azimuth": "atan2(y, x) about +Z",
    "elevation": "up from the XY plane",
    "arrival": "aoa points from the RX toward the incoming ray (where it came from)",
    "doppler": "positive = closing (Sionna convention)",
    "snr_db": "post-integration: echo power - noise floor + 10 log10(cpi_pulses)",
    "missing": "NaN in float columns and \"\" in string columns mean None",
    "association": (
        "oracle: the ray tracer labels each echo with its target and as direct or "
        "multipath; a real receiver would have to infer both"
    ),
}

README = """SEAM sensing dataset
====================

Exported from the per-frame sensing of stored SEAM scenario results
(Scenario playback with Sensing (ISAC) on). manifest.json lists every column
with its dtype, unit and role, the run constants of each result, the split
and the label balance.

Files
-----
links.<fmt>   one row per (result, frame, TX -> RX link, target): node
              kinematics, the reported echo (range, Doppler, SNR, angles,
              Pd), detection labels, target truth, fusion estimate, EKF track
echoes.<fmt>  one row per echo path of each frame (include_echo_paths)

Loading
-------
    import numpy as np
    links = np.load("links.npz", allow_pickle=False)   # one 1-D array per column
    X = np.stack([links["snr_db"], links["bistatic_range_m"]], axis=1)
    y = links["detected"]

    import pandas as pd
    df = pd.read_csv("links.csv")          # empty field = missing (NaN)
    df = pd.read_parquet("links.parquet")  # needs pyarrow

Column roles
------------
meta      ids, split, frame index and time
feature   what a receiver could measure (plus node kinematics)
label     detection outcome and the target truth
estimate  the multistatic fusion of that frame (one value per target,
          repeated on each of its link rows)
track     the EKF track (NaN / "" when the run had tracking off)

Caveats
-------
- Association is an oracle: the ray tracer labels each echo with its target
  and as direct or multipath.
- velocity_true is the trajectory velocity; at a trajectory end (mode
  "once") it can be non-zero while the actor already stands still.
- Split is by frame: every row of one (result, frame) lands in one split, so
  links of a frame never leak across splits. Consecutive frames are still
  correlated; for strict evaluation hold out whole results.
- Results solved before v0.1.13 did not store per-frame TX/RX kinematics:
  those are rebuilt from the current scene (see the export warnings and
  "kinematics_source" in manifest.json).
"""


# ------------------------------------------------------------------ sources


def _has_sensing(result: ScenarioResultSet) -> bool:
    return any(f.sensing is not None for f in result.frames)


def _load(store, project_id: str, ref: ResultSetRef) -> ScenarioResultSet:
    try:
        return ScenarioResultSet.model_validate(store.load_json(project_id, ref.uri))
    except (OSError, ValueError) as exc:  # pydantic's ValidationError is a ValueError
        raise SensingDatasetNotFound(
            f"result file missing or unreadable: {ref.uri}"
        ) from exc


def load_sources(
    store,
    project_id: str,
    scene: Scene,
    result_ids: Optional[list[str]],
    *,
    skip_without_sensing: bool = False,
) -> tuple[list[DatasetSource], list[str]]:
    """(sources, warnings): the scenario results to export, ``result_ids``
    in order (404 unknown or unreadable; 400 without sensing frames, or
    skipped with a warning under ``skip_without_sensing``), else every stored
    scenario result that has sensing frames, in ref order (unreadable ones
    skipped with a warning). 400 when nothing is left."""
    refs = [r for r in scene.result_sets if r.kind == "scenario"]
    out: list[DatasetSource] = []
    warnings: list[str] = []
    if result_ids is not None:
        by_id = {r.result_id: r for r in refs}
        for rid in result_ids:
            ref = by_id.get(rid)
            if ref is None:
                raise SensingDatasetNotFound(f"unknown scenario result: {rid}")
            result = _load(store, project_id, ref)
            if _has_sensing(result):
                out.append(DatasetSource(result, ref.label))
            elif skip_without_sensing:
                warnings.append(f"scenario result {rid} has no sensing frames; skipped")
            else:
                raise SensingDatasetError(
                    f"scenario result {rid} has no sensing frames; run the scenario "
                    "with sensing enabled"
                )
        if not out:
            raise SensingDatasetError(
                "none of the listed scenario results has sensing frames; run a "
                "scenario with sensing enabled"
            )
        return out, warnings
    for ref in refs:
        try:
            result = _load(store, project_id, ref)
        except SensingDatasetNotFound as exc:
            warnings.append(f"{exc}; skipped")
            continue
        if _has_sensing(result):
            out.append(DatasetSource(result, ref.label))
    if not out:
        raise SensingDatasetError(
            "no stored scenario result has sensing frames; run a scenario with "
            "sensing enabled"
        )
    return out, warnings


def _require_pyarrow() -> None:
    try:
        import pyarrow  # noqa: F401
        import pyarrow.parquet  # noqa: F401
    except ImportError as exc:
        raise SensingDatasetError(
            "parquet needs pyarrow (pip install 'seam-studio[results]'); "
            "export npz or csv instead"
        ) from exc


# ------------------------------------------------------------------ rows


class _Table:
    def __init__(self, columns: list[Column]):
        self.columns = columns
        self.data: dict[str, list] = {c.name: [] for c in columns}

    def add(self, row: dict[str, Any]) -> None:
        for c in self.columns:
            self.data[c.name].append(_cell(c.dtype, row.get(c.name)))

    def __len__(self) -> int:
        return len(self.data[self.columns[0].name])

    def arrays(self) -> dict[str, np.ndarray]:
        out: dict[str, np.ndarray] = {}
        for c in self.columns:
            values = self.data[c.name]
            if c.dtype == "str":
                out[c.name] = np.array(values, dtype=np.str_)
            elif c.dtype == "bool":
                out[c.name] = np.array(values, dtype=np.bool_)
            elif c.dtype == "int64":
                out[c.name] = np.array(values, dtype=np.int64)
            else:
                out[c.name] = np.array(values, dtype=np.float64)
        return out


def _cell(dtype: str, value: Any) -> Any:
    if dtype == "float64":
        return math.nan if value is None else float(value)
    if dtype == "int64":
        return 0 if value is None else int(value)
    if dtype == "bool":
        return bool(value)
    return "" if value is None else str(value)


def _vec(prefix: str, v: Optional[list[float]]) -> dict[str, Optional[float]]:
    if v is None:
        return {f"{prefix}_{a}": None for a in "xyz"}
    return {f"{prefix}_{a}": float(v[i]) for i, a in enumerate("xyz")}


def _angles(prefix: str, v: Optional[list[float]]) -> dict[str, Optional[float]]:
    if v is None or len(v) < 2:
        return {f"{prefix}_az_deg": None, f"{prefix}_el_deg": None}
    return {f"{prefix}_az_deg": float(v[0]), f"{prefix}_el_deg": float(v[1])}


Kinematics = dict[str, tuple[list[float], list[float]]]


def _frame_kinematics(frame: ScenarioFrame, scene: Scene) -> tuple[Kinematics, bool]:
    """(device id -> (position, velocity), stored) of one frame: the frame's
    own nodes (v0.1.13+), else rebuilt from the current scene the way the
    tracker built them (attached-device positions from device_states,
    velocities from the trajectories)."""
    sensing = frame.sensing
    if sensing is not None and sensing.nodes is not None:
        return {n.id: (list(n.position), list(n.velocity)) for n in sensing.nodes}, True
    positions = {d.id: list(d.position) for d in frame.device_states}
    _, device_velocities = _velocities_at(scene, frame.time_s)
    out: Kinematics = {}
    for dev in scene.devices:
        pos = positions.get(dev.id, list(dev.position))
        vel = device_velocities.get(dev.id, dev.velocity_m_s or [0.0, 0.0, 0.0])
        out[dev.id] = ([float(c) for c in pos], [float(c) for c in vel])
    return out, False


def _estimate_row(est: Optional[TargetEstimate]) -> dict[str, Any]:
    if est is None:
        return {}
    row: dict[str, Any] = {
        "est_status": est.status,
        "n_links_detected": est.n_links_detected,
        "n_links_used": est.n_links_used,
        "position_error_m": est.position_error_m,
        "velocity_error_m_s": est.velocity_error_m_s,
        "gdop": est.gdop,
        "track_status": est.track_status,
        "track_position_error_m": est.track_position_error_m,
        "track_velocity_error_m_s": est.track_velocity_error_m_s,
        "track_position_std_m": est.track_position_std_m,
        "track_updates": est.track_updates,
        "track_gated": est.track_gated,
    }
    row.update(_vec("position_true", est.position_true))
    row.update(_vec("velocity_true", est.velocity_true))
    row.update(_vec("position_est", est.position_est))
    row.update(_vec("velocity_est", est.velocity_est))
    row.update(_vec("track_position", est.track_position))
    row.update(_vec("track_velocity", est.track_velocity))
    return row


def _link_row(
    r: SensingLinkReport, kin: Kinematics, echo: Optional[RayPath]
) -> dict[str, Any]:
    tx = kin.get(r.tx_id)
    rx = kin.get(r.rx_id)
    row: dict[str, Any] = {
        "tx_id": r.tx_id,
        "rx_id": r.rx_id,
        "target_id": r.target_id,
        "bistatic_range_m": r.bistatic_range_m,
        "doppler_hz": r.doppler_hz,
        "echo_power_dbm": r.echo_power_dbm,
        "snr_db": r.snr_db,
        "measured_range_m": r.measured_range_m,
        "measured_doppler_hz": r.measured_doppler_hz,
        "range_bin": r.range_bin,
        "doppler_bin": r.doppler_bin,
        "num_echoes": r.num_echoes,
        "multipath": r.multipath,
        "pd": r.pd,
        "pd_mc": r.pd_mc,
        "detected": r.detected,
        "reason": r.reason,
    }
    row.update(_vec("tx_pos", tx[0] if tx else None))
    row.update(_vec("tx_vel", tx[1] if tx else None))
    row.update(_vec("rx_pos", rx[0] if rx else None))
    row.update(_vec("rx_vel", rx[1] if rx else None))
    row.update(_angles("aod", echo.aod_deg if echo else None))
    row.update(_angles("aoa", echo.aoa_deg if echo else None))
    return row


def _echo_row(p: RayPath, reported: bool) -> dict[str, Any]:
    row: dict[str, Any] = {
        "path_id": p.path_id,
        "tx_id": p.tx_id,
        "rx_id": p.rx_id,
        "target_id": p.target_id,
        "path_type": p.path_type,
        "delay_ns": p.delay_ns,
        "bistatic_range_m": SPEED_OF_LIGHT * p.delay_ns * 1e-9,
        "power_dbm": p.power_dbm,
        "path_gain_db": p.path_gain_db,
        "phase_rad": p.phase_rad,
        "doppler_hz": p.doppler_hz,
        "num_interactions": len(p.interactions),
        "direct": [i.type for i in p.interactions] == ["sensing"],
        "reported": reported,
    }
    row.update(_angles("aod", p.aod_deg))
    row.update(_angles("aoa", p.aoa_deg))
    return row


def split_counts(num_frames: int, split) -> list[int]:
    """[train, val, test] frame counts: the fractions x F by largest
    remainder (ties to the smaller fraction, then train, val, test), so they
    sum to F and a fraction of 0 never gets a frame."""
    fractions = (split.train, split.val, split.test)
    exact = [f * num_frames for f in fractions]
    counts = [math.floor(e) for e in exact]
    order = sorted(
        (i for i in range(3) if fractions[i] > 0.0),
        key=lambda i: (-(exact[i] - counts[i]), fractions[i], i),
    )
    for k in range(max(0, num_frames - sum(counts))):
        counts[order[k % len(order)]] += 1
    return counts


def assign_splits(num_frames: int, split) -> list[str]:
    """Split label per frame unit (export order). None: "all". Else the units
    are permuted with default_rng(seed); the first split_counts train are
    train, the next val, the rest test."""
    if split is None:
        return ["all"] * num_frames
    perm = np.random.default_rng(split.seed).permutation(num_frames)
    n_train, n_val, _ = split_counts(num_frames, split)
    labels = [""] * num_frames
    for k, unit in enumerate(perm):
        labels[int(unit)] = (
            "train" if k < n_train else ("val" if k < n_train + n_val else "test")
        )
    return labels


# ------------------------------------------------------------------ writers


def _npz_bytes(arrays: dict[str, np.ndarray]) -> bytes:
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)  # type: ignore[arg-type]
    return buf.getvalue()


def _write_csv(fh, table: _Table) -> None:
    """Row by row into ``fh`` (the zip entry), never the whole text in memory."""
    writer = csv.writer(fh, lineterminator="\n")
    writer.writerow([c.name for c in table.columns])
    fmt = {
        "float64": lambda v: "" if math.isnan(v) else repr(float(v)),
        "int64": lambda v: str(int(v)),
        "bool": lambda v: "true" if v else "false",
        "str": lambda v: v,
    }
    cols = [(table.data[c.name], fmt[c.dtype]) for c in table.columns]
    for i in range(len(table)):
        writer.writerow([f(values[i]) for values, f in cols])


def _parquet_bytes(table: _Table, arrays: dict[str, np.ndarray]) -> bytes:
    import pyarrow as pa
    import pyarrow.parquet as pq

    types = {"float64": pa.float64(), "int64": pa.int64(), "bool": pa.bool_(), "str": pa.string()}
    pa_table = pa.table(
        {
            c.name: pa.array(
                arrays[c.name].tolist() if c.dtype == "str" else arrays[c.name],
                type=types[c.dtype],
            )
            for c in table.columns
        }
    )
    sink = pa.BufferOutputStream()
    pq.write_table(pa_table, sink)
    return sink.getvalue().to_pybytes()


def _open_zip(out_dir: Path, stamp: str) -> tuple[zipfile.ZipFile, Path]:
    """A new zip named sensing_dataset_<stamp>[_n].zip (exclusive create)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    n = 1
    while True:
        name = f"sensing_dataset_{stamp}" + (f"_{n}" if n > 1 else "") + ".zip"
        path = out_dir / name
        try:
            return zipfile.ZipFile(path, "x", compression=zipfile.ZIP_DEFLATED), path
        except FileExistsError:
            n += 1


def _column_specs(columns: list[Column]) -> list[dict]:
    return [
        {"name": c.name, "dtype": c.dtype, "unit": c.unit, "role": c.role,
         "description": c.description}
        for c in columns
    ]


# ------------------------------------------------------------------ export


def count_rows(sources: list[DatasetSource], include_echo_paths: bool) -> tuple[int, int]:
    """(link rows, echo rows) the export would write."""
    links = echoes = 0
    for src in sources:
        for frame in src.result.frames:
            if frame.sensing is None:
                continue
            links += len(frame.sensing.links)
            if include_echo_paths:
                echoes += len(frame.sensing.echoes)
    return links, echoes


def export_sensing_dataset(
    project_dir: Path,
    project_id: str,
    scene: Scene,
    sources: list[DatasetSource],
    request: SensingDatasetExportRequest,
    *,
    current_scene_hash: Optional[str] = None,
    now: Optional[datetime] = None,
) -> dict:
    """Write the dataset zip; returns SensingDatasetExportResult fields.
    Validation (pyarrow for parquet, the row cap) happens before anything is
    written."""
    started = time.perf_counter()
    if "parquet" in request.formats:
        _require_pyarrow()
    n_links, n_echoes = count_rows(sources, request.include_echo_paths)
    if n_links + n_echoes > MAX_SENSING_DATASET_ROWS:
        raise SensingDatasetError(
            f"{n_links} link rows + {n_echoes} echo rows = {n_links + n_echoes} rows; "
            f"at most {MAX_SENSING_DATASET_ROWS} per export (export fewer results)"
        )

    units = [
        (k, i)
        for k, src in enumerate(sources)
        for i, frame in enumerate(src.result.frames)
        if frame.sensing is not None
    ]
    split_of = dict(zip(units, assign_splits(len(units), request.split)))

    links = _Table(LINK_COLUMNS)
    echoes = _Table(ECHO_COLUMNS)
    warnings: list[str] = []
    rows_per_result: dict[str, int] = {}
    result_entries: list[dict] = []
    for k, src in enumerate(sources):
        result = src.result
        rid = result.result_id
        rows_before = len(links)
        stored_kinematics = True
        missing: set[str] = set()
        for i, frame in enumerate(result.frames):
            sensing = frame.sensing
            if sensing is None:
                continue
            kin, stored = _frame_kinematics(frame, scene)
            stored_kinematics &= stored
            echo_by_id: dict[str, RayPath] = {}
            for p in sensing.echoes:
                echo_by_id.setdefault(p.path_id, p)
            est_by_target = {e.target_id: e for e in sensing.estimates}
            meta = {
                "result_id": rid,
                "split": split_of[(k, i)],
                "frame_index": i,
                "time_s": frame.time_s,
            }
            for r in sensing.links:
                missing.update(d for d in (r.tx_id, r.rx_id) if d not in kin)
                echo = echo_by_id.get(r.path_id) if r.path_id is not None else None
                links.add({
                    **meta,
                    **_estimate_row(est_by_target.get(r.target_id)),
                    **_link_row(r, kin, echo),
                })
            if request.include_echo_paths:
                reported = {
                    (r.tx_id, r.rx_id, r.target_id, r.path_id)
                    for r in sensing.links if r.path_id is not None
                }
                for p in sensing.echoes:
                    echoes.add({
                        **meta,
                        **_echo_row(p, (p.tx_id, p.rx_id, p.target_id, p.path_id) in reported),
                    })
        rows_per_result[rid] = len(links) - rows_before
        scene_hash = result.metadata.get("scene_hash")
        if not stored_kinematics:
            msg = (
                f"result {rid} predates v0.1.13: tx/rx kinematics reconstructed from "
                "the current scene"
            )
            if scene_hash is not None and current_scene_hash is not None and (
                scene_hash != current_scene_hash
            ):
                msg += " (scene changed since the run)"
            warnings.append(msg)
        if missing:
            warnings.append(
                f"result {rid}: device(s) {', '.join(sorted(missing))} not in the current "
                "scene; their kinematics are NaN"
            )
        sensing_meta = result.metadata.get("sensing") or {}
        result_entries.append({
            "result_id": rid,
            "label": src.label,
            "backend": result.backend,
            "created_at": result.created_at,
            "frequency_hz": result.metadata.get("frequency_hz", sensing_meta.get("frequency_hz")),
            "num_frames": len(result.frames),
            "dt_s": result.metadata.get("dt_s"),
            "num_rows": rows_per_result[rid],
            "scene_hash": scene_hash,
            "current_scene_hash": current_scene_hash,
            "kinematics_source": "frames" if stored_kinematics else "current_scene",
            "sensing": {key: v for key, v in sensing_meta.items() if key != "targets"},
        })

    split_names = ["all"] if request.split is None else ["train", "val", "test"]
    split_col = links.data["split"]
    rows_per_split = {s: sum(1 for v in split_col if v == s) for s in split_names}
    detected = links.data["detected"]
    detected_fraction = (sum(detected) / len(detected)) if detected else None
    reason_counts: dict[str, int] = {}
    for reason in links.data["reason"]:
        reason_counts[reason] = reason_counts.get(reason, 0) + 1

    tables = [("links", links)]
    if request.include_echo_paths:
        tables.append(("echoes", echoes))
    files = ["manifest.json", "README.txt"] + [
        f"{name}.{fmt}" for name, _ in tables for fmt in request.formats
    ]
    created = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    split = request.split
    manifest = {
        "schema": SCHEMA,
        "schema_version": SCHEMA_VERSION,
        "created_at": created.isoformat(),
        "seam_version": APP_VERSION,
        "project_id": project_id,
        "formats": list(request.formats),
        "tables": {
            name: {"rows": len(table), "files": [f"{name}.{fmt}" for fmt in request.formats]}
            for name, table in tables
        },
        "split": None if split is None else {
            "train": split.train, "val": split.val, "test": split.test,
            "seed": split.seed, "unit": "frame",
        },
        "rows_per_split": rows_per_split,
        "label_balance": {"detected_fraction": detected_fraction, "reason_counts": reason_counts},
        "results": result_entries,
        "columns": {name: _column_specs(table.columns) for name, table in tables},
        "conventions": CONVENTIONS,
    }

    out_dir = project_dir / EXPORT_DIR
    zf, zip_path = _open_zip(out_dir, created.strftime("%Y%m%dT%H%M%SZ"))
    try:
        with zf:
            zf.writestr("manifest.json", json.dumps(manifest, indent=2))
            zf.writestr("README.txt", README)
            for name, table in tables:
                # Column arrays once per table, shared by npz and parquet.
                arrays = (
                    table.arrays() if {"npz", "parquet"} & set(request.formats) else {}
                )
                for fmt in request.formats:
                    entry = f"{name}.{fmt}"
                    if fmt == "npz":
                        zf.writestr(entry, _npz_bytes(arrays), zipfile.ZIP_STORED)
                    elif fmt == "csv":
                        with io.TextIOWrapper(
                            zf.open(entry, "w", force_zip64=True), encoding="utf-8", newline=""
                        ) as fh:
                            _write_csv(fh, table)
                    else:
                        zf.writestr(entry, _parquet_bytes(table, arrays), zipfile.ZIP_STORED)
                del arrays
    except BaseException:
        zip_path.unlink(missing_ok=True)
        raise

    return {
        "export_dir": EXPORT_DIR,
        "zip_name": zip_path.name,
        "download_url": f"/api/projects/{project_id}/assets/{EXPORT_DIR}/{zip_path.name}",
        "files": files,
        "result_ids": [src.result.result_id for src in sources],
        "num_rows": len(links),
        "rows_per_result": rows_per_result,
        "rows_per_split": rows_per_split,
        "num_echo_rows": len(echoes),
        "detected_fraction": detected_fraction,
        "size_bytes": zip_path.stat().st_size,
        "elapsed_s": time.perf_counter() - started,
        "warnings": warnings,
    }
