# RF materials

RF materials are electromagnetic surface descriptions consumed by the RF
projection and the ray-tracing backends. They are deliberately disjoint from
visual/PBR materials: the only cosmetic field they carry is `preview_color`,
which drives the frontend's RF overlay mode and has no electromagnetic
meaning.

## Library files

The app ships a built-in library at
`backend/app/data/default_rf_materials.yaml`. Creating a project copies it
to `<project>/rf/materials.yaml`, which the project may edit and extend;
from then on the project file is authoritative for that project (if it is
missing, the built-in defaults are used).

Format (`RFMaterialLibrary` / `RFMaterial` in
`backend/app/schemas/materials.py`):

```yaml
materials:
  - id: itu_concrete                  # ^[a-z0-9_]+$
    display_name: ITU Concrete
    category: concrete
    model: itu_frequency_dependent    # or: constant
    itu_name: itu_concrete            # Sionna RT built-in name; null for custom
    relative_permittivity: null       # used only by model: constant
    conductivity_s_per_m: null        # used only by model: constant
    thickness_m: 0.30                 # default slab thickness; null allowed
    scattering_coefficient: 0.10      # 0..1
    xpd_coefficient: 0.10             # 0..1
    transmissive: true                # can radio waves pass through?
    preview_color: "#9e9e9e"          # frontend overlay only
    notes: Default structural concrete (ITU-R P.2040).
    builtin: true                     # false for user-defined/edited materials
```

## ITU vs constant models

- `model: itu_frequency_dependent` — permittivity and conductivity are
  derived from the ITU-R P.2040 parameterization *at the simulation
  frequency*. These map onto Sionna RT's built-in materials via `itu_name`
  (e.g. `itu_concrete`, `itu_medium_dry_ground`), so
  `relative_permittivity` / `conductivity_s_per_m` stay `null` in the YAML.
- `model: constant` — `relative_permittivity` and `conductivity_s_per_m` are
  used exactly as given, at every frequency. Use this for measured or
  literature values (asphalt, vegetation, calibrated customs). Constant
  materials are valid only over the frequency range their values were taken
  from — note it in `notes`.

## Default library

| id | display name | category | model | itu_name | thickness_m | scat. | xpd | transmissive | preview |
|---|---|---|---|---|---|---|---|---|---|
| `itu_concrete` | ITU Concrete | concrete | itu | `itu_concrete` | 0.30 | 0.10 | 0.10 | yes | `#9e9e9e` |
| `itu_brick` | ITU Brick | brick | itu | `itu_brick` | 0.24 | 0.15 | 0.10 | yes | `#b5551d` |
| `itu_glass` | ITU Glass | glass | itu | `itu_glass` | 0.012 | 0.02 | 0.05 | yes | `#4fc3f7` |
| `itu_wood` | ITU Wood | wood | itu | `itu_wood` | 0.03 | 0.20 | 0.10 | yes | `#8d6e63` |
| `metal` | Metal | metal | itu | `itu_metal` | — | 0.05 | 0.05 | no | `#b0bec5` |
| `ground` | Ground (medium dry) | ground | itu | `itu_medium_dry_ground` | — | 0.30 | 0.15 | no | `#795548` |
| `asphalt_custom` | Asphalt (custom) | asphalt | constant (εr 5.72, σ 0.005 S/m) | — | — | 0.25 | 0.10 | no | `#37474f` |
| `vegetation_custom` | Vegetation (custom) | vegetation | constant (εr 5.0, σ 0.10 S/m) | — | 1.0 | 0.60 | 0.30 | yes | `#43a047` |
| `unknown_rf` | Unknown RF material | unknown | constant (εr 3.0, σ 0.01 S/m) | — | — | 0.20 | 0.10 | yes | `#e91e63` |

`unknown_rf` is a deliberate placeholder: assign it when a surface cannot be
classified yet, so it is visibly tracked instead of silently defaulting.

## Per-prim overrides

A prim's `rf` binding may override the material's defaults without forking
the material:

```json
"rf": {
  "material_id": "itu_glass",
  "thickness_m": 0.008,
  "scattering_coefficient": null,
  "xpd_coefficient": null,
  "assignment_status": "user_confirmed",
  "assignment_sources": ["user"],
  "confidence": 1.0
}
```

Overrides are set through the assignment API (`AssignRequest.overrides`,
fields `thickness_m`, `scattering_coefficient`, `xpd_coefficient`). A `null`
override means "inherit", not "zero". Overrides are stored on the prim and
shown in the inspector, but **the MVP's Mode 2 grouped compile cannot
represent them**: the exported RF projection uses the library material's
parameters, and the compiler emits a warning per overridden prim.
Override-at-compile-time (prim override if set, else material default) is
the intended behavior once groups can split on parameter sets.

## Thickness semantics

`thickness_m` is the slab thickness used for transmission loss through a
surface modeled as a single-layer slab:

- **Transmissive materials** (glass, brick, concrete, wood, vegetation)
  need a thickness from *somewhere* — the material default or a per-prim
  override. If a transmissive material ends up with no thickness anywhere,
  validation emits a `MISSING_THICKNESS` warning (a warning, not an error:
  compilation proceeds and the backend applies its own fallback behavior).
- **Non-transmissive materials** (metal, ground, asphalt) are treated as
  reflective half-spaces; thickness is meaningless and left `null`.
- Vegetation is a coarse effective-medium: its 1.0 m default thickness
  stands in for propagation through a foliage volume, not a solid wall.

## Editing materials

`PUT /api/projects/{id}/rf/materials/{material_id}` upserts a material in the
project library (persisted to `rf/materials.yaml`). Edited or newly created
materials carry `builtin: false`. Removing a material that prims still
reference is not blocked at write time; the dangling reference surfaces as an
`UNKNOWN_RF_MATERIAL` validation issue.
