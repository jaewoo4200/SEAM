# Architecture

SEAM Studio is a local-first workbench for authoring RF-aware digital
twins on top of Sionna RT. Everything runs on a consumer machine: a FastAPI
backend over plain project folders, a React/Three.js frontend, and optional
local extras (Sionna RT, Ollama) that degrade gracefully when absent.

## Unified scene graph and its two projections

There is exactly one source of truth per project: `scene.sionnatwin.json`,
the canonical unified scene graph (`app.schemas.scene.Scene`). The visual
and RF sides are *projections* compiled from it — never independent files
that can drift.

```text
                 scene.sionnatwin.json  (canonical Scene)
                 prims / devices / configs / result refs
                 each prim: mesh_ref + visual binding + rf binding
                        │
        ┌───────────────┴────────────────────┐
        ▼                                    ▼
  Visual projection                    RF projection
  visual/scene.glb (+textures)         rf/generated_scene.xml (Mitsuba)
  named meshes, PBR materials          rf/meshes/*.ply grouped by RF material
  rendered by Three.js frontend        consumed by Sionna RT / mock backend
        │                                    │
        └────────────── prim ids ────────────┘
          every mesh, warning, suggestion and ray-path
          interaction maps back to a canonical prim id
```

A mesh primitive carries two independent material bindings:

- `visual`: PBR appearance from the GLB (name, base color, texture). Used for
  rendering and as *suggestion evidence* only.
- `rf`: electromagnetic material (`rf.material_id` into the project's RF
  material library) plus provenance (`assignment_status`,
  `assignment_sources`, `confidence`).

Visual material info is never used as RF truth. A texture called
`concrete.jpg` can *suggest* `itu_concrete`, with tracked provenance, but
only a user confirmation (or a future calibration run) promotes it.

## Module map

```text
backend/app/
  main.py                     FastAPI factory; mounts routers under /api
  core/
    config.py                 env-driven Settings (project roots, AI config)
    paths.py                  repo anchors, default project roots
  schemas/                    canonical Pydantic v2 contracts (wire format)
    common.py scene.py devices.py materials.py simulation.py
    results.py ai.py validation.py compile.py projects.py
  services/
    project_store.py          project folder persistence (atomic writes)
    availability.py           import-light optional-dependency probes
    scene_validator.py        validate_scene(scene, library, project_dir)
    material_assignment.py    assign_materials(scene, request, library)
    rf_compiler.py            compile_project(project_dir, scene, library)
    ai_provider.py            suggestion providers + fallback chain
    simulation_backends/      RayTracingBackend protocol, mock + sionna
  api/
    health.py projects.py scene.py materials.py ai.py compile.py simulate.py
  data/default_rf_materials.yaml   built-in RF material library

frontend/src/
  types/api.ts                TypeScript mirror of the Pydantic schemas
  (viewer, scene tree, inspector, RF overlay, AI panel, result explorer)

examples/
  scripts/create_demo_project.py   generates examples/demo_project/sample_demo.sionnatwin
```

## Request flows

### Assign an RF material

```text
POST /api/projects/{id}/rf/assign            body: AssignRequest
  deps.get_store() -> load_scene_or_404      404 if project unknown
  store.load_materials(id)                   project rf/materials.yaml
  material_assignment.assign_materials(scene, request, library)
      mutates prims in place; UnknownMaterialError -> 404
  store.save_scene(id, scene)                atomic write
  -> AssignResponse {updated_prim_ids, skipped_prim_ids, warnings}
```

Batch assignment (`/rf/batch-assign`) repeats the same mutation for each
`AssignRequest` before a single save.

### Compile the RF projection

```text
POST /api/projects/{id}/compile/sionna
  rf_compiler.compile_project(project_dir, scene, library)
      1. scene_validator.validate_scene(...)  -> ValidationReport
         warnings do NOT block; errors abort the compile
      2. group mesh prims by rf.material_id   (Mode 2 grouping)
      3. export rf/meshes/<group>.ply         world-space, Z-up ENU
      4. write rf/generated_scene.xml         Mitsuba XML for Sionna RT
         bsdf ids use the "mat-" prefix: bsdf id "mat-itu_concrete"
         binds shapes to Sionna RadioMaterial "itu_concrete"
  -> CompileResult {material_groups, generated_files, validation, warnings}
```

### Simulate

```text
POST /api/projects/{id}/simulate/paths       body: SimulateRequest
  resolve SimulationConfig (inline config wins over config_id; 404/400)
  simulation_backends.resolve_backend(config)
      "auto"  -> sionna when importable, else mock
      "sionna" when not installed -> BackendUnavailableError -> HTTP 409
  backend.simulate_paths(...) -> PathResultSet (backend-neutral schema)
  persist results/<result_id>.json
      result_id = f"{backend_name}_{kind}_{n:03d}",
      n = 1 + count of existing refs of that kind in scene.result_sets
  append ResultSetRef to scene.result_sets; save scene
  -> PathResultSet
GET /api/projects/{id}/results/paths         latest = last ref of that kind
```

`simulate/radio-map` follows the same shape with `RadioMapResultSet`.

## Result schemas, reproducibility, and events

The backend-neutral result models (`app.schemas.results`) carry more than raw
power. What the frontend reads out of them:

### Angle of arrival / departure (AoA / AoD)

Every `RayPath` carries `path_gain_db` (per-path channel gain = `power_dbm`
minus the configured TX power, so links compare independent of transmit power)
and two angle pairs:

- `aod_deg = [azimuth_deg, elevation_deg]` — direction of **departure** at the
  TX.
- `aoa_deg = [azimuth_deg, elevation_deg]` — direction of **arrival**, pointing
  *from the RX toward where the ray came from*.

Azimuth is `atan2(y, x)` about `+Z`; elevation is up from the XY plane. Both
default to `null` when a backend cannot resolve them. The frontend
`AngularPlot` renders these as a polar scatter — azimuth = polar angle, path
power = radius (inner ring weakest), AoD as filled markers and AoA as hollow —
with elevation carried in the CSV export and each marker's tooltip.

### Multi-TX radio maps: SINR and serving cell

`RadioMapResultSet.metric` is one of `path_gain_db | rss_dbm | sinr_db`
(`sinr_db` models true co-channel interference `S/(I+N)`; with a single TX it
degenerates to SNR). Multi-TX maps also fill:

- `tx_ids: string[]` — every TX that contributed, in solver order.
- `serving_tx: (number|null)[][]` — row-major per-cell index into `tx_ids` for
  the strongest TX at each cell (only populated for multi-TX scenes, so single
  TX payloads stay small).

`values` is row-major `[ny][nx]`; `null` marks a cell that was not computed
(progressive refinement leaves holes rather than fabricating values).

### Mesh radio map (surface coverage)

`POST /projects/{id}/simulate/mesh-radio-map` paints coverage on actual mesh
surfaces (facades, floors, roads) instead of a horizontal plane. The request
`MeshRadioMapRequest {prim_ids (≥1), tx_id?, metric: path_gain_db|rss_dbm
(default rss_dbm), max_triangles=2000, offset_m=0.05}` places a probe RX at
each triangle center, offset `offset_m` along the face normal. The response
`MeshRadioMapResultSet` holds one `MeshRadioMapSurface` per prim with aligned
`centers` / `normals` / `values` lists (so the viewer never has to reproduce
the backend's triangle ordering) and a `sample_stride > 1` when a mesh exceeded
`max_triangles` and every k-th triangle was sampled. Latest fetch:
`GET /projects/{id}/results/mesh-radio-map?result_id=`.

### Region refinement

`RadioMapGridConfig` adds `center_xy` / `size_xy` (`[x, y]` in meters, both
optional; `null` = auto-fit to scene geometry). A caller re-solves a selected
region at a finer `cell_size_m` by passing an explicit center/size instead of
recomputing the whole map — the refined values overwrite that region's cells.

### Reproducibility hashes

Every persisted result's `metadata` is stamped with content hashes so a stale
result is detectable (`simulate.py::_provenance_hashes`):

- `scene_hash` — the canonical scene **minus** `result_sets` (results must not
  churn the hash of the scene that produced them).
- `rf_assignment_hash` — just `(prim_id, material_id, assignment_status)`, so a
  pure material re-assignment is detectable on its own.
- `sim_config_hash` — the exact solver knobs; the full `config_snapshot` is
  stored alongside.

The frontend compares a result's stamped hashes against the live scene to badge
results as stale when the scene or assignments have moved on since the solve.

### Backends and capabilities

`GET /api/backends` returns `[{name, available, detail, capabilities}]` for
capability-aware UIs. `capabilities` is a stable, additive feature map
(`paths`, `radio_map`, `mesh_radio_map`, `cir`, `beamforming`, `doppler`,
`diffraction`, `gpu`, …); **frontends treat a missing key as `false`**. The
`mock` backend is always available; `sionna` reports `available: false` with a
"not installed (optional)" detail when Sionna RT is not importable.

### Live events (WebSocket)

`WS /ws/projects/{id}/events` (mounted **without** the `/api` prefix) streams
JSON frames as work runs: a one-shot `{type: "connected"}` hello, then
`compile_started` / `compile_finished`, `simulation_started` /
`simulation_finished` (the finished frames carry `kind`, `result_id`,
`backend`). The frontend uses these to show live compile/solve progress without
polling.

### AODT import

`POST /projects/{id}/results/import-aodt` reads NVIDIA AODT parquet exports from
a server-local `source_dir` (`kinds: ["paths", "radio_map"]`), normalizes them
into the same result schemas, remaps AODT object ids to canonical prim ids via
`mapping/object_map.json`, and persists them through the shared
`_persist_result` helper — so imported sets get canonical ids, provenance
hashes, and a `ResultSetRef` with `backend: "aodt_import"` exactly like a local
solve. Returns 409 when `pyarrow` is not installed.

### Measurement CSV import

`POST /projects/{id}/calibrate/measurements/import-csv {csv_text}` parses
measured per-link samples (RX position + measured path gain) into
`MeasurementSample`s (each with an optional `measurement_id`), reporting
`skipped` rows and `warnings`; `GET /projects/{id}/calibrate/measurements`
returns the stored set. These feed material calibration and RF disambiguation
(see `docs/ai_assistant.md`, `docs/accuracy.md`).

### OpenStreetMap import

`POST /projects/import-osm {name, lat, lon, width_m, height_m, ...}` builds a
ready-to-simulate outdoor project from a geographic rectangle in one shot
(`app.services.osm_import`). It fetches building footprints from the Overpass
API, projects each way's lon/lat ring to local ENU meters via an
equirectangular tangent-plane approximation about the center (sub-metre for the
≤3 km rectangles it allows), extrudes the footprints with
`trimesh.creation.extrude_polygon` (height from the OSM `height` /
`building:levels` tags, else a default), and drops a thin ground plane under
them. The result is one named geometry per building in a single
`visual/scene.glb` (matching each prim's `mesh_ref.mesh_name`), one prim per
building tagged `building` plus a `ground`/`terrain` prim, all RF-bound to the
requested materials with status `rule_suggested` and source `osm_import`, and a
`coordinate_system.origin_lat_lon_alt` anchoring the scene. Both material ids
are validated against the default library (400 on unknown); footprints with
fewer than three points or invalid polygons are skipped and counted, and above
2000 buildings only the largest-area ones are kept (warned). The Overpass
endpoint is overridable via `SEAM_OVERPASS_URL` (`SIONNATWIN_OVERPASS_URL`
fallback); an unreachable endpoint or garbage response returns 502 and a
timeout returns 504.

## Simulation backend interface

All backends implement one protocol
(`app.services.simulation_backends`):

```python
class RayTracingBackend(Protocol):
    name: str
    def is_available(self) -> bool: ...
    def simulate_paths(...) -> PathResultSet: ...
    def simulate_radio_map(...) -> RadioMapResultSet: ...

get_backend(name: str) -> RayTracingBackend
resolve_backend(config: SimulationConfig) -> RayTracingBackend   # 409 on unavailable
available_backends() -> list[HealthBackendStatus]                # feeds /api/health
```

- **mock** is always available and deterministic: a LoS path, a reflection
  path with prim-id interactions, and a synthetic radio map. It exists so the
  entire app (frontend, tests, result explorer) works with no GPU, no Sionna.
- **sionna** imports Sionna RT lazily inside functions
  (`availability.sionna_available()` only probes module specs). If the import
  or run fails, the API reports 409 instead of crashing the app.
- **AODT (future)** slots in two ways without touching the schemas: as
  another `RayTracingBackend` wrapping a remote AODT worker, or as an
  importer that normalizes AODT Parquet outputs into `PathResultSet` /
  `RadioMapResultSet`, remapping AODT object ids to canonical prim ids via
  `mapping/object_map.json`. Either way results land in `results/` with a
  `ResultSetRef` whose `backend` field records their origin.

## Key decisions

- **snake_case wire format, end-to-end.** JSON keys are snake_case in the
  scene file, all HTTP bodies, and the TypeScript mirror types
  (`frontend/src/types/api.ts`). There is no camelCase conversion boundary
  to drift.
- **Z-up ENU meters, everywhere.** Scene JSON positions, GLB vertex data,
  RF submeshes, and ray-path vertices all share one frame. The demo GLB
  deliberately stores Z-up vertices (glTF's Y-up convention is not applied);
  world transforms are baked into vertices and prim transforms stay identity,
  so no consumer ever reconciles axes.
- **Short device ids, path-like prim ids.** Prims are addressable tree nodes
  (`/buildings/b01/walls`); devices are flat simulation endpoints (`tx_001`)
  referenced directly by result rows. The frontend shows devices under a
  synthetic `/devices` node.
- **Duplicate prim ids rejected at parse time.** `Scene`'s model validator
  raises on duplicates, so a corrupt scene never loads — validation does not
  need to defend against it downstream.
- **Warnings never block compilation.** `ValidationReport.ok` means "no
  error-severity issues". Unassigned materials, missing thickness, or
  visual/RF mismatches are warnings the user can ship past; only structural
  errors abort a compile.
- **Results stored per-id, latest by ref order.** Every run writes an
  immutable `results/<result_id>.json`; the scene keeps an ordered
  `result_sets` list and "latest" is simply the last ref of a kind. History
  is never overwritten.
- **Moving-RX (UE) trajectories move the existing device.** Each waypoint/step
  deep-copies the scene and mutates only the routed UE's `position` and
  finite-difference `velocity_m_s`; every other field (antenna
  pattern/rows/cols/polarization/spacing, power, name, orientation) is
  inherited unchanged, so a trajectory for `rx_001` solves with `rx_001`'s
  configured array. Multi-UE runs (`routes`) are **step-major**: one solve per
  step over all routed UEs, samples ordered all-UEs-at-step-0, then step-1,
  ...; each `TrajectorySample` carries its `ue_id` and metadata lists
  `ue_ids`/`num_steps`. Caveat: Sionna applies ONE scene-level `rx_array`
  (from the first selected RX device), so when routed UEs carry non-identical
  antenna configs only the first is honored — the routes path emits a warning
  naming the ignored UEs. The RFData `trajectory.csv` always carries a `ue_id`
  column (a fixed AODT-viewer schema column) with rows in the result's
  step-major order, so the viewer splits it into per-UE sequences; single-UE
  is the degenerate case (one ue_id). The ML dataset generator is a separate,
  single-UE sweep (it does not consume `TrajectoryResultSet`): each `.npz` is
  one UE's sequence, with the swept UE recorded as `ue_id`/`source_rx_id` in
  `metadata.json`.
- **`mat-` BSDF prefix for Sionna.** Generated Mitsuba XML names each BSDF
  `mat-<rf_material_id>`, the convention Sionna RT uses to bind shapes to
  `RadioMaterial` instances.
- **AI fallback chain: ollama → rule_based.** Suggestions prefer a local
  Ollama model when configured and reachable; otherwise (or on invalid AI
  JSON) they fall back to the deterministic rule-based provider. Suggestions
  never auto-apply by default.
- **Portable local toolchain.** The whole stack runs from user-local,
  relocatable installs — the backend venv under `backend/.venv` and a
  portable Node distribution for the frontend. No admin rights, system
  services, or cloud dependencies are required.
