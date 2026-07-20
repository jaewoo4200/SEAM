# Point-import example files

> **English** · [한국어](README.ko.md)

Ready-made JSON files for the two import buttons — **`⤓ JSON`** in the scene
tree's DEVICES header, and **`⤓ Import JSON`** in the Results-mode
**UE trajectory** panel. Format reference: [docs/point_import.md](../../docs/point_import.md).

| File | Use on | What it demonstrates | Expected result |
| --- | --- | --- | --- |
| `devices_sample_demo.json` | Sample Demo | Cartesian devices: explicit `z`, `agl_m`, and `position` array forms; `orientation_deg`, `power_dbm` passthrough | Toast **“Imported devices — 3 added, 0 updated”**; re-import the same file → **“0 added, 3 updated”** (`upsert`) |
| `devices_geographic_hyu.json` | An **OSM-imported** project (default HYU area) | Geographic `lat`/`lon` + `agl_m` points resolved through the scene's geodetic anchor | 1 TX + 2 RX appear inside the imported area. On a scene **without** an anchor (e.g. Sample Demo) it must fail with *“scene has no geodetic anchor … use cartesian x/y/z or import the scene via OSM”* — that error is the negative test |
| `trajectory_sample_demo.json` | Sample Demo (UE trajectory panel, an RX selected) | Waypoint object + bare-array forms, `agl_m` heights | Toast **“Imported 5 waypoint(s) for rx_001”**; a 5-point route crossing the demo scene appears as a route row |
| `trajectory_oriented.json` | Sample Demo | Per-waypoint `orientation_deg` (antenna aim turns along the route) | Toast **“… (with orientation)”** and an **`· oriented`** chip on the route row |
| `trajectory_underground_warning.json` | Sample Demo | Explicit `z` below the surface is flagged, never auto-corrected | Import succeeds with a ⚠ warning that the middle waypoint sits ≈3 m below the surface; the warning stays visible on the route row |

Tips

- The devices button also accepts a bare array (without the `{"mode": …, "devices": …}`
  wrapper); the trajectory button likewise accepts a bare `[...]` of points.
- Auto-generated ids: omit `"id"` on a device and it gets `tx_00N`/`rx_00N`.
- `GET /api/import/templates` serves the same examples with a full field reference.
