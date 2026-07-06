"""Import NVIDIA AODT (Aerial Omniverse Digital Twin) result parquet files.

AODT exports its ClickHouse ground-truth tables (raypaths, radio maps, ...) as
Parquet. This service reads those files and normalizes them into our
backend-neutral result schemas (:class:`RayPath`, :class:`RadioMapResultSet`)
so an AODT-solved scene can be viewed and post-processed in SionnaTwin Studio.

Column naming: AODT and our exporter use overlapping-but-different names. We
accept BOTH - our own field names AND the AODT names documented in
``dataset.py``'s ``aodt_field_map`` (e.g. ``power_dB`` == our per-path power,
``cir_delay`` == our delay). Unknown columns are ignored.

pyarrow is imported LAZILY inside the reader and, when missing, we raise the
typed :class:`AodtImportUnavailable` so the API layer can answer 409 instead of
500. No hard dependency on pyarrow is added to the package.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from ..schemas.results import (
    PathResultSet,
    RadioMapGrid,
    RadioMapResultSet,
    RayPath,
)

BACKEND_NAME = "aodt_import"

# AODT/our column aliases, first present wins. Mirrors dataset.py aodt_field_map.
_PATH_ID_COLS = ("path_id", "ray_id", "id")
_TX_COLS = ("tx_id", "ru_id", "tx", "ru")
_RX_COLS = ("rx_id", "ue_id", "rx", "ue")
_POWER_COLS = ("power_dbm", "power_dB", "power_db", "rx_power_dbm")
_GAIN_COLS = ("path_gain_db", "gain_db", "channel_gain_db")
_DELAY_COLS = ("delay_ns", "cir_delay", "delay")  # cir_delay is seconds in AODT
_PHASE_COLS = ("phase_rad", "phase")
_PATHTYPE_COLS = ("path_type", "interaction_type", "type")
# Per-vertex polyline of the ray, when present (list<list<float>> column).
_POINTS_COLS = ("points", "vertices", "path_points", "interaction_points")
# Straight tx->rx fallback endpoints.
_TXPOS_COLS = ("tx_position", "ru_position", "tx_pos")
_RXPOS_COLS = ("rx_position", "ue_position", "rx_pos")

_VALID_PATH_TYPES = {
    "los", "reflection", "diffraction", "scattering", "transmission", "mixed",
}


class AodtImportUnavailable(RuntimeError):
    """pyarrow is not installed, so AODT parquet cannot be read."""


class AodtImportError(ValueError):
    """The AODT export is present but malformed (bad columns / no rows)."""


def _require_pyarrow():
    try:
        import pyarrow.parquet as pq  # noqa: F401

        return pq
    except ImportError as exc:  # pragma: no cover - exercised via 409 path
        raise AodtImportUnavailable(
            "pyarrow is required to import AODT parquet results; install "
            "pyarrow in the backend venv."
        ) from exc


def _first(row: dict, names: tuple[str, ...]):
    for n in names:
        if n in row and row[n] is not None:
            return row[n]
    return None


def _as_vec3(value) -> Optional[list[float]]:
    if value is None:
        return None
    try:
        seq = list(value)
    except TypeError:
        return None
    if len(seq) < 3:
        return None
    return [float(seq[0]), float(seq[1]), float(seq[2])]


def _read_table(path: Path) -> list[dict]:
    """Read a parquet file into a list of plain dict rows (order preserved)."""
    pq = _require_pyarrow()
    table = pq.read_table(path)
    return table.to_pylist()


def _normalize_path_type(raw) -> str:
    if raw is None:
        return "mixed"
    t = str(raw).strip().lower()
    return t if t in _VALID_PATH_TYPES else "mixed"


def _vertices_for_row(row: dict) -> Optional[list[list[float]]]:
    """A >=2 vertex polyline: the points column, else a straight tx->rx line."""
    pts = _first(row, _POINTS_COLS)
    if pts is not None:
        try:
            verts = [_as_vec3(p) for p in pts]
        except TypeError:
            verts = None
        if verts is not None:
            verts = [v for v in verts if v is not None]
            if len(verts) >= 2:
                return verts
    tx_pos = _as_vec3(_first(row, _TXPOS_COLS))
    rx_pos = _as_vec3(_first(row, _RXPOS_COLS))
    if tx_pos is not None and rx_pos is not None:
        return [tx_pos, rx_pos]
    return None


def _delay_ns(raw) -> float:
    """Delay in ns. AODT's ``cir_delay`` is seconds; ours is already ns.

    Heuristic: a raw value below 1e-3 is seconds (a 1 mm path is ~3.3e-12 s but
    ~3.3e-3 ns), so scale to ns; otherwise assume it is already ns.
    """
    v = float(raw)
    if v == 0.0:
        return 0.0
    if abs(v) < 1e-3:
        return v * 1e9
    return v


def import_paths(project_dir_source: Path, warnings: list[str]) -> PathResultSet:
    """paths.parquet -> PathResultSet."""
    parquet = project_dir_source / "paths.parquet"
    if not parquet.is_file():
        raise AodtImportError(f"paths.parquet not found in {project_dir_source}")
    rows = _read_table(parquet)
    if not rows:
        raise AodtImportError("paths.parquet has no rows")

    paths: list[RayPath] = []
    skipped = 0
    for i, row in enumerate(rows):
        vertices = _vertices_for_row(row)
        if vertices is None:
            skipped += 1
            continue
        power = _first(row, _POWER_COLS)
        delay = _first(row, _DELAY_COLS)
        phase = _first(row, _PHASE_COLS)
        gain = _first(row, _GAIN_COLS)
        paths.append(
            RayPath(
                path_id=str(_first(row, _PATH_ID_COLS) or f"aodt_{i}"),
                tx_id=str(_first(row, _TX_COLS) or "tx"),
                rx_id=str(_first(row, _RX_COLS) or "rx"),
                path_type=_normalize_path_type(_first(row, _PATHTYPE_COLS)),
                vertices=vertices,
                power_dbm=float(power) if power is not None else 0.0,
                path_gain_db=float(gain) if gain is not None else None,
                delay_ns=_delay_ns(delay) if delay is not None else 0.0,
                phase_rad=float(phase) if phase is not None else 0.0,
            )
        )
    if skipped:
        warnings.append(
            f"{skipped}/{len(rows)} path rows lacked usable geometry "
            "(no points column and no tx/rx positions); skipped"
        )
    if not paths:
        raise AodtImportError(
            "no usable path rows in paths.parquet (need a points column or "
            "tx/rx position columns)"
        )
    return PathResultSet(
        result_id="unsaved",
        backend=BACKEND_NAME,
        simulation_config_id="aodt_import",
        paths=paths,
        warnings=list(warnings),
        metadata={"source": str(project_dir_source), "row_count": len(rows)},
    )


_RM_X_COLS = ("x", "cell_x", "pos_x", "grid_x")
_RM_Y_COLS = ("y", "cell_y", "pos_y", "grid_y")
_RM_VALUE_COLS = ("value", "path_gain_db", "rss_dbm", "gain_db", "power_dB")
_RM_METRIC_BY_COL = {
    "path_gain_db": "path_gain_db",
    "gain_db": "path_gain_db",
    "rss_dbm": "rss_dbm",
    "power_dB": "rss_dbm",
}


def _min_positive_delta(sorted_unique: list[float]) -> Optional[float]:
    best: Optional[float] = None
    for a, b in zip(sorted_unique, sorted_unique[1:]):
        d = b - a
        if d > 1e-9 and (best is None or d < best):
            best = d
    return best


def import_radio_map(project_dir_source: Path, warnings: list[str]) -> RadioMapResultSet:
    """radio_map.parquet -> RadioMapResultSet.

    Two accepted layouts:
    - per-cell rows carrying (x, y, value): the grid is rebuilt from the
      distinct x/y coordinates, inferring cell size from the smallest positive
      coordinate delta;
    - a pre-gridded single-row/metadata layout is not assumed - we only support
      the per-cell form, which is what AODT's radio-map export emits.
    """
    parquet = project_dir_source / "radio_map.parquet"
    if not parquet.is_file():
        raise AodtImportError(f"radio_map.parquet not found in {project_dir_source}")
    rows = _read_table(parquet)
    if not rows:
        raise AodtImportError("radio_map.parquet has no rows")

    sample = rows[0]
    x_col = next((c for c in _RM_X_COLS if c in sample), None)
    y_col = next((c for c in _RM_Y_COLS if c in sample), None)
    val_col = next((c for c in _RM_VALUE_COLS if c in sample), None)
    if x_col is None or y_col is None or val_col is None:
        raise AodtImportError(
            "radio_map.parquet needs x, y and a value column "
            f"(got columns {sorted(sample.keys())})"
        )
    metric = _RM_METRIC_BY_COL.get(val_col, "path_gain_db")

    xs = sorted({float(r[x_col]) for r in rows if r.get(x_col) is not None})
    ys = sorted({float(r[y_col]) for r in rows if r.get(y_col) is not None})
    if len(xs) < 1 or len(ys) < 1:
        raise AodtImportError("radio_map.parquet has no valid x/y coordinates")

    dx = _min_positive_delta(xs)
    dy = _min_positive_delta(ys)
    cell = next((d for d in (dx, dy) if d is not None), None)
    if cell is None:
        cell = 1.0
        warnings.append(
            "radio_map cell size could not be inferred (single row/column); "
            "defaulted to 1.0 m"
        )

    # Reconstruct a regular grid spanning [min, max] at the inferred spacing.
    nx = max(1, int(round((xs[-1] - xs[0]) / cell)) + 1)
    ny = max(1, int(round((ys[-1] - ys[0]) / cell)) + 1)
    origin = [xs[0], ys[0], 0.0]
    height = 0.0
    for r in rows:
        z = r.get("z") if "z" in r else r.get("height_m")
        if z is not None:
            height = float(z)
            break

    values: list[list[Optional[float]]] = [[None] * nx for _ in range(ny)]
    tx_ids: list[str] = []
    for r in rows:
        if r.get(x_col) is None or r.get(y_col) is None:
            continue
        ix = int(round((float(r[x_col]) - xs[0]) / cell))
        iy = int(round((float(r[y_col]) - ys[0]) / cell))
        if 0 <= ix < nx and 0 <= iy < ny:
            v = r.get(val_col)
            values[iy][ix] = float(v) if v is not None else None
        tx_raw = _first(r, _TX_COLS)
        if tx_raw is not None and str(tx_raw) not in tx_ids:
            tx_ids.append(str(tx_raw))

    tx_id = tx_ids[0] if tx_ids else "tx"
    return RadioMapResultSet(
        result_id="unsaved",
        backend=BACKEND_NAME,
        simulation_config_id="aodt_import",
        tx_id=tx_id,
        metric=metric,
        grid=RadioMapGrid(origin=origin, cell_size_m=float(cell), nx=nx, ny=ny,
                          height_m=height),
        values=values,
        tx_ids=tx_ids,
        warnings=list(warnings),
        metadata={"source": str(project_dir_source), "row_count": len(rows)},
    )


def import_aodt_results(project_dir_source: Path, kind: str):
    """Import one AODT result of ``kind`` ("paths" | "radio_map").

    Returns the normalized result set (backend="aodt_import"). Raises
    :class:`AodtImportUnavailable` when pyarrow is missing and
    :class:`AodtImportError` on bad input.
    """
    project_dir_source = Path(project_dir_source)
    if not project_dir_source.is_dir():
        raise AodtImportError(f"not a directory: {project_dir_source}")
    warnings: list[str] = []
    if kind == "paths":
        return import_paths(project_dir_source, warnings)
    if kind == "radio_map":
        return import_radio_map(project_dir_source, warnings)
    raise AodtImportError(f"unsupported AODT import kind: {kind!r}")
