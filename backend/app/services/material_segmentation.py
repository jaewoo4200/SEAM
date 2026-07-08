"""Split a textured prim into per-material sub-prims (multi-material buildings).

Real buildings are not one material: facades mix glass, concrete and metal.
This service ports the validated FTC pipeline
(``ftc_material_segmentation_portable_20260707/tools/material_split_ftc_by_uv.py``,
the offline SAM2/DINOv2 study's in-repo scaffold) into SEAM:

    texture atlas + UV mesh -> material mask -> per-face assignment
    -> physical split into per-material sub-meshes -> one prim per material

Mask sources are tiered (research-grade to instant):
- ``color_heuristic``: the tool's bootstrap color rules, instant and offline;
- ``vlm_tile_vote``: the texture is tiled and each tile classified by the
  local VLM (LM Studio); tile votes are painted back into a mask;
- ``user_png``: an externally produced mask (e.g. the SAM2/DINOv2 offline
  pipeline) uploaded as an L-mode PNG of material ids, same size as the atlas.

The split is PHYSICAL (new named geometries baked into visual/scene.glb, one
prim each) rather than ``face_group`` sub-references: the compiler, viewer and
picking already treat separate named meshes correctly (the FTC/OSM demos ship
exactly this shape), and the previous GLB is backed up for undo.

Faces are assigned by sampling the mask at each face's UV CENTROID (one
nearest pixel; the tool's behavior, not a majority vote). trimesh loads GLB
UVs with a bottom-left v origin while mask images are top-left, so sampling
uses ``y = (1 - v) * (H - 1)`` when ``flip_v`` (the default, matching the
tool's convention).
"""

from __future__ import annotations

import base64
import io
import json
import os
import re
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from ..schemas.scene import MeshRef, Prim, RFBinding, Scene, VisualBinding
from . import mesh_tools

# ---------------------------------------------------------------- classes

# Ported from the FTC tool's MATERIALS table, remapped onto SEAM library ids.
# ``ground`` intentionally binds the 28 GHz-safe constant material (never the
# ITU very_dry_ground, which is undefined above 10 GHz - import guardrail
# parity). ``unknown`` stays visible as unknown_rf so validation flags it.
@dataclass(frozen=True)
class MaterialClass:
    id: int
    name: str
    rf_material_id: str
    color_rgb: tuple[int, int, int]


DEFAULT_MATERIALS: tuple[MaterialClass, ...] = (
    MaterialClass(0, "unknown", "unknown_rf", (40, 40, 40)),
    MaterialClass(1, "concrete", "itu_concrete", (105, 169, 142)),
    MaterialClass(2, "glass", "itu_glass", (74, 124, 176)),
    MaterialClass(3, "metal", "metal", (217, 119, 6)),
    MaterialClass(4, "ground", "ground_28ghz", (176, 131, 62)),
)
_BY_ID = {m.id: m for m in DEFAULT_MATERIALS}
_BY_NAME = {m.name: m for m in DEFAULT_MATERIALS}


class SegmentationError(ValueError):
    """User-input problem (no texture, no UVs, bad mask); API maps to 400."""


# Per-project write lock: a GLB-mutating operation (split apply/undo, agent
# apply) rewrites visual/scene.glb AND the scene JSON; interleaving two of
# them - or racing a crashy one - can leave the GLB split while the scene
# does not reference the new prims. Held by the API routes across
# bake + save_scene so the two files move together.
_project_locks: dict[str, threading.Lock] = {}
_project_locks_guard = threading.Lock()


def project_write_lock(project_dir: Path) -> threading.Lock:
    key = str(project_dir.resolve())
    with _project_locks_guard:
        lock = _project_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _project_locks[key] = lock
        return lock


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically: temp sibling + ``os.replace``.

    The active visual/scene.glb is overwritten in place during a split/undo; a
    crash mid-write would otherwise leave a truncated, unloadable GLB. Writing
    to a sibling temp in the SAME directory and then ``os.replace`` makes the
    swap atomic on one filesystem (including Windows), so a reader ever only
    sees the old or the new file whole. The temp is cleaned on failure.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


# ---------------------------------------------------------------- masks


def build_color_heuristic_mask(texture) -> "object":
    """Port of the tool's ``create_heuristic_mask`` (bootstrap color rules).

    Order matters and later rules win: concrete default -> glass -> metal ->
    ground -> unknown(very dark). Thresholds are the validated FTC bootstrap;
    they are a starting point the user reviews, never silent truth.
    """
    import numpy as np

    rgb = np.asarray(texture.convert("RGB"), dtype=np.float32)
    r, g, b = rgb[..., 0], rgb[..., 1], rgb[..., 2]
    brightness = rgb.mean(axis=-1)
    spread = rgb.max(axis=-1) - rgb.min(axis=-1)
    saturation = spread / np.maximum(rgb.max(axis=-1), 1.0)

    labels = np.full(rgb.shape[:2], 1, dtype=np.uint8)  # concrete default
    glass = ((b > r * 1.05) & (b > g * 0.92) & (brightness < 150)) | (
        (brightness < 85) & (saturation < 0.42)
    )
    labels[glass] = 2
    metal = (brightness > 165) & (saturation < 0.18)
    labels[metal] = 3
    ground = ((r > g * 1.08) & (g > b * 1.05) & (brightness > 70)) | (
        (g > r * 1.08) & (g > b * 1.08) & (brightness > 55)
    )
    labels[ground] = 4
    labels[brightness < 18] = 0
    return labels


def load_user_mask(png_bytes: bytes, expected_hw: tuple[int, int]) -> "object":
    """Validate + load an externally produced id mask (SAM2/DINOv2 etc.).

    L-mode PNG, same (H, W) as the texture atlas, pixel value == material id.
    """
    import numpy as np
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(png_bytes))
        img.load()
    except Exception as exc:
        raise SegmentationError(f"mask is not a readable image: {exc}")
    labels = np.asarray(img.convert("L"), dtype=np.uint8)
    if labels.shape != expected_hw:
        raise SegmentationError(
            f"mask size {labels.shape[1]}x{labels.shape[0]} does not match the "
            f"texture atlas {expected_hw[1]}x{expected_hw[0]}"
        )
    unknown_ids = sorted(set(int(v) for v in set(labels.ravel().tolist())) - set(_BY_ID))
    if unknown_ids:
        raise SegmentationError(
            f"mask contains unknown material ids {unknown_ids}; allowed: "
            f"{sorted(_BY_ID)} ({', '.join(m.name for m in DEFAULT_MATERIALS)})"
        )
    return labels


# VLM tile classification prompt: constrained one-word answer.
_VLM_TILE_PROMPT = (
    "This image is a tile cropped from a building/site photo texture. "
    "Classify the DOMINANT surface material. Answer with exactly one word "
    "from: concrete, glass, metal, ground, unknown."
)
_VLM_WORD_RE = re.compile(r"\b(concrete|glass|metal|ground|unknown)\b", re.IGNORECASE)


def build_vlm_tile_vote_mask(
    texture,
    tile_px: int = 512,
    max_tiles: int = 64,
    model: Optional[str] = None,
    progress: Optional[callable] = None,
) -> tuple["object", list[dict]]:
    """Tile the atlas and classify each tile with the local VLM (LM Studio).

    The atlas is downsampled until the tile grid fits ``max_tiles`` (a cost
    guard: every tile is one VLM round-trip). Each tile paints its region of
    the mask with the voted material id. Returns (labels, tile_records).
    """
    import httpx
    import numpy as np

    from app.core.config import get_settings

    settings = get_settings().ai
    rgb = texture.convert("RGB")
    # Downsample so (W/tile)*(H/tile) <= max_tiles.
    while (max(1, rgb.width // tile_px)) * (max(1, rgb.height // tile_px)) > max_tiles:
        rgb = rgb.resize((max(tile_px, rgb.width // 2), max(tile_px, rgb.height // 2)))
    cols = max(1, rgb.width // tile_px)
    rows = max(1, rgb.height // tile_px)
    labels = np.full((rgb.height, rgb.width), 1, dtype=np.uint8)
    records: list[dict] = []
    total = rows * cols
    for ty in range(rows):
        for tx in range(cols):
            left, top = tx * tile_px, ty * tile_px
            right = rgb.width if tx == cols - 1 else left + tile_px
            bottom = rgb.height if ty == rows - 1 else top + tile_px
            tile = rgb.crop((left, top, right, bottom))
            buf = io.BytesIO()
            tile_small = tile.copy()
            tile_small.thumbnail((256, 256))
            tile_small.save(buf, format="JPEG", quality=80)
            data_url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
            name = "unknown"
            try:
                resp = httpx.post(
                    f"{settings.openai_url}/chat/completions",
                    json={
                        "model": model or settings.openai_model,
                        "messages": [{
                            "role": "user",
                            "content": [
                                {"type": "text", "text": _VLM_TILE_PROMPT},
                                {"type": "image_url", "image_url": {"url": data_url}},
                            ],
                        }],
                        "temperature": 0,
                        "max_tokens": 300,
                        "stream": False,
                    },
                    timeout=settings.vision_timeout_s,
                )
                resp.raise_for_status()
                content = resp.json()["choices"][0]["message"].get("content") or ""
                m = _VLM_WORD_RE.search(content)
                if m:
                    name = m.group(1).lower()
            except Exception as exc:
                records.append({"tile": [tx, ty], "material": "unknown", "error": str(exc)[:120]})
                if progress:
                    progress(len(records), total)
                continue
            labels[top:bottom, left:right] = _BY_NAME[name].id
            records.append({"tile": [tx, ty], "material": name})
            if progress:
                progress(len(records), total)
    return labels, records


# ---------------------------------------------------------------- assignment


def assign_face_materials(mesh, labels, flip_v: bool = True):
    """Per-face material id by sampling the mask at the face's UV centroid.

    The tool's exact rule: mean of the 3 vertex UVs -> one nearest pixel
    (clamped), ``y = (1 - v) * (H - 1)`` when ``flip_v``. Not a majority vote.
    """
    import numpy as np

    uv = getattr(mesh.visual, "uv", None)
    if uv is None or len(uv) != len(mesh.vertices):
        raise SegmentationError("mesh has no per-vertex UV coordinates")
    face_uv = np.asarray(uv)[mesh.faces].mean(axis=1)  # (F, 2)
    u = np.clip(face_uv[:, 0], 0.0, 1.0)
    v = np.clip(face_uv[:, 1], 0.0, 1.0)
    h, w = labels.shape
    x = np.rint(u * (w - 1)).astype(np.int64)
    y = np.rint(((1.0 - v) if flip_v else v) * (h - 1)).astype(np.int64)
    return labels[y, x]


def _subset_mesh(mesh, face_indices):
    """Sub-mesh of the given faces, vertices re-indexed, visuals carried.

    UVs + texture material ride along when present so split prims keep their
    photo texture in the viewer (and AI evidence crops keep working per
    region); untextured meshes keep plain geometry.
    """
    import numpy as np
    import trimesh

    faces_global = mesh.faces[face_indices]
    used, inverse = np.unique(faces_global.ravel(), return_inverse=True)
    sub = trimesh.Trimesh(
        vertices=mesh.vertices[used],
        faces=inverse.reshape(-1, 3),
        process=False,
    )
    uv = getattr(mesh.visual, "uv", None)
    if uv is not None and len(uv) == len(mesh.vertices):
        sub.visual = trimesh.visual.texture.TextureVisuals(
            uv=np.asarray(uv)[used],
            material=getattr(mesh.visual, "material", None),
        )
    return sub


def split_by_face_material(mesh, face_materials) -> dict[str, "object"]:
    """One sub-mesh per non-empty material class; strict partition."""
    import numpy as np

    out: dict[str, object] = {}
    for mat in DEFAULT_MATERIALS:
        sel = np.nonzero(face_materials == mat.id)[0]
        if len(sel) == 0:
            continue
        out[mat.name] = _subset_mesh(mesh, sel)
    total = sum(len(m.faces) for m in out.values())
    if total != len(mesh.faces):  # pragma: no cover - partition invariant
        raise SegmentationError(
            f"split lost faces ({total} != {len(mesh.faces)}); aborting"
        )
    return out


# ---------------------------------------------------------------- orchestration


def _resolve_prim_geometry(project_dir: Path, scene: Scene, prim_id: str):
    """(prim, trimesh geometry) for any mesh prim (no texture requirement)."""
    prim = scene.prim_by_id(prim_id)
    if prim is None or prim.mesh_ref is None:
        raise SegmentationError(f"prim not found or has no mesh: {prim_id}")
    tm_scene = mesh_tools.load_visual_scene(project_dir, prim.mesh_ref.asset_uri)
    if tm_scene is None:
        raise SegmentationError("visual scene GLB could not be loaded")
    geom = tm_scene.geometry.get(prim.mesh_ref.mesh_name)
    if geom is None:
        # node-name indirection (mirrors ai_provider._resolve_prim_geometry)
        for node in sorted(tm_scene.graph.nodes_geometry):
            if node == prim.mesh_ref.mesh_name:
                _, gname = tm_scene.graph[node]
                geom = tm_scene.geometry.get(gname)
                break
    if geom is None:
        raise SegmentationError(f"mesh {prim.mesh_ref.mesh_name} not found in GLB")
    return prim, geom


def _resolve_textured_prim(project_dir: Path, scene: Scene, prim_id: str):
    """(prim, trimesh geometry, PIL texture) for a textured, UV-carrying prim."""
    from PIL import Image

    prim, geom = _resolve_prim_geometry(project_dir, scene, prim_id)
    tex_rel = prim.visual.base_color_texture if prim.visual else None
    if not tex_rel:
        raise SegmentationError(
            f"prim {prim_id} has no persisted texture (visual.base_color_texture); "
            "segmentation needs the original texture atlas"
        )
    tex_path = (project_dir / tex_rel).resolve()
    if not tex_path.is_file() or not tex_path.is_relative_to(project_dir.resolve()):
        raise SegmentationError(f"texture file missing: {tex_rel}")
    if getattr(geom.visual, "uv", None) is None:
        raise SegmentationError(f"mesh {prim.mesh_ref.mesh_name} has no UVs")
    img = Image.open(tex_path)
    img.load()
    return prim, geom, img


def _batch_dir(project_dir: Path) -> tuple[str, Path]:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    batch = f"{stamp}-{uuid.uuid4().hex[:4]}"
    out = project_dir / "ai" / "segmentation" / batch
    out.mkdir(parents=True, exist_ok=True)
    return batch, out


def save_mask_artifacts(out_dir: Path, labels, texture) -> dict[str, str]:
    """Persist mask ids + a color overlay for FE review (tool parity)."""
    import numpy as np
    from PIL import Image

    Image.fromarray(labels, mode="L").save(out_dir / "material_mask_ids.png")
    palette = np.zeros((*labels.shape, 3), dtype=np.uint8)
    for mat in DEFAULT_MATERIALS:
        palette[labels == mat.id] = mat.color_rgb
    tex_rgb = np.asarray(
        texture.convert("RGB").resize((labels.shape[1], labels.shape[0])),
        dtype=np.float32,
    )
    overlay = np.clip(0.58 * tex_rgb + 0.42 * palette.astype(np.float32), 0, 255).astype(np.uint8)
    over_img = Image.fromarray(overlay, mode="RGB")
    # FE preview does not need atlas-native resolution.
    over_img.thumbnail((1024, 1024))
    over_img.save(out_dir / "material_mask_overlay.png")
    return {
        "mask_ids": (out_dir / "material_mask_ids.png").name,
        "overlay": (out_dir / "material_mask_overlay.png").name,
    }


def segment_preview(
    project_dir: Path,
    scene: Scene,
    prim_id: str,
    labels,
    texture,
    source: str,
    flip_v: bool = True,
    extra: Optional[dict] = None,
) -> dict:
    """Assign faces from a mask and persist reviewable artifacts.

    Returns the preview payload: batch id, per-material face counts, artifact
    asset paths, and the per-face material ids (for the 3D region tint).
    """
    _prim, geom, _img = _resolve_textured_prim(project_dir, scene, prim_id)
    face_mats = assign_face_materials(geom, labels, flip_v=flip_v)
    batch, out_dir = _batch_dir(project_dir)
    names = save_mask_artifacts(out_dir, labels, texture)
    manifest = []
    for mat in DEFAULT_MATERIALS:
        count = int((face_mats == mat.id).sum())
        if count:
            manifest.append(
                {
                    "material_id": mat.id,
                    "name": mat.name,
                    "rf_material_id": mat.rf_material_id,
                    "face_count": count,
                }
            )
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "prim_id": prim_id,
        "mask_source": source,
        "flip_v": flip_v,
        "manifest": manifest,
        **(extra or {}),
    }
    (out_dir / "preview.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
    rel = f"ai/segmentation/{batch}"
    return {
        "batch_id": batch,
        "mask_ref": f"{rel}/material_mask_ids.png",
        "overlay_asset_path": f"{rel}/{names['overlay']}",
        "manifest": manifest,
        "face_materials": [int(v) for v in face_mats],
        "total_faces": int(len(face_mats)),
    }


def apply_split(
    project_dir: Path,
    scene: Scene,
    prim_id: str,
    mask_ref: str,
    flip_v: bool = True,
) -> tuple[Scene, dict]:
    """Split the prim by the persisted mask and bake the new GLB.

    The previous GLB is backed up (visual/scene.pre-split-<batch>.glb) and the
    removed prim's JSON is stored alongside the mask so the split can be
    undone. Returns (updated scene, summary info).
    """
    import numpy as np
    from PIL import Image

    prim, geom, texture = _resolve_textured_prim(project_dir, scene, prim_id)
    mask_path = (project_dir / mask_ref).resolve()
    if not mask_path.is_file() or not mask_path.is_relative_to(project_dir.resolve()):
        raise SegmentationError(f"mask not found: {mask_ref}")
    labels = np.asarray(Image.open(mask_path).convert("L"), dtype=np.uint8)
    face_mats = assign_face_materials(geom, labels, flip_v=flip_v)
    submeshes = split_by_face_material(geom, face_mats)
    if len(submeshes) < 2:
        raise SegmentationError(
            "mask assigns every face to one material; nothing to split"
        )

    def make_prim(cls_name: str, node: str) -> Prim:
        mat = _BY_NAME[cls_name]
        return Prim(
            id=f"{prim.id}_{cls_name}",
            name=f"{prim.name}_{cls_name}",
            type="mesh_primitive",
            semantic_tags=[cls_name],
            mesh_ref=MeshRef(
                asset_uri=prim.mesh_ref.asset_uri, mesh_name=node, face_group=None
            ),
            visual=VisualBinding(
                material_name=f"segmentation:{cls_name}",
                base_color_rgba=[c / 255 for c in mat.color_rgb] + [1.0],
                base_color_texture=(
                    prim.visual.base_color_texture if prim.visual else None
                ),
            ),
            rf=RFBinding(
                material_id=mat.rf_material_id,
                assignment_status="rule_suggested",
                assignment_sources=[f"segmentation:{mask_path.parent.name}"],
                confidence=0.5,
            ),
        )

    return _bake_submeshes(
        project_dir,
        scene,
        prim,
        submeshes,
        batch=mask_path.parent.name,
        make_prim=make_prim,
        undo_extra={"mask_ref": mask_ref, "flip_v": flip_v},
    )


def _bake_submeshes(
    project_dir: Path,
    scene: Scene,
    prim: Prim,
    submeshes: dict[str, "object"],
    batch: str,
    make_prim,
    undo_extra: Optional[dict] = None,
) -> tuple[Scene, dict]:
    """Rewrite the GLB (source mesh -> named sub-meshes) and swap the prims.

    Shared by the material split and the connected-parts split: backs up the
    prior GLB (visual/scene.pre-split-<batch>.glb), writes undo.json under
    ai/segmentation/<batch>/, and returns (updated scene, summary info).
    """
    uri = prim.mesh_ref.asset_uri
    tm_scene = mesh_tools.load_visual_scene(project_dir, uri)
    mesh_name = prim.mesh_ref.mesh_name
    if mesh_name in tm_scene.geometry:
        tm_scene.delete_geometry(mesh_name)
    new_names: dict[str, str] = {}
    for cls_name, sub in submeshes.items():
        node = f"{mesh_name}__{cls_name}"
        tm_scene.add_geometry(sub, geom_name=node, node_name=node)
        new_names[cls_name] = node

    glb_path = project_dir / uri
    backup = project_dir / "visual" / f"scene.pre-split-{batch}.glb"
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(glb_path.read_bytes())
    _atomic_write_bytes(glb_path, tm_scene.export(file_type="glb"))
    # load_visual_scene reads fresh per call and terrain's cache keys on
    # mtime, so the rewritten GLB is picked up without explicit invalidation.

    removed = prim.model_dump(mode="json")
    scene.prims = [p for p in scene.prims if p.id != prim.id]
    added_ids: list[str] = []
    for cls_name, node in new_names.items():
        new_prim = make_prim(cls_name, node)
        added_ids.append(new_prim.id)
        scene.prims.append(new_prim)

    batch_dir = project_dir / "ai" / "segmentation" / batch
    batch_dir.mkdir(parents=True, exist_ok=True)
    undo = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "removed_prim": removed,
        "added_prim_ids": added_ids,
        "backup_glb": f"visual/{backup.name}",
        **(undo_extra or {}),
    }
    (batch_dir / "undo.json").write_text(json.dumps(undo, indent=2), encoding="utf-8")
    info = {
        "added_prim_ids": added_ids,
        "removed_prim_id": prim.id,
        "backup_glb": f"visual/{backup.name}",
        "batch_id": batch,
    }
    return scene, info


def split_connected_parts(
    project_dir: Path,
    scene: Scene,
    prim_id: str,
    min_faces: int = 200,
    max_parts: int = 64,
) -> tuple[Scene, dict]:
    """Split a merged multi-building mesh into its connected components.

    City exports often concatenate many buildings into ONE mesh; per-building
    prims are what material assignment (and per-region segmentation) need.
    Components are found over face adjacency; parts below ``min_faces`` (and
    everything beyond the ``max_parts`` largest) are pooled into a single
    ``rest`` sub-mesh so street furniture cannot explode into thousands of
    prims. Every new prim inherits the source prim's RF binding and texture.
    """
    import numpy as np
    import trimesh

    prim, geom = _resolve_prim_geometry(project_dir, scene, prim_id)
    labels = trimesh.graph.connected_component_labels(
        geom.face_adjacency, node_count=len(geom.faces)
    )
    ids, counts = np.unique(labels, return_counts=True)
    if len(ids) < 2:
        raise SegmentationError(
            "mesh is a single connected component; nothing to split"
        )
    order = np.argsort(-counts)
    keep = [
        int(ids[i])
        for i in order
        if counts[i] >= min_faces
    ][: max_parts - 1]
    if not keep:
        raise SegmentationError(
            f"no connected component reaches min_faces={min_faces}; "
            "lower the threshold"
        )
    submeshes: dict[str, object] = {}
    kept_mask = np.isin(labels, keep)
    for n, comp in enumerate(keep):
        submeshes[f"part_{n:02d}"] = _subset_mesh(
            geom, np.nonzero(labels == comp)[0]
        )
    rest_idx = np.nonzero(~kept_mask)[0]
    if len(rest_idx):
        submeshes["rest"] = _subset_mesh(geom, rest_idx)
    if len(submeshes) < 2:
        raise SegmentationError(
            "split produced a single part; nothing to gain"
        )

    batch, _out_dir = _batch_dir(project_dir)

    def make_prim(cls_name: str, node: str) -> Prim:
        return Prim(
            id=f"{prim.id}_{cls_name}",
            name=f"{prim.name}_{cls_name}",
            type="mesh_primitive",
            semantic_tags=list(prim.semantic_tags),
            mesh_ref=MeshRef(
                asset_uri=prim.mesh_ref.asset_uri, mesh_name=node, face_group=None
            ),
            # Parts inherit the source prim's look and RF binding verbatim -
            # this split changes granularity, not assignments.
            visual=prim.visual.model_copy(deep=True) if prim.visual else None,
            rf=prim.rf.model_copy(deep=True),
        )

    scene, info = _bake_submeshes(
        project_dir,
        scene,
        prim,
        submeshes,
        batch=batch,
        make_prim=make_prim,
        undo_extra={"split": "connected_parts", "min_faces": min_faces},
    )
    info["part_face_counts"] = {
        name: int(len(sub.faces)) for name, sub in submeshes.items()
    }
    return scene, info


def undo_split(project_dir: Path, scene: Scene, batch_id: str) -> tuple[Scene, dict]:
    """Restore the backed-up GLB and re-insert the original prim."""
    if not re.fullmatch(r"[0-9TZ]+-[0-9a-f]{4}", batch_id):
        raise SegmentationError(f"invalid batch id: {batch_id}")
    undo_path = project_dir / "ai" / "segmentation" / batch_id / "undo.json"
    if not undo_path.is_file():
        raise SegmentationError(f"no applied split found for batch {batch_id}")
    undo = json.loads(undo_path.read_text(encoding="utf-8"))
    backup = project_dir / undo["backup_glb"]
    if not backup.is_file():
        raise SegmentationError(f"backup GLB missing: {undo['backup_glb']}")
    original = Prim.model_validate(undo["removed_prim"])
    uri = original.mesh_ref.asset_uri if original.mesh_ref else "visual/scene.glb"
    _atomic_write_bytes(project_dir / uri, backup.read_bytes())
    scene.prims = [p for p in scene.prims if p.id not in set(undo["added_prim_ids"])]
    scene.prims.append(original)
    undo_path.unlink()
    return scene, {"restored_prim_id": original.id, "removed_prim_ids": undo["added_prim_ids"]}


# ---------------------------------------------------------------- VLM jobs

# In-process job registry for the (slow) VLM tile-vote path. Deliberately not
# a task queue: local-first single-user tool; a restart just means re-running
# the preview.
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()


def start_vlm_job(
    project_dir: Path,
    scene: Scene,
    prim_id: str,
    tile_px: int,
    max_tiles: int,
    model: Optional[str],
    flip_v: bool,
) -> str:
    job_id = uuid.uuid4().hex[:12]
    with _jobs_lock:
        _jobs[job_id] = {"status": "running", "progress": 0, "total": 0}

    def run() -> None:
        try:
            _prim, _geom, texture = _resolve_textured_prim(project_dir, scene, prim_id)

            def on_progress(done: int, total: int) -> None:
                with _jobs_lock:
                    _jobs[job_id]["progress"] = done
                    _jobs[job_id]["total"] = total

            labels, records = build_vlm_tile_vote_mask(
                texture, tile_px=tile_px, max_tiles=max_tiles, model=model,
                progress=on_progress,
            )
            result = segment_preview(
                project_dir, scene, prim_id, labels, texture,
                source="vlm_tile_vote", flip_v=flip_v,
                extra={"tiles": records, "model": model},
            )
            with _jobs_lock:
                _jobs[job_id] = {"status": "done", "result": result}
        except Exception as exc:  # surface the failure to the poller
            with _jobs_lock:
                _jobs[job_id] = {"status": "error", "detail": str(exc)}

    threading.Thread(target=run, daemon=True).start()
    return job_id


def get_vlm_job(job_id: str) -> Optional[dict]:
    with _jobs_lock:
        job = _jobs.get(job_id)
        return dict(job) if job is not None else None
