# ML Ground-Truth Datasets (AODT-style research loop)

> **English** · [한국어](ml_datasets.ko.md)

The core reason communications researchers use AODT — **training and validating AI
algorithms (channel estimation, beam prediction, LOS classification, localization)
against simulation ground truth** — is carried out directly in this tool. By
sweeping a UE across positions in the scene, ray-tracing ground truth is collected
and exported as NumPy `.npz`.

## How to generate

- **UI**: Results mode → "ML dataset" section → configure sampling
  (random/grid/trajectory), number of samples, CFR points, region → Generate.
  When finished, download `dataset.npz` / `metadata.json` from the list.
- **API**: `POST /api/projects/{pid}/datasets/generate`
  (schema: `backend/app/schemas/datasets.py`). The backend supports both mock
  (no GPU required, for testing) and sionna (real, with the `engine` field to
  select the sionna-rt version).
- File location: `<project>/export/datasets/<dataset_id>/`.

## Sampling region configuration

- **Auto-seed from scene bounds**: If you leave the sampling region
  (`region_min`/`region_max`) empty, it no longer guesses ±25 m but instead uses
  the **actual scene AABB** (`GET /api/projects/{pid}/scene/bounds`, service:
  `scene_bounds.compute_scene_bounds`). The AABB is the visual GLB's world bounds
  merged with device/actor positions, which eliminates the all-zero dataset problem
  (audit F3) where, in small indoor scenes, the region would fall entirely outside
  the geometry. The UI pre-fills these bounds.
- **Fit to scene**: The *Fit to scene* button in the dataset panel calls the bounds
  API above and fills `region_min`/`region_max` with the whole scene.
- **Pick region in viewport**: *Pick region in viewport* lets you specify a region
  by clicking two points (diagonal corners) in the viewport — the clicked XY builds
  an AABB and z is left at `height_m`. Use it to narrow down to just a region of
  interest while looking at the scene geometry.
- **Follow terrain**: When `sampling.follow_terrain=true`, the z of each sample
  position is snapped to the terrain surface below it (a vertical raycast against
  the visual mesh) and then `height_m` is added (service:
  `terrain.snap_to_terrain`). Turn it on to keep the antenna height constant over
  sloped terrain (e.g., FTC outdoor). **For indoor scenes it should be off** — with
  a roof or multiple floors, it snaps to the highest hit (the roof). Points outside
  the mesh (no surface underfoot) keep their z as-is and leave a single summary
  warning.

## Array layout (`dataset.npz`)

| key | shape/dtype | meaning |
|---|---|---|
| `positions_m` | `[N,3] f32` | UE positions (Z-up, m) |
| `tx_position_m` | `[3] f32` | fixed TX position |
| `cfr` | `[N,K] c64` | channel frequency response H(f_k) |
| `cfr_freq_offset_hz` | `[K] f64` | [-B/2, +B/2] offset |
| `cir_gain` | `[N,P] c64` | per-path complex voltage gain (zero-padded) |
| `cir_delay_ns` | `[N,P] f32` | per-path delay (NaN-padded) |
| `num_paths` | `[N] i32` | number of valid paths |
| `los` | `[N] bool` | whether a LOS path exists (classification label) |
| `rss_dbm` | `[N] f32` | total received power |
| `mean_delay_ns`, `rms_delay_spread_ns`, `k_factor_db` | `[N] f32` | dispersion/Rician metrics (NaN=undefined) |

`H(f_k) = Σ_l g_l·exp(-j2πf_k·τ_l)` — this is the same tap model as the interactive
channel analysis panel, so **the values seen in the panel and the dataset samples
agree by construction**. `metadata.json` records a config echo, the backend/engine
(sionna-rt version), the sampling spec, the coordinate/unit conventions, and the
**AODT ClickHouse schema (cirs/cfrs/raypaths) field mapping** (`aodt_field_map`).
Because the field semantics are aligned with AODT (cir_re/cir_im ↔ real/imag of
cir_gain, etc.), reusing an AODT pipeline is easy. Per-antenna-element tensors
(`[N_time, N_tx_ant, N_rx_ant, N_freq]`, AODT's `ru_ant_el/ue_ant_el` structure)
are on the roadmap — for now, per-link (antenna-axis aggregated) ground truth is
exported.

## Zero-path warning

A dataset full of samples with no paths at all **looks like a success (200 + file
created) but is essentially garbage** — usually caused by the sampling region
falling outside the scene geometry. The generator detects this and warns loudly:

- **All zero**: leaves an `ALL {n} samples produced zero paths — ...` warning and
  advises re-picking the region with scene bounds (`GET /scene/bounds` or the UI's
  *Fit to scene*) and regenerating.
- **Some zero**: `{k}/{n} samples produced zero paths (...)` — the cfr/labels of
  those samples are 0/NaN.

In both cases, **`num_zero_path_samples`** (the count of zero-path samples) is
recorded in `metadata.json` and `DatasetInfo.metadata`, so a pipeline can check it
programmatically and filter out bad datasets.

## Training example

`examples/ml/train_channel_estimator.py` — loads a dataset, sets up a pilot-based
channel estimation task, and compares an LS baseline (numpy) with a small MLP (if
PyTorch is installed):

```powershell
backend\.venv\Scripts\python.exe examples/ml/train_channel_estimator.py examples/demo_project/sample_demo.sionnatwin/export/datasets/<dataset_id>/dataset.npz
```

Run it with `<dataset_id>` replaced by the actual dataset id shown in the UI (the
ML dataset list in Results mode) or in the generated `metadata.json`.

If PyTorch is not present, only the LS/LMMSE baseline runs (enable it with
`pip install torch`). Adjust `PILOT_SPACING`, `SNR_DB` at the top of the script to
change the experimental conditions.

## Reproducibility checklist

When using this for a paper, report metadata.json's `engine` (sionna-rt version —
see [sionna_versions.md](sionna_versions.md)), `config` (mechanism flags, seed,
number of samples), and `sampling.seed` together.
