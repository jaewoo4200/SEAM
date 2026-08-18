# ML datasets and exports

> **English** · [한국어](datasets_export.ko.md)

SEAM Studio is not just a viewer — everything you simulate can leave the app as
files: NumPy `.npz` ground-truth datasets for training ML models, an
AODT-viewer-compatible RFData bundle, WYSIWYG viewport captures, and per-chart
CSV/PNG/SVG exports. This guide walks through each export path.

Everything here works with the **Mock backend** too — if Sionna RT is not
installed, dataset generation and all exports still run end to end (the numbers
come from the mock solver, which is fine for testing pipelines).

---

## 1. The ML dataset panel

The dataset generator lives in the **ML dataset** panel (its header renders as
`ML DATASET`). You can reach it two ways:

- **Results** mode — it is one of the dockable cards on the right.
- From any mode via the toolbar **Panels ▾** menu — click the **ML dataset**
  row to float it over the viewport, or dock it left/right with the **◧ / ◨**
  buttons. A floated panel stays visible when you switch modes.

![Floating ML DATASET panel over a radio-map heatmap, showing sampling controls, region fields, the Generate dataset button, and two existing dataset rows with npz/json links](../images/10_dataset.png)
*The ML DATASET panel floated over a computed radio map — sampling controls on top, the region picker in the middle, and the dataset list with `npz` / `json` downloads at the bottom.*

### Generate a dataset step by step

1. **Name** — the dataset's display name (it also appears in the list below).
2. **Sampling mode** — how UE positions are chosen:
   - `random` — uniform random positions inside the region,
   - `grid` — a regular grid over the region (adds a **Grid spacing** field, in m),
   - `trajectory` — points along a straight start→end line.
3. **Actor flight path** — optionally sample along a scene actor's authored
   trajectory (a car, pedestrian, or UAV with waypoints). Pick an actor here and
   it **overrides** the region / start-end below; leave it at `— none —` to use
   the sampling mode's own geometry. If no actor has a trajectory yet, the panel
   says so — assign one in Visual mode first. The hint under the field spells
   out the precedence: *waypoints > actor flight path > start/end (or region)*.
4. **dt** (s) — the finite-difference time step behind the velocity / Doppler
   labels the backend adds to moving samples.
5. **Num samples** — how many UE positions to solve (1–20000).
   **CFR points** — frequency-response samples per position (2–4096).
   **Height** (m) — the UE sampling height.
6. Set the region (for `random` / `grid`):
   - **⌖ Pick region in viewport** — the button switches to
     `Click 2 corners… (Esc)`; click two opposite corners of the region on the
     scene surface and the XY fields fill in (Esc cancels).
   - **Fit to scene** — sets the region and height to cover the whole scene
     (the panel also seeds itself from the real scene bounds when a project
     loads, so you rarely start from garbage values).
   - **Region min** / **Region max** — the XY corners as numbers.
   - Watch the hint line, e.g. *"Scene spans [-40.0, -40.0]…[40.0, 40.0] m —
     samples outside it get zero paths."* Sampling outside the geometry is the
     #1 cause of useless datasets.

   For `trajectory` mode you instead get **⌖ Pick path in viewport** plus
   **Start** / **End** XYZ fields.
7. **Seed** — makes the random sampling reproducible; record it for papers.
8. **Include paths** — additionally dumps every sample's full ray paths
   (vertices + interactions) as `paths.jsonl`. Large; off by default.
9. **Follow terrain** — snaps each sample's height to the surface below it
   plus the height offset. Use it on sloped outdoor scenes; leave it off
   indoors (it would snap to the roof).
10. Press **Generate dataset**. The button shows `Generating…` while the solver
    sweeps the positions.

### The dataset list

Finished datasets appear in the table below the button, one row each:

- **name / # / created / size** — name, sample count, creation time, file size.
- **files** — download links for **npz** (`dataset.npz`, the arrays) and
  **json** (`metadata.json`, the config echo + conventions).
- A **⚠ N zero-path** flag on the name means N samples produced no paths at
  all (UE outside the scene or fully occluded) — re-check your region.
- The **×** button deletes a dataset; it arms to **✓?** and you click again to
  confirm (it auto-disarms after a few seconds).

On disk, datasets live under the project folder at
`export/datasets/<dataset_id>/`.

### What is in the labels

The `.npz` contains per-sample positions, complex CFR, per-path CIR gains and
delays, LOS flags, RSS, and dispersion metrics — the exact array schema, the
AODT field mapping, and a ready-to-run training example
(`examples/ml/train_channel_estimator.py`) are documented in
[ML ground-truth datasets](../ml_datasets.md).

---

## 2. RFData export (AODT-viewer bundle)

To hand results to an external AODT-style viewer or your own pipeline, use the
toolbar: **Actions ▾ → Export RFData**. It writes a bundle to
`export/rfdata/` inside the project folder:

| File | Content |
|---|---|
| `scenario_meta.json` | units, frequency, coordinate transform, time window |
| `devices.json` | transmitters + receivers (positions in meters) |
| `paths.json` | time-indexed ray paths |
| `trajectory.csv` | per-waypoint UE metrics (`time_s, ue_id, x_m, y_m, z_m, rss_dbm, sinr_db, path_gain_db`) |
| `radio_map.csv` | plane heatmap samples |
| `calibration_points.json` | 3 coordinate-check reference points |

After the export, a dismissible row appears in **Results** — *"Exported RFData
to `export/rfdata`"* — with a download link per file, so you don't have to dig
through the project folder.

### AODT results-schema export

The bundle above is the *viewer* contract. For tooling written against NVIDIA
AODT's own [results schemas](https://docs.nvidia.com/aerial/aodt/), there is a
second export that writes those tables as Parquet:

```
POST /api/projects/{project_id}/export/aodt
     {"source": "paths", "result_id": null, "fft_size": 64,
      "subcarrier_spacing_hz": 30000}
```

It writes one `<table>.parquet` per AODT table into `export/aodt/`:
`ues`, `scatterers`, `rus`, `dus`, `panels`, `patterns`, `time_info`, `cfrs`,
`cirs`, `raypaths` — column names and types verbatim from the AODT docs.
`source: "paths"` writes a single snapshot (`time_idx` 0) from a stored paths
result; `source: "playback"` writes one `time_idx` per frame of a stored
playback pack, so `time_info`, `raypaths`, `cirs` and `cfrs` span the drive and
the UE's `route_*` columns carry the frame positions.

Two AODT tables are **never** written: `telemetry` and `ran_config`. Both are
RAN-simulation outputs (scheduler/PHY KPIs, gNB configuration) that a
ray-tracing pipeline does not produce — emitting empty ones would claim
coverage SEAM does not have.

Alongside the tables, `id_map.json` records the string ↔ integer id mapping the
Parquet rows cannot carry: device id → `ru_id`/`ue_id`, prim id → `prim_ids`
index, actor id → scatterer `ID`, and device → panel index.

Caveats worth knowing before you consume the tables:

- `normals` is all zeros — SEAM records the interaction point and the prim it
  belongs to, never the surface normal there; `object_ids` repeat `prim_ids`
  (there is no separate USD object table).
- `ru_ant_el`/`ue_ant_el` are `(0, 0[, 0])` and each `ampl_*` list has one
  entry: the path solver resolves per-link coefficients, not per-element ones.
- `patterns` is a single isotropic placeholder row.
- Amplitudes (and therefore `cirs`/`cfrs`) are channel coefficients —
  |a| = 10^(path_gain_dB/20), phase from the path's carrier phase — not
  received power. `cir_delay` is in **seconds**, per the AODT schema.
- In `raypaths`, `points` is the whole polyline (TX, every interaction, RX), so
  `normals`/`prim_ids`/`object_ids`/`vegetation_depths` are parallel to it,
  while `interaction_types` is `"emission"` plus one entry per bounce — the
  arrival at the RX is not an interaction.

This export needs `pyarrow` (`pip install "seam-studio[results]"`); without it
the endpoint answers **409**, and it answers **404** when the project has no
stored result of the requested source kind. The written `raypaths.parquet`
reads back through SEAM's own AODT importer (`POST /results/import-aodt`).

---

## 3. Channel dataset (`.npz`) — the AODT/HYRAY per-link layout

The ML dataset in section 1 is a *per-position* dataset (CFR, aggregated KPIs).
When you need the raw **per-link multipath** in the layout NVIDIA AODT / HYRAY
reference files use — one row per UE, one column per TRP, one entry per path —
use the toolbar: **Actions ▾ → Channel dataset (.npz)**. It writes
`export/channel_npz/channel_dataset.npz` plus a `metadata.json` sidecar.

The UE grid is chosen for you: the **latest stored trajectory run** if the
project has one (many UE rows), otherwise the scene's **receiver devices** —
which is exactly what a UE list imported through `POST /import/devices` lands
in. The transient notice and the dismissible **Results** row both say which grid
was used, together with the link count. Because the export runs one paths solve
per UE, it reports progress in the solve-progress card and is cancellable.

The npz carries the 13 canonical keys, nothing more:

| key | shape | dtype |
|---|---|---|
| `num_TRP`, `num_trajectory`, `batch` | scalar | `int64` |
| `dataset_TRP_pos` | (T, 3) | `float32` |
| `dataset_TRP_ori_angle` | (T, 2) | `float32` |
| `dataset_UE_pos` | (U, 3) | `float32` |
| `sorted_dataset_ampl` | (U, T, P) | `complex128` |
| `sorted_dataset_azimuth` / `_elevation` | (U, T, P) | `float32` |
| `sorted_dataset_toa` | (U, T, P) | `float32` |
| `is_nlos` | (U, T) | `bool` |
| `ue_id`, `time_idx` | (U,) | `int32` |

T = transmitters, U = UE positions, P = `max_paths` (default 500). Each link's
path axis is sorted **strongest-first** by |amplitude| and zero-padded to P; a
link the solver found no path for stays all-zero with `is_nlos = True`.

Conventions you must know before consuming the arrays:

- `sorted_dataset_ampl` is the linear complex channel amplitude
  `10^((path_gain_db + normalization_db)/20) · e^{j·phase_rad}` — **not**
  received power. `normalization_db` defaults to `0.0` (SEAM's own physical
  gain); the lab's AODT files carry a fixed **−5.06 dB** offset, so pass
  `normalization_db: -5.06` to match them numerically.
- `sorted_dataset_azimuth` / `_elevation` are **departure** angles in the
  emitting TX's **local array frame**, in **radians**: azimuth is
  `atan2(y_local, x_local)` in (−π, π], elevation is the **zenith** angle
  `arccos(z_local)` in [0, π]. The local frame comes from that TX device's own
  `orientation_deg` ([yaw, pitch, roll] degrees) through
  `R = Rz(yaw)·Ry(pitch)·Rx(roll)` — the exact matrix sionna-rt builds from the
  orientation SEAM hands its `Transmitter`, so the dataset's angles and the
  solver's array steering share one frame.
- `sorted_dataset_toa` is absolute propagation delay in **seconds**.
- `dataset_TRP_ori_angle` is `[yaw_deg, pitch_deg]` per TX, in **degrees**
  (matching the reference files, whose orientation pair is degree-valued while
  the per-path angle arrays are radians).

The whole surface is available over HTTP for scripted runs:

```
POST /api/projects/{project_id}/export/channel-npz
     {"ue_source": "explicit",
      "ue_positions": [[25, 5, 1.5], [40, -12, 1.5]],
      "tx_ids": null,          // null = every tx device, in scene order
      "max_paths": 500,
      "normalization_db": 0.0,
      "ue_ids": null, "time_idx": null, "batch": 0}
```

`ue_source` is `"explicit"` (the `ue_positions` list), `"devices"` (every `rx`
device, ordered by id) or `"trajectory"` (a stored trajectory result's
per-sample positions — `ue_result_id` picks one, `null` takes the latest).
`ue_ids` / `time_idx` are optional passthrough columns (defaults `arange(U)` and
`zeros(U)`); when given they must be exactly U long. A single export is capped
at 5000 UE positions.

The export **never touches your scene**: each UE is solved on an in-memory copy
whose only receiver is an ephemeral probe, and no result set is persisted — only
`export/channel_npz/` is written, plus one `export_channel_npz` provenance
event.

> Pair this with the **Max paths / TX** field in the Paths solver panel
> (`max_num_paths_per_src`) when you want the solver itself to stop at the same
> path count the export keeps.

---

## 4. Viewport captures — Snapshot and Render

The two icon buttons in the bottom-right cluster of the viewport save scene
images:

- **Snapshot** (camera icon) — saves *exactly* what you see (WYSIWYG): current
  camera pose, rays, markers, radio-map overlay, at full canvas resolution, as
  PNG. The tooltip reads *"Save this exact view as a PNG (what you see, full
  resolution — paper-ready)"*. This is the button for paper and slide figures.
- **Render** (film icon) — an offline, physically shaded path-traced render via
  Mitsuba. Slower, and deliberately *not* the on-screen view — no rays or
  overlays, just the shaded scene.

The entity **POV inset** (the live first-person view from a device or actor)
has its own camera button that saves the POV frame as a full-resolution PNG.

---

## 5. CSV and figure exports from the dashboards

- Every chart in the **Metrics dashboard** panel (and the other paper-styled
  charts) sits in a frame with **PNG / SVG / CSV** buttons in its header —
  bitmap at 3×, vector, or the raw data as CSV. Figures export as shown: white
  background, Times New Roman.
- The dashboard header has an **Export all (CSV)** button that downloads the
  entire KPI table as `metric,value,unit` rows.
- The paths table in **Results** has **Export filtered CSV (N)** — it exports
  exactly the currently filtered path set, one row per path with type, power,
  delay, and interaction materials.

---

## Related docs

- [ML ground-truth datasets](../ml_datasets.md) — the `.npz` schema, zero-path
  warnings, AODT field mapping, and the training example script.
- [Getting started](getting_started.md) — install, first project, the mode tabs.
- [Simulation guide](simulation.md) — paths, radio maps, and the solver
  settings a dataset inherits.
- [Scene & project format](../scene_format.md) — where files live inside a
  project folder.
- [Sionna versions](../sionna_versions.md) — the `engine` recorded in
  `metadata.json` for reproducibility.
- [15-minute tutorial](../../TUTORIAL.md) — the full first-session loop,
  including dataset generation.
