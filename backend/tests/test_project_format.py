"""Dual project format (SionnaTwin -> SEAM rename) and env-var aliasing.

Covers the invariants from TASK 2:
  * NEW projects created by the store use ``<id>.seam`` dirs holding
    ``scene.seam.json``.
  * A hand-built LEGACY ``<id>.sionnatwin`` project (with
    ``scene.sionnatwin.json``) still loads AND saves back to the same legacy
    file - it is never silently migrated to the new name.
  * Both formats are discovered/listed together, with a stable order.
  * ``SEAM_*`` env vars take precedence over the legacy ``SIONNATWIN_*`` ones.
"""

from pathlib import Path

import pytest

from seam_studio.core import config
from seam_studio.services.project_store import (
    LEGACY_PROJECT_SUFFIX,
    LEGACY_SCENE_FILENAME,
    PROJECT_SUFFIX,
    SCENE_FILENAME,
    ProjectStore,
    project_id_from_dir,
)

from .conftest import make_demo_scene


# --------------------------------------------------------------- new format


def test_create_project_uses_seam_dir_and_scene_file(tmp_path: Path) -> None:
    store = ProjectStore(roots=[tmp_path])
    info = store.create_project(name="Fresh Project", project_id="fresh")

    project_dir = Path(info.path)
    assert project_dir.name == "fresh.seam"
    assert project_dir.name.endswith(PROJECT_SUFFIX)
    assert (project_dir / SCENE_FILENAME).is_file()
    assert (project_dir / "scene.seam.json").is_file()
    # The legacy file is NOT created for new projects.
    assert not (project_dir / LEGACY_SCENE_FILENAME).is_file()

    # It resolves back by id and round-trips a saved scene.
    assert store.resolve("fresh") == project_dir
    store.save_scene("fresh", make_demo_scene(scene_id="fresh"))
    reloaded = store.load_scene("fresh")
    assert reloaded.scene_id == "fresh"
    # Save still targets the new file, never spawning a legacy sibling.
    assert (project_dir / SCENE_FILENAME).is_file()
    assert not (project_dir / LEGACY_SCENE_FILENAME).is_file()


# ------------------------------------------------------------ legacy format


def _build_legacy_project(root: Path, pid: str) -> Path:
    """Hand-build a ``<pid>.sionnatwin`` project with a legacy scene file."""
    project_dir = root / f"{pid}{LEGACY_PROJECT_SUFFIX}"
    project_dir.mkdir(parents=True)
    scene = make_demo_scene(scene_id=pid)
    (project_dir / LEGACY_SCENE_FILENAME).write_text(
        scene.model_dump_json(indent=2), encoding="utf-8"
    )
    return project_dir


def test_legacy_project_loads(tmp_path: Path) -> None:
    project_dir = _build_legacy_project(tmp_path, "old_scene")
    store = ProjectStore(roots=[tmp_path])

    # Discovered by id (suffix stripped) and loadable.
    assert project_id_from_dir(project_dir) == "old_scene"
    assert store.resolve("old_scene") == project_dir
    scene = store.load_scene("old_scene")
    assert scene.scene_id == "old_scene"


def test_legacy_project_saves_back_to_legacy_file(tmp_path: Path) -> None:
    project_dir = _build_legacy_project(tmp_path, "legacy_save")
    store = ProjectStore(roots=[tmp_path])

    scene = store.load_scene("legacy_save")
    scene.name = "Renamed In Place"
    store.save_scene("legacy_save", scene)

    # The legacy file was updated; NO new scene.seam.json was written
    # (never silently migrated).
    legacy_file = project_dir / LEGACY_SCENE_FILENAME
    assert legacy_file.is_file()
    assert not (project_dir / SCENE_FILENAME).is_file()
    assert store.load_scene("legacy_save").name == "Renamed In Place"


# ------------------------------------------------------- both listed together


def test_both_formats_listed_together(tmp_path: Path) -> None:
    _build_legacy_project(tmp_path, "legacy_one")
    store = ProjectStore(roots=[tmp_path])
    store.create_project(name="New One", project_id="new_one")

    ids = {p.project_id for p in store.list_projects()}
    assert {"legacy_one", "new_one"} <= ids

    # Stable order across repeated scans.
    first = [p.project_id for p in store.list_projects()]
    second = [p.project_id for p in store.list_projects()]
    assert first == second

    # Both resolve independently to their own suffixed folders.
    assert store.resolve("legacy_one").name == "legacy_one.sionnatwin"
    assert store.resolve("new_one").name == "new_one.seam"


def _build_seam_project(root: Path, pid: str) -> Path:
    """Hand-build a ``<pid>.seam`` project with a new-format scene file."""
    project_dir = root / f"{pid}{PROJECT_SUFFIX}"
    project_dir.mkdir(parents=True)
    scene = make_demo_scene(scene_id=pid)
    (project_dir / SCENE_FILENAME).write_text(
        scene.model_dump_json(indent=2), encoding="utf-8"
    )
    return project_dir


def test_seam_wins_on_suffix_collision(tmp_path: Path) -> None:
    # Same stem present as both .seam and .sionnatwin in one root. Both are
    # hand-built because create_project would refuse the second (id taken).
    _build_legacy_project(tmp_path, "dup")
    seam_dir = _build_seam_project(tmp_path, "dup")
    store = ProjectStore(roots=[tmp_path])

    with pytest.warns(UserWarning, match="preferring the .seam folder"):
        resolved = store.resolve("dup")
    assert resolved == seam_dir
    assert resolved.name == "dup.seam"

    with pytest.warns(UserWarning):
        listed = [p.project_id for p in store.list_projects()]
    assert listed.count("dup") == 1


# ----------------------------------------------------------- env precedence


def test_seam_env_overrides_sionnatwin(monkeypatch: pytest.MonkeyPatch) -> None:
    seam_root = "/tmp/seam_roots_precedence"
    legacy_root = "/tmp/sionnatwin_roots_precedence"
    monkeypatch.setenv("SEAM_PROJECT_ROOTS", seam_root)
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", legacy_root)
    monkeypatch.setenv("SEAM_AI_TEXT_MODEL", "seam-model")
    monkeypatch.setenv("SIONNATWIN_AI_TEXT_MODEL", "legacy-model")

    config.get_settings.cache_clear()
    try:
        settings = config.get_settings()
        # SEAM_ wins for the project roots and the AI text model.
        assert str(settings.project_roots[0]) == str(Path(seam_root))
        assert all("sionnatwin_roots" not in str(p) for p in settings.project_roots)
        assert settings.ai.text_model == "seam-model"
    finally:
        config.get_settings.cache_clear()


def test_sionnatwin_env_used_when_no_seam(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SEAM_AI_TEXT_MODEL", raising=False)
    monkeypatch.setenv("SIONNATWIN_AI_TEXT_MODEL", "legacy-only-model")

    config.get_settings.cache_clear()
    try:
        assert config.get_settings().ai.text_model == "legacy-only-model"
    finally:
        config.get_settings.cache_clear()
