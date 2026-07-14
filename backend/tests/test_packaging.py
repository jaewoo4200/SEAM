"""Packaging invariants: path anchors + the demo project template.

The pip-installable package must resolve its data relative to the package
itself (never the repo root), and ``POST /projects`` with ``template="demo"``
must materialize the full Sample Demo content — that is how a fresh install
gets its first project without bundled binary assets.
"""

from pathlib import Path

from seam_studio.core import paths


def test_data_dir_is_package_relative():
    # DATA_DIR must live inside the package so wheels resolve it via their own
    # __file__, not via a repo-root guess that breaks in site-packages.
    assert paths.DATA_DIR == paths.APP_ROOT / "data"
    assert paths.APP_ROOT.name == "seam_studio"
    assert paths.DEFAULT_RF_MATERIALS_FILE.is_file()


def test_source_checkout_detected_in_tests():
    # These tests run from the repo, so the source-checkout layout (repo
    # projects/ + committed example) must be the default.
    assert paths.IS_SOURCE_CHECKOUT is True
    assert paths.REPO_ROOT is not None
    assert paths.DEFAULT_PROJECT_ROOTS[0] == paths.REPO_ROOT / "projects"


def test_demo_template_materializes_full_project(api_client):
    resp = api_client.post(
        "/api/projects", json={"name": "Sample Demo", "template": "demo"}
    )
    assert resp.status_code == 201, resp.text
    info = resp.json()
    assert info["project_id"] == "sample_demo"

    scene = api_client.get(f"/api/projects/{info['project_id']}/scene").json()
    assert len(scene["prims"]) == 13  # 8 mesh + 5 group
    assert len(scene["devices"]) == 2
    assert len(scene["actors"]) == 2
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
