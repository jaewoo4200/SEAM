# Research ideas — paper-worthy directions for SEAM Studio

This document collects concrete, publishable research directions that are
*grounded in what SEAM Studio already has*, so each one is a short
implementation hop from a working prototype rather than a greenfield project.

What the tool already ships (the substrate every idea below builds on):

- a **unified RF-visual scene graph** where every prim carries dual visual + RF
  material bindings with tracked provenance
  (`unassigned → rule_suggested → ai_suggested → user_confirmed →
  measurement_calibrated`);
- **AI/rule material suggestion** with a strict-JSON contract and provenance
  log (`services/ai_provider.py`, `ai/suggestions.jsonl`);
- a **Mitsuba/Sionna XML importer** (`services/mitsuba_import.py`) that ingests
  CV-material-split scenes and ships the bundle's `lab_room`;
- **mock + real Sionna RT backends** behind one `RayTracingBackend` protocol
  (`services/simulation_backends/`), so every method runs with no GPU;
- **grid-search measurement calibration** with recompile-per-trial correctness
  (`services/calibration.py`) — the key subtlety is that the Sionna backend
  reads materials from the on-disk projection, so every trial recompiles;
- **MIMO beamforming** (TX-MRT + both-ends SVD from the Sionna channel);
- **RFData / AODT-style export** (`services/rfdata_export.py`);
- **trajectory RF metrics** (`services/trajectory.py`);
- a full **28 GHz solver surface** (`schemas/simulation.py`) with an
  out-of-band ITU-ground guardrail and the FTC/lab-room scenes.

Reference scenes available for every experiment below:
- **`lab_room`** — measured 6.86 × 5.32 × 2.69 m indoor room, five ITU
  materials, 28 GHz, an optional 1.70 m `human_target` mesh (`human_body`
  material). Bundled and already loadable.
- **FTC outdoor** — reconstructed OSM/photogrammetry campus building with a
  SAM2/DINOv2 CV material split (concrete/glass/metal/ground), plus a
  single-concrete baseline and a 28 GHz-safe variant. Trajectory + CFR metrics
  already defined in `MATERIAL_MAPPING_EVALUATION_PLAN.md`.

Where the competitive landscape sits (used throughout for differentiation):

- **NVlabs `sionna-rt-gui`** — a Polyscope desktop viewer: load a scene,
  place/animate TX/RX, show paths and a raster radio map. No material editing,
  no provenance, no calibration UX, no uncertainty, no CV coupling.
- **NVIDIA AODT** — datacenter-scale Omniverse twin. Materials are assigned
  *per whole building / per terrain* through a USD property widget; no
  sub-object / per-face material lifecycle, no provenance states, no
  measurement-in-the-loop calibration UI, requires NVIDIA-grade infrastructure.
- **Remcom Wireless InSite / Altair WinProp+Feko / MATLAB raytracer** —
  commercial/closed, fixed material databases, no differentiable calibration,
  no open provenance model, no CV-material coupling, weak or no per-material
  interactive sensitivity tooling exposed to the user.

Recent literature this tool is well-positioned to extend (see Sources):
differentiable Sionna calibration (Learning Radio Environments; VLM-guided
diff-RT; RF inverse rendering), automated material assignment (HoRAMA),
CV/LiDAR-driven RF twins (RFDT-Channel), and uncalibrated-DT uncertainty
(GP channel-statistics prediction). None of them ship the *authoring +
provenance + closed-loop UX* that SEAM already has — that gap is the
paper hook for most ideas below.

---

## Idea 1 — Provenance-tracked, AI-assisted material lifecycle for RT digital twins (closed loop: suggest → confirm → calibrate)

**(a) Pitch.** Ray-tracing accuracy is dominated by material EM parameters
(see `docs/accuracy.md`: 5–15 dB uncalibrated error), yet every existing tool
treats material assignment as a *one-shot, untracked* action. SEAM's
provenance lifecycle
(`unassigned → rule_suggested → ai_suggested → user_confirmed →
measurement_calibrated`) makes the *entire assignment history* a first-class,
queryable object. This idea formalizes that lifecycle as a **closed-loop
authoring protocol**: rules/AI suggest a material with evidence and confidence,
the human confirms or edits, measurement calibration then promotes specific
prims to `measurement_calibrated` and writes the fitted parameter back with a
`calibration:run_id` source. The paper contribution is (i) the lifecycle model
itself, (ii) a metric suite over it — *provenance yield* (fraction of RF-active
surface area at each lifecycle stage), *confirmation cost* (human actions to
reach a target accuracy), and *accuracy-per-provenance-stage* — and (iii) an
end-to-end demonstration that closing the loop reduces path-gain error while
keeping a fully auditable trail.

**(b) Differentiation.** `sionna-rt-gui` has no material editing at all. AODT
assigns one material per building through a USD widget with no history, no
confidence, and no calibration promotion. InSite/WinProp use fixed databases
and never record *why* a face got a material or *how confident* the choice was.
Differentiable-calibration papers (Learning Radio Environments; VLM-guided
diff-RT) optimize parameters but discard the *authoring provenance* that lets a
practitioner audit, roll back, or explain a twin. No prior system exposes
assignment provenance as a lifecycle with measured accuracy at each stage.

**(c) Experiment design.**
- *Scenes*: `lab_room` (5 materials, ground truth from the measured room) and
  FTC outdoor (concrete/glass/metal/ground).
- *Protocol*: run three authoring policies to the same twin — manual-only,
  rule+AI-assisted, and rule+AI+calibration (full loop) — recording every
  provenance transition.
- *Metrics*: per-link path-gain RMSE/MAE and RMS-delay-spread error vs. the
  measured/GT reference; provenance yield curves; human confirmation-action
  count; calibration promotion count.
- *Baselines*: all-concrete default (AODT-style single material), fixed-ITU
  assignment (WinProp-style database), and the tool's own uncalibrated
  suggestions.

**(d) Implementation gap.** The pieces exist but are not wired into one loop:
add a **provenance-yield / lifecycle-report endpoint** aggregating
`assignment_status` × surface area from the scene graph; extend
`ProjectStore.append_provenance` events with a lifecycle-transition record; and
add a thin "loop orchestrator" that chains
`ai/suggest-materials → rf/batch-assign → calibrate/materials` and emits the
metric suite. No schema changes (all read-only fields already exist).

**(e) Venues.** IEEE GLOBECOM/ICC *Digital Twins over NextG* workshops
(WS-23 GLOBECOM 2026, WS-18 TwinNetApp ICC 2026); IEEE Networking Letters or
IEEE Open Journal of the ComSoc for the metric-suite version; a **demo/tool
track** for the interactive loop.

---

## Idea 2 — CV material-split → RF-fidelity evaluation pipeline (operationalizing SAM2/DINOv2)

**(a) Pitch.** The bundle already contains a SAM2 + DINOv2 material-split of the
FTC building (concrete/glass/metal/ground) and a matched single-concrete
baseline. Today the CV output is evaluated ad hoc. This idea turns it into a
**reproducible CV-to-RF fidelity benchmark**: fix the geometry, vary only the
*material-labeling method* (single-concrete → semantic prior → SAM2/DINOv2
split → hand-labeled GT), and measure the downstream RF cost of each labeling
error. Two coupled metric layers: a **CV layer** (per-class pixel IoU, boundary
F1, multi-view label consistency, mesh face coverage — already listed in
`MATERIAL_MAPPING_EVALUATION_PLAN.md`) and an **RF layer** (CFR NMSE and cosine
similarity vs. the material-mapped "GT" scene, per-trajectory path gain, RMS
delay spread, throughput proxy). The headline result answers a question the
CV community cannot: *which segmentation errors actually move RF, and by how
much?* — e.g. the tool already observes glass-vs-concrete confusion costing
~10–15 dB median path gain on FTC.

**(b) Differentiation.** HoRAMA and RFDT-Channel *produce* CV/LiDAR-driven
material assignments but evaluate them mostly at the geometry or aggregate-KPI
level; none provide an *interactive, per-class, error-attributed* CV→RF
pipeline where a user swaps the labeling method and watches CFR/NMSE respond on
a fixed twin. `sionna-rt-gui`/AODT/InSite/WinProp have no CV coupling at all —
materials come from a human or a database. The differentiator is the *unified
scene graph*: the same prim ids carry the CV label, the RF material, and the
result overlay, so error can be attributed back to a specific mislabeled face
group.

**(c) Experiment design.**
- *Scenes*: FTC outdoor (primary; multi-view photos + the five AnyLabeling seed
  masks) and `lab_room` (controlled indoor cross-check).
- *Conditions*: single-concrete baseline, SegFormer/Mask2Former semantic prior,
  SAM2+DINOv2 prototype-labeled split, hand-labeled GT (v14/v15 scenes).
- *Metrics*: CV — per-class IoU, boundary F1, multi-view consistency, coverage;
  RF — active-channel CFR NMSE (the plan already flags that outage points
  dominate naive means, so report active-channel), path gain / RMS delay spread
  vs. time, throughput proxy, frequency scaling 3.5 vs 28 GHz.
- *Baselines*: the tool's own single-concrete scene as the null; hand-labeled
  as the ceiling.

**(d) Implementation gap.** The RF metrics live in the bundle's eval scripts,
not the app. Port a **CFR/NMSE + trajectory metric evaluator** into a service
(reuse `services/trajectory.py` aggregation; add CFR via Sionna `Paths.cfr(f)`
in the Sionna backend), and add a **scene-variant A/B harness** that runs the
same TX/RX/trajectory across two compiled projections and diffs the metrics.
The CV inference stays external (out of scope for this environment), but the
`POST /rf/batch-assign` segment→material contract is the integration point, so
the pipeline can be driven end-to-end from label maps.

**(e) Venues.** KICS (already the user's target), IEEE ICC/GLOBECOM main or
*AI-native RAN* workshop; IEEE Communications Letters for the compact
error-attribution result; a CV-adjacent venue (WACV/CVPR workshop on 3D/vision
for wireless) if the segmentation contribution is strengthened.

---

## Idea 3 — Interactive per-material sensitivity analysis (grid sweeps as heatmaps / tornado charts)

**(a) Pitch.** The calibration service already sweeps one material parameter
over a grid and re-simulates; the FTC plan already runs conductivity sweeps.
This idea generalizes that into an **interactive material-sensitivity module**:
for a chosen KPI (path gain, RMS delay spread, CFR NMSE, coverage) sweep each
material's `relative_permittivity / conductivity / scattering_coefficient /
thickness / xpd_coefficient` and render the result as (i) **sensitivity
heatmaps** (material × parameter × ΔKPI) and (ii) **tornado charts** ranking
which material parameters most move the KPI at this geometry/frequency. This
directly operationalizes the calibration code's own "no-sensitivity" guard
(it already detects when a swept parameter does not move the prediction) into a
*positive* analysis product: it tells the user *which materials are worth
measuring* before they spend effort on drive tests. It is the practical
front-end to differentiable calibration — global structure first, gradients
second.

**(b) Differentiation.** No competitor exposes interactive per-material
sensitivity. InSite/WinProp require manual re-runs; AODT/`sionna-rt-gui` have no
sweep tooling. Differentiable-calibration papers give *local* gradients but not
the *global, user-facing* sensitivity map that guides where to place
measurements — and they need GPU + Sionna, whereas this runs on the mock
backend for the UX and Sionna for the numbers.

**(c) Experiment design.**
- *Scenes*: `lab_room` (5 materials) and FTC (4 materials).
- *Design*: one-at-a-time (OAT) grid sweeps per material parameter; optionally a
  small Sobol/Morris screening for interaction effects.
- *Metrics*: ΔKPI range per (material, parameter); rank correlation between the
  cheap OAT tornado ranking and the eventual differentiable-calibration
  parameter movement (does sensitivity predict what calibration fits?).
- *Baselines*: uniform-perturbation sensitivity; random parameter ablation.

**(d) Implementation gap.** Factor the grid-sweep loop out of
`services/calibration.py` into a reusable `sensitivity` service that returns a
`(material, param, grid, kpi_values)` tensor for arbitrary KPIs (not just RMSE
vs. measurements). Add a `POST /analyze/sensitivity` endpoint and a frontend
heatmap/tornado view. Recompile-per-trial correctness is already solved in the
calibration code and must be reused verbatim.

**(e) Venues.** IEEE ICC/GLOBECOM workshop or **demo track** (the interactivity
is the story); IEEE Antennas & Wireless Propagation Letters (AWPL) if framed as
material-parameter sensitivity of mmWave RT.

---

## Idea 4 — Monte-Carlo uncertainty / convergence overlays for radio maps (seed/sample ensembles)

**(a) Pitch.** Radio maps are reported as single deterministic rasters, but they
are Monte-Carlo estimates: the `RadioMapResultSet.values` field already allows
`None` holes, and `SimulationConfig` already exposes `seed` and `num_samples`.
This idea adds a **radio-map uncertainty layer**: run an *ensemble* over seeds
(estimator variance) and over `num_samples` (convergence), then overlay
per-cell mean, standard deviation, and a **confidence/convergence map** on the
same jet raster. It surfaces the `docs/accuracy.md` concern that too few
`samples_per_src` biases diffuse paths low, and gives the user a *stopping
rule*: refine only cells whose std exceeds a threshold. Naturally couples to the
progressive-simulation roadmap (Milestone 10): refine high-variance cells first.

**(b) Differentiation.** `sionna-rt-gui` and AODT show a single raster with no
variance. Remcom has a Monte-Carlo *object-position* uncertainty example, but
it is offline, closed, and not an interactive per-cell convergence overlay tied
to the solver's seed/sample knobs. The GP-uncertainty paper predicts channel
*statistics* uncertainty from few measurements — complementary but different:
this idea quantifies the *estimator/solver* uncertainty of the twin itself,
which nobody exposes interactively.

**(c) Experiment design.**
- *Scenes*: `lab_room` and FTC outdoor.
- *Design*: N-seed ensemble at fixed `num_samples`; sample-budget ladder
  (e.g. 1e5 → 1e6 → 1e7) for convergence.
- *Metrics*: per-cell std vs. sample budget (convergence rate), coverage-outage
  probability with confidence bounds, and a *variance-guided refinement* cost
  vs. uniform refinement (time-to-target-uncertainty).
- *Baselines*: single-seed single-budget map (status quo); uniform refinement.

**(d) Implementation gap.** Add an **ensemble runner** that calls
`simulate_radio_map` across seeds/budgets and reduces to mean/std/count grids
(a new `RadioMapEnsembleResultSet`, mirroring the existing `None`-hole
convention). Extend the frontend overlay with a std/confidence colormap toggle.
The mock backend gets deterministic per-seed jitter so the UI is buildable
without a GPU.

**(e) Venues.** IEEE GLOBECOM/ICC DT workshops; IEEE Wireless Communications
Letters for the convergence-stopping-rule result; **demo track** for the
interactive variance overlay.

---

## Idea 5 — Differentiable calibration UX (Adam over Dr.Jit params) as a tool paper

**(a) Pitch.** `docs/accuracy.md` and `docs/roadmap.md` already name this as the
next calibration step, with published targets (path-loss error ~4.9 → ~1.0 dB,
delay-spread error ~54% → ~13%). The research contribution here is *not* the
algorithm (Sionna is already differentiable) but a **calibration UX and
protocol**: mark material params trainable via Dr.Jit, run the PathSolver,
Adam-minimize a power + RMS-delay-spread loss (SMAPE/NMSE), and — crucially —
expose it as a *governed authoring action* that promotes prims to
`measurement_calibrated`, writes fitted values back with provenance, and holds
out a validation split for honest held-out error. Mode-2 material grouping
already gives per-material parameter sharing for free. The tool-paper hook: a
reproducible, provenance-aware, held-out-validated differentiable-calibration
workflow that a non-expert can drive, initialized by the grid search (Idea 3)
and the AI suggestions (Idea 1) to escape the local minima and bad
initialization that the VLM-guided diff-RT paper flags as the core failure mode.

**(b) Differentiation.** The Learning-Radio-Environments and VLM-guided diff-RT
papers deliver the *math*; none deliver a *tool* with authoring provenance, a
train/validation split enforced in the UI, and a fallback to robust grid search
when gradients stall. AODT/InSite/WinProp are non-differentiable. `sionna-rt-gui`
has no calibration. This is the first differentiable-calibration workflow
embedded in a provenance-tracked scene-authoring tool with a mock-backend
fallback for everything except the gradient step.

**(c) Experiment design.**
- *Scenes*: `lab_room` (measured indoor; strongest ground truth) and FTC (
  synthetic-GT material-map as the reference, per the KICS plan).
- *Design*: initialize from (a) ITU defaults, (b) grid-search optimum (Idea 3),
  (c) AI-suggested prior; Adam over
  {`relative_permittivity`, `conductivity`, `scattering_coefficient`,
  `xpd_coefficient`} with a held-out link split.
- *Metrics*: held-out path-gain RMSE, RMS-delay-spread error, CFR NMSE, and
  convergence iterations to target; ablate initialization source.
- *Baselines*: grid-search calibration (the tool's current method), uncalibrated
  ITU, and random-init Adam (to show the initialization benefit).

**(d) Implementation gap.** Add a `DifferentiableCalibrationBackend` path inside
the Sionna backend that marks `mi.traverse` params trainable, runs Adam over the
grouped materials, and returns a `CalibrationReport`-compatible result (extend
the report schema is *not* needed if it maps onto the existing before/after
fields — report a contract gap if a `fitted_values` dict per material is
required). Add train/validation split handling in the calibration request. Keep
grid search as the non-GPU fallback.

**(e) Venues.** A dedicated **tool/demo paper** (ICC/GLOBECOM demo, or a
journal tool paper); IEEE Transactions on Antennas and Propagation (TAP)
communication or IEEE TWC letter if the held-out-accuracy result is strong on
the measured `lab_room`.

---

## Idea 6 — A/B scenario-diff radio maps (governed twin comparison)

**(a) Pitch.** The RF compiler is deterministic and byte-identical on unchanged
input, and results are immutable per-run files. That makes **scenario diffing**
cheap and exact: compile two variants of the same twin (e.g. windows =
`itu_glass` vs `metal`; TX at position A vs B; diffraction on vs off) and render
a **per-cell ΔKPI radio map** plus a summary (mean/percentile Δ, area improved).
This is the natural product of the unified scene graph + immutable results and
is the everyday question of network planning ("what changes if I…?").

**(b) Differentiation.** InSite/WinProp require manual re-runs and external
diffing; AODT/`sionna-rt-gui` have no built-in scenario-diff. The determinism
guarantee (no timestamps, sorted groups) makes the diff *reproducible and
attributable to the single changed field* — a property the closed tools do not
advertise.

**(c) Experiment design.** `lab_room` and FTC; sweep one design variable at a
time; metrics = ΔRSS/Δpath-gain per cell, fraction of area improved, delay-
spread change; validate that an unchanged field yields a byte-identical diff
(regression test of determinism).

**(d) Implementation gap.** A `POST /analyze/scenario-diff` endpoint taking two
config/scene variants, reusing the ensemble/trajectory plumbing to produce a
diff grid; frontend diverging colormap. Small and self-contained.

**(e) Venues.** Demo track; IEEE Networking Letters; strong supporting result
inside Ideas 1/2 papers.

---

## Idea 7 — Mesh radio maps on facades/floors (surface-attached coverage)

**(a) Pitch.** Roadmap Milestone 9. Today radio maps are planar grids at a fixed
height; real coverage lives on *surfaces* (facades, floors, terrain). Attach
per-face coverage to the actual meshes (the compiler already extracts per-
material geometry and the demo scene tags prims `road`/`building`/`terrain`),
and compare **planar vs mesh** radio maps for the same twin. Research angle:
quantify how much a planar-height assumption misrepresents facade/vertical
coverage at 28 GHz, where it matters for fixed-wireless and RIS placement.

**(b) Differentiation.** `sionna-rt-gui` is explicitly planar-raster only; AODT's
coverage is not surface-attached in an editable per-face scene graph; InSite has
surface studies but closed and not tied to a provenance-tracked material split.
The mesh radio map inherits the tool's per-prim material provenance, so a facade
heatmap is attributable to specific labeled faces.

**(c) Experiment design.** FTC facade + `lab_room` walls; metric = coverage
error vs. a dense planar sampling of the same surface; case study on facade
material (glass vs concrete) driving vertical coverage.

**(d) Implementation gap.** New `MeshRadioMapResultSet` schema (prim-keyed
per-face values, `None` holes), extend `ResultSetRef.kind`, generate
measurement surfaces from tagged prims via the compiler's mesh path, mock-
backend surface-distance falloff first, Sionna mesh radio-map solver when
available; frontend vertex-color overlay. Report the `ResultSetRef.kind`
`Literal` extension as a contract touch-point (schema is read-only here).

**(e) Venues.** IEEE AWPL / TAP letter (the physical facade-coverage result);
GLOBECOM/ICC workshop.

---

## Idea 8 — LLM-driven scenario authoring + human-target ISAC integration (two feasible add-ons)

**8A — LLM/VLM scenario authoring.** The AI provider abstraction and strict-JSON
contract already exist for material suggestion; the same pattern extends to
**natural-language scenario authoring**: "place a TX on the north facade at 6 m,
add a UE trajectory along the corridor, set windows to glass, run at 28 GHz."
The LLM emits a validated action list against existing endpoints
(`rf/batch-assign`, `simulate/trajectory`, `scene` PUT); nothing auto-applies
without the same confirm step material suggestions use. Research angle:
grounded, schema-constrained scenario generation with a provenance trail and a
guardrail against invalid RF setups (the validator already flags out-of-band
materials, duplicate ids, missing thickness).

*Differentiation*: no RT tool offers NL scenario authoring; the novelty is
*safety* — the strict-JSON + validator + provenance sandwich prevents the LLM
from silently corrupting a twin.

*Gap*: an `ai/author-scenario` provider returning a typed action list; reuse the
validator as the guard; a confirm-diff UI.

**8B — Human-target ISAC integration.** The `human_body` material (literature-
backed 28 GHz skin presets, `docs/human_material_literature.md`) and the
`human_target` mesh are in the library and bundle; the roadmap scopes the
PADP → MPC → DBSCAN → Kalman tracking pipeline. Research angle: an **RT-driven
ISAC sensing benchmark** where the human material and scattering coefficient are
*calibrated* (Ideas 1/3/5) and tracking accuracy is reported as a function of
that calibration — closing the loop between material provenance and sensing KPI
(RMSE, 80th-percentile error), reproducing the ICC-workshop testbed numbers
(9.0 cm / 25.3 cm) in a fully open, reproducible twin.

*Differentiation*: the ICC testbed is hardware; `sionna-rt-gui`/AODT don't ship a
human material or a tracking pipeline. The contribution is *material-calibrated,
reproducible* synthetic ISAC with an auditable material trail.

*Gap*: a `TrackingResultSet` schema, a target-motion + human-scatterer model,
and the DSP pipeline ported from `rt_isac_paper_pipeline.py` (DSP-heavy; the RF
and material sides are ready).

**Venues.** 8A: demo/tool track, or a workshop on LLMs-for-networking. 8B: ICC/
GLOBECOM ISAC workshop, IEEE TWC/JSAC ISAC special issue.

---

## Prioritized "implement next" shortlist (effort vs. paper value)

Ranked for the best paper-value-per-unit-effort given what already exists:

| Rank | Idea | Effort | Paper value | Why now |
|---|---|---|---|---|
| 1 | **Idea 2 — CV→RF fidelity pipeline** | Low–Med | High | RF metrics already exist in bundle scripts; FTC SAM2/DINOv2 split is done; batch-assign contract is the driver. Directly serves the user's KICS target. Port CFR/trajectory eval + A/B harness. |
| 2 | **Idea 3 — Interactive material sensitivity** | Low | Med–High | Pure refactor of the calibration grid loop + one endpoint + heatmap. De-risks Idea 5 and is a clean demo. |
| 3 | **Idea 1 — Provenance lifecycle closed loop** | Low–Med | High | No new schema; wires existing suggest→assign→calibrate into one governed loop + a lifecycle-report endpoint. Strong, defensible differentiation vs. AODT/InSite. |
| 4 | **Idea 6 — A/B scenario diff** | Low | Med | Determinism guarantee makes it nearly free; strong supporting result for Ideas 1/2 and a good demo. |
| 5 | **Idea 4 — Monte-Carlo uncertainty overlays** | Med | Med–High | Reuses seed/num_samples; mock-backend jitter makes UI buildable; couples to progressive-sim roadmap. |
| 6 | **Idea 5 — Differentiable calibration UX** | Med–High | High | Highest ceiling (measured `lab_room` held-out accuracy) but GPU + Dr.Jit plumbing; best *after* Ideas 1/3 provide initialization. |
| 7 | **Idea 7 — Mesh radio maps** | Med | Med | Schema + compiler + overlay work; physically interesting facade result but heavier. |
| 8 | **Idea 8A/8B — LLM authoring / ISAC** | Med (8A) / High (8B) | Med / High | 8A is a self-contained demo; 8B is the most novel but DSP-heavy — sequence it last, after material calibration lands. |

Suggested first paper: **Idea 2 + Idea 3 + Idea 1** bundled as
"*A provenance-tracked, CV-assisted material authoring and sensitivity workflow
for reproducible RF digital twins*", with **Idea 5** as the follow-on
differentiable-calibration tool paper on the measured `lab_room`.

---

## Sources

- NVlabs Sionna RT — https://github.com/NVlabs/sionna-rt
- Sionna RT: Differentiable Ray Tracing for Radio Propagation Modeling —
  https://arxiv.org/pdf/2303.11103 ; https://github.com/NVlabs/diff-rt
- Learning Radio Environments by Differentiable Ray Tracing —
  https://arxiv.org/pdf/2311.18558
- Vision-Language-Model-Guided Differentiable Ray Tracing for Fast and Accurate
  Multi-Material RF Parameter Estimation — https://arxiv.org/abs/2601.18242
- Physically Accurate Differentiable Inverse Rendering for RF Digital Twin —
  https://arxiv.org/pdf/2603.18026
- HoRAMA: Holistic Reconstruction with Automated Material Assignment for Ray
  Tracing using NYURay — https://arxiv.org/pdf/2602.12942
- RFDT-Channel: RGB-LiDAR-Based RF Digital Twin Scene Construction for 28 GHz
  Indoor Ray-Tracing Channel Simulation — https://arxiv.org/pdf/2606.01261
- Prediction of Wireless Channel Statistics with Ray Tracing and Uncalibrated
  Digital Twin (GP uncertainty) — https://arxiv.org/abs/2411.13360
- Digital Twin-Assisted Measurement Design and Channel Statistics Prediction —
  https://arxiv.org/pdf/2603.23787
- Deterministic Modeling of Dynamic ISAC Channels in RF Digital Twin
  Environments — https://arxiv.org/pdf/2603.28736
- Quantifying System-Level KPI Deviations of Sionna RT: Material and Near-Field
  Error Analysis (5G OAI testbed) — https://arxiv.org/html/2605.10352
- Toward Real-Time Digital Twins of EM Environments: Benchmark of Ray Launching
  Software — https://arxiv.org/pdf/2406.05042
- Upsampling DINOv2 Features for Weakly Supervised Materials Segmentation —
  https://arxiv.org/html/2410.19836v2
- Modeling Uncertainty in Urban Propagation Using Monte Carlo Theory (Remcom) —
  https://www.remcom.com/resources/examples/modeling-uncertainty-in-urban-propagation-using-monte-carlo-theory
- NVIDIA Aerial Omniverse Digital Twin — material assignment (GUI) —
  https://docs.nvidia.com/aerial/aerial-dt/text/gui.html ;
  https://developer.nvidia.com/aerial-omniverse-digital-twin
- Altair Feko/WinProp release notes —
  https://help.altair.com/winprop/topics/feko/release_notes/2025/release_notes_intro_feko_winprop_r.htm
- IEEE ICC 2026 workshops (TwinNetApp) —
  https://icc2026.ieee-icc.org/program/workshops
- IEEE GLOBECOM 2026 workshops (Digital Twins over NextG) —
  https://globecom2026.ieee-globecom.org/workshops

---

## 한국어 요약 (Korean summary)

SEAM Studio가 **이미 갖고 있는 기능**(프로버넌스 추적 재질 라이프사이클,
AI/규칙 재질 제안, CV 재질 분할 씬 임포트, mock+실제 Sionna 백엔드, 그리드
서치 측정 보정, 빔포밍, RFData/AODT 내보내기, 궤적 지표, 28 GHz 가드레일)을
토대로 **논문화 가능한 8가지 연구 방향**을 정리했습니다. 각 아이디어마다
(a) 한 문단 제안, (b) sionna-rt-gui / AODT / WinProp / InSite 대비 차별점,
(c) 실험 설계(씬: `lab_room` 실내 + FTC 실외, 지표, 베이스라인),
(d) 이 저장소에서 추가로 만들어야 할 부분, (e) 투고처(IEEE ICC/GLOBECOM DT
워크샵, TWC/TAP letter, 데모 트랙)를 포함했습니다.

핵심 8가지:
1. **프로버넌스 기반 재질 라이프사이클 폐루프** — 제안→확정→보정을 하나의
   감사 가능한 루프로. 경쟁 도구에는 재질 이력/신뢰도/보정 승격 개념이 없음.
2. **CV 재질 분할 → RF 충실도 평가 파이프라인** — 사용자의 SAM2/DINOv2 작업을
   재현 가능한 벤치마크로. 어떤 세그멘테이션 오류가 실제로 RF(CFR/NMSE, 경로
   이득)에 영향을 주는지 정량화. **가장 먼저 구현 권장.**
3. **인터랙티브 재질 민감도 분석** — 파라미터 그리드 스윕을 히트맵/토네이도
   차트로. 어느 재질을 실측해야 하는지 알려줌. 노력 대비 가치 높음.
4. **라디오맵 몬테카를로 불확실성 오버레이** — seed/샘플 앙상블로 셀별 표준편차
   /수렴 맵. 정제 중단 규칙 제공.
5. **미분 가능 보정 UX (Dr.Jit + Adam)** — 알고리즘이 아니라 프로버넌스 인지
   +홀드아웃 검증 워크플로가 기여점. 측정된 `lab_room`에서 최고 정확도 기대.
6. **A/B 시나리오 차이 라디오맵** — 결정론적 컴파일 덕분에 거의 무료.
7. **파사드/바닥 메쉬 라디오맵** — 표면 부착 커버리지, 28 GHz 수직 커버리지.
8. **LLM 시나리오 저작 + 인간 타깃 ISAC 통합** — 8A는 데모, 8B는 가장 참신하나
   DSP 부담으로 마지막 순서.

**우선순위**: (1) CV→RF 평가 파이프라인 → (2) 재질 민감도 → (3) 프로버넌스
폐루프 → (6) A/B 차이 → (4) 불확실성 → (5) 미분 보정 순으로 권장.
첫 논문은 아이디어 2+3+1을 묶어 "재현 가능한 RF 디지털 트윈을 위한 프로버넌스
추적·CV 보조 재질 저작 및 민감도 워크플로"로, 후속으로 측정된 `lab_room`에서
미분 보정 도구 논문(아이디어 5)을 제안합니다. KICS를 포함해 IEEE ICC/GLOBECOM
디지털 트윈 워크샵과 데모 트랙이 우선 투고처입니다.
