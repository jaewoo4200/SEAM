# Roadmap

Where SionnaTwin Studio goes after the MVP vertical slice (unified scene →
material authoring → RF compile → mock/Sionna simulation → result overlay).
Milestone numbers follow HANDOFF.md section 12. Each item lists concrete
next steps grounded in the current code so a future contributor can start
without re-deriving the design.

## FTC / AODT alignment (from the reference bundle)

Alignment with `sionna-rt-gui-jaewoo-examples/` (the FTC 28 GHz ISAC digital
twin). Status:

- **Done** — AODT-style dark viewer palette (LOS cyan / reflection magenta /
  diffraction orange, TX red / UE blue, jet radio map); full ITU-R P.2040
  material set + `human_body` presets; 28 GHz default with a >10 GHz ITU-ground
  safety warning; RFData export contract (`services/rfdata_export.py`);
  trajectory RF metrics (`services/trajectory.py`); Mitsuba/Sionna XML import
  (`services/mitsuba_import.py`, ships the imported `lab_room` scene); and
  **MIMO beamforming gain** — real TX-MRT + both-ends SVD from the Sionna
  channel (`SionnaBackend.simulate_beamforming`, `POST /simulate/beamforming`),
  verified ~12 dB (4x4 MRT) / ~24 dB (SVD), matching the 1124 handoff numbers.

- **ISAC target tracking** (planned, DSP-heavy) — the 1124 handoff's PADP →
  MPC peak extraction → DBSCAN clustering → Kalman tracking pipeline, with a
  moving `human_target` mesh (material `human_body`, already in the library).
  Next steps: a `TrackingResultSet` schema (GT + estimate + cluster samples,
  per-frame), a target-motion + human-scatterer model, and overlays (green GT,
  yellow clusters, red estimate/trail, black GT path) matching the handoff
  colors. The synthetic PADP/tracking math is a research module ported from
  `rt_isac_paper_pipeline.py`; it is not reproduced here yet.

- **CV material split** (planned, external-model) — SAM2 + DINOv2/CLIP segment
  masks → material labels → per-face mesh split. The RF side is already
  compatible: the material taxonomy matches the CV classes
  (concrete/glass/metal/ground/unknown) and `POST /rf/batch-assign` accepts a
  segment→material mapping, so a CV pipeline can drive assignment today. The
  segmentation/embedding inference itself needs the SAM2/DINOv2 models and is
  out of scope for this environment; the integration point is the batch-assign
  API plus the material-split PLY grouping the compiler already emits.

## Milestone 8 — Result explorer polish (near-term)

The backend-neutral result schemas (`PathResultSet`, `RayPath`,
`PathInteraction` in `backend/app/schemas/results.py`) already carry
everything the explorer needs, including per-interaction prim ids and
optional `aod_deg`/`aoa_deg`.

Next steps:
- path table with filtering by `path_type`, interaction `rf_material_id`,
  and interaction `prim_id` (all present in the schema — pure frontend work);
- selected-path inspector and delay/power scatter plot;
- click-through from a path interaction to the prim in the scene tree
  (interactions already reference canonical prim ids).

## Milestone 9 — Mesh radio maps

Today radio maps are planar grids: `RadioMapResultSet` stores a
`RadioMapGrid` (origin/cell size/nx/ny at a fixed height). Mesh radio maps
attach values to actual surfaces (roads, facades, floors, terrain) instead.

Next steps:
- add a `MeshRadioMapResultSet` schema: `prim_id`-keyed blocks of per-face
  (or per-vertex) values, mirroring the `values: list[Optional[float]]`
  convention so uncomputed entries stay `null`;
- extend `ResultSetRef.kind` with `"mesh_radio_map"` (currently a
  `Literal["paths", "radio_map"]` — a deliberate MVP restriction);
- generate measurement surfaces from prims tagged `road` / `terrain` /
  `building` (the demo scene already tags all prims accordingly), reusing
  the compiler's mesh-extraction path in `rf_compiler`;
- integrate Sionna RT's mesh-based radio map solver when available; the mock
  backend gets a deterministic surface-distance falloff so the frontend
  overlay can be built first;
- frontend: heatmap overlay as vertex colors on the existing GLB meshes,
  compared side-by-side with the planar map.

## Milestone 10 — Progressive simulation

Goal: coarse result in seconds on consumer hardware, refinement afterwards.
Two hooks for this already exist: `RadioMapResultSet.values` allows `None`
holes ("progressive refinement leaves holes rather than fabricating
values"), and `SimulationConfig.num_samples` is an explicit ray budget.

Next steps:
- coarse-to-fine scheduler: run at large `radio_map.cell_size_m`, then
  subdivide cells near the viewport / near high-gradient regions;
- tile/cache manager service keyed by (config hash, tile coords) so
  re-runs after unrelated scene edits reuse tiles;
- API shape: either a job endpoint with incremental result revisions, or
  server-sent events pushing partial `RadioMapResultSet` updates; result
  files stay immutable per the `results/<result_id>.json` convention, with
  refinement writing successive result ids;
- a time-to-first-result benchmark script under `examples/scripts/` using
  the kaist_demo project as the fixed workload.

## Milestone 11 — Measurement calibration

The provenance model was designed for this from day one:
`measurement_calibrated` is already the top of the `AssignmentStatus`
lifecycle, and `RFMaterial.model == "constant"` gives calibrated parameters
a place to live (`relative_permittivity`, `conductivity_s_per_m`).

Next steps:
- measurement import: CSV of (position ENU, rss_dbm | path_gain_db) into a
  `measurements/` folder with its own small schema;
- error evaluation: compare measurements against the latest
  `RadioMapResultSet` / `PathResultSet`; render an error heatmap with the
  same overlay machinery as radio maps;
- parameter fitting: optimize constant-model material parameters (and
  per-prim `thickness_m` overrides) to minimize error; scipy-free first pass
  can be a coarse grid/Nelder-Mead over 2–3 parameters;
- on acceptance, update the material in `rf/materials.yaml`
  (`builtin: false`), promote affected prims to `measurement_calibrated`
  with `assignment_sources` extended (e.g. `[..., "calibration:run_003"]`),
  and write `results/calibration_report.json` plus a `provenance.json`
  event via `ProjectStore.append_provenance`.

## Milestone 12 — Mobility and dynamic actors

Devices already carry `position` and `orientation_deg`; what is missing is
time.

Next steps:
- trajectory schema: per-device list of `(t_s, position, orientation_deg)`
  keyframes, imported from CSV/GPX, stored in the scene or a sidecar file;
- time-indexed results: a timeline container that maps `t_s` to result ids,
  reusing the existing immutable per-run result files rather than inventing
  a new storage format;
- batch runner that sweeps the trajectory through the existing
  `simulate_paths` path (mock backend first — it is deterministic, so
  playback tests are stable);
- frontend timeline scrubber replaying device markers and path overlays;
- dynamic scatterer placeholders (moving vehicles as boxes with RF
  bindings) once static mobility works.

## Engine-neutral result import (AODT and others)

Not a numbered milestone but a standing design constraint (HANDOFF 7.3 /
19.5): result schemas stay backend-neutral so high-end engine outputs can be
compared against local Sionna runs.

Next steps:
- an importer service that reads AODT Parquet path/CIR outputs and
  normalizes them into `PathResultSet` (pyarrow is already an anticipated
  optional dependency; import it lazily like Sionna and Ollama);
- id remapping: translate AODT object identifiers to canonical prim ids via
  `mapping/object_map.json`, leaving `PathInteraction.prim_id` as `null`
  when no mapping exists (the schema explicitly allows this);
- imported results are stored like any run: `results/<result_id>.json` with
  `backend: "aodt_import"` in the `ResultSetRef`, so the result explorer and
  latest-by-ref logic need no changes;
- optionally, a remote-worker backend implementing the `RayTracingBackend`
  protocol for live AODT sessions — `resolve_backend` and the HTTP 409
  unavailable convention already accommodate backends that come and go.
