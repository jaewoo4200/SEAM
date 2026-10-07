"""Packaging invariants: path anchors + the demo project template.

The pip-installable package must resolve its data relative to the package
itself (never the repo root), and ``POST /projects`` with ``template="demo"``
must materialize the full Sample Demo content — that is how a fresh install
gets its first project without bundled binary assets. A source checkout runs
on the repo's ``projects/`` and seeds it from the committed examples.
"""

import shutil
from pathlib import Path

import pytest

from seam_studio.core import paths
from seam_studio.services import demo_project


def test_data_dir_is_package_relative():
    # DATA_DIR must live inside the package so wheels resolve it via their own
    # __file__, not via a repo-root guess that breaks in site-packages.
    assert paths.DATA_DIR == paths.APP_ROOT / "data"
    assert paths.APP_ROOT.name == "seam_studio"
    assert paths.DEFAULT_RF_MATERIALS_FILE.is_file()


def test_source_checkout_detected_in_tests():
    # These tests run from the repo, so the source-checkout layout (repo
    # projects/ only; the examples are copied in, never opened in place) is
    # the default.
    assert paths.IS_SOURCE_CHECKOUT is True
    assert paths.REPO_ROOT is not None
    assert paths.DEFAULT_PROJECT_ROOTS == [paths.REPO_ROOT / "projects"]
    assert paths.EXAMPLE_PROJECTS_DIR == paths.REPO_ROOT / "examples" / "demo_project"


def test_demo_template_materializes_full_project(api_client):
    resp = api_client.post(
        "/api/projects", json={"name": "Sample Demo", "template": "demo"}
    )
    assert resp.status_code == 201, resp.text
    info = resp.json()
    assert info["project_id"] == "sample_demo"

    scene = api_client.get(f"/api/projects/{info['project_id']}/scene").json()
    assert len(scene["prims"]) == 13  # 8 mesh + 5 group
    assert len(scene["devices"]) == 3  # + the v0.1.14 co-located sensing RX
    assert len(scene["actors"]) == 3  # + the v0.1.14 drone target
    assert scene["simulation_configs"][0]["frequency_hz"] == 28e9

    # The GLB was generated and every mesh prim resolves into it.
    glb = Path(info["path"]) / "visual" / "scene.glb"
    assert glb.is_file() and glb.stat().st_size > 10_000
    assert (Path(info["path"]) / "mapping" / "object_map.json").is_file()

    # The demo must be immediately simulatable on the mock backend.
    sim = api_client.post(
        f"/api/projects/{info['project_id']}/simulate/paths",
        json={"config": {"id": "t", "name": "t", "backend": "mock",
                          "frequency_hz": 28e9, "max_depth": 2}},
    )
    assert sim.status_code == 200, sim.text
    assert len(sim.json()["paths"]) > 0


def test_demo_template_duplicate_id_400(api_client):
    first = api_client.post(
        "/api/projects", json={"name": "Demo", "template": "demo"}
    )
    assert first.status_code == 201
    again = api_client.post(
        "/api/projects", json={"name": "Demo", "template": "demo"}
    )
    assert again.status_code == 400


MOCK = {"id": "t", "name": "t", "backend": "mock", "frequency_hz": 28e9, "max_depth": 2}


def test_demo_supports_sensing_out_of_the_box(api_client):
    # The Sample Demo ships a drone target and a sensing RX at the TX, so the
    # sensing guide's examples run on a fresh install.
    assert api_client.post("/api/projects", json={"name": "Demo", "template": "demo"}).status_code == 201
    base = "/api/projects/sample_demo"
    sensing = api_client.post(f"{base}/simulate/sensing", json={"config": MOCK})
    assert sensing.status_code == 200, sensing.text
    echoes = [p for p in sensing.json()["paths"] if p["target_id"] == "uav_001"]
    assert any(p["tx_id"] == "tx_001" and p["rx_id"] == "tx_001_rx" for p in echoes)

    scenario = api_client.post(
        f"{base}/simulate/scenario",
        json={"config": MOCK, "num_frames": 3, "dt_s": 0.5, "include_paths": False,
              "sensing": {"enabled": True}},
    )
    assert scenario.status_code == 200, scenario.text
    summary = scenario.json()["metadata"]["sensing"]
    assert summary["sensing_rx_ids"] == ["tx_001_rx"]
    assert summary["sensing_rx_rule"] == "colocated"

    isac = api_client.post(
        f"{base}/simulate/isac", json={"config": MOCK, "tx_rows": 2, "tx_cols": 2}
    )
    assert isac.status_code == 200, isac.text
    tx = isac.json()["txs"][0]
    assert tx["tx_id"] == "tx_001" and tx["sensing_rx_id"] == "tx_001_rx"
    assert tx["ue_ids"] == ["rx_001"] and tx["target_ids"] == ["uav_001"]


# ------------------------------------------------ source-checkout seeding


@pytest.fixture()
def examples(tmp_path: Path) -> Path:
    """A private copy of two committed examples (ftc_outdoor is ~190 MB)."""
    src = paths.REPO_ROOT / "examples" / "demo_project"
    out = tmp_path / "examples"
    for pid in ("lab_room", "sample_demo"):
        shutil.copytree(src / f"{pid}.seam", out / f"{pid}.seam")
    # The compiled rf artefacts are gitignored (absent on a fresh checkout / CI):
    # stand-ins make the "not copied" checks meaningful everywhere.
    rf = out / "sample_demo.seam" / "rf"
    (rf / "meshes").mkdir(parents=True, exist_ok=True)
    (rf / "meshes" / "stand_in.ply").write_text("ply\n", encoding="utf-8")
    (rf / "generated_scene.xml").write_text("<scene/>", encoding="utf-8")
    (rf / "compile_manifest.json").write_text("{}", encoding="utf-8")
    return out


def test_seed_copies_missing_examples_once(tmp_path: Path, examples: Path):
    from seam_studio.services.project_store import ProjectStore

    root = tmp_path / "projects"
    root.mkdir()
    assert (examples / "sample_demo.seam" / "rf" / "generated_scene.xml").is_file()
    assert demo_project.seed_checkout_projects(root, examples) == ["lab_room", "sample_demo"]

    scene = ProjectStore(roots=[root]).load_scene("sample_demo")
    assert scene.device_by_id("tx_001_rx") is not None
    assert [a.id for a in scene.actors][-1] == "uav_001"
    rf = root / "sample_demo.seam" / "rf"
    assert not (rf / "generated_scene.xml").exists()
    assert not (rf / "compile_manifest.json").exists() and not (rf / "meshes").exists()
    assert (rf / "materials.yaml").is_file()  # the rest of rf/ is copied
    # The examples themselves are never touched.
    example_scene = ProjectStore(roots=[examples]).load_scene("sample_demo")
    assert example_scene.device_by_id("tx_001_rx") is None
    assert (examples / "sample_demo.seam" / "rf" / "generated_scene.xml").is_file()

    assert demo_project.seed_checkout_projects(root, examples) == []


def _failing_copytree(monkeypatch, fail_name: str, exc: BaseException) -> None:
    real = shutil.copytree

    def copy_or_fail(src, dst, *a, **kw):
        if Path(src).name == fail_name:
            raise exc
        return shutil.copy2(src, dst)

    def copytree(src, dst, *a, **kw):
        if Path(src).name == "sample_demo.seam":
            kw["copy_function"] = copy_or_fail
        return real(src, dst, *a, **kw)

    monkeypatch.setattr(demo_project.shutil, "copytree", copytree)


@pytest.mark.parametrize("fail_name", ["scene.seam.json", "provenance.json"])
def test_interrupted_seed_leaves_nothing_and_retries(monkeypatch, tmp_path: Path,
                                                     examples: Path, fail_name: str):
    # A copy that stops partway (file lock, full disk) used to leave a partial
    # sample_demo.seam that every later start skipped (no scene, or no drone).
    from seam_studio.services.project_store import ProjectStore

    root = tmp_path / "projects"
    root.mkdir()
    with monkeypatch.context() as m:
        _failing_copytree(m, fail_name, PermissionError(32, "locked"))
        assert demo_project.seed_checkout_projects(root, examples) == ["lab_room"]
    assert sorted(p.name for p in root.iterdir()) == ["lab_room.seam"]

    assert demo_project.seed_checkout_projects(root, examples) == ["sample_demo"]
    scene = ProjectStore(roots=[root]).load_scene("sample_demo")
    assert scene.device_by_id("tx_001_rx") is not None
    assert any(a.id == "uav_001" for a in scene.actors)
    assert sorted(p.name for p in root.iterdir()) == ["lab_room.seam", "sample_demo.seam"]


def test_ctrl_c_during_seed_leaves_nothing(monkeypatch, tmp_path: Path, examples: Path):
    root = tmp_path / "projects"
    root.mkdir()
    with monkeypatch.context() as m:
        _failing_copytree(m, "scene.glb", KeyboardInterrupt())
        with pytest.raises(KeyboardInterrupt):
            demo_project.seed_checkout_projects(root, examples)
    assert sorted(p.name for p in root.iterdir()) == ["lab_room.seam"]
    # A staging folder left by a hard kill is cleared, never listed or kept.
    stale = root / ".sample_demo.seam.seeding"
    stale.mkdir()
    (stale / "scene.seam.json").write_text("{}", encoding="utf-8")
    assert demo_project.seed_checkout_projects(root, examples) == ["sample_demo"]
    assert sorted(p.name for p in root.iterdir()) == ["lab_room.seam", "sample_demo.seam"]


def test_seed_never_touches_an_existing_project(tmp_path: Path, examples: Path):
    root = tmp_path / "projects"
    mine = root / "lab_room.seam"
    mine.mkdir(parents=True)
    (mine / "scene.seam.json").write_bytes(b'{"mine": true}')
    legacy = root / "sample_demo.sionnatwin"
    legacy.mkdir()
    (legacy / "scene.sionnatwin.json").write_bytes(b"{}")
    assert demo_project.seed_checkout_projects(root, examples) == []
    assert (mine / "scene.seam.json").read_bytes() == b'{"mine": true}'
    assert sorted(p.name for p in root.iterdir()) == ["lab_room.seam", "sample_demo.sionnatwin"]


def test_seed_default_is_a_noop_with_explicit_roots(monkeypatch, tmp_path: Path):
    # The conftest and every API test point SEAM_PROJECT_ROOTS at tmp dirs:
    # nothing may be copied into the real projects/.
    calls = []
    monkeypatch.setattr(demo_project, "seed_checkout_projects", lambda *a: calls.append(a) or [])
    monkeypatch.setenv("SEAM_PROJECT_ROOTS", str(tmp_path))
    assert demo_project.seed_default_checkout_projects() == []
    monkeypatch.delenv("SEAM_PROJECT_ROOTS")
    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(tmp_path))
    assert demo_project.seed_default_checkout_projects() == []
    assert calls == []


def test_seed_default_targets_the_checkout_projects_root(monkeypatch):
    from seam_studio.core import config

    calls = []
    monkeypatch.setattr(demo_project, "seed_checkout_projects", lambda *a: calls.append(a) or [])
    monkeypatch.delenv("SEAM_PROJECT_ROOTS", raising=False)
    monkeypatch.delenv("SIONNATWIN_PROJECT_ROOTS", raising=False)
    config.get_settings.cache_clear()
    try:
        assert demo_project.seed_default_checkout_projects() == []
    finally:
        config.get_settings.cache_clear()
    assert calls == [(paths.REPO_ROOT / "projects", paths.EXAMPLE_PROJECTS_DIR)]
