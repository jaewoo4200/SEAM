"""Terrain drape (snap_to_terrain) with interior gap-fill.

The drape raycasts straight down onto the visual mesh to put each waypoint's z
on the surface + a constant height. ``fill_gaps`` (default True) interpolates
the surface z of an INTERIOR run of misses (a hole in the footprint bracketed
by hits) between its draped neighbors; END misses (before the first / after the
last hit) always keep their original z and add one summary warning.

trimesh's pure-python raycaster needs rtree's native spatial index, so the
raycasting tests skip when that isn't functional (mirrors test_scene_bounds).
"""

from pathlib import Path

import pytest
import trimesh

from seam_studio.schemas.scene import Scene
from seam_studio.services.terrain import snap_to_terrain


def _box_glb(path: Path, boxes: list[tuple[tuple[float, float, float], tuple[float, float, float]]]) -> None:
    """Write a GLB of axis-aligned boxes to ``path``.

    ``boxes`` is a list of (extents, center) pairs. A box with extents
    (l, w, h) centered at (cx, cy, cz) has its top face at z = cz + h/2.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tm = trimesh.Scene()
    for i, (extents, center) in enumerate(boxes):
        box = trimesh.creation.box(extents=extents)
        box.apply_translation(center)
        name = f"box{i}"
        tm.add_geometry(box, geom_name=name, node_name=name)
    path.write_bytes(tm.export(file_type="glb"))


def _terrain_ray_available() -> bool:
    """True when trimesh can actually raycast (rtree native index loads)."""
    try:
        import numpy as np

        box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
        box.ray.intersects_location(
            ray_origins=np.array([[0.0, 0.0, 5.0]]),
            ray_directions=np.array([[0.0, 0.0, -1.0]]),
        )
        return True
    except Exception:  # noqa: BLE001 - any import/native failure => skip
        return False


ray_only = pytest.mark.skipif(
    not _terrain_ray_available(),
    reason="trimesh raycast unavailable (rtree native index not loadable)",
)


def _two_box_scene(tmp_path: Path) -> Scene:
    """Two 4x20x4 boxes (top at z=2) with footprints x in [-12,-8] and
    [8, 12]; the strip x in (-8, 8) between them is an empty GAP. y spans
    [-10, 10] over both. Nothing exists outside x in [-12, 12]."""
    _box_glb(
        tmp_path / "visual" / "scene.glb",
        boxes=[
            ((4.0, 20.0, 4.0), (-10.0, 0.0, 0.0)),  # left box:  x in [-12, -8]
            ((4.0, 20.0, 4.0), (10.0, 0.0, 0.0)),   # right box: x in [8, 12]
        ],
    )
    return Scene(scene_id="t", name="t")


# -------------------------------------------------------- basic drape (a)


@ray_only
def test_points_over_boxes_snap_to_top_plus_height(tmp_path: Path):
    # Box tops sit at z = 2; with a 1.5 m height each point over a box lands
    # at 2 + 1.5 = 3.5 regardless of its incoming z.
    scene = _two_box_scene(tmp_path)
    warnings: list[str] = []
    over_left = [-10.0, 0.0, 50.0]
    over_right = [10.0, 0.0, -3.0]
    out = snap_to_terrain(tmp_path, scene, [over_left, over_right], 1.5, warnings)

    assert out[0] == pytest.approx([-10.0, 0.0, 3.5])
    assert out[1] == pytest.approx([10.0, 0.0, 3.5])
    assert warnings == []


# --------------------------------------------- interior gap fill (b) / (c)


@ray_only
def test_interior_gap_point_interpolates_with_fill(tmp_path: Path):
    # A point over the empty strip between the two boxes is an INTERIOR miss
    # (bracketed by hits on both boxes). With fill_gaps=True its surface z is
    # interpolated from the bracketing hits (both surfaces at z=2), so the
    # output z = 2 + 1.5 = 3.5 - NOT its original z of 99.
    scene = _two_box_scene(tmp_path)
    warnings: list[str] = []
    left = [-10.0, 0.0, 0.0]
    gap = [0.0, 0.0, 99.0]  # over the empty strip; no surface underneath
    right = [10.0, 0.0, 0.0]
    out = snap_to_terrain(tmp_path, scene, [left, gap, right], 1.5, warnings)

    assert out[0] == pytest.approx([-10.0, 0.0, 3.5])
    assert out[2] == pytest.approx([10.0, 0.0, 3.5])
    # Interior hole filled: z is the interpolated surface (2.0) + height, and
    # crucially differs from the point's raw z (99.0).
    assert out[1][0] == pytest.approx(0.0)
    assert out[1][2] == pytest.approx(3.5)
    assert out[1][2] != pytest.approx(99.0)
    # A filled interior hole is not an unresolved miss -> no warning.
    assert warnings == []


@ray_only
def test_interior_gap_interpolates_between_unequal_neighbors(tmp_path: Path):
    # Two boxes of DIFFERENT heights so the interpolation is non-trivial: the
    # left box top is z=2, the right box top is z=6. A gap point exactly
    # halfway (in index) between the bracketing hits gets the mean surface z=4.
    _box_glb(
        tmp_path / "visual" / "scene.glb",
        boxes=[
            ((4.0, 20.0, 4.0), (-10.0, 0.0, 0.0)),   # top at z=2
            ((4.0, 20.0, 12.0), (10.0, 0.0, 0.0)),   # top at z=6
        ],
    )
    scene = Scene(scene_id="t2", name="t2")
    warnings: list[str] = []
    left = [-10.0, 0.0, 0.0]
    gap = [0.0, 0.0, 99.0]
    right = [10.0, 0.0, 0.0]
    out = snap_to_terrain(tmp_path, scene, [left, gap, right], 1.0, warnings)

    assert out[0][2] == pytest.approx(3.0)   # 2 + 1
    assert out[2][2] == pytest.approx(7.0)   # 6 + 1
    # Halfway between the hit indices (0 and 2) => surface (2+6)/2 = 4, + 1.
    assert out[1][2] == pytest.approx(5.0)
    assert warnings == []


@ray_only
def test_interior_gap_point_keeps_z_without_fill(tmp_path: Path):
    # The SAME interior-gap point with fill_gaps=False keeps its original z
    # (the raw chord z across the hole) and is counted as a miss -> one warning.
    scene = _two_box_scene(tmp_path)
    warnings: list[str] = []
    left = [-10.0, 0.0, 0.0]
    gap = [0.0, 0.0, 99.0]
    right = [10.0, 0.0, 0.0]
    out = snap_to_terrain(
        tmp_path, scene, [left, gap, right], 1.5, warnings, fill_gaps=False
    )

    assert out[0] == pytest.approx([-10.0, 0.0, 3.5])
    assert out[2] == pytest.approx([10.0, 0.0, 3.5])
    # Gap point unchanged: original z kept, not interpolated.
    assert out[1] == pytest.approx([0.0, 0.0, 99.0])
    assert len(warnings) == 1
    assert "no surface underneath" in warnings[0]
    assert "1/3" in warnings[0]


# ------------------------------------------------ leading/trailing misses (d)


@ray_only
def test_leading_and_trailing_misses_keep_z_and_one_warning(tmp_path: Path):
    # Points outside the whole footprint at either END keep their z even with
    # fill_gaps=True (gap-fill only spans INTERIOR holes bracketed by hits).
    # One summary warning mentions the miss/total counts.
    scene = _two_box_scene(tmp_path)
    warnings: list[str] = []
    before = [-40.0, 0.0, 5.0]   # left of both boxes (leading miss)
    left = [-10.0, 0.0, 0.0]     # over left box (hit)
    right = [10.0, 0.0, 0.0]     # over right box (hit)
    after = [40.0, 0.0, 8.0]     # right of both boxes (trailing miss)
    out = snap_to_terrain(
        tmp_path, scene, [before, left, right, after], 1.5, warnings, fill_gaps=True
    )

    # End misses keep their original z.
    assert out[0] == pytest.approx([-40.0, 0.0, 5.0])
    assert out[3] == pytest.approx([40.0, 0.0, 8.0])
    # Bracketing hits still drape.
    assert out[1] == pytest.approx([-10.0, 0.0, 3.5])
    assert out[2] == pytest.approx([10.0, 0.0, 3.5])
    # Exactly one summary warning, naming the 2/4 miss count.
    assert len(warnings) == 1
    assert "no surface underneath" in warnings[0]
    assert "2/4" in warnings[0]


@ray_only
def test_leading_gap_and_trailing_gap_around_interior_fill(tmp_path: Path):
    # Combined: a leading end-miss, an interior gap (filled), and a trailing
    # end-miss. The interior hole is interpolated; only the two END misses are
    # counted in the single warning.
    scene = _two_box_scene(tmp_path)
    warnings: list[str] = []
    pts = [
        [-40.0, 0.0, 5.0],   # 0 leading miss  -> z kept
        [-10.0, 0.0, 0.0],   # 1 hit
        [0.0, 0.0, 99.0],    # 2 interior gap  -> interpolated
        [10.0, 0.0, 0.0],    # 3 hit
        [40.0, 0.0, 8.0],    # 4 trailing miss -> z kept
    ]
    out = snap_to_terrain(tmp_path, scene, pts, 1.5, warnings, fill_gaps=True)

    assert out[0][2] == pytest.approx(5.0)    # end miss kept
    assert out[1][2] == pytest.approx(3.5)    # hit
    assert out[2][2] == pytest.approx(3.5)    # interior filled (surface 2 + 1.5)
    assert out[3][2] == pytest.approx(3.5)    # hit
    assert out[4][2] == pytest.approx(8.0)    # end miss kept
    # Only the two end misses are reported (the filled interior is resolved).
    assert len(warnings) == 1
    assert "2/5" in warnings[0]


# --------------------------------------------------------- no-mesh short path


def test_no_mesh_keeps_z_and_warns(tmp_path: Path):
    # No visual mesh: returns before any raycast (needs no ray engine), points
    # unchanged, one warning. fill_gaps is irrelevant here.
    scene = Scene(scene_id="nm", name="nm")
    warnings: list[str] = []
    out = snap_to_terrain(tmp_path, scene, [[1.0, 2.0, 9.0]], 1.5, warnings)
    assert out == [[1.0, 2.0, 9.0]]
    assert len(warnings) == 1
    assert "no visual mesh" in warnings[0]
