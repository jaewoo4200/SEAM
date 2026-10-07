# Roadmap

What SEAM Studio has shipped since the MVP vertical slice (unified scene →
material authoring → RF compile → mock/Sionna simulation → result overlay),
and what remains. Milestone numbers follow HANDOFF.md section 12. Remaining
items list concrete next steps grounded in the current code so a future
contributor can start without re-deriving the design. Paths are relative to
`backend/seam_studio/` unless they say otherwise.

## Shipped

### FTC / AODT alignment (from the reference bundle)

Alignment with `reference-bundle/` (the FTC 28 GHz ISAC digital twin):
AODT-style dark viewer palette (LOS cyan / reflection magenta / diffraction
orange, TX red / UE blue, jet radio map); full ITU-R P.2040 material set +
`human_body` presets; 28 GHz default with a >10 GHz ITU-ground safety
warning; RFData export contract (`services/rfdata_export.py`); trajectory RF
metrics (`services/trajectory.py`); Mitsuba/Sionna XML import
(`services/mitsuba_import.py`, ships the imported `lab_room` scene); and
**MIMO beamforming gain** — real TX-MRT + both-ends SVD from the Sionna
channel (`SionnaBackend.simulate_beamforming`, `POST /simulate/beamforming`),
verified ~12 dB (4x4 MRT) / ~24 dB (SVD), matching the 1124 handoff numbers.

### Milestone 8 — Result explorer

The Results panel lists every path (type / power / delay / interaction
count) with overlay filters (strongest N, minimum power, color by
type/power/depth); a selected path shows its vertices and interactions mapped
to canonical prim ids and RF materials, plus delay-vs-power and AoA/AoD
plots. The backend-neutral schemas (`PathResultSet`, `RayPath`,
`PathInteraction` in `backend/seam_studio/schemas/results.py`) carry
everything it needs. Run history labels, loads and prunes stored runs.

### Milestone 9 — Mesh radio maps

Values on actual surfaces (roads, facades, floors, terrain) instead of a
horizontal plane:
- `MeshRadioMapResultSet` (`backend/seam_studio/schemas/results.py`) carries
  `MeshRadioMapSurface` blocks, each `prim_id`-keyed with aligned `centers` /
  `normals` / `values` lists (`None` = not computed);
- `services/mesh_radio_map.py` samples triangle centers from the requested
  prims' meshes and solves probe receivers in chunks through the active
  backend's `simulate_paths`, so the mock and Sionna both work;
- `POST /simulate/mesh-radio-map` and `GET /results/mesh-radio-map`
  (`backend/seam_studio/api/simulate.py`), painted by the frontend's
  `MeshRadioMapOverlay.tsx`; region refinement (`center_xy`/`size_xy`),
  multi-TX `sinr_db` and per-cell serving-TX maps.

### Milestone 11 — Measurement calibration

- Measurement import: `POST /calibrate/measurements/import-csv` (UI:
  Flight-log validation → **Import measurement CSV…**), stored per project
  and listed by `GET /calibrate/measurements`;
- error evaluation along a drive/flight log:
  `POST /calibrate/validate-trajectory`;
- parameter fitting: `POST /calibrate/materials` grid-searches one RF
  material parameter against measured path gains and returns a before/after
  report; with `apply: true` it writes the fitted value to the library,
  promotes the affected prims to `measurement_calibrated` and appends a
  provenance event (`ProjectStore.append_provenance`);
- RF disambiguation of visually identical materials
  (`POST /calibrate/disambiguate`) and material-impact analysis
  (`POST /analyze/material-impact`, KICS 2026), both with UI.

### Milestone 12 — Mobility and dynamic actors

Actor waypoint trajectories (dt or constant-speed pacing, `once` / `loop` /
`pingpong`), UAV actors with free 3D paths, UE trajectories with per-step
handover, terrain following, devices attached to moving actors,
**Simulate scenario** with a timeline scrubber replaying device/actor markers
and ray overlays, and Doppler from actor/device velocities on the Sionna
backend (see `docs/guides/trajectory_uav.md` and `docs/dynamic_scattering.md`).

### Engine-neutral result import and export (AODT)

- Importer: `POST /results/import-aodt` (UI: **Actions ▾ → AODT import
  (parquet)**) reads AODT Parquet ray paths / radio maps and normalizes them
  into the backend-neutral schemas (interaction points and types), stored like
  any run with `backend: "aodt_import"`.
- Exporter: `POST /export/aodt` (UI: **AODT export (parquet)**) writes stored
  paths / sensing results and playback packs as AODT results-schema tables.
- pyarrow is the optional `parquet` extra (`pip install "seam-studio[parquet]"`),
  imported lazily; without it the routes answer 409 with that command.

### Radar sensing and ISAC

RCS targets (TR 38.901 / constant) on any actor, echo solves with per-path
Doppler (`RCSSolver`), sensing over scenario frames with per-link detection,
multistatic position/velocity fusion and EKF tracking, ISAC beam trade-off
(comm vs sensing beam, Pd–rate Pareto, elevation codebooks, inter-TX
interference), sensing coverage maps, Swerling detector models with Monte
Carlo, and a labeled sensing dataset export (npz/csv/parquet); see
`docs/guides/sensing.md`. Association is oracle (the ray tracer labels each
echo); the 1124 handoff's PADP → MPC → DBSCAN front end was not ported.

### CV material split

Material segmentation of a monolithic mesh into per-material faces from a
texture mask (color heuristic, local-VLM tile vote, or an uploaded SAM2-grade
mask; `services/material_segmentation.py`), connected-parts splitting, and
SEAM-Agent's retrieval-augmented per-segment material proposals
(`services/seam_agent.py`). `POST /rf/batch-assign` takes segment → material
mappings from external CV pipelines.

## Remaining

### Milestone 10 — Progressive simulation

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

### Native Sionna mesh radio-map solver

Integrate Sionna RT's native mesh-based radio map solver as a faster path
than probe-receiver sampling (Milestone 9), when available.

### AODT import id remapping and remote engine backends

- Id remapping: translate AODT object identifiers on imported interactions to
  canonical prim ids via `mapping/object_map.json`; today imported
  interactions carry `prim_id: null` (the schema allows it).
- Optionally, a remote-worker backend implementing the `RayTracingBackend`
  protocol for live AODT sessions — `resolve_backend` and the HTTP 409
  unavailable convention already accommodate backends that come and go.

### Novel features backlog

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
7. **Mesh radio maps on facades/floors** (Idea 7, Milestone 9) — shipped
   (above); only the native Sionna mesh solver remains.
8. **LLM scenario authoring** (Idea 8A). An `ai/author-scenario` provider
   returning a validator-guarded typed action list (reuse the strict-JSON +
   confirm-diff pattern). *Effort: med.* The human-target ISAC half (8B) ships
   as the sensing stack above, with oracle association instead of the
   PADP → MPC → DBSCAN front end.
