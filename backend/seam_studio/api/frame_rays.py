"""Read-only communication-ray previews for previously saved motion results."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from pydantic import Field

from seam_studio.api.deps import get_store, load_scene_live
from seam_studio.api.simulate import _load_result, _readout_guard
from seam_studio.schemas.actors import ActorState, ScenarioResultSet
from seam_studio.schemas.common import StrictModel
from seam_studio.schemas.results import PathResultSet, RayPath, TrajectoryResultSet
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services import solve_ctx
from seam_studio.services.scenario import _solve_frame_paths
from seam_studio.services.simulation_backends import BackendUnavailableError, resolve_backend
from seam_studio.services.simulation_backends.sionna_backend import _SCENE_CACHE

router = APIRouter(tags=["simulate"])


class FrameRaysRequest(StrictModel):
    max_returned_paths_per_tx: int = Field(ge=1)


class FrameRaysResponse(StrictModel):
    paths: list[list[RayPath]]
    warnings: list[str]


@dataclass
class _Frame:
    time: float
    positions: dict[str, list[float]] = field(default_factory=dict)
    actors: list[ActorState] = field(default_factory=list)
    samples: list[tuple[int, Optional[str]]] = field(default_factory=list)
    archived: Optional[list[RayPath]] = field(default_factory=list)
    tx_ids: Optional[list[str]] = None
    rx_ids: Optional[list[str]] = None
    orientations: dict[str, list[float]] = field(default_factory=dict)
    velocities: dict[str, list[float]] = field(default_factory=dict)


def _frames(kind, raw):
    if kind == "scenario":
        rows = ScenarioResultSet.model_validate(raw).frames
        return [
            _Frame(
                time=row.time_s,
                positions={device.id: device.position for device in row.device_states},
                orientations={d.id: d.orientation_deg for d in row.device_states if d.orientation_deg is not None},
                velocities={d.id: d.velocity_m_s for d in row.device_states if d.velocity_m_s is not None},
                actors=row.actor_states,
                samples=[(i, None)],
                archived=row.paths,
                tx_ids=sorted({link.tx_id for link in row.links}) or None,
                rx_ids=sorted({link.rx_id for link in row.links}) or None,
            )
            for i, row in enumerate(rows)
        ], len(rows)
    rows = TrajectoryResultSet.model_validate(raw).samples
    by_time = {}
    for i, row in enumerate(rows):
        frame = by_time.setdefault(row.time_s, _Frame(row.time_s))
        frame.positions[row.ue_id] = row.position
        if row.orientation_deg is not None:
            frame.orientations[row.ue_id] = row.orientation_deg
        frame.samples.append((i, row.ue_id))
        if row.paths is None:
            frame.archived = None
        elif frame.archived is not None:
            frame.archived.extend(row.paths)
    frames = sorted(by_time.values(), key=lambda frame: frame.time)
    for frame in frames:
        frame.rx_ids = list(frame.positions)
    return frames, len(rows)


def _velocities(before, after, dt):
    if dt <= 0:
        return {}
    return {
        key: [(b - a) / dt for a, b in zip(pos, after[key])]
        for key, pos in before.items() if key in after
    }


@contextmanager
def _preview_workspace(project_dir):
    # RF compilation may update meshes and mapping files. Isolate these
    # outputs while sharing the (often very large) read-only visual assets.
    with TemporaryDirectory(prefix="seam-frame-rays-") as tmp:
        dest = Path(tmp)
        for item in project_dir.iterdir():
            if item.name in ("rf", "mapping") and item.is_dir():
                shutil.copytree(item, dest / item.name)
            elif item.name not in ("results", "history", "ai"):
                (dest / item.name).symlink_to(item, target_is_directory=item.is_dir())
        try:
            yield dest
        finally:
            _SCENE_CACHE.pop(str(dest / "rf/generated_scene.xml"), None)


@router.post("/projects/{project_id}/results/{kind}/{result_id}/frame-rays",
             response_model=FrameRaysResponse)
def frame_rays(project_id: str, kind: Literal["scenario", "trajectory"],
               result_id: str, request: FrameRaysRequest) -> FrameRaysResponse:
    raw = _load_result(project_id, kind, result_id)
    frames, count = _frames(kind, raw)
    limit = request.max_returned_paths_per_tx
    output = [[] for _ in range(count)]
    warnings = {}
    with _readout_guard(project_id, "frame_rays", raw["backend"]), ExitStack() as resources:
        backend = None
        for i, frame in enumerate(frames):
            solve_ctx.tick(i, len(frames))
            if frame.archived is not None:
                archived = PathResultSet(result_id=result_id, backend=raw["backend"],
                                         simulation_config_id=raw["simulation_config_id"],
                                         paths=frame.archived)
                rays = archived.limited_per_tx(limit).paths
            else:
                if backend is None:
                    store = get_store()
                    scene = load_scene_live(store, project_id)
                    library = store.load_materials(project_id)
                    snapshot = raw.get("metadata", {}).get("config_snapshot")
                    if snapshot:
                        config = SimulationConfig.model_validate(snapshot)
                    else:
                        config = next((c for c in scene.simulation_configs
                                       if c.id == raw["simulation_config_id"]), SimulationConfig())
                        warnings["The saved result has no solver settings. Using the current project settings."] = None
                    if snapshot and "max_returned_paths_per_tx" not in snapshot:
                        # Earlier releases sent the UI path count to Sionna's
                        # candidate buffer rather than limiting validated rays.
                        config = config.model_copy(update={
                            "max_num_paths_per_src": max(config.max_num_paths_per_src,
                                                        SimulationConfig().max_num_paths_per_src)
                        })
                    if raw["backend"] in ("sionna", "mock"):
                        config = config.model_copy(update={"backend": raw["backend"]})
                    try:
                        backend = resolve_backend(config)
                    except BackendUnavailableError as exc:
                        raise HTTPException(409, str(exc)) from exc
                    project_dir = store.resolve(project_id)
                    if backend.name == "sionna":
                        project_dir = resources.enter_context(_preview_workspace(project_dir))
                    warnings["Reconstructed rays use recorded positions with the current geometry, materials and device settings."] = None
                    if kind == "trajectory" and any(row.positions.keys() - row.orientations.keys() for row in frames):
                        warnings["Where the trajectory omits antenna orientation, the preview uses the current device orientation."] = None
                # Estimate velocities from the recorded positions, including
                # the backward difference at the last frame.
                j = i + 1 if i + 1 < len(frames) else i
                prev, following = frames[max(j - 1, 0)], frames[j]
                dt = following.time - prev.time
                velocity = _velocities(prev.positions, following.positions, dt)
                velocity.update(frame.velocities)
                step_config = config.model_copy(update={
                    "rx_ids": frame.rx_ids if frame.rx_ids is not None else config.rx_ids,
                    "tx_ids": frame.tx_ids if frame.tx_ids is not None else config.tx_ids,
                })
                if kind == "scenario":
                    actor_velocity = _velocities(
                        {a.id: a.position for a in prev.actors},
                        {a.id: a.position for a in following.actors}, dt
                    )
                    result = _solve_frame_paths(
                        backend, project_dir, scene, library, step_config, frame.actors,
                        frame.positions, actor_velocity, velocity, frame.orientations,
                    )
                else:
                    step_scene = scene.model_copy(deep=True)
                    for uid, position in frame.positions.items():
                        device = step_scene.device_by_id(uid)
                        if device is None or device.kind != "rx":
                            raise HTTPException(409, f"Recorded receiver {uid!r} is missing from the current scene.")
                        device.position = position
                        if uid in frame.orientations:
                            device.orientation_deg = frame.orientations[uid]
                        device.velocity_m_s = velocity.get(uid, [0.0, 0.0, 0.0])
                    result = backend.simulate_paths(project_dir, step_scene, library, step_config)
                for warning in result.warnings:
                    if "backend failed:" in warning or "engine worker failed" in warning:
                        raise HTTPException(409, warning)
                    warnings[warning] = None
                rays = result.frame_rays(limit)
            for index, rx_id in frame.samples:
                output[index] = rays if rx_id is None else [p for p in rays if p.rx_id == rx_id]
        solve_ctx.tick(len(frames), len(frames))
    return FrameRaysResponse(paths=output, warnings=list(warnings))
