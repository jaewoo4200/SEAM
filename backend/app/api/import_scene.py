"""Import a Mitsuba/Sionna scene XML into a new project from the UI.

POST /projects/import (multipart/form-data):
    file          the primary Mitsuba .xml (required)
    meshes        zero or more additional files (e.g. the .ply meshes the XML
                  references by relative path); each is written into the temp
                  import dir preserving its uploaded filename so the importer
                  resolves ``<string name="filename" value="meshes/x.ply"/>``
    project_id    ^[a-z0-9_\\-]+$, must not already exist
    name          human-readable project name
    environment   auto | indoor | outdoor

The heavy lifting (XML parse -> canonical scene + combined visual trimesh
scene) is done by ``app.services.mitsuba_import.import_mitsuba_scene``; the
persistence layout mirrors ``examples/scripts/import_bundle_scene.py`` (canonical
scene.sionnatwin.json, visual/scene.glb, rf/materials.yaml, provenance.json, a
default 28 GHz SimulationConfig).

Single self-contained scenes work when every referenced mesh is either inline
or uploaded alongside the XML via ``meshes``. A scene whose XML references
external meshes that were not uploaded imports zero prims; that is reported as a
400 telling the user to use examples/scripts/import_bundle_scene.py for
multi-file bundle scenes.
"""

import json
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
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
async def import_project(
    file: UploadFile = File(..., description="Mitsuba/Sionna scene .xml"),
    project_id: str = Form(...),
    name: str = Form(...),
    environment: str = Form("auto"),
    meshes: list[UploadFile] = File(default=[]),
) -> ProjectInfo:
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

    xml_name = _safe_upload_name(file.filename) or "scene.xml"
    if not xml_name.lower().endswith(".xml"):
        xml_name = f"{Path(xml_name).stem or 'scene'}.xml"

    with tempfile.TemporaryDirectory(prefix="sionnatwin_import_") as td:
        tmp_dir = Path(td)
        xml_path = tmp_dir / xml_name
        xml_bytes = await file.read()
        if not xml_bytes.strip():
            raise HTTPException(status_code=400, detail="uploaded XML is empty")
        xml_path.write_bytes(xml_bytes)

        # Companion mesh files: written flat under tmp_dir AND under a nested
        # ``meshes/`` dir, since Sionna XMLs conventionally reference
        # ``meshes/<name>.ply``. Writing both makes either reference resolve.
        (tmp_dir / "meshes").mkdir(exist_ok=True)
        for extra in meshes:
            leaf = _safe_upload_name(extra.filename)
            if not leaf:
                continue
            data = await extra.read()
            (tmp_dir / leaf).write_bytes(data)
            (tmp_dir / "meshes" / leaf).write_bytes(data)

        library = load_default_library()
        try:
            scene, tm_scene, warnings = import_mitsuba_scene(
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
            # uploaded). A prim-less project is not useful, so refuse it and
            # point the user at the multi-file bundle importer.
            missing = [w for w in warnings if "mesh not found" in w]
            hint = (
                " This scene references external mesh files that were not "
                "uploaded. Upload the .ply/.obj meshes alongside the XML, or "
                "use examples/scripts/import_bundle_scene.py for multi-file "
                "bundle scenes."
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
        store.save_scene(project_id, scene)
        ProjectStore.save_materials_to_dir(project_dir, library)
        (project_dir / "provenance.json").write_text(
            json.dumps(
                {
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "created_by": f"sionnatwin-studio/{APP_VERSION} (import)",
                    "source_xml": xml_name,
                    "environment": environment,
                    "import_warnings": warnings,
                    "events": [],
                },
                indent=2,
            ),
            encoding="utf-8",
        )

    return store.info(project_dir)
