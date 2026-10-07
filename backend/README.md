# SEAM Studio

**Unified RF–visual scene authoring, RF material assignment, and
[Sionna RT](https://github.com/NVlabs/sionna-rt) digital-twin simulation — in one
local workbench.**

SEAM Studio pairs a FastAPI backend with a bundled React/three.js frontend:
author a 3D scene, bind ITU/custom RF materials to its surfaces (by hand,
by rules, or with a local-LLM agent), then ray-trace paths, radio maps,
UE/UAV trajectories, Doppler, beamforming, handover and radar sensing / ISAC —
all persisted as reproducible result sets.

## Install

```bash
pip install seam-studio
```

This installs the real ray-tracing engine (`sionna-rt`, Dr.Jit/Mitsuba) by
default. It runs on an NVIDIA GPU (CUDA). Without one, Dr.Jit's CPU backend
needs LLVM installed separately (Windows: the official LLVM installer, then
set `DRJIT_LIBLLVM_PATH` to its `LLVM-C.dll`; Linux: LLVM 18+, e.g.
`apt install libllvm18` on Ubuntu 24.04, from apt.llvm.org on Ubuntu 22.04 /
Debian 12 whose default LLVM 14 is too old; macOS: Xcode CLT or
`brew install llvm`). When neither backend works, the app
reports Sionna as unavailable with the reason (`/api/health`) and `auto` runs
its deterministic Mock engine instead. Details:
[INSTALL.md](https://github.com/jaewoo4200/SEAM/blob/master/INSTALL.md#cpu-only-machines-no-nvidia-gpu-install-llvm).

**Optional extras:** `pip install "seam-studio[parquet]"` adds `pyarrow` for
AODT parquet import/export and parquet sensing datasets.

## Quickstart

```bash
seam-studio            # starts on http://127.0.0.1:8000 and opens the browser
```

The first run creates a **Sample Demo** project (toy urban scene with a
rooftop TX and its co-located sensing RX, a street RX, car and pedestrian
actors, and a drone sensing target) under `~/.seam/projects`, so you can
press **Simulate paths** immediately.

```
seam-studio --port 9000            # different port
seam-studio --project-root D:\twins  # keep projects elsewhere
seam-studio --no-browser
```

## Highlights

- **Scene → RF binding**: import Mitsuba XML / scene bundles / OpenStreetMap
  extrusions; assign RF materials per surface with validation and provenance.
- **Simulation**: paths, planar & mesh radio maps, multi-TX SINR/RSRP/RSRQ,
  MIMO beamforming, UE/UAV trajectories with per-step handover (3GPP A3),
  Doppler spectrograms, ML ground-truth dataset export (NPZ).
- **Radar sensing & ISAC**: RCS targets (TR 38.901 / constant), echo solves
  with Doppler, per-frame multistatic fusion and EKF tracking, Swerling Pd/Pfa
  detectors with Monte Carlo, ISAC beam trade-off, sensing coverage maps,
  labeled sensing dataset export (npz/csv/parquet).
- **AI assist**: local LLM/VLM material suggestion agent (Ollama / LM Studio),
  natural-language assignment rules, validation explains.
- **Reproducibility**: every run persisted with config snapshots + content
  hashes; measurement import and calibration against real logs; NVIDIA AODT
  parquet import/export.

## Links

- Repository & docs: <https://github.com/jaewoo4200/SEAM>
- Website: <https://jaewoo4200.github.io/SEAM/>

Apache-2.0 © Jaewoo Lee
