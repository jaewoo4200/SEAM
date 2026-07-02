# HANDOFF.md — SionnaTwin Studio

## 0. Project summary

**Project name:** SionnaTwin Studio

**Goal:** Build a local-first, consumer-accessible, AI-assisted wireless digital twin authoring and visualization workbench built around **Sionna RT** as the default ray-tracing backend.

The tool should let a user load a textured 3D scene, assign RF materials to objects/submeshes/face groups, compile the scene into a Sionna-compatible RF projection, run or mock path/radio-map simulations, and inspect ray paths/CIR/radio-map outputs in an AODT-inspired but lightweight UI.

This is **not** a full AODT clone. The value proposition is:

- open Sionna-native workflow;
- consumer-level/local-first operation;
- unified textured scene authoring;
- RF material assignment and validation;
- optional local LLM/VLM assistance;
- Sionna RT compilation and result exploration;
- future progressive simulation, mesh radio maps, mobility, and measurement calibration.

---

## 1. Core design principle

### 1.1 The scene must feel unified to the user

The user should experience **one scene**, not two separate worlds.

They should click a wall, window, road, vehicle, tree, or terrain object and see:

- visual/PBR material information;
- RF material information;
- assignment source and confidence;
- validation warnings;
- simulation/result overlays tied to that same object.

### 1.2 Internally, use one canonical scene with two projections

Use this architecture:

```text
Unified RF-Visual Scene Graph
  ├─ Visual Projection
  │   └─ Web viewer / Three.js / CesiumJS later / GLB / textures / PBR
  └─ RF Projection
      └─ Sionna RT / Mitsuba XML / OBJ or PLY submeshes / RadioMaterial
```

Do **not** implement this as two unrelated scene files that drift apart. The canonical scene graph is the source of truth. Visual and RF outputs are compiled projections.

### 1.3 Dual material bindings are mandatory

A mesh primitive can have:

```text
visual material binding:
  brick_albedo.jpg, normal map, roughness, glass alpha, etc.

RF material binding:
  itu_brick, itu_glass, concrete, metal, wood, ground, custom material, etc.
```

A visual material is **not** an RF material. AI may suggest RF material candidates from visual evidence, but the system must preserve status/provenance:

```text
unassigned
rule_suggested
ai_suggested
user_confirmed
measurement_calibrated
```

---

## 2. Initial technology choices

If an existing repository already has a stack, adapt to it. If greenfield, use this default stack.

### Backend

- Python 3.11+
- FastAPI
- Pydantic v2
- Uvicorn
- NumPy
- trimesh for mesh parsing/splitting/export
- PyYAML or ruamel.yaml
- optional DuckDB and PyArrow for future result schemas
- optional Zarr/HDF5 for CIR tensors
- optional Sionna RT backend when installed
- Mock backend always available

### Frontend

- React + Vite + TypeScript
- Three.js or React Three Fiber for MVP textured mesh viewer
- Zustand or Redux Toolkit for state
- ECharts/Plotly/Vega-Lite for plots later
- Future path: CesiumJS + 3D Tiles for geospatial/city-scale scenes

### Local AI

- Provider interface first, model-specific logic second
- Ollama-compatible endpoint support
- Optional local VLM support
- Must degrade gracefully if no AI server, no GPU, or no compatible model
- AI output must be validated against a strict JSON schema

---

## 3. Repository structure recommendation

If greenfield, start with:

```text
sionnatwin-studio/
├─ README.md
├─ HANDOFF.md
├─ backend/
│  ├─ pyproject.toml
│  ├─ app/
│  │  ├─ main.py
│  │  ├─ api/
│  │  │  ├─ health.py
│  │  │  ├─ projects.py
│  │  │  ├─ scene.py
│  │  │  ├─ materials.py
│  │  │  ├─ ai.py
│  │  │  ├─ compile.py
│  │  │  └─ simulate.py
│  │  ├─ core/
│  │  │  ├─ config.py
│  │  │  └─ paths.py
│  │  ├─ schemas/
│  │  │  ├─ scene.py
│  │  │  ├─ materials.py
│  │  │  ├─ devices.py
│  │  │  ├─ simulation.py
│  │  │  ├─ results.py
│  │  │  └─ ai.py
│  │  ├─ services/
│  │  │  ├─ project_store.py
│  │  │  ├─ scene_validator.py
│  │  │  ├─ material_assignment.py
│  │  │  ├─ rf_compiler.py
│  │  │  ├─ mesh_tools.py
│  │  │  ├─ ai_provider.py
│  │  │  └─ simulation_backends/
│  │  │     ├─ base.py
│  │  │     ├─ mock_backend.py
│  │  │     └─ sionna_backend.py
│  │  └─ data/
│  │     └─ default_rf_materials.yaml
│  └─ tests/
│     ├─ test_scene_schema.py
│     ├─ test_material_assignment.py
│     ├─ test_rf_compiler.py
│     ├─ test_ai_parser.py
│     └─ test_mock_backend.py
├─ frontend/
│  ├─ package.json
│  ├─ index.html
│  └─ src/
│     ├─ main.tsx
│     ├─ App.tsx
│     ├─ api/
│     ├─ store/
│     ├─ components/
│     │  ├─ Viewer3D.tsx
│     │  ├─ SceneTree.tsx
│     │  ├─ InspectorPanel.tsx
│     │  ├─ RFMaterialPanel.tsx
│     │  ├─ AISuggestionPanel.tsx
│     │  ├─ ValidationPanel.tsx
│     │  └─ ResultExplorer.tsx
│     └─ types/
├─ examples/
│  ├─ demo_project/
│  └─ scripts/
│     └─ create_demo_project.py
└─ docs/
   ├─ architecture.md
   ├─ scene_format.md
   ├─ rf_materials.md
   ├─ ai_assistant.md
   └─ roadmap.md
```

---

## 4. Project folder format

Each SionnaTwin project should be a folder that can be zipped, shared, and reproduced.

```text
project_name.sionnatwin/
├─ scene.sionnatwin.json
├─ visual/
│  ├─ scene.glb
│  ├─ textures/
│  └─ tileset.json              # optional future 3D Tiles path
├─ rf/
│  ├─ materials.yaml
│  ├─ assignments.yaml
│  ├─ generated_scene.xml        # generated RF projection for Sionna/Mitsuba
│  └─ meshes/
│     ├─ building_07_wall.obj
│     ├─ building_07_window.obj
│     └─ road_asphalt.obj
├─ mapping/
│  ├─ object_map.json
│  ├─ face_group_map.json
│  └─ lod_map.json
├─ ai/
│  ├─ suggestions.jsonl
│  └─ model_config.yaml
├─ results/
│  ├─ paths.json                 # MVP; later Parquet
│  ├─ paths.parquet              # future
│  ├─ radio_map.parquet          # future
│  ├─ cir.zarr                   # future
│  └─ calibration_report.json    # future
└─ provenance.json
```

For the MVP, JSON/YAML is acceptable. Design schemas so Parquet/Zarr can replace large JSON files later.

---

## 5. Canonical scene schema

Create typed Pydantic models for the canonical scene.

### 5.1 Minimum scene model

```json
{
  "schema_version": "0.1.0",
  "scene_id": "demo_scene",
  "name": "Demo Scene",
  "coordinate_system": {
    "type": "local_enu",
    "origin_lat_lon_alt": null,
    "units": "meters"
  },
  "assets": {
    "visual_scene_uri": "visual/scene.glb"
  },
  "prims": [],
  "devices": [],
  "simulation_configs": [],
  "result_sets": []
}
```

### 5.2 Prim model

Each prim represents an object, submesh, face group, or semantic RF-relevant element.

```json
{
  "id": "/buildings/b07/wall_03",
  "name": "wall_03",
  "type": "mesh_primitive",
  "parent_id": "/buildings/b07",
  "semantic_tags": ["building", "wall"],
  "mesh_ref": {
    "asset_uri": "visual/scene.glb",
    "mesh_name": "building_07",
    "primitive_index": 0,
    "face_group": "wall_03"
  },
  "transform": {
    "translation": [0, 0, 0],
    "rotation_quat_xyzw": [0, 0, 0, 1],
    "scale": [1, 1, 1]
  },
  "visual": {
    "material_id": "brick_wall_pbr",
    "base_color_texture": "visual/textures/brick_wall.jpg"
  },
  "rf": {
    "material_id": "itu_brick",
    "thickness_m": 0.24,
    "scattering_coefficient": 0.15,
    "xpd_coefficient": 0.1,
    "assignment_status": "user_confirmed",
    "assignment_sources": ["visual_material_name", "user"],
    "confidence": 0.82
  }
}
```

### 5.3 RF material model

Support built-ins and custom materials.

```json
{
  "id": "itu_concrete",
  "display_name": "ITU Concrete",
  "category": "concrete",
  "model": "itu_frequency_dependent",
  "relative_permittivity": null,
  "conductivity_s_per_m": null,
  "thickness_m": 0.3,
  "scattering_coefficient": 0.1,
  "xpd_coefficient": 0.1,
  "preview_color": "#888888",
  "notes": "Default concrete RF material."
}
```

Use a library such as:

```text
itu_concrete
itu_brick
itu_glass
itu_wood
metal
ground
asphalt_custom
vegetation_custom
unknown_rf
```

### 5.4 Assignment status enum

```text
unassigned
rule_suggested
ai_suggested
user_confirmed
measurement_calibrated
```

---

## 6. Backend API MVP

Implement the following endpoints first.

### Health

```text
GET /api/health
```

Return backend status and whether Sionna and AI providers are available.

### Projects

```text
POST /api/projects
GET /api/projects
GET /api/projects/{project_id}
```

Create/load/list projects.

### Scene

```text
GET /api/projects/{project_id}/scene
PUT /api/projects/{project_id}/scene
POST /api/projects/{project_id}/scene/validate
```

Validation should detect:

- missing RF materials;
- visual material present but RF unassigned;
- suspicious contradictions, e.g. visual name contains glass but RF material is concrete;
- missing thickness for transmissive materials;
- unsupported mesh reference;
- duplicate prim IDs.

### RF materials and assignment

```text
GET /api/projects/{project_id}/rf/materials
PUT /api/projects/{project_id}/rf/materials/{material_id}
POST /api/projects/{project_id}/rf/assign
POST /api/projects/{project_id}/rf/batch-assign
```

Single assignment request:

```json
{
  "prim_ids": ["/buildings/b07/window_12"],
  "rf_material_id": "itu_glass",
  "assignment_status": "user_confirmed",
  "overrides": {
    "thickness_m": 0.012
  }
}
```

### AI material suggestion

```text
POST /api/projects/{project_id}/ai/suggest-materials
```

Request can include selected prim IDs, object names, visual material names, texture thumbnails, screenshots, and semantic tags.

Response must be structured and schema-validated:

```json
{
  "suggestions": [
    {
      "prim_id": "/buildings/b07/window_12",
      "recommended_rf_material_id": "itu_glass",
      "confidence": 0.86,
      "evidence": [
        "object name contains window",
        "visual material name contains blue_glass"
      ],
      "alternatives": [
        {"rf_material_id": "metal", "confidence": 0.11}
      ],
      "needs_user_confirmation": true
    }
  ],
  "provider": "rule_based" 
}
```

Always implement a rule-based fallback provider. Local LLM/VLM is optional.

### Compile RF projection

```text
POST /api/projects/{project_id}/compile/sionna
```

This should:

1. validate scene;
2. group geometry by RF material;
3. export RF submeshes when possible;
4. generate a Sionna/Mitsuba-compatible projection file or a structured placeholder if the real compiler is not ready;
5. write outputs to `rf/generated_scene.xml` and `rf/meshes/` or a mock equivalent;
6. return warnings and generated file paths.

### Simulation

```text
POST /api/projects/{project_id}/simulate/paths
GET /api/projects/{project_id}/results/paths
POST /api/projects/{project_id}/simulate/radio-map
GET /api/projects/{project_id}/results/radio-map
```

First implement mock simulation so frontend/result explorer can be built before real Sionna integration is perfect.

---

## 7. Simulation backend interface

Define a stable interface.

```python
class RayTracingBackend(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def compile(self, project: Project) -> CompileResult: ...

    def simulate_paths(
        self,
        project: Project,
        config: SimulationConfig,
    ) -> PathResultSet: ...

    def simulate_radio_map(
        self,
        project: Project,
        config: SimulationConfig,
    ) -> RadioMapResultSet: ...
```

### 7.1 Mock backend

The Mock backend must always work. It should generate deterministic example outputs:

- a few ray paths from TX to RX;
- LoS and one reflection path;
- power/delay metadata;
- simple fake radio map values.

This enables frontend and tests on machines with no GPU and no Sionna.

### 7.2 Sionna backend

The real Sionna backend is optional at first. It should:

- import Sionna RT lazily;
- fail gracefully if not installed;
- use generated RF projection;
- run minimal path computation for a small scene;
- return normalized results matching the same schema as the Mock backend.

Never make the whole app fail just because Sionna is not installed.

### 7.3 Future AODT support

Do not implement full AODT integration in MVP. Keep a backend-neutral result schema so future support can import AODT Parquet/Iceberg outputs or connect to an AODT worker if credentials/runtime exist.

---

## 8. Frontend MVP

Build the UI around five modes.

### 8.1 Visual Mode

- show textured scene;
- orbit/pan/zoom camera;
- object picking;
- scene tree;
- selected object inspector.

### 8.2 RF Material Mode

- show RF material color overlay;
- highlight unassigned objects in a warning color;
- object inspector shows visual material and RF material side by side;
- allow RF material assignment from a dropdown;
- support batch assignment from selected scene tree nodes.

### 8.3 Validation Mode

Show warnings:

- missing RF material;
- suspicious visual/RF mismatch;
- unsupported or missing mesh refs;
- missing thickness;
- unknown material category;
- duplicate IDs.

### 8.4 AI Assist Mode

- button: “Suggest RF materials”;
- provider status: disabled, rule-based, Ollama text, Ollama VLM, etc.;
- suggestions list with confidence and evidence;
- approve/reject/edit actions;
- never silently apply AI suggestions unless user explicitly chooses auto-apply.

### 8.5 Result Mode

- ray path overlay in 3D;
- path table;
- selected path inspector;
- basic delay/power plot placeholder;
- future CIR/AoA/AoD plots.

---

## 9. Local AI design

### 9.1 Requirements

AI assistance is optional and must be safe to disable.

The app must support these modes:

```text
manual-only
rule-based suggestions
local text LLM suggestions
local VLM suggestions
segmentation-assisted suggestions, future
```

### 9.2 Provider abstraction

Create an interface such as:

```python
class MaterialSuggestionProvider(Protocol):
    name: str

    def is_available(self) -> bool: ...

    def suggest(
        self,
        scene: Scene,
        prim_ids: list[str],
        context: SuggestionContext,
    ) -> MaterialSuggestionResponse: ...
```

Implement:

1. `RuleBasedMaterialSuggestionProvider`
2. `OllamaMaterialSuggestionProvider`, optional
3. `DisabledMaterialSuggestionProvider`

### 9.3 Rule-based fallback

Rules should inspect:

- object name;
- visual material name;
- semantic tags;
- texture filename;
- OSM/GIS tag if present.

Example rules:

```text
window, glass, pane              -> itu_glass
wall, concrete, cement           -> itu_concrete
brick                            -> itu_brick
metal, steel, aluminum, frame    -> metal
wood, tree, trunk                -> itu_wood
road, asphalt, street            -> asphalt_custom or ground
terrain, soil, grass             -> ground or vegetation_custom
```

### 9.4 Ollama/VLM provider

The provider should accept model config from environment or project config:

```yaml
ai:
  enabled: auto
  provider: ollama
  base_url: http://localhost:11434
  text_model: qwen3:8b
  vision_model: qwen2.5vl:3b
  timeout_s: 60
  allow_cpu: true
  auto_apply: false
```

Do not hardcode one model. Treat model names as configuration.

### 9.5 AI output contract

The LLM/VLM must output JSON matching a strict schema. If parsing fails, return a clear warning and fall back to rule-based suggestions.

Required fields:

```text
prim_id
recommended_rf_material_id
confidence
evidence[]
alternatives[]
needs_user_confirmation
```

No free-form AI output should directly mutate the scene.

### 9.6 Provenance

Every AI suggestion must be stored in `ai/suggestions.jsonl` with:

```text
timestamp
provider
model
prompt_version
input_prim_ids
suggestion JSON
accepted/rejected/edited status
final assigned material, if any
```

---

## 10. RF compiler behavior

The RF compiler converts the canonical scene to a Sionna-compatible RF projection.

### 10.1 MVP behavior

For each prim or face group with an RF material:

1. resolve mesh reference;
2. extract the mesh/submesh/face group if possible;
3. group by RF material when safe;
4. export OBJ/PLY into `rf/meshes/`;
5. generate a simple RF scene manifest;
6. optionally generate Mitsuba XML/Sionna scene file.

If mesh extraction is not yet implemented, produce a structured placeholder and warnings, not a crash.

### 10.2 Important rule

A visual mesh can be the same geometry used for RF simulation in the MVP. But the architecture must allow:

```text
Mode 1: same mesh, dual material binding
Mode 2: same source mesh, RF material group split
Mode 3: visual high-poly mesh + simplified RF proxy mesh
```

Start with Mode 1 and Mode 2. Keep Mode 3 as a future optimization.

---

## 11. Result schemas

### 11.1 Path result MVP

```json
{
  "result_id": "mock_paths_001",
  "backend": "mock",
  "simulation_config_id": "default",
  "paths": [
    {
      "path_id": "path_0001",
      "tx_id": "tx_001",
      "rx_id": "rx_001",
      "path_type": "los",
      "vertices": [[0, 0, 2], [10, 0, 1.5]],
      "power_dbm": -62.5,
      "delay_ns": 33.3,
      "phase_rad": 0.0,
      "interactions": []
    },
    {
      "path_id": "path_0002",
      "tx_id": "tx_001",
      "rx_id": "rx_001",
      "path_type": "reflection",
      "vertices": [[0, 0, 2], [5, 4, 2], [10, 0, 1.5]],
      "power_dbm": -78.1,
      "delay_ns": 47.2,
      "phase_rad": 1.1,
      "interactions": [
        {
          "type": "reflection",
          "prim_id": "/buildings/b01/wall_01",
          "rf_material_id": "itu_concrete",
          "point": [5, 4, 2]
        }
      ]
    }
  ]
}
```

### 11.2 Future result types

Design for:

- CIR taps;
- AoA/AoD;
- radio maps;
- mesh radio maps;
- mobility timeline;
- calibration reports.

Use normalized IDs so results can always refer back to canonical prim IDs.

---

## 12. Milestones

### Milestone 0 — Repo audit and setup

Tasks:

- inspect existing files;
- identify current stack;
- create or update README;
- create docs/architecture.md;
- set up backend and frontend dev commands;
- add a minimal demo project.

Acceptance:

- repo runs with a documented command;
- health endpoint works;
- frontend shell loads.

### Milestone 1 — Unified scene schema and project persistence

Tasks:

- define Pydantic models;
- define TypeScript mirror types;
- create/load/save project folders;
- create demo scene JSON;
- implement validation.

Acceptance:

- tests pass for schema load/save;
- duplicate IDs are rejected;
- missing RF materials are reported.

### Milestone 2 — Textured viewer and scene inspector

Tasks:

- load demo GLB or generated primitive scene;
- render textured scene;
- object picking;
- scene tree;
- inspector shows visual and RF fields.

Acceptance:

- user can select an object and see visual/RF material state.

### Milestone 3 — RF material editor and overlay

Tasks:

- material library endpoint;
- material assignment endpoint;
- frontend dropdown/editor;
- RF color overlay;
- unassigned object warnings.

Acceptance:

- user can assign `itu_glass`, `itu_concrete`, etc. to selected objects;
- assignment persists in project;
- overlay updates.

### Milestone 4 — RF projection compiler

Tasks:

- implement RF compiler service;
- group objects by RF material;
- export placeholder or real submesh files;
- generate `rf/generated_scene.xml` or manifest;
- expose compile endpoint.

Acceptance:

- compile endpoint produces deterministic output and warnings;
- test verifies material groups and file paths.

### Milestone 5 — Simulation backend interface and Mock backend

Tasks:

- define backend interface;
- implement Mock backend;
- add simulate paths endpoint;
- add result storage;
- show mock rays in frontend.

Acceptance:

- no Sionna/GPU required;
- user can run mock simulation and see ray paths.

### Milestone 6 — Optional Sionna backend

Tasks:

- lazy import Sionna RT;
- implement availability check;
- support minimal scene load/compute path if Sionna installed;
- normalize results into common schema.

Acceptance:

- app does not fail when Sionna missing;
- if installed and demo scene is compatible, a minimal Sionna run works.

### Milestone 7 — AI-assisted RF material suggestion

Tasks:

- implement rule-based provider;
- implement Ollama-compatible provider;
- add JSON schema validation;
- add AI suggestion panel;
- support approve/reject/edit;
- store suggestion provenance.

Acceptance:

- rule-based suggestions work with no AI server;
- Ollama provider is used when configured and available;
- bad AI JSON is safely rejected;
- suggestions never auto-apply unless configured.

### Milestone 8 — Result explorer

Tasks:

- path table;
- selected path inspector;
- delay/power chart;
- path filtering by type/material/object.

Acceptance:

- user can click a ray and see path metadata.

### Milestone 9 — Mesh radio map, future

Tasks:

- define mesh radio map schema;
- generate measurement surface meshes;
- support road/facade/floor surface overlays;
- Sionna mesh radio map integration if feasible.

Acceptance:

- can display a heatmap attached to mesh surfaces.

### Milestone 10 — Progressive simulation, future

Tasks:

- coarse-to-fine radio map;
- tile/cache manager;
- viewport-priority computation;
- time-to-first-result benchmark.

Acceptance:

- user sees coarse result quickly and refinement later.

### Milestone 11 — Measurement calibration, future

Tasks:

- measurement CSV import;
- error heatmap;
- material parameter optimization;
- before/after calibration report.

Acceptance:

- simulation error can be visualized and RF material parameters updated with provenance.

### Milestone 12 — Mobility/dynamic actors, future

Tasks:

- UE trajectory import;
- timeline playback;
- dynamic scatterer placeholders;
- time-indexed result schema.

Acceptance:

- user can replay movement and inspect changing paths.

---

## 13. Definition of done for the first useful MVP

The first MVP is done when all of this works:

1. Launch backend and frontend locally.
2. Open a demo SionnaTwin project.
3. See a textured or colored 3D scene.
4. Select objects from viewer or scene tree.
5. See visual material and RF material side by side.
6. Assign RF materials manually.
7. Ask for AI/rule-based RF material suggestions.
8. Approve suggestions and persist them.
9. Validate scene and see warnings.
10. Compile RF projection.
11. Run mock path simulation with no GPU and no Sionna.
12. See ray paths overlaid in the viewer.
13. Inspect path metadata in a result panel.
14. Tests pass.
15. README explains setup, commands, demo flow, and current limitations.

---

## 14. Critical implementation rules

### Do

- Keep canonical scene IDs stable.
- Track provenance for material assignment.
- Preserve the distinction between visual and RF material.
- Make AI optional.
- Make Sionna optional until the real backend is implemented.
- Start with small demo scenes.
- Prefer working vertical slices over large unfinished abstractions.
- Document every schema and interface.

### Do not

- Do not build a pure AODT clone.
- Do not require a datacenter GPU.
- Do not require an LLM/VLM to use the app.
- Do not silently apply AI suggestions.
- Do not treat a texture filename as ground-truth RF material.
- Do not make separate visual and RF scenes that cannot be mapped back to common object IDs.
- Do not break the app when Sionna is missing.
- Do not hardcode a proprietary backend.

---

## 15. Prioritization if time is limited

If time is short, implement in this order:

1. Unified scene schema.
2. Project load/save.
3. RF material library and assignment.
4. Basic viewer and inspector.
5. RF overlay and validation.
6. RF projection compiler placeholder.
7. Mock backend path results.
8. Result overlay.
9. Rule-based AI suggestions.
10. Optional Ollama provider.
11. Real Sionna backend.

The core contribution is the **unified RF-visual scene graph with material authoring and Sionna-compatible projection**. Everything else builds on that.

---

## 16. Suggested first coding steps

1. Create `backend/app/schemas/scene.py` with Pydantic models.
2. Create `backend/app/data/default_rf_materials.yaml`.
3. Create `backend/app/services/project_store.py`.
4. Create `examples/scripts/create_demo_project.py` that writes a minimal project.
5. Add `/api/health`, `/api/projects`, and `/api/projects/{id}/scene`.
6. Add unit tests for scene loading and validation.
7. Scaffold frontend with a project loader and inspector panel.
8. Add RF assignment endpoint and UI.
9. Add Mock simulation backend and render simple ray polylines.
10. Add rule-based AI suggestion provider.

---

## 17. Naming conventions

Use stable IDs:

```text
/buildings/b07/wall_03
/buildings/b07/window_12
/roads/r01/surface
/terrain/tile_001
/devices/tx_001
/devices/rx_001
```

Use snake_case for JSON keys unless frontend conventions strongly require camelCase. If frontend uses camelCase, define explicit conversion boundaries.

---

## 18. Example user story

A user opens SionnaTwin Studio and loads `kaist_demo.sionnatwin`.

They see a textured campus building. The viewer is in Visual Mode. They click a window. The inspector shows:

```text
Object: /buildings/b07/window_12
Visual material: blue_glass_pbr
RF material: unassigned
Warning: RF material missing
```

They switch to AI Assist Mode and click “Suggest RF materials.” The system returns:

```text
Recommended: itu_glass
Confidence: 0.86
Evidence: object name contains “window”; visual material contains “glass”
Status: needs user confirmation
```

The user approves. The object now has:

```text
RF material: itu_glass
Status: user_confirmed
Source: ai_suggested + user_confirmed
```

They run validation. Missing walls are highlighted. They batch-assign walls to `itu_concrete` and roads to `asphalt_custom`.

They compile the RF projection. The compiler creates RF submeshes and a Sionna-compatible manifest/XML.

They run mock or real Sionna simulation. Ray paths appear in the viewer. Clicking a ray shows:

```text
Path type: reflection
Power: -78.1 dBm
Delay: 47.2 ns
Interaction: /buildings/b07/wall_03, itu_concrete
```

This is the first complete vertical slice.

---

## 19. Long-term research roadmap

After MVP, build toward:

### 19.1 Mesh-aware radio map

- attach radio maps to road/facade/floor/terrain meshes;
- overlay heatmaps on textured surfaces;
- compare planar vs mesh radio maps.

### 19.2 Progressive consumer-level simulation

- coarse-to-fine radio map;
- viewport-priority refinement;
- tile cache;
- time-to-first-result benchmark.

### 19.3 Measurement-calibrated RF material fitting

- import measurement CSV;
- compare measurement vs simulation;
- estimate material parameters;
- track calibrated status and provenance.

### 19.4 Dynamic mobility layer

- UE trajectory;
- GPX/CSV import;
- moving scatterers;
- time-indexed paths/CIR.

### 19.5 Engine-neutral result import

- import AODT-like results if available;
- compare SionnaBackend and remote/high-end backend outputs;
- use normalized path/CIR/radio-map schemas.

---

## 20. Final north-star statement

Build **SionnaTwin Studio** as an AI-assisted RF-aware scene authoring and simulation workbench:

> A unified textured scene editor where every mesh primitive can carry both visual and RF material bindings; local LLM/VLM modules can suggest RF assignments; users can validate and confirm assignments; the canonical scene compiles into Sionna RT; and path/CIR/radio-map results are visualized back on the same scene, all while remaining usable on consumer-level hardware with graceful fallbacks.
