# SEAM Studio

**Unified RF–visual scene authoring, RF material assignment, and
[Sionna RT](https://github.com/NVlabs/sionna-rt) digital-twin simulation — in one
local workbench.**

SEAM Studio pairs a FastAPI backend with a bundled React/three.js frontend:
author a 3D scene, bind ITU/custom RF materials to its surfaces (by hand,
by rules, or with a local-LLM agent), then ray-trace paths, radio maps,
UE/UAV trajectories, Doppler, beamforming and handover — all persisted as
reproducible result sets.

## Install

```bash
pip install seam-studio
```

This installs the real ray-tracing engine (`sionna-rt`, Dr.Jit/Mitsuba) by
default — no GPU required (an LLVM CPU backend is used when CUDA is absent),
and the app falls back to its deterministic Mock engine if the import fails.

## Quickstart

```bash
seam-studio            # starts on http://127.0.0.1:8000 and opens the browser
```

The first run creates a **Sample Demo** project (toy urban scene with a
rooftop TX, street RX, car and pedestrian actors) under `~/.seam/projects`,
so you can press **Simulate paths** immediately.

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
- **AI assist**: local LLM/VLM material suggestion agent (Ollama / LM Studio),
  natural-language assignment rules, validation explains.
- **Reproducibility**: every run persisted with config snapshots + content
  hashes; measurement import and calibration against real logs.

## Links

- Repository & docs: <https://github.com/jaewoo4200/SEAM>
- Website: <https://jaewoo4200.github.io/SEAM/>

Apache-2.0 © Jaewoo Lee
