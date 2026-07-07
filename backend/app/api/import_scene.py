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

The heavy lifting (XML parse -> canonical scene + combined visual trimesh
scene) is done by ``app.services.mitsuba_import.import_mitsuba_scene``; the
persistence layout mirrors ``examples/scripts/import_bundle_scene.py`` (canonical
scene.sionnatwin.json, visual/scene.glb, rf/materials.yaml, provenance.json, a
default 28 GHz SimulationConfig). Bitmap textures referenced by the XML are
copied into the project under ``visual/textures/`` (full resolution, for AI
evidence crops) and baked into the GLB (downscaled, for the viewer).

The zip path extracts the archive preserving relative paths (rejecting
traversal, skipping macOS zip cruft) so Blender-style bundles whose XML
references ``meshes_tex/x.ply`` + ``textures/y.png`` resolve exactly as they
would on disk. When a zip contains several scene XMLs (e.g. ``scene.xml`` and
``scene_textured.xml``), the one that resolves the most meshes wins, textured
variants preferred on a tie.
"""

import io
import json
import re
import shutil
import tempfile
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, UploadFile

from app.api import deps
from app.core.config import APP_VERSION
from app.schemas.projects import ProjectInfo
from app.schemas.simulation import SimulationConfig
from app.services.mitsuba_import import import_mitsuba_scene
from app.services.project_store import (
    ProjectNotFoundError,
    ProjectStore,
    load_default_library,
)

router = APIRouter(tags=["projects"])

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


def _safe_extract_zip(data: bytes, dest: Path) -> list[str]:
    """Extract a scene bundle preserving relative paths; return extracted names.

    Rejects entries that would escape ``dest`` (absolute paths, ``..``, drive
    letters) and skips macOS archive cruft (``__MACOSX/``, ``.DS_Store``,
    AppleDouble ``._*`` siblings) so a zip made with Finder imports cleanly.
    """
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
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


@router.post("/projects/import", response_model=ProjectInfo, status_code=201)
def import_project(
    file: UploadFile = File(..., description="Mitsuba/Sionna scene .xml"),
    project_id: str = Form(...),
    name: str = Form(...),
    environment: str = Form("auto"),
    meshes: list[UploadFile] = File(default=[]),
) -> ProjectInfo:
    # Deliberately SYNC (FastAPI runs it in the threadpool): a campus zip
    # means minutes of extract + Mitsuba parse + GLB export, and an async def
    # would pin all of it on the event loop - /health, the project list and
    # the events WebSocket froze exactly while the user watched an import.
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
    # Duplicate check up front so we never touch disk for an existing id.
    try:
        store.resolve(project_id)
        raise HTTPException(
            status_code=409, detail=f"project already exists: {project_id}"
        )
    except ProjectNotFoundError:
        pass

    upload_name = _safe_upload_name(file.filename) or "scene.xml"

    with tempfile.TemporaryDirectory(prefix="sionnatwin_import_") as td:
        tmp_dir = Path(td)
        payload = file.file.read()
        if not payload.strip():
            raise HTTPException(status_code=400, detail="uploaded file is empty")

        source_xml = upload_name
        if payload[:4] == _ZIP_MAGIC or upload_name.lower().endswith(".zip"):
            # Bundle path: the zip carries the scene folder's relative tree
            # (meshes_tex/, textures/, ...), so the importer's XML-dir-relative
            # resolution works exactly as it would on disk.
            _safe_extract_zip(payload, tmp_dir)
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
            xml_path.write_bytes(payload)
            source_xml = xml_name

            # Companion mesh files: written flat under tmp_dir AND under a
            # nested ``meshes/`` dir, since Sionna XMLs conventionally
            # reference ``meshes/<name>.ply``. Writing both makes either
            # reference resolve.
            (tmp_dir / "meshes").mkdir(exist_ok=True)
            for extra in meshes:
                leaf = _safe_upload_name(extra.filename)
                if not leaf:
                    continue
                data = extra.file.read()
                (tmp_dir / leaf).write_bytes(data)
                (tmp_dir / "meshes" / leaf).write_bytes(data)

        library = load_default_library()
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
                    "created_by": f"sionnatwin-studio/{APP_VERSION} (import)",
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

    return store.info(project_dir)
