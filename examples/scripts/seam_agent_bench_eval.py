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
import sys
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree

CLASSES = ["concrete", "glass", "metal", "ground", "unknown"]

# RF material id -> GT class. The bridge between the agent's material library
# and this benchmark's 5 classes; materials with no GT counterpart (itu_wood,
# vegetation_custom, unknown_rf, ...) fall to "unknown" via .get below.
RF_TO_CLASS = {
    "itu_concrete": "concrete",
    "itu_brick": "concrete",
    "itu_glass": "glass",
    "metal": "metal",
    "ground": "ground",
    "ground_28ghz": "ground",
    "itu_very_dry_ground": "ground",
    "itu_wet_ground": "ground",
    "asphalt_custom": "ground",
    "unknown_rf": "unknown",
}

# Labels of the RETIRED v0 vocabulary, kept so old face_labels.npz artifacts
# still score identically. Current labels are derived from the app's own table
# below - never hand-maintain a second copy of the live vocabulary here.
_LEGACY_SEMANTIC_TO_CLASS = {
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


def _load_semantic_to_rf() -> dict[str, tuple[str, list[str]]]:
    """The shipped agent's label -> (rf_material, alternatives) table."""
    try:
        from seam_studio.services.seam_agent import SEMANTIC_TO_RF
    except ImportError:
        # Plain checkout without an installed/editable seam-studio.
        sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))
        try:
            from seam_studio.services.seam_agent import SEMANTIC_TO_RF
        except ImportError as exc:  # pragma: no cover - environment problem
            raise SystemExit(
                "cannot import seam_studio.services.seam_agent (needed for the "
                "current label vocabulary); run this with the backend venv, e.g. "
                "backend/.venv/Scripts/python.exe examples/scripts/"
                f"seam_agent_bench_eval.py ... ({exc})"
            ) from exc
    return SEMANTIC_TO_RF


def _build_semantic_to_class() -> dict[str, str]:
    """SEAM-Agent semantic label -> GT class.

    Derived from the app's SEMANTIC_TO_RF composed with RF_TO_CLASS, so the
    evaluator tracks the live vocabulary instead of a stale duplicate. A
    hand-maintained copy previously covered only the v0 9-label vocabulary,
    silently scoring current labels (concrete_wall, roof_concrete,
    metal_panel, roof_metal, brick_wall) as "unknown" - on the 725,857-face
    FTC building that dropped 202,167 labeled faces and reported
    iou_concrete = 0.0.
    """
    out = dict(_LEGACY_SEMANTIC_TO_CLASS)
    for label, (rf_material, _alternatives) in _load_semantic_to_rf().items():
        out[label] = RF_TO_CLASS.get(rf_material, "unknown")
    return out


SEMANTIC_TO_CLASS = _build_semantic_to_class()
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
    """Per-face GT-class labels from a job's persisted face_labels.npz.

    An unmapped label is an error, never a silent "unknown": that failure mode
    is what let a vocabulary change go unnoticed and corrupt the metrics.
    """
    data = np.load(npz_path, allow_pickle=False)
    sem = [str(c) for c in data["classes"]]
    raw = data["labels"]
    unmapped = [c for c in sem if c not in SEMANTIC_TO_CLASS]
    if unmapped:
        raise ValueError(
            f"{npz_path}: agent labels {unmapped} have no GT-class mapping; "
            "extend SEMANTIC_TO_RF / RF_TO_CLASS instead of scoring them unknown"
        )
    out = np.full(len(raw), CLASSES.index("unknown"), dtype=np.int8)
    for i, label in enumerate(sem):
        out[raw == i] = CLASSES.index(SEMANTIC_TO_CLASS[label])
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
