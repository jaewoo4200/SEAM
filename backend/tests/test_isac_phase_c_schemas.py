"""ISAC Phase C (v0.1.13) wire contract: defaults keep v0.1.12 behaviour,
validators reject inconsistent requests, stored v0.1.12 results validate."""

import pytest
from pydantic import ValidationError

from seam_studio.schemas.results import (
    ISACBeam,
    ISACPoint,
    PdCurveResult,
    SensingCoverageSummary,
    SensingDatasetExportResult,
    SensingFrame,
    SensingLinkReport,
    TargetEstimate,
)
from seam_studio.schemas.sensing import (
    MAX_MC_TRIALS,
    DetectorOptions,
    ISACRequest,
    PdCurveRequest,
    SensingCoverageRequest,
    SensingDatasetExportRequest,
    SensingDatasetSplit,
    SensingTrackOptions,
    TrackingOptions,
    metadata_dump,
)


def test_defaults_are_v0112():
    opts = SensingTrackOptions()
    assert opts.tracking is None and not opts.tracking_enabled()
    assert opts.detector == DetectorOptions() and opts.detector.model == "swerling1"
    assert opts.pfa is None
    isac = ISACRequest()
    assert not isac.elevation_sweep() and not isac.interference
    assert isac.codebook_size() == (25, 1)
    assert SensingCoverageRequest().detector.monte_carlo_trials == 0
    t = TrackingOptions()
    assert (t.enabled, t.process_accel_sigma_m_s2, t.gate_chi2, t.coast_max_frames) == (
        False, 2.0, 16.0, 10,
    )
    assert t.use_as_prior and t.init_from == "fusion"
    assert t.max_position_std_m == 25.0
    with pytest.raises(ValidationError):
        TrackingOptions(max_position_std_m=0.0)


def test_metadata_dump_omits_phase_c_defaults():
    plain = ISACRequest()
    dump = metadata_dump(plain, exclude=("config",))
    for name in ("elevation_start_deg", "interference", "detector", "config"):
        assert name not in dump
    assert dump["sweep_step_deg"] == 5.0
    changed = ISACRequest(interference=True, detector={"model": "swerling3"})
    dump = metadata_dump(changed, exclude=("config",))
    assert dump["interference"] is True and dump["detector"]["model"] == "swerling3"
    opts = metadata_dump(SensingTrackOptions(tracking={"enabled": True}))
    assert opts["tracking"]["enabled"] is True and "detector" not in opts and "pfa" not in opts


def test_detector_validation():
    with pytest.raises(ValidationError, match="empirical_threshold needs monte_carlo_trials"):
        DetectorOptions(empirical_threshold=True)
    with pytest.raises(ValidationError):
        DetectorOptions(monte_carlo_trials=MAX_MC_TRIALS + 1)
    with pytest.raises(ValidationError, match="needs sensing.pfa"):
        SensingTrackOptions(detector={"monte_carlo_trials": 1000})
    SensingTrackOptions(pfa=1e-3, detector={"monte_carlo_trials": 1000})
    # 20 / pfa exceedances for an empirical threshold.
    with pytest.raises(ValidationError, match=">= 20 / pfa = 20000000"):
        ISACRequest(detector={"monte_carlo_trials": 1_000_000, "empirical_threshold": True})
    ISACRequest(pfa=1e-3, detector={"monte_carlo_trials": 20_000, "empirical_threshold": True})
    with pytest.raises(ValidationError, match="20 / pfa"):
        SensingCoverageRequest(
            pfa=1e-3, detector={"monte_carlo_trials": 19_999, "empirical_threshold": True}
        )


def test_isac_elevation_and_interference_validation():
    req = ISACRequest(elevation_start_deg=-10, elevation_stop_deg=40, elevation_step_deg=5)
    assert req.elevation_sweep() and req.codebook_size() == (25, 11)
    with pytest.raises(ValidationError, match="go together"):
        ISACRequest(elevation_start_deg=0.0)
    with pytest.raises(ValidationError, match="elevation_start_deg must be <="):
        ISACRequest(elevation_start_deg=10, elevation_stop_deg=0, elevation_step_deg=5)
    # 121 azimuth x 41 elevation beams: over the 2-D cap.
    with pytest.raises(ValidationError, match="azimuth x"):
        ISACRequest(
            sweep_step_deg=1.0, elevation_start_deg=-20, elevation_stop_deg=20,
            elevation_step_deg=1.0, slot_ratios=[0.0],
        )
    # 25 x 11 = 275 beams x 7 nonzero ratios = 1925 points: allowed.
    ISACRequest(elevation_start_deg=-10, elevation_stop_deg=40, elevation_step_deg=5)
    # 25 x 25 = 625 beams x 7 = 4375 points.
    with pytest.raises(ValidationError, match="trade-off points"):
        ISACRequest(
            elevation_start_deg=-60, elevation_stop_deg=60, elevation_step_deg=5,
        )
    with pytest.raises(ValidationError, match="interference needs ue_association"):
        ISACRequest(interference=True, ue_association="all")


def test_pd_curve_request():
    req = PdCurveRequest()
    axis = req.snr_axis_db()
    assert axis[0] == -5.0 and axis[-1] == 30.0 and len(axis) == 71
    assert req.models == ["swerling0", "swerling1", "swerling3"]
    assert PdCurveRequest(models=["swerling3", "swerling3", "swerling0"]).models == [
        "swerling3", "swerling0",
    ]
    with pytest.raises(ValidationError, match="snr_min_db must be <="):
        PdCurveRequest(snr_min_db=10, snr_max_db=0)
    with pytest.raises(ValidationError, match="more than 1001"):
        PdCurveRequest(snr_min_db=-100, snr_max_db=200, step_db=0.1)
    with pytest.raises(ValidationError, match="Monte Carlo trials; at most"):
        PdCurveRequest(monte_carlo_trials=MAX_MC_TRIALS)
    # The demo request (Pfa 1e-6, 4096 pulses, all models, 200k trials) fits.
    PdCurveRequest(cpi_pulses=4096, monte_carlo_trials=200_000)
    with pytest.raises(ValidationError):
        PdCurveRequest(models=[])


def test_dataset_request():
    req = SensingDatasetExportRequest()
    assert req.result_ids is None and req.formats == ["npz"] and req.split is None
    assert req.skip_without_sensing is False
    req = SensingDatasetExportRequest(formats=["csv", "npz", "csv"], result_ids=["a", "a"])
    assert req.formats == ["csv", "npz"] and req.result_ids == ["a"]
    assert SensingDatasetSplit().train == 0.8
    with pytest.raises(ValidationError, match="sum to 1"):
        SensingDatasetSplit(train=0.5, val=0.1, test=0.1)
    with pytest.raises(ValidationError):
        SensingDatasetExportRequest(formats=["hdf5"])


def test_stored_v0112_payloads_validate():
    est = TargetEstimate.model_validate(
        {
            "target_id": "uav", "status": "ok", "position_true": [0, 0, 60],
            "velocity_true": [5, 0, 0], "position_est": [0.1, 0, 60],
        }
    )
    assert est.track_status is None and est.track_updates is None
    link = SensingLinkReport.model_validate({"tx_id": "a", "rx_id": "b", "target_id": "uav"})
    assert link.pd is None and link.pd_mc is None
    assert SensingFrame.model_validate({"echoes": [], "links": [], "estimates": []}).nodes is None
    beam = ISACBeam.model_validate({"angle_deg": 0.0, "pd": 1e-6})
    assert beam.elevation_deg is None and beam.ue_interference_dbm is None and beam.pd_mc is None
    assert ISACPoint.model_validate({"rho": 0.0, "sum_rate_bps_hz": 1.0, "pd": 1e-6}).pd_mc is None
    summary = SensingCoverageSummary.model_validate(
        {
            "num_cells": 1, "num_links": 1, "num_geometries": 1, "pct_cells_los": 0.0,
            "pct_cells_detected": 0.0, "pct_cells_fusion_feasible": 0.0,
        }
    )
    assert summary.mc_spot_check is None


def test_result_models_shape():
    PdCurveResult(
        pfa=1e-6, pd_target=0.9, cpi_pulses=1, monte_carlo_trials=0, seed=0,
        snr_db=[0.0], threshold=13.8,
        models=[{"model": "swerling1", "pd": [1e-6]}],
    )
    SensingDatasetExportResult(
        export_dir="export/sensing_dataset", zip_name="x.zip",
        download_url="/api/projects/p/assets/export/sensing_dataset/x.zip",
    )
