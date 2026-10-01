# Radar sensing: RCS targets, echo solves, and Doppler

> **English** · [한국어](sensing.ko.md)

SEAM Studio can treat any actor (car, pedestrian, UAV, custom object) as a
**radar sensing target** and solve the echo paths TX → target → RX with
Sionna RT's radar cross-section solver (`sionna.rt.rcs.RCSSolver`, new in
sionna-rt 2.2). Every echo carries its own Doppler shift, so a moving target
shows up as a colored velocity signature in the viewport. Without sionna-rt
2.2 the whole workflow still runs on the **Mock backend** (bistatic radar
equation), so you can try it without a GPU.

---

## 1. What RCS is

- The **radar cross-section** σ (m², or dBsm = 10·log10(σ / 1 m²)) is the
  area of an isotropic re-radiator that would send back the same power as the
  real target.
- The received echo follows the bistatic radar equation
  `Pr / Pt = Gt · Gr · λ² · σ / ((4π)³ · d1² · d2²)`, with d1 = TX→target and
  d2 = target→RX. Monostatic (RX at the TX) gives the familiar `d⁴` law.
- 3GPP **TR 38.901 §7.9** goes further: each target type has one or more
  scattering points whose RCS depends on the incident/scattered directions
  (angular lobes), plus optional random components. Sionna RT 2.2 implements
  that model; SEAM exposes it per actor.

## 2. Binding an actor

Select an actor and open **Inspector → Sensing target**. The binding is
stored on the actor as `actor.sensing` (see
[../scene_format.md](../scene_format.md)); `null` means "not a target".

**Model** is one of:

- **TR 38.901** (`"tr38901"`) — the 3GPP target models below. `object_type`
  defaults from the actor kind (car → `vehicle-multi-sp`, human → `human`,
  UAV → `uav-small-size`); a `custom` actor must pick one. `model_type`
  defaults to the highest model the type defines.
- **Constant RCS** (`"constant"`) — one isotropic scattering point at the
  target center with `rcs_dbsm` (blank = 0 dBsm) and an optional `xpr_db`
  cross-polarization ratio (blank = no depolarization).

| `object_type` | Model types | Default for kind | Mean monostatic σ_M (dBsm) | Scattering points | TR 38.901 default size L×W×H (m) |
|---|---|---|---|---|---|
| `uav-small-size` | 1 | uav | −12.81 | 1 | 0.3 × 0.4 × 0.2 |
| `uav-large-size` | 2 | — | −5.85 | 1 | 1.6 × 1.5 × 0.7 |
| `human` | 1, 2 | human | −1.37 | 1 | 0.5 × 0.5 × 1.75 |
| `vehicle-single-sp` | 2 | — | 11.25 | 1 | 5.0 × 2.0 × 1.6 |
| `vehicle-multi-sp` | 2 | car | 11.25 | 5 (left, back, right, front, roof) | 5.0 × 2.0 × 1.6 |
| `agv-single-sp` | 2 | — | −4.25 | 1 | 0.5 × 1.0 × 0.5 |
| `agv-multi-sp` | 2 | — | −4.25 | 5 | 0.5 × 1.0 × 0.5 |

Plain `"vehicle"` / `"agv"` are not valid: Sionna requires the single- or
multi-scattering-point variant. A model type the object type does not define
(e.g. model 2 on `uav-small-size`) is rejected with a 422 on save.

Other fields (both models unless noted):

| Field | Meaning |
|---|---|
| `size_m` | Target cuboid `[length, width, height]` m. Blank = the actor's own box size (`shape.size_m`) — not the TR 38.901 default above. Mesh actors should set it. |
| `velocity_m_s` | World-frame velocity of the target. Blank = the actor trajectory's tangent at t = 0; a static actor is at rest. |
| `random_components` | TR 38.901 only: draw σ_S, XPR and initial phases per direction pair (off = deterministic RCS). |
| `enabled` | Untick to keep the binding but skip the target. |

The target cuboid is centered at the actor base plus half its height and
takes the actor's `[yaw, pitch, roll]`. When the velocity comes from the
trajectory (blank `velocity_m_s` on an actor with waypoints), the whole pose
does too: the base sits at the trajectory's t = 0 position and the yaw faces
the direction of travel, exactly as playback shows the first frame, so the
scattering lobes and the Doppler describe the same motion. An explicit
`velocity_m_s` keeps the authored pose. Other actors with a trajectory keep
their authored pose in the solve but move at their t = 0 velocity, so legs
that reflect off them carry their Doppler.

## 3. Running a sensing solve

Place at least one TX and one RX. For **monostatic** sensing put an RX at the
TX position; any other RX gives a **bistatic** geometry. Then choose
**Actions ▾ → Sensing solve** in Results mode. The solve covers every actor
whose binding is enabled and persists as a `sensing` result set (it appears
in the run history like any other solve).

The same solve over the API:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/sensing \
  -H "Content-Type: application/json" \
  -d '{"config": {"backend": "auto", "frequency_hz": 28e9}, "include_comm_paths": false}'
curl http://127.0.0.1:8000/api/projects/demo/results/sensing   # latest stored result
```

| Request field | Meaning |
|---|---|
| `config_id` / `config` | Solver configuration, exactly as for `/simulate/paths`. |
| `tx_ids` / `rx_ids` | Override the config's device selection. |
| `target_actor_ids` | Restrict the solve to these actors (default: every enabled binding). |
| `max_depth` | RCS depth. It **counts the scattering event itself**, so `1` = direct legs only and each leg can bounce at most `max_depth − 1` times. Default `max(1, config.max_depth)`. |
| `samples_per_sp` | Rays launched per scattering point (default 1 000 000). |
| `include_comm_paths` | Also run the normal paths solve and append its paths after the echoes (`target_id` null, ids `path_…`). In that solve each target stands in for its actor as an absorber cuboid (how Sionna's `PathSolver` treats sensing targets), so no comm path also reflects off the target's own mesh. |

**Backends.** `sionna` runs `RCSSolver` (LoS, specular reflection and
refraction legs). `mock` evaluates the bistatic radar equation over one
scattering point at each target center (LoS legs, no occlusion). `auto`
picks sionna when sionna-rt ≥ 2.2 is installed and otherwise falls back to
the mock with a warning; an explicit `"backend": "sionna"` on an older
sionna-rt answers **409** (`sensing requires sionna-rt>=2.2`). Requests with
no bound actor, an unknown actor or device id, or no TX/RX answer **400**.

## 4. Reading Doppler

Each echo path carries `doppler_hz`, **positive when the path is closing**
(target approaching). For a single scattering point Sionna computes

`f_D = (v_tx · k̂₁ − v_rx · k̂₂ + v_t · (k̂₂ − k̂₁)) / λ`

with k̂₁ the unit vector TX → target and k̂₂ target → RX; a target receding
from a monostatic radar at v gives −2v/λ (−1868 Hz at 10 m/s, 28 GHz).

In the viewport, tick **Show: Sensing** in the Results panel: echo paths are
colored by Doppler (red approaching, gray static, blue receding) with a
legend, and every scattering point gets a marker. The **sensing card** lists
each target (model, scattering points, σ, path count) and the strongest
echoes (power, delay, Doppler).

## 5. Export

- **RFData** (`POST /export/rfdata`) writes `export/rfdata/sensing.json` when a
  sensing result exists: target summaries plus the echo paths
  (`type: "SENSING"`, `doppler_hz`, `target_id`); `has_sensing` in the summary.
- **AODT parquet** (`POST /export/aodt` with `"source": "sensing"`) writes one
  snapshot of the stored sensing result. AODT has no sensing token, so the
  target vertex uses `"scattering"`; its `object_ids` entry is
  1 000 000 + the target's position in **sorted actor-id order** (not the
  order of `targets`); map it back with `sensing_targets` in `id_map.json`.
- **Channel npz** (`POST /export/channel-npz` with `"include_sensing": true`)
  appends each echo to the UE × TX link whose UE sits at the echo's RX and
  whose TX still sits at the echo's TX (1 mm match) before strongest-first
  sorting; `is_nlos` ignores echoes and `sensing_path_count` reports how many
  were matched. The per-UE comm solves treat the result's targets as absorbers
  (as `include_comm_paths` does). A sensing result solved at another
  frequency answers **400**; echoes whose TX has moved since are skipped with
  a warning.

## 6. Limits

- Targets move by rigid translation only: no rotation, no micro-Doppler.
- The cuboid is the actor's box size; mesh actors need an explicit `size_m`.
- The actor's own mesh is removed from the scene during its sensing solve (its
  scattering model describes it completely) and restored afterwards.
- No diffuse-scattering or diffraction legs; sensing (echoes and
  `include_comm_paths` alike) always runs on the builtin sionna-rt engine.
- The mock uses one LoS scattering point at the target center with σ_M — no
  angular lobes, no multi-point layouts, no occlusion. Like Sionna, it has no
  LoS comm path between a co-located (monostatic) TX and RX.
- Random components are off by default; the solver runs with
  `deterministic=False`, so the same seed reproduces in practice but Sionna
  does not guarantee it.

## Related docs

- [simulation.md](simulation.md) — paths, radio maps, beamforming, channel analysis
- [trajectory_uav.md](trajectory_uav.md) — actor trajectories (the default target velocity)
- [datasets_export.md](datasets_export.md) — RFData / AODT / channel npz exports
- [../dynamic_scattering.md](../dynamic_scattering.md) — Doppler of moving devices and actors in plain paths solves
