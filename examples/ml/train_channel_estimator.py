"""Channel-estimation demo on a SEAM ML dataset.

Task: estimate the full CFR H[N,K] from noisy pilot observations at a subset
of subcarriers - the canonical use of ray-traced ground truth (AODT-style).

    python examples/ml/train_channel_estimator.py <path/to/dataset.npz>

Always runs a numpy LS + linear-interpolation baseline. If PyTorch is
installed (pip install torch), also trains a small MLP that maps pilot
observations to the dense CFR and reports its NMSE against the baseline.
Generate a dataset first: Results mode -> "ML dataset" -> Generate, or
POST /api/projects/<pid>/datasets/generate (see docs/ml_datasets.md).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

PILOT_SPACING = 4  # every 4th subcarrier carries a pilot
SNR_DB = 10.0
TRAIN_FRACTION = 0.8
EPOCHS = 200
SEED = 0


def nmse_db(h_hat: np.ndarray, h: np.ndarray) -> float:
    err = np.sum(np.abs(h_hat - h) ** 2)
    ref = np.sum(np.abs(h) ** 2)
    return 10.0 * np.log10(err / ref) if ref > 0 else float("nan")


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    npz = Path(sys.argv[1])
    with np.load(npz) as z:
        H = z["cfr"].astype(np.complex64)  # [N, K]
        los = z["los"]
    n, k = H.shape
    keep = np.abs(H).sum(axis=1) > 0
    total = n
    H = H[keep]
    n = len(H)
    if n == 0:
        print(
            f"ERROR: dataset has 0 usable (non-zero) channel samples of {total} total.\n"
            "Every sampled UE position produced zero ray paths — the sampling\n"
            "region was almost certainly outside the scene geometry. Regenerate\n"
            "the dataset with the region fit to the scene bounds (use the UI's\n"
            "'Fit to scene' / 'Pick region in viewport' buttons)."
        )
        return 1
    print(f"dataset: {npz.name} | {n} usable samples x {k} subcarriers "
          f"| LOS ratio {los[keep].mean():.2f}")

    rng = np.random.default_rng(SEED)
    # Normalize per-sample power so the SNR definition is uniform.
    scale = np.sqrt(np.mean(np.abs(H) ** 2, axis=1, keepdims=True))
    scale[scale == 0] = 1
    Hn = H / scale

    pilots = np.arange(0, k, PILOT_SPACING)
    noise_std = 10 ** (-SNR_DB / 20) / np.sqrt(2)
    noise = noise_std * (rng.standard_normal((n, len(pilots)))
                         + 1j * rng.standard_normal((n, len(pilots))))
    Y = Hn[:, pilots] + noise  # pilot observations (X=1 pilots)

    # ---- LS + linear interpolation baseline (per sample, numpy only).
    ls = np.empty_like(Hn)
    for i in range(n):
        ls[i] = np.interp(np.arange(k), pilots, Y[i].real) \
            + 1j * np.interp(np.arange(k), pilots, Y[i].imag)
    split = int(n * TRAIN_FRACTION)
    print(f"LS + interp baseline NMSE (test): {nmse_db(ls[split:], Hn[split:]):+.2f} dB")

    # ---- Optional: small MLP trained on the ray-traced ground truth.
    try:
        import torch
        import torch.nn as nn
    except ImportError:
        print("PyTorch not installed - skipped the learned estimator "
              "(pip install torch to enable).")
        return 0

    torch.manual_seed(SEED)
    def to_ri(x: np.ndarray) -> np.ndarray:  # complex -> [.., 2*len] real
        return np.concatenate([x.real, x.imag], axis=-1).astype(np.float32)

    X_tr = torch.from_numpy(to_ri(Y[:split]))
    T_tr = torch.from_numpy(to_ri(Hn[:split]))
    X_te = torch.from_numpy(to_ri(Y[split:]))

    model = nn.Sequential(
        nn.Linear(X_tr.shape[1], 256), nn.ReLU(),
        nn.Linear(256, 256), nn.ReLU(),
        nn.Linear(256, T_tr.shape[1]),
    )
    opt = torch.optim.Adam(model.parameters(), lr=1e-3)
    for epoch in range(EPOCHS):
        opt.zero_grad()
        loss = nn.functional.mse_loss(model(X_tr), T_tr)
        loss.backward()
        opt.step()
        if (epoch + 1) % 50 == 0:
            print(f"  epoch {epoch + 1}: train MSE {loss.item():.5f}")

    with torch.no_grad():
        pred = model(X_te).numpy()
    h_hat = pred[:, :k] + 1j * pred[:, k:]
    print(f"MLP estimator NMSE (test):        {nmse_db(h_hat, Hn[split:]):+.2f} dB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
