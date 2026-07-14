"""AI evidence pipeline: per-prim texture crops + persisted evidence provenance.

Pins ai_provider.extract_prim_texture_crops (file-source-first, UV-bbox crop,
traversal-guarded) and the evidence-persistence gating in suggest_materials
(_persist_evidence_crops runs only when the RESPONDING provider is the
selected multimodal one - never after an internal fallback to rule_based).

No network: the two provider-gating tests register fake providers in
ai_provider._PROVIDER_CLASSES and force them via request.provider, so nothing
here probes httpx or an LLM server.
"""

from pathlib import Path

import pytest
from PIL import Image

from seam_studio.schemas.ai import MaterialSuggestionResponse, SuggestMaterialsRequest
from seam_studio.schemas.scene import MeshRef, Prim, Scene, VisualBinding
from seam_studio.services import ai_provider
from seam_studio.services.ai_provider import (
    MaterialSuggestionProvider,
    _crop_to_uv_bbox,
    _persist_evidence_crops,
    extract_prim_texture_crops,
    suggest_materials,
)
from seam_studio.services.project_store import load_default_library


@pytest.fixture()
def library():
    return load_default_library()


def _project_with_texture(root: Path, rel: str = "visual/textures/t.png") -> Scene:
    """A mini project dir with one persisted texture and a scene whose single
    prim references it. mesh_ref points at a GLB that does NOT exist, so
    geometry resolution fails and the crop must come from the FILE source."""
    tex_path = root / rel
    tex_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (64, 64), (10, 150, 60)).save(tex_path)
    scene = Scene(
        scene_id="ev",
        prims=[
            Prim(
                id="/p1",
                name="p1",
                mesh_ref=MeshRef(asset_uri="visual/scene.glb", mesh_name="missing"),
                visual=VisualBinding(base_color_texture=rel),
            )
        ],
    )
    return scene


# --------------------------------------------------------------------------- #
# extract_prim_texture_crops: file source wins, works without a GLB           #
# --------------------------------------------------------------------------- #


def test_extract_crops_from_file_source_without_glb(tmp_path: Path):
    """With no GLB present, geometry resolution returns None; the crop must
    still be produced from the ORIGINAL persisted texture file (the preferred
    evidence source), encoded as a JPEG data URL."""
    scene = _project_with_texture(tmp_path)
    crops = extract_prim_texture_crops(tmp_path, scene, ["/p1"])

    assert len(crops) == 1
    assert crops[0]["prim_id"] == "/p1"
    assert crops[0]["data_url"].startswith("data:image/jpeg;base64,")


def test_extract_crops_traversal_guarded(tmp_path: Path):
    """A base_color_texture pointing outside the project dir yields no crop and
    raises no exception (the file source is rejected by the traversal guard and
    there is no GLB to fall back to)."""
    scene = _project_with_texture(tmp_path)
    scene.prims[0].visual.base_color_texture = "../../outside.png"

    crops = extract_prim_texture_crops(tmp_path, scene, ["/p1"])
    assert crops == []


# --------------------------------------------------------------------------- #
# _crop_to_uv_bbox: sub-window crops (V flipped), full [0,1]^2 no-ops         #
# --------------------------------------------------------------------------- #


class _FakeGeometry:
    """Minimal stand-in exposing ``.visual.uv`` like a trimesh geometry."""

    def __init__(self, uv):
        self.visual = type("V", (), {"uv": uv})()


def test_crop_to_uv_bbox_crops_sub_window():
    """UVs covering [0.2, 0.8]^2 on a 100x100 image crop to ~60x60. V is flipped
    (UV origin bottom-left, PIL top-left), so v in [0.2, 0.8] maps to pixel rows
    ~20..80 - a sub-window near the image center, not the whole atlas."""
    geometry = _FakeGeometry(
        [[0.2, 0.2], [0.8, 0.8], [0.2, 0.8], [0.8, 0.2]]
    )
    image = Image.new("RGB", (100, 100), (0, 0, 0))

    cropped = _crop_to_uv_bbox(geometry, image)

    width, height = cropped.size
    # ~60x60 (int() truncation may leave a 1px slack on either axis).
    assert 58 <= width <= 62
    assert 58 <= height <= 62
    # Strictly smaller than the source atlas: a real crop happened.
    assert (width, height) != image.size


def test_crop_to_uv_bbox_full_uv_is_noop():
    """A fully-unwrapped [0, 1]^2 mesh uses (nearly) the whole texture, so the
    image is returned UNCHANGED (same object, no crop)."""
    geometry = _FakeGeometry(
        [[0.0, 0.0], [1.0, 1.0], [0.0, 1.0], [1.0, 0.0]]
    )
    image = Image.new("RGB", (100, 100), (0, 0, 0))

    assert _crop_to_uv_bbox(geometry, image) is image


# --------------------------------------------------------------------------- #
# _persist_evidence_crops + gating in suggest_materials                       #
# --------------------------------------------------------------------------- #


def test_persist_evidence_crops_writes_files(tmp_path: Path):
    """_persist_evidence_crops writes each crop under ai/evidence/<batch>/ and
    returns EvidenceImage refs whose asset_path resolves to the written file."""
    crops = [
        {
            "prim_id": "/buildings/b01/window_12",
            "data_url": (
                "data:image/jpeg;base64,"
                "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsL"
            ),
        }
    ]
    refs = _persist_evidence_crops(tmp_path, crops)

    assert refs is not None and len(refs) == 1
    ref = refs[0]
    assert ref.prim_id == "/buildings/b01/window_12"
    assert ref.asset_path.startswith("ai/evidence/")
    assert ref.asset_path.endswith(".jpg")
    assert (tmp_path / ref.asset_path).is_file()


class _FakeVLMProvider(MaterialSuggestionProvider):
    """A multimodal provider that answers AS ITSELF (no internal fallback)."""

    name = "fake_vlm"
    multimodal = True

    def is_available(self) -> bool:
        return True

    def suggest(self, scene, library, prim_ids, screenshot=None, screenshots=None, texture_crops=None, model=None):
        return MaterialSuggestionResponse(
            suggestions=[], provider=self.name, model="fake", prompt_version="v2"
        )


class _FallbackVLMProvider(MaterialSuggestionProvider):
    """A multimodal provider that INTERNALLY falls back: it responds with
    provider='rule_based' while its own name differs (VLM timeout -> rules)."""

    name = "fake_fallback"
    multimodal = True

    def is_available(self) -> bool:
        return True

    def suggest(self, scene, library, prim_ids, screenshot=None, screenshots=None, texture_crops=None, model=None):
        return MaterialSuggestionResponse(
            suggestions=[], provider="rule_based", model=None, prompt_version="v2"
        )


@pytest.fixture()
def _fake_providers(monkeypatch):
    """Register the two fake providers in the provider registry for the test."""
    registry = dict(ai_provider._PROVIDER_CLASSES)
    registry[_FakeVLMProvider.name] = _FakeVLMProvider
    registry[_FallbackVLMProvider.name] = _FallbackVLMProvider
    monkeypatch.setattr(ai_provider, "_PROVIDER_CLASSES", registry)


def test_suggest_materials_sets_evidence_when_provider_answers(
    tmp_path: Path, library, _fake_providers
):
    """When the selected multimodal provider answers as itself, the crops it
    saw are persisted under ai/evidence/ and referenced on the response."""
    scene = _project_with_texture(tmp_path)
    request = SuggestMaterialsRequest(
        prim_ids=["/p1"], provider="fake_vlm", attach_texture_crops=True
    )

    response = suggest_materials(scene, library, request, project_dir=tmp_path)

    assert response.provider == "fake_vlm"
    assert response.evidence_images is not None
    assert len(response.evidence_images) == 1
    ref = response.evidence_images[0]
    assert ref.prim_id == "/p1"
    assert (tmp_path / ref.asset_path).is_file()


def test_suggest_materials_no_evidence_after_internal_fallback(
    tmp_path: Path, library, _fake_providers
):
    """When the selected provider internally falls back (responds as
    rule_based), the crops were never consumed - evidence_images stays None so
    the response makes no dishonest evidence claim."""
    scene = _project_with_texture(tmp_path)
    request = SuggestMaterialsRequest(
        prim_ids=["/p1"], provider="fake_fallback", attach_texture_crops=True
    )

    response = suggest_materials(scene, library, request, project_dir=tmp_path)

    assert response.provider == "rule_based"
    assert response.evidence_images is None
