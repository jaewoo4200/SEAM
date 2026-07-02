# Architecture

SionnaTwin Studio is a local-first workbench for authoring RF-aware digital
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
  scripts/create_demo_project.py   generates examples/demo_project/kaist_demo.sionnatwin
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
