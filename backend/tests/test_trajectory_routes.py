"""Multi-UE trajectory support via routes (BACKEND agent workstream).

Covers the ``routes`` branch of run_trajectory: N routes x M steps yield N*M
STEP-MAJOR samples, per-UE metrics come from one solve per step, metadata
carries ue_ids/num_steps, include_paths filters per UE, bad routes raise
ValueError, and the legacy single-UE path is unchanged.

Also pins PARAMETER INHERITANCE (the step scenes move the SAME device, so each
routed UE keeps its antenna/power/name/orientation; only position+velocity
change, and velocity is a finite difference along the route) and the multi-UE
antenna caveat (sionna's scene-level rx_array honors only the first UE's
antenna -> a warning), plus the ue_ids-through-save/load round trip.
"""

import json
import math
from pathlib import Path

import pytest

from app.schemas.devices import Antenna, Device
from app.schemas.results import TrajectoryResultSet
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene
from app.schemas.simulation import (
    SimulationConfig,
    TrajectorySimulateRequest,
    UERoute,
)
from app.services.project_store import load_default_library
from app.services.simulation_backends.mock_backend import MockBackend
from app.services.trajectory import resample_polyline, run_trajectory


class _CapturingBackend(MockBackend):
    """MockBackend that records the (scene, config) it was solved with per step,
    so a test can inspect the step scenes' moved devices and the rx_ids passed."""

    def __init__(self):
        self.calls: list[tuple[Scene, SimulationConfig]] = []

    def simulate_paths(self, project_dir, scene, library, config):
        self.calls.append((scene, config))
        return super().simulate_paths(project_dir, scene, library, config)


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


# ------------------------------------------ include_static_rx (fixed UEs)


def test_routes_include_static_rx_adds_fixed_rx_each_step():
    """Routing only rx_001 with include_static_rx=True also solves the scene's
    other (un-routed) RX rx_002 at its fixed position every step: both UE ids
    appear per step (static appended after the routed UE), the routed UE moves,
    and the static UE stays parked at its scene position with unchanged metrics
    across steps."""
    scene = _multi_ue_scene()
    lib = load_default_library()
    route = [UERoute(ue_id="rx_001", waypoints=[[5.0, 0.0, 1.5], [40.0, 0.0, 1.5]])]
    req = TrajectorySimulateRequest(
        routes=route, num_points=3, dt_s=0.1, include_static_rx=True
    )
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    # 2 UEs (1 routed + 1 static) x 3 steps = 6 samples, step-major, the static
    # RX appended after the routed UE within each step.
    assert len(result.samples) == 6
    assert [s.ue_id for s in result.samples] == [
        "rx_001", "rx_002",  # step 0
        "rx_001", "rx_002",  # step 1
        "rx_001", "rx_002",  # step 2
    ]

    # rx_002 sits at its fixed scene position [0, 20, 1.5] at every step...
    scene_pos = scene.device_by_id("rx_002").position
    static = [s for s in result.samples if s.ue_id == "rx_002"]
    assert len(static) == 3
    for s in static:
        assert s.position == pytest.approx(scene_pos)
    # ...and, never moving relative to the TX, its metrics are constant.
    assert static[0].rss_dbm is not None
    for s in static[1:]:
        assert s.rss_dbm == pytest.approx(static[0].rss_dbm)
        assert s.path_count == static[0].path_count

    # rx_001 actually advances along its route (endpoints match).
    routed_pos = [s.position for s in result.samples if s.ue_id == "rx_001"]
    assert routed_pos[0] == pytest.approx([5.0, 0.0, 1.5])
    assert routed_pos[-1] == pytest.approx([40.0, 0.0, 1.5])
    assert routed_pos[0] != pytest.approx(routed_pos[-1])

    # Metadata lists both UEs (within-step order) and marks rx_002 as static.
    assert result.metadata["ue_ids"] == ["rx_001", "rx_002"]
    assert result.metadata["static_rx_ids"] == ["rx_002"]
    assert result.metadata["num_steps"] == 3


def test_routes_include_static_rx_default_false_excludes_fixed_rx():
    """Default (include_static_rx False): a single-route request over rx_001
    yields ONLY rx_001 samples — the un-routed rx_002 is absent, no
    static_rx_ids metadata — and the routed UE's per-step metrics are identical
    to the include_static_rx=True run (RXs don't interfere with each other, so
    riding a static RX along doesn't double-count into the routed UE)."""
    scene = _multi_ue_scene()
    lib = load_default_library()
    route = [UERoute(ue_id="rx_001", waypoints=[[5.0, 0.0, 1.5], [40.0, 0.0, 1.5]])]

    off = run_trajectory(
        MockBackend(), Path("."), scene, lib, _cfg(),
        TrajectorySimulateRequest(routes=route, num_points=3, dt_s=0.1),
    )
    on = run_trajectory(
        MockBackend(), Path("."), scene, lib, _cfg(),
        TrajectorySimulateRequest(
            routes=route, num_points=3, dt_s=0.1, include_static_rx=True
        ),
    )

    # Default: only the routed UE appears; no static_rx_ids key emitted.
    assert all(s.ue_id == "rx_001" for s in off.samples)
    assert len(off.samples) == 3
    assert off.metadata["ue_ids"] == ["rx_001"]
    assert "static_rx_ids" not in off.metadata

    # The routed UE's samples are identical whether or not static RXs ride along.
    on_routed = [s for s in on.samples if s.ue_id == "rx_001"]
    assert len(on_routed) == len(off.samples)
    for a, b in zip(off.samples, on_routed):
        assert a.position == pytest.approx(b.position)
        assert a.rss_dbm == pytest.approx(b.rss_dbm)
        assert a.sinr_db == pytest.approx(b.sinr_db)
        assert a.interference_dbm == b.interference_dbm  # both None (single TX)
        assert a.path_count == b.path_count


def test_routes_include_static_rx_honors_config_rx_filter():
    """include_static_rx only picks up un-routed RXs that pass the config's rx
    filter: with config.rx_ids=['rx_001'] the un-routed rx_002 is filtered out,
    so no static sample is added even though include_static_rx is True."""
    scene = _multi_ue_scene()
    lib = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock", rx_ids=["rx_001"])
    req = TrajectorySimulateRequest(
        routes=[UERoute(ue_id="rx_001", waypoints=[[5.0, 0.0, 1.5], [40.0, 0.0, 1.5]])],
        num_points=2, dt_s=0.1, include_static_rx=True,
    )
    result = run_trajectory(MockBackend(), Path("."), scene, lib, cfg, req)

    assert all(s.ue_id == "rx_001" for s in result.samples)
    assert result.metadata["ue_ids"] == ["rx_001"]
    assert "static_rx_ids" not in result.metadata


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


# -------------------------------------------- A. parameter inheritance


def _distinctive_antenna_scene() -> Scene:
    """rx_001 carries a distinctive tr38901 4x2 VH array (non-default power,
    orientation, name); rx_002 keeps the default isotropic antenna. Used to
    prove the step scenes preserve every non-position field of the moved UE."""
    scene = _multi_ue_scene()
    for d in scene.devices:
        if d.id == "rx_001":
            d.antenna = Antenna(
                pattern="tr38901", polarization="VH", num_rows=4, num_cols=2,
                vertical_spacing=0.7, horizontal_spacing=0.5,
            )
            d.name = "Distinctive UE"
            d.orientation_deg = [30.0, 0.0, 0.0]
            d.power_dbm = 12.0
    return scene


def test_routes_step_scene_preserves_moved_device_identity():
    """Every step scene deep-copies the scene and moves the SAME rx device, so
    the moved UE keeps its antenna/power/name/orientation across all steps —
    only position and velocity change. Pins parameter inheritance (A1)."""
    scene = _distinctive_antenna_scene()
    orig = {d.id: d for d in scene.devices}
    lib = load_default_library()
    backend = _CapturingBackend()
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=3, dt_s=0.2)
    run_trajectory(backend, Path("."), scene, lib, _cfg(), req)

    assert len(backend.calls) == 3  # one solve per step
    for step_scene, step_cfg in backend.calls:
        # The solve is scoped to exactly the routed UEs.
        assert step_cfg.rx_ids == ["rx_001", "rx_002"]
        moved = {d.id: d for d in step_scene.devices if d.kind == "rx"}
        for uid in ("rx_001", "rx_002"):
            got, want = moved[uid], orig[uid]
            # Identity of every field EXCEPT position/velocity is preserved.
            assert got.antenna == want.antenna
            assert got.antenna.pattern == want.antenna.pattern
            assert got.antenna.num_rows == want.antenna.num_rows
            assert got.antenna.num_cols == want.antenna.num_cols
            assert got.antenna.polarization == want.antenna.polarization
            assert got.antenna.vertical_spacing == want.antenna.vertical_spacing
            assert got.name == want.name
            assert got.power_dbm == want.power_dbm
            assert got.orientation_deg == want.orientation_deg
            assert got.kind == want.kind
    # And the original scene's devices are untouched (deep copy, not in place).
    r1 = orig["rx_001"]
    assert r1.position == [20.0, 0.0, 1.5] and r1.velocity_m_s is None
    assert r1.antenna.pattern == "tr38901" and r1.antenna.num_rows == 4


def test_routes_step_scene_velocity_is_finite_difference_along_route():
    """Each moved UE's velocity in a step scene is the finite difference of its
    resampled route positions over dt (forward in the interior, backward at the
    last step); pins the moving-UE Doppler input (A1)."""
    scene = _distinctive_antenna_scene()
    lib = load_default_library()
    backend = _CapturingBackend()
    dt = 0.2
    # rx_001 walks a straight 35 m line over 3 steps => 17.5 m per step in +x,
    # so speed = 17.5 / dt on x, 0 on y/z, at every step (uniform spacing).
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=3, dt_s=dt)
    run_trajectory(backend, Path("."), scene, lib, _cfg(), req)

    per_step_dx = (40.0 - 5.0) / 2  # 3 points => 2 gaps of 17.5 m
    expected_vx = per_step_dx / dt
    for step_scene, _ in backend.calls:
        rx1 = next(d for d in step_scene.devices if d.id == "rx_001")
        assert rx1.velocity_m_s is not None
        vx, vy, vz = rx1.velocity_m_s
        assert vx == pytest.approx(expected_vx)
        assert vy == pytest.approx(0.0) and vz == pytest.approx(0.0)
        # Velocity is finite (never NaN/inf) — the finite-difference guard.
        assert all(math.isfinite(c) for c in rx1.velocity_m_s)


def test_routes_differing_antennas_emit_scene_level_array_warning():
    """When routed UEs carry non-identical antenna configs, the routes path
    warns that sionna's scene-level rx_array only honors the first UE (A2)."""
    scene = _distinctive_antenna_scene()  # rx_001 tr38901 4x2, rx_002 default
    lib = load_default_library()
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=2, dt_s=0.1)
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    warned = [w for w in result.warnings if "scene-level array" in w]
    assert len(warned) == 1, result.warnings
    msg = warned[0]
    assert "'rx_001'" in msg  # first UE's antenna is the one applied
    assert "rx_002" in msg  # the differing UE is named
    assert "not individually honored" in msg


def test_routes_antenna_warning_names_scene_first_ue_not_routes_first():
    """Sionna's scene-level rx_array comes from the first SELECTED rx in SCENE
    device order (not routes order). With routes listed rx_002-then-rx_001 but
    rx_001 first in the scene, the warning must name rx_001 as the applied UE
    and rx_002 as the ignored one — matching what sionna actually does."""
    scene = _distinctive_antenna_scene()  # scene order: rx_001, then rx_002
    lib = load_default_library()
    reversed_routes = [
        UERoute(ue_id="rx_002", waypoints=[[2.0, 2.0, 1.5], [8.0, 8.0, 1.5]]),
        UERoute(ue_id="rx_001", waypoints=[[5.0, 0.0, 1.5], [40.0, 0.0, 1.5]]),
    ]
    req = TrajectorySimulateRequest(routes=reversed_routes, num_points=2, dt_s=0.1)
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    warned = [w for w in result.warnings if "scene-level array" in w]
    assert len(warned) == 1, result.warnings
    assert "'rx_001'" in warned[0]  # scene-first UE is the applied antenna
    assert "rx_002" in warned[0]  # not the routes-first UE


def test_routes_identical_antennas_emit_no_antenna_warning():
    """Identical antenna configs across routed UEs -> no scene-level-array
    warning (the sionna solve honors them all equally)."""
    scene = _multi_ue_scene()  # both RX keep the default isotropic antenna
    assert scene.device_by_id("rx_001").antenna == scene.device_by_id("rx_002").antenna
    lib = load_default_library()
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=2, dt_s=0.1)
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    assert not any("scene-level array" in w for w in result.warnings), result.warnings


# --------------------------------------- B3. persistence round-trip


def test_routes_result_roundtrips_through_save_load_with_ue_ids():
    """A routes result persists via the normal results/*.json flow (model_dump
    -> JSON -> model_validate) with ue_ids and step-major samples intact (B3)."""
    scene = _multi_ue_scene()
    lib = load_default_library()
    req = TrajectorySimulateRequest(routes=_two_routes(), num_points=3, dt_s=0.1)
    result = run_trajectory(MockBackend(), Path("."), scene, lib, _cfg(), req)

    # Mirror _persist_result's store.save_json(..., result.model_dump(mode="json"))
    # and get_trajectory_result's TrajectoryResultSet.model_validate(loaded).
    on_disk = json.loads(result.model_dump_json())
    loaded = TrajectoryResultSet.model_validate(on_disk)

    assert loaded.metadata["ue_ids"] == ["rx_001", "rx_002"]
    assert loaded.metadata["num_steps"] == 3
    assert loaded.ue_id == "rx_001"
    # Step-major sample order survives the round trip.
    assert [s.ue_id for s in loaded.samples] == [
        "rx_001", "rx_002", "rx_001", "rx_002", "rx_001", "rx_002",
    ]
    # And a numeric metric survives unchanged (sanity that samples aren't lost).
    assert loaded.samples[0].rss_dbm == pytest.approx(result.samples[0].rss_dbm)


# ------------------------------------------------- config.tx_ids device filter


def _two_tx_scene() -> Scene:
    """The single-TX multi-UE scene plus a second TX. The mock backend loads no
    geometry, so appending a device is enough to exercise the TX filter."""
    scene = _multi_ue_scene()
    scene.devices.append(
        Device(id="tx_002", name="TX2", kind="tx", position=[20.0, 0.0, 10.0], power_dbm=30.0)
    )
    return scene


def test_trajectory_tx_ids_filter_selects_serving_tx():
    """With two TXs in the scene but config.tx_ids=['tx_002'], only tx_002 is an
    active transmitter: the default serving TX becomes the first ACTIVE tx
    (tx_002), so the serving-link metrics are populated (not the None/zero they'd
    be if serving defaulted to the filtered-out tx_001), no tx_001 ray survives,
    and there is no interference (tx_002 is the sole active TX)."""
    scene = _two_tx_scene()
    lib = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock", tx_ids=["tx_002"])
    req = TrajectorySimulateRequest(
        ue_id="rx_001", start_m=[5.0, 0.0, 1.5], end_m=[40.0, 0.0, 1.5],
        num_points=3, dt_s=0.1, include_paths=True,
    )
    result = run_trajectory(MockBackend(), Path("."), scene, lib, cfg, req)

    assert result.samples
    for s in result.samples:
        assert s.rss_dbm is not None  # serving TX (tx_002) produced paths
        assert s.path_count > 0
        assert s.interference_dbm is None  # tx_002 is the only active TX
        # No tx_001 contribution appears in the rendered rays.
        assert s.paths is not None
        assert all(p.tx_id == "tx_002" for p in s.paths)
        assert not any(p.tx_id == "tx_001" for p in s.paths)


def test_trajectory_serving_tx_id_rejected_when_excluded_by_tx_ids():
    """serving_tx_id is validated against the ACTIVE (tx_ids-filtered) TXs, so
    naming tx_001 while config.tx_ids=['tx_002'] excludes it raises ValueError."""
    scene = _two_tx_scene()
    lib = load_default_library()
    cfg = SimulationConfig(id="default", backend="mock", tx_ids=["tx_002"])
    req = TrajectorySimulateRequest(
        ue_id="rx_001", start_m=[5.0, 0.0, 1.5], end_m=[40.0, 0.0, 1.5],
        num_points=2, dt_s=0.1, serving_tx_id="tx_001",
    )
    with pytest.raises(ValueError):
        run_trajectory(MockBackend(), Path("."), scene, lib, cfg, req)


# ----------------------------------------- per-waypoint orientation (P2 fix)

from app.services.trajectory import resample_orientations  # noqa: E402


def test_resample_orientations_nearest_waypoint():
    # 3 waypoints along +x at 0, 10, 20 m with distinct yaws; resample to 5
    # steps (every 5 m). Each step takes the NEAREST waypoint's orientation.
    wps = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [20.0, 0.0, 0.0]]
    oris = [[0.0, 0.0, 0.0], [90.0, 0.0, 0.0], [180.0, 0.0, 0.0]]
    out = resample_orientations(wps, oris, 5)
    # steps at 0,5,10,15,20 m; nearest wp indices 0,(tie->0.5 uses seg+1 at 5m),1,...,2
    assert out[0] == [0.0, 0.0, 0.0]     # 0 m -> wp0
    assert out[2] == [90.0, 0.0, 0.0]    # 10 m -> wp1
    assert out[4] == [180.0, 0.0, 0.0]   # 20 m -> wp2


def test_resample_orientations_none_when_absent():
    wps = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0]]
    assert resample_orientations(wps, [], 4) == [None, None, None, None]
    assert resample_orientations(wps, [None, None], 3) == [None, None, None]


def test_resample_orientations_fills_null_gap():
    # Middle waypoint has no orientation -> falls back to a segment endpoint.
    wps = [[0.0, 0.0, 0.0], [10.0, 0.0, 0.0], [20.0, 0.0, 0.0]]
    oris = [[10.0, 0.0, 0.0], None, [30.0, 0.0, 0.0]]
    out = resample_orientations(wps, oris, 3)
    assert all(o is not None for o in out)  # no None leaks through


def test_routes_orientation_sets_device_per_step():
    """The routed UE's antenna orientation is set per step from the route's
    per-waypoint orientation (proves the plumbing Sionna reads for beam aim)."""
    scene = _multi_ue_scene()
    lib = load_default_library()
    backend = _CapturingBackend()
    route = UERoute(
        ue_id="rx_001",
        waypoints=[[0.0, 0.0, 1.5], [20.0, 0.0, 1.5]],
        orientations_deg=[[0.0, 0.0, 0.0], [90.0, 0.0, 0.0]],
    )
    req = TrajectorySimulateRequest(routes=[route], num_points=3, dt_s=0.1)
    run_trajectory(backend, Path("."), scene, lib, _cfg(), req)

    # 3 step scenes captured; rx_001's orientation follows the nearest waypoint:
    # step 0 (0 m) -> [0,0,0], step 2 (20 m) -> [90,0,0].
    def rx1_orient(step_scene):
        return next(d.orientation_deg for d in step_scene.devices if d.id == "rx_001")

    assert len(backend.calls) == 3
    assert rx1_orient(backend.calls[0][0]) == [0.0, 0.0, 0.0]
    assert rx1_orient(backend.calls[2][0]) == [90.0, 0.0, 0.0]


def test_routes_without_orientation_keep_authored():
    """No per-waypoint orientation -> the device keeps its authored value."""
    scene = _multi_ue_scene()
    scene.devices[1].orientation_deg = [45.0, 0.0, 0.0]  # rx_001 authored yaw
    lib = load_default_library()
    backend = _CapturingBackend()
    req = TrajectorySimulateRequest(
        routes=[UERoute(ue_id="rx_001", waypoints=[[0.0, 0.0, 1.5], [20.0, 0.0, 1.5]])],
        num_points=2,
        dt_s=0.1,
    )
    run_trajectory(backend, Path("."), scene, lib, _cfg(), req)
    for step_scene, _cfg_used in backend.calls:
        rx1 = next(d for d in step_scene.devices if d.id == "rx_001")
        assert rx1.orientation_deg == [45.0, 0.0, 0.0]
