"""SEAM-MaterialBench v0 - evaluate material-assignment runs against the
research-grade SAM2/DINOv2 split of the FTC building.

Ground truth: the per-material PLYs under
``ftc_material_segmentation_portable_20260707/generated/material_split_sam2_dinov2_v5_frontseed_samroof/meshes``
partition (a subdivided copy of) the original FTC mesh into
concrete / glass / metal / ground / unknown. Each ORIGINAL face gets the GT
class of the nearest split-face centroid (cKDTree) - robust to the split's
boundary subdivision.

A "run" is a per-face class array over the original mesh (0..len-1 in
``CLASSES`` order). Adapters below produce that array from
- a SEAM segmentation preview response (face_materials ids), or
- a SEAM-Agent trace.json segments list (face groups per semantic label).

Metrics: overall accuracy, per-class IoU + F1, coverage (non-unknown share),
and accuracy restricted to faces the run actually labeled (quality of what it
DID claim - the honest headline number for a low-coverage v0).
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

CLASSES = ["concrete", "glass", "metal", "ground", "unknown"]

# SEAM-Agent semantic labels -> GT classes.
SEMANTIC_TO_CLASS = {
    "exterior_wall": "concrete",
    "roof": "concrete",  # FTC GT labels the roof faces concrete
    "curtain_wall_glass": "glass",
    "glass_window": "glass",
    "metal_frame": "metal",
    "door": "unknown",
    "vegetation": "unknown",
    "ground": "ground",
    "unknown": "unknown",
}
# Segmentation-preview material ids -> GT classes (material_segmentation.DEFAULT_MATERIALS order).
SEG_ID_TO_CLASS = {0: "unknown", 1: "concrete", 2: "glass", 3: "metal", 4: "ground"}


def load_ground_truth(original_ply: Path, split_dir: Path) -> np.ndarray:
    mesh = trimesh.load(original_ply, process=False)
    centers = np.asarray(mesh.triangles_center)
    all_pts: list[np.ndarray] = []
    all_cls: list[np.ndarray] = []
    for i, cls in enumerate(CLASSES):
        ply = split_dir / f"FTC_{cls}.ply"
        if not ply.is_file():
            continue
        m = trimesh.load(ply, process=False)
        c = np.asarray(m.triangles_center)
        all_pts.append(c)
        all_cls.append(np.full(len(c), i, dtype=np.int8))
    pts = np.concatenate(all_pts)
    cls = np.concatenate(all_cls)
    tree = cKDTree(pts)
    _, idx = tree.query(centers, k=1, workers=-1)
    return cls[idx]


def labels_from_agent_npz(npz_path: Path) -> np.ndarray:
    """Per-face GT-class labels from a job's persisted face_labels.npz."""
    data = np.load(npz_path, allow_pickle=False)
    sem = [str(c) for c in data["classes"]]
    raw = data["labels"]
    out = np.full(len(raw), CLASSES.index("unknown"), dtype=np.int8)
    for i, label in enumerate(sem):
        cls = SEMANTIC_TO_CLASS.get(label, "unknown")
        out[raw == i] = CLASSES.index(cls)
    return out


def labels_from_seg_preview(face_materials: list[int]) -> np.ndarray:
    ids = np.asarray(face_materials, dtype=np.int8)
    out = np.full(len(ids), CLASSES.index("unknown"), dtype=np.int8)
    for mid, cls in SEG_ID_TO_CLASS.items():
        out[ids == mid] = CLASSES.index(cls)
    return out


def metrics(gt: np.ndarray, pred: np.ndarray) -> dict:
    assert gt.shape == pred.shape
    n = len(gt)
    unk = CLASSES.index("unknown")
    labeled = pred != unk
    out = {
        "faces": int(n),
        "coverage": float(labeled.mean()),
        "accuracy_all": float((gt == pred).mean()),
        "accuracy_labeled": float((gt[labeled] == pred[labeled]).mean()) if labeled.any() else 0.0,
        "per_class": {},
    }
    for i, cls in enumerate(CLASSES):
        if cls == "unknown":
            continue
        tp = int(((gt == i) & (pred == i)).sum())
        fp = int(((gt != i) & (pred == i)).sum())
        fn = int(((gt == i) & (pred != i)).sum())
        iou = tp / (tp + fp + fn) if (tp + fp + fn) else 0.0
        prec = tp / (tp + fp) if (tp + fp) else 0.0
        rec = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * prec * rec / (prec + rec) if (prec + rec) else 0.0
        out["per_class"][cls] = {
            "gt_faces": int((gt == i).sum()),
            "iou": round(iou, 4),
            "f1": round(f1, 4),
            "precision": round(prec, 4),
            "recall": round(rec, 4),
        }
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--original", required=True, help="original FTC PLY")
    ap.add_argument("--splits", required=True, help="SAM2/DINOv2 split mesh dir")
    ap.add_argument("--gt-out", default=None, help="cache the GT labels npz here")
    args = ap.parse_args()
    gt = load_ground_truth(Path(args.original), Path(args.splits))
    counts = {CLASSES[i]: int((gt == i).sum()) for i in range(len(CLASSES))}
    print("GT face counts:", json.dumps(counts))
    if args.gt_out:
        np.savez_compressed(args.gt_out, gt=gt)
        print("saved ->", args.gt_out)
