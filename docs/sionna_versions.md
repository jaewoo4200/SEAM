# History of Sionna Features, Materials, and Modeling by Version (Verified Literature)

> **English** · [한국어](sionna_versions.ko.md)

This document is a verified reference summarizing the evolution of features, materials, and physical modeling across versions of Sionna and Sionna RT, intended for wireless communication researchers. The evidence is limited to official sources (NVIDIA/NVlabs GitHub release notes and Discussions, official documentation, PyPI metadata, the `pyproject.toml` of each tag, and original arXiv papers and technical reports), and every claim has gone through an adversarial fact-checking procedure that re-fetches primary sources. Claims judged WRONG during verification were adopted only in their corrected form, and items that could not be confirmed against official sources are included in the body but explicitly marked "(unverified)". Dates use absolute dates (YYYY-MM-DD), and release dates are based, as a rule, on the PyPI upload time.

## Version Summary Matrix

| Version (representative) | Release | RT foundation | Major feature additions | Material model | Caveats |
|---|---|---|---|---|---|
| 0.14.0 | 2023-03-20 | TensorFlow + Mitsuba 3 | First RT introduction: paths, coverage map, CIR, LoS + specular reflection (Fresnel) [1][2][3] | ITU-R P.2040-2, εᵣ=a·f^b / σ=c·f^d, μᵣ=1 [3] | No diffraction, scattering, RIS, or mobility at introduction [1] |
| 0.15.0 | 2023-07-11 | TensorFlow + Mitsuba 3 | First-order diffraction (UTD), diffuse scattering (Lambertian/Directive/Backscattering) [4] | Scattering coefficient S, XPD Kₓ added (P.2040-2) [4][8] | Breaking changes: coverage map redefined, Paths returns coefficients [4] |
| 0.17.0 | 2024-04-25 | TensorFlow + Mitsuba 3 | Mobility/Doppler (velocity·apply_doppler), post-load position/orientation editing [5] | No change | Only mobility added, no breaking changes [5] |
| 0.18.0 | 2024-06-11 | TensorFlow + Mitsuba 3 | First RIS support (exact paths + coverage map) [6] | No change | Increased memory usage (fixed in 0.19) [6] |
| 0.19.2 | 2025-02-25 | TensorFlow + Mitsuba 3 | (0.19.0) SINR/RSS maps, cell-TX association, CDF, bandwidth·temperature·power (W/dBm) [7] | ITU-R P.2040 (era transition) | Last RT release of the TF era. Pin here if you need diffraction/RIS [7][9] |
| 1.0.0 (sionna-rt) | 2025-03-18 | Dr.Jit + Mitsuba 3 | Full rewrite, separate packaging, PathSolver/RadioMapSolver, refraction/transmission, post-load editing, NumPy/TF/PyTorch/JAX interoperability [9][10] | ITU-R P.2040-3, BSDF-based [11] | Diffraction·RIS removed ("future release"); use 0.19 if needed [9][10] |
| 1.1.0 (sionna-rt) | 2025-06-05 | Dr.Jit + Mitsuba 3 | Arbitrary mesh-based radio maps, object cloning, mesh conversion utilities [10][12] | Fixed material differentiability NaN gradient [10] | Retains Mitsuba 3.6.2/Dr.Jit 1.0.3 [13] |
| 1.2.0 (sionna-rt) | 2025-09-19 | Dr.Jit + Mitsuba 3 | First-order diffraction reintroduced (edge·lit-region flags), path solver improvements [10][12] | No change | Python 3.8/3.9 removed; deps=Mitsuba 3.7.1/Dr.Jit 1.2.0 [13] |
| 1.2.2 (sionna-rt) | 2026-03-19 | Dr.Jit + Mitsuba 3 | Mitsuba 3.8.0/Dr.Jit 1.3.1, ARM-Linux pip, radio map memory reduction [10][13] | Material differentiability improvements [10] | (Same-day 2.0.0 omits these changes) [10] |
| 2.0.1 (sionna-rt) | 2026-03-31 | Dr.Jit + Mitsuba 3 | Restores 2.0.0 regression, reverts to Mitsuba 3.8.0/Dr.Jit 1.3.1. PHY/SYS migrated to PyTorch (2.0.0) [10][13][14] | ITU-R P.2040-3 [11] | RT itself has no feature changes (version sync); RIS still unsupported [10][15] |

## Detailed Breakdown by Era

### TensorFlow era: 0.14 – 0.19 (2023-03 ~ 2025-02)
During this period RT was a TensorFlow extension inside the monolithic `sionna` package, built on top of the Mitsuba 3 renderer with triangle meshes, and differentiability came from TensorFlow automatic differentiation. [3]

- **0.14.0 — 2023-03-20**: First introduction of Sionna RT. "The world's first fully differentiable ray tracer for wireless propagation modeling." Features at release: differentiable propagation path computation (`compute_paths`), differentiable **coverage map (present from day one)**, path→CIR layer, transceivers at arbitrary positions/orientations, arbitrary antenna arrays/patterns, frequency-dependent custom radio materials + ITU materials, and a 3D viewer. At introduction it supported only LoS + specular reflection (Fresnel), with no diffraction, scattering, RIS, or mobility. Dockerfile default TF 2.11. [1][2][3]
- **0.15.0 — 2023-07-11**: Added **first-order diffraction** (transmitter→wedge→receiver) and **diffuse scattering** (Lambertian/Directive/Backscattering). Introduced scattering coefficient to materials. Replaced initial ray direction sampling with a Fibonacci lattice. 3GPP TR38901 polarization Model-1 support. Breaking changes: coverage map redefined, `Paths` returns coefficients instead of transition matrices, `Paths.cir()` added. [4]
- **0.16.0 — 2023-11-28**: Separated path tracing from EM field computation (`trace_paths()` + `compute_fields()`, executable in graph mode). Callable objects for customizing materials/scattering patterns. Multi-GPU for `sim_ber()` (`tf.distribute`, PHY side). Requirements: TF 2.10–2.13, Python 3.9–3.11. Breaking changes: delay normalization range changed, `trainable_*` flags removed from `RadioMaterial`/`Transmitter`/`Receiver`. [4] (0.16.2: coverage map changed to sum energy over the entire antenna)
- **0.17.0 — 2024-04-25**: Assign a **velocity vector** to each scene object → per-path Doppler shift. Time evolution of the CIR via `Paths.apply_doppler()`. `position`/`orientation` editable after load (mobility). No breaking changes. [5]
- **0.18.0 — 2024-06-11**: **First support for RIS (Reconfigurable Intelligent Surface)** — can compute both exact paths and coverage map. Physics-based re-radiation model (Degli-Esposti 2022 / Vitucci 2024). [6]
- **0.19.0 — 2024-09-30**: Added **SINR·RSS** maps to `CoverageMap` (besides path gain), cell-TX association and CDF visualization. `bandwidth`·`temperature` (thermal noise) on `Scene`, transmit power (W·dBm) on `Transmitter`. Multiple shoot-and-bounce runs, DFT beam grid, reproducibility seed control. Breaking change: `CoverageMap.as_tensor()` → `path_gain`/`rss`/`sinr` properties. [7]
- **0.19.1 — 2024-11-29**: Tightened Mitsuba requirement to ≥3.2.0 & <3.6.0, removed `Scene.mi2sionna_shift_obj_id`. [7]
- **0.19.2 — 2025-02-25**: Fixed a RIS-related issue in the coverage map solver. This is the **last RT release of the TF era** and the terminal version for users who need diffraction/RIS. [7][9]

### The 1.0 transition (2025-03)
With the "Announcing Sionna 1.0" announcement on 2025-03-18, the monolithic package was reorganized into three modules. [9]

- **Package separation**: **Sionna PHY** (non-RT physical layer; imports changed from `sionna.channel...`→`sionna.phy.channel...`), **Sionna SYS** (new system level: PHY abstraction, link adaptation, power control, scheduling), **Sionna RT** (standalone repository `sionna-rt` + PyPI `sionna-rt`). Distribution: `pip install sionna` (everything) / `sionna-rt` (RT only) / `sionna-no-rt` (RT excluded). [9]
- **On frameworks (avoid confusion)**: RT left TensorFlow and was "rewritten from scratch with Dr.Jit + Mitsuba 3", interoperating with NumPy/TF/PyTorch/JAX (RT itself is not a TF/PyTorch program but a Dr.Jit/Mitsuba wrapper). By contrast, **PHY/SYS at the 1.0 point were still TensorFlow-based**, with the old Keras `Layer` replaced by the framework-agnostic Sionna `Block` architecture. [9][14]
- **New API (breaking)**: `scene.compute_paths()`/`scene.coverage_map()` removed → replaced by the **`PathSolver`/`RadioMapSolver`** classes. Post-load scene editing, and first-class **refraction/transmission** support in addition to specular + diffuse reflection. [9][14]
- **Note on version tags**: the `sionna-rt` repository has a 1.0.0 tag (2025-03-18), but the upper `sionna` umbrella package jumped from 0.19.2→1.0.1, so **the `sionna` repository has no v1.0.0 tag**. [9][14]
- **Feature regression**: the 1.0 rewrite **removed diffraction and RIS**. Release notes: "Diffraction and RIS will be added in a future release. Users who need these features should use the latest 0.19 release." [9][10]

### sionna-rt 1.x – 2.x (current)
Based on each tag's `pyproject.toml` and release notes in the standalone `sionna-rt` line. [10][13]

- **1.0.2 — 2025-04-03**: Fixed an AoA computation issue and a path-order inconsistency between returned tensors. [10]
- **1.1.0 — 2025-06-05**: **Arbitrary mesh radio maps** (including arbitrary terrain), scene object cloning, mesh load/conversion utilities. Fixed NaN gradients in radio map differentiation and a custom material load failure (#879). Deps unchanged (Mitsuba 3.6.2/Dr.Jit 1.0.3). [10][12][13]
- **1.2.0 — 2025-09-19**: **★ First-order diffraction reintroduced** — for both path computation and radio maps. Flags to independently toggle edge diffraction and lit-region diffraction. The path solver finds more paths with less memory. Coordinate readout and clipping slider in the viewer, custom colormaps for radio maps. **deps: Mitsuba 3.7.1, Dr.Jit 1.2.0** (the release prose said "Dr.Jit 1.1", but the pyproject pin is authoritative — 1.2.0 is correct). Python 3.8/3.9 removed (`requires-python`→`>=3.10`). [10][12][13]
- **1.2.1 — 2025-10-16**: No RT feature changes (version-synced with upper `sionna` 1.2.1). (The `published_at 2026-02-26` on the GitHub release page is a backfill artifact; the actual PyPI release is 2025-10-16.) [10][13]
- **1.2.2 — 2026-03-19**: **★ Mitsuba 3.8.0 + Dr.Jit 1.3.1**, ARM-Linux pip installation support (DGX Spark). Fixed RX antenna pattern miscomputation, a `PathBuffer.path_counter` bug, radio map memory reduction, material differentiability improvements, XML material color preservation. [10][13]
- **2.0.0 — 2026-03-19**: RT is "for version-tracking purposes, no feature changes". The 2.0 major bump stems from the upper `sionna` **PHY/SYS migration to PyTorch** (removing the TensorFlow dependency). **★ Bad release**: the authors explicitly state on the release page that "this release accidentally omitted the v1.2.2 changes. Use v2.0.1." Deps regressed to Mitsuba 3.7.1/Dr.Jit 1.2.0. **Do not use.** [10][14]
- **2.0.1 — 2026-03-31** (current latest): Restores all the 1.2.2 changes that 2.0.0 omitted (including the **Mitsuba 3.8.0/Dr.Jit 1.3.1** pins). Explicit error when a material `<bsdf>` name is missing, fixed colorbar cmap reflection in `Scene.render()`. **This is why a correct installation shows up as sionna-rt 2.0.1 + Mitsuba 3.8.0 + Dr.Jit 1.3.1.** Runtime deps (2.0.1 pyproject): `mitsuba==3.8.0`, `drjit==1.3.1`, `matplotlib>=3.10`, `scipy>=1.14.1`, `numpy>=1.26`, `ipywidgets>=8.1.5`, `pythreejs>=2.4.2`; `requires-python>=3.10`. [10][13]

## Material and Physical Modeling Details

### ITU-R P.2040 revisions (the key difference for reproducibility)
- **Revision transition**: the TF-era original paper (2023, 0.14–0.19) cites **ITU-R P.2040-2** (2021-09), while the current documentation (1.x/2.x) cites **ITU-R P.2040-3** (2023-09). Even for a material of the same name, εᵣ/σ may differ slightly depending on the -2 vs -3 coefficients, so papers should state both the Sionna version and the P.2040 revision. [3][11]
- **Exact transition version (unverified)**: the exact 0.x minor at which the -2→-3 transition occurred could not be pinpointed from official sources. Only the two endpoints are confirmed (original paper=-2, current docs=-3), bounding it to the 0.19→1.0 transition period, but until an archived 0.19.2 documentation snapshot is checked it remains unverified at the exact minor granularity.
- **Parameterization**: εᵣ = a·f_GHz^b, σ = c·f_GHz^d (S/m), complex relative permittivity η = εᵣ − j·σ/(ε₀·ω). Automatically updated when `scene.frequency` changes. Supports **only non-magnetic materials (μᵣ=1)**. [3][11]
- **Built-in material list**: concrete, brick, plasterboard, wood, glass, ceiling_board, chipboard, plywood, marble, floorboard, metal, very_dry_ground, medium_dry_ground, wet_ground, vacuum. Only glass and ceiling_board have a separate high-band (220–450 GHz) coefficient set. **Ground materials (very_dry/medium_dry/wet_ground) are limited to 1–10 GHz** — for mmWave outdoor work, outside this band exceeds the P.2040 valid range. [11]
- **Coefficient values (partially unverified)**: representative a, b, c, d values (e.g., concrete a=5.24/c=0.0462/d=0.7822; metal c=10⁷; glass 6.31/0.0036/1.3394 and high-band 5.79/0.0004/1.658) were confirmed from documentation summaries, but the individual numbers/units are unverified until the live table is directly rendered, so cross-checking before publication is recommended.
- **Class hierarchy**: `RadioMaterialBase` (abstract) → `RadioMaterial` (εᵣ, σ, thickness d, scattering coefficient S, XPD Kₓ, scattering pattern) → `ITURadioMaterial` (frequency-parameterized ITU model). Materials are treated as Mitsuba BSDFs, registered in the scene XML in the form `<bsdf type="itu-radio-material">`, with custom registration via `mi.register_bsdf(...)`. [11]

### Scattering (Lambertian / Directive / Backscattering)
- All three diffuse scattering patterns are based on Degli-Esposti et al. (2007): Lambertian (f_s=cos(θ_s)/π), Directive (concentrated around the specular direction, exponent α_R), Backscattering (directive lobe + backscatter lobe, α_I·Λ). [8]
- Scattering coefficient S∈[0,1]: diffuse scattering energy fraction = S², specular reduced by R²=1−S². XPD_s = 10·log₁₀((1−Kₓ)/Kₓ). [8]
- Availability: introduced in TF-era 0.15, retained after the 1.0 rewrite (specular + diffuse reflection + refraction). InteractionType bitmask in the 1.x solver: `NONE=0, SPECULAR=1, DIFFUSE=2, REFRACTION=4, DIFFRACTION=8`. [4][8][10][16]

### Diffraction (highest version sensitivity)
- **Drop-and-restore pattern** (essential for researchers to recognize): first-order diffraction introduced in TF-era 0.15 (NaN bug fixed in 0.16) → **removed in the 1.0 rewrite** → **reintroduced in 1.2.0 (2025-09-19)** (paths + radio maps, edge·lit-region flags). [4][9][10]
- **Theory**: UTD (Kouyoumjian-Pathak 1974, with Luebbers 1984 extension for finitely conducting wedges), consistent with ITU-R P.526. Diffracted rays lie on the Keller cone. The conceptual framing is GTD/Keller, but the implementation model is UTD (unverified: whether the current UTD coefficient formulation is numerically identical to the TF era is unconfirmed — closed-form solutions were re-derived during the rewrite). [3][11][17]
- **Order limit**: **only first-order diffraction** (single wedge) in every era. A path contains at most one diffraction event, and diffraction and diffuse reflection cannot be included simultaneously. Higher-order/multi-edge diffraction is in no release. [11][17]
- **Flag naming differences**: the flag sets differ between the 0.x API (`diffraction`, `edge_diffraction`) and the 1.2+ API (edge + lit-region). [4][12]

### Transmission / Refraction
- **Refraction/transmission is a new 1.0-rewrite feature**: 1.0.0 supports refraction in addition to specular + diffuse reflection. The TF-era original paper is reflection/diffraction-centric and does not describe a refracted-ray model through geometry (unverified: whether a partial-transmission/transmission-loss model existed before 0.19 cannot be confirmed). [9][10][17]
- **Single-layer slab model** (ITU-R P.2040-3): reflection/transmission fields account for thickness d (Fresnel-slab with internal multiple reflections, Jones matrices R(d)/T(d)), but the transmitted ray is traced as a single ray with no geometric refractive bending. Walls are recommended to be modeled as a single planar surface. Multi-layer/composite walls are natively unsupported — approximate with a single effective material + thickness. [11][17]

### Antennas and Polarization
- **Built-in patterns (current 2.x)**: `iso`, `dipole` (short dipole, Balanis 4-26a), `hw_dipole` (half-wave, Balanis 4-84), `tr38901` (3GPP TR 38.901 Table 7.3-1). [18]
- **Polarization**: "V" (0), "H" (π/2), "VH" (dual), "cross" (±π/4). Two polarization models: `polarization_model_tr38901_1` (direction-dependent spherical rotation), `..._2` (direct slant scaling). Custom extension via `register_antenna_pattern()`/`register_polarization()`. [18]
- The TF-era original paper also references the "tr38901"·"dipole" patterns. (Unverified: whether `hw_dipole` and the pattern-registry API existed under the same names in 0.14–0.19 was only confirmed for 2.x.) [3][18]
- **Accuracy caveat**: antennas are modeled with far-field patterns only — inaccurate inside the Fraunhofer boundary (near field), a source of validation error. [18][19]

### Differentiability (Dr.Jit vs old TensorFlow)
- **TF era**: differentiable via TensorFlow automatic differentiation with respect to materials (εᵣ, σ), antenna patterns, array geometry, and Tx/Rx position/orientation. RIS configuration is non-differentiable (original paper future work). [3]
- **Dr.Jit era**: fully differentiable via Dr.Jit automatic differentiation with respect to CIR, radio maps, etc. Geometric differentiability uses intersection reparameterization (obtaining ∂t/∂x, ∂t/∂φ via Dr.Jit AD). Differentiability is preserved through the rewrite while being greatly accelerated. The Dr.Jit path matured as radio map/material gradient (NaN) bugs were fixed in v1.1.0·v1.2.2. (Unverified: the report contains no head-to-head TF-vs-DrJit gradient comparison.) [3][10][11]
- **Calibration** (based on unverified literature, author-provided figures): "Learning Radio Environments by Differentiable Ray Tracing" (arXiv:2311.18558) validates εᵣ, σ, S, Kₓ along with differentiable antenna/scattering patterns and "neural materials" (MLP) against a DICHASUS channel sounder (3.438 GHz). Mean absolute power error: ITU 4.93 dB → learned 2.16 dB → neural 1.00 dB. (These are author-reported figures, so independent verification is incomplete.) [19]

## Recommended Version Guide by Research Type

- **RIS research**: use **only TF-era 0.18 – 0.19.2**. RIS was removed in the 1.0 rewrite and remains a "future release", and **through 2.0.1 (mid-2026) no RT release notes announce a RIS reintroduction** — even the current 2.0.1 documentation does not list RIS as an RT feature. (No official statement affirming a RIS return was found; marked as unverified.) Terminal version 0.19.2. [6][9][10][15]
- **Differentiable calibration**: for a TF pipeline, 0.19.x; if you need modern Dr.Jit AD·velocity·editability, **≥1.1.0** (reflecting the NaN gradient fix); if you need material differentiability improvements, **≥1.2.2 / 2.0.1**. Gradients with respect to materials·antennas·arrays·positions are supported in both eras. [3][10][11]
- **Large-scale coverage/radio maps**: if you need SINR·RSS·cell association·CDF, 0.19.0+. Arbitrary terrain/mesh radio maps require **≥1.1.0**, memory efficiency **≥1.2.2**. Based on `RadioMapSolver`. [7][10][12]
- **ISAC/sensing·RCS (unverified area)**: RCS is not a first-class parameter of `RadioMaterial` in any version and is a value that emerges from geometry + scattering coefficient/pattern (unverified: no built-in RCS API). For RCS workflows, refer to ray-sampling-based community/official discussion (Discussion #844) and related literature (arXiv:2505.08754, 2411.03206). Recommend **≥1.2.0** where differentiability·diffuse scattering are stable, but design on the premise that RCS primitives are absent. [20][21]
- **Legacy reproducibility**: to reproduce published results, pin the exact Sionna version + P.2040 revision. Reproduce TF-era (P.2040-2) results with **0.19.2**, and post-rewrite (P.2040-3) results with the corresponding 1.x/2.x tag. If diffraction is needed, use ≤0.19.2 or ≥1.2.0 (avoid 1.0.0–1.1.0). [3][7][11]

## Implications for Our Tool (SEAM Studio, currently sionna-rt 2.0.1)

This history supports the idea that an "engine version swap" feature is not optional but essential, for the following reasons.

1. **The feature set does not increase monotonically across versions (drop-and-restore)**: diffraction was introduced in 0.15 → removed in 1.0 → restored in 1.2.0, and RIS exists only in 0.18–0.19.2 and has not returned through 2.0.1. Therefore a single fixed engine cannot cover research that requires specific physics (diffraction·RIS), and users must be able to select and switch engine versions to match their research goals. [4][6][9][10][15]
2. **RIS/diffraction legacy workflows exist only in 0.19.2**: the official guide explicitly says "if you need these features, use the 0.19 release." To support RIS researchers, one must be able to run TF-era (TensorFlow) engines and Dr.Jit-era engines side by side, which means the coexistence of two stacks with different frameworks (TF vs PyTorch/Dr.Jit). [9][10]
3. **Reproducibility depends on the exact (engine version + P.2040 revision) combination**: because the P.2040-2↔-3 transition can change the εᵣ/σ of the same material, reproducing published results requires being able to revert to the exact engine tag the paper used. Without version pinning/switching, legacy reproducibility breaks. [3][11]
4. **Dependency pins differ per tag and there are regression cases**: sionna-rt 2.0.0 accidentally omitted the 1.2.2 changes and carries a "use v2.0.1" warning, and the Mitsuba/Dr.Jit pins differ per tag (1.0.0=3.6.2/1.0.3 … 2.0.1=3.8.0/1.3.1). An engine-swap feature implies that each engine version must be installed and managed in isolation with its verified, exact dependency pins. [10][13][14]
5. **Platform/framework requirements shift by era**: the Python floor rises (3.8/3.9 removed in 1.2.0), PHY/SYS migrated to PyTorch in 2.0.0 (removing the TF dependency), and ARM-Linux is supported from 1.2.2. The ability to select and switch to the engine version that fits a particular user environment (GPU/OS/Python/framework) determines the tool's portability and longevity. [13][14]

## Source List

1. Sionna v0.14 announcement (Discussion #105): https://github.com/NVlabs/sionna/discussions/105
2. Sionna RT paper (Hoydis et al., arXiv:2303.11103): https://arxiv.org/abs/2303.11103
3. Sionna RT paper HTML (ar5iv): https://ar5iv.labs.arxiv.org/html/2303.11103
4. Sionna v0.15 announcement (Discussion #166): https://github.com/NVlabs/sionna/discussions/166
5. Sionna v0.17 announcement (Discussion #415): https://github.com/NVlabs/sionna/discussions/415
6. Sionna v0.18 announcement (Discussion #479): https://github.com/NVlabs/sionna/discussions/479
7. Sionna v0.19 announcement (Discussion #605) and releases: https://github.com/NVlabs/sionna/discussions/605 · https://github.com/NVlabs/sionna/releases
8. EM Primer (scattering·polarization): https://nvlabs.github.io/sionna/rt/em_primer.html
9. Announcing Sionna 1.0 (Discussion #776): https://github.com/NVlabs/sionna/discussions/776
10. sionna-rt releases: https://github.com/NVlabs/sionna-rt/releases
11. RadioMaterial documentation (ITU-R P.2040-3): https://nvlabs.github.io/sionna/rt/api/radio_materials.html
12. sionna-rt tag release pages (v1.1.0/v1.2.0): https://github.com/NVlabs/sionna-rt/releases/tag/v1.2.0
13. Per-tag pyproject.toml and PyPI JSON: https://raw.githubusercontent.com/NVlabs/sionna-rt/&lt;tag&gt;/pyproject.toml · https://pypi.org/pypi/sionna-rt/json
14. sionna v2.0.0 release (PyTorch migration): https://github.com/NVlabs/sionna/releases
15. Sionna RT technical report (arXiv:2504.21719, RIS footnote·version table): https://arxiv.org/abs/2504.21719 · https://arxiv.org/html/2504.21719v2
16. Paths API (InteractionType): https://nvlabs.github.io/sionna/rt/api/paths.html
17. Diffraction tutorial: https://nvlabs.github.io/sionna/rt/tutorials/Diffraction.html
18. Antenna Pattern API: https://nvlabs.github.io/sionna/rt/api/antenna_pattern.html
19. Calibration paper (arXiv:2311.18558): https://ar5iv.labs.arxiv.org/html/2311.18558
20. RCS computation discussion (Discussion #844): https://github.com/NVlabs/sionna/discussions/844
21. RCS characterization literature: https://arxiv.org/pdf/2505.08754 · https://arxiv.org/pdf/2411.03206

## Appendix: Local Empirical Verification in This Repo (2026-07-03)

Separately from the literature survey above, the following were directly confirmed on the actual installation in this repository.

- `backend/.venv` (builtin): `sionna-rt 2.0.1 + mitsuba 3.8.0 + drjit 1.3.1` — matches the 2.0.1 row of the matrix.
- `backend/.venv-sionna-rt-122`: `sionna-rt 1.2.2 + mitsuba 3.8.0 + drjit 1.3.1` — matches the 1.2.2 row.
- Both versions have the `specular_reflection / diffuse_reflection / refraction / diffraction / edge_diffraction / diffraction_lit_region` parameters on `PathSolver.__call__` (consistent with the 1.2.0 diffraction reintroduction and the edge·lit-region flag claims). `RadioMapSolver` also exposes the same three diffraction flags.
- Measured PyPI upload dates (`pypi.org/pypi/sionna-rt/json`): 1.0.0=2025-03-18, 1.1.0=2025-06-05, 1.2.0=2025-09-19, 1.2.2=2026-03-19, 2.0.0=2026-03-19 (same day), 2.0.1=2026-03-31 — all six dates in the matrix match.
- Cross-engine physics consistency: on the same lab_room scene (28 GHz, max_depth 3), the builtin 2.0.1 and 1.2.2 subprocess engines agree with 62 paths each and a strongest path of −13.84 dBm @ 9.737 ns.

For engine-swap usage, see [engines.md](engines.md).
