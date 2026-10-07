# Scene and project format

> **English** · [한국어](scene_format.ko.md)

A SEAM project is a plain folder (conventionally named
`<project_id>.seam`; legacy `<project_id>.sionnatwin` folders keep loading)
that can be zipped, shared, and reproduced. The canonical scene file inside
it is the single source of truth; everything else is either an input asset
or generated output.

## Project folder layout

```text
<project_id>.seam/
├─ scene.seam.json            canonical unified scene (source of truth)
├─ visual/
│  ├─ scene.glb               visual projection source (named meshes, PBR)
│  └─ textures/               optional
├─ rf/
│  ├─ materials.yaml          project RF material library (see rf_materials.md)
│  ├─ generated_scene.xml     compiled Mitsuba/Sionna projection (generated)
│  └─ meshes/                 compiled RF submeshes (generated)
├─ mapping/
│  └─ object_map.json         prim_id -> {"mesh_name": ...} for mesh prims
├─ ai/
│  └─ suggestions.jsonl       AI suggestion + decision provenance log
├─ results/
│  └─ <result_id>.json        normalized simulation results
└─ provenance.json            project-level event log
```

The backend discovers projects by scanning its configured roots
(`SEAM_PROJECT_ROOTS`, legacy `SIONNATWIN_PROJECT_ROOTS`; defaulting to the
repo's `projects/` in a source checkout, where the committed examples from
`examples/demo_project/` are copied on first start, and to `~/.seam/projects`
after a pip install) for folders containing
`scene.seam.json` (or the legacy `scene.sionnatwin.json`). The project id is
the folder name without the `.seam` (or legacy `.sionnatwin`) suffix. All
writes are atomic (temp file + rename), so a crash never corrupts the scene.

### Legacy `.sionnatwin` layout

Projects created before the SEAM rename use a `<project_id>.sionnatwin` folder
with `scene.sionnatwin.json` inside. These remain fully supported — the store
loads and saves them in place — but new projects use `.seam` / `scene.seam.json`
as shown above.

## scene.seam.json

Serialized `Scene` model (`backend/seam_studio/schemas/scene.py`). All models reject
unknown keys, so schema drift fails loudly at load time. All coordinates are
Z-up ENU meters.

### Top level

| field | type | notes |
|---|---|---|
| `schema_version` | str | currently `"0.1.0"` |
| `scene_id` | str | stable scene identifier, usually equals the project id |
| `name` | str | display name |
| `coordinate_system` | object | see below |
| `assets` | object | see below |
| `prims` | Prim[] | the scene graph, flattened with `parent_id` links |
| `devices` | Device[] | transmitters/receivers |
| `simulation_configs` | SimulationConfig[] | stored, reusable run configs |
| `result_sets` | ResultSetRef[] | ordered pointers to stored results |

Duplicate prim ids or device ids are rejected at parse time (a
`Scene.model_validate` error), not merely flagged by validation.

### coordinate_system

| field | type | notes |
|---|---|---|
| `type` | `"local_enu"` | fixed for now |
| `origin_lat_lon_alt` | [lat, lon, alt] \| null | geodetic anchor when georeferenced (future 3D Tiles path) |
| `units` | `"meters"` | fixed |

### assets

| field | type | notes |
|---|---|---|
| `visual_scene_uri` | str \| null | project-relative GLB, default `"visual/scene.glb"` |
| `tileset_uri` | str \| null | future 3D Tiles tileset |

### Prim

One entry per object, submesh, or grouping node. Ids are absolute and
path-like (`/buildings/b01/window_01`); the leading segment carries no
special meaning beyond structure.

| field | type | notes |
|---|---|---|
| `id` | str | must start with `/`, no trailing `/`, no `//` |
| `name` | str | display name, usually the last path segment |
| `type` | `"mesh_primitive"` \| `"group"` | groups carry no geometry |
| `parent_id` | str \| null | id of the parent prim; null for top-level |
| `semantic_tags` | str[] | e.g. `["building", "window"]`; used by rules/AI |
| `mesh_ref` | MeshRef \| null | required in practice for mesh_primitive |
| `transform` | Transform | translation/rotation_quat_xyzw/scale; identity when transforms are baked into the GLB (the demo convention) |
| `visual` | VisualBinding \| null | PBR appearance evidence |
| `rf` | RFBinding | always present, possibly unassigned |

### MeshRef — the three modes

| field | type | notes |
|---|---|---|
| `asset_uri` | str | project-relative, e.g. `"visual/scene.glb"` |
| `mesh_name` | str | exact named mesh inside the asset |
| `primitive_index` | int | glTF primitive within the mesh, default 0 |
| `face_group` | str \| null | named face subset; null = whole mesh |

- **Mode 1 — whole named mesh** (`face_group: null`): one prim per GLB mesh,
  dual material binding on the whole thing. The demo project uses only this.
- **Mode 2 — face-group split**: several prims share one `mesh_name` and
  partition it by `face_group` (e.g. walls vs windows in one building mesh).
  The field is stored and round-tripped (`mapping/face_group_map.json`), but
  **face-subset extraction is not implemented in the MVP**: the compiler uses
  the whole named mesh and emits a warning, so prims sharing a mesh should
  currently share one RF material.
- **Mode 3 — RF proxy mesh (future)**: a high-poly visual mesh paired with a
  simplified RF proxy. Would add an `rf_proxy_uri` to MeshRef; not
  implemented in the MVP.

### VisualBinding

| field | type | notes |
|---|---|---|
| `material_id` | str \| null | app-level visual material id, if any |
| `material_name` | str \| null | material name as authored in the GLB |
| `base_color_texture` | str \| null | project-relative texture path |
| `base_color_rgba` | [r,g,b,a] \| null | 0–1 floats |

Visual data is rendering + suggestion evidence only; it is never an RF
input.

### RFBinding

| field | type | notes |
|---|---|---|
| `material_id` | str \| null | id into `rf/materials.yaml` |
| `thickness_m` | float \| null | per-prim override (> 0) |
| `scattering_coefficient` | float \| null | per-prim override (0–1) |
| `xpd_coefficient` | float \| null | per-prim override (0–1) |
| `assignment_status` | enum | see lifecycle below |
| `assignment_sources` | str[] | ordered provenance, e.g. `["rule_based"]`, `["ai:ollama/qwen3:8b", "user"]` |
| `confidence` | float \| null | 0–1 |

Invariant enforced by the model: `material_id == null` if and only if
`assignment_status in {"unassigned", "rejected"}`.

### Assignment status lifecycle

```text
unassigned
   │  rule engine / AI proposes (never auto-applied by default),
   │  or a deterministic rule assigns a material outright → rule_assigned
   ▼
rule_suggested | ai_suggested
   │  user approves or edits in the UI (or assigns manually from unassigned);
   │  declining a suggestion → rejected (no material)
   ▼
user_confirmed
   │  measurement calibration (POST /calibrate/materials, apply: true) refines parameters
   ▼
measurement_calibrated
```

Ordering reflects increasing trust. Suggested-but-unconfirmed bindings are
usable (the compiler accepts them) but produce an `UNCONFIRMED_SUGGESTION`
validation warning. The demo scene ships one example of each interesting
state: `/terrain/ground` is `user_confirmed`, `/roads/r01/surface` is
`rule_suggested`, and the buildings/windows/tree are `unassigned`.

### Device

| field | type | notes |
|---|---|---|
| `id` | str | short id (`tx_001`), pattern `[a-z0-9_\-]+` |
| `name` | str | display name |
| `kind` | `"tx"` \| `"rx"` | |
| `position` | [e,n,u] | Z-up ENU meters |
| `orientation_deg` | [yaw,pitch,roll] | degrees, ENU frame |
| `power_dbm` | float | transmit power; ignored for rx |
| `antenna` | Antenna | `pattern` (Sionna name), `polarization`, `num_rows`, `num_cols` |
| `color` | `#rrggbb` | viewer marker color |

### Actor

Movable RF objects (`kind`: `"car"` \| `"human"` \| `"uav"` \| `"custom"`), each
compiled as its own Sionna shape. `position` is the base center (z = ground
contact) and `shape.size_m` the box extents. The fields relevant here:

| field | type | notes |
|---|---|---|
| `trajectory` | ActorTrajectory \| null | waypoint path the actor follows in scenarios; null = static |
| `sensing` | SensingTargetSpec \| null | radar sensing-target binding for `POST /simulate/sensing`; null = not a target. See [guides/sensing.md](guides/sensing.md) |

`ActorTrajectory`:

| field | type | notes |
|---|---|---|
| `waypoints` | [e,n,u][] | base positions, Z-up ENU meters; the actor moves linearly between them |
| `dt_s` | float > 0 | seconds per waypoint step, **however long the step is** (default 0.1). Ignored while `speed_m_s` is set |
| `speed_m_s` | float > 0 \| null | constant travel speed: each segment takes length / speed. null = paced by `dt_s` (scenes without the field behave and hash exactly as before) |
| `mode` | `"once"` \| `"loop"` \| `"pingpong"` \| null | once clamps at the last waypoint, loop jumps back to the first, pingpong reverses; null = from `loop` |
| `loop` | bool | deprecated, superseded by `mode` |

With `dt_s` pacing the speed is segment length / `dt_s`, so waypoints 50 m
apart at `dt_s` 0.25 s mean 200 m/s, and that speed drives the Doppler. Set
`speed_m_s` to decouple speed from waypoint spacing.

`SensingTargetSpec`: `model` (`"tr38901"` \| `"constant"`), `object_type`
(TR 38.901 type; null = derived from the actor kind, required for `custom`),
`model_type` (1 \| 2 \| null), `rcs_dbsm` / `xpr_db` (constant only),
`random_components`, `size_m` (null = actor size), `velocity_m_s` (null =
trajectory tangent at t=0), `enabled`. Fields of the other model are rejected
(422 on `PUT /scene`).

### SimulationConfig

| field | type | notes |
|---|---|---|
| `id`, `name` | str | |
| `backend` | `"auto"` \| `"mock"` \| `"sionna"` | auto = sionna if installed, else mock |
| `frequency_hz` | float | e.g. `3.5e9` |
| `max_depth` | int 0–12 | max interaction depth |
| `tx_ids`, `rx_ids` | str[] \| null | null = all devices of that kind |
| `los`, `reflection`, `diffraction`, `scattering` | bool | enabled interaction types |
| `num_samples` | int | ray-launching budget |
| `radio_map` | object | `cell_size_m`, `height_m`, `metric` |

### ResultSetRef

| field | type | notes |
|---|---|---|
| `result_id` | str | `{backend}_{kind}_{n:03d}`, e.g. `mock_paths_001` |
| `kind` | `"paths"` \| `"radio_map"` \| `"mesh_radio_map"` \| `"trajectory"` \| `"scenario"` \| `"channel"` \| `"playback"` \| `"sensing"` \| `"isac"` \| `"sensing_coverage"` | |
| `backend` | str | backend that produced it |
| `simulation_config_id` | str | |
| `uri` | str | project-relative, `results/<result_id>.json` |
| `created_at` | str \| null | ISO 8601 UTC |
| `label` | str \| null | a named run; prune never deletes labeled runs |
| `size_bytes` | int \| null | file size at save, shown in Run history |

Result files are immutable; the list is ordered and only grows, except that
pruning (`POST /results/prune`, or `SEAM_AUTO_PRUNE_KEEP` after each solve)
removes unlabeled refs and their files. The "latest" result of a kind is the
last ref of that kind.

## Demo project

`examples/scripts/create_demo_project.py` regenerates
`examples/demo_project/sample_demo.seam` deterministically: 8 named
meshes in `visual/scene.glb` (world transforms baked into vertices), 13
prims (5 groups + 8 mesh primitives), 7 devices (rooftop TX `tx_001`, street
RX `rx_001`, the TX's co-located sensing RX `tx_001_rx`, and two more TRPs
`tx_002` / `tx_003` with their co-located sensing RXs `tx_002_rx` /
`tx_003_rx`), 3 actors (car, pedestrian and the drone sensing target
`uav_001`), and two stored simulation configs (`default`, 28 GHz, and
`sensing_fr1`, 3.5 GHz / 20 MHz for the sensing guide). The committed copy
predates the v0.1.14 sensing RX and drone and the v0.1.15 TRPs and
`sensing_fr1`; a source checkout adds them when it copies the demo into
`projects/`. It doubles as the reference example for every convention on
this page.
