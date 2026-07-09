# Compute engines (Swapping the Sionna version)

> **English** · [한국어](engines.ko.md)

SEAM Studio can **swap the Sionna engine version** used to run path solves.
For per-version differences in features, materials, and physics models, and for
"which version fits which research," see
[sionna_versions.md](sionna_versions.md).

## Structure

- **builtin** — Calls sionna-rt installed in the backend venv (`backend/.venv`)
  directly, in-process (default, fastest — shares the scene cache).
- **subprocess engine** — A different sionna-rt version installed in a separate venv.
  `backend/engine_workers/sionna_rt_worker.py` runs under that venv's interpreter
  and returns path results over a file-based JSON protocol (job.json → out.json).
  The PathSolver call is assembled via **signature filtering**, so mechanism
  flags not present in a version are ignored with a warning (it does not crash).

The current engine selection **applies to paths solves**. Radio maps/beamforming/channel
analysis are always computed with builtin (a documented limitation), and the reflection
of actor state during scenario playback is also builtin-only (subprocess computes with
authored poses + a warning).

## Compute device (automatic GPU/CPU selection)

Sionna RT runs on top of Mitsuba 3's Dr.Jit backend. Whatever the engine, just before
a solve it **automatically selects** a variant in the order
`cuda_ad_mono_polarized` (GPU) → `llvm_ad_mono_polarized` (CPU)
(`sionna_backend._pick_variant`).

- **Mock backend** — Requires nothing. It works on CPU alone, without Dr.Jit/Mitsuba.
- **Linux / Windows + NVIDIA GPU** — Automatically selects the CUDA variant (no extra setup).
- **macOS (including Apple Silicon)** — Because Dr.Jit **has no Metal/MPS backend**, it
  always runs on **CPU/LLVM**. It works correctly but is **slower** than GPU. If it cannot
  find CUDA it automatically falls back to LLVM and leaves one line in the result warnings:
  `CUDA unavailable — using LLVM (CPU) ray tracing …` (harmless).

On CUDA machines the behavior does not change (always selects the CUDA variant, no warning).
The variant is process-global, so whichever is pinned first wins; if one is already pinned,
it is respected as-is.

## How to add an engine

1. Create a venv + install the desired sionna-rt:

   ```powershell
   python -m venv backend\.venv-sionna-rt-110
   backend\.venv-sionna-rt-110\Scripts\pip install "sionna-rt==1.1.0"
   ```

2. Add an entry to the repo-root `engines.json`:

   ```json
   {"id": "sionna-rt-1.1.0", "label": "Sionna RT 1.1.0",
    "python": "backend/.venv-sionna-rt-110/Scripts/python.exe",
    "adapter": "sionna_rt"}
   ```

3. Refresh the probe with `GET /api/engines?refresh=true` (or restart the backend).
   In the UI it appears in Results mode → Global → the **Engine** select.

Availability is confirmed by actually running `import sionna.rt` in the target venv
(a cold import can take tens of seconds → cached per process). Engines that are not
installed are shown as disabled in the select.

## Support scope

| adapter | target | status |
|---|---|---|
| `builtin` | sionna-rt in the backend venv (currently 2.0.1) | full features |
| `sionna_rt` | standalone sionna-rt 1.x / 2.x venv | paths solve (verified: 1.2.2 vs 2.0.1 lab_room, 62 paths match) |
| (roadmap) | TF-based sionna ≤ 0.19 | not implemented — requires a Python 3.11 + TensorFlow venv and a dedicated worker. 0.x uses a different `scene.compute_paths()` API and different material handling, so it must be written as a separate adapter, and it also needs a plain-bsdf variant XML rather than this repo's compiler XML (the `itu-radio-material` plugin). |

## Protocol summary

Job: `{kind, xml_path, manifest_path, frequency_hz, max_depth, seed,
num_samples, synthetic_array, flags{...}, txs[], rxs[], material_to_prims{}}`
→ Out: `{ok, engine_version, paths[<RayPath shape>], warnings[], error}`.
For materials, the worker reapplies the ITU plugin of the compiled XML + the constant
material overrides from `compile_manifest.json`. Tests are in `backend/tests/test_engines.py`
(protocol/dispatch/registry verified with a fake worker; no real venv required).
