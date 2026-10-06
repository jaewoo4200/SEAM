"""Sensing-target binding (RCS), the sensing solve request, and the ISAC
trade-off / sensing-coverage requests."""

from typing import Annotated, Literal, Optional

from pydantic import Field, model_validator

from .common import StrictModel, Vec3
from .simulation import SimulationConfig

SensingModel = Literal["tr38901", "constant"]
TR38901ObjectType = Literal[
    "uav-small-size", "uav-large-size", "human",
    "vehicle-single-sp", "vehicle-multi-sp", "agv-single-sp", "agv-multi-sp",
]

# Mirrors sionna-rt 2.2 sionna/rt/rcs/tr38901/parameters.py (OBJECT_TYPES /
# TARGET_PARAMETERS): the model types TR 38.901 clause 7.9.2.1 defines per
# object type, and 10lg(sigma_M), the mean monostatic RCS [dBsm].
TR38901_OBJECT_TYPES: tuple[str, ...] = (
    "uav-small-size", "uav-large-size", "human",
    "vehicle-single-sp", "vehicle-multi-sp", "agv-single-sp", "agv-multi-sp",
)
TR38901_MODEL_TYPES: dict[str, tuple[int, ...]] = {
    "uav-small-size": (1,), "uav-large-size": (2,), "human": (1, 2),
    "vehicle-single-sp": (2,), "vehicle-multi-sp": (2,),
    "agv-single-sp": (2,), "agv-multi-sp": (2,),
}
TR38901_NOMINAL_RCS_DBSM: dict[str, float] = {
    "uav-small-size": -12.81, "uav-large-size": -5.85, "human": -1.37,
    "vehicle-single-sp": 11.25, "vehicle-multi-sp": 11.25,
    "agv-single-sp": -4.25, "agv-multi-sp": -4.25,
}
# Actor kind -> TR 38.901 object type used when sensing.object_type is None.
DEFAULT_OBJECT_TYPE_BY_KIND: dict[str, Optional[str]] = {
    "car": "vehicle-multi-sp", "human": "human", "uav": "uav-small-size", "custom": None,
}


def model_type_error(object_type: str, model_type: int) -> str:
    return (
        f"TR 38.901 does not define RCS model {model_type} for '{object_type}'; "
        f"available: {list(TR38901_MODEL_TYPES[object_type])}"
    )


class SensingTargetSpec(StrictModel):
    """Binds an actor as a radar sensing target (sionna.rt.rcs)."""

    model: SensingModel = "tr38901"
    # tr38901 only. None = derived from the actor kind (custom: required).
    object_type: Optional[TR38901ObjectType] = None
    # tr38901 only. None = the highest model type the object type defines.
    model_type: Optional[Literal[1, 2]] = None
    # constant only. None = 0 dBsm (1 m^2).
    rcs_dbsm: Optional[float] = Field(default=None, ge=-80.0, le=80.0)
    # constant only. None = the scattering point does not depolarize.
    xpr_db: Optional[float] = None
    # tr38901 only: draw sigma_S, XPR and initial phases per direction pair.
    random_components: bool = False
    # Target cuboid (length, width, height) [m]; None = actor.shape.size_m.
    size_m: Optional[Vec3] = None
    # World-frame m/s; None = the actor trajectory tangent at t=0 (static: 0).
    velocity_m_s: Optional[Vec3] = None
    enabled: bool = True

    @model_validator(mode="after")
    def _fields_match_model(self) -> "SensingTargetSpec":
        if self.size_m is not None and any(v <= 0 for v in self.size_m):
            raise ValueError("sensing.size_m entries must be > 0")
        if self.model == "constant":
            offending = [
                name
                for name, set_ in (
                    ("object_type", self.object_type is not None),
                    ("model_type", self.model_type is not None),
                    ("random_components", self.random_components),
                )
                if set_
            ]
            if offending:
                raise ValueError(
                    f"sensing.{', sensing.'.join(offending)} applies to model "
                    "'tr38901' only"
                )
        else:
            offending = [
                name
                for name, value in (("rcs_dbsm", self.rcs_dbsm), ("xpr_db", self.xpr_db))
                if value is not None
            ]
            if offending:
                raise ValueError(
                    f"sensing.{', sensing.'.join(offending)} applies to model "
                    "'constant' only; TR 38.901 derives RCS/XPR from the spec tables"
                )
            if (
                self.object_type is not None
                and self.model_type is not None
                and self.model_type not in TR38901_MODEL_TYPES[self.object_type]
            ):
                raise ValueError(model_type_error(self.object_type, self.model_type))
        return self


class SensingSimulateRequest(StrictModel):
    """Body for POST /projects/{id}/simulate/sensing."""

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    # Override config.tx_ids / config.rx_ids (None = keep the config's).
    tx_ids: Optional[list[str]] = None
    rx_ids: Optional[list[str]] = None
    # None = every actor whose sensing binding is enabled.
    target_actor_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # Also run the normal paths solve and append its RayPaths to the result.
    include_comm_paths: bool = False
    samples_per_sp: int = Field(default=1_000_000, ge=1, le=100_000_000)
    # RCSSolver depth (counts the scattering event). None = max(1, config.max_depth).
    max_depth: Optional[int] = Field(default=None, ge=1, le=12)


class SensingTrackOptions(StrictModel):
    """Sensing over time: run a sensing solve in every scenario frame and turn
    the echoes into per-link detections and a fused target estimate."""

    enabled: bool = False
    # Detection threshold on the post-integration SNR [dB].
    threshold_db: float = Field(default=13.0, ge=-30.0, le=60.0)
    # Coherent processing interval [s]: Doppler resolution = 1 / cpi_s.
    cpi_s: float = Field(default=0.01, gt=0.0, le=10.0)
    # Coherently integrated pulses / OFDM samples: gain 10*log10(cpi_pulses).
    cpi_pulses: int = Field(default=1, ge=1, le=100_000_000)
    # MTI notch: |f_D| below this is rejected as static clutter.
    # None = 1 / cpi_s (one Doppler cell); 0 disables MTI.
    mti_min_doppler_hz: Optional[float] = Field(default=None, ge=0.0)
    # Also solve each frame's comm paths with the targets as absorbers and
    # append them to SensingFrame.echoes (target_id None): the static returns
    # MTI removes. One extra solve per frame.
    include_comm_paths: bool = False
    # None = every actor whose sensing binding is enabled.
    target_actor_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # Passed through to the per-frame sensing solve (SensingSimulateRequest).
    samples_per_sp: int = Field(default=1_000_000, ge=1, le=100_000_000)
    max_depth: Optional[int] = Field(default=None, ge=1, le=12)
    # Perturb the measured bistatic range / Doppler fed to fusion with
    # Gaussian noise of sigma = resolution cell / sqrt(2 * SNR) (seeded).
    measurement_noise: bool = False
    noise_seed: int = Field(default=0, ge=0)

    def resolved_mti_min_doppler_hz(self) -> float:
        return 1.0 / self.cpi_s if self.mti_min_doppler_hz is None else self.mti_min_doppler_hz


SharingMode = Literal["time_sharing", "dual_function"]
UeAssociation = Literal["serving", "all"]
MAX_ISAC_BEAMS = 361
# Trade-off points per tx: codebook beams x nonzero slot ratios (+ rho = 0).
MAX_ISAC_POINTS = 4096


class ISACRequest(StrictModel):
    """Body for POST /projects/{id}/simulate/isac."""

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    # None = every tx device.
    tx_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # Role split. Both None: an rx within 1.0 m of a selected tx is that tx's
    # sensing receiver, every other rx is a UE. See plan_isac_roles.
    ue_rx_ids: Optional[list[str]] = Field(default=None, min_length=1)
    sensing_rx_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # None = every actor whose sensing binding is enabled.
    target_actor_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # serving: each UE belongs to the tx with the strongest best-beam RSS;
    # all: every tx serves every UE (single-cell view per tx).
    ue_association: UeAssociation = "serving"
    tx_rows: int = Field(default=4, ge=1, le=16)
    tx_cols: int = Field(default=4, ge=1, le=16)
    # Sensing-RX array; None = the tx array. UEs are always single element.
    rx_rows: Optional[int] = Field(default=None, ge=1, le=16)
    rx_cols: Optional[int] = Field(default=None, ge=1, le=16)
    # Fixed-bearing panels (Device.orientation_deg). False is rejected (400):
    # look_at cannot aim one panel at UEs and targets in different directions.
    use_device_orientation: bool = True
    sweep_start_deg: float = Field(default=-60.0, ge=-90.0, le=90.0)
    sweep_stop_deg: float = Field(default=60.0, ge=-90.0, le=90.0)
    sweep_step_deg: float = Field(default=5.0, gt=0.0, allow_inf_nan=False)
    cpi_pulses: int = Field(default=4096, ge=1, le=100_000_000)
    # Metadata only (the resolutions in metadata).
    cpi_s: float = Field(default=0.01, gt=0.0, le=10.0)
    # Per-beam "detected" flag.
    threshold_db: float = Field(default=13.0, ge=-30.0, le=60.0)
    pfa: float = Field(default=1e-6, gt=0.0, lt=1.0)
    # Operating point of the Pareto summary.
    pd_target: float = Field(default=0.9, gt=0.0, lt=1.0)
    # Fractions of slots / pulses spent sensing (sorted and deduped).
    slot_ratios: list[Annotated[float, Field(ge=0.0, le=1.0)]] = Field(
        default_factory=lambda: [0.0, 0.05, 0.1, 0.2, 0.3, 0.5, 0.7, 1.0],
        min_length=1,
        max_length=32,
    )
    sharing_mode: SharingMode = "dual_function"
    samples_per_sp: int = Field(default=1_000_000, ge=1, le=100_000_000)
    max_depth: Optional[int] = Field(default=None, ge=1, le=12)
    # Store the echo + comm paths of the solve in the result (echoes first).
    include_paths: bool = False

    @model_validator(mode="after")
    def _sweep(self) -> "ISACRequest":
        if self.sweep_start_deg > self.sweep_stop_deg:
            raise ValueError("sweep_start_deg must be <= sweep_stop_deg")
        steps = (self.sweep_stop_deg - self.sweep_start_deg) / self.sweep_step_deg + 1e-9
        # Not "<": also catches inf (a subnormal step overflows the division).
        if not steps < MAX_ISAC_BEAMS:
            n = f"{int(steps) + 1}" if steps < 1e9 else "too many"
            raise ValueError(f"codebook has {n} beams; at most {MAX_ISAC_BEAMS}")
        n_beams = int(steps) + 1
        n_ratios = len({r for r in self.slot_ratios if r > 0.0})
        if n_beams * n_ratios > MAX_ISAC_POINTS:
            raise ValueError(
                f"{n_beams} beams x {n_ratios} nonzero slot ratios = "
                f"{n_beams * n_ratios} trade-off points per tx; at most {MAX_ISAC_POINTS}"
            )
        return self


SensingCoverageMetric = Literal["best_snr_db", "n_links_detected", "pd_best", "fusion_feasible"]


class SensingCoverageRequest(StrictModel):
    """Body for POST /projects/{id}/simulate/sensing-coverage."""

    config_id: Optional[str] = None
    config: Optional[SimulationConfig] = None
    tx_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # None = rx devices co-located (<= 1 m) with a selected tx (ISAC rule).
    sensing_rx_ids: Optional[list[str]] = Field(default=None, min_length=1)
    # Point target RCS. Both None = TR 38.901 uav-small-size nominal (-12.81 dBsm).
    rcs_dbsm: Optional[float] = Field(default=None, ge=-80.0, le=80.0)
    object_type: Optional[TR38901ObjectType] = None
    height_m: float = Field(default=60.0, ge=-1e5, le=1e5)
    cell_size_m: float = Field(default=10.0, gt=0.0, le=1e4)
    # Optional explicit extent (RadioMapGridConfig semantics); None = scene bounds.
    center_xy: Optional[list[Annotated[float, Field(ge=-1e7, le=1e7)]]] = Field(
        default=None, min_length=2, max_length=2
    )
    size_xy: Optional[list[Annotated[float, Field(le=1e6, allow_inf_nan=False)]]] = Field(
        default=None, min_length=2, max_length=2
    )
    threshold_db: float = Field(default=13.0, ge=-30.0, le=60.0)
    cpi_pulses: int = Field(default=4096, ge=1, le=100_000_000)
    pfa: float = Field(default=1e-6, gt=0.0, lt=1.0)
    # steered: ideal full array gain on both legs (every link beams at every cell).
    array_gain: Literal["none", "steered"] = "none"
    tx_rows: int = Field(default=4, ge=1, le=16)
    tx_cols: int = Field(default=4, ge=1, le=16)
    # None = the tx array.
    rx_rows: Optional[int] = Field(default=None, ge=1, le=16)
    rx_cols: Optional[int] = Field(default=None, ge=1, le=16)
    min_links_for_fusion: int = Field(default=3, ge=1, le=64)

    @model_validator(mode="after")
    def _rcs_source(self) -> "SensingCoverageRequest":
        if self.rcs_dbsm is not None and self.object_type is not None:
            raise ValueError("set rcs_dbsm or object_type, not both")
        if (self.center_xy is None) != (self.size_xy is None):
            raise ValueError("center_xy and size_xy go together")
        if self.size_xy is not None and min(self.size_xy) <= 0:
            raise ValueError("size_xy entries must be > 0")
        return self

    def resolved_rcs_dbsm(self) -> float:
        if self.rcs_dbsm is not None:
            return float(self.rcs_dbsm)
        return TR38901_NOMINAL_RCS_DBSM[self.object_type or "uav-small-size"]


def resolve_object_type(kind: str, spec: SensingTargetSpec) -> Optional[str]:
    """TR 38.901 object type of a binding (None for constant / unresolved)."""
    if spec.model != "tr38901":
        return None
    return spec.object_type or DEFAULT_OBJECT_TYPE_BY_KIND.get(kind)


def resolve_model_type(object_type: str, spec: SensingTargetSpec) -> int:
    if spec.model_type is not None:
        return int(spec.model_type)
    return max(TR38901_MODEL_TYPES[object_type])
