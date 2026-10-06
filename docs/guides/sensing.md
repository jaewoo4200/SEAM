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

## 6. Sensing over time: detection and multistatic fusion

A sensing solve is one snapshot. **Simulate scenario** with sensing on runs a
sensing solve in **every frame**, at the frame's actor poses and velocities
(and rider positions), turns each echo into a per-link detection, and fuses
the detected links into a position/velocity estimate that is scored against
the true actor track.

**Running it.** In Results mode open **Scenario playback**, tick
**Sensing (ISAC)** under *Include paths*, set the threshold, CPI and
integrated pulses, and run. The checkbox stays disabled until at least one
actor has an enabled sensing binding. Over the API, add a `sensing` block to
the scenario request:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/scenario \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "num_frames": 61, "dt_s": 0.5, "include_paths": false,
       "sensing": {"enabled": true, "threshold_db": 13, "cpi_s": 0.01,
                   "cpi_pulses": 4096, "measurement_noise": true}}'
```

The result is an ordinary `scenario` result. Each frame gains
`sensing: {echoes, links, estimates}` and `metadata.sensing` holds the run
summary. With `sensing` absent or `enabled: false` the result is the same as
before (each frame's `sensing` is `null`). Comm and sensing share one backend:
`auto` resolves as for any scenario, and a backend without sensing (sionna-rt
< 2.2) answers **409** instead of falling back to the mock. No TX or no RX
answers **400**, and so does no bound actor or an unknown
`target_actor_ids` entry. The run holds the per-project solve lock and
reports progress per frame, and **Cancel** works.

| `sensing` field | Default | Meaning |
|---|---|---|
| `enabled` | `false` | Run the per-frame sensing solve. |
| `threshold_db` | 13 | Detection threshold on the post-integration SNR (dB, −30…60). |
| `cpi_s` | 0.01 | Coherent processing interval (s). Doppler resolution = 1 / CPI. |
| `cpi_pulses` | 1 | Coherently integrated pulses or OFDM resource elements. Gain = 10·log10(N). |
| `mti_min_doppler_hz` | `null` | MTI notch: \|f_D − f_nodes\| below it is rejected (f_nodes = 0 with static TX/RX; see MTI below). `null` = 1 / CPI (one Doppler cell); `0` disables MTI. |
| `include_comm_paths` | `false` | Also solve each frame's comm paths with the targets as absorbers and append them to `echoes` (`target_id` null). One extra solve per frame. |
| `target_actor_ids` | `null` | Restrict to these actors (default: every enabled binding). |
| `samples_per_sp` / `max_depth` | 1 000 000 / `null` | Passed to each frame's sensing solve, as in §3. |
| `measurement_noise` | `false` | Add Gaussian noise to the range and Doppler fed to the fusion (below). |
| `noise_seed` | 0 | Seed of that noise. Same seed = same numbers. |

**Detection.** For each TX → RX link and target, the strongest **direct**
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
SNR, and links whose foci coincide (a reciprocal pair, or a repeated link)
are kept once. With at least **3** geometrically distinct links, the
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

- `links[]` has one row per TX × RX × target, in that order: `bistatic_range_m`,
  `doppler_hz`, `snr_db`, the `measured_*` values the fusion used, `range_bin`
  (floor(R / (c/B))), `doppler_bin` (round(f_D · CPI)) and `reason`:
  `detected`, `below_threshold`, `mti_rejected` or `no_echo`.
- `estimates[]` has one row per target: `status` is `ok`,
  `insufficient_links` (< 3 distinct links) or `diverged` (no convergence,
  degenerate geometry, an ambiguous 3-link fit, or an RMS residual above one
  range-sum cell c/B). It
  also carries `links_used`, `position_est` / `velocity_est`, the truth
  (`position_true` = the target cuboid center, `velocity_true`), and their
  errors. `gdop` = sqrt(trace((JᵀJ)⁻¹)): the position error is about
  GDOP × the range error.
- `metadata.sensing` holds the constants (noise floor, integration gain,
  λ, range and Doppler resolutions, MTI notch, blind speed) and, per target,
  `detection_rate` (frames with ≥1 detected link), `link_detection_rate`,
  `frames_ge3_links_rate`, `ok_frames`, the median and p90 position error,
  the median velocity error and the median GDOP.

Without `measurement_noise` the measured values are the solver's exact
delay and Doppler, so an `ok` estimate is exact to rounding (useful as a
sanity check). The one exception is an exact mirror tie: when all nodes of
the fused links lie in one plane (always so for three nodes), the mirror
across that plane fits exactly too, and the tie rule above picks the side.
With it, the range and
Doppler get noise of
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
counts single elements (see §9 Limits), but §7 synthesizes real codebook
beams and §8's `steered` mode adds the ideal array gain. The mmWave notch,
on the other hand, is 8× narrower in speed.

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
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/isac \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "tx_rows": 4, "tx_cols": 4,
       "sweep_start_deg": -60, "sweep_stop_deg": 60, "sweep_step_deg": 5,
       "cpi_pulses": 4096, "pfa": 1e-6, "sharing_mode": "dual_function"}'
curl http://127.0.0.1:8000/api/projects/demo/results/isac   # latest stored result
```

The result persists as an `isac` result set (run history, prune, label).
Like `/simulate/sensing`, `auto` uses sionna when sionna-rt ≥ 2.2 is
installed and otherwise the mock with a warning, and an explicit sionna
without the RCS solver answers **409**. An unknown device or actor, no UE,
a TX without a sensing receiver, or `use_device_orientation: false` answers
**400** before anything is solved.

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
  N0 = −174 + 10·log10(B) + NF. There is no inter-TX interference, so
  SINR = SNR.
- `rate_k,u = log2(1 + SINR_k,u)` and `R(k) = Σ_u rate_k,u` over the UEs
  this TX serves.
- Echo of target q with TX beam k and RX beam m, every echo t → s → q
  summed coherently: `E_q[k, m] = Σ_e α_e (w_kᴴ a_t(e)) (w_mᴴ a_s(e))`. The
  best RX beam is taken per TX beam, and

  `SNR_q(k, ρ) = P_t + 20·log10 max_m |E_q[k, m]| − N0 + 10·log10(ρ · cpi_pulses)`.

  The weakest target sets the sensing SNR of a beam.

**Pd.** A Swerling-1 target with a square-law detector on the integrated
sample has P_fa = e^(−T) and P_d = e^(−T / (1 + SNR)), so

`P_d = P_fa^(1 / (1 + SNR))`.

At P_fa = 10⁻⁶, P_d = 0.9 needs **21.1 dB**, and the 13 dB threshold gives
P_d = **0.517**. No echo gives P_d = P_fa.

**Slot sharing.** The comm beam k_c = argmax R(k). A fraction ρ of the slots
(or pulses) senses with beam k, so the echo integrates ρ · `cpi_pulses`.

- `time_sharing`: sensing slots carry no data. rate_u = (1 − ρ) · rate_k_c,u.
- `dual_function`: the sensing beam also carries data to whoever it reaches.
  rate_u = (1 − ρ) · rate_k_c,u + ρ · rate_k,u.

ρ = 0 is one comm-only point (`beam_idx` null, P_d = P_fa). With
`dual_function` the ρ = 1 rows equal the beams themselves (one beam does
both). With `time_sharing` they carry no rate.

**Reading the output.** Each `txs[]` entry holds:

- `beams[]`: one row per codebook angle, with each UE's SINR, the sum rate,
  each target's echo SNR and best RX angle, the weakest-target SNR, P_d and
  `detected`. The panel's beam table tints the comm-beam row cyan and the
  sensing-beam row magenta, and the **ISAC lobes** overlay draws both beams
  on each TX in the same colors (see §9 for what it leaves out).
- `comm_beam_angle_deg`, `sensing_beam_angle_deg` and `angle_gap_deg`.
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
`pd_at_threshold`. A TX that serves no UE gets a sensing-only trade-off (all
rates 0) and says so in its `warnings`.

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
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/sensing-coverage \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "height_m": 60, "cell_size_m": 10,
       "threshold_db": 13, "cpi_pulses": 4096, "array_gain": "none"}'
curl http://127.0.0.1:8000/api/projects/demo/results/sensing-coverage
```

The result persists as a `sensing_coverage` result set. Sensing receivers
follow the ISAC rule (an RX within 1 m of a selected TX, or
`sensing_rx_ids`). A TX without one answers **400**.

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

**Model.** Per link and cell, with R_t = |cell − TX| and R_r = |cell − RX|:

`SNR = P_t + G + G_e,t + G_e,r + 10·log10(λ² σ / ((4π)³ R_t² R_r²)) − A(R_t + R_r) − N0 + 10·log10(cpi_pulses)`

when **both legs are line-of-sight**, and no echo otherwise. G is the array
gain above and A the atmospheric absorption (0 unless enabled in the
config). G_e,t and G_e,r are the TX's and the RX's element gain toward the
cell, from each device's `antenna.pattern` in its own frame
(`orientation_deg`), as Sionna applies them: 0 dB for `iso`, 8 dBi down to
−22 dBi (a 30 dB floor) for `tr38901`. `metadata.element_patterns` lists them.
With `iso` elements the equation is the mock echo's, so a mock sensing
solve with a target at a cell center returns exactly the map's value (the
mock's echoes are always isotropic). On sionna the LOS test is a Mitsuba
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
- `pd_best`: the Swerling-1 P_d of the best link (§7).
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
its LOS and detected fractions.

## 9. Limits

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
- Sensing over time (§6) uses **oracle association**: the ray tracer labels
  each echo with its target and as direct or multipath. A real receiver would
  have to infer both.
- Multi-scattering-point targets (`vehicle-multi-sp`, `agv-multi-sp`): each
  link reports its strongest direct echo, which may come from a different
  scattering point on each link, while `position_true` is the cuboid center.
  Expect a bias of up to half the target size (about 2.5 m for a car) in
  `position_error_m`. The mock uses one point at the center, so it has none.
- Detection is per echo and noise-limited: no CFAR, no range–Doppler map, no
  clutter or self-interference power.
- Sensing over time (§6) counts antennas as single elements (no array
  gain), so integration gain is its only processing gain.
- `measurement_noise` is a rule of thumb (cell / sqrt(2·SNR)), not a
  Cramér–Rao bound.
- Each frame is an independent snapshot. There is no tracking filter: the
  previous estimate only seeds the next frame's solver.
- ISAC trade-off (§7):
  - The codebook is azimuth-only: the vertical rows stay broadside. A
    4-row λ/2 panel has a vertical null 30° off its plane, and is at least
    14 dB per end below broadside anywhere from 25° to 35° (21–23 dB at
    28° and 33°). An up-tilted rooftop TRP can therefore lose more than its
    array gain toward street UEs or a steep drone, so a 4×4 beam can come
    out below the single-element anchor.
  - With the default `iso` element the panel has a mirror back lobe: beam θ
    also points at 180° − θ behind the panel at full gain, so UEs and
    targets behind a TRP are not clipped. Use the `tr38901` element for a
    front-to-back ratio. With λ/2 spacing, a target outside the sweep near
    endfire (say 78° off broadside with a ±60° sweep) is seen by both edge
    beams within about 1 dB of each other, through the grating-lobe skirt,
    so which edge wins is nearly arbitrary.
  - The **ISAC lobes** overlay draws each beam's azimuth cut over the
    panel's front hemisphere only. With `iso` (or another symmetric)
    element a TX can serve a UE behind its panel through the back lobe
    while its cyan comm lobe points away from that UE.
  - No inter-TX interference (SINR = SNR), and each UE's rate assumes it
    has the full band.
  - P_d is the Swerling-1 closed form: no CFAR, no range or Doppler
    straddle loss.
  - One t = 0 snapshot. Only the first polarization port of a
    dual-polarized antenna is synthesized, and UEs are single elements.
- Sensing coverage (§8):
  - The target is a constant point RCS: no TR 38.901 angular lobes.
  - Only direct LOS legs count (no multipath echoes), and actor meshes are
    ignored by the LOS test.
  - `steered` is the ideal full-array gain on every link and cell.
  - Element gain uses each device's own antenna. Sionna solves apply the
    first selected TX's (RX's) antenna to every TX (RX), so with mixed
    patterns the two differ. Polarization mismatch is not modeled.
  - The mock treats every leg as line-of-sight.

## Related docs

- [simulation.md](simulation.md) — paths, radio maps, beamforming, channel analysis
- [trajectory_uav.md](trajectory_uav.md) — actor trajectories (the default target velocity)
- [datasets_export.md](datasets_export.md) — RFData / AODT / channel npz exports
- [../dynamic_scattering.md](../dynamic_scattering.md) — Doppler of moving devices and actors in plain paths solves
