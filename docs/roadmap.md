# Roadmap

Where SEAM Studio goes after the MVP vertical slice (unified scene →
material authoring → RF compile → mock/Sionna simulation → result overlay).
Milestone numbers follow HANDOFF.md section 12. Each item lists concrete
next steps grounded in the current code so a future contributor can start
without re-deriving the design.

## FTC / AODT alignment (from the reference bundle)

Alignment with `reference-bundle/` (the FTC 28 GHz ISAC digital
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

Planar radio maps (`RadioMapResultSet` storing a `RadioMapGrid` —
origin/cell size/nx/ny at a fixed height) are now complemented by mesh radio
maps, which attach values to actual surfaces (roads, facades, floors,
terrain) instead. This ships end-to-end:
- `MeshRadioMapResultSet` (`backend/app/schemas/results.py`) carries a list
  of `MeshRadioMapSurface` blocks — each `prim_id`-keyed with aligned
  `centers` / `normals` / `values` lists, using the same
  `values: list[Optional[float]]` convention so uncomputed triangles stay
  `null`;
- `ResultSetRef.kind` already includes `"mesh_radio_map"` (it is now a
  `Literal["paths", "radio_map", "mesh_radio_map", "trajectory",
  "scenario"]`);
- `services/mesh_radio_map.py` samples triangle centers from the requested
  prims' meshes (via `mesh_tools`, reusing the compiler's mesh-extraction
  path) and solves probe receivers in chunks through the active backend's
  `simulate_paths`, so it is backend-agnostic — the mock backend and Sionna
  both work with no dedicated mesh solver;
- `POST /simulate/mesh-radio-map` and `GET /results/mesh-radio-map`
  (`backend/app/api/simulate.py`) run and fetch it;
- frontend `MeshRadioMapOverlay.tsx` paints the values as vertex colors on
  the existing GLB meshes.

Remaining:
- integrate Sionna RT's native mesh-based radio map solver as a faster path
  than probe-receiver sampling, when available.

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
  the sample_demo project as the fixed workload.

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

## Novel features backlog

Research-driven feature directions grounded in what the tool already ships,
each a short hop from a working prototype. Full pitches, differentiation,
experiment designs, implementation gaps, and candidate venues are in
`docs/research_ideas.md`; this backlog is the engineering shortlist.

Ordered by paper-value-per-effort (see the shortlist table in
`research_ideas.md`):

1. **CV → RF fidelity evaluation pipeline** (Idea 2). Port a CFR/NMSE +
   trajectory-metric evaluator into a service (reuse `services/trajectory.py`
   aggregation; add CFR via Sionna `Paths.cfr(f)`), and add a scene-variant A/B
   harness that runs the same TX/RX/trajectory across two compiled projections
   and diffs the metrics. Driven by the existing `POST /rf/batch-assign`
   segment→material contract; CV inference stays external. *Effort: low–med.*
2. **Interactive material-sensitivity analysis** (Idea 3). Factor the grid-sweep
   loop out of `services/calibration.py` into a reusable `sensitivity` service
   returning `(material, param, grid, kpi_values)` for arbitrary KPIs; add
   `POST /analyze/sensitivity` and a heatmap/tornado frontend view. Reuse the
   recompile-per-trial correctness verbatim. *Effort: low.*
3. **Provenance-tracked material lifecycle closed loop** (Idea 1). Add a
   lifecycle/provenance-yield report endpoint (aggregate `assignment_status` ×
   surface area), extend `ProjectStore.append_provenance` with lifecycle-
   transition events, and add a loop orchestrator chaining
   `ai/suggest-materials → rf/batch-assign → calibrate/materials`. No schema
   changes. *Effort: low–med.*
4. **A/B scenario-diff radio maps** (Idea 6). `POST /analyze/scenario-diff`
   over two config/scene variants → per-cell ΔKPI grid; diverging-colormap
   overlay. Determinism guarantee (sorted groups, no timestamps) makes the diff
   reproducible and attributable to the single changed field. *Effort: low.*
5. **Monte-Carlo uncertainty / convergence overlays** (Idea 4). Ensemble runner
   over `seed` / `num_samples` reducing to mean/std/count grids
   (`RadioMapEnsembleResultSet`, `None`-hole convention); std/confidence
   colormap toggle; mock-backend per-seed jitter for GPU-free UI. *Effort: med.*
6. **Differentiable calibration UX** (Idea 5). `DifferentiableCalibrationBackend`
   path inside the Sionna backend: mark `mi.traverse` material params trainable,
   Adam over grouped materials with a held-out link split, return a
   `CalibrationReport`-compatible result. Initialize from grid search (item 2)
   and AI suggestions (item 3) to avoid bad-init/local-minima failure. Grid
   search remains the non-GPU fallback. May need a per-material `fitted_values`
   dict on the report schema — flag as a schema contract change. *Effort:
   med–high.*
7. **Mesh radio maps on facades/floors** (Idea 7, Milestone 9). Shipped:
   `MeshRadioMapResultSet` (prim-keyed per-face values, `None` holes),
   `ResultSetRef.kind` extended, measurement surfaces generated from the
   requested prims' meshes via the compiler's mesh path, backend-agnostic
   probe-receiver solve (mock and Sionna both work), vertex-color overlay.
   Remaining: Sionna's native mesh solver as a faster path. *Effort: med.*
8. **LLM scenario authoring / human-target ISAC** (Idea 8). 8A: an
   `ai/author-scenario` provider returning a validator-guarded typed action list
   (reuse the strict-JSON + confirm-diff pattern). 8B: a `TrackingResultSet`
   schema plus the PADP → MPC → DBSCAN → Kalman pipeline ported from
   `rt_isac_paper_pipeline.py`; RF/material sides are ready, DSP is the work.
   *Effort: med (8A) / high (8B).*
