# Point / Device / Trajectory Import

> **English** · [한국어](point_import.ko.md)

Import radio devices and UE trajectory waypoints from a single JSON schema a
user can hand-author or export from a GPS tool. Points may be **cartesian**
(local ENU meters, Z-up) or **geographic** (WGS84 latitude/longitude), detected
per point, and the two forms may be mixed within one file.

Schemas: `backend/app/schemas/point_import.py`. Resolution logic:
`backend/app/services/point_import.py`. Routes:
`backend/app/api/point_import.py`.

## Coordinate rules

- **Cartesian** points are the canonical scene frame: **local ENU meters,
  Z-up** — `x` = East, `y` = North, `z` = Up. No anchor needed.
- **Geographic** points are **WGS84** degrees. They **require** the scene's
  geodetic anchor `coordinate_system.origin_lat_lon_alt` (`[lat_deg, lon_deg,
  alt_m]`), which the OSM import sets. Without it the request fails **400**:

  > `scene has no geodetic anchor (coordinate_system.origin_lat_lon_alt); use
  > cartesian x/y/z or import the scene via OSM`

  Conversion is the standard closed-form **geodetic → ECEF → local ENU** about
  the anchor (WGS84 ellipsoid, implemented by hand, no extra dependency). As a
  sanity check, 0.001° of latitude north of the anchor lands at ≈ +111.32 m in
  `y`. Absolute altitude maps to ENU up as `z = alt_m - origin_alt`.

## Point forms

A point is accepted anywhere a point is expected, in any of these forms:

| Form | Example | Notes |
| --- | --- | --- |
| array `[x, y]` | `[12.0, -4.0]` | cartesian; `z` defaults to 0 (or the default AGL) |
| array `[x, y, z]` | `[30.0, 5.0, 1.5]` | cartesian |
| `{x, y, z?}` | `{"x": 12, "y": -4, "z": 1.5}` | cartesian |
| `{x, y, agl_m?}` | `{"x": 0, "y": 0, "agl_m": 1.5}` | cartesian XY, AGL height |
| `{lat, lon, alt_m?}` | `{"lat": 37.5563, "lon": 127.0448, "alt_m": 45.2}` | geographic, absolute height |
| `{lat, lon, agl_m?}` | `{"lat": 37.5561, "lon": 127.0451, "agl_m": 1.5}` | geographic, AGL height |

Auto-detection: the presence of **both `lat` and `lon`** selects the
geographic reading; otherwise the point is cartesian. Mixing `x`/`y` with
`lat`/`lon` in one point, or giving only one of `lat`/`lon`, is a **400**.

## AGL semantics

`agl_m` is **height above the scene surface**. Each AGL point is resolved by
raycasting straight down onto the visual mesh
(`app.services.terrain.snap_to_terrain`) and taking `z = surface + agl_m`, so a
device or waypoint keeps a constant antenna height over sloped ground.

- If nothing lies under the point (off the mesh footprint, or the scene has no
  visual mesh), the `agl_m` value is kept as an **absolute z** and a warning is
  emitted.
- `agl_m` and `z`/`alt_m` are **mutually exclusive** per point. If both are
  given, **AGL wins** and a warning is emitted.
- Trajectory import applies a **default AGL** (`agl_m` on the request body,
  default `1.5`) to any waypoint that gives neither `z` nor `agl_m`. Pass
  `"agl_m": null` to place such waypoints at `z = 0` instead.

## Underground warnings

For a point given with an **explicit `z`** (or geographic `alt_m`), the surface
z under it is computed anyway. When the point sits below that surface
(`z < surface - 0.05 m`), a warning is appended:

> `device 'ue_01' sits 3.0 m below the surface under it`

The explicit `z` is **never auto-corrected** — only flagged. (AGL points, by
construction, sit on the surface and are never underground.)

## Endpoints

### `POST /api/projects/{project_id}/import/devices`

Upsert or add devices into the scene. Auto-generates ids (`tx_00N` / `rx_00N`)
when `id` is omitted; `kind` defaults to `rx`.

Request:

```jsonc
{
  "mode": "upsert",          // "upsert" (default) | "add"
  "devices": [
    { "id": "tx_001", "kind": "tx", "position": [0, 0, 10], "power_dbm": 30 },
    { "kind": "rx", "x": 12, "y": -4, "agl_m": 1.5, "name": "car UE" },
    { "id": "ue_geo", "lat": 37.5563, "lon": 127.0448, "alt_m": 45.2 }
  ]
}
```

- `mode: "upsert"` updates a device with a matching `id` in place (position,
  orientation, power, etc.), noting a warning. `mode: "add"` returns **409** on
  an id collision.
- Each device gives its location either as `position` (any point form) or as
  top-level coordinate fields (`x`/`y`/`z` or `lat`/`lon`/`alt_m`/`agl_m`).
- Optional passthrough fields: `name`, `orientation_deg` (`[yaw, pitch,
  roll]`), `power_dbm`, `velocity_m_s`, `antenna`, `color`.

Response:

```json
{ "added_ids": ["tx_001", "rx_001", "ue_geo"], "updated_ids": [], "warnings": [] }
```

A `device_import` provenance event is appended
(`{"type": "device_import", "count": N, "warnings": [...]}`).

```bash
curl -X POST http://localhost:8000/api/projects/my_project/import/devices \
  -H 'Content-Type: application/json' \
  -d '{
        "mode": "upsert",
        "devices": [
          { "id": "tx_001", "kind": "tx", "position": [0, 0, 10], "power_dbm": 30 },
          { "kind": "rx", "x": 12, "y": -4, "agl_m": 1.5, "name": "car UE" }
        ]
      }'
```

### `POST /api/projects/{project_id}/import/trajectory`

Resolve UE trajectory waypoints to fully cartesian `[x, y, z]` for the
trajectory-routes UI. **Does not mutate the scene.**

Request:

```jsonc
{
  "ue_id": "ue_01",          // optional; echoed back, not resolved against the scene
  "agl_m": 1.5,              // optional default height for points lacking z/agl (null => z = 0)
  "points": [
    { "x": 0, "y": 0, "agl_m": 1.5, "orientation_deg": [0, 0, 0] },   // per-waypoint antenna aim
    { "lat": 37.5560, "lon": 127.0450, "agl_m": 1.5, "orientation_deg": [90, 0, 0] },
    [30.0, 5.0, 1.5]         // bare arrays carry no orientation
  ]
}
```

An object waypoint may carry `orientation_deg` (`[yaw, pitch, roll]` degrees).
Each solved step aims the moving UE's antenna to the **nearest waypoint's**
orientation (piecewise-constant between waypoints), so a turning UE's beam
turns with it — Sionna honors it; the Mock backend is isotropic and unaffected.
Waypoints with no orientation keep the device's authored orientation.

Response:

```json
{ "ue_id": "ue_01",
  "waypoints": [[0,0,1.5],[12.3,-4.1,1.5],[30,5,1.5]],
  "orientations_deg": [[0,0,0],[90,0,0],null],
  "warnings": [] }
```

`orientations_deg` is parallel to `waypoints` (null where a point gave none),
or omitted entirely when no waypoint carried an orientation. The frontend feeds
it into `UERoute.orientations_deg` for the trajectory solve.

```bash
curl -X POST http://localhost:8000/api/projects/my_project/import/trajectory \
  -H 'Content-Type: application/json' \
  -d '{
        "ue_id": "ue_01",
        "points": [ {"x":0,"y":0,"agl_m":1.5}, [30.0, 5.0, 1.5] ]
      }'
```

### `GET /api/import/templates`

Static JSON (no project needed) with example payloads for both endpoints, a
combined hand-authored file example, and a `field_reference` describing every
field — so the frontend can offer a downloadable, self-describing template.

```bash
curl http://localhost:8000/api/import/templates
```

## Template (combined file)

```jsonc
{
  "devices": [
    { "id": "ue_01", "kind": "rx",
      "position": [12.0, -4.0, 1.5],
      "orientation_deg": [90, 0, 0],
      "power_dbm": 23.0, "name": "car UE" },
    { "id": "ue_02", "lat": 37.5563, "lon": 127.0448, "alt_m": 45.2 },
    { "id": "ue_03", "lat": 37.5561, "lon": 127.0451, "agl_m": 1.5 }
  ],
  "trajectories": [
    { "ue_id": "ue_01",
      "points": [
        {"x": 0, "y": 0, "agl_m": 1.5},
        {"lat": 37.5560, "lon": 127.0450, "agl_m": 1.5},
        [30.0, 5.0, 1.5]
      ] }
  ]
}
```

The `devices` array feeds `POST .../import/devices`; each entry in
`trajectories` feeds one `POST .../import/trajectory` call (`ue_id` + `points`).
