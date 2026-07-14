"""Measurement-based calibration endpoint (HANDOFF Milestone 11).

POST /projects/{project_id}/calibrate/materials
Import measured per-link path gain, fit one RF material parameter by grid
search, and return a before/after report. With apply=true, the fitted value is
written into the project material library and prims using that material are
promoted to assignment_status "measurement_calibrated".

POST /projects/{project_id}/calibrate/validate-trajectory
Measured-vs-predicted path gain along the (time-ordered) measurement log:
the log's RX positions are replayed through the trajectory solver and scored
per point with the calibration module's level-offset alignment.
"""

import csv
import io
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import Field

from seam_studio.api.deps import get_store, load_scene_or_404
from seam_studio.schemas.calibration import (
    CalibrationReport,
    CalibrationRequest,
    DisambiguationReport,
    DisambiguationRequest,
    MeasurementSample,
    TrajectoryValidationReport,
    TrajectoryValidationRequest,
)
from seam_studio.schemas.common import StrictModel
from seam_studio.schemas.scene import RFBinding, Scene
from seam_studio.schemas.simulation import SimulateRequest, SimulationConfig
from seam_studio.services.measurement_validation import order_measurements
from seam_studio.services.project_store import ProjectNotFoundError
from seam_studio.services.simulation_backends import BackendUnavailableError, resolve_backend

router = APIRouter(tags=["calibrate"])

# Where the raw imported measurement CSV is kept verbatim so GET can re-parse it.
MEASUREMENTS_CSV_URI = "measurements/measurements.csv"

# Accepted CSV column aliases (case-insensitive). Position may use either the
# bare x/y/z or the rx_-prefixed form; the metric column accepts rsrp_dbm as an
# alias for measured_path_gain_db; the optional capture time (seconds) accepts
# the common drive/flight-log spellings.
_X_KEYS = ("x", "rx_x")
_Y_KEYS = ("y", "rx_y")
_Z_KEYS = ("z", "rx_z")
_GAIN_KEYS = ("measured_path_gain_db", "rsrp_dbm")
_TIME_KEYS = ("time_s", "time", "t", "timestamp_s")


class MeasurementImportRequest(StrictModel):
    csv_text: str


class MeasurementImportResponse(StrictModel):
    measurements: list[MeasurementSample] = Field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)


def _first(row: dict[str, str], keys: tuple[str, ...]) -> Optional[str]:
    for k in keys:
        if k in row and row[k] not in (None, ""):
            return row[k]
    return None


def _parse_measurement_csv(csv_text: str) -> MeasurementImportResponse:
    """Parse measurement rows from CSV text (headers required).

    Accepts headers measurement_id, x/y/z (or rx_x/rx_y/rx_z), tx_id, an
    optional capture time in seconds (time_s/time/t/timestamp_s), and
    measured_path_gain_db (alias rsrp_dbm). Rows missing a coordinate or the
    gain, or with unparseable numbers, are skipped and counted - never fatal.
    When any row carries a time, the returned samples are sorted by time
    ascending (time-less logs keep file order untouched).
    """
    warnings: list[str] = []
    reader = csv.DictReader(io.StringIO(csv_text))
    if reader.fieldnames is None:
        return MeasurementImportResponse(
            measurements=[], skipped=0,
            warnings=["csv had no header row; nothing imported"],
        )
    # Normalize header names to lowercase/stripped for tolerant matching.
    field_map = {(f or "").strip().lower(): f for f in reader.fieldnames}
    have_x = any(k in field_map for k in _X_KEYS)
    have_y = any(k in field_map for k in _Y_KEYS)
    have_z = any(k in field_map for k in _Z_KEYS)
    have_gain = any(k in field_map for k in _GAIN_KEYS)
    if not (have_x and have_y and have_z and have_gain):
        warnings.append(
            "csv is missing required columns (need x/y/z or rx_x/rx_y/rx_z and "
            "measured_path_gain_db or rsrp_dbm); rows may be skipped"
        )

    measurements: list[MeasurementSample] = []
    skipped = 0
    bad_times = 0
    for row in reader:
        # Re-key each row via the normalized header map so lookups are tolerant
        # of original-case / whitespace in the source headers.
        norm = {
            key: row.get(orig)
            for key, orig in field_map.items()
        }
        x = _first(norm, _X_KEYS)
        y = _first(norm, _Y_KEYS)
        z = _first(norm, _Z_KEYS)
        gain = _first(norm, _GAIN_KEYS)
        if x is None or y is None or z is None or gain is None:
            skipped += 1
            continue
        try:
            rx = [float(x), float(y), float(z)]
            gain_db = float(gain)
        except (TypeError, ValueError):
            skipped += 1
            continue
        mid = _first(norm, ("measurement_id",))
        tx = _first(norm, ("tx_id",))
        # Optional capture time: a present-but-unparseable value degrades to
        # "no time" (the row's required data is intact), counted in a warning.
        time_s: Optional[float] = None
        raw_time = _first(norm, _TIME_KEYS)
        if raw_time is not None:
            try:
                time_s = float(raw_time)
            except (TypeError, ValueError):
                bad_times += 1
        measurements.append(
            MeasurementSample(
                measurement_id=mid,
                time_s=time_s,
                rx_position=rx,
                tx_id=tx,
                measured_path_gain_db=gain_db,
            )
        )

    if skipped:
        warnings.append(f"skipped {skipped} malformed row(s)")
    if bad_times:
        warnings.append(
            f"{bad_times} row(s) had an unparseable time value; treated as "
            "missing (kept, ordered after the timed rows)"
        )
    return MeasurementImportResponse(
        measurements=order_measurements(measurements),
        skipped=skipped,
        warnings=warnings,
    )


def _resolve_config(scene: Scene, request: CalibrationRequest) -> SimulationConfig:
    if request.config is not None:
        return request.config
    if request.config_id is not None:
        for cfg in scene.simulation_configs:
            if cfg.id == request.config_id:
                return cfg
        raise HTTPException(status_code=404, detail=f"config not found: {request.config_id}")
    return scene.simulation_configs[0] if scene.simulation_configs else SimulationConfig()


@router.post(
    "/projects/{project_id}/calibrate/materials", response_model=CalibrationReport
)
def calibrate_materials(project_id: str, request: CalibrationRequest) -> CalibrationReport:
    from seam_studio.services.calibration import calibrate_material

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(scene, request)
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    project_dir = store.resolve(project_id)

    try:
        report = calibrate_material(backend, project_dir, scene, library, config, request)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if report.applied and report.fitted_value is not None:
        # Persist the fitted parameter and promote prims using this material.
        mat = library.get(request.target_material_id)
        setattr(mat, request.param, report.fitted_value)
        mat.builtin = False
        mat.notes = (mat.notes + " ").strip() + "[measurement-calibrated]"
        store.save_materials(project_id, library)
        promoted = 0
        for prim in scene.prims:
            if prim.rf.material_id == request.target_material_id:
                prim.rf = RFBinding(
                    material_id=prim.rf.material_id,
                    thickness_m=prim.rf.thickness_m,
                    scattering_coefficient=prim.rf.scattering_coefficient,
                    xpd_coefficient=prim.rf.xpd_coefficient,
                    assignment_status="measurement_calibrated",
                    assignment_sources=list(prim.rf.assignment_sources) + ["calibration"],
                    confidence=prim.rf.confidence,
                )
                promoted += 1
        store.save_scene(project_id, scene)
        store.append_provenance(
            project_id,
            {
                "type": "calibrate",
                "material": request.target_material_id,
                "param": request.param,
                "fitted_value": report.fitted_value,
                "rmse_before_db": report.before.rmse_db,
                "rmse_after_db": report.after.rmse_db,
                "prims_promoted": promoted,
            },
        )
    return report


@router.post(
    "/projects/{project_id}/calibrate/disambiguate",
    response_model=DisambiguationReport,
)
def disambiguate(project_id: str, request: DisambiguationRequest) -> DisambiguationReport:
    """Rank candidate RF materials for a prim by measurement fit (the
    RF-sensing disambiguation companion to the AI suggestion flow)."""
    from seam_studio.services.calibration import disambiguate_materials

    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(scene, request)
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    try:
        return disambiguate_materials(
            backend, store.resolve(project_id), scene, library, config, request
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post(
    "/projects/{project_id}/calibrate/measurements/import-csv",
    response_model=MeasurementImportResponse,
)
def import_measurements_csv(
    project_id: str, request: MeasurementImportRequest
) -> MeasurementImportResponse:
    """Import measurement samples from CSV text and persist the raw CSV.

    Bad rows are skipped and counted (never fatal). The uploaded CSV is stored
    verbatim so a later GET re-parses the exact source the user provided.
    """
    store = get_store()
    # 404 on an unknown project, consistent with the other calibrate routes.
    load_scene_or_404(store, project_id)
    result = _parse_measurement_csv(request.csv_text)
    store.save_text(project_id, MEASUREMENTS_CSV_URI, request.csv_text)
    return result


@router.get(
    "/projects/{project_id}/calibrate/measurements",
    response_model=MeasurementImportResponse,
)
def get_measurements(project_id: str) -> MeasurementImportResponse:
    """Re-parse the stored measurement CSV (404 when none was ever imported)."""
    store = get_store()
    load_scene_or_404(store, project_id)
    try:
        path = store.asset_path(project_id, MEASUREMENTS_CSV_URI)
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=f"project not found: {project_id}")
    if not path.is_file():
        raise HTTPException(
            status_code=404, detail="no measurements imported for this project"
        )
    return _parse_measurement_csv(path.read_text(encoding="utf-8"))


@router.post(
    "/projects/{project_id}/calibrate/validate-trajectory",
    response_model=TrajectoryValidationReport,
)
def validate_trajectory(
    project_id: str, request: Optional[TrajectoryValidationRequest] = None
) -> TrajectoryValidationReport:
    """Measured-vs-predicted path gain along the flight/drive log.

    The measurement points (inline, or the project's stored import when the
    body carries none) are time-ordered and their RX positions replayed as the
    waypoints of the trajectory solver; per-point predictions are then aligned
    to the measurements with the calibration module's level-offset math and
    scored (RMSE/MAE). Computed on demand - no result set is persisted.
    """
    from seam_studio.services.measurement_validation import validate_trajectory as _validate

    request = request or TrajectoryValidationRequest()
    store = get_store()
    scene = load_scene_or_404(store, project_id)
    library = store.load_materials(project_id)
    config = _resolve_config(scene, request)
    try:
        backend = resolve_backend(config)
    except BackendUnavailableError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    measurements = request.measurements
    if measurements is None:
        # Fall back to the project's stored measurement import.
        try:
            path = store.asset_path(project_id, MEASUREMENTS_CSV_URI)
        except ProjectNotFoundError:
            raise HTTPException(
                status_code=404, detail=f"project not found: {project_id}"
            )
        if not path.is_file():
            raise HTTPException(
                status_code=400,
                detail="no measurements in the request and none imported for "
                "this project (POST calibrate/measurements/import-csv first)",
            )
        measurements = _parse_measurement_csv(
            path.read_text(encoding="utf-8")
        ).measurements
    if not measurements:
        raise HTTPException(status_code=400, detail="measurements must not be empty")

    try:
        return _validate(
            backend, store.resolve(project_id), scene, library, config,
            request, measurements,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
