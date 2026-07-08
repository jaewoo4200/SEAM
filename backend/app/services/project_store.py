"""Project folder persistence.

A SEAM project is a plain folder that contains at minimum a canonical scene
file. Layout (HANDOFF.md section 4):

    <project>/
      scene.seam.json            canonical unified scene (source of truth)
      visual/                    GLB + textures (visual projection source)
      rf/materials.yaml          project RF material library
      rf/meshes/                 compiled RF submeshes (generated)
      rf/generated_scene.xml     compiled Sionna/Mitsuba projection (generated)
      mapping/                   prim id <-> mesh/face-group maps (generated)
      ai/suggestions.jsonl       AI suggestion + decision provenance log
      results/                   normalized simulation results
      provenance.json            project-level event log

Dual format (SionnaTwin -> SEAM rename):
    NEW projects are created as ``<id>.seam`` folders holding ``scene.seam.json``.
    LEGACY projects named ``<id>.sionnatwin`` holding ``scene.sionnatwin.json``
    keep loading forever - they are discovered alongside new ones, and saving
    a legacy project writes back to its original ``scene.sionnatwin.json``
    (never silently migrated). Discovery globs both suffixes; on an id
    collision (both ``<id>.seam`` and ``<id>.sionnatwin`` in one root) the
    ``.seam`` folder wins and a warning is logged.

All writes are atomic (tmp file + os.replace) so a crash never corrupts the
canonical scene.
"""

import json
import os
import re
import threading
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Optional

import yaml

from app.core.config import APP_VERSION, get_settings
from app.core.paths import DEFAULT_RF_MATERIALS_FILE
from app.schemas.materials import RFMaterialLibrary
from app.schemas.projects import ProjectInfo
from app.schemas.scene import Scene

# New (SEAM) format, written for all newly created projects.
SCENE_FILENAME = "scene.seam.json"
PROJECT_SUFFIX = ".seam"

# Legacy (SionnaTwin) format, still discovered and saved back in place.
LEGACY_SCENE_FILENAME = "scene.sionnatwin.json"
LEGACY_PROJECT_SUFFIX = ".sionnatwin"

# Every scene filename we recognize, in preference order (new wins).
SCENE_FILENAMES = (SCENE_FILENAME, LEGACY_SCENE_FILENAME)
# Every project-folder suffix we recognize, longest/newest first.
PROJECT_SUFFIXES = (PROJECT_SUFFIX, LEGACY_PROJECT_SUFFIX)


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


# Per-project append lock, keyed on the resolved project dir. Serializes the
# read-modify-write of provenance.json (and appends to jsonl logs) so that
# concurrent writers - FastAPI sync endpoints run in a threadpool - never lose
# events to a lost update. Kept deliberately SEPARATE from
# material_segmentation.project_write_lock (which is non-reentrant and held by
# GLB-mutating API routes): acquiring this one inside the store can never
# deadlock a caller already holding that other lock. The whole fix lives here
# so all call sites are covered without touching the API routes.
_append_locks: dict[str, threading.Lock] = {}
_append_locks_guard = threading.Lock()


def _append_lock(project_dir: Path) -> threading.Lock:
    key = str(project_dir.resolve())
    with _append_locks_guard:
        lock = _append_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _append_locks[key] = lock
        return lock


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Unique tmp suffix (pid + thread id + uuid) so two concurrent atomic
    # writers to the same target never share - and clobber - one tmp file.
    tmp = path.with_suffix(
        f"{path.suffix}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}.tmp"
    )
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def load_default_library() -> RFMaterialLibrary:
    raw = yaml.safe_load(DEFAULT_RF_MATERIALS_FILE.read_text(encoding="utf-8"))
    return RFMaterialLibrary.model_validate(raw)


def project_id_from_dir(path: Path) -> str:
    name = path.name
    for suffix in PROJECT_SUFFIXES:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return name


def scene_file_in(project_dir: Path) -> Optional[Path]:
    """Return the canonical scene file inside ``project_dir``.

    Prefers the new ``scene.seam.json`` and falls back to the legacy
    ``scene.sionnatwin.json``. Returns ``None`` when the folder is not a
    project (no recognized scene file present).
    """
    for name in SCENE_FILENAMES:
        candidate = project_dir / name
        if candidate.is_file():
            return candidate
    return None


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
            # A root may itself be a project folder (new or legacy layout).
            if scene_file_in(root) is not None:
                yield root
                continue
            # Discover child project folders. Both ``.seam`` and
            # ``.sionnatwin`` are recognized; iteration is sorted for a
            # stable order, and on an id collision within this root the
            # ``.seam`` folder wins (a warning is emitted).
            best: dict[str, Path] = {}
            order: list[str] = []
            for child in sorted(root.iterdir()):
                if not child.is_dir() or scene_file_in(child) is None:
                    continue
                pid = project_id_from_dir(child)
                existing = best.get(pid)
                if existing is None:
                    best[pid] = child
                    order.append(pid)
                    continue
                # Collision: keep the ``.seam`` folder, warn about the other.
                # ``sorted`` yields ".seam" before ".sionnatwin" for a shared
                # stem, so ``existing`` is already the preferred one; but guard
                # explicitly rather than rely on lexical order.
                if child.name.endswith(PROJECT_SUFFIX) and not existing.name.endswith(
                    PROJECT_SUFFIX
                ):
                    best[pid] = child
                warnings.warn(
                    f"project id {pid!r} exists as both {existing.name!r} and "
                    f"{child.name!r} in {root}; preferring the .seam folder",
                    stacklevel=2,
                )
            for pid in order:
                yield best[pid]

    def resolve(self, project_id: str) -> Path:
        for d in self._iter_project_dirs():
            if project_id_from_dir(d) == project_id:
                return d
        raise ProjectNotFoundError(project_id)

    def info(self, project_dir: Path) -> ProjectInfo:
        scene_file = scene_file_in(project_dir) or (project_dir / SCENE_FILENAME)
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
                    "created_by": f"seam-studio/{APP_VERSION}",
                    "events": [],
                },
                indent=2,
            ),
        )
        return self.info(project_dir)

    # ------------------------------------------------------------ scene I/O

    def load_scene(self, project_id: str) -> Scene:
        project_dir = self.resolve(project_id)
        scene_file = scene_file_in(project_dir)
        if scene_file is None:  # resolve() guaranteed a scene file exists
            scene_file = project_dir / SCENE_FILENAME
        raw = scene_file.read_text(encoding="utf-8")
        return Scene.model_validate_json(raw)

    def save_scene(self, project_id: str, scene: Scene) -> None:
        project_dir = self.resolve(project_id)
        # Write back to whichever scene file the project already has; a legacy
        # project keeps its scene.sionnatwin.json (never silently migrated).
        scene_file = scene_file_in(project_dir) or (project_dir / SCENE_FILENAME)
        _atomic_write_text(scene_file, scene.model_dump_json(indent=2))

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
        project_dir = self.resolve(project_id)
        path = self.asset_path(project_id, relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        with _append_lock(project_dir):
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")

    def append_provenance(self, project_id: str, event: dict) -> None:
        project_dir = self.resolve(project_id)
        prov_file = project_dir / "provenance.json"
        with _append_lock(project_dir):
            try:
                data = json.loads(prov_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                data = {"created_at": _utcnow(), "events": []}
            data.setdefault("events", []).append({"timestamp": _utcnow(), **event})
            _atomic_write_text(prov_file, json.dumps(data, indent=2))
