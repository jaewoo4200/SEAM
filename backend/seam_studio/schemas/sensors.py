"""Frame-indexed multimodal sensor data (playback dashboard, issue #3).

The minimal subset of docs/sensor_ingestion.md the GT-vs-DT playback needs:
NOT streams/adapters — just "indexed file sequences + one manifest". A project
may carry a ``sensor_data/`` directory:

    sensor_data/
      manifest.json          <- SensorManifest (this module)
      camera/342.jpg          <- referenced by frames[].files["camera"]
      lidar/342.pcd
      gt_beam/342.json        <- {"azimuth_deg": [...], "power_dbm": [...]}

Files are served by the existing asset route (GET /projects/{id}/assets/...),
so frame files must live INSIDE the project directory and are referenced by
project-relative paths. Frame ``index`` is the dataset's own frame key (e.g.
DeepVerse scene_N); ``time_s`` drives sequence splitting — drive datasets are
segment concats, and uniform-index playback across a segment boundary looks
shuffled (verification-session lesson), so the API returns detected segments.

All positions are scene coordinates (Z-up ENU meters), matching Device.
"""

from typing import Literal, Optional

from pydantic import Field

from seam_studio.schemas.common import StrictModel, Vec3


class SensorChannel(StrictModel):
    """One modality present in the frame files."""

    key: str = Field(pattern=r"^[a-z0-9_\-]+$")
    # image: browser-renderable frame (jpg/png). pointcloud: PCD (ascii or
    # binary xyz[+rgb]). beam_profile: JSON {azimuth_deg: [...world deg...],
    # power_dbm: [...]} — measured GT beam power. json: free-form extras.
    kind: Literal["image", "pointcloud", "beam_profile", "json"]
    label: str = ""


class SensorFramePose(StrictModel):
    """Pose of the tracked entity (usually the UE) at this frame."""

    position: Vec3
    # [yaw, pitch, roll] degrees — same convention as Device.orientation_deg.
    orientation_deg: Vec3 = Field(default_factory=lambda: [0.0, 0.0, 0.0])


class SensorGtMetrics(StrictModel):
    """Optional per-frame ground-truth scalars (curves go in a beam_profile
    channel file; only small numbers belong inline)."""

    rss_dbm: Optional[float] = None
    rss_coherent_dbm: Optional[float] = None
    tau_rms_ns: Optional[float] = None
    n_paths: Optional[int] = None
    # World azimuth (deg) of the measured best beam, when known.
    best_beam_deg: Optional[float] = None


class SensorFrame(StrictModel):
    index: int
    time_s: Optional[float] = None
    # channel key -> project-relative file path (servable via /assets/).
    files: dict[str, str] = Field(default_factory=dict)
    pose: Optional[SensorFramePose] = None
    gt: Optional[SensorGtMetrics] = None


class SensorManifest(StrictModel):
    version: int = 1
    # Scene device (rx) the per-frame pose tracks; the playback builder moves
    # this device. None = the request must name an rx_id explicitly.
    entity_id: Optional[str] = None
    channels: list[SensorChannel] = Field(default_factory=list)
    frames: list[SensorFrame] = Field(default_factory=list)


class SensorSegment(StrictModel):
    """One contiguous drive segment (frame-time jump detection)."""

    label: str
    # Inclusive positions into the SORTED frames array (not frame indices).
    start: int
    end: int


class SensorManifestResponse(StrictModel):
    manifest: SensorManifest
    segments: list[SensorSegment] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
