"""Per-triangle coverage on prim surfaces (mesh radio map).

Backend-agnostic by construction: probe receivers are parked at each sampled
triangle center (offset along the face normal so they sit just off the
surface) and solved in chunks through the active backend's ``simulate_paths``.
This paints facades, roads, and floors instead of a horizontal plane - the
per-surface analog of the planar radio map.

Centers and normals are returned with the values so the viewer never has to
reproduce the backend's triangle ordering (trimesh and three.js do not agree
on face order after import).
"""

from __future__ import annotations

import math
from pathlib import Path

from app.schemas.materials import RFMaterialLibrary
from app.services import solve_ctx
from app.schemas.devices import Device
from app.schemas.results import MeshRadioMapResultSet, MeshRadioMapSurface
from app.schemas.scene import Scene
from app.schemas.simulation import MeshRadioMapRequest, SimulationConfig
from app.services.simulation_backends.base import (
    UNSAVED_RESULT_ID,
    RayTracingBackend,
)

# Probe receivers solved per simulate_paths call. Large chunks amortize the
# solver's fixed cost; sionna handles hundreds of RX in one PathSolver run.
CHUNK_SIZE = 256


def _sample_surface(project_dir: Path, scene: Scene, prim, budget: int, warnings):
    """(centers, normals, stride) for one prim, or None when unloadable."""
    from app.services import mesh_tools

    if prim.mesh_ref is None:
        warnings.append(f"{prim.id}: no mesh_ref; skipped")
        return None
    try:
        tm_scene = mesh_tools.load_visual_scene(project_dir, prim.mesh_ref.asset_uri)
        mesh = mesh_tools.extract_prim_mesh(tm_scene, prim.mesh_ref)
    except Exception as exc:  # noqa: BLE001 - per-surface best effort
        warnings.append(f"{prim.id}: mesh load failed ({exc}); skipped")
        return None
    if mesh is None or len(mesh.faces) == 0:
        warnings.append(f"{prim.id}: mesh empty; skipped")
        return None
    centers = mesh.triangles_center
    normals = mesh.face_normals
    stride = 1
    if len(centers) > budget:
        stride = math.ceil(len(centers) / budget)
        centers = centers[::stride]
        normals = normals[::stride]
        warnings.append(
            f"{prim.id}: {len(mesh.faces)} triangles exceed the budget; "
            f"sampled every {stride}th triangle"
        )
    return (
        [[float(c) for c in row] for row in centers],
        [[float(n) for n in row] for row in normals],
        stride,
    )


def mesh_radio_map(
    backend: RayTracingBackend,
    project_dir: Path,
    scene: Scene,
    library: RFMaterialLibrary,
    config: SimulationConfig,
    request: MeshRadioMapRequest,
) -> MeshRadioMapResultSet:
    txs = [d for d in scene.devices if d.kind == "tx"]
    if not txs:
        raise ValueError("scene has no transmitters")
    tx = next((d for d in txs if d.id == request.tx_id), txs[0])
    if request.tx_id and tx.id != request.tx_id:
        raise ValueError(f"unknown tx device: {request.tx_id}")

    warnings: list[str] = []
    prims = []
    for prim_id in request.prim_ids:
        prim = scene.prim_by_id(prim_id)
        if prim is None:
            raise ValueError(f"unknown prim: {prim_id}")
        prims.append(prim)

    # Split the triangle budget evenly across the requested surfaces.
    per_surface = max(1, request.max_triangles // max(len(prims), 1))
    sampled = []
    for prim in prims:
        got = _sample_surface(project_dir, scene, prim, per_surface, warnings)
        if got is not None:
            sampled.append((prim, *got))

    surfaces: list[MeshRadioMapSurface] = []
    for prim, centers, normals, stride in sampled:
        probe_points = [
            [
                c[0] + n[0] * request.offset_m,
                c[1] + n[1] * request.offset_m,
                c[2] + n[2] * request.offset_m,
            ]
            for c, n in zip(centers, normals)
        ]
        values: list = []
        for start in range(0, len(probe_points), CHUNK_SIZE):
            solve_ctx.tick(start, len(probe_points))
            chunk = probe_points[start : start + CHUNK_SIZE]
            step = scene.model_copy(deep=True)
            # Only the serving TX plus this chunk's probes: probe receivers
            # replace the scene's own devices for the solve.
            step.devices = [tx.model_copy(deep=True)] + [
                Device(
                    id=f"probe_{i:04d}",
                    name=f"probe {i}",
                    kind="rx",
                    position=[float(x) for x in pt],
                )
                for i, pt in enumerate(chunk)
            ]
            cfg = config.model_copy(
                update={
                    "tx_ids": [tx.id],
                    "rx_ids": [d.id for d in step.devices if d.kind == "rx"],
                }
            )
            result = backend.simulate_paths(project_dir, step, library, cfg)
            by_rx: dict[str, float] = {}
            for path in result.paths:
                if path.tx_id != tx.id:
                    continue
                by_rx[path.rx_id] = by_rx.get(path.rx_id, 0.0) + 10.0 ** (
                    path.power_dbm / 10.0
                )
            for i in range(len(chunk)):
                lin = by_rx.get(f"probe_{i:04d}")
                if lin is None or lin <= 0.0:
                    values.append(None)
                    continue
                dbm = 10.0 * math.log10(lin)
                if request.metric == "path_gain_db":
                    dbm -= tx.power_dbm
                values.append(dbm)
        surfaces.append(
            MeshRadioMapSurface(
                prim_id=prim.id,
                mesh_ref=(
                    f"{prim.mesh_ref.asset_uri}#{prim.mesh_ref.mesh_name}"
                    if prim.mesh_ref
                    else None
                ),
                triangle_count=len(centers),
                centers=centers,
                normals=normals,
                values=values,
                sample_stride=stride,
            )
        )

    if not surfaces:
        warnings.append("no requested surface produced samples")
    return MeshRadioMapResultSet(
        result_id=UNSAVED_RESULT_ID,
        backend=backend.name,
        simulation_config_id=config.id,
        tx_id=tx.id,
        metric=request.metric,
        surfaces=surfaces,
        warnings=warnings,
        metadata={
            "frequency_hz": config.frequency_hz,
            "offset_m": request.offset_m,
            "max_triangles": request.max_triangles,
        },
    )
