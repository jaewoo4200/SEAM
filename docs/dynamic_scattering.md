# Dynamic Scattering

> **English** · [한국어](dynamic_scattering.ko.md)

This document covers (1) what "dynamic scattering" actually is in real channel physics (source-based), (2) how commercial/research tools model it, (3) a step-by-step plan for implementing it in this repository (SEAM Studio) with the exact sionna-rt 2.0.1 API (including an effect/cost ranking), and (4) empirically verified Doppler how-to snippets.

Every sionna-rt 2.0.1 API claim was verified by (a) reading the installed source `backend/.venv/Lib/site-packages/sionna/rt/`, and (b) running live probes with `backend/.venv/Scripts/python.exe`. Package version `2.0.1` confirmed (`sionna/rt/__init__.py:9`). Items not confirmed against an official/peer-reviewed source are marked **(unverified)**.

---

## 1. What is dynamic scattering (physics)

"Dynamic scattering" is not a single defined term; in the RF channel literature it refers to **the time-varying component of the propagation channel caused by objects (scatterers) that move relative to Tx/Rx**. There are three physically distinct sub-effects.

1. **Moving scatterers → time-varying multipath geometry.** When vehicles/pedestrians/foliage move, the set of ray interaction points changes frame by frame, so path delays, angles, and amplitudes vary over time. The "geometry re-solve" effect.
2. **Doppler spread from scatterer motion.** A single moving interaction point imparts a per-bounce Doppler `f_Δ = (1/λ)·vᵀ(k_out − k_in)`. A cluster of many moving scatterers creates a Doppler **spread** around 0 Hz, broadening the Doppler power spectrum and shortening the channel coherence time (even when Tx/Rx are stationary). The scatterer-motion analog of the classic Clarke/Jakes spectrum (moving Rx).
3. **Time-varying diffuse multipath.** Rough surfaces re-radiate a non-specular (diffuse) component. When the rough surface or illuminated patch moves, the diffuse cloud fluctuates — the dominant fading mechanism in dense scattering environments (foliage/crowds/vehicles), and precisely what "dynamic diffuse scattering" targets.

**How measurement campaigns characterize it (peer-reviewed):**
- **Effective Roughness (ER) model** — Degli-Esposti et al., IEEE TAP 2007 [1]. Defines the scattering coefficient S ∈ [0,1] and the Lambertian/directive/backscatter lobe patterns → implemented and cited by Sionna (`cite:p:Degli-Esposti07`).
- **mmWave directive model parameterization** — building-material measurements of α_R, Int. J. Antennas Propag. 2020 [2].
- **ITU-R P.2040** (building material/structure effects) and **ITU-R P.1411** (short-range outdoor) provide the material electrical parameters and diffuse-scattering guidance used to seed S and permittivity — *the clause-level original text was not consulted in this work* **(unverified)**.
- Doppler spread/coherence time extend the classic **Clarke/Jakes** framework (moving terminal) to moving scatterers; T_c ≈ 0.42/f_d,max.

---

## 2. How the tools model it

### 2.1 Sionna RT 2.0.1 (verified against the installed package)

All claims below were verified against `backend/.venv/Lib/site-packages/sionna/rt/`.

**(A) Diffuse scattering models** — `radio_materials/scattering_pattern.py`:
- `LambertianPattern` (`fs = cos θ_o / π`, `:201-232`).
- `DirectivePattern(alpha_r)` — a lobe around the specular direction (`:394-416`).
- `BackscatteringPattern(alpha_r, alpha_i, lambda_)` — normalized dual lobe (`:234-392`).
- Name registration: `"lambertian"`, `"directive"`, `"backscattering"`; direct import available: `from sionna.rt import LambertianPattern, DirectivePattern, BackscatteringPattern`.
- `RadioMaterial` (`radio_material.py`): `scattering_coefficient` (S, `:220-233`), `xpd_coefficient` (K_x ∈ [0,1], `:235-249`, reconstructs the XPD Jones matrix after range validation), `scattering_pattern` (must be a `ScatteringPattern` instance, `:251-263`). Diffuse energy is generated only when `scattering_coefficient > 0` **and** the solver's `diffuse_reflection=True`.

**(B) Object velocity → per-path Doppler (the core dynamic-scattering API)**:
- `SceneObject.velocity` — settable `mi.Vector3f` [m/s] (`scene_object.py:252-283`); stored as the mesh's `"velocity"` `rawconstant` texture attribute. Default `(0,0,0)`.
- `Transmitter`/`Receiver` also take a `velocity=` argument (`radio_devices/radio_device.py:50,109-119`).
- The field calculator reads the interacting object's velocity via `shape.eval_attribute_3("velocity", …)` and accumulates a per-bounce Doppler `v_effective = (k_out − k_in)·v_world; doppler += v_effective/wavelength` (`field_calculator.py:526-562`). Applies to **specular, diffuse, and refraction alike**.
- The Tx/Rx terminal terms come from `paths.py:_finalize_doppler_shift_compute` (`:1215-1246`): `doppler = paths_buffer.doppler + tx_doppler − rx_doppler`.
- The result is `Paths.doppler` [Hz/path] (`paths.py:336-385`), multi-bounce `f_Δ = (1/λ)[v₀ᵀk₀ − v_{n+1}ᵀk_n + Σ vᵢᵀ(kᵢ−k_{i-1})]`.

**(C) Channel time evolution from Doppler**:
- `Paths.cir(sampling_frequency=1.0, num_time_steps=1, normalize_delays=True, reverse_direction=False, out_type="drjit")` (`paths.py:387-393`). When `num_time_steps>1`, the per-path `aᵇ_i(t) = aᵇ_i · e^{j2π f_Δ,i t}` is applied (`paths.py:506-516`) — synthesizing a time series from Doppler alone **within a single solve**. `Paths.cfr(...)` is the frequency-domain equivalent. In 2.x there is **no separate `apply_doppler`** — it is folded into `cir`/`cfr`.

**(D) Solver flags** — `PathSolver.__call__` (`path_solvers/path_solver.py:144-157`): `diffuse_reflection: bool = False`, `specular_reflection`, `refraction`, `diffraction`, `edge_diffraction`, `max_depth=3`, `samples_per_src=1_000_000`, `synthetic_array=True`, `seed=42`. Diffuse paths are Monte Carlo samples → seed-dependent, requiring sufficient `samples_per_src`.

Docs: scattering tutorial [4], radio materials [5], Paths API [6], technical report [7].

### 2.2 Remcom Wireless InSite
Implements the same **ER family** (Lambertian, directive, directive-with-backscatter) via a scattering coefficient S and cross-polarization fraction — a verified reference implementation of the Degli-Esposti model. For dynamic scenes it re-runs propagation per time step for moving objects; motion Doppler is derived from the path-geometry change between frames [8].

### 2.3 NVIDIA AODT (Aerial Omniverse Digital Twin)
Turns on EM scattering from moving vehicles via an explicit **"Enable Dynamic Scatterers"** flag. Computes time-varying channel/Doppler from consistent UE+vehicle mobility and Omniverse geometry+motion data. Spiritually the closest to this design's goals — per-frame re-solve + object velocity, GPU accelerated [9][10].

### 2.4 MATLAB (Communications/Antenna Toolbox)
`raytrace` (SBR/image) supports reflection/diffraction/diffuse scattering; `comm.RayTracingChannel` applies **Tx/Rx velocity as Doppler** and reuses the ray set along a mobility track. However, per-path *scatterer* velocity Doppler is not a first-class input the way Sionna's `SceneObject.velocity` is [11][12].

---

## 3. Current state of this tool (verified against the repository)

- **Per-frame re-solve for moving actors is already wired.** `apply_actor_states()` (`sionna_backend.py:339`) sets `SceneObject.position`/`.orientation` per scenario frame; the solver runs once per frame (`:776`, `:896`). → Effect #1 (time-varying geometry) is already produced.
- **The diffuse flag is passed through:** `diffuse_reflection=config.scattering` (`:781`).
- **Materials:** `_apply_custom_materials` (`:973`) sets only `scattering_coefficient` (`:1024-1027`). `xpd_coefficient` / `scattering_pattern` are not set.
- **Missing physics (Gap):**
  - **(Resolved — Design A)** `apply_actor_states` now takes a `velocities` argument and sets per-actor `obj.velocity = mi.Vector3f(...)` (`sionna_backend.py:417-424`) → moving actors carry per-path Doppler (no longer identically 0). See the "Implementation Complete" section below for details.
  - `.cir(num_time_steps=…)` is not called → no in-frame time evolution; paths are converted straight to `RayPath`.
  - Lambertian/directive `alpha_r` and XPD are not exposed.
- **Time basis exists:** `dt_s` is present in both the actor trajectory schema (`schemas/scene.py:155`) and the simulation config (`schemas/simulation.py:182`), and `TrajectorySample.time_s = i*dt_s` (`services/trajectory.py:363/381/505`) — the per-frame velocity `(pₙ₊₁ − pₙ)/dt_s` can be derived directly.

---

## 4. Implementation plan (effect/cost ranking)

Recommended order: **A → B → C** (A is the minimal work that unlocks Doppler, B is the minimal work for patterns/XPD, and C combines the two into a complete dynamic-diffuse output).

### Design A — velocity-based per-path Doppler for moving actors (highest value / lowest cost)

**New physics:** Effect #2 — true per-path Doppler shift from actor motion, enabling coherence time, Doppler spread, and time-evolving CIR output. Resolves today's biggest gap (Doppler=0).

**Exact API changes (all verified against 2.0.1):**
1. In `apply_actor_states` (`sionna_backend.py:226`), compute and set per-actor velocity. Velocity = `(state.position − prev_state.position)/dt_s`, or the trajectory tangent × speed. Then:
   ```python
   import mitsuba as mi
   obj.velocity = mi.Vector3f(vx, vy, vz)   # m/s, world frame (scene_object.py:266)
   ```
   `dt_s` already exists in the scenario; pass the previous frame's actor position (or the two bracketing waypoints) into `apply_actor_states`.
2. (Optional) Same for trajectory-following Tx/Rx: `Transmitter(..., velocity=...)` / `rx.velocity = ...` (`radio_device.py:109`).
3. After solve, read `solved.doppler` (`paths.py:336`) and pass it to `RayPath` (add a `doppler_hz` field), or emit a time-evolving CIR:
   ```python
   a_real, a_imag, tau = solved.cir(
       sampling_frequency=config.bandwidth_hz or 1/dt_s,
       num_time_steps=N, out_type="numpy")
   ```

**Cost:** ~0.5–1 day. No new dependencies (velocity is a subtraction). The main work is threading the previous-frame position into `apply_actor_states` + a one-line `doppler_hz` schema field/conversion.

**Caution:** `SceneObject.velocity` requires that the actor be **individually addressable** (not merged with same-material geometry) — the code already warns on merged actors (`sionna_backend.py:266-270`), so apply the same guard.

### Design B — expose scattering pattern + XPD (medium value / low cost)

**New physics:** control over the *shape* and *polarization* of the diffuse lobe (effect #3). A range from Lambertian (fully diffuse) to directive (large α_R, quasi-specular) plus cross-polarization coupling. Needed to match real mmWave rough-surface behavior and to calibrate measured α_R.

**Exact API changes (verified against 2.0.1):** extend the custom material block (`sionna_backend.py:797`) and the material schema (`schemas/materials.py`):
```python
from sionna.rt import LambertianPattern, DirectivePattern, BackscatteringPattern
pat = custom.get("scattering_pattern")          # "lambertian" | "directive" | "backscattering"
if pat == "directive":
    rt_mat.scattering_pattern = DirectivePattern(alpha_r=int(custom["alpha_r"]))
elif pat == "backscattering":
    rt_mat.scattering_pattern = BackscatteringPattern(
        alpha_r=int(custom["alpha_r"]), alpha_i=int(custom["alpha_i"]),
        lambda_=float(custom["lambda_"]))
else:
    rt_mat.scattering_pattern = LambertianPattern()
xpd = custom.get("xpd_coefficient")
if xpd is not None:
    rt_mat.xpd_coefficient = float(xpd)          # radio_material.py:243, validates [0,1]
```
Setter validation: `scattering_pattern` (`radio_material.py:259`, `ScatteringPattern` type check), `xpd_coefficient` (`:243`).

**Cost:** ~0.5 day. Pure material plumbing + 3 new schema fields (`scattering_pattern`, `alpha_r/alpha_i/lambda_`, `xpd_coefficient`). Combines with the existing `scattering_coefficient` and `diffuse_reflection`.

### Design C — per-frame diffuse solve + coherent time series for a time-varying diffuse channel (high value / high cost)

**New physics:** full "dynamic diffuse scattering" — effects #1+#2+#3 simultaneously. Time-varying channel both *across* and *within* frames, with realistic Doppler spread from many moving diffuse scatterers. What AODT "dynamic scatterers" and Remcom dynamic diffuse provide.

**Composition (built on A+B):**
1. Enable diffuse for all frames (`diffuse_reflection=True`, `scattering_coefficient>0`, and the Design B pattern specified).
2. Set actor **velocity** per frame (Design A) → diffuse interaction points carry Doppler (`field_calculator.py:550` reads velocity for all interaction types including `InteractionType.DIFFUSE`, verified `:256-300`).
3. Emit a short coherent time series per frame: `solved.cir(sampling_frequency=fs, num_time_steps=N)` (`paths.py:387`) → in-frame Doppler evolution; concatenate across frames for the full time-varying CIR.
4. Raise `samples_per_src` (`path_solver.py:148`) to stabilize diffuse paths, and fix a per-frame `seed` for trajectory reproducibility.
5. Derive Doppler spread/coherence time metrics from `solved.doppler` (RMS Doppler = the |a|²-weighted standard deviation of the per-path `doppler`), and expose them on `TrajectorySample` (`services/trajectory.py:104`) like the existing RMS delay spread.

**Cost:** ~2–4 days. Cost drivers: diffuse Monte Carlo increases per-frame solve time and memory (tuning `samples_per_src`/`max_depth`); designing the concatenated time-series output schema and the Doppler-spread aggregation; and verifying that the diffuse path count does not exceed the `[:100]` path cap (`trajectory.py:205`). The most physics, the heaviest computation.

### Ranking summary

| Design | New physics effect | Value | Cost | Prerequisite |
|--------|-------------|------|------|------|
| **A** velocity-based Doppler | #2 | Highest | ~0.5–1 day | None |
| **B** scattering pattern+XPD | #3 | Medium | ~0.5 day | None |
| **C** time-varying diffuse channel | #1+#2+#3 | Highest (combined) | ~2–4 days | A, B |

---

## 5. Verified Doppler How-To (sionna-rt 2.0.1)

**Empirically verified:** empty scene, pure LOS probe, 3.5 GHz, RX approaching TX at 30 m/s → `paths.doppler == 350.2423 Hz`, exactly `v/λ = 30/0.085655 = 350.2423`. Approaching → positive (+) Doppler. On this machine Mitsuba resolves the `cuda_ad_mono_polarized` variant (CUDA present); the LLVM-init stderr warning that appears on CPU fallback is harmless. The Doppler formula cites Wiffen et al. 2018 (`paths.py:363`).

### 5.1 Where to set velocity (3 independent sources)

| Entity | API | Source (file:line) |
|--------|-----|------------------|
| Transmitter/Receiver (base `RadioDevice`) | `velocity` constructor argument **and** `.velocity` get/set; `mi.Vector3f` [m/s] | `radio_devices/radio_device.py:35,50,109-119` |
| Scene object (arbitrary mesh, e.g. a vehicle) | `SceneObject.velocity` get/set; `mi.Vector3f` [m/s] | `scene_object.py:253-283` |

Live verification notes:
- `RadioDevice.velocity` defaults to `Vector3f(0,0,0)` (`radio_device.py:68-69`). The setter wraps the input in `mi.Vector3f` → Python lists are allowed: `Transmitter(..., velocity=[10,0,0])` and `tx.velocity = mi.Vector3f(5,0,0)` both succeed.
- `SceneObject.velocity` is lazily backed by the `"velocity"` `rawconstant` texture attribute (`scene_object.py:274-283`). Before it is first set, it returns `Vector3f(0.)` (`:261-262`). Only a **single** vector per object is allowed (`assert dr.width(v)==1`, `:269`).
- Constructor signatures (probed): `Transmitter.__init__(self, name, position, orientation=None, look_at=None, velocity=None, power_dbm=..., color=..., display_radius=None)`; `Receiver.__init__(self, name, position, orientation=None, look_at=None, velocity=None, color=..., display_radius=None)`.

### 5.2 What the PathSolver computes from velocity

- One Doppler per path → **`Paths.doppler`** (`paths.py:335-385`).
- **Setting velocity does not change the static tx/rx positions, geometry, or ray tracing** — it affects only the Doppler channel (geometry is a snapshot; time evolution is synthesized analytically from Doppler).
- Two contributions: (1) moving objects — during field calculation each interaction reads the object's `"velocity"` attribute and accumulates `v·(k_out − k_in)/λ` (`field_calculator.py:526-562`, `_update_doppler_shift`); static objects contribute 0. (2) moving tx/rx — `paths.py:1215-1246`: `f_Δ = paths_buffer.doppler + (k_tx·v_tx)/λ − (k_rx·v_rx)/λ`, `k_tx=r̂(θ_t,φ_t)` (departure), `k_rx=−r̂(θ_r,φ_r)` (arrival). Device velocity is obtained from `.velocity` by `scene.sources()/targets()`.
- **`paths.doppler` shape** (`mi.TensorXf`, Hz): synthetic array (default) `[num_rx, num_tx, num_paths]`, non-synthetic `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths]`. Probe: single-antenna tx/rx over `simple_street_canyon_with_cars` → `(1,1,10)`.

### 5.3 Time-evolving CIR — the primary output API

`Paths.cir(*, sampling_frequency=1.0, num_time_steps=1, normalize_delays=True, reverse_direction=False, out_type="drjit")` (`paths.py:387-524`).

Baseband coefficient per path and time step (`paths.py:404-405`):
```
a^b_i(t) = a_i · e^{−j2π f τ_i} · e^{ j2π f_Δ,i t},   t = n/sampling_frequency,  n = 0..N−1
```
The Doppler phase term is applied **only when `num_time_steps > 1`** (`paths.py:505-520`); with the default `num_time_steps=1` it is a static snapshot, so velocity does not show up in the `cir` output (although `paths.doppler` is always populated).

- **Returns** `(a, tau)`: `a` = a real/imag pair (drjit) or a single complex array (numpy/tf/torch/jax), shape `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths, num_time_steps]`. `tau` = delay [s], `[num_rx, num_tx, num_paths]` (synthetic).
- Probe: static `a=(1,1,1,1,10,1)`; `num_time_steps=16` → `a=(1,1,1,1,10,16)`, `complex64`; magnitude constant, phase rotating (confirmed the last step differs from the first).
- `out_type`: `"drjit"` (default) / `"numpy"` / `"jax"` / `"tf"` / `"torch"`.

Related helpers (both evolve Doppler identically via `sampling_frequency`/`num_time_steps`):
- **`Paths.taps(bandwidth, l_min, l_max, sampling_frequency=None, num_time_steps=1, ...)`** — discrete-time (TDL) CIR, `[..., num_time_steps, l_max−l_min+1]` (`paths.py:526-`). Probe `(1,1,1,1,16,11)`, complex64.
- **`Paths.cfr(frequencies, sampling_frequency=1.0, num_time_steps=1, ...)`** — channel frequency response `[..., num_time_steps, num_frequencies]` (`paths.py:660-`).

Choosing parameters: `sampling_frequency` is the CIR resampling rate. To resolve the maximum Doppler without aliasing, `sampling_frequency ≥ 2·max|f_Δ|`; the evolution window length = `num_time_steps / sampling_frequency` [s]. For an OFDM slot the convention is `sampling_frequency = subcarrier_spacing` (or `1/slot_duration`).

### 5.4 Minimal runnable snippet (verified against the backend venv)

```python
import sionna.rt as rt
from sionna.rt import Transmitter, Receiver, PlanarArray, PathSolver, load_scene
import mitsuba as mi
import numpy as np

scene = load_scene(rt.scene.simple_street_canyon_with_cars)
scene.frequency = 3.5e9  # Hz; scene.wavelength 자동 설정

scene.tx_array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization="V")
scene.rx_array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization="V")

# (a) 이동 라디오 디바이스 — 속도 m/s, Z-up world frame
tx = Transmitter("tx", position=[-30, 0, 10], velocity=[0, 0, 0])
rx = Receiver("rx", position=[30, 0, 1.5], velocity=[30, 0, 0])  # RX 30 m/s +x
scene.add(tx); scene.add(rx)

# (b) 이동 씬 물체(예: 차량 메쉬) — velocity 속성 설정
for name, obj in scene.objects.items():
    if "car" in name.lower():
        obj.velocity = mi.Vector3f(20.0, 0.0, 0.0)

# 경로 solve (velocity는 기하 불변, Doppler만 영향)
paths = PathSolver()(scene, max_depth=3)

# 경로별 Doppler [Hz], 형상 [num_rx, num_tx, num_paths] (synthetic array)
doppler_hz = paths.doppler.numpy()
print("Doppler [Hz]:", doppler_hz.ravel())

# 시간전개 기저대역 CIR: a[num_rx,num_rx_ant,num_tx,num_tx_ant,num_paths,num_time_steps]
fs = 1000.0          # CIR sampling frequency [Hz] -> window = num_time_steps/fs [s]
num_time_steps = 16
a, tau = paths.cir(sampling_frequency=fs, num_time_steps=num_time_steps, out_type="numpy")
print("a:", a.shape, a.dtype, "| tau:", tau.shape)  # a: (...,16) complex64
```

**Project invariants (Z-up, m/s):** the velocity vector is in the same world frame as position, with `vz` vertical in `[vx, vy, vz]`. `scene.frequency` must be set before solving so that λ (and therefore Doppler) is correct — `scene.wavelength` is derived and read-only.

### 5.5 Pitfalls (verification/source based)

- The default `num_time_steps=1` ⇒ the Doppler phase is not applied in `cir`/`taps`/`cfr` (`paths.py:505`); even if a device moves, `num_time_steps > 1` is required to see time evolution.
- `paths.doppler` is always computed regardless of `num_time_steps` — you can read the raw shift without any evolution.
- `SceneObject.velocity` must be set at least once to be stored/differentiable (`scene_object.py:256-257`); before that it is `Vector3f(0.)`.
- Non-relativistic approximation: first-order Taylor (‖v‖ ≪ c); the exact formula and derivation are in the `Paths.doppler` docstring (`paths.py:365-381`).
- `reverse_direction=True` swaps the tx/rx roles in `cir`/`taps`/`cfr` (uplink/downlink reuse).

**Relevant files (absolute paths):**
- `backend/.venv/Lib/site-packages/sionna/rt/radio_devices/radio_device.py`
- `backend/.venv/Lib/site-packages/sionna/rt/scene_object.py`
- `backend/.venv/Lib/site-packages/sionna/rt/path_solvers/paths.py`
- `backend/.venv/Lib/site-packages/sionna/rt/path_solvers/field_calculator.py`
- `backend/.venv/Lib/site-packages/sionna/rt/scene.py`

---

## Sources

- [1] Degli-Esposti et al., "Measurement and Modelling of Scattering From Buildings," IEEE TAP 2007 (Sionna ER citation `cite:p:Degli-Esposti07`) — DOI 10.1109/TAP.2007.897329
- [2] "Diffuse Scattering Directive Model Parameterization Method for Construction Materials at mmWave Frequencies," Int. J. Antennas Propag. 2020 — https://www.hindawi.com/journals/ijap/2020/1583854/
- [3] ITU-R P.2040 (material EM parameters) · ITU-R P.1411 (short-range outdoor) — *clause-level original text not consulted* **(unverified)**
- [4] Sionna RT scattering tutorial — https://nvlabs.github.io/sionna/rt/tutorials/Scattering.html
- [5] Sionna RT Radio Materials API — https://nvlabs.github.io/sionna/rt/api/radio_materials.html
- [6] Sionna RT Paths API (doppler, cir/taps/cfr) — https://nvlabs.github.io/sionna/rt/api/paths.html ; Radio devices — https://nvlabs.github.io/sionna/rt/api/radio_devices.html
- [7] Sionna RT Technical Report, arXiv:2504.21719 — https://arxiv.org/pdf/2504.21719
- [8] Remcom Wireless InSite — Diffuse Scattering — https://www.remcom.com/wireless-insite-em-propagation-software/diffuse-scattering
- [9] NVIDIA AODT — RAN Digital Twin (Dynamic Scatterers) — https://docs.nvidia.com/aerial/aerial-dt/text/ran_digital_twin.html
- [10] Simulate an Accurate Radio Environment using NVIDIA AODT — https://developer.nvidia.com/blog/simulate-an-accurate-radio-environment-using-nvidia-aerial-omniverse-digital-twin/
- [11] MATLAB `comm.RayTracingChannel` — https://www.mathworks.com/help/comm/ref/comm.raytracingchannel-system-object.html
- [12] Mobility Modeling with Ray Tracing Channel — https://www.mathworks.com/help/comm/ug/mobility-modeling-with-ray-tracing-channel.html

**Repository evidence (installed Sionna RT 2.0.1, repo-verified):** `backend/.venv/Lib/site-packages/sionna/rt/scene_object.py:252-283`, `radio_devices/radio_device.py:50-119`, `radio_materials/scattering_pattern.py:201-416`, `radio_materials/radio_material.py:220-263`, `path_solvers/path_solver.py:144-157`, `path_solvers/field_calculator.py:256-300,526-562`, `path_solvers/paths.py:336-524,660-,1215-1246`, `scene.py:1055,1073-1078`. **Integration points:** `backend/app/services/simulation_backends/sionna_backend.py:339,776,896,973,1024-1027`; `backend/app/services/trajectory.py:205,363,381,505`; `backend/app/schemas/scene.py:147-177`; `backend/app/schemas/simulation.py:182`.

**Unverified items:** ITU-R P.2040/P.1411 clause-level original text (cited only as a reference for material/diffuse guidance, not consulted) **(unverified)**.

---

## Implementation Complete (Design A — velocity-based per-path Doppler + time-varying CIR)

Of the plans in this document, **Design A** (effect #2: velocity-based per-path Doppler) has been implemented. Design B (scattering pattern/XPD) and C (in-frame diffuse time series) are left as follow-up work. The APIs below were all re-verified with live probes against the installed sionna-rt 2.0.1.

### Exact APIs used (re-verified)

- `Transmitter(..., velocity=[vx,vy,vz])` / `Receiver(..., velocity=...)` constructor arguments, and the `.velocity` setter (wraps in `mi.Vector3f`) — `radio_devices/radio_device.py:45-71,108-119`. Live confirmation: empty scene, pure LOS, 3.5 GHz, RX approaching TX at 30 m/s → `paths.doppler == 350.2423 Hz == v/λ = 30/0.085655`. Approaching → positive (+) Doppler.
- `SceneObject.velocity = mi.Vector3f(...)` (actor mesh) — `scene_object.py:252-283`. A single vector per object, geometry invariant.
- `Paths.doppler` — per-path Doppler [Hz], synthetic array shape `[num_rx, num_tx, num_paths]` (`paths.py:335-385`). Always populated regardless of `num_time_steps`.
- `Paths.cir(*, sampling_frequency, num_time_steps, out_type="numpy")` — time-varying CIR (`paths.py:387-524`). `a·e^{j2π f_Δ t}` is applied only when `num_time_steps>1`. Probe: `a` shape `(...,num_time_steps)` complex64, magnitude constant and phase rotating, per-step phase exactly matching `2π f_Δ/fs`.

### Implementation details (by file)

- **`backend/app/schemas/devices.py`** — `Device.velocity_m_s: Optional[Vec3] = None` (world frame m/s, Z-up). None=stationary, geometry/ray-tracing invariant.
- **`backend/app/schemas/channel.py`** — added `num_time_steps: int(1..64, default 1)` and `sampling_frequency_hz: Optional[float]` (None→Nyquist=2·max|f_Δ|, 1 kHz when there is no motion) to `ChannelAnalysisRequest`. `CirTap.doppler_hz: Optional[float]`. Added `doppler_spread_hz`, `mean_doppler_hz`, `max_doppler_hz`, `coherence_time_ms` (≈0.42/max|f_Δ|), `cir_time_s`, `cir_time_envelope_db` (the time-varying fading envelope `|Σ_i a_i e^{j2π f_Δ,i t}|` in dB) to `ChannelAnalysisResult`.
- **`backend/app/services/simulation_backends/sionna_backend.py`** — passes `velocity_m_s` through when creating Transmitter/Receiver. Sets per-actor `obj.velocity` via the `apply_actor_states(..., velocities=)` argument. Adds an optional `actor_velocities` kwarg to `simulate_paths`/`_simulate_paths_impl`. `_convert_paths` reads `solved.doppler` and returns a list aligned 1:1 with the retained paths → exposed as `PathResultSet.metadata["doppler_hz"]` only when something is moving (a static solve stays byte-identical). Since the RayPath schema is out of ownership, it is carried via metadata.
- **`backend/app/services/channel_analysis.py`** — `doppler_metrics()` (power-weighted mean/spread/max, coherence time), `doppler_time_envelope()` (backend-agnostic, synthesizes the time-varying envelope from per-path power/phase/doppler — the same model as `paths.cir`). `build_cir(paths, doppler_by_path_id)` fills the per-tap `doppler_hz`. `analyze_channel` maps `metadata["doppler_hz"]` by path_id and skips the link filter and delay sort to preserve alignment.
- **`backend/app/services/trajectory.py`** — derives UE velocity via a finite difference of waypoints `(wp[i+1]-wp[i])/dt` (backward difference for the last point) → sets `velocity_m_s` on the moving RX. Exposes the per-waypoint Doppler spread as `metadata["doppler_spread_hz"]` (a list aligned with samples).
- **`backend/app/services/scenario.py`** — adds `actor_velocity_at()` (trajectory tangent central difference = tangent × speed). Passes per-frame actor velocity + attached device velocity (inheriting the actor velocity) into the solve. Exposes the per-frame Doppler spread as `ScenarioResultSet.metadata["doppler_spread_hz"]`. (The LinkMetrics/ScenarioFrame/TrajectorySample schemas are out of ownership, so the metadata channel is used.)
- **`backend/tests/test_doppler.py`** (new) — 18 cases: the schema velocity field, the Doppler spectrum formula (hand-calculated), the time-varying envelope ripple, the service velocity plumbing (a capture-fake backend so sionna is not required), and sionna-guarded real solves (moving-RX Doppler ≈ v/λ, static links expose no doppler_hz, channel-analysis Doppler metrics populated).

### Added schema fields (for frontend type mirroring)

- `Device.velocity_m_s: [vx,vy,vz] | null` (m/s, Z-up world frame).
- `ChannelAnalysisRequest.num_time_steps: int`, `.sampling_frequency_hz: float | null`.
- `CirTap.doppler_hz: float | null`.
- `ChannelAnalysisResult`: `doppler_spread_hz`, `mean_doppler_hz`, `max_doppler_hz`, `coherence_time_ms`, `cir_time_s: float[]`, `cir_time_envelope_db: float[]` (all None/[] if there are no moving objects).
- `TrajectoryResultSet.metadata.doppler_spread_hz: (float|null)[]` (aligned with samples), `ScenarioResultSet.metadata.doppler_spread_hz: (float|null)[]` (aligned with frames), `PathResultSet.metadata.doppler_hz: float[]` (aligned with paths, only when moving).

### Test results

`tests/test_doppler.py tests/test_sionna_backend.py tests/test_channel_analysis.py tests/test_scenario.py` → all pass (57 passed). Excluding `test_render.py` (unrelated to this work; a pre-existing test-isolation problem where Mitsuba global plugin state pollution breaks subsequent sionna GPU tests), the full suite is **249 passed, 2 skipped, 0 failed**. `test_render.py` does not reference any velocity/doppler/simulate_paths/channel/trajectory/scenario code, and the same 6 cases fail even when the new test file is excluded, so it is not a regression from this change.
