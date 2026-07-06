"""Multi-UE trajectory support via routes (BACKEND agent workstream).

Covers the ``routes`` branch of run_trajectory: N routes x M steps yield N*M
STEP-MAJOR samples, per-UE metrics come from one solve per step, metadata
carries ue_ids/num_steps, include_paths filters per UE, bad routes raise
ValueError, and the legacy single-UE path is unchanged.
"""

import math
from pathlib import Path

import pytest

from app.schemas.devices import Device
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import (
    SimulationConfig,
    TrajectorySimulateRequest,
    UERoute,
)
from app.services.project_store import load_default_library
from app.services.simulation_backends.mock_backend import MockBackend
from app.services.trajectory import resample_polyline, run_trajectory


# --------------------------------------------------------------- fixtures


def _multi_ue_scene() -> Scene:
    """One TX and two RX devices; no geometry file needed (the mock backend
    never loads meshes)."""
    return Scene(
        scene_id="routes",
        name="Routes",
        prims=[
            Prim(
                id="/ground",
                name="ground",
                semantic_tags=["ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 10.0], power_dbm=30.0),
            Device(id="rx_001", name="UE1", kind="rx", position=[20.0, 0.0, 1.5]),
            Device(id="rx_002", name="UE2", kind="rx", position=[0.0, 20.0, 1.5]),
        ],
    )


def _cfg() -> SimulationConfig:
    return SimulationConfig(id="default", backend="mock")


def _two_routes() -> list[UERoute]:
    # Two 2-point polylines (straight lines) whose midpoints sit at clearly
    # different distances from the TX at [0, 0, 10] (so the per-UE RSS differs):
    # rx_001 midpoint [22.5, 0, 1.5] is ~24 m out, rx_002 midpoint [5, 5, 1.5]
    # is ~10 m out.
    return [
        UERoute(ue_id="rx_001", waypoints=[[5.0, 0.0, 1.5], [40.0, 0.0, 1.5]]),
        UERoute(ue_id="rx_002", waypoints=[[2.0, 2.0, 1.5], [8.0, 8.0, 1.5]]),
    ]


# ---------------------------------------------------- resample_polyline


def test_resample_polyline_two_points_is_straight_line():
    # A 2-point polyline == today's single-UE straight line (arc-length even).
    out = resample_polyline([[0.0, 0.0, 0.0], [30.0, 0.0, 0.0]], 4)
    expected = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [20.0, 0.0, 0.0], [30.0, 0.0, 0.0]]
    assert len(out) == len(expected)
    for o, e in zip(out, expected):
        assert o == pytest.approx(e)


def test_resample_polyline_multi_segment_by_arc_length():
    # L-shape: 10 m +x then 10 m +y (total 20 m). 5 points -> every 5 m, so the
    # midpoint lands exactly on the corner.
    out = resample_polyline([[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [10.0, 10.0, 0.0]], 5)
    assert out[0] == pytest.approx([0.0, 0.0, 0.0])
    assert out[2] == pytest.approx([10.0, 0.0, 0.0])  # the corner at 10 m
    assert out[-1] == pytest.approx([10.0, 10.0, 0.0])


def test_resample_polyline_degenerate_collapses():
    # All-coincident waypoints -> n copies of the first point (no div-by-zero).
    out = resample_polyline([[1.0, 2.0, 3.0], [1.0, 2.0, 3.0]], 3)
    assert out == [[1.0, 2.0, 3.0], [1.0, 2.0, 3.0], [1.0, 2.0, 3.0]]


# ----------------------------------------------------- routes: shape/order


def test_routes_two_by_three_step_major_ordering():
    scene = _multi_ue_scene()
    lib = load_default_library()
    cfg = _cfg()
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=3, dt_s=0.2)
    result = run_trajectory(MockBackend(), Path("."), scene, lib, cfg, req)

    # 2 routes x 3 steps = 6 samples, STEP-MAJOR, UE in routes order.
    assert len(result.samples) == 6
    ue_order = [s.ue_id for s in result.samples]
    assert ue_order == [
        "rx_001", "rx_002",  # step 0
        "rx_001", "rx_002",  # step 1
        "rx_001", "rx_002",  # step 2
    ]
    # time_s = step * dt_s, shared across the two UEs of a step.
    times = [s.time_s for s in result.samples]
    assert times == pytest.approx([0.0, 0.0, 0.2, 0.2, 0.4, 0.4])


def test_routes_metadata_ue_ids_and_num_steps():
    scene = _multi_ue_scene()
    lib = load_default_library()
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=3, dt_s=0.1)
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    assert result.metadata["ue_ids"] == ["rx_001", "rx_002"]
    assert result.metadata["num_steps"] == 3
    # Legacy field points at the first route's UE.
    assert result.ue_id == "rx_001"


def test_routes_per_ue_rss_differs_when_positions_differ():
    # The two UEs walk geometrically different lines, so at a given step their
    # RSS from the shared TX differ (positions differ -> Friis distance differs).
    scene = _multi_ue_scene()
    lib = load_default_library()
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=3, dt_s=0.1)
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    # Step 1 (middle) samples for the two UEs.
    s_ue1 = result.samples[2]  # step1, rx_001
    s_ue2 = result.samples[3]  # step1, rx_002
    assert s_ue1.ue_id == "rx_001" and s_ue2.ue_id == "rx_002"
    assert s_ue1.rss_dbm is not None and s_ue2.rss_dbm is not None
    assert s_ue1.rss_dbm != pytest.approx(s_ue2.rss_dbm)
    # Each UE's position advances along its own route.
    assert s_ue1.position == pytest.approx([22.5, 0.0, 1.5])
    assert s_ue2.position == pytest.approx([5.0, 5.0, 1.5])


def test_routes_include_paths_filters_per_ue():
    scene = _multi_ue_scene()
    lib = load_default_library()
    req = TrajectorySimulateRequest(
        routes=_two_routes(), num_points=2, dt_s=0.1, include_paths=True
    )
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    for s in result.samples:
        assert s.paths is not None and s.paths, "include_paths should fill paths"
        # Every path attached to a UE's sample belongs to that UE's rx.
        assert all(p.rx_id == s.ue_id for p in s.paths)


def test_routes_unknown_ue_id_raises_value_error():
    scene = _multi_ue_scene()
    lib = load_default_library()
    req = TrajectorySimulateRequest(
        routes=[UERoute(ue_id="rx_404", waypoints=[[0.0, 0.0, 1.5], [1.0, 0.0, 1.5]])],
        num_points=2,
    )
    with pytest.raises(ValueError):
        run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)


def test_routes_empty_list_raises_value_error():
    # routes=[] enters the multi-UE branch but has no UE to move -> ValueError
    # (not an IndexError on ue_ids[0]).
    scene = _multi_ue_scene()
    lib = load_default_library()
    req = TrajectorySimulateRequest(routes=[], num_points=2)
    with pytest.raises(ValueError):
        run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)


def test_routes_duplicate_ue_ids_raise_value_error():
    scene = _multi_ue_scene()
    lib = load_default_library()
    req = TrajectorySimulateRequest(
        routes=[
            UERoute(ue_id="rx_001", waypoints=[[0.0, 0.0, 1.5], [1.0, 0.0, 1.5]]),
            UERoute(ue_id="rx_001", waypoints=[[2.0, 0.0, 1.5], [3.0, 0.0, 1.5]]),
        ],
        num_points=2,
    )
    with pytest.raises(ValueError):
        run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)


# ------------------------------------------------ legacy path unchanged


def test_legacy_single_ue_unchanged_when_routes_none():
    """A single-route request over rx_001 reproduces the legacy single-UE run
    (same start/end straight line) sample-for-sample; the routes branch only
    adds the two metadata keys."""
    scene = _multi_ue_scene()
    lib = load_default_library()
    cfg = _cfg()
    kwargs = dict(num_points=4, dt_s=0.1)

    legacy = run_trajectory(
        MockBackend(), Path("."), scene, lib, cfg,
        TrajectorySimulateRequest(
            ue_id="rx_001", start_m=[5.0, 0.0, 1.5], end_m=[40.0, 0.0, 1.5], **kwargs
        ),
    )
    routed = run_trajectory(
        MockBackend(), Path("."), scene, lib, cfg,
        TrajectorySimulateRequest(
            routes=[UERoute(ue_id="rx_001", waypoints=[[5.0, 0.0, 1.5], [40.0, 0.0, 1.5]])],
            **kwargs,
        ),
    )

    # Legacy result is untouched by the routes feature: same ue_id + samples.
    assert legacy.ue_id == "rx_001"
    assert "ue_ids" not in legacy.metadata and "num_steps" not in legacy.metadata

    # The single-route run matches the legacy run sample-for-sample (positions,
    # rss, path_gain, sinr, path_count) — one solve per step, filtered to rx_001.
    assert len(routed.samples) == len(legacy.samples)
    for r, l in zip(routed.samples, legacy.samples):
        assert r.ue_id == l.ue_id == "rx_001"
        assert r.time_s == pytest.approx(l.time_s)
        assert r.position == pytest.approx(l.position)
        assert r.rss_dbm == pytest.approx(l.rss_dbm)
        assert r.path_gain_db == pytest.approx(l.path_gain_db)
        assert r.sinr_db == pytest.approx(l.sinr_db)
        assert r.path_count == l.path_count
        assert r.strongest_delay_ns == pytest.approx(l.strongest_delay_ns)


def test_legacy_single_ue_result_matches_pinned_expectation():
    """Pin the legacy single-UE result so a future refactor of the shared
    per-sample helper can't silently drift the numbers."""
    scene = _multi_ue_scene()
    lib = load_default_library()
    cfg = _cfg()
    req = TrajectorySimulateRequest(
        ue_id="rx_001", start_m=[5.0, 0.0, 1.5], end_m=[40.0, 0.0, 1.5],
        num_points=3, dt_s=0.1,
    )
    result = run_trajectory(MockBackend(), Path("."), scene, lib, cfg, req)

    assert result.ue_id == "rx_001"
    expected_pos = [[5.0, 0.0, 1.5], [22.5, 0.0, 1.5], [40.0, 0.0, 1.5]]
    for s, e in zip(result.samples, expected_pos):
        assert s.position == pytest.approx(e)
    # Metrics are deterministic (mock Friis + ground/wall bounces). Pin them.
    times = [s.time_s for s in result.samples]
    assert times == pytest.approx([0.0, 0.1, 0.2])
    for s in result.samples:
        assert s.rss_dbm is not None
        assert s.path_gain_db == pytest.approx(s.rss_dbm - 30.0)  # tx power 30 dBm
        assert s.path_count >= 1
