"""Import a Mitsuba/Sionna scene XML (or a whole scene bundle zip) from the UI.

POST /projects/import (multipart/form-data):
    file          the primary Mitsuba .xml, OR a .zip of the whole scene
                  folder (scene XML + mesh subdirs + textures/) with relative
                  paths preserved (required)
    meshes        zero or more additional files (e.g. the .ply meshes the XML
                  references by relative path); each is written into the temp
                  import dir preserving its uploaded filename so the importer
                  resolves ``<string name="filename" value="meshes/x.ply"/>``.
                  Only used for the plain-XML path; a zip carries its own tree.
    project_id    ^[a-z0-9_\\-]+$, must not already exist
    name          human-readable project name
    environment   auto | indoor | outdoor

POST /projects/import/start accepts the same multipart form but returns 202
{"job_id"} immediately and runs the import on a background thread; GET
/projects/import/jobs/{job_id} polls its phase/progress and final result (see
``app.services.import_jobs``). The sync endpoint stays for scripts and tests;
the UI uses the job flow so a campus-scale bundle shows live progress instead
of an opaque frozen button. Both paths stream the upload to a temp file first,
so a multi-hundred-MB bundle never sits in RAM.

The heavy lifting (XML parse -> canonical scene + combined visual trimesh
scene) is done by ``app.services.mitsuba_import.import_mitsuba_scene``; the
persistence layout mirrors ``examples/scripts/import_bundle_scene.py`` (canonical
scene.seam.json (legacy scene.sionnatwin.json), visual/scene.glb,
rf/materials.yaml, provenance.json, a default 28 GHz SimulationConfig). Bitmap textures referenced by the XML are
copied into the project under ``visual/textures/`` (full resolution, for AI
evidence crops) and baked into the GLB (downscaled, for the viewer).

The zip path extracts the archive preserving relative paths (rejecting
traversal, skipping macOS zip cruft) so Blender-style bundles whose XML
references ``meshes_tex/x.ply`` + ``textures/y.png`` resolve exactly as they
would on disk. When a zip contains several scene XMLs (e.g. ``scene.xml`` and
``scene_textured.xml``), the one that resolves the most meshes wins, textured
variants preferred on a tie.
"""

import json
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Literal, Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.api import deps
from app.core.config import APP_VERSION
from app.schemas.common import StrictModel
from app.schemas.projects import ProjectInfo
from app.schemas.simulation import SimulationConfig
from app.services import import_jobs
from app.services.mitsuba_import import import_mitsuba_scene
from app.services.project_store import (
    ProjectNotFoundError,
    ProjectStore,
    load_default_library,
)

router = APIRouter(tags=["projects"])


class SceneImportResult(ProjectInfo):
    """Imported project plus any non-fatal import warnings.

    Warnings (skipped meshes, out-of-band material remaps, degenerate faces)
    used to be written only to provenance.json where they were easy to miss —
    a scene could import "successfully" with buildings silently dropped. The UI
    surfaces this list as a toast right after import.
    """

    warnings: list[str] = []


class ImportJobStarted(StrictModel):
    """202 response of POST /projects/import/start."""

    job_id: str


class ImportJobStatus(StrictModel):
    """Polling snapshot of a background import job.

    ``project``/``warnings`` are populated once ``status == "done"``;
    ``error`` once ``status == "error"`` (same message the sync endpoint's
    HTTPException would have carried).
    """

    job_id: str
    status: Literal["running", "done", "error"]
    phase: str
    done: int
    total: int
    project: Optional[ProjectInfo] = None
    warnings: list[str] = []
    error: Optional[str] = None


_PROJECT_ID_RE = re.compile(r"^[a-z0-9_\-]+$")

# Frequency of the default SimulationConfig created for imported projects
# (28 GHz mmWave ISAC twin). Shared with the importer so out-of-band ITU
# materials are remapped to a band-safe alternative at import time.
_DEFAULT_CONFIG_FREQUENCY_HZ = 28e9


# Zip-bomb guards for bundle uploads: campus-scale scenes are tens of MB of
# PLY + a hundred MB of PNG; these caps are far above any real bundle while
# bounding a hostile archive.
_ZIP_MAX_FILES = 20_000
_ZIP_MAX_TOTAL_BYTES = 4 * 1024**3
_ZIP_MAGIC = b"PK\x03\x04"


def _safe_extract_zip(archive: Path, dest: Path) -> list[str]:
    """Extract a scene bundle preserving relative paths; return extracted names.

    ``archive`` is the upload already streamed to disk — zipfile reads members
    directly from the file, so the whole bundle is never held in RAM.
    Rejects entries that would escape ``dest`` (absolute paths, ``..``, drive
    letters) and skips macOS archive cruft (``__MACOSX/``, ``.DS_Store``,
    AppleDouble ``._*`` siblings) so a zip made with Finder imports cleanly.
    """
    try:
        zf = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise HTTPException(status_code=400, detail=f"invalid zip file: {exc}")
    extracted: list[str] = []
    total = 0
    for entry in zf.infolist():
        if entry.is_dir():
            continue
        name = entry.filename.replace("\\", "/")
        parts = PurePosixPath(name).parts
        if (
            not parts
            or name.startswith("/")
            or any(p == ".." for p in parts)
            or ":" in parts[0]
        ):
            continue  # unsafe entry: never let an archive place files outside dest
        if "__MACOSX" in parts or parts[-1] == ".DS_Store" or parts[-1].startswith("._"):
            continue
        total += entry.file_size
        if len(extracted) >= _ZIP_MAX_FILES or total > _ZIP_MAX_TOTAL_BYTES:
            raise HTTPException(
                status_code=413,
                detail="zip bundle too large "
                f"(> {_ZIP_MAX_FILES} files or > {_ZIP_MAX_TOTAL_BYTES // 1024**3} GB)",
            )
        target = dest.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(entry) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)
        extracted.append("/".join(parts))
    return extracted


def _pick_scene_xml(tmp_dir: Path) -> Optional[tuple[Path, int, int]]:
    """Choose the best scene XML in an extracted bundle.

    Returns (xml_path, resolved_meshes, referenced_meshes) for the candidate
    whose PLY references resolve best relative to its own directory; on a tie
    a textured variant (any ``<texture type="bitmap">``) wins, then the
    shallower path. None when no parseable ``<scene>`` XML exists.
    """
    best: Optional[tuple[Path, int, int]] = None
    best_key: Optional[tuple[int, int, int]] = None
    for xml in sorted(tmp_dir.rglob("*.xml")):
        try:
            root = ET.parse(xml).getroot()
        except ET.ParseError:
            continue
        if root.tag != "scene":
            continue
        refs = [
            el.get("value", "")
            for shape in root.findall("shape")
            for el in [shape.find("string[@name='filename']")]
            if shape.get("type") == "ply" and el is not None
        ]
        resolved = sum(1 for r in refs if (xml.parent / r).is_file())
        textured = 1 if root.find(".//texture[@type='bitmap']") is not None else 0
        key = (resolved, textured, -len(xml.relative_to(tmp_dir).parts))
        if best_key is None or key > best_key:
            best, best_key = (xml, resolved, len(refs)), key
    return best


def _safe_upload_name(name: Optional[str]) -> Optional[str]:
    """Keep only the basename of an uploaded file, refusing traversal.

    Browsers send a bare filename, but a crafted multipart part could carry a
    path. We only ever want the leaf name written into the flat temp dir (or a
    single ``meshes/`` subdir), never something that escapes it.
    """
    if not name:
        return None
    leaf = Path(name.replace("\\", "/")).name
    if not leaf or leaf in {".", ".."}:
        return None
    return leaf


def _payload_is_empty(path: Path) -> bool:
    """True when the streamed upload contains no non-whitespace bytes.

    Reads in chunks so a multi-GB bundle is never pulled into RAM just for
    the empty-file check (real uploads bail out on the first chunk).
    """
    with path.open("rb") as fh:
        while chunk := fh.read(65536):
            if chunk.strip():
                return False
    return True


def _check_import_request(project_id: str, environment: str) -> None:
    """Shared fail-fast validation for both import endpoints.

    400 on a bad project_id/environment, 409 when the project already exists —
    checked up front so we never touch disk (or start a doomed job) for an
    invalid request.
    """
    if not _PROJECT_ID_RE.match(project_id):
        raise HTTPException(
            status_code=400,
            detail=(
                f"invalid project_id {project_id!r}: use only lowercase "
                "letters, digits, hyphen and underscore"
            ),
        )
    if environment not in {"auto", "indoor", "outdoor"}:
        raise HTTPException(
            status_code=400,
            detail=f"invalid environment {environment!r}: use auto|indoor|outdoor",
        )
    store = deps.get_store()
    try:
        store.resolve(project_id)
        raise HTTPException(
            status_code=409, detail=f"project already exists: {project_id}"
        )
    except ProjectNotFoundError:
        pass


def _run_import(
    payload_path: Path,
    upload_name: str,
    project_id: str,
    name: str,
    environment: str,
    extra_meshes: list[tuple[str, bytes]],
    progress: Callable[..., None],
) -> tuple[ProjectInfo, list[str]]:
    """Import the streamed upload into a new project; return (info, warnings).

    Shared by the sync endpoint (``progress`` = no-op) and the background job
    worker. ``payload_path`` is the upload already streamed to a temp file on
    disk; only the 4-byte magic is read up front and the zip/XML handling
    works off the path, so a campus-scale bundle never sits in RAM.

    ``progress(phase, done, total)`` is called at phase boundaries:
    ("extracting", 1, 4) while unzipping a bundle (zip path only),
    ("parsing", 2, 4) before import_mitsuba_scene, ("writing", 3, 4) before
    project materialization/GLB export, and ("done", 4, 4) at the end.

    Failures raise HTTPException exactly like the original inline body; the
    job runner records ``.detail`` as the job's error message.
    """
    store = deps.get_store()
    with tempfile.TemporaryDirectory(prefix="sionnatwin_import_") as td:
        tmp_dir = Path(td)
        if _payload_is_empty(payload_path):
            raise HTTPException(status_code=400, detail="uploaded file is empty")
        with payload_path.open("rb") as fh:
            magic = fh.read(4)

        source_xml = upload_name
        if magic == _ZIP_MAGIC or upload_name.lower().endswith(".zip"):
            # Bundle path: the zip carries the scene folder's relative tree
            # (meshes_tex/, textures/, ...), so the importer's XML-dir-relative
            # resolution works exactly as it would on disk.
            progress("extracting", 1, 4)
            _safe_extract_zip(payload_path, tmp_dir)
            picked = _pick_scene_xml(tmp_dir)
            if picked is None:
                raise HTTPException(
                    status_code=400,
                    detail="no Mitsuba <scene> XML found inside the zip bundle",
                )
            xml_path, _resolved, _referenced = picked
            source_xml = xml_path.relative_to(tmp_dir).as_posix()
        else:
            xml_name = upload_name
            if not xml_name.lower().endswith(".xml"):
                xml_name = f"{Path(xml_name).stem or 'scene'}.xml"
            xml_path = tmp_dir / xml_name
            shutil.copyfile(payload_path, xml_path)
            source_xml = xml_name

            # Companion mesh files: written flat under tmp_dir AND under a
            # nested ``meshes/`` dir, since Sionna XMLs conventionally
            # reference ``meshes/<name>.ply``. Writing both makes either
            # reference resolve.
            (tmp_dir / "meshes").mkdir(exist_ok=True)
            for leaf, data in extra_meshes:
                (tmp_dir / leaf).write_bytes(data)
                (tmp_dir / "meshes" / leaf).write_bytes(data)

        library = load_default_library()
        progress("parsing", 2, 4)
        try:
            scene, tm_scene, warnings, texture_files = import_mitsuba_scene(
                xml_path,
                project_id,
                library,
                scene_name=name or project_id,
                # Keep in sync with the default SimulationConfig below so ITU
                # materials out of band at the project frequency (e.g. ITU
                # ground at 28 GHz) are remapped to a band-safe alternative at
                # import time.
                default_frequency_hz=_DEFAULT_CONFIG_FREQUENCY_HZ,
            )
        except Exception as exc:  # malformed XML, unreadable meshes, ...
            raise HTTPException(
                status_code=400, detail=f"failed to import scene: {exc}"
            )

        if not scene.prims:
            # Every shape was skipped (typically: referenced external meshes not
            # uploaded). A prim-less project is not useful, so refuse it with a
            # count of what was missing.
            missing = [w for w in warnings if "mesh not found" in w]
            hint = (
                f" {len(missing)} referenced mesh file(s) were not found "
                f"(first: {missing[0].split(': ', 1)[-1] if missing else '?'}). "
                "Upload the .ply/.obj meshes alongside the XML, or upload a "
                ".zip of the whole scene folder (XML + mesh dirs + textures)."
                if missing
                else " No importable <shape type=\"ply\"> geometry was found."
            )
            raise HTTPException(
                status_code=400,
                detail="no geometry imported from the uploaded XML." + hint,
            )

        scene.environment = environment  # type: ignore[assignment]
        scene.simulation_configs = [
            SimulationConfig(
                id="default", name="Default 28 GHz", backend="auto",
                frequency_hz=_DEFAULT_CONFIG_FREQUENCY_HZ, max_depth=3,
            )
        ]

        # Materialize the project folder via the store (creates the scaffold +
        # default library + provenance) then overwrite scene, GLB and
        # provenance with the imported content, mirroring import_bundle_scene.py.
        progress("writing", 3, 4)
        info = store.create_project(name=name or project_id, project_id=project_id)
        project_dir = Path(info.path)

        (project_dir / "visual" / "scene.glb").write_bytes(
            tm_scene.export(file_type="glb")
        )
        # Persist the ORIGINAL full-resolution textures the XML referenced:
        # the GLB embeds viewer-sized copies, but AI evidence crops read these.
        for rel, src in texture_files.items():
            dest = project_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dest)
        store.save_scene(project_id, scene)
        ProjectStore.save_materials_to_dir(project_dir, library)
        (project_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "created_by": f"seam-studio/{APP_VERSION} (import)",
                    "source_upload": upload_name,
                    "source_xml": source_xml,
                    "environment": environment,
                    "textures_persisted": len(texture_files),
                    "import_warnings": warnings,
                    "events": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        result = store.info(project_dir)

    progress("done", 4, 4)
    return result, warnings


@router.post("/projects/import", response_model=SceneImportResult, status_code=201)
def import_project(
    file: UploadFile = File(..., description="Mitsuba/Sionna scene .xml"),
    project_id: str = Form(...),
    name: str = Form(...),
    environment: str = Form("auto"),
    meshes: list[UploadFile] = File(default=[]),
) -> SceneImportResult:
    # Deliberately SYNC (FastAPI runs it in the threadpool): a campus zip
    # means minutes of extract + Mitsuba parse + GLB export, and an async def
    # would pin all of it on the event loop - /health, the project list and
    # the events WebSocket froze exactly while the user watched an import.
    _check_import_request(project_id, environment)

    upload_name = _safe_upload_name(file.filename) or "scene.xml"

    with tempfile.TemporaryDirectory(prefix="sionnatwin_upload_") as td:
        # Stream the upload to disk (chunked) instead of file.file.read():
        # the old whole-payload read held a full campus bundle in RAM.
        payload_path = Path(td) / "payload.bin"
        with payload_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        extra_meshes: list[tuple[str, bytes]] = []
        for extra in meshes:
            leaf = _safe_upload_name(extra.filename)
            if leaf:
                extra_meshes.append((leaf, extra.file.read()))
        info, warnings = _run_import(
            payload_path,
            upload_name,
            project_id,
            name,
            environment,
            extra_meshes,
            progress=lambda *args, **kwargs: None,
        )

    return SceneImportResult(**info.model_dump(), warnings=warnings)


@router.post(
    "/projects/import/start", response_model=ImportJobStarted, status_code=202
)
def start_project_import(
    file: UploadFile = File(..., description="Mitsuba/Sionna scene .xml or bundle .zip"),
    project_id: str = Form(...),
    name: str = Form(...),
    environment: str = Form("auto"),
    meshes: list[UploadFile] = File(default=[]),
) -> ImportJobStarted:
    """Kick off a background import; poll GET /projects/import/jobs/{job_id}.

    Same multipart contract as POST /projects/import, but returns 202 with a
    job id immediately so the UI can show phase/progress instead of a frozen
    button during a minutes-long campus bundle import. Validation and the
    duplicate-id check run BEFORE the job is registered, so obvious mistakes
    still fail fast with the familiar 400/409 instead of a doomed job.
    """
    _check_import_request(project_id, environment)
    if import_jobs.is_import_running(project_id):
        raise HTTPException(
            status_code=409,
            detail=f"import already in progress for this project id: {project_id}",
        )

    upload_name = _safe_upload_name(file.filename) or "scene.xml"

    # mkdtemp, NOT TemporaryDirectory: the request scope ends before the
    # worker thread runs, so the WORKER owns cleanup (its finally below).
    staging = Path(tempfile.mkdtemp(prefix="sionnatwin_import_job_"))
    try:
        payload_path = staging / "payload.bin"
        with payload_path.open("wb") as out:
            shutil.copyfileobj(file.file, out)
        # Stream each companion mesh too; an index prefix keeps duplicate leaf
        # names as distinct files while _run_import still sees the original
        # leaf (last-one-wins, same as the sync path).
        mesh_dir = staging / "extra_meshes"
        mesh_dir.mkdir()
        mesh_files: list[tuple[str, Path]] = []
        for i, extra in enumerate(meshes):
            leaf = _safe_upload_name(extra.filename)
            if not leaf:
                continue
            stored = mesh_dir / f"{i:04d}_{leaf}"
            with stored.open("wb") as out:
                shutil.copyfileobj(extra.file, out)
            mesh_files.append((leaf, stored))
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    def worker(progress: import_jobs.ProgressCb) -> tuple[ProjectInfo, list[str]]:
        try:
            extra_meshes = [(leaf, path.read_bytes()) for leaf, path in mesh_files]
            return _run_import(
                payload_path,
                upload_name,
                project_id,
                name,
                environment,
                extra_meshes,
                progress,
            )
        finally:
            shutil.rmtree(staging, ignore_errors=True)

    try:
        job_id = import_jobs.start_import_job(worker, project_id=project_id)
    except import_jobs.ImportInProgressError:
        # Race window: another request registered a job for this id between
        # our is_import_running check and here.
        shutil.rmtree(staging, ignore_errors=True)
        raise HTTPException(
            status_code=409,
            detail=f"import already in progress for this project id: {project_id}",
        )
    return ImportJobStarted(job_id=job_id)


@router.get("/projects/import/jobs/{job_id}", response_model=ImportJobStatus)
def get_import_job_status(job_id: str) -> ImportJobStatus:
    """Poll a background import job started by POST /projects/import/start."""
    job = import_jobs.get_import_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"unknown import job: {job_id}")
    return ImportJobStatus(
        job_id=job["job_id"],
        status=job["status"],
        phase=job["phase"],
        done=job["done"],
        total=job["total"],
        project=job["project"],
        warnings=job["warnings"],
        error=job["error"],
    )
