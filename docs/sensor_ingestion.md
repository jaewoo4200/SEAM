# Multimodal Sensor Ingestion — Design (for review)

> 상태: **설계안(검토 전)**. 구현 전, 방향 합의를 위한 문서입니다. 아직 코드는 없습니다.
> Status: **design proposal**, not yet implemented — for review before building.

## 1. Why (motivation)

SEAM already ingests real-world state through several *ad-hoc* endpoints:

| Surface | What it ingests | Where it lands | Code |
|---|---|---|---|
| `POST /live/state` | device/actor **positions** (one push) | ephemeral live overlay + optional re-solve | `api/scenario.py:apply_live_state`, `services/live_state.py` |
| `POST /import/devices`, `/import/trajectory` | device/waypoint **poses** (JSON, cartesian **or** geographic) | scene devices / route waypoints | `api/point_import.py`, `services/point_import.py` |
| `POST /calibrate/measurements/import-csv` | measured **link path gains** | material calibration fit | `api/calibrate.py` |

These prove the value of a live/measured feed, but each is a **separate,
one-shot, format-specific** entry point. A "multimodal sensor" world (GNSS,
motion capture, UWB/RTLS anchors, IMU, RF scanners, camera-derived poses,
V2X CAM/CPM, MQTT/WebSocket telemetry) needs a *general* way to:

1. **register** a sensor source and its transport (push / pull / stream),
2. **normalize** heterogeneous payloads into one canonical observation schema
   (Z-up ENU meters, ISO-8601 UTC time, scene entity ids),
3. **route** each observation to the right consumer (live overlay, calibration
   buffer, or an environment/material change),
4. **drive** the closed loop measure → sync → predict → (calibrate) → act,
   on a schedule the user controls.

This document proposes that abstraction. It is **additive** — the three
existing endpoints keep working and are refactored to sit *on top of* the new
core, so nothing regresses.

## 2. Core concepts

```
                         ┌──────────────── SensorSource (registered, per project) ───────────────┐
 raw payload  ─────────► │  Transport (push │ pull │ stream)  →  Adapter.parse()  →  Observation[] │
 (GPS/mocap/RF/…)        └──────────────────────────────────────────────┬───────────────────────┘
                                                                         ▼
                                                              Router (by observation.kind)
                                       ┌───────────────────────────────┼────────────────────────────────┐
                                       ▼                               ▼                                 ▼
                             pose  → live_state overlay        measurement → calibration buffer   environment → scene edit
                                       │  (+ optional resimulate)        │  (+ optional auto-fit)          │  (RF material / actor add)
                                       ▼                                 ▼                                 ▼
                                  viewer follows                   material calibrated               twin geometry updates
```

### 2.1 `Observation` — the canonical unit (already half-exists)

One normalized fact from a sensor at a moment in time. This is the single
schema every adapter must emit; downstream code never sees a raw sensor format.

```python
class Observation(StrictModel):
    kind: Literal["pose", "measurement", "environment"]
    entity_id: str                 # scene device/actor id (or a new id to create)
    t: Optional[str] = None        # ISO-8601 UTC; None → server receive time
    frame: Literal["enu", "wgs84"] = "enu"   # coordinate frame of `position`
    # --- pose (kind="pose") ---
    position: Optional[Vec3] = None          # [x,y,z] ENU m  OR  [lat,lon,alt] wgs84
    orientation_deg: Optional[Vec3] = None   # [yaw,pitch,roll]
    velocity_m_s: Optional[Vec3] = None      # ENU m/s (drives Doppler)
    agl_m: Optional[float] = None            # height above surface (terrain raycast)
    # --- measurement (kind="measurement") ---
    metric: Optional[Literal["path_gain_db","rss_dbm","rsrp_dbm"]] = None
    value: Optional[float] = None
    link_tx_id: Optional[str] = None         # measured link endpoints (optional)
    # --- shared ---
    confidence: Optional[float] = None        # 0..1 sensor-reported quality
    source_id: Optional[str] = None           # set by the ingest layer
```

Note how this **generalizes what already exists**: a `pose` observation is a
superset of `DeviceState`/`ActorState` (`schemas/actors.py:17-25`) plus the
`agl_m`/geographic handling already implemented in `point_import`; a
`measurement` observation is a `MeasurementSample` (`api/calibrate.py`).

### 2.2 `SensorAdapter` — format normalization (the extension point)

An adapter turns one sensor's raw payload into `Observation[]`. This is the
*only* thing a new sensor integration must implement.

```python
class SensorAdapter(Protocol):
    key: str                                   # "gnss_nmea", "mocap_optitrack", …
    def parse(self, raw: bytes | dict | str,
              *, mapping: dict) -> list[Observation]: ...
```

`mapping` is per-source config that binds the sensor's native ids/fields to
scene entities (e.g. `{"tracker_7": "veh_001", "units": "mm"}`), so the same
adapter serves different scenes.

Ship a small adapter registry (`services/sensors/adapters/`) so adapters are
pluggable exactly like the existing AI-provider chain and plugin architecture
(`docs/extending.md`). **Phase-1 adapters** (below) are deliberately few.

### 2.3 `Transport` — how observations arrive

Three modes, all reducing to "produce raw payloads that the adapter parses":

| Mode | Meaning | Reuses |
|---|---|---|
| **push** | external system POSTs to `/sensors/{id}/observe` | FastAPI route |
| **pull** | server polls an external URL every *N* s | the periodic-refresh timer pattern already added in the FE store (`radioMapIntervalSec`) — moved server-side |
| **stream** | server holds a WebSocket/MQTT subscription | the existing WS infra (`api/events.py`) |

The **scheduler** ties pull sources to an interval and pushes their normalized
observations into the same router, so "live / interval / one-shot" (the user's
original ask) all become one mechanism.

### 2.4 `Router` — where an observation goes

Deterministic dispatch by `observation.kind`, into machinery that **already
exists**:

- **pose** → `live_state.record(...)` overlay (viewer follows via `GET /scene`)
  → if the source has `resimulate=true`, run the existing `_quick_solve`.
  Geographic → ENU uses `point_import.geodetic_to_enu`; `agl_m` uses
  `terrain.snap_to_terrain`. **The disk is never written** unless the source is
  explicitly `persist=true` (the overlay/persist contract we just fixed).
- **measurement** → append to a per-project calibration buffer; when the source
  is `auto_calibrate=true`, feed the existing `calibrate_materials` grid-fit.
- **environment** → a guarded scene edit (add/remove an actor, swap a material)
  through the normal validated write path (never bypasses validation).

## 3. Data model & API surface (proposed)

```python
class SensorSource(StrictModel):
    id: str
    name: str = ""
    adapter: str                       # registry key
    transport: Literal["push", "pull", "stream"] = "push"
    mapping: dict = {}                 # sensor-id/field → scene entity + units
    # pull/stream:
    url: Optional[str] = None
    interval_sec: Optional[float] = None   # pull cadence
    # routing behavior:
    resimulate: bool = False           # re-solve links on each pose batch
    persist: bool = False              # write poses to disk (default: ephemeral)
    auto_calibrate: bool = False       # fit materials on measurement batches
    enabled: bool = True
```

Stored per project (e.g. `sensors.json` next to the scene), so a twin remembers
its wiring.

```
POST   /projects/{id}/sensors                 register a source        → SensorSource
GET    /projects/{id}/sensors                 list sources + status
PATCH  /projects/{id}/sensors/{sid}           enable/disable, edit mapping
DELETE /projects/{id}/sensors/{sid}
POST   /projects/{id}/sensors/{sid}/observe   push raw payload         → {applied, unknown, warnings, links?}
GET    /projects/{id}/sensors/{sid}/status    last-seen, rate, error, buffered count
GET    /import/sensor-templates               example payloads per adapter (like /import/templates)
```

`/observe` returns the same shape as `LiveStateResponse` for pose batches, so a
push source doubles as today's `/live/state` with an adapter in front.

## 4. Closed loop (the point of it all)

```
 sensor → observe → normalize → route(pose) → overlay ─┐
                                                        ├─ resimulate → fresh links → (UI overlay / control)
 sensor → observe → normalize → route(meas) → buffer ──┘        └─ auto_calibrate → material fit → better predictions
```

This makes the user's "dynamic environment, live map update" a first-class,
generalized flow: **any** registered sensor keeps the twin's positions and
(optionally) its material calibration current, at a cadence the source declares
(live push, periodic pull, or streamed) — instead of three bespoke endpoints.

## 5. Scope & phasing (recommended)

**Phase 1 (MVP, ~the size of the point-import feature):**
- `Observation` + `SensorSource` schemas; per-project `sensors.json` store.
- Router (pose → overlay/resimulate, measurement → calibration buffer).
- Transport: **push** only (`/observe`), + refactor `/live/state` and the CSV
  measurement import to emit `Observation[]` through the router (no behavior
  change; proves the core).
- Adapters: `seam_json` (the native `Observation` list), `gnss_latlon` (WGS84
  → ENU via existing geodetic), `rf_measurement_csv` (existing CSV).
- CRUD + `/observe` + templates + tests.

**Phase 2:**
- **pull** transport + server-side scheduler (generalizes the FE interval).
- `mocap_csv` / UWB-RTLS adapter; `auto_calibrate` loop wired end-to-end.
- Sensor status panel in the UI (last-seen, rate, errors).

**Phase 3 (only if needed):**
- **stream** transport (WebSocket/MQTT subscription).
- V2X (CAM/CPM) and camera-pose adapters; environment observations
  (dynamic actor add/remove, material swaps from a semantic sensor).

## 6. What this deliberately does NOT do (guardrails)

- No new solver or physics — it only *feeds* the existing Mock/Sionna backends.
- No silent disk writes — poses stay ephemeral unless a source opts into
  `persist` (the overlay contract).
- No bypass of scene validation for environment edits.
- Adapters never trust raw ids blindly — unknown entity ids are reported, not
  auto-created, unless the source's mapping says to.

## 7. Open questions for the reviewer

1. **Auth/trust** for push sources — a shared token per source, or trust the
   local-first boundary (no auth, localhost only)? (Default proposal: opt-in
   token, off by default since the app is local-first.)
2. **Backpressure** — cap observation rate / coalesce (the overlay is
   last-write-wins, so a 100 Hz mocap feed only needs the latest per entity)?
   Proposal: coalesce per `entity_id` per scheduler tick.
3. **Which Phase-1 adapters** matter most to you (GNSS? mocap? an RF scanner?)
   — that decides the first concrete adapter beyond the native JSON one.
4. **Environment observations** — do you want sensors that change *geometry/
   materials* (Phase 3), or is pose + measurement enough for the paper/demo?
