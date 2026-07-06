# RT accuracy: concerns and mitigations

Communication researchers report that ray tracing (Sionna RT included) can be
5–15 dB off from measurements when uncalibrated. This doc records the main
error sources and what SEAM Studio does about them.

## Main error sources (from the literature)

1. **Material EM parameters** — the dominant, uncorrelated error. ITU-R P.2040
   presets are population averages defined only over limited bands; real
   surfaces deviate. Uncalibrated path-loss error ~5–15 dB is common.
2. **Diffuse scattering** — off by default in most RT, yet at 28 GHz diffuse
   energy can be ~20–40% of received power. Ignoring it under-predicts NLOS.
3. **Geometry fidelity** — missing furniture/vehicles/foliage and coarse meshes
   cause large local errors (tens of dB, hundreds of ns) near TX/RX.
4. **Phase/coherence** — RT phase needs sub-wavelength geometry (~1 mm at
   28 GHz); wall-position error flips interference and biases magnitude fits.
5. **Antenna model** — an isotropic assumption drops pattern gain/roll-off and
   cross-polarization.
6. **Refraction/transmission & diffraction** — through-wall paths and
   higher-order diffraction are often disabled or first-order only.
7. **Monte-Carlo variance** — too few `samples_per_src` biases diffuse paths low.

## What this tool implements now

- **Diffuse-scattering-ready materials.** `RFMaterial.scattering_coefficient`
  is set to measurement-backed values (concrete/brick ~0.2) and pushed onto the
  Sionna `RadioMaterial`; enable it per run with `SimulationConfig.scattering`
  (→ `diffuse_reflection=True`).
- **Out-of-band guardrail.** `validate_scene` emits `MATERIAL_OUT_OF_BAND` when
  an ITU ground material is used above ~10 GHz, pointing at `ground_28ghz`; the
  Sionna backend also warns at solve time (`_frequency_warnings`).
- **Measurement calibration** (`POST /calibrate/materials`,
  `services/calibration.py`). Import measured per-link path gain; the tool
  simulates the same links, computes a **level offset** (absorbs unknown
  absolute TX power) and the residual **RMSE/MAE** (the shape error a material
  fit can fix), then **grid-searches one material parameter**
  (`scattering_coefficient` / `relative_permittivity` / `conductivity_s_per_m`)
  to minimize RMSE and reports before/after. With `apply=true` the fitted value
  is written into the library and prims are promoted to
  `assignment_status: measurement_calibrated`.
- **Frequency-aware defaults** — 28 GHz default, ITU vs constant material split.

## Solver / accuracy presets

Getting the solver knobs right is as much an accuracy lever as material choice:
too few samples biases diffuse paths low, too shallow a `max_depth` drops NLOS
bounces, and the wrong mechanisms (scattering/refraction/diffraction off) can
mis-predict coverage by tens of dB. Rather than leave every knob to the user,
`SolverControls` offers **named presets** (`frontend/src/configPresets.ts`)
that bundle a coherent set of solver knobs plus the radio-map grid for a
canonical deployment:

| preset | freq | max_depth | mechanisms | grid cell |
|---|---|---|---|---|
| **28 GHz Indoor Lab** | 28 GHz | 5 | reflection + refraction + scattering | 0.25 m |
| **28 GHz Outdoor Campus** | 28 GHz | 3 | reflection + scattering | 2.0 m |
| **3.5 GHz Urban Macro** | 3.5 GHz | 4 | reflection + refraction + diffraction | 5.0 m |
| **60 GHz Indoor** | 60 GHz | 4 | reflection + refraction | 0.25 m |

Selecting a preset patches **both** the paths config and the radio-map grid; it
leaves the user's backend/TX/RX selection untouched. Presets only ever set keys
that already exist in the pinned `SimulationConfig` wire type, so they cannot
introduce drift. Hand-editing any covered knob flips the dropdown to **Custom**
(the sentinel for "no named preset matches"). The indoor presets deliberately
turn on refraction (through-wall transmission dominates indoor NLOS) and a
finer 0.25 m grid; the outdoor/urban presets trade depth and grid resolution
for area coverage. These are starting points, not calibrated ground truth —
run measurement calibration (above) to close the residual material error.

## Planned next steps

- **Differentiable (Adam) calibration.** Sionna RT is differentiable w.r.t.
  `relative_permittivity`, `conductivity`, `scattering_coefficient`,
  `xpd_coefficient`. Mark these trainable via Dr.Jit, run PathSolver, and
  Adam-minimize a power + RMS-delay-spread loss (SMAPE/NMSE) — published results
  cut path-loss error ~4.9 dB → ~1.0 dB and delay-spread error ~54% → ~13%.
  Our Mode-2 material grouping already gives per-material parameter sharing for
  free; add a train/validation split for held-out error.
- **Directive scattering patterns** — add `scattering_pattern`
  (Lambertian / Directive / Backscattering + `alpha_r`) per material; directive
  lobes match building surfaces far better than Lambertian at mmWave.
- **Realistic antenna patterns** — plumb per-device `antenna.pattern`
  (`tr38901`/`dipole`) and polarization into the solver arrays (devices already
  carry the config).
- **Validation report** — path-gain error, RMS delay spread, Rician K, and CDF
  comparison against measured CIRs, with a convergence check (double
  `samples_per_src` / second seed) to bound Monte-Carlo variance.
