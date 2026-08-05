"""Atmospheric gas attenuation (ITU-R P.676 post-process, DEV_HANDOFF item).

Contract: disabled -> exactly 0 and no warning; explicit override -> used
verbatim with no warning; enabled without override -> the COARSE built-in
curve WITH a warning (approximate numbers must never pass silently).
"""

import math
from types import SimpleNamespace

import pytest

from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import atmosphere
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.mock_backend import (
    SPEED_OF_LIGHT,
    MockBackend,
)

TX_POS = [0.0, 0.0, 10.0]
RX_POS = [20.0, 0.0, 1.5]


def make_scene() -> Scene:
    return Scene(
        scene_id="atm_test",
        name="Atm Test",
        prims=[
            Prim(
                id="/terrain/ground",
                name="ground",
                semantic_tags=["terrain", "ground"],
                mesh_ref=MeshRef(mesh_name="ground"),
                rf=RFBinding(
                    material_id="ground",
                    assignment_status="user_confirmed",
                    assignment_sources=["user"],
                ),
            ),
        ],
        devices=[
            Device(id="tx_001", name="TX", kind="tx", position=TX_POS, power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=RX_POS),
        ],
    )


def _cfg(**kw):
    return SimpleNamespace(
        atmospheric_absorption=kw.get("enabled", False),
        absorption_db_per_km=kw.get("override"),
        frequency_hz=kw.get("frequency_hz", 60e9),
    )


def test_disabled_is_exactly_zero():
    alpha, warning = atmosphere.absorption_db_per_km(_cfg(enabled=False))
    assert alpha == 0.0 and warning is None


def test_override_wins_and_is_silent():
    alpha, warning = atmosphere.absorption_db_per_km(
        _cfg(enabled=True, override=14.17)
    )
    assert alpha == 14.17 and warning is None


def test_builtin_curve_warns_and_hits_oxygen_peak():
    alpha, warning = atmosphere.absorption_db_per_km(
        _cfg(enabled=True, frequency_hz=60e9)
    )
    # The 60 GHz oxygen band anchor is the one number the curve must get right.
    assert alpha == pytest.approx(15.0, rel=0.05)
    assert warning is not None and "APPROXIMATE" in warning
    # Below ~10 GHz the effect is negligible.
    low, _ = atmosphere.absorption_db_per_km(_cfg(enabled=True, frequency_hz=3.5e9))
    assert low < 0.02


def test_path_attenuation_is_linear_in_length():
    # 1 km of path at 10 dB/km is exactly 10 dB.
    delay_1km = 1000.0 / atmosphere.SPEED_OF_LIGHT
    assert atmosphere.path_attenuation_db(10.0, delay_1km) == pytest.approx(10.0)
    assert atmosphere.path_attenuation_db(0.0, delay_1km) == 0.0
    assert atmosphere.path_attenuation_db(10.0, 0.0) == 0.0


def test_mock_paths_shift_by_exactly_alpha_times_distance(tmp_path):
    """Enabling absorption with an explicit alpha lowers every mock path by
    alpha * path_length_km and nothing else changes."""
    backend = MockBackend()
    scene = make_scene()
    library = load_default_library()
    base_cfg = SimulationConfig()
    on_cfg = SimulationConfig(atmospheric_absorption=True, absorption_db_per_km=10.0)

    base = backend.simulate_paths(tmp_path, scene, library, base_cfg)
    on = backend.simulate_paths(tmp_path, scene, library, on_cfg)
    assert len(base.paths) == len(on.paths)
    for pb, po in zip(base.paths, on.paths):
        # Path length from the shared delay (delay * c).
        length_km = (pb.delay_ns * 1e-9) * SPEED_OF_LIGHT / 1000.0
        assert po.power_dbm == pytest.approx(pb.power_dbm - 10.0 * length_km, abs=1e-9)
        assert po.delay_ns == pb.delay_ns and po.phase_rad == pb.phase_rad

    # LoS sanity against raw geometry.
    d_km = math.dist(TX_POS, RX_POS) / 1000.0
    los_b = next(p for p in base.paths if p.path_type == "los")
    los_o = next(p for p in on.paths if p.path_type == "los")
    assert los_b.power_dbm - los_o.power_dbm == pytest.approx(10.0 * d_km, abs=1e-9)


def test_mock_warns_when_builtin_curve_used(tmp_path):
    backend = MockBackend()
    result = backend.simulate_paths(
        tmp_path,
        make_scene(),
        load_default_library(),
        SimulationConfig(atmospheric_absorption=True),
    )
    assert any("APPROXIMATE" in w for w in result.warnings)


def test_out_of_anchor_range_warns_extrapolation():
    """Above 100 GHz the built-in curve is a flat clamp, not an interpolation
    (183 GHz H2O line: true ~28 dB/km vs clamp 0.7 dB/km, ~40x low). The
    regular '~2x off' wording would be a lie there — the warning must say the
    number is an extrapolation that can be an order of magnitude wrong."""
    cfg = SimulationConfig(
        id="default", backend="mock", frequency_hz=183e9,
        atmospheric_absorption=True,
    )
    alpha, warning = atmosphere.absorption_db_per_km(cfg)
    assert alpha == pytest.approx(0.7)
    assert warning is not None and "OUTSIDE" in warning
    assert "order of magnitude" in warning
    # In-range frequencies keep the regular approximate-curve wording.
    cfg60 = SimulationConfig(
        id="default", backend="mock", frequency_hz=60e9,
        atmospheric_absorption=True,
    )
    _, w60 = atmosphere.absorption_db_per_km(cfg60)
    assert w60 is not None and "OUTSIDE" not in w60


def test_grid_radio_map_warns_absorption_not_applied(tmp_path):
    """The grid radio map is solver-side and skips the per-path absorption
    post-process — without a warning the heatmap silently reads alpha*distance
    dB above paths/trajectory results in the same session (4.5 dB at 300 m /
    60 GHz, DeepVerse review finding)."""
    scene = make_scene()
    library = load_default_library()
    backend = MockBackend()
    on = SimulationConfig(
        id="default", backend="mock", frequency_hz=60e9,
        atmospheric_absorption=True, absorption_db_per_km=15.0,
    )
    rm = backend.simulate_radio_map(tmp_path, scene, library, on)
    assert any("NOT corrected" in w for w in rm.warnings)

    off = SimulationConfig(id="default", backend="mock", frequency_hz=60e9)
    rm_off = backend.simulate_radio_map(tmp_path, scene, library, off)
    assert not any("NOT corrected" in w for w in rm_off.warnings)
