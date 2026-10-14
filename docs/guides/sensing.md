# Radar sensing: RCS targets, echo solves, and Doppler

> **English** · [한국어](sensing.ko.md)

SEAM Studio can treat any actor (car, pedestrian, UAV, custom object) as a
**radar sensing target** and solve the echo paths TX → target → RX with
Sionna RT's radar cross-section solver (`sionna.rt.rcs.RCSSolver`, new in
sionna-rt 2.2). Every echo carries its own Doppler shift, so a moving target
shows up as a colored velocity signature in the viewport. Without sionna-rt
2.2 the whole workflow still runs on the **Mock backend** (bistatic radar
equation), so you can try it without a GPU.

The **Sample Demo** (v0.1.14 and later) is ready for sensing: it ships a
drone target (`uav_001`, TR 38.901 `uav-small-size`, flying a 90 m L at 40 m
and 10 m/s, then hovering) and a sensing receiver co-located with the rooftop
TX (`tx_001_rx`, "TX 1 sensing RX"), so the API examples below run on
`sample_demo` as they are. Since v0.1.15 it also has two more sensing sites
on street masts (`tx_002` with `tx_002_rx`, `tx_003` with `tx_003_rx`) and a
second stored config, `sensing_fr1` (3.5 GHz, 20 MHz), with which the
scenario of §6 detects and tracks the drone out of the box. A project
created by an earlier version keeps its devices, actors and configs (an
upgrade never rewrites a project). To get the current demo, create a fresh
one next to it, reload the page and pick **Sample Demo v2** in the project
select (or use `sample_demo_v2` in place of `sample_demo` in the API
examples):

```bash
curl -X POST http://127.0.0.1:8000/api/projects -H "Content-Type: application/json" \
  -d '{"name": "Sample Demo v2", "template": "demo", "project_id": "sample_demo_v2"}'
```

In a source checkout you can instead delete `projects/sample_demo.seam` and
restart: it is copied again with every addition. Or add a target and
co-located RXs to your own project (§2, §3).

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
that reflect off them carry their Doppler. Plain paths solves follow the
same rule (§4).

## 3. Running a sensing solve

Place at least one TX and one RX. For **monostatic** sensing put an RX at the
TX position; any other RX gives a **bistatic** geometry. Then choose
**Actions ▾ → Sensing solve** in Results mode. The solve covers every actor
whose binding is enabled and persists as a `sensing` result set (it appears
in the run history like any other solve).

The same solve over the API:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/sensing \
  -H "Content-Type: application/json" \
  -d '{"config": {"backend": "auto", "frequency_hz": 28e9}, "include_comm_paths": false}'
curl http://127.0.0.1:8000/api/projects/sample_demo/results/sensing   # latest stored result
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
scattering point at each target center (LoS legs, no occlusion), with each
device's element gain toward the target and the per-leg polarization term of
§8 (v0.1.14; earlier mock echoes were isotropic). `auto` picks sionna when
sionna-rt ≥ 2.2 is installed and has a working Dr.Jit backend (a CUDA GPU, or
LLVM on the CPU: see [INSTALL](../../INSTALL.md#the-real-sionna-rt-engine-installed-automatically)),
and otherwise falls back to the mock with a warning; an explicit
`"backend": "sionna"` on an older sionna-rt answers **409** (`sensing requires
sionna-rt>=2.2`). Requests with no bound actor, an unknown actor or device id,
or no TX/RX answer **400**.

**Antennas.** Sionna applies the first selected TX's (RX's) antenna to every
TX (RX) and warns when they differ; the mock applies each device's own
element pattern and orientation (and the polarization term of §8).

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

**Comm paths.** On the Sionna backend, paths solves give actors a velocity
too, so a communication path that reflects off a moving actor is
Doppler-shifted even when the TX and RX are static. Each bounce off an actor
moving at v adds `v · (k̂_out − k̂_in) / λ`, with k̂_in the unit vector into
the bounce and k̂_out the one leaving it. For example, a
car 20 m out, driving away at 10 m/s from a static TX/RX pair 8 m apart,
gives about −228 Hz at 3.5 GHz on its face reflection and 0 Hz on the LoS
and ground paths. Which velocity an actor gets:

- **Simulate scenario**: the actor's velocity at each frame's time.
- **Every other paths solve** (Simulate paths, channel analysis, datasets,
  UE trajectories, GT playback, `include_comm_paths`): the trajectory
  velocity at t = 0. These solves do not move actors between steps, so each
  actor stays at its authored pose for the whole run.
- **An actor without a trajectory** is at rest.
- **The actor whose path a dataset samples** (`sampling.actor_id`) is at
  rest as well: the UE travels that path while the actor's mesh stays
  parked at its authored pose.

A device attached to a moving actor (`attached_device_ids`) moves with it
at the actor's velocity, in every solve above and in a sensing solve.
Outside Simulate scenario, a device's own `velocity_m_s` takes precedence.
A UE riding a car is therefore Doppler-shifted on every path, the LoS
included.

A plain solve reports this Doppler in `metadata.doppler_hz`, a list aligned
with `paths`. The list is present when the TX or RX has a velocity or any
actor in the scene moves at t = 0, even if no path touches that actor (the
paths then read 0 Hz); an all-static solve has no Doppler field, as before.
The comm paths of a sensing result also carry it per path, in
`doppler_hz`. The mock backend has
no Doppler on paths solves, and alternate `engine`s (subprocess worker) apply
no velocities.

## 5. Export

- **RFData** (`POST /export/rfdata`) writes `export/rfdata/sensing.json` when a
  sensing result exists: target summaries plus the echo paths
  (`type: "SENSING"`, `doppler_hz`, `target_id`); `has_sensing` in the summary.
- **AODT parquet** (`POST /export/aodt` with `"source": "sensing"`) writes one
  snapshot of the stored sensing result. Each row runs `"emission"` …
  `"reception"`; AODT has no sensing token, so the target vertex uses its
  documented diffuse-scattering token `"diffuse"`; its `object_ids` entry is
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
- **Sensing dataset** (`POST /export/sensing-dataset`) flattens the sensing
  frames of scenario results into links and echoes tables (§10).

## 6. Sensing over time: detection and multistatic fusion

A sensing solve is one snapshot. **Simulate scenario** with sensing on runs a
sensing solve in **every frame**, at the frame's actor poses and velocities
(and rider positions), turns each echo into a per-link detection, and fuses
the detected links into a position/velocity estimate that is scored against
the true actor track.

**Running it.** In Results mode open **Scenario playback**, tick
**Sensing (ISAC)** under *Include paths*, set the threshold, CPI and
integrated pulses (the form defaults to 4096 since v0.1.15), pick the
**Sensing receivers** (default **Auto**, below), and run. The checkbox stays
disabled until at least one actor has an enabled sensing binding. The form
solves with the Simulation panel's config, which starts from the project's
first stored config (`default` on the Sample Demo), so for the demo run below
first pick the stored config **Sensing demo (3.5 GHz)** in that panel's
**Preset** dropdown: the run then uses `sensing_fr1` as is, and the result's
`simulation_config_id` is `sensing_fr1`, as with the API call below. Over the
API, add a `sensing` block to the scenario request:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/scenario \
  -H "Content-Type: application/json" \
  -d '{"config_id": "sensing_fr1", "num_frames": 19, "dt_s": 0.5, "include_paths": false,
       "sensing": {"enabled": true, "threshold_db": 13, "cpi_s": 0.01,
                   "cpi_pulses": 4096, "measurement_noise": true,
                   "tracking": {"enabled": true}}}'
```

A Sample Demo created before v0.1.15 has one site and no `sensing_fr1`
(this request answers 404 and the Preset dropdown lists no stored configs):
create a fresh demo as in the introduction, or, after adding the two sites
yourself, replace `"config_id": "sensing_fr1"` with the inline
`"config": {"frequency_hz": 3.5e9, "bandwidth_hz": 2e7, "noise_figure_db": 7, "max_depth": 2}`.

On the Sample Demo that covers the drone's 9 s flight. The demo has three
TRPs, each a 30 dBm iso TX with a co-located sensing RX: `tx_001` on the
roof of building b01 at (−9, 7, 10.5) m, and `tx_002` at (30, −30, 10) m and
`tx_003` at (35, 35, 10) m on street masts. Auto makes their three RXs the
radar receivers, so every frame has 9 links (3 monostatic, 6 bistatic) over
6 distinct geometries. The example runs the demo's second stored config,
`sensing_fr1` (3.5 GHz, 20 MHz, NF 7 dB, max depth 2): at 28 GHz / 100 MHz a
single element's drone echo stays under the 13 dB threshold even with 4096
pulses (+36 dB), because λ² costs 18 dB (*FR1 vs mmWave* below) and the 5×
bandwidth adds 7 dB of noise. At 3.5 GHz every link of the flight is at
15–33 dB, and every link has a direct echo in every frame, with or without
RF materials on the buildings and the tree. Measured (noise seed 0):

| `metadata.sensing.targets.uav_001` | mock | sionna (sionna-rt 2.2.0, CUDA) |
|---|---|---|
| `detection_rate` | 0.947 (18 / 19 frames) | 0.947 (18 / 19 frames) |
| `link_detection_rate` | 0.439 | 0.439 |
| `frames_ge3_links` | 15 | 15 |
| `ok_frames` (fusion) | 10 | 10 |
| `median_position_error_m` (fusion) | 0.43 m | 0.79 m |
| `median_gdop` | 1.62 | 1.62 |
| `tracked_frames` / `lost_frames` | 19 / 0 | 19 / 0 |
| `median_track_position_error_m` (EKF) | 0.36 m | 0.38 m |
| `p90_track_position_error_m` | 2.89 m | 2.98 m |
| `median_track_velocity_error_m_s` | 0.66 m/s | 0.66 m/s |

Every missed link is `mti_rejected`: at 3.5 GHz the 100 Hz notch
blinds a monostatic link below 4.28 m/s of radial speed, a large share of the
drone's 10 m/s. At t = 9 s the drone hovers and all 9 links are rejected (the
one frame without a detection). From t = 1.5 to 3 s only links between TX 2
and TX 3 survive (at t = 5 s, between TX 1 and TX 3): two sites, which the
fusion reports `diverged` (see *Fusion*). At t = 4–4.5 s only TX 1's
monostatic link does. The EKF carries the track through those frames
(`frames_track_without_fusion` = 9). It starts at t = 0 from a 5-link fusion
7.6 m off (GDOP 2.8: the drone starts outside the triangle of the three
sites), is 1.3–2.5 m off from t = 1.5 to 3 s and 0.4 m from t = 3.5 s,
restarts at the corner (t = 5.5 s, `init`), and stays within 0.1–0.4 m on
the second leg.

The other examples in this guide keep `"config_id": "default"` (28 GHz). With
4096 pulses its best drone link peaks at 7.7 dB, so nothing is detected, but
it does detect the drone when it integrates the whole 10 ms CPI, B · cpi_s =
10⁶ samples (+60 dB): the request above with `"config_id": "default"` and
`"cpi_pulses": 1000000` detects it in 18 of 19 frames on Sionna, with 18 `ok`
fusions, a 0.11 m median fusion error and a 0.08 m median track error (the
100 MHz range cell is 1.5 m, not 15 m). 10⁶ is the most a 10 ms CPI can
integrate at that bandwidth.

The result is an ordinary `scenario` result. Each frame gains
`sensing: {echoes, links, estimates}` and `metadata.sensing` holds the run
summary. With `sensing` absent or `enabled: false` the result is the same as
before (each frame's `sensing` is `null`). Comm and sensing share one backend:
`auto` resolves as for any scenario, and a backend without sensing (sionna-rt
< 2.2) answers **409** instead of falling back to the mock. No TX or no RX
answers **400**, and so does no bound actor, an unknown
`target_actor_ids` entry or a `sensing_rx_ids` entry that is not a selected
RX. The run holds the per-project solve lock and
reports progress per frame, and **Cancel** works.

A scenario with sensing always stores every frame's echoes (whatever
`include_paths` says): about 45 KB per frame on sionna with 16 links
(≈ 2.6 MB for 61 frames, ≈ 45 MB per 1 000 frames), which the browser also
loads. (Results are stored as compact JSON since v0.1.14; the pretty-printed
files of earlier versions were about twice that size.)

| `sensing` field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Run the per-frame sensing solve. |
| `threshold_db` | 13 | Detection threshold on the post-integration SNR (dB, −30…60). |
| `cpi_s` | 0.01 | Coherent processing interval (s). Doppler resolution = 1 / CPI. |
| `cpi_pulses` | 1 (the form: 4096) | Coherently integrated pulses or OFDM resource elements. Gain = 10·log10(N). |
| `mti_min_doppler_hz` | `null` | MTI notch: \|f_D − f_nodes\| below it is rejected (f_nodes = 0 with static TX/RX; see MTI below). `null` = 1 / CPI (one Doppler cell); `0` disables MTI. |
| `include_comm_paths` | `false` | Also solve each frame's comm paths with the targets as absorbers and append them to `echoes` (`target_id` null). One extra solve per frame. |
| `target_actor_ids` | `null` | Restrict to these actors (default: every enabled binding). |
| `sensing_rx_ids` | `null` | Bistatic radar receivers. `null` = auto: the selected RX within 1 m of a selected TX (as §7/§8), else every selected RX (v0.1.13). Comm links still cover every RX. Unknown id: 400. |
| `samples_per_sp` / `max_depth` | 1 000 000 / `null` | Passed to each frame's sensing solve, as in §3. |
| `measurement_noise` | `false` | Add Gaussian noise to the range and Doppler fed to the fusion (below). |
| `noise_seed` | 0 | Seed of that noise. Same seed = same numbers. |
| `tracking` | `null` | EKF tracking over the frames (see *Tracking (EKF)* below): `{"enabled": true}` plus optional `process_accel_sigma_m_s2` (2), `gate_chi2` (16), `coast_max_frames` (10), `max_position_std_m` (25), `use_as_prior` (`true`). `null` or `enabled: false` = no tracking. |
| `pfa` | `null` | False-alarm probability of a per-link `pd` (§9). `null` = the link reports carry no Pd. |
| `detector` | Swerling 1, no Monte Carlo | Model of that `pd`, and `monte_carlo_trials` for a Monte Carlo `pd_mc` per link with an echo (§9; needs `pfa`). Detection itself stays `SNR ≥ threshold_db`. |

**Sensing receivers.** Only the sensing receivers form radar links; every
selected RX keeps its comm link in the same frames. In the form, **Auto**
(the default) sends nothing; picking devices from the list sends
`sensing_rx_ids`. Auto is the §7/§8 rule: the selected RXs within 1 m of a
selected TX (monostatic/TRP receivers) when there is at least one, else every
selected RX. The resolved list and the rule (`explicit`, `colocated` or
`all_rx`) are stored in `metadata.sensing.sensing_rx_ids` and
`sensing_rx_rule`. **v0.1.14:** a project with co-located RXs now uses only
those as radar receivers by default; before, every selected RX (UEs
included) was a bistatic radar receiver. A project without co-located RXs
runs exactly as in v0.1.13.

**Detection.** For each TX → sensing RX link and target, the strongest **direct**
echo (TX → scattering point → RX, no other bounce) is reported. If there is
none, the strongest echo of any kind is reported and flagged `multipath`. Then

`SNR = P_echo − (−174 + 10·log10(B) + NF) + 10·log10(cpi_pulses)`  [dB]

with B = `config.bandwidth_hz` and NF = `config.noise_figure_db`.
`cpi_pulses` counts the samples integrated coherently over the CPI: radar
pulses, or OFDM resource elements (subcarriers × symbols). The full CPI caps
it at `B · cpi_s` (20 MHz × 10 ms = 2·10⁵, +53 dB). A link is **detected**
when `SNR ≥ threshold_db` and `|f_D − f_nodes| ≥ mti_min_doppler_hz` (below).

**MTI.** The notch defaults to one Doppler cell, 1 / CPI (100 Hz at 10 ms).
That rejects everything slower than the monostatic **blind speed**
λ · f_min / 2: 4.28 m/s at 3.5 GHz and 0.54 m/s at 28 GHz (10 ms CPI). With
static TX and RX, static scene returns sit at 0 Hz, so MTI is what separates
the drone from the buildings. A TX or RX riding a moving actor shifts the
static scene too, so the notch is centred on that shift: f_nodes =
(v_tx · k̂_dep − v_rx · k̂_arr) / λ, the Doppler a static scatterer on the
echo's own path would have (k̂_dep = TX → first bounce, k̂_arr = last
bounce → RX; 0 for static nodes). A hovering drone seen from a moving radar
is therefore rejected, and one flying alongside it is detected. The
reported `doppler_hz` stays the raw f_D. Comm paths and clutter are never
target echoes, so they never count as a detection either way. Set
`mti_min_doppler_hz: 0` to switch MTI off.

**Fusion.** Per target and frame, the detected direct echoes are sorted by
SNR, and links between the same two sites, in either order (a reciprocal
pair, or a repeated link), are kept once, the strongest. A site is a cluster
of TX/RX positions chained within 1 m, the co-location rule §7 and §8 use
too, so a sensing RX up to 1 m from its TX is the same focus (v0.1.12 merged
foci within 0.5 m). With at least **3** geometrically distinct links, the
position p solves the bistatic range sums

`|p − TX_i| + |p − RX_i| = R_i`,  R_i = c · τ_i

by Gauss–Newton. Each R_i is an ellipsoid with foci TX_i and RX_i (a sphere
for a monostatic link). Rooftop TRPs are nearly coplanar, so the ellipsoids
also meet at a mirror image across that plane. The solver therefore starts
from the previous frame's estimate (advanced by its velocity) and from
points above and below the TRPs; without a previous estimate it also starts
above each link's midpoint. A tie goes to the candidate nearest the previous
estimate. Without one it goes to the candidate nearest the ground (z = 0)
for a `car` or `human` target, and to the highest one otherwise (a drone is
assumed above the TRPs). With `measurement_noise` on, candidates whose cost
is within Δχ² ≤ 9 of the best (½·Σr² within 4.5σ̄², σ̄² = the mean of
σ² = ((c/B)/√(2·SNR))² over the links) also count as tied: with noisy ranges
the mirror often fits slightly better by chance. With exactly **3** links
every root fits exactly, so the cost cannot choose: without a previous
estimate, two or more distinct roots not underground (z ≥ −2 m) make the
estimate `diverged` (ambiguous). The velocity then
follows from a linear least-squares fit of the measured Doppler with the
convention of §4 (positive = closing):

`λ · f_D,i + v_rx · k̂₂ − v_tx · k̂₁ = v_t · (k̂₂ − k̂₁)`

Three links that touch only **two** nodes (TRP A monostatic, B monostatic and
A→B) are degenerate. All three ellipsoids are symmetric about the A–B axis,
so the solutions form a circle, and the estimate reports `diverged`.

**Reading the output.**

- `links[]` has one row per TX × sensing RX × target, in that order: `bistatic_range_m`,
  `doppler_hz`, `snr_db`, the `measured_*` values the fusion used, `range_bin`
  (floor(R / (c/B))), `doppler_bin` (round(f_D · CPI)) and `reason`:
  `detected`, `below_threshold`, `mti_rejected` or `no_echo`. With `pfa`
  set, `pd` (and `pd_mc`) give the detection probability of `snr_db` (§9);
  no echo gives `pd = pfa`.
- `estimates[]` has one row per target: `status` is `ok`,
  `insufficient_links` (< 3 distinct links) or `diverged` (no convergence,
  degenerate geometry, an ambiguous 3-link fit, or an RMS residual above one
  range-sum cell c/B). It
  also carries `links_used`, `position_est` / `velocity_est`, the truth
  (`position_true` = the target cuboid center, `velocity_true`), and their
  errors. `gdop` = sqrt(trace((JᵀJ)⁻¹)): the position error is about
  GDOP × the range error. With tracking on, it also carries the `track_*`
  fields (below).
- `nodes[]` lists every selected TX, then every sensing RX, with the position
  and velocity the frame used (a device riding an actor moves with it).
  Results from before v0.1.13 have `null` here.
- `metadata.sensing` holds the constants (noise floor, integration gain,
  λ, range and Doppler resolutions, MTI notch, blind speed), the
  `sensing_rx_ids` and `sensing_rx_rule` of the run and, per target,
  `detection_rate` (frames with ≥1 detected link), `link_detection_rate`,
  `frames_ge3_links_rate`, `ok_frames`, the median and p90 position error,
  the median velocity error and the median GDOP.

**Velocity truth.** `velocity_true` (and the Doppler the solver uses) is the
trajectory tangent × speed. At an exact waypoint time it is the outgoing
leg's velocity, at the end of a `once` trajectory it is 0 (the actor stops),
and at a pingpong turnaround it is the reversed leg. Before v0.1.14 those
frames reported the average of the two legs (7.07 m/s at a 90° turn at
10 m/s) or half the speed, so Doppler and `velocity_true` at waypoint frames
changed in v0.1.14.

Without `measurement_noise` the measured values are the solver's exact
delay and Doppler, so an `ok` estimate is exact to rounding (useful as a
sanity check). The one exception is an exact mirror tie: when all nodes of
the fused links lie in one plane (always so for three nodes), the mirror
across that plane fits exactly too, and the tie rule above picks the side.
With `measurement_noise` on, the range and Doppler get noise of
σ = cell / sqrt(2 · SNR), with cell = c/B for range and 1/CPI for Doppler.
The noise is seeded per frame, link and target. Detection decisions always
use the exact values.

**FR1 vs mmWave.** The echo SNR is

`SNR = P_t G_t G_r λ² σ N_int / ((4π)³ R_t² R_r² · kTB · NF)`

so at equal antennas, power and bandwidth the λ² term costs 28 GHz
20·log10(8) = **18.06 dB** against 3.5 GHz. Monostatic, P_t = 43 dBm,
isotropic antennas, `uav-small-size` (σ = −12.81 dBsm), B = 20 MHz, NF 7 dB,
`cpi_pulses` 4096 (+36.12 dB):

| f | λ (m) | P_r at 200 m | SNR at 200 m | Range at 13 dB | MTI blind speed (10 ms) |
|---|---|---|---|---|---|
| 3.5 GHz | 0.0857 | −116.17 dBm | 13.94 dB (detected) | 211 m | 4.28 m/s |
| 28 GHz | 0.0107 | −134.23 dBm | −4.12 dB (missed) | 74.6 m | 0.54 m/s |

The monostatic range therefore shrinks by 10^(−18.06/40) = ×0.354 at
28 GHz. Antenna arrays buy that back. The per-frame detection here still
counts single elements (see §11 Limits), but §7 synthesizes real codebook
beams and §8's `steered` mode adds the ideal array gain. The mmWave notch,
on the other hand, is 8× narrower in speed.

### Tracking (EKF)

Fusion needs three distinct links in one frame. With
`"tracking": {"enabled": true}` in the `sensing` block (or **Tracking (EKF)**
in the form), a constant-velocity extended Kalman filter per target carries
the estimate from frame to frame. A frame with only one or two detected
links still refines it, and a frame with none still predicts.

- **Motion.** State x = [p, v] in the world frame. Between frames (dt = the
  frame spacing) x ← F·x and P ← F·P·Fᵀ + Q, with discrete white-noise
  acceleration Q = σ_a²·[[dt⁴/4·I, dt³/2·I], [dt³/2·I, dt²·I]] and
  σ_a = `process_accel_sigma_m_s2` (2 m/s²).
- **Measurements.** Every detected direct link of the target, strongest
  first, gives two scalars: its range sum `measured_range_m`
  (h = |p − TX| + |p − RX|) and its Doppler `measured_doppler_hz`
  (h = (v_tx · k̂₁ − v_rx · k̂₂ + v · (k̂₂ − k̂₁)) / λ, the §4 sign). There is no
  geometry merge here: two links of one ellipsoid are independent receiver
  draws. σ_R = (c/B)/√(2·SNR) and σ_f = (1/CPI)/√(2·SNR) (at least 1 mm and
  1 mHz), the `measurement_noise` rule, whether the noise is on or not. A
  moving TX or RX enters with its frame position and velocity.
- **Update.** One scalar at a time, re-linearized at the current state:
  ν = z − h(x), S = H·P·Hᵀ + σ². A scalar with ν²/S > `gate_chi2` (16 = 4σ,
  chi-square with 1 dof) is rejected and counted in `track_gated`. Otherwise
  the Joseph-form update runs and it counts in `track_updates`.
- **Start.** The first frame whose fusion is `ok` starts the track at the
  fused position and velocity (0 without a Doppler velocity). P starts at
  max(GDOP²·σ̄_R²/3, 0.25 m²) per position axis (σ̄_R² = the mean σ_R² of the
  fused links; 100 m² without a GDOP) and 25 (m/s)² per velocity axis
  (400 without a velocity). That frame does not also update with the same
  data.
- **Restart.** The track restarts from the frame's fusion when two things
  hold together: the gate rejected at least one of the frame's scalars, and
  the fusion is `ok` but outside the predicted track's 3-D gate. That gate
  is a Mahalanobis distance² over P_pred + the start covariance above the
  chi-square (3 dof) value with the tail of `gate_chi2` (22.1 for 16). A
  waypoint corner is the usual case: the prediction keeps the old velocity,
  the Doppler scalars that would correct it are gated, and the range sums
  alone cannot turn it. The fusion test alone is not enough: the fusion's
  error has heavier tails than its covariance says, so on a straight path
  an occasional outlier fusion would replace a good track even though every
  raw scalar of that frame agrees with it. Such a frame updates the track
  instead.
- **Coast and loss.** A frame without an accepted scalar is `coasting`
  (prediction only, P grows). On the frame that makes more than
  `coast_max_frames` (10) of them in a row the track is dropped (`lost`). It
  is also dropped once its posterior `track_position_std_m` exceeds
  `max_position_std_m` (25 m): long stretches on one or two links keep it
  `tracking` while it drifts along the directions they do not observe.
  Either way it starts again at the next `ok` fusion.
- **Fusion prior.** With `use_as_prior` (default) the frame's Gauss–Newton
  fusion starts from the track's prediction instead of the last `ok`
  estimate advanced by its velocity. With `false` every fusion field is
  exactly what it would be without tracking.

Each `estimates[]` row then carries:

| Field | Meaning |
|---|---|
| `track_status` | `none` (no track yet), `init` (started or restarted from this frame's fusion), `tracking` (≥ 1 scalar accepted), `coasting` (none accepted), `lost` (dropped; until the next start) |
| `track_position`, `track_velocity` | Posterior state (`null` for `none` and `lost`) |
| `track_position_error_m`, `track_velocity_error_m_s` | Against `position_true` and `velocity_true` |
| `track_position_std_m` | sqrt(trace(P_pos)): the 3-D RMS error the filter expects, comparable to `track_position_error_m` |
| `track_updates`, `track_gated` | Scalars accepted and gated in this frame (0 and 0 on `init`; still counted on the frame the track is lost, `null` after it) |

With tracking off all of them are `null`. Per target, `metadata.sensing`
adds `tracked_frames` (`init`, `tracking` or `coasting`), `coasting_frames`,
`lost_frames`, `lost_by_std_frames` (drops by the `max_position_std_m` cap;
a frame whose own fusion restarts the track at once shows `init`, not
`lost`), the median and p90 track position error, the median track
velocity error, `frames_improved_over_fusion` (fusion `ok` and the track
closer to the truth) and `frames_track_without_fusion` (a track in a frame
whose fusion is not `ok`). The run adds `median_track_position_error_m` and
`tracking_model`.

### Waveform numerology (#23)

Without a waveform, `cpi_pulses` is a free number and the integration gain
is `10log10(cpi_pulses)` whatever `cpi_s` is (v0.1.15, still the default).
`sensing.waveform` replaces it with an OFDM numerology: the pilot resource
elements (REs) of one CPI set the gain, and the occupied bandwidth sets the
noise floor and the resolutions. The same object is accepted by the ISAC
request (`waveform`), the coverage request and `/analysis/pd-curve`
(`waveform` plus `cpi_s`, default 0.01 s) and channel analysis (`waveform`:
the RSRP/RSRQ grid then has `n_active_sc` subcarriers at `scs_hz`, e.g. 51
PRB / 612 SC in 20 MHz at 30 kHz instead of `int(B / (12 scs))` = 55 RB).

| Field | Default | Meaning |
|---|---|---|
| `scs_hz` | 30e3 | Subcarrier spacing Δf [Hz] |
| `n_fft` | required | FFT size (≥ `n_active_sc`) |
| `n_active_sc` | 612 | Active subcarriers; B_occ = `n_active_sc` · Δf |
| `cp` | `normal` | `normal` or `extended` |
| `symbol_duration_s` | `null` | Average symbol duration incl. CP; `null` = NR slot timing (Δf = 15 · 2^μ kHz: (1 ms / 2^μ) / 14, or / 12 with the extended CP), else (1/Δf)(1 + 144/2048 or 512/2048) |
| `symbols_per_cpi` | `null` | `null` = floor(`cpi_s` / T_sym) |
| `pilot.pattern` | `comb` | `comb` or `block` (block needs `subcarrier_step` 1) |
| `pilot.subcarrier_step`, `pilot.symbol_step` | 1, 1 | Pilots on every n-th active subcarrier of every m-th symbol, from index 0 |

The pilot lattice and everything derived from it:

```
K = ceil(n_active_sc / subcarrier_step)      N = ceil(symbols / symbol_step)
cpi_pulses = K N        integration gain = 10 log10(K N)
B_occ = n_active_sc Δf  noise floor = -174 dBm/Hz + 10 log10(B_occ) + NF
range-sum cell c / B_occ (monostatic range c / 2 B_occ)
delay bin 1 / (K · subcarrier_step Δf)     Doppler cell 1 / (N · symbol_step T_sym)
pilot axes  f_k = (k · subcarrier_step - n_active_sc // 2) Δf,   t_j = j · symbol_step T_sym
```

Amplitude and noise convention (shared by every Track B feature): a path's
channel coefficient is dimensionless, `a = 10^(path_gain_db / 20) e^(j phase_rad)`
(`phase_rad` already holds −2π f_c τ); every active subcarrier carries
Es = P_tx / `n_active_sc` per RE (CP excluded) and the noise per RE is
N0 = −174 dBm/Hz + 10log10(Δf) + NF. A path's per-RE SNR |a|² Es / N0 is
then P_echo − N_floor(B_occ), and `snr_db = P_echo − N_floor(B_occ) + 10log10(K N)`
is the matched-filter SNR of the K N pilot REs.

| Preset, `cpi_s` | T_sym | Symbols | K × N | Gain | B_occ | c / B_occ | Doppler cell | Floor (NF 7 dB) |
|---|---|---|---|---|---|---|---|---|
| NR 30 kHz 51 PRB, 10 ms | 0.5 ms / 14 | 280 | 612 × 280 = 171,360 | 52.34 dB | 18.36 MHz | 16.33 m | 100 Hz | −94.36 dBm |
| NR 30 kHz 51 PRB, 50 ms | | 1400 | 612 × 1400 = 856,800 | 59.33 dB | | | 20 Hz | |
| Grid S, 10 ms | 1/14400 s | 144 | 256 × 16 = 4096 | 36.12 dB | 20.0 MHz | 14.99 m | 100 Hz | −93.99 dBm |
| Grid S, 50 ms | | 720 | 256 × 80 = 20,480 | 43.11 dB | | | 20 Hz | |

NR 30 kHz 51 PRB is `{"scs_hz": 30000, "n_fft": 1024, "n_active_sc": 612}`;
Grid S (the 4096-pulse default of the v0.1.15 demos, read as a pilot grid)
is `{"scs_hz": 15625, "n_fft": 2048, "n_active_sc": 1280,
"symbol_duration_s": 0.0000694444444444, "pilot": {"subcarrier_step": 5,
"symbol_step": 9}}` (pass 1/14400 exactly from code). At the same pilot
density a 50 ms CPI adds 10log10(5) = 6.99 dB over 10 ms; Grid S at 10 ms
reproduces the v0.1.15 numbers (4096 pulses on the 20 MHz floor).

With a waveform, `metadata.sensing` (and the ISAC, coverage and pd-curve
metadata) gains `cpi_pulses` (= K N), `b_occ_hz`, `n_re_per_cpi`,
`symbols_per_cpi`, `symbol_duration_s`, `pilot_fraction`, `grid_k`,
`grid_n`, `n0_dbm_per_re` and the full `numerology` dict, and
`noise_floor_dbm`, `integration_gain_db`, the resolutions and
`velocity_resolution_m_s` follow it. `bandwidth_hz`, `cpi_s` and the MTI
keys keep their v0.1.15 meaning. Two warnings keep old requests honest:
`cpi_pulses` set together with a `cpi_s` other than 0.01 s and no waveform
("integration gain does not follow cpi_s without sensing.waveform"), and a
`cpi_pulses` that differs from the waveform's K N ("cpi_pulses 4096
overridden by the waveform numerology: 171360 pilot REs per CPI (612 x
280)"); the stored request keeps the number you sent. A `cpi_s` shorter
than one symbol, `symbols_per_cpi` that do not fit the CPI, or more than
10⁸ pilot REs per CPI answer 400 before any solve on every route that takes
a waveform: scenario, ISAC, sensing coverage and `/analysis/pd-curve`. The
coverage request warns "cpi_s is unused without waveform" when it gets a
`cpi_s` and no waveform. Channel analysis has no `cpi_s`: its
`metadata.numerology` covers `symbols_per_cpi` symbols when that is set,
else 10 ms (at least one symbol), and only the subcarrier count and spacing
enter RSRP/RSRQ, so any waveform the other routes accept is accepted there.

```json
"sensing": {"enabled": true, "cpi_s": 0.05, "threshold_db": 13,
            "waveform": {"scs_hz": 30000, "n_fft": 1024, "n_active_sc": 612}}
```

### Background paths H0/H1 (#21)

`include_comm_paths` appends the targets-as-absorbers comm paths to
`echoes`: the background with the target present (H1). A false-alarm
calibration needs the background with the target absent (H0), solved in
the same frame, and the RCS solver traces no diffuse clutter.
`sensing.background` solves both with PathSolver over the TX → sensing-RX
links of every frame:

- **H1** (`h1_absorber`): every target actor's mesh hidden and the target
  added as an absorber (the `include_comm_paths` mechanism).
- **H0** (`h0_removed`): every target actor's mesh hidden, nothing added.

| Field | Default | Meaning |
|---|---|---|
| `mode` | `off` | `off`, `h1_absorber`, `h0_removed` or `both` |
| `diffuse_reflection` | `true` | Diffuse clutter (the materials' scattering coefficients) |
| `diffraction`, `edge_diffraction` | `false` | |
| `max_depth`, `num_samples` | `null` | `null` = the config's |
| `deterministic` | `true` | `PathSolver(deterministic=True)`: two runs give the same paths |
| `max_paths_per_link` | 256 | Keep the strongest n paths per link and solve; `null` keeps every path. `dropped_paths` counts the rest, and one run warning sums them |
| `cache` | `static_nodes` | `static_nodes`: solve once while the key repeats; `per_frame`: every frame |
| `store` | `h0` | Hypotheses kept in the result: `h0` (H1 feeds the CFAR map in memory only), `both`, or `none` (maps only) |

LoS, specular reflection and refraction follow the config. Each frame
stores `background_h0` / `background_h1` (lists of `RayPath`: `power_dbm`,
`path_gain_db`, `phase_rad`, `delay_ns`, `doppler_hz` always a float,
`interactions` with type, `prim_id` and `rf_material_id`, `target_id`
`null`) and `background_meta` (`mode`, `mechanisms`, `max_depth`,
`num_samples`, `seed`, `deterministic`, and per hypothesis `solved`,
`cache_hit`, `source_frame`, `cache_key`, `solve_s`, `num_paths`,
`dropped_paths`, `total_power_db` = 10log10 Σ|a|² per `"tx>rx"` of the kept
paths, `stored` and, after a failed solve, `error`). The paths come in a
canonical order (link, then delay, interactions and vertices; renumbered
`path_0001`…): the GPU solver returns the same set in a run-dependent order.

`store` decides which solved hypotheses the result keeps. With the default
`h0`, H1 is solved whenever the mode needs it and feeds the CFAR map, but
`background_h1` stays out of the frame (`background_meta.h1.stored:
false`); `both` keeps H1 too (needed to export `background_h1` or `total`
channels, §10) and `none` keeps neither. The map is the same in all three.
`max_paths_per_link` keeps the strongest paths of each link (by power): the
sionna backend drops the rest before it turns solver paths into rows, and
the run ends with one warning "background: max_paths_per_link 256 kept the
strongest 256 paths per link: … of … solved background paths dropped over
the run". The dropped paths are the weak diffuse tail, so
`total_power_db` (of the kept paths) changes little; set `null` to keep
every path.

The `static_nodes` key hashes the RF projection fingerprint, the solver
options, every TX and sensing RX position and velocity, every non-target
actor's pose and velocity, and for H1 the target cuboids (H0 only needs
which actors are removed). A frame with the key of an earlier frame stores
no paths: `background_meta.h0.source_frame` names the frame that holds
them (`services.sensing_background.resolve_background(frames, i, "h0")`
follows it, and returns `null` for a hypothesis that was not stored). With
static nodes and a static scene H0 is solved once per run; a moving TX/RX or
car re-solves every frame. A run keeps the last 4 solved keys per
hypothesis in memory. A failed solve (the sionna backend degrades to no
paths and a warning) is never cached: that frame's meta carries `error`,
its map falls back to H0 (or to no background), and the next frame solves
again. `metadata.sensing.background` sums the run up (`store`,
`max_paths_per_link`, `solves_h0`, `cache_hits_h0`, `failed_solves_h0`,
`solve_s_total`, `paths_stored`, `paths_dropped`, …) and
`metadata.sensing.sensing_tx_ids` lists the TXs.

With a background the run may have no target at all (no bound actor):
`links` and `estimates` are empty and the frames carry the background only
(a background-only H0 run for calibration). Without one, a scene with no
target still answers 400 "no sensing targets". The other frame fields are
unchanged by the background (`echoes`, `links`, `estimates`, `nodes`).

Diffuse clutter is many paths: the test scene of a 300 m baseline over a
ground slab and one building gives 16,776 paths per link at 400,000 samples
(about 10 MB of JSON per solved hypothesis), which is why the default keeps
256 per link. The mock backend's background is its LoS, ground and wall
bounce per link (H1 drops the paths whose legs cross a target cuboid), with
the Doppler of the TX/RX motion.

A moving target changes the H1 key, so H1 is solved again every frame even
when every node is static. On the `isac_drone_fr1` demo (16 links, config
`max_depth` 2, 200,000 samples) one background solve gives 64,356 paths
(about 4,000 per link). Measured over 8 frames (NR 612 × 280, mode `both`,
CFAR): with every path kept and H1 stored, 11.1 s per frame (H1 3.9 s per
solve) and a 403 MB result; with the defaults (256 per link, H1 not
stored), 1.2 s per frame (H1 0.28 s per solve, 4,025 paths) and a 4.6 MB
result, with the same CFAR maps whatever `store` is. Before the cap moved
ahead of the row conversion, a capped H1 solve still took about 2.6 s.
`h0_removed` with static nodes solves once per run (0.6 s per frame with
CFAR on the same demo).

```json
"sensing": {"enabled": true, "waveform": {"scs_hz": 30000, "n_fft": 1024, "n_active_sc": 612},
            "background": {"mode": "both", "store": "both", "max_paths_per_link": 2000}}
```

## 7. ISAC trade-off: beams and slot sharing

One TRP panel cannot point at its UEs and at a drone at the same time. The
ISAC trade-off answers three questions per TX: which codebook beam serves
its UEs best (the **comm beam**), which one sees the targets best (the
**sensing beam**), and what sharing slots between the two costs in rate and
buys in detection probability.

**Running it.** In Results mode open the **ISAC trade-off** panel, pick the
TXs (none ticked = all), the array size, the sweep, CPI pulses, P_fa, the
slot ratios and the sharing mode, and run. Over the API:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/isac \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "tx_rows": 4, "tx_cols": 4,
       "sweep_start_deg": -60, "sweep_stop_deg": 60, "sweep_step_deg": 5,
       "cpi_pulses": 4096, "pfa": 1e-6, "sharing_mode": "dual_function"}'
curl http://127.0.0.1:8000/api/projects/sample_demo/results/isac   # latest stored result
```

The result persists as an `isac` result set (run history, prune, label).
Like `/simulate/sensing`, `auto` uses sionna when sionna-rt ≥ 2.2 is
installed and otherwise the mock with a warning, and an explicit sionna
without the RCS solver answers **409**. An unknown device or actor, no UE,
a TX without a sensing receiver, or `use_device_orientation: false` answers
**400** before anything is solved.

On the Sample Demo with `"config_id": "sensing_fr1"` (Sionna, drone at its
t = 0 position), the default azimuth-only 4×4 codebook serves `rx_001` from
TX 2 (comm beam 55°) and sees the drone with TX 1 (sensing beam −35°,
31.9 dB, P_d 0.99) and TX 3 (−40°, 23.4 dB, P_d 0.94), but not with TX 2:
from its 10 m mast the drone is 28° up, in the vertical null of an
elevation-0 beam of a 4-row panel (−1.0 dB). Adding the elevation sweep
`"elevation_start_deg": -10, "elevation_stop_deg": 60, "elevation_step_deg": 5`
gives every TRP a detecting sensing beam (51.5, 44.9 and 39.4 dB at 40°, 30°
and 20° elevation), and TX 1 then serves the UE (−20° azimuth, −10°
elevation).

| Request field | Default | Meaning |
|---|---|---|
| `tx_ids` | `null` | TXs to evaluate (default: every TX). |
| `ue_rx_ids` / `sensing_rx_ids` | `null` | Role split; see *Roles* below. |
| `target_actor_ids` | `null` | Targets (default: every enabled sensing binding). |
| `ue_association` | `serving` | `serving`: each UE belongs to the TX with the strongest best-beam RSS. `all`: every TX serves every UE (single-cell view per TX). |
| `tx_rows` × `tx_cols` | 4 × 4 | TX panel. Element spacing comes from the device antenna. |
| `rx_rows` × `rx_cols` | `null` | Sensing-RX panel (default: the TX size). UEs are always single elements. |
| `use_device_orientation` | `true` | Panels keep their `orientation_deg`. `false` is rejected: `look_at` cannot aim one panel at both its UEs and its targets. |
| `sweep_start_deg` / `sweep_stop_deg` / `sweep_step_deg` | −60 / 60 / 5 | Azimuth codebook in the panel's local frame, exactly as `/simulate/beamforming`'s sweep (at most 361 beams, and at most 4096 beams × nonzero slot ratios per TX). |
| `cpi_pulses` | 4096 | Coherently integrated pulses / OFDM resource elements at ρ = 1. |
| `cpi_s` | 0.01 | Metadata only (resolutions). |
| `threshold_db` | 13 | Per-beam `detected` flag. |
| `pfa` | 1e-6 | False-alarm probability of the Pd model. |
| `pd_target` | 0.9 | Operating point of the Pareto summary. |
| `slot_ratios` | 0, .05, .1, .2, .3, .5, .7, 1 | Fractions ρ of the slots (pulses) spent sensing; sorted and deduped. |
| `sharing_mode` | `dual_function` | `time_sharing` or `dual_function` (below). |
| `samples_per_sp` / `max_depth` | 1 000 000 / `null` | Passed to the echo solve, as in §3. |
| `include_paths` | `false` | Store the solve's paths in the result: echoes first, then the comm paths. |
| `elevation_start_deg` / `elevation_stop_deg` / `elevation_step_deg` | `null` | Elevation sweep of a 2-D codebook in the panel's local frame (all three or none; at most 361 elevations and 4096 beams in all). `null` = the azimuth-only codebook. See *Elevation codebook*. |
| `interference` | `false` | Inter-TX interference in the UE SINR (needs `ue_association: serving`). See *Interference*. |
| `detector` | Swerling 1, no Monte Carlo | P_d model, Monte Carlo trials, empirical threshold and seed (§9). |

**Roles.** With `ue_rx_ids` and `sensing_rx_ids` both unset, an RX within
1 m of a selected TX is that TX's sensing receiver (monostatic). Every RX
that is not within 1 m of any TX of the scene is a UE, so evaluating a
subset of TRPs never turns the other TRPs' radar receivers into served
users. Each TX takes the nearest RX of the sensing pool, so several TXs may
share one (bistatic) when you pass `sensing_rx_ids`. `ue_rx_ids` overrides
the UE rule. An RX cannot be both a UE and a sensing receiver.

**Model.** One t = 0 solve pair: the echo solve (TX → target → sensing RX,
§3) and the comm solve (TX → UE with the targets as absorbers, as
`include_comm_paths`). Both run with every antenna forced to a single
element, so each stored path is referenced to the panel center. The beams
are then synthesized from the paths' world-frame departure/arrival angles
in Sionna's synthetic-array convention. Element n of a panel with
orientation o responds to a wave along world direction k̂ with

`a_n = exp(+j 2π p_n · R(o)ᵀ k̂)`,

where p_n is the PlanarArray element position in wavelengths. Beam w scores
w^H a on both ends: the conjugate-weight matched filter of
`/simulate/beamforming`. On sionna the synthesized TX codebook reproduces
`/simulate/beamforming` (`codebook_sweep`, `use_device_orientation: true`)
beam for beam: the regression tests see the same best beam, and every beam
within 20 dB of the peak agrees within 0.1 dB (measured: under 0.01 dB).
Per TX t, codebook beam k and UE u:

- `h_k,u = Σ_l α_l · w_kᴴ a_t(l)` over the paths t → u (α_l from
  `path_gain_db` and `phase_rad`),
  `SINR_k,u = P_t + 20·log10|h_k,u| − N0`, with
  N0 = −174 + 10·log10(B) + NF. Without `interference` there is no
  inter-TX interference, so SINR = SNR (see *Interference*).
- `rate_k,u = log2(1 + SINR_k,u)` and `R(k) = Σ_u rate_k,u` over the UEs
  this TX serves.
- Echo of target q with TX beam k and RX beam m, every echo t → s → q
  summed coherently: `E_q[k, m] = Σ_e α_e (w_kᴴ a_t(e)) (w_mᴴ a_s(e))`. The
  best RX beam is taken per TX beam, and

  `SNR_q(k, ρ) = P_t + 20·log10 max_m |E_q[k, m]| − N0 + 10·log10(ρ · cpi_pulses)`.

  The weakest target sets the sensing SNR of a beam.

**Elevation codebook.** With the three `elevation_*` fields set, the
codebook is every azimuth of the sweep at every elevation, elevation outer:
beam k = i_el · n_az + i_az. Beam (φ, θ) steers toward the panel-local
direction [cos θ cos φ, cos θ sin φ, sin θ]:

`w_n(φ, θ) = exp(+j 2π (y_n cos θ sin φ + z_n sin θ)) / sqrt(N)`.

On the planar grid this is kron(w_z(θ), w_y(φ, θ)). It has unit norm and
the full 10·log10(N) toward its own direction, and at θ = 0 it is the
azimuth beam above. The sensing RX uses the same grid, and its best beam is
taken over all of it. A drone at local elevation 32.6° on a 4×4 panel shows
what this buys: the best azimuth-only beam gives −9.84 dB per end, the
−10…40° / 5° grid 11.98 dB, so about 22 dB comes back per end. On sionna,
the elevation beams of a 4×1 vertical ULA (TX side and RX side) match the
same steering vectors applied to Sionna's own synthetic-array channel. In
the regression tests the best beam agrees and sits on the LoS elevation,
and every beam within 20 dB of the peak agrees within 0.1 dB (measured:
under 0.005 dB). Without the fields the azimuth-only code runs unchanged.

**Interference.** With `interference: true` (only with `ue_association:
serving`), a UE served by TX t hears every other selected TX that has a
comm path to it. All TXs share ρ and the slot timing (synchronized slots).
In comm slots each other TX radiates its comm beam c (none if it serves
nobody); in sensing slots it radiates its sensing beam s (none without an
echo). With RSS_t2,u(k) = P_t2 + 20·log10|h_k,u| of TX t2, in mW:

`I_comm(u) = Σ_t2 10^(RSS_t2,u(c_t2)/10)`, `I_sens(u) = Σ_t2 10^(RSS_t2,u(s_t2)/10)`

`SINR_k,u = RSS_t,u(k) − 10·log10(10^(N0/10) + I_comm(u))`.

Each TX's comm beam depends on the others' comm beams, so the choice is
iterated. Round 0 is the interference-free choice. Each later round lets
every TX in turn pick the beam with the best interference-aware sum rate
given the others' current beams. It stops when a round changes nothing, or
after 10 rounds with a warning (the last round's beams are reported).
Sensing beams do not depend on interference, and echo SNRs stay
noise-limited. With one selected TX nothing changes: SINR = SNR exactly.

**Pd.** A Swerling-1 target with a square-law detector on the integrated
sample has P_fa = e^(−T) and P_d = e^(−T / (1 + SNR)), so

`P_d = P_fa^(1 / (1 + SNR))`.

At P_fa = 10⁻⁶, P_d = 0.9 needs **21.1 dB**, and the 13 dB threshold gives
P_d = **0.517**. No echo gives P_d = P_fa. Swerling 1 is the default
`detector.model`; §9 has Swerling 0 and 3 and the Monte Carlo. With
`detector.monte_carlo_trials` > 0 every beam with an echo and every Pareto
point with ρ > 0 also get `pd_mc`. A run may spend at most 2·10⁸ trials,
counted as TXs × beams × (1 + nonzero slot ratios) × trials (that is
250 000 trials for the default request with 4 TXs). A larger request
answers **400** before anything is solved.

**Slot sharing.** The comm beam k_c = argmax R(k). A fraction ρ of the slots
(or pulses) senses with beam k, so the echo integrates ρ · `cpi_pulses`.

- `time_sharing`: sensing slots carry no data. rate_u = (1 − ρ) · rate_k_c,u.
- `dual_function`: the sensing beam also carries data to whoever it reaches.
  rate_u = (1 − ρ) · rate_k_c,u + ρ · rate_k,u.

ρ = 0 is one comm-only point (`beam_idx` null, P_d = P_fa). With
`dual_function` the ρ = 1 rows equal the beams themselves (one beam does
both). With `time_sharing` they carry no rate. With `interference` each
slot sees the other TXs' beams of that slot, with S the interference-free
signal power:

- `time_sharing`: rate_u = (1 − ρ) · log2(1 + S_k_c,u / (N0 + I_comm)).
- `dual_function`: rate_u = (1 − ρ) · log2(1 + S_k_c,u / (N0 + I_comm)) +
  ρ · log2(1 + S_k,u / (N0 + I_sens)).

Under these synchronized slots the rate can rise with ρ: when the other TXs'
sensing beams reach a UE more weakly than their comm beams (I_sens <
I_comm), a `dual_function` point beats the comm-only rate, and its loss
against comm-only comes out negative. That is the model, not a bug.

**Reading the output.** Each `txs[]` entry holds:

- `beams[]`: one row per codebook angle, with each UE's SINR, the sum rate,
  each target's echo SNR and best RX angle, the weakest-target SNR, P_d and
  `detected`. The panel's beam table tints the comm-beam row cyan and the
  sensing-beam row magenta, and the **ISAC lobes** overlay draws both beams
  on each TX in the same colors (see §11 for what it leaves out). With an
  elevation sweep each beam also has `elevation_deg` and, per target,
  `target_best_rx_elevation_deg`, and the overlay draws the azimuth cuts of
  the elevation slice that holds the comm (sensing) beam. With
  `interference` each beam also has `ue_snr_db` (interference-free) and
  `ue_interference_dbm` (I_comm; null when no other TX reaches the UE).
- `comm_beam_angle_deg`, `sensing_beam_angle_deg` and `angle_gap_deg` (the
  azimuth gap). `angles_deg` stays aligned with `beams` (each azimuth
  repeated per elevation). With an elevation sweep `elevations_deg` is
  aligned the same way, and `comm_beam_elevation_deg`,
  `sensing_beam_elevation_deg` and `elevation_gap_deg` complete the pair.
  With `interference`, `ue_interference_sensing_dbm` gives I_sens per UE.
  `ue_single_element_rss_dbm` and `target_single_element_snr_db` are 1×1
  anchors for hand checks. For a target the array gain of a beam is
  `target_snr_db − target_single_element_snr_db`. For a UE it is
  `ue_sinr_db + noise_floor_dbm − ue_single_element_rss_dbm`, because the
  UE anchor is an RSS in dBm, not an SINR.
- `points[]`: every (ρ, sensing beam) operating point with its sum rate,
  SNR, P_d and `pareto` flag. The **Pareto chart** plots rate against P_d.
  The front holds the points no other point beats on both axes (duplicates
  count once).
- `pareto`: `comm_only_rate_bps_hz` (ρ = 0), `max_pd`,
  `rate_at_pd_target_bps_hz` / `rho_at_pd_target` / `beam_idx_at_pd_target`
  (the best rate among points with P_d ≥ `pd_target`; null when none
  reaches it), `rate_loss_at_pd_target_bps_hz`, and `pd_at_95pct_rate` (the
  best P_d that keeps 95 % of the comm-only rate). Rates within 10⁻¹² tie,
  and a tie goes to the higher P_d, so the named point is on the front. In
  `dual_function` the comm beam keeps its full rate at every ρ, so when it
  reaches the target it is named at ρ = 1, not at the smallest ρ that
  qualifies. A sensing-only TX (all rates 0) names its highest-P_d point.

The top level adds `ue_serving_tx`, the `noise_floor_dbm` and, in `metadata`,
the detection constants of §6 plus `snr_for_pd_target_db` and
`pd_at_threshold` (both under `detector.model`). A TX that serves no UE gets
a sensing-only trade-off (all rates 0) and says so in its `warnings`. Keys
appear only with their option: `codebook_shape` ([n_el, n_az]) with an
elevation sweep; `interference`, `ue_interferers` (per UE, the other TXs
that reach it), `interference_rounds`, `interference_converged` and
`interference_model` with `interference`; `detector` (model, threshold and
the measured P_fa of the Monte Carlo) with a non-default `detector`. A
request with every new field at its default stores the same
`metadata.request` and `request_hash` as v0.1.12.

## 8. Sensing coverage map

Where would a drone at 60 m be seen, and by how many links? The coverage
map puts a **virtual point target** in every cell of a horizontal grid and
evaluates every TX × sensing-RX link (monostatic and bistatic). No RCS solve
runs, so a whole map takes about a second.

**Running it.** In Results mode open the **Sensing coverage** panel, set
the height, cell size, RCS (blank = TR 38.901 `uav-small-size`,
−12.81 dBsm), threshold, pulses, P_fa and the array gain, and run. The
metric selector switches the map between the four layers below. Over the
API:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/sensing-coverage \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "height_m": 60, "cell_size_m": 10,
       "threshold_db": 13, "cpi_pulses": 4096, "array_gain": "none"}'
curl http://127.0.0.1:8000/api/projects/sample_demo/results/sensing-coverage
```

The result persists as a `sensing_coverage` result set. Sensing receivers
follow the ISAC rule (an RX within 1 m of a selected TX, or
`sensing_rx_ids`). A TX without one answers **400**.

On the Sample Demo with `"config_id": "sensing_fr1"`, `height_m` 40 and
`cell_size_m` 5 (Sionna), the map has 441 cells and 9 links over 6
geometries: 100 % of the cells are LOS, detected and fusion-feasible, with a
median best SNR of 29.3 dB. The map has no MTI, so the scenario of §6 still
loses links to the Doppler notch along the actual flight.

| Request field | Default | Meaning |
|---|---|---|
| `tx_ids` / `sensing_rx_ids` | `null` | Devices (default: every TX and its co-located RX). |
| `rcs_dbsm` / `object_type` | `null` | Point-target σ: a value, or a TR 38.901 type's mean σ_M (not both). Both unset = `uav-small-size`. |
| `height_m` | 60 | Height of the grid plane (target center), within ±100 000 m. |
| `cell_size_m` | 10 | Cell size, at most 10 000 m. More than 40 000 cells coarsens it, with a warning. |
| `center_xy` / `size_xy` | `null` | Explicit extent (both or neither; center within ±10⁷ m, sides up to 10⁶ m). Default: the scene bounds (visual mesh, devices, actors) padded by min(15 m, max(3 m, 15 % of the larger side)), as for the sionna radio map. |
| `threshold_db` / `cpi_pulses` / `pfa` | 13 / 4096 / 1e-6 | Detection, as in §6 and §7. |
| `array_gain` | `none` | `steered` adds 10·log10(N_tx) + 10·log10(N_rx) of the `tx_rows`×`tx_cols` / `rx_rows`×`rx_cols` panels: every link beams at every cell, an upper bound. |
| `min_links_for_fusion` | 3 | Distinct geometries a cell needs to count as fusion-feasible. |
| `detector` | Swerling 1, no Monte Carlo | Model of `pd_best` (§9). `monte_carlo_trials` > 0 adds a Monte Carlo spot check of 5 cells. |

**Model.** Per link and cell, with R_t = |cell − TX| and R_r = |cell − RX|:

`SNR = P_t + G + G_e,t + G_e,r + L_pol + 10·log10(λ² σ / ((4π)³ R_t² R_r²)) − A(R_t + R_r) − N0 + 10·log10(cpi_pulses)`

when **both legs are line-of-sight**, and no echo otherwise. G is the array
gain above and A the atmospheric absorption (0 unless enabled in the
config). G_e,t and G_e,r are the TX's and the RX's element gain toward the
cell, from each device's `antenna.pattern` in its own frame
(`orientation_deg`): 0 dB for `iso`, 8 dBi down to −22 dBi (a 30 dB floor)
for `tr38901`. `metadata.element_patterns` lists them. L_pol is the
polarization term below (v0.1.14).

**Polarization.** Each element radiates one linear field (the first port, as
in Sionna's echo): along its local θ̂ for `V` and `VH`, along φ̂ for `H`, and
θ̂ turned by −45° for `cross`. Pitch or roll tilts that field in the world.
Sionna's RCS scattering is the identity in the world θ̂/φ̂ bases of the
incident direction k_i (TX → target) and the scattered direction k_s
(target → RX), so with p_t the TX element's world field toward the target
and p_r the RX element's field toward the target:

`F = (p_t · θ̂(k_i)) (p_r · θ̂(k_s)) + (p_t · φ̂(k_i)) (p_r · φ̂(k_s))`,  `L_pol = 20·log10|F|` (≤ 0 dB)

where θ̂(k) and φ̂(k) are the world spherical unit vectors of a direction.
What follows from it:

- Unpitched, unrolled elements with the same `V` (or `H`) polarization on
  both ends give F = ±1 exactly, so L_pol = 0 and such maps are unchanged
  from v0.1.13 (and carry no `metadata.polarization_model`).
- On a monostatic link φ̂ flips sign between the two legs, so the echo keeps
  20·log10|cos 2ψ| of a single element whose field is tilted by ψ from θ̂
  toward the cell. A down-tilted rooftop TRP loses echo power toward most
  cells.
- `cross` (±45°, first port) panels at zero tilt receive the echo
  cross-polarized, a co-located panel its own echo included: F = 0, so such a
  link has **no echo** at the cell, monostatic or bistatic, as if a leg were
  blocked (Sionna's echo there is float32 noise, about 150 dB down), and a
  mock solve has no echo path for it (`no_echo`). Use `V` (or `H`) elements
  on sensing links.

Measured on the L2 regression site (TRP at (0, 0, 10) m, 3.5 GHz, a 10 dBsm
constant target 30 m up, `iso` elements, V polarization; monostatic = RX at
the TX, bistatic = RX at (0, 40, 10) m with the same orientation), the Sionna
echo minus the v0.1.13 map over five LOS cells (four on the bistatic link):

| Link | Pitch | Echo loss (dB) |
|---|---|---|
| monostatic | 0° | 0.000 |
| monostatic | −15° | −0.35 … −1.51 |
| monostatic | +15° | −0.42 … −1.52 |
| monostatic | −45° | −3.99 … −29.03 |
| bistatic | −15° | −0.12 … −1.88 |
| bistatic | −45° | −1.20 … −18.69 |
| monostatic, `cross` | −15° | −5.32 … −6.61 |

So a rooftop TRP tilted −13…−16° loses about 0.4–1.5 dB per monostatic link,
which v0.1.13 maps and mock echoes did not show: they were optimistic by that
much, and the Sionna echo was right. With L_pol the map matches `RCSSolver`
echoes within 0.01 dB (measured ≤ 3·10⁻⁵ dB) for V, H, VH and `cross`
elements at pitch 0, ±15° and −45°, monostatic and bistatic. When any link's
loss is nonzero, `metadata.polarization_model` names the model.

**Mock and LOS.** The mock echo applies the same element gains and L_pol
(v0.1.14), so a mock sensing solve with a target at a cell center still
returns the map's value, for `iso` and `tr38901` elements in any
orientation. A constant target with `xpr_db` set gets no L_pol in the mock
(its depolarization is not modeled). On sionna the LOS test is a Mitsuba
shadow-ray test against the cached static scene with every actor mesh
taken out (the map is about the site, not about where the real drone
happens to be). In the regression tests a constant-RCS echo solved by
`RCSSolver` at a LOS cell matches the map within 0.01 dB, with `iso` and
with `tr38901` elements (measured: about 10⁻⁵ dB), and cells in a
building's shadow have no direct echo. The mock has no geometry occlusion:
every leg counts as LOS, and the result says so in `warnings` and
`metadata.los_model`.

**Layers** (`values`, row-major [ny][nx] like a radio map):

- `best_snr_db`: the best link's SNR (null where no link has an echo).
- `n_links_detected`: links with SNR ≥ `threshold_db`.
- `pd_best`: the P_d of the best link under `detector.model` (Swerling 1 by
  default, §7 and §9).
- `fusion_feasible`: 1 where the detected links span at least
  `min_links_for_fusion` **distinct geometries**, else 0. Devices within
  1 m of each other (a TRP's TX and its sensing RX, the ISAC co-location
  rule) are one site, and links between the same two sites count once, in
  either order. So a reciprocal pair (A→B, B→A) is one geometry even when
  the sensing panels sit up to 1 m from their TXs. Each link's
  `geometry_group` shows the grouping.

`summary` gives the cell, link and geometry counts, the percentage of cells
with ≥ 1 LOS link, ≥ 1 detection and fusion feasibility, and the median best
SNR over the cells with an echo. `links[]` gives each link's baseline and
its LOS and detected fractions. With `detector.monte_carlo_trials` > 0,
`summary.mc_spot_check` holds 5 distinct cells at the 0/25/50/75/100 %
quantiles of `best_snr_db` over the cells with an echo (nearest rank; a
value shared by several cells goes to the lowest cell index not yet taken;
fewer cells when fewer have an echo). Each gives `cell` [ix, iy], `snr_db`,
the analytic `pd`, `pd_mc` and its 95 % interval (`ci_low`, `ci_high`).
`metadata.detector` appears with a non-default `detector`.

## 9. Detector models and Pd/Pfa Monte Carlo

Every P_d SEAM reports models a square-law detector on the coherently
integrated sample, at the post-integration SNR (the link `snr_db` of §6,
the beam SNRs of §7, `best_snr_db` of §8). The integrated noise sample is
normalized to CN(0, 1), so noise alone gives y = |z|² ~ Exp(1), and the
threshold T = −ln P_fa gives exactly P_fa. S = 10^(SNR/10) is the mean |A|²
of the target amplitude A. A stays constant over the CPI and fluctuates
from scan to scan as `detector.model` says:

| `model` | Target | P_d | SNR for P_d 0.9 / 0.5 at P_fa 10⁻⁶ |
|---|---|---|---|
| `swerling0` | Steady | Q₁(√(2S), √(2T)) (Marcum Q) | 13.18 / 11.24 dB |
| `swerling1` (default) | Rayleigh, \|A\|² ~ Exp(S) | P_fa^(1/(1 + S)) | 21.14 / 12.77 dB |
| `swerling3` | One dominant scatterer, \|A\|² ~ Gamma(2, S/2) | e^(−T/(1 + S/2)) · (1 + T·(S/2)/(1 + S/2)²) | 17.30 / 11.95 dB |

A fluctuating target needs more SNR for a high P_d: 8 dB more for Swerling 1
than for a steady target at P_d 0.9.

**Derivation.** For a fixed |A|² = s, P(y > T) = P(X ≤ K) with K ~ Pois(s)
and X ~ Pois(T) independent (the Poisson mixture of the noncentral
chi-square). Swerling 0 sums that series in log space over the window
S ± (40√S + 40); it matches scipy's `ncx2.sf` to about 10⁻¹³ from P_fa 0.1
down to 10⁻³⁰⁰. Mixing s over Gamma(2, θ) makes K negative binomial,
P(K = k) = (k + 1) p² qᵏ with p = 1/(1 + θ) and q = θ/(1 + θ). Then
Σ_{k≥x} (k + 1) p² qᵏ = qˣ((x + 1)p + q), and averaging over X gives
e^(−Tp)(1 + Tpq): Swerling 3 with θ = S/2. Gamma(1, S) in the same steps
gives e^(−T/(1 + S)), Swerling 1. Swerling 1 is evaluated with the v0.1.12
expression, so the default numbers do not move.

**Where the model applies.** ISAC (§7): every beam's and point's `pd`, plus
`snr_for_pd_target_db` and `pd_at_threshold`. Coverage (§8): `pd_best`.
Scenario sensing (§6): with `sensing.pfa` set, the link reports' `pd`.
Detection there stays the `snr_db ≥ threshold_db` test.

**Monte Carlo.** `detector.monte_carlo_trials` > 0 adds a Monte Carlo
estimate `pd_mc` next to the analytic `pd`. Each trial draws the target
amplitude for the model (Swerling 0: √S·e^(jφ); Swerling 1: CN(0, S);
Swerling 3: √Gamma(2, S/2)·e^(jφ); φ uniform) and the integrated sample
z = A + CN(0, 1), and detects when |z|² exceeds the threshold. That is the
law of the N = `cpi_pulses` pulses A/√N + n_i summed coherently and divided
by √N (N iid CN(0, 1) summed and divided by √N is CN(0, 1)), so each trial
costs one sample whatever N is and the caps below bound the run time.
Drawing every pulse gives the same numbers at N times the cost; the
regression tests check that the two agree.

- **Seeds** are streams keyed by position, so a result does not depend on
  evaluation order: `[seed, model index, SNR index]` on the Pd curve,
  `[seed, tx index, beam index]` and `[seed, tx index, 10⁶ + point index]`
  in ISAC, `[seed, frame index, link index]` in a scenario and
  `[seed, cell index]` in coverage.
- **Threshold.** −ln P_fa, or with `empirical_threshold` the (1 − P_fa)
  quantile of a noise-only run. That needs trials ≥ 20 / P_fa (at least 20
  exceedances), so at P_fa 10⁻⁶ it is out of reach of the 2·10⁶-trial cap.
  The Pd curve, ISAC and coverage take the threshold and the measured P_fa
  from one noise-only pair per request (stream `[seed, 999, 0, 1]`): run 1
  gives the empirical threshold, and run 2, drawn independently of it,
  counts false alarms against the threshold in use. The key has four
  entries because numpy pads shorter keys with zeros (`[seed, 999]` and
  `[seed, 999, 0]` are one stream), so the nonzero last entry keeps it
  apart from every estimate's stream. A scenario link with
  `empirical_threshold` draws its own noise-only run for its threshold
  (first in the link's stream, then the signal run). Scenario runs measure
  and report no P_fa.
- **Intervals** are 95 % Wilson score intervals. In the regression tests,
  200 000 trials per point hold the closed forms at five SNRs per model
  (checked against a 99.9 % interval, so the fixed-seed tests cannot flake).
- **Caps.** At most 2·10⁶ trials per estimate and 2·10⁸ per request,
  counted as trials × estimates: Pd-curve points × models (**422**), ISAC
  TXs × beams × (1 + nonzero slot ratios) (**400**), scenario frames × TXs ×
  sensing RXs × targets (**400**; trials count twice with `empirical_threshold`,
  for each link's noise run), coverage 5.

**Pd curve.** `POST /analysis/pd-curve` plots P_d against SNR without a
solve and stores nothing. In the UI: Results ▸ **Detector (Pd curve)**.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/analysis/pd-curve \
  -H "Content-Type: application/json" \
  -d '{"pfa": 1e-6, "cpi_pulses": 4096, "monte_carlo_trials": 200000}'
```

| Field | Default | Meaning |
|---|---|---|
| `pfa` | 1e-6 | False-alarm probability. |
| `snr_min_db` / `snr_max_db` / `step_db` | −5 / 30 / 0.5 | SNR axis (71 points; at most 1001). |
| `models` | all three | Deduplicated, order kept. |
| `monte_carlo_trials` | 0 | Trials per point (0 = analytic only). |
| `empirical_threshold` | `false` | Threshold from a noise-only run (needs trials ≥ 20 / P_fa). |
| `seed` | 0 | Base of the Monte Carlo streams. |
| `cpi_pulses` | 1 | Pulses per trial. The SNR axis is post-integration and the Monte Carlo draws the integrated sample, so no number depends on it. |
| `pd_target` | 0.9 | Gives `snr_for_pd_target_db` per model. |

The response holds `snr_db` and `threshold` (−ln P_fa). Per model it gives
`pd` and `snr_for_pd_target_db`, and with trials > 0 also `pd_mc` with
`pd_mc_ci_low` / `pd_mc_ci_high`, `mc_max_abs_deviation` and
`mc_within_ci_fraction`. The top level then adds `empirical_threshold`,
`pfa_measured` and `pfa_measured_ci`, and `metadata.mc_method` says which
draw ran. An unknown project answers **404** and an invalid request,
including one over the trial budget, **422**.

### Signal-level detection: delay-Doppler map and CFAR (#24)

The §6 detection is a threshold on the true SNR of the strongest echo. It
never misses a strong echo and never raises a false alarm. With
`sensing.detection.mode: "cfar"` each frame instead builds the received
pilot channel of every TX → sensing-RX link, turns it into a delay-Doppler
map and runs a 2-D CFAR detector on it. A detection is now a cell that
crosses a threshold. Thermal noise, clutter and target sidelobes can cross
it too, and those false alarms are counted. CFAR mode needs
`sensing.waveform` (*Waveform numerology (#23)* in §6), because the map is
built on its pilot grid.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/scenario \
  -H "Content-Type: application/json" \
  -d '{"num_frames": 20, "dt_s": 0.5, "sensing": {"enabled": true,
       "waveform": {"scs_hz": 15625, "n_fft": 2048, "n_active_sc": 1280,
                    "symbol_duration_s": 6.944444444444444e-05,
                    "pilot": {"subcarrier_step": 5, "symbol_step": 9}},
       "background": {"mode": "both"},
       "detection": {"mode": "cfar",
                     "map": {"window": "hann", "clutter_removal": "mean_subtract"},
                     "cfar": {"type": "ca", "guard": [1, 1], "train": [8, 4],
                              "pfa_cell": 1e-4}}}}'
```

**Signal model.** Per frame i and link (tx, rx), on the K × N pilot lattice
(subcarrier f_k, pilot symbol time t_n):

    Y[n, k] = Σ_p a_p e^(−j2π f_k τ_p) e^(+j2π f_D,p t_n) + W[n, k]

The sum runs over the frame's echoes of every target on the link (direct and
multipath) and the background paths: H1 when the run solved it, else H0,
else none (§6, *Background paths H0/H1*). a_p = 10^(path_gain_db/20) ·
e^(j·phase_rad) is the dimensionless path gain. `phase_rad` already holds the
carrier term −2π f_c τ, so f_k is the baseband subcarrier frequency and no
carrier term is added again. W is circular Gaussian with variance

    σ_H² = N0 / Es = 10^((N0,RE − Es,RE)/10),  N0,RE = −174 + 10 log10(scs) + NF,  Es,RE = P_tx − 10 log10(n_active_sc)

per RE, the noise of a least-squares pilot estimate. σ_H² equals
N_floor(B_occ) / P_tx, so a path's per-RE SNR |a|²/σ_H² is P_echo −
N_floor(B_occ). The noise of (frame i, tx, rx) is drawn from
`SeedSequence(entropy=[noise_seed, 0x5EA024], spawn_key=(0, i, id32(tx), id32(rx)))`
(PCG64; real parts first, then imaginary parts; id32 = the first 4 bytes of
sha256, little-endian). Two runs with the same `noise_seed` therefore share
the noise on equal (frame, tx, rx) keys, and the channel export (§10) redraws
it bit for bit. H0 calibration maps (below) draw from a stream of their own,
0x5EA031, so a calibration never reuses the noise a run is tested on.

**Map.** `clutter_removal: "mean_subtract"` first removes the slow-time mean
of each subcarrier, which cancels every static path exactly. Then the window
(periodic Hann w[i] = 0.5 − 0.5 cos(2πi/L), or rect) is applied on both axes,
followed by a K' = z·K point IFFT over subcarriers (delay) and an N' = z·N
point FFT over symbols (Doppler), z = `zero_pad`. The Doppler axis is
shifted so 0 Hz is row N'/2:

    Z[v, u] = Σ_n Σ_k w_t[n] w_f[k] Y[n, k] e^(+j2π k u/K') e^(−j2π n v/N')
    P[v, u] = |Z|² / (σ_H² Σw_f² Σw_t²)

A noise cell has mean 1, so P is an SNR. A path at (τ, f_D) peaks at
u = τ K' Δf_p and v = f_D N' T_p (Δf_p = pilot subcarrier spacing, T_p =
pilot period), at range sum c·u/(K'Δf_p) and Doppler v/(N'T_p). On a bin
centre the peak is 10 log10 P = SNR_RE + 10 log10(K N) + L_t + L_f, with
window loss L = 10 log10((Σw)²/(L Σw²)): 0 for rect and −1.76 dB for Hann,
per axis. Off a bin centre the straddle loss can reach about 3.9 dB per axis
without padding, and `zero_pad` 4–8 recovers most of it.
`zero_doppler_notch_bins: k` excludes the coarse Doppler bins −k…k (rows
|v| < (k + 0.5)·z): they are neither cells under test nor training cells.
`mean_subtract` leaves 0 Hz without noise, so without a notch it implies
k = 0, and every row whose noise variance it lowers by more than 1 % does
not train either: 1 − |W_t(v)|² / (N Σw_t²) < 0.99, W_t the N'-point DFT of
the slow-time window. That is the 0 Hz row for rect at z = 1, the rows
−1…1 for Hann (±1 keep 5/6 of their noise). `metadata.sensing.detection.map`
lists `notch_bins`, `notched_rows` and `non_training_rows` (signed v).

**CFAR.** Each cell under test has training cells inside a box of
±(guard + train) cells around it, minus the guard box. `guard` and `train`
are [delay, Doppler] half-widths, and the box wraps around both axes. The
defaults are guard [1, 1] and train [8, 4], N_t = 17·9 − 3·3 = 200. A cell is
detected when its statistic s exceeds the threshold (strictly) and it is not
notched:

| `type` | Statistic s | Analytic threshold α at `pfa_cell` |
|---|---|---|
| `ca` | P / mean(training) | α = N_t (P_fa^(−1/N_t) − 1), so P_fa = (1 + α/N_t)^(−N_t) |
| `os` | P / x₍k₎, the k-th smallest training cell (`os_rank`, default ⌈0.75 N_t⌉) | α solves Π_{i<k} (N_t − i)/(N_t − i + α) = P_fa |

A cell under test whose training box holds only n_v < N_t cells of training
rows (next to the notch) uses their mean (CA) or their k_v-th smallest, k_v
= ⌈k·n_v/N_t⌉ (OS), and its statistic is mapped onto the N_t scale so one
threshold keeps P_fa per cell everywhere: CA s' = N_t((1 + s/n_v)^(n_v/N_t)
− 1), exact for every threshold; OS s' = s·α(N_t, k)/α(n_v, k_v), exact at
`pfa_cell`. (When the emptied 0 Hz row still trained, rect with
`mean_subtract` false-alarmed at about 1.9 × `pfa_cell` in the rows
|v| = 1…5.)

These P_fa values assume iid exponential cells (rect window, no padding).
Hann and zero padding correlate neighbouring cells, so the achieved rate is
higher than `pfa_cell`: 1.9·10⁻³ against 10⁻³ at N_t = 40 with a periodic
Hann map. The report below shows the rate actually achieved.
`threshold: "h0_calibrated"` replaces α with an empirical one, computed on
the H0 maps of a stored result (`h0_result_id`): a `sensing_background_ensemble`
(§12; every realization × noise frame × link) or a scenario result with
stored H0 background frames (every frame; frames sharing one cached H0 list
are synthesized once). These maps use this run's waveform, map, CFAR and
σ_H², and their noise is drawn from
`SeedSequence(entropy=[seed, 0x5EA031], spawn_key=(r, m, id32(tx), id32(rx)))`
with the ensemble's `seed` and realization r, or the scenario source's
`noise_seed` and r = 0: never the 0x5EA024 stream of in-run noise, so the
achieved P_FA below stays a held-out check even when the seeds are equal.
With n statistics of non-notched cells and m = ⌊n·p⌋, the threshold is
`sorted(s)[n − m]` (0-based). m must be at least 20, and at most 5·10⁸ cells
are evaluated. With `system_pfa` the statistic is the per-frame maximum over
every link and every cell, and its quantile at `system_pfa` sets the
threshold; the H0 source must then cover every TX → sensing-RX link of this
run (400 otherwise), because a maximum over fewer links sets the threshold
too low (about #links / #calibrated links × `system_pfa`). For a cell
threshold, links without H0 maps use the threshold calibrated on the others
with a warning (`source.uncalibrated_links`).

**Output.** `SensingFrame.detections` lists every link's detections in link
order (tx-major), strongest statistic first. Each entry holds `tx_id`, `rx_id`,
`cell_tau` (u), `cell_nu` (signed v), `delay_ns`, `range_sum_m`, `doppler_hz`
(parabolic interpolation of 10 log10 P across the neighbouring cells per
axis, clipped to ±0.5 cell), `statistic`, `threshold`, `power_db`
(10 log10 P) and `snr_est_db` (10 log10(P/noise − 1) with the unbiased noise
mean: CA the training mean, OS x₍k₎ / Σ_{i=N_t−k+1}^{N_t} 1/i). Detections
carry no truth. Association is still an oracle. A link report is `detected`
when a detection lies within G = `oracle_gate_cells` coarse cells
(× `zero_pad`) of the map cell nearest its echo's true (range sum, Doppler),
circularly: 2G + 1 cells per axis, and `oracle_gate_cells: 0` is that cell
alone. The strongest such
detection supplies `measured_range_m`, `measured_doppler_hz`, `range_bin`,
`doppler_bin` and `detection_index` (its index in `detections`), and fusion
and the EKF use those values. With no detection in the gate, the reason is
`cfar_miss` and the measured fields are null. `snr_db`, `bistatic_range_m`,
`doppler_hz` and `path_id` keep the oracle values, and the MTI notch of §6
does not apply. `measurement_noise` is ignored, with a warning, because the
map already adds the noise. At most `max_detections_per_link` (256) are
stored per link, strongest first, and a gated detection is always kept.

**Achieved P_FA.** `metadata.sensing.detection` holds the map, CFAR and
threshold settings (with α, or the calibration source with n, m and the
quantile rule), the noise seed, N0 per RE and σ_H² per TX, and `achieved`:

- `cell`: false-alarm detections over the tested cells (non-notched cells
  outside every gate);
- `link_frame`: maps with at least one false alarm;
- `system`: frames with at least one false alarm on any link.

Each comes with a 95 % Clopper–Pearson interval. A false alarm is any
detection outside every gate of its link: thermal noise, clutter residue,
multipath ghosts and target sidelobes beyond the gate all count.
`link_frame` and `system` count one Bernoulli trial per map and per frame,
so theirs are exact binomial intervals. Cells are not independent: Hann
and zero padding correlate neighbours and false alarms cluster, so the
`cell` interval is the Clopper–Pearson interval of the effective sample
k/φ of n/φ, where φ = max(1, χ²/(M − 1)) is the Pearson dispersion of the
M per-map false-alarm counts (`cell.dispersion`; measured on noise: 1.3 for
Hann, 3 for Hann with `zero_pad` 2, about 1 for rect without padding). The
Pd Monte Carlo of §9 keeps its Wilson intervals.

A strong static path masks a weak echo near 0 Hz. In free space with a
300 m bistatic baseline, 43 dBm and a 0.052 m² target (G1 geometry), the
direct path is 68 dB above the echo. With Grid S at 10 ms, Hann and no
clutter removal, the echo (12 dB integrated SNR at 22 Hz) was a `cfar_miss`
in every frame, and the direct path's sidelobes raised the achieved cell
P_FA to 1.3·10⁻³ against a 10⁻⁴ design (95 % interval 0.7–2.2·10⁻³,
dispersion 3.9). With a 50 ms CPI, a rect window and
`clutter_removal: "mean_subtract"` (notch 0 implied), the bistatic and the
monostatic echo were detected in every frame, at an achieved cell P_FA of
6.9·10⁻⁵ (95 % interval 3.1–13.3·10⁻⁵). On the `isac_drone_fr1` demo
(NR 612 × 280, H0 background), mean subtraction raised the gated
detections from 36 to 127 of 128 link-frames. The scenario form's
"Clutter removal" select defaults to mean subtraction when a background is
on.

| Field | Default | Meaning |
|---|---|---|
| `detection.mode` | `"threshold"` | `"cfar"` turns on the detector (needs `waveform`). |
| `map.window`, `map.zero_pad` | `"hann"`, 1 | Window on both axes; FFT length factor (1–8). |
| `map.clutter_removal` | `"none"` | `"mean_subtract"` removes the slow-time mean per subcarrier (implies notch 0; emptied rows do not train). |
| `map.zero_doppler_notch_bins` | `null` | Coarse Doppler bins −k…k neither tested nor training. |
| `cfar.type`, `cfar.guard`, `cfar.train` | `"ca"`, [1, 1], [8, 4] | Detector and [delay, Doppler] half-widths. |
| `cfar.os_rank`, `cfar.pfa_cell` | ⌈0.75 N_t⌉, 1e-4 | OS rank; design P_fa per cell. |
| `threshold`, `h0_result_id`, `system_pfa` | `"analytic"`, null, null | Calibrated threshold source and per-frame target. |
| `oracle_gate_cells`, `max_detections_per_link` | 1, 256 | Association gate around the nearest cell; stored detections per link. |

These answer 400 before any solve: a CFAR window wider than the map (raise
`zero_pad` or shrink `train`), a map of more than 2²⁴ = 16,777,216 cells
(z²·K·N), a Hann window on an axis with one pilot, a notch that covers
every Doppler row, and a tested row left without a training cell. So does
an `h0_result_id` that is unknown, of another kind, without stored H0
frames, without a link of this run, missing a link of this run with
`system_pfa`, or with fewer than 20 expected exceedances. CFAR without
`waveform` answers 422.

## 10. Sensing dataset export

`POST /export/sensing-dataset` flattens the per-frame sensing of stored
scenario results (§6) into tables for learning or offline analysis, in one
zip under `export/sensing_dataset/`. It runs no solve. In the UI: Toolbar ▸
Actions ▸ **Sensing dataset (.npz)**.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/export/sensing-dataset \
  -H "Content-Type: application/json" \
  -d '{"formats": ["npz", "csv"], "include_echo_paths": true,
       "split": {"train": 0.8, "val": 0.1, "test": 0.1, "seed": 0}}'
```

| Field | Default | Meaning |
|---|---|---|
| `result_ids` | `null` | Scenario results to export, in this order. `null` = every stored scenario result that has sensing frames. |
| `skip_without_sensing` | `false` | With `result_ids`: skip a listed result without sensing frames (named in `warnings`) instead of answering 400. The UI sends it for a partial selection. |
| `formats` | `["npz"]` | Any of `npz`, `csv`, `parquet` (parquet needs pyarrow: `pip install "seam-studio[parquet]"`, the `parquet` extra). |
| `include_echo_paths` | `false` | Also write the `echoes` table. |
| `split` | `null` | Frame-level split `{train, val, test, seed}` (fractions sum to 1). `null` = every row is `all`. |

The response gives `zip_name` and `download_url` (the project assets route),
the zip's `files`, the row counts (`num_rows`, `rows_per_result`,
`rows_per_split`, `num_echo_rows`), `detected_fraction`, `size_bytes` and
`warnings`. The zip holds:

- `links.<fmt>`: one row per result × frame × TX → sensing RX link × target,
  in the frame's `links[]` order (frames × TX × sensing RX × targets rows per
  result).
- `echoes.<fmt>` (`include_echo_paths`): one row per echo path of each frame.
- `manifest.json`: every column's dtype, unit, role and description; per
  result its label, backend, frequency, frames, dt, rows, `scene_hash` (and
  the current one), `kinematics_source` and the run constants of
  `metadata.sensing` (without the per-target statistics); the split, rows per
  split, the label balance (`detected_fraction`, `reason_counts`) and the
  conventions.
- `README.txt`: what each file is, how to load it, and the caveats.

Every column is always there, so the schema is stable. A missing value is
NaN in a float column and `""` in a string column. **npz** has one 1-D array
per column (strings as `<U`, so `np.load(..., allow_pickle=False)` works).
**csv** has a header, floats in shortest round-trip form, NaN as an empty
field and `true`/`false`. **parquet** keeps NaN as NaN.

**links columns**

| Column | dtype | Unit | Role | Source |
|---|---|---|---|---|
| `result_id`, `split` | str | | meta | |
| `frame_index` | int64 | | meta | index in `frames` |
| `time_s` | float64 | s | meta | `frame.time_s` |
| `tx_id`, `rx_id`, `target_id` | str | | meta | link report |
| `tx_pos_x/y/z`, `rx_pos_x/y/z` | float64 | m | feature | `frame.sensing.nodes` |
| `tx_vel_x/y/z`, `rx_vel_x/y/z` | float64 | m/s | feature | `frame.sensing.nodes` |
| `bistatic_range_m`, `doppler_hz`, `echo_power_dbm`, `snr_db`, `measured_range_m`, `measured_doppler_hz` | float64 | m, Hz, dBm, dB | feature | link report |
| `range_bin`, `doppler_bin` | float64 (NaN = none) | cell | feature | link report |
| `num_echoes` | int64 | | feature | link report |
| `multipath` | bool | | feature | link report |
| `aod_az_deg`, `aod_el_deg`, `aoa_az_deg`, `aoa_el_deg` | float64 | deg | feature | the reported echo (`path_id` in `frame.sensing.echoes`) |
| `pd`, `pd_mc` | float64 | | feature | link report (NaN unless the run set `pfa`) |
| `detected` | bool | | label | |
| `reason` | str | | label | `detected` / `no_echo` / `below_threshold` / `mti_rejected` / `cfar_miss` (CFAR runs, see §9) |
| `position_true_x/y/z`, `velocity_true_x/y/z` | float64 | m, m/s | label | the target's `estimates[]` row |
| `est_status` | str | | estimate | `ok` / `insufficient_links` / `diverged` |
| `n_links_detected`, `n_links_used` | int64 | | estimate | |
| `position_est_x/y/z`, `velocity_est_x/y/z`, `position_error_m`, `velocity_error_m_s`, `gdop` | float64 | m, m/s | estimate | |
| `track_status` | str | | track | `""` when tracking was off |
| `track_position_x/y/z`, `track_velocity_x/y/z`, `track_position_error_m`, `track_velocity_error_m_s`, `track_position_std_m`, `track_updates`, `track_gated` | float64 | m, m/s | track | NaN when absent |

The estimate and track columns hold one value per target and frame, repeated
on each of its link rows. The `velocity_true_*` labels follow §6: at an exact
waypoint time they hold the outgoing leg's velocity, and 0 at the end of a
`once` trajectory. Results from before v0.1.14 hold the two-leg average (or
half the speed) at those frames, so a dataset that mixes old and new results
mixes both conventions there.

**echoes columns**: `result_id`, `split`, `frame_index`, `time_s`, `path_id`,
`tx_id`, `rx_id`, `target_id` (`""` = a comm path from
`include_comm_paths`), `path_type`, `delay_ns`, `bistatic_range_m` (c·τ),
`power_dbm`, `path_gain_db`, `phase_rad`, `doppler_hz`, `aod_az_deg`,
`aod_el_deg`, `aoa_az_deg`, `aoa_el_deg`, `num_interactions` (int64),
`direct` (bool: interactions == [sensing]) and `reported` (bool: the echo its
link report was built from).

**Split.** The unit is the frame: the (result, frame) pairs, in export order,
are permuted with `numpy.random.default_rng(seed)`. The first n_train are
`train`, the next n_val `val` and the rest `test`. The counts are train·F,
val·F and test·F rounded by largest remainder (a tie goes to the smaller
fraction, then train, val, test), so they add up to F, each is within one
frame of its exact share, and a fraction of 0 never gets a frame. Every row
of one frame, links and echoes alike, lands in the same split, and the same
seed gives the same split.

**Older results.** Results from before v0.1.13 did not store `nodes`. Their
TX/RX positions come from the frame's `device_states`, else the current
scene, and their velocities from the current scene's trajectories (the way
the run computed them, except at exact waypoint times, where the v0.1.14 rule
of §6 applies). The export then warns "predates v0.1.13" (adding
"scene changed since the run" when the scene hash differs) and the manifest
says `"kinematics_source": "current_scene"`.

**Errors.** Unknown project, or a `result_ids` entry that is unknown or whose
file is missing or unreadable (also: not a valid scenario result): **404**.
With `result_ids` null such a file is skipped and named in `warnings`. A
listed result without sensing frames (unless `skip_without_sensing`), no
such result left at all, parquet without pyarrow, or more than 500 000 rows
(links + echoes): **400**, and nothing is written. An unknown format:
**422**.

**Memory.** Rows are built in memory: about 1.9 KB per row as Python lists
plus about 0.6 KB per row of arrays and the encoded files, so an export at
the 500 000-row cap peaks around 1.3 GB of RAM; export fewer results on a
small machine.

### Channel tensors (include_channel, #22)

Two opt-in flags add what a signal-level detector needs to the export. The
export synthesizes the tensors from the stored path rows, so it still runs no
solve.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/export/sensing-dataset \
  -H "Content-Type: application/json" \
  -d '{"include_echo_paths": true, "include_background_paths": true,
       "include_channel": {"subcarriers": "pilot",
                           "components": ["echo", "background_h0", "total"],
                           "noise": {"enabled": true}}}'
```

| Field | Default | Meaning |
|---|---|---|
| `include_background_paths` | `false` | Also write `background.<fmt>`: one row per stored H0 / H1 background path (§6, *Background paths H0/H1*). |
| `include_channel` | `null` | Also write `channel.npz` (options below). |
| `include_channel.subcarriers` | `"pilot"` | `pilot`: the K × N pilot lattice. `active`: every active subcarrier of every symbol of the CPI. |
| `include_channel.waveform` | `null` | Grid of the tensors. `null`: each run's `sensing.waveform`, else Grid S (15.625 kHz × 1280 SC, comb-5 pilots on every 9th 1/14400 s symbol: K 256 × N 16 per 10 ms). |
| `include_channel.components` | `["echo"]` | Any of `echo`, `background_h0`, `background_h1`, `total` (deduplicated). |
| `include_channel.noise.enabled` | `false` | Also write the thermal noise realization `noise`. |
| `include_channel.noise.n0_dbm_per_re` | `null` | N0 per RE [dBm]. `null`: −174 + 10 log10(scs) + NF of the run's config. |
| `include_channel.noise.seed` | `null` | `null`: each run's `sensing.noise_seed`. |
| `include_channel.pilot_ls` | `false` | Also write `H_ls = H_total + noise`, the LS pilot estimate (needs noise). |
| `include_channel.max_elements` | 50,000,000 | Cap on every complex64 element of `channel.npz` together (at most 2·10⁸). |

With either flag on, `echoes` rows also get `a_re` and `a_im` (Re and Im of
the dimensionless gain 10^(path_gain_db/20)·e^(j·phase_rad), carrier phase
included), `tx_power_dbm`, and per interaction (`;`-joined; `""` for LoS or a
missing value) `interaction_types`, `rf_material_ids` and `prim_ids` (the
target actor id on a sensing interaction). The manifest then says
`schema_version: 2`, and the response counts `num_background_rows`,
`num_channel_rows` and `channel_elements`. Without either flag every table
and manifest key is the v0.1.15 one (apart from `created_at` and
`seam_version`).

**background.\<fmt\>** has the columns `result_id`, `split`, `frame_index`,
`time_s`, `hypothesis` (`h0` / `h1`), `path_id`, `tx_id`, `rx_id`,
`path_type`, `delay_ns`, `bistatic_range_m`, `power_dbm`, `path_gain_db`,
`phase_rad`, `doppler_hz`, the departure and arrival angles,
`num_interactions` and the six channel columns above. It has rows only for
frames that solved and stored a hypothesis. A cache-hit frame reuses the
paths of an earlier frame, and the manifest's `background_index` maps each
frame to `[frame_index, h0 source frame, h1 source frame]` (null = not
solved, not stored, or failed). With the default `sensing.background.store:
"h0"` a run has no H1 rows. The table has its own cap of 500 000 rows.

**channel.npz** (stored uncompressed inside the zip) holds one row per
result × frame with sensing × TX → sensing-RX link (TX-major, the run's
`sensing_tx_ids` × `sensing_rx_ids`):

| Array | Shape, dtype | Meaning |
|---|---|---|
| `row`, `frame_index` | [R] int64 | Row number, frame index. |
| `result_id`, `tx_id`, `rx_id` | [R] str | |
| `time_s`, `sigma_h2`, `es_dbm_per_re` | [R] float64 | Frame time, σ_H² = N0/Es of the row's TX, Es per RE [dBm]. |
| `f_hz`, `t_s` | [K], [N] float64 | Baseband subcarrier frequencies, symbol times. |
| `H_echo`, `H_background_h0`, `H_background_h1`, `H_total` | [R, N, K] complex64 | Requested components, noiseless. |
| `noise`, `H_ls` | [R, N, K] complex64 | With `noise.enabled` / `pilot_ls`. |

    H[n, k] = Σ_p a_p e^(−j2π f_k τ_p) e^(+j2π f_D,p t_n)

f_k is the baseband frequency of subcarrier k. Because `phase_rad` already
holds −2π f_c τ, a lone LoS path has phase −2π(f_c + f_k)τ, with the carrier
applied once. No delay normalization is applied, and the Doppler sign is
positive for a closing path. `echo` sums the link's target echoes (not the
comm paths of `include_comm_paths`). `total` = echo + H1 when the frame has
it, else + H0, the same rule the CFAR map uses. A missing background is
zeros, with a warning. A run that solved H1 without storing it (the default
`sensing.background.store: "h0"`) cannot rebuild `background_h1`, `total`
or `pilot_ls` (whose total would differ from the run's map): those answer
400; run with `store: "both"`, or export `echo` / `background_h0`. The same
holds for H0 with `store: "none"`. The noise of a row is drawn with the key of the CFAR
section above, at the row's σ_H². With the run's own waveform on the pilot
grid, the exported noise is therefore the noise the run's map used, bit for
bit. The manifest's `channel` block holds the grid (`waveform`,
`numerology`), the components, the noise seeds and N0 per result, the noise
key, the amplitude convention and the synthesis formula. The export warns
when two exported results share a noise seed.

```python
import numpy as np
ch = np.load("channel.npz", allow_pickle=False)
H = ch["H_total"][0].astype(np.complex128)      # [symbol, subcarrier]
Y = H + ch["noise"][0]                           # the LS pilot estimate
snr_re = np.abs(H) ** 2 / ch["sigma_h2"][0]      # per-RE SNR of the noiseless channel
```

All results of one export must resolve to the same grid (same frequencies
and symbol times), else 400: export them separately or set
`include_channel.waveform`. More than `max_elements` elements (rows × N × K ×
arrays) answers 400 before anything is written. A TX whose power is unknown
(no stored path gain and no longer in the scene) is left out with a warning.
The tensors are built in memory, at 8 bytes per element (400 MB at the
default cap).

## 11. Limits

- Targets move by rigid translation only: no rotation, no micro-Doppler.
- The cuboid is the actor's box size; mesh actors need an explicit `size_m`.
- The actor's own mesh is removed from the scene during its sensing solve (its
  scattering model describes it completely) and restored afterwards.
- No diffuse-scattering or diffraction legs; sensing (echoes and
  `include_comm_paths` alike) always runs on the builtin sionna-rt engine.
- The mock uses one LoS scattering point at the target center with σ_M — no
  angular lobes, no multi-point layouts, no occlusion. Like Sionna, it has no
  LoS comm path between a co-located (monostatic) TX and RX. Since v0.1.14
  its echoes include each device's element gain and the polarization term of
  §8 (before, they were isotropic).
- Mixed antennas: Sionna applies the first selected TX's (RX's) antenna to
  every TX (RX) and warns when they differ; the mock applies each device's
  own element pattern and orientation (and the polarization term of §8). With
  mixed antennas the two backends therefore differ.
- Random components are off by default. The solver runs with
  `deterministic=False` in float32 on the GPU: identical inputs can differ by
  about 1e-6 dB on strong paths and up to about 0.1 dB near beam nulls, and
  path order and `path_id` can change between runs, so compare results by
  geometry or path type rather than by `path_id` or exact dB. The same
  `noise_seed` reproduces the measurement noise exactly. The background
  solve (§6) uses `PathSolver(deterministic=True)` by default
  (`background.deterministic`); the RCS echo solver stays non-deterministic.
- Sensing over time (§6) uses **oracle association**: the ray tracer labels
  each echo with its target and as direct or multipath. A real receiver would
  have to infer both.
- Multi-scattering-point targets (`vehicle-multi-sp`, `agv-multi-sp`): each
  link reports its strongest direct echo, which may come from a different
  scattering point on each link, while `position_true` is the cuboid center.
  Expect a bias of up to half the target size (about 2.5 m for a car) in
  `position_error_m`. The mock uses one point at the center, so it has none.
- Threshold detection (the default `sensing.detection.mode`) is per echo and
  noise-limited: no CFAR, no range–Doppler map, no clutter or
  self-interference power. `mode: "cfar"` (§9) builds the delay-Doppler map
  with H0/H1 clutter and thermal noise and runs CFAR; self-interference and
  a direct-path model (#25) are still missing, and association stays an
  oracle.
- Background (§6): in a scene whose only shape is an actor, the Sionna
  loader cannot address that actor, so H0 cannot hide it: it stays in H0
  with a warning. Add any other shape (a 0.1 m cube far away is enough). A
  sensing RX at exactly the TX position has no TX → RX LoS background path
  (the zero-length path is dropped, so `total_power_db` is null on that
  link); self-interference is not modelled (#25).
- Sensing over time (§6) counts antennas as single elements (no array
  gain), so integration gain is its only processing gain.
- `measurement_noise` is a rule of thumb (cell / sqrt(2·SNR)), not a
  Cramér–Rao bound.
- Without `tracking` each frame is an independent snapshot: the previous
  estimate only seeds the next frame's solver.
- Tracking (§6) is one constant-velocity EKF per target with oracle
  association. There is no manoeuvre model or IMM (a turn is handled by
  restarting from the fusion, so a frame or two after a sharp corner is the
  fusion's accuracy), no false tracks and no track-to-track fusion, and the
  measurement sigmas are the `measurement_noise` rule of thumb. With only one or two
  links in a frame the range sums leave the position free along the
  ellipsoids, so long stretches without three links drift. On such a
  stretch `track_position_std_m` can understate the error several-fold, so
  the `max_position_std_m` cap
  drops the track later than its true error would warrant, and
  `coast_max_frames` never does while one scalar per frame is still
  accepted.
- Sensing dataset (§10): labels come from the ray tracer (oracle
  association). The split is by frame, so neighbouring frames of one run are
  correlated across splits: hold out whole results for a strict test.
- ISAC trade-off (§7):
  - Without an elevation sweep the codebook is azimuth-only: the vertical
    rows stay broadside. A 4-row λ/2 panel has a vertical null 30° off its
    plane, and is at least 14 dB per end below broadside anywhere from 25°
    to 35° (21–23 dB at 28° and 33°). An up-tilted rooftop TRP can therefore
    lose more than its array gain toward street UEs or a steep drone, so a
    4×4 beam can come out below the single-element anchor. The `elevation_*`
    sweep steers the rows too; it is still a fixed grid, so a path between
    two grid elevations loses up to the grid's scalloping.
  - With the default `iso` element the panel has a mirror back lobe: beam θ
    also points at 180° − θ behind the panel at full gain, so UEs and
    targets behind a TRP are not clipped. Use the `tr38901` element for a
    front-to-back ratio. With λ/2 spacing, a target outside the sweep near
    endfire (say 78° off broadside with a ±60° sweep) is seen by both edge
    beams within about 1 dB of each other, through the grating-lobe skirt,
    so which edge wins is nearly arbitrary.
  - The **ISAC lobes** overlay draws each beam's azimuth cut over the
    panel's front hemisphere only (with an elevation sweep, the slice of the
    comm or sensing beam's elevation). With `iso` (or another symmetric)
    element a TX can serve a UE behind its panel through the back lobe
    while its cyan comm lobe points away from that UE.
  - Without `interference`, SINR = SNR. With it, every TX is full-buffer
    and always radiates its comm or sensing beam in synchronized slots,
    interference reaches only the UEs (the sensing receivers stay
    noise-limited: no cross-TX echo or direct-path leakage), and the comm
    beams are a best-response equilibrium, not a joint optimum. Each UE's
    rate assumes it has the full band.
  - P_d is a closed form (Swerling 0, 1 or 3; §9) with a fixed threshold:
    no CFAR, no range or Doppler straddle loss, and no pulse-to-pulse
    fluctuation (Swerling 2 and 4).
  - One t = 0 snapshot. Only the first polarization port of a
    dual-polarized antenna is synthesized, and UEs are single elements.
- Sensing coverage (§8):
  - The target is a constant point RCS: no TR 38.901 angular lobes.
  - Only direct LOS legs count (no multipath echoes), and actor meshes are
    ignored by the LOS test.
  - `steered` is the ideal full-array gain on every link and cell.
  - Element gain uses each device's own antenna. Sionna solves apply the
    first selected TX's (RX's) antenna to every TX (RX) and warn, so with
    mixed patterns the two differ.
  - Polarization: the first port of each array only (as Sionna's echo); a
    TR 38.901 target on a bistatic link deviates from the projection by up
    to 0.2 dB at 15° tilt and 2.5 dB at 45°; targets with `xpr_db`
    (depolarizing) are not modeled on the map or the mock.
  - The mock treats every leg as line-of-sight.
- Detector models (§9): the Monte Carlo draws the same idealized model the
  closed forms describe (constant amplitude over the CPI, white Gaussian
  noise, ideal coherent integration). It checks the arithmetic and shows
  the sampling spread; it is not a waveform-level simulation.

## 12. H0 calibration ensemble (#30)

`POST /simulate/sensing-background-ensemble` builds the target-free (H0)
frames that a CFAR threshold is calibrated on and that digital-twin mismatch
studies compare against. It re-solves the H0 background of one stored
scenario frame (§6, *Background paths H0/H1*: the target actors removed,
PathSolver with diffuse reflection) at that frame's node poses, once per
realization. Realization 0 is the nominal twin. Every later realization draws
a perturbation from `seed`: a scale on each radio material's ε_r and σ, one
position offset per node site, and a carrier-frequency offset (CFO) per
bistatic sensing receiver. The result stores each realization's paths and
the seed. Noise frames are never stored; the seed redraws them bit for bit.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/sensing-background-ensemble \
  -H "Content-Type: application/json" \
  -d '{"source": {"result_id": "sionna_scenario_003", "frame_index": 0},
       "n_realizations": 20, "noise_frames_per_realization": 500, "seed": 1,
       "perturb": {"material": {"eps_r_pct": 10, "sigma_pct": 30},
                   "node_pos_sigma_m": 0.1, "cfo_hz": 5}}'
```

| Field | Default | Meaning |
|---|---|---|
| `source.result_id`, `source.frame_index` | required | The scenario result and frame whose node kinematics, actor poses and targets the ensemble uses. The frame must have sensing. |
| `n_realizations` | required (1–200) | Realization 0 is nominal. |
| `noise_frames_per_realization` | required (1–100,000) | H0 noise frames m = 0 … n−1 per realization, drawn on demand. |
| `seed` | `0` | Seeds the perturbation draws and the noise. |
| `perturb.material.eps_r_pct`, `perturb.material.sigma_pct` | `0`, `0` | Standard deviation [%] of each radio material's scale: ε_r × (1 + d) and σ × (1 + d′), d, d′ ~ N(0, (pct/100)²). The solver clips ε_r at 1 and σ at 0. |
| `perturb.material.scope` | `"material"` | One draw per solver radio material. `"prim"` answers 400: material groups are merged meshes. |
| `perturb.node_pos_sigma_m` | `0` | Per node site, an N(0, σ² I₃) offset [m]. A TX and a sensing RX within 1 m of each other are one site and move together. |
| `perturb.cfo_hz` | `0` | Per bistatic sensing RX (more than 1 m from every TX), N(0, cfo²) [Hz], applied at synthesis as a Doppler offset of every path (positive = closing). A co-located receiver shares its TX's oscillator and gets none. |
| `background` | `null` | Background solver options (§6). `null`: the source run's `sensing.background` with mode `h0_removed`, else the defaults. When set, `mode` must be `"h0_removed"` (422 otherwise). |

**What is solved.** The links are every TX × sensing RX of the source run,
TX-major (`metadata.sensing.sensing_tx_ids`, else the frame's nodes that are
not sensing receivers). The nodes sit at the source frame's positions and
velocities (`frame.sensing.nodes`) plus their offsets, the actors at the
frame's `actor_states`, and the targets are resolved at that pose and
removed. The ensemble uses the source run's backend and its simulation config
(`config_snapshot`), and solves the realizations one after another. A
realization with no material or position draw (nominal, or CFO only) has
realization 0's solve inputs: it reuses those paths (`background_h0: null`,
`source_realization: 0`) and is not solved again.

**Draws.** Realization r ≥ 1 draws from
`numpy.random.default_rng(SeedSequence(entropy=[seed, 0x5EA030], spawn_key=(r,)))`,
in this order:

1. per radio material (sorted by name): `normal(0, eps_r_pct/100)`, then
   `normal(0, sigma_pct/100)`;
2. per node site (TX first, then the sensing receivers, in site order):
   `normal(0, node_pos_sigma_m, 3)`;
3. per bistatic sensing RX (sorted ids): `normal(0, cfo_hz)`.

Every draw is taken even at σ = 0, so switching one perturbation on does not
change the values of another. Zero draws are left out of `material_draws`,
`node_offsets_m` and `cfo_hz`, and `label` is `perturbed` when anything is
left.

**H0 frames.** Noise frame m of realization r on link (tx, rx) is

Y[n, k] = H_r[n, k] + w[n, k],
H_r[n, k] = Σ_p |a_p| e^{jφ_p} e^{−j2π f_k τ_p} e^{+j2π (f_D,p + cfo_rx) t_n}

on the consumer's pilot grid (§6, *Waveform numerology*; f_k baseband, t_n
the pilot symbol times, φ_p the stored `phase_rad`, which already holds the
carrier term). w is complex Gaussian with E|w|² = σ_H² (N0 per RE over Es per
RE), drawn from
`SeedSequence(entropy=[seed, 0x5EA031], spawn_key=(r, m, id32(tx), id32(rx)))`,
where `id32` is the first 4 bytes (little endian) of the id's SHA-256.
0x5EA031 is the H0 calibration stream: a scenario run draws its in-run noise
from 0x5EA024, so an ensemble `seed` equal to a run's `noise_seed` never
reproduces that run's noise. The noise level is the consumer's: the
detector uses its own waveform, noise figure and TX power. Frames,
realizations and links draw from independent streams (adjacent frames
correlate below |ρ| = 0.01 over 10⁵ REs).

**Calibrating a threshold.** Set `sensing.detection.threshold:
"h0_calibrated"` and `h0_result_id` to the ensemble's `result_id` (§9,
*Signal-level detection*). The detector builds the map of every realization
× noise frame × link with the run's numerology, map and CFAR options and
takes `sorted(values)[n − floor(n·p)]` of the cell statistic, or of the
per-frame maximum over every link with `system_pfa`. With n = 10,000
calibration frames at P_FA 10⁻², a held-out 10,000 frames show 74–129 false
alarms in 95 % of calibrations (beta-binomial: the calibration's own spread
widens the binomial 81–120). 10⁻³ needs n ≥ 20,000; 10⁵ is better.

**Result.** `SensingBackgroundEnsembleResultSet` holds `source`, `seed`,
`noise_frames_per_realization`, `background` (the options used), `links`,
the nominal `nodes`, and per realization `index`, `label`, `material_draws`
(name → `eps_r_scale`, `sigma_scale`), `node_offsets_m`, `cfo_hz`,
`background_h0` (RayPath list; null when reused), `source_realization`,
`solve_s`, `num_paths`, `dropped_paths`, `total_power_db` ("tx>rx" →
10 log10 Σ|a|²) and `warnings`. `metadata` carries the request and its
`request_hash`, `material_names`, `sites`, `bistatic_rx_ids`, `solves`,
`total_solve_s`, `draw_key`, `noise_key` and the provenance hashes. Read it
back with `GET /results/sensing-background-ensemble?result_id=…` (the latest
without `result_id`); `&summary=true` returns every realization without its
`background_h0` paths, which is what the UI shows. Labels and pruning work
as for every result kind. Each stored path takes about 0.7–1 KB of JSON, and
the background options default to 256 paths per link.

**Errors.** Unknown project or source result, or a source file that is not a
valid scenario result: **404**. A frame index out of range or a frame without
sensing, a device or target actor of the run missing from the scene, `scope:
"prim"`, more than 2,000,000 paths stored over the solved realizations
(lower `background.max_paths_per_link`), or a realization whose background
solve failed (it would store a noise-only H0): **400**. The source run's backend
unavailable or without sensing: **409**. Out-of-range values, unknown keys or
a background mode other than `h0_removed`: **422**.

**Limits.**

- The material scales act on the loaded solver materials for one solve and
  are restored after it; the stored project and its RF projection are never
  written. The mock perturbs constant-model library materials only (ITU
  materials keep their loss there).
- With diffuse reflection on and `background.deterministic: false`,
  realizations also differ by Monte Carlo ray resampling. That is RT sampling
  noise, not a clutter realization, and the ensemble warns. Every realization
  uses the config's solver seed.
- The perturbations are static: no internal clutter motion, phase noise or
  timing error beyond the CFO. A static nominal twin has an exactly 0 Hz
  background.
- Per-prim draws (`scope: "prim"`) are not available.

## Related docs

- [simulation.md](simulation.md) — paths, radio maps, beamforming, channel analysis
- [trajectory_uav.md](trajectory_uav.md) — actor trajectories (the default target velocity)
- [datasets_export.md](datasets_export.md) — RFData / AODT / channel npz exports
- [../dynamic_scattering.md](../dynamic_scattering.md) — Doppler of moving devices and actors in plain paths solves
