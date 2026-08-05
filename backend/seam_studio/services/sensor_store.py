"""Read a project's ``sensor_data/manifest.json`` (playback dashboard, issue #3).

The only reader of the GT side of the playback: parses the manifest
(schemas/sensors.py), orders frames by their dataset frame key, splits them
into contiguous drive segments, and reports what the UI cannot see for itself
(files that are not on disk, channels the frames use but the manifest never
declares, duplicate frame indices).

Segments exist because drive datasets are concatenations of separate runs:
playing straight across a boundary teleports the UE and looks shuffled
(verification-session lesson), so the response hands the UI the detected
ranges instead of one uniform timeline.
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path
from statistics import median
from typing import Optional

from pydantic import ValidationError

from ..schemas.sensors import (
    SensorFrame,
    SensorManifest,
    SensorManifestResponse,
    SensorSegment,
)

MANIFEST_REL = "sensor_data/manifest.json"

# A frame gap ends a segment when it exceeds this multiple of the median frame
# interval, but never below _MIN_GAP_S: capture jitter must not split a run,
# while the seconds-long jump between concatenated runs must.
_GAP_FACTOR = 3.0
_MIN_GAP_S = 2.0
# Missing-file warnings name at most this many paths before summarizing.
_MAX_LISTED_FILES = 5


def load_manifest(project_dir: Path) -> Optional[SensorManifest]:
    """Parse the project's sensor manifest; None when it carries none.

    A manifest that exists but cannot be read raises ValueError (the route maps
    it to 422) - a half-parsed manifest must never be reported as "this project
    has no sensor data".
    """
    path = project_dir / MANIFEST_REL
    if not path.is_file():
        return None
    try:
        return SensorManifest.model_validate_json(path.read_text(encoding="utf-8"))
    except (ValidationError, UnicodeDecodeError) as exc:
        raise ValueError(f"invalid {MANIFEST_REL}: {exc}") from exc


def manifest_response(
    project_dir: Path, manifest: SensorManifest
) -> SensorManifestResponse:
    """Manifest + detected segments + warnings, frames sorted by frame index.

    The sort is on a copy: the caller's manifest (and the file it came from)
    keeps its authored frame order.
    """
    frames = sorted(manifest.frames, key=lambda f: f.index)
    return SensorManifestResponse(
        manifest=manifest.model_copy(update={"frames": frames}),
        segments=detect_segments(frames),
        warnings=_collect_warnings(project_dir, manifest, frames),
    )


def detect_segments(frames: list[SensorFrame]) -> list[SensorSegment]:
    """Split sorted frames into runs at frame-time jumps.

    Needs >= 2 timed frames to detect anything; an untimed or single-frame
    manifest is one segment covering everything (positions, not frame indices).
    """
    if not frames:
        return []
    times = [f.time_s for f in frames]
    dts = [
        times[i] - times[i - 1]
        for i in range(1, len(times))
        if times[i] is not None and times[i - 1] is not None
    ]
    breaks: list[int] = []
    if dts:
        threshold = max(_GAP_FACTOR * median(dts), _MIN_GAP_S)
        for i in range(1, len(frames)):
            before, after = times[i - 1], times[i]
            if before is None or after is None:
                continue
            dt = after - before
            # dt <= 0 means the clock restarted: a new run, not a slow frame.
            if dt > threshold or dt <= 0:
                breaks.append(i)

    bounds = [0, *breaks, len(frames)]
    return [
        SensorSegment(
            label=f"Seq {n} ({frames[start].index}-{frames[stop - 1].index})",
            start=start,
            end=stop - 1,
        )
        for n, (start, stop) in enumerate(zip(bounds, bounds[1:]), start=1)
    ]


def _collect_warnings(
    project_dir: Path, manifest: SensorManifest, frames: list[SensorFrame]
) -> list[str]:
    warnings: list[str] = []

    declared = {c.key for c in manifest.channels}
    for key in sorted({k for f in frames for k in f.files if k not in declared}):
        warnings.append(
            f"frames reference channel {key!r} that manifest.channels does not declare"
        )

    missing: list[str] = []
    seen: set[str] = set()
    for frame in frames:
        for rel in frame.files.values():
            if rel in seen:
                continue
            seen.add(rel)
            if not (project_dir / rel).is_file():
                missing.append(rel)
    if missing:
        listed = ", ".join(missing[:_MAX_LISTED_FILES])
        extra = len(missing) - _MAX_LISTED_FILES
        if extra > 0:
            listed += f", and {extra} more"
        warnings.append(f"{len(missing)} frame file(s) not found in the project: {listed}")

    counts = Counter(f.index for f in frames)
    for index in sorted(i for i, n in counts.items() if n > 1):
        warnings.append(f"duplicate frame index {index} ({counts[index]} frames)")

    return warnings
