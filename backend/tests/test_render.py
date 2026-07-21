"""Mitsuba render-to-file endpoint tests.

Skipped when Mitsuba is not importable (the render path is optional, like the
Sionna backend). When Mitsuba is present these render the real ``sample_demo``
projection at low spp / small resolution through the API and assert a valid PNG
comes back.

sample_demo already carries a compiled ``rf/generated_scene.xml`` (bbox roughly
[-40,-40,0] .. [40,40,1.7]), so the camera below frames the whole scene.
"""

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from seam_studio.core.paths import REPO_ROOT

DEMO_SRC = REPO_ROOT / "examples" / "demo_project" / "sample_demo.seam"


def _mitsuba_available() -> bool:
    try:
        import mitsuba  # noqa: F401
    except Exception:  # noqa: BLE001 - any failure means the render path is off
        return False
    return True


pytestmark = pytest.mark.skipif(
    not _mitsuba_available() or not DEMO_SRC.is_dir(),
    reason="mitsuba not installed or sample_demo example not present",
)


@pytest.fixture()
def render_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """TestClient whose project root holds a private copy of sample_demo.

    Copying keeps the render output (export/renders/*.png and any transient
    rewritten XML) out of the real example folder.
    """
    from seam_studio.api import deps
    from seam_studio.core import config

    root = tmp_path / "render_projects"
    root.mkdir()
    shutil.copytree(DEMO_SRC, root / "sample_demo.seam")

    monkeypatch.setenv("SIONNATWIN_PROJECT_ROOTS", str(root))
    config.get_settings.cache_clear()
    deps.get_store.cache_clear()

    from seam_studio.main import create_app

    with TestClient(create_app()) as client:
        yield client

    config.get_settings.cache_clear()
    deps.get_store.cache_clear()


def test_render_sample_demo_returns_png(render_client: TestClient, tmp_path: Path):
    body = {
        "camera_position": [80.0, -80.0, 50.0],
        "look_at": [0.0, 0.0, 1.0],
        "fov_deg": 45.0,
        "width": 320,
        "height": 180,
        "spp": 16,
    }
    resp = render_client.post("/api/projects/sample_demo/render", json=body)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "image/png"

    content = resp.content
    # PNG magic bytes.
    assert content[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(content) > 100

    # The service echoes the on-disk path; the file must exist and be non-empty.
    render_path = Path(resp.headers["X-Render-Path"])
    assert render_path.is_file()
    assert render_path.stat().st_size == len(content) > 0
    assert render_path.suffix == ".png"
    assert render_path.parent.name == "renders"

    # The render must not be an all-black frame: the ambient emitter lights the
    # geometry, so some pixels are non-zero. Decode with PIL rather than
    # mitsuba.Bitmap -- reading the PNG must NOT touch the global Mitsuba
    # variant, which the Sionna solver tests share within a pytest session.
    from PIL import Image

    with Image.open(render_path) as im:
        assert im.size == (320, 180)
        extrema = im.convert("L").getextrema()  # (min_lum, max_lum)
    assert extrema[1] > 0, "render is an all-black frame (no lighting?)"


def test_render_unknown_project_404(render_client: TestClient):
    resp = render_client.post(
        "/api/projects/does_not_exist/render",
        json={"camera_position": [1.0, 1.0, 1.0], "look_at": [0.0, 0.0, 0.0]},
    )
    assert resp.status_code == 404


def test_render_rejects_out_of_range_caps(render_client: TestClient):
    # width > 1920 and spp > 256 must be rejected by request validation (422).
    resp = render_client.post(
        "/api/projects/sample_demo/render",
        json={
            "camera_position": [80.0, -80.0, 50.0],
            "look_at": [0.0, 0.0, 1.0],
            "width": 4096,
            "spp": 4096,
        },
    )
    assert resp.status_code == 422


def test_render_degenerate_camera_400(render_client: TestClient):
    # camera_position == look_at is a bad scene/camera -> 400.
    resp = render_client.post(
        "/api/projects/sample_demo/render",
        json={"camera_position": [0.0, 0.0, 1.0], "look_at": [0.0, 0.0, 1.0], "spp": 16},
    )
    assert resp.status_code == 400
