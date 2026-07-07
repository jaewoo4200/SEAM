"""Service-level material-segmentation unit tests.

Covers the pure pipeline pieces (no HTTP, no VLM): UV-centroid face sampling
with v-flip, the color heuristic bootstrap rules, the strict per-material mesh
partition, and user-mask validation. The GLB rewrite / apply-undo orchestration
is exercised end-to-end through the API in test_segmentation_api.py.
"""

import io

import numpy as np
import pytest
import trimesh
from PIL import Image

from app.services import material_segmentation as seg
from app.services.material_segmentation import (
    SegmentationError,
    assign_face_materials,
    build_color_heuristic_mask,
    load_user_mask,
    split_by_face_material,
)


# --------------------------------------------------------- assign_face_materials


def _two_face_quad_uv(v_top: float, v_bot: float):
    """Two independent triangles (no shared vertices) whose UV centroids are
    exactly (0.5, v_top) for face 0 and (0.5, v_bot) for face 1.

    Each triangle's three vertices carry the SAME uv (0.5, v_*), so the mean
    of the 3 vertex UVs == that uv - isolating the v-sampling row from any u or
    geometry variation, and keeping the two faces fully independent.
    """
    verts = np.array(
        [
            [0.0, 0.0, 0.0], [1.0, 0.0, 0.0], [0.0, 1.0, 0.0],  # face 0
            [0.0, 0.0, 1.0], [1.0, 0.0, 1.0], [0.0, 1.0, 1.0],  # face 1
        ],
        dtype=np.float64,
    )
    faces = np.array([[0, 1, 2], [3, 4, 5]], dtype=np.int64)
    uv = np.array(
        [
            [0.5, v_top], [0.5, v_top], [0.5, v_top],  # face 0 -> centroid v_top
            [0.5, v_bot], [0.5, v_bot], [0.5, v_bot],  # face 1 -> centroid v_bot
        ],
        dtype=np.float64,
    )
    mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    mesh.visual = trimesh.visual.texture.TextureVisuals(uv=uv)
    return mesh


def test_assign_face_materials_flip_v_samples_upper_quarter_row():
    # 8x8 mask: top 2 rows (y=0,1) = id 3, the rest = id 1. A UV centroid at
    # v=0.75 with flip_v maps to y = (1 - 0.75) * (8 - 1) = 1.75 -> round 2...
    # so v=0.875 lands on row 1 (upper quarter). Use v=0.90 for a crisp upper-
    # row sample and v=0.10 for a crisp lower-row sample.
    labels = np.full((8, 8), 1, dtype=np.uint8)
    labels[0:2, :] = 3  # top two rows (small y) are metal
    mesh = _two_face_quad_uv(v_top=0.90, v_bot=0.10)

    out = assign_face_materials(mesh, labels, flip_v=True)
    # Face 0 (v=0.90): y = (1-0.90)*7 = 0.7 -> round 1 -> top rows -> id 3.
    assert out[0] == 3
    # Face 1 (v=0.10): y = (1-0.10)*7 = 6.3 -> round 6 -> bottom -> id 1.
    assert out[1] == 1


def test_assign_face_materials_no_flip_reverses_the_row():
    # Same mask, flip_v=False: v is used directly as the row fraction, so the
    # high-v face now samples the BOTTOM and the low-v face the TOP.
    labels = np.full((8, 8), 1, dtype=np.uint8)
    labels[0:2, :] = 3
    mesh = _two_face_quad_uv(v_top=0.90, v_bot=0.10)

    out = assign_face_materials(mesh, labels, flip_v=False)
    # Face 0 (v=0.90): y = 0.90*7 = 6.3 -> round 6 -> bottom -> id 1.
    assert out[0] == 1
    # Face 1 (v=0.10): y = 0.10*7 = 0.7 -> round 1 -> top rows -> id 3.
    assert out[1] == 3


def test_assign_face_materials_v075_flip_samples_upper_row():
    # The prompt's specific case: v=0.75 with flip_v samples an UPPER row of
    # the mask; with flip_v=False it samples a LOWER row. Split the 8-row mask
    # into top half = metal(3), bottom half = ground(4).
    #   flip:   y = round((1 - 0.75) * 7) = round(1.75) = 2 -> top half -> 3
    #   noflip: y = round(0.75 * 7)       = round(5.25) = 5 -> bottom half -> 4
    labels = np.empty((8, 8), dtype=np.uint8)
    labels[0:4, :] = 3  # top half metal
    labels[4:8, :] = 4  # bottom half ground
    mesh = _two_face_quad_uv(v_top=0.75, v_bot=0.75)

    flipped = assign_face_materials(mesh, labels, flip_v=True)
    plain = assign_face_materials(mesh, labels, flip_v=False)
    assert flipped[0] == 3   # upper region row
    assert plain[0] == 4     # lower region row


def test_assign_face_materials_requires_uv():
    # A mesh with no per-vertex UV raises a SegmentationError (400-mapped).
    mesh = trimesh.creation.box(extents=(1.0, 1.0, 1.0))
    mesh.visual = trimesh.visual.ColorVisuals()  # no .uv
    labels = np.ones((4, 4), dtype=np.uint8)
    with pytest.raises(SegmentationError):
        assign_face_materials(mesh, labels)


# ----------------------------------------------------- build_color_heuristic_mask


def test_heuristic_bright_grey_is_metal():
    # Bright, near-neutral grey: brightness > 165 and saturation < 0.18 -> the
    # metal rule (id 3) wins over the concrete default.
    img = Image.new("RGB", (4, 4), (200, 200, 200))
    labels = build_color_heuristic_mask(img)
    assert (labels == 3).all()


def test_heuristic_very_dark_is_unknown():
    # brightness < 18 is forced to unknown (id 0), the last rule, overriding
    # everything including the dark-glass branch.
    img = Image.new("RGB", (4, 4), (5, 5, 5))
    labels = build_color_heuristic_mask(img)
    assert (labels == 0).all()


def test_heuristic_ground_rule_wins_over_concrete_default():
    # A warm greenish pixel (r > g*1.08 ... or g-dominant) that is NOT dark,
    # bright-grey, or blue: the ground rule (id 4) wins over the concrete
    # default (id 1). This is the "later rule wins" ordering: ground is applied
    # after the concrete base and before the very-dark override.
    # (110, 90, 70): r=110 > g*1.08=97.2 and g=90 > b*1.05=73.5, brightness=90
    # > 70 -> ground branch 1.
    img = Image.new("RGB", (4, 4), (110, 90, 70))
    labels = build_color_heuristic_mask(img)
    assert (labels == 4).all()
    # Sanity: it did NOT stay the concrete default.
    assert not (labels == 1).any()


def test_heuristic_neutral_midtone_stays_concrete_default():
    # A neutral mid-grey that trips none of glass/metal/ground/unknown stays
    # the concrete default (id 1): brightness ~128 (not > 165, not < 85/18),
    # saturation 0 (fails metal's need for brightness>165), no colour cast.
    img = Image.new("RGB", (4, 4), (128, 128, 128))
    labels = build_color_heuristic_mask(img)
    assert (labels == 1).all()


# --------------------------------------------------------- split_by_face_material


def _uv_box_with_texture():
    """A textured UV box: 12 faces, per-vertex UV, a PBR material carrying a
    small image (so we can assert the texture rides along to each split)."""
    box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
    rng = np.random.default_rng(7)
    uv = rng.random((len(box.vertices), 2))
    img = Image.new("RGB", (8, 8), (123, 45, 67))
    material = trimesh.visual.material.PBRMaterial(baseColorTexture=img)
    box.visual = trimesh.visual.texture.TextureVisuals(uv=uv, material=material)
    return box


def test_split_partitions_faces_and_reindexes_vertices():
    mesh = _uv_box_with_texture()
    n_faces = len(mesh.faces)
    # Assign half the faces to concrete(1) and half to metal(3).
    face_mats = np.array(
        [1 if i < n_faces // 2 else 3 for i in range(n_faces)], dtype=np.uint8
    )
    out = split_by_face_material(mesh, face_mats)

    assert set(out) == {"concrete", "metal"}
    # Strict partition: the split face counts sum back to the original.
    assert sum(len(sub.faces) for sub in out.values()) == n_faces
    assert len(out["concrete"].faces) == n_faces // 2
    assert len(out["metal"].faces) == n_faces - n_faces // 2

    for name, sub in out.items():
        # Vertices re-indexed to only those used by this sub-mesh's faces.
        assert len(sub.vertices) <= len(mesh.vertices)
        assert int(sub.faces.max()) == len(sub.vertices) - 1
        # UV subset carried (one uv per sub vertex).
        assert sub.visual.uv is not None
        assert len(sub.visual.uv) == len(sub.vertices)
        # Texture material preserved (same object identity, so the photo atlas
        # is kept for the split prim's viewer render / AI crops).
        assert sub.visual.material is mesh.visual.material


def test_split_single_material_yields_one_submesh():
    # Every face one class -> a single sub-mesh (the API turns <2 into a 400).
    mesh = _uv_box_with_texture()
    face_mats = np.full(len(mesh.faces), 2, dtype=np.uint8)  # all glass
    out = split_by_face_material(mesh, face_mats)
    assert set(out) == {"glass"}
    assert len(out["glass"].faces) == len(mesh.faces)


# ------------------------------------------------------------------ load_user_mask


def _png_bytes(arr: np.ndarray) -> bytes:
    buf = io.BytesIO()
    Image.fromarray(arr, mode="L").save(buf, format="PNG")
    return buf.getvalue()


def test_load_user_mask_wrong_size_reports_both_sizes():
    # expected_hw is (H, W). Mask is 6x4 (H=6,W=4); expect 8x8. The message
    # must quote BOTH the mask size and the atlas size (as WxH).
    mask = np.ones((6, 4), dtype=np.uint8)
    with pytest.raises(SegmentationError) as ei:
        load_user_mask(_png_bytes(mask), (8, 8))
    msg = str(ei.value)
    assert "4x6" in msg      # mask reported as WxH
    assert "8x8" in msg      # atlas reported as WxH


def test_load_user_mask_unknown_ids_listed():
    # Valid ids are 0..4. A mask carrying 7 and 9 must be rejected, listing the
    # offending ids.
    mask = np.array([[0, 1, 7], [2, 9, 3]], dtype=np.uint8)
    with pytest.raises(SegmentationError) as ei:
        load_user_mask(_png_bytes(mask), (2, 3))
    msg = str(ei.value)
    assert "7" in msg and "9" in msg
    assert "unknown material ids" in msg


def test_load_user_mask_valid_roundtrips():
    # A correctly sized L-mode mask of allowed ids loads to the same array.
    mask = np.array([[0, 1, 2], [3, 4, 1]], dtype=np.uint8)
    out = load_user_mask(_png_bytes(mask), (2, 3))
    assert np.array_equal(np.asarray(out), mask)


def test_load_user_mask_not_an_image():
    # Arbitrary bytes that do not decode as an image -> SegmentationError.
    with pytest.raises(SegmentationError) as ei:
        load_user_mask(b"this is definitely not a png", (8, 8))
    assert "not a readable image" in str(ei.value)
