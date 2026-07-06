"""Project folder persistence.

A SionnaTwin project is a plain folder (optionally named ``*.sionnatwin``)
that contains at minimum ``scene.sionnatwin.json``. Layout (HANDOFF.md
section 4):

    <project>/
      scene.sionnatwin.json      canonical unified scene (source of truth)
      visual/                    GLB + textures (visual projection source)
      rf/materials.yaml          project RF material library
      rf/meshes/                 compiled RF submeshes (generated)
      rf/generated_scene.xml     compiled Sionna/Mitsuba projection (generated)
      mapping/                   prim id <-> mesh/face-group maps (generated)
      ai/suggestions.jsonl       AI suggestion + decision provenance log
      results/                   normalized simulation results
      provenance.json            project-level event log

All writes are atomic (tmp file + os.replace) so a crash never corrupts the
canonical scene.
"""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import yaml

from app.core.config import APP_VERSION, get_settings
from app.core.paths import DEFAULT_RF_MATERIALS_FILE
from app.schemas.materials import RFMaterialLibrary
from app.schemas.projects import ProjectInfo
from app.schemas.scene import Scene

SCENE_FILENAME = "scene.sionnatwin.json"
PROJECT_SUFFIX = ".sionnatwin"


class ProjectNotFoundError(KeyError):
    def __init__(self, project_id: str):
        super().__init__(project_id)
        self.project_id = project_id

    def __str__(self) -> str:
        return f"project not found: {self.project_id}"


class InvalidAssetPathError(ValueError):
    pass


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def load_default_library() -> RFMaterialLibrary:
    raw = yaml.safe_load(DEFAULT_RF_MATERIALS_FILE.read_text(encoding="utf-8"))
    return RFMaterialLibrary.model_validate(raw)


def project_id_from_dir(path: Path) -> str:
    name = path.name
    if name.endswith(PROJECT_SUFFIX):
        name = name[: -len(PROJECT_SUFFIX)]
    return name


def slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9_\-]+", "_", name.strip().lower()).strip("_")
    return slug or "project"


class ProjectStore:
    def __init__(self, roots: Optional[list[Path]] = None):
        if roots is None:
            roots = [Path(r) for r in get_settings().project_roots]
        self.roots = [Path(r) for r in roots]

    # ------------------------------------------------------------------ scan

    def _iter_project_dirs(self) -> Iterator[Path]:
        for root in self.roots:
            if not root.is_dir():
                continue
            # A root may itself be a project folder.
            if (root / SCENE_FILENAME).is_file():
                yield root
                continue
            for child in sorted(root.iterdir()):
                if child.is_dir() and (child / SCENE_FILENAME).is_file():
                    yield child

    def resolve(self, project_id: str) -> Path:
        for d in self._iter_project_dirs():
            if project_id_from_dir(d) == project_id:
                return d
        raise ProjectNotFoundError(project_id)

    def info(self, project_dir: Path) -> ProjectInfo:
        scene_file = project_dir / SCENE_FILENAME
        scene_id = None
        name = project_id_from_dir(project_dir)
        try:
            raw = json.loads(scene_file.read_text(encoding="utf-8"))
            scene_id = raw.get("scene_id")
            name = raw.get("name") or name
        except (OSError, json.JSONDecodeError):
            pass
        stat = scene_file.stat()
        return ProjectInfo(
            project_id=project_id_from_dir(project_dir),
            name=name,
            path=str(project_dir.resolve()),
            scene_id=scene_id,
            created_at=datetime.fromtimestamp(stat.st_ctime, timezone.utc).isoformat(),
            modified_at=datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
        )

    def list_projects(self) -> list[ProjectInfo]:
        out: list[ProjectInfo] = []
        seen: set[str] = set()
        for d in self._iter_project_dirs():
            pid = project_id_from_dir(d)
            if pid in seen:  # first root wins on id collision
                continue
            seen.add(pid)
            out.append(self.info(d))
        return out

    # ---------------------------------------------------------------- create

    def create_project(
        self,
        name: str,
        project_id: Optional[str] = None,
        root: Optional[Path] = None,
    ) -> ProjectInfo:
        pid = project_id or slugify(name)
        try:
            self.resolve(pid)
            raise ValueError(f"project already exists: {pid}")
        except ProjectNotFoundError:
            pass
        base = Path(root) if root else self.roots[0]
        project_dir = base / f"{pid}{PROJECT_SUFFIX}"
        for sub in ("visual", "rf/meshes", "mapping", "ai", "results"):
            (project_dir / sub).mkdir(parents=True, exist_ok=True)

        scene = Scene(scene_id=pid, name=name)
        _atomic_write_text(
            project_dir / SCENE_FILENAME, scene.model_dump_json(indent=2)
        )
        self.save_materials_to_dir(project_dir, load_default_library())
        _atomic_write_text(
            project_dir / "provenance.json",
            json.dumps(
                {
                    "created_at": _utcnow(),
                    "created_by": f"sionnatwin-studio/{APP_VERSION}",
                    "events": [],
                },
                indent=2,
            ),
        )
        return self.info(project_dir)

    # ------------------------------------------------------------ scene I/O

    def load_scene(self, project_id: str) -> Scene:
        raw = (self.resolve(project_id) / SCENE_FILENAME).read_text(encoding="utf-8")
        return Scene.model_validate_json(raw)

    def save_scene(self, project_id: str, scene: Scene) -> None:
        project_dir = self.resolve(project_id)
        _atomic_write_text(
            project_dir / SCENE_FILENAME, scene.model_dump_json(indent=2)
        )

    # -------------------------------------------------------- materials I/O

    def load_materials(self, project_id: str) -> RFMaterialLibrary:
        mat_file = self.resolve(project_id) / "rf" / "materials.yaml"
        if not mat_file.is_file():
            return load_default_library()
        raw = yaml.safe_load(mat_file.read_text(encoding="utf-8"))
        return RFMaterialLibrary.model_validate(raw)

    def save_materials(self, project_id: str, library: RFMaterialLibrary) -> None:
        self.save_materials_to_dir(self.resolve(project_id), library)

    @staticmethod
    def save_materials_to_dir(project_dir: Path, library: RFMaterialLibrary) -> None:
        text = yaml.safe_dump(
            library.model_dump(mode="json"), sort_keys=False, allow_unicode=True
        )
        _atomic_write_text(project_dir / "rf" / "materials.yaml", text)

    # ------------------------------------------------------------ asset I/O

    def asset_path(self, project_id: str, relative: str) -> Path:
        """Resolve a project-relative asset path, refusing traversal escapes."""
        project_dir = self.resolve(project_id).resolve()
        candidate = (project_dir / relative).resolve()
        if not candidate.is_relative_to(project_dir):
            raise InvalidAssetPathError(f"asset path escapes project: {relative!r}")
        return candidate

    # ---------------------------------------------------------- misc  I/O

    def save_json(self, project_id: str, relative: str, obj: dict) -> Path:
        path = self.asset_path(project_id, relative)
        _atomic_write_text(path, json.dumps(obj, indent=2))
        return path

    def save_text(self, project_id: str, relative: str, text: str) -> Path:
        """Atomically write raw text to a project-relative path (e.g. an
        imported CSV kept verbatim). Mirrors ``save_json`` traversal safety."""
        path = self.asset_path(project_id, relative)
        _atomic_write_text(path, text)
        return path

    def load_json(self, project_id: str, relative: str) -> dict:
        return json.loads(
            self.asset_path(project_id, relative).read_text(encoding="utf-8")
        )

    def append_jsonl(self, project_id: str, relative: str, record: dict) -> None:
        path = self.asset_path(project_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_provenance(self, project_id: str, event: dict) -> None:
        project_dir = self.resolve(project_id)
        prov_file = project_dir / "provenance.json"
        try:
            data = json.loads(prov_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {"created_at": _utcnow(), "events": []}
        data.setdefault("events", []).append({"timestamp": _utcnow(), **event})
        _atomic_write_text(prov_file, json.dumps(data, indent=2))
