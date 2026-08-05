"""Coherent narrowband RSS (DEV_HANDOFF_coherent_rss).

The noncoherent power sum is a wideband-average quantity; CW/narrowband
receivers measure |sum a_l e^{j phi_l}|^2 — fading nulls included. DeepVerse
DT31: scoring SEAM's noncoherent prediction against the GT's coherent link
loss produced +10..20 dB outliers (RMSE 2.93 dB) that vanished once the
definitions were matched (0.53 dB).
"""

import math
from pathlib import Path

import pytest

from seam_studio.schemas.calibration import (
    MeasurementSample,
    TrajectoryValidationRequest,
)
from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.simulation import (
    SimulationConfig,
    TrajectorySimulateRequest,
)
from seam_studio.services.measurement_validation import validate_trajectory
from seam_studio.services.project_store import load_default_library
from seam_studio.services.simulation_backends.mock_backend import MockBackend
from seam_studio.services.trajectory import _aggregate, run_trajectory


def _scene() -> Scene:
    return Scene(
        scene_id="coh_test",
        name="Coh Test",
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
            Device(id="tx_001", name="TX", kind="tx", position=[0.0, 0.0, 10.0], power_dbm=30.0),
            Device(id="rx_001", name="RX", kind="rx", position=[20.0, 0.0, 1.5]),
        ],
    )


# ------------------------------------------------------------- _aggregate


def test_aggregate_coherent_in_phase_adds_3db():
    # Two equal-power paths, identical phase: coherent |2a|^2 sits exactly
    # 3.01 dB above the noncoherent 2|a|^2.
    rss, _, _, _, coh = _aggregate([-60.0, -60.0], [50.0, 60.0], 30.0, [0.5, 0.5])
    assert rss is not None and coh is not None
    assert coh - rss == pytest.approx(10.0 * math.log10(2.0), abs=1e-9)


def test_aggregate_coherent_opposite_phase_nulls():
    rss, _, _, _, coh = _aggregate(
        [-60.0, -60.0], [50.0, 60.0], 30.0, [0.0, math.pi]
    )
    # Perfect cancellation: the coherent sum vanishes (None from log(0)) or
    # lands far below the noncoherent sum on float residue.
    assert rss is not None
    assert coh is None or coh < rss - 60.0


def test_aggregate_without_phases_returns_none_coherent():
    rss, _, _, _, coh = _aggregate([-60.0], [50.0], 30.0, None)
    assert rss is not None and coh is None


# ---------------------------------------------------- trajectory + validate


def test_trajectory_samples_carry_coherent_rss(tmp_path: Path):
    result = run_trajectory(
        MockBackend(), tmp_path, _scene(), load_default_library(),
        SimulationConfig(id="default", backend="mock"),
        TrajectorySimulateRequest(
            waypoints=[[20.0, 0.0, 1.5], [30.0, 0.0, 1.5]], num_points=2
        ),
    )
    assert result.samples
    for s in result.samples:
        assert s.rss_coherent_dbm is not None
        # Coherent can only lose to (or tie) the noncoherent sum by at most
        # the full constructive bound; sanity: both finite and within 20 dB.
        assert s.rss_dbm is not None
        assert s.rss_coherent_dbm <= s.rss_dbm + 10.0 * math.log10(s.path_count) + 1e-6


def test_validate_metric_matches_definitions(tmp_path: Path):
    """Measurements built from the mock's own COHERENT prediction: scoring
    them with metric='coherent' is near-exact, while the noncoherent default
    shows the definition mismatch as extra RMSE."""
    scene = _scene()
    library = load_default_library()
    config = SimulationConfig(id="default", backend="mock")
    backend = MockBackend()
    positions = [[15.0, 0.0, 1.5], [22.0, 3.0, 1.5], [31.0, -4.0, 1.5], [40.0, 6.0, 1.5]]

    truth = run_trajectory(
        backend, tmp_path, scene, library, config,
        TrajectorySimulateRequest(
            waypoints=[[float(c) for c in p] for p in positions],
            num_points=len(positions),
        ),
    )
    tx_power = scene.devices[0].power_dbm
    measurements = [
        MeasurementSample(
            rx_position=p, measured_path_gain_db=s.rss_coherent_dbm - tx_power
        )
        for p, s in zip(positions, truth.samples)
    ]

    coh = validate_trajectory(
        backend, tmp_path, scene, library, config,
        TrajectoryValidationRequest(config=config, metric="coherent"),
        measurements,
    )
    non = validate_trajectory(
        backend, tmp_path, scene, library, config,
        TrajectoryValidationRequest(config=config, metric="noncoherent"),
        measurements,
    )
    assert coh.stats.n == len(positions)
    assert coh.stats.rmse_db == pytest.approx(0.0, abs=1e-6)
    # The mismatched definition must show up as strictly worse RMSE.
    assert non.stats.rmse_db > coh.stats.rmse_db + 0.01
