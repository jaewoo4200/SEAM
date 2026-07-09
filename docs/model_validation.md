# Model Validation

> **English** · [한국어](model_validation.ko.md)

This document summarizes the results of cross-checking the communication channel/propagation models implemented in SEAM Studio against official standards, textbooks, and peer-reviewed literature, together with industry-standard validation practices and a practical validation checklist that can be applied directly to this repository.

The validation evidence comes from (a) formulas extracted verbatim from the official 3GPP TR 38.901 V17.0.0 spec (Table 7.4.1-1, Notes 1–6), (b) standard textbooks (Rappaport, Tse & Viswanath, Goldsmith), and (c) repository source code `file:line`. Items that could not be confirmed against official/peer-reviewed sources are marked **(unverified)**.

---

## 1. Validation Targets and Audit Summary

- **Target files:** `backend/app/services/channel_analysis.py`, `backend/app/services/simulation_backends/sionna_backend.py`, `plugins/example_two_ray/plugin.py`
- **Primary reference:** 3GPP TR 38.901 V17.0.0 (2022-03) Table 7.4.1-1 [1] (V16/V17 formulas identical). Textbooks [7][8][9].

**Summary:** All 20 audited formulas matched their cited references (**CORRECT**). Every 3GPP constant matches the spec character-for-character in value and unit. **No clear deviations (DEVIATION) were found.** However, 2 items requiring notes on their valid range/convention are flagged as **NEEDS-CHECK (low)**. In other words, the "adopted fixes" below are not calculation bug fixes but **comment/documentation-level supplements**.

---

## 2. Validation Table — Implemented Models vs References

### 2.1 Path loss / channel metrics (`channel_analysis.py`)

| # | Model | Implemented formula (file:line) | Reference | Verdict | Notes |
|---|------|----------------------|------|------|------|
| 1 | **FSPL** | `20·log10(4π·d·f/c)`, d floored to 1 m — `:67` | Friis/FSPL; equivalent to `32.45 + 20log10(f_MHz) + 20log10(d_km)` [2][3] | CORRECT | Closed form, unit-independent. Explanation of the 32.45 constant equivalence is accurate. |
| 2 | **CI (close-in) n=2/3** | `PL = FSPL(1m) + 10·n·log10(d)`, d0=1 m — `:75-83` | Rappaport CI: `PL(d)=FSPL(d0)+10n·log10(d/d0)`, d0=1 m; free space recovered at n=2 [6] | CORRECT | Standard d0=1 m anchor; n=2 converges exactly to the FSPL slope. |
| 3 | **UMa LOS** | `PL1=28.0+22log10(d3D)+20log10(fc)`; `PL2=28.0+40log10(d3D)+20log10(fc)−9log10(dBP²+(hBS−hUT)²)` — `:112-122` | TR 38.901 Table 7.4.1-1 [1] | CORRECT | 28.0/22/20/40/−9 all match. |
| 4 | **UMa NLOS** | `PL'=13.54+39.08log10(d3D)+20log10(fc)−0.6(hUT−1.5)`; `max(LOS, NLOS')` — `:130-136` | TR 38.901 [1] | CORRECT | 4 constants and the `max()` combining rule accurate. |
| 5 | **UMi LOS** | `PL1=32.4+21log10(d3D)+20log10(fc)`; `PL2=…40log10…−9.5log10(dBP²+(hBS−hUT)²)` — `:139-149` | TR 38.901 [1] | CORRECT | UMi −9.5 vs UMa −9.0 coefficient distinction accurate. |
| 6 | **UMi NLOS** | `PL'=35.3log10(d3D)+22.4+21.3log10(fc)−0.3(hUT−1.5)`; `max(LOS,NLOS')` — `:157-163` | TR 38.901 [1] | CORRECT | 35.3/22.4/21.3/−0.3 match. |
| 7 | **InH LOS** | `32.4+17.3log10(d3D)+20log10(fc)` — `:166-168` | TR 38.901 [1] | CORRECT | Match. |
| 8 | **InH NLOS** | `PL'=38.3log10(d3D)+17.30+24.9log10(fc)`; `max(LOS,NLOS')` — `:171-177` | TR 38.901 [1] | CORRECT | 38.3/17.30/24.9 match, `max()` rule accurate. |
| 9 | **Breakpoint distance** | `d'BP = 4·h'BS·h'UT·fc/c`, h'=h−1.0 (hE=1.0 m), fc [Hz], c=2.998e8 — `:93-98` | TR 38.901 Note 1: `d'BP=4·h'BS·h'UT·fc/c`, fc in **Hz**, hE=1.0 m (UMi) [1] | CORRECT (Flag A) | Formula, coefficient 4, effective-height subtraction, and fc[Hz] convention accurate. See Flag A below for UMa hE. |
| 10 | **Noise floor** | `−174 + 10log10(B) + NF` — `sionna_backend.py:302` | kTB = −174 dBm/Hz at 290 K [4] | CORRECT | Standard kTB+NF. Now **SINR = S/(I+N)** (interference from other TX in the same scene summed via ray tracing); with no interference, `SINR = SNR`. |
| 11 | **Shannon capacity** | `B·log2(1+SINR_lin)/1e6` Mbps — `:562-565` | Shannon–Hartley `C=B·log2(1+SINR)` [8][9] | CORRECT | SINR converted dB→linear before use. Identical to the SNR-based result when there is no interference. |
| 11a | **RSRP** | `RSS − 10log10(N_sc)`, `N_sc=N_RB·12` — `:577-578` | 3GPP TS 38.215 [18]: RSRP = average received power per resource element (RE) | CORRECT | Distributes wideband RSS evenly across occupied subcarriers (power per RE). |
| 11b | **RSSI** | `10log10(lin(RSS)+lin(I)+lin(noise_floor))` — `:579-580` | TS 38.215 [18]: RSSI = total in-band received power (signal+noise+interference) | CORRECT | Now **includes the interference power term** (Flag C resolved). In a single-TX scene `I=0`, so it is identical to before. |
| 11c | **RSRQ** | `10log10(N_RB·lin(RSRP)/lin(RSSI))` — `:581` | TS 38.215 [18]: RSRQ = N_RB·RSRP/RSSI | CORRECT | `10log10(1/12)=−10.79 dB` upper bound in the signal-dominant limit ignoring interference/noise; with interference it drops below that. |
| 12 | **RMS delay spread** | power-weighted `sqrt(Σw(τ−τ̄)²/Σw)`, weights = linear power — `:300-313` | 2nd central moment of the PDP (linear-power weighted) [7] | CORRECT | Both mean delay and variance are linear-power weighted, accurate. |
| 13 | **Coherence bandwidth** | `Bc = 1/(2π·στ)` [MHz] — `:316-322` | Jakes theoretical form (0.5 correlation): `Bc=1/(2πστ)` [5] | CORRECT | Theoretical (Jakes) 50% bound. Rappaport/Lee empirical forms `1/(5στ)`·`1/(50στ)` are merely different conventions, not more accurate. Recommend stating "theoretical form" in the docstring. |
| 14 | **K-factor** | `10log10(P_LoS / ΣP_NLoS)` — `:286-297` | Rician K = dominant (LOS) power / scattered (NLOS) power [7][9] | CORRECT | Returns None when LOS/NLOS is absent (undefined/∞) — handling accurate. |

### 2.2 Beamforming / codebook (`sionna_backend.py`)

| # | Model | Implemented formula (file:line) | Reference | Verdict | Notes |
|---|------|----------------------|------|------|------|
| 15 | **Azimuth steering vector** | `w = exp(j·2π·y·sin(θ))`, y = element offset in wavelengths, normalized — `:49-51` | ULA/planar array: element phase `2π·(d/λ)·sin(θ)`, with y already in λ units [8] | CORRECT | Uses actual `normalized_positions` (λ), robust to element ordering. Sign convention probe-verified in the code. Unit-norm normalization accurate. |
| 16 | **DFT codebook sweep** | Azimuth scan, gain=`|w_r^H · H · w_t|²`, best pair selected — `:54-85` | Angular-codebook beam training, received power = `|w_r^H H w_t|²` [8] | CORRECT | Full [rx][tx] gain map · argmax pair selection. Consistent single-element `h00` normalization (relative array gain). |
| 17 | **TX-MRT gain** | `‖H[0,:]‖² / h00` [dB] — `:693-696` | MRT: array gain `‖h‖²`, coherent-combining upper bound 10log10(Nt) [8] | CORRECT | `vdot(h0,h0)=‖h0‖²`. First-RX-antenna direction (SIMO row) definition valid, array gain via h00 relativization. |
| 18 | **SVD upper bound** | `σ_max² / h00` [dB] — `:697-699` | SVD/eigenbeamforming: optimal gain = largest singular value σ_max, power gain = σ_max² [8] | CORRECT | `svd(H)[0]=σ_max` squared → power gain. A true MIMO upper bound above MRT/codebook, ordering accurate. |

### 2.3 Two-ray plugin (`plugins/example_two_ray/plugin.py`)

| # | Model | Implemented formula (file:line) | Reference | Verdict | Notes |
|---|------|----------------------|------|------|------|
| 19 | **Two-ray far-field PL** | `40log10(d) − 10log10(Gt) − 10log10(Gr) − 20log10(ht) − 20log10(hr)` — `:117-123` | Two-ray ground-reflection far field: `PL=40log10(d)−20log10(ht·hr)−10log10(Gt·Gr)`, d⁴ slope, frequency-independent [7][9] | CORRECT | d⁴/40log10 slope, height terms, and frequency independence match. Gt=Gr=1 (0 dBi) documented, actual gains applied separately by the caller. |
| 20 | **Crossover distance** | `d_c = 4π·ht·hr·f/c` — `:104` | Two-ray breakpoint `d_c = 4·ht·hr/λ = 4π·ht·hr·f/c` [7][9] | CORRECT (Flag B) | `4/λ = 4πf/c` algebra accurate. Below d_c falls back to FSPL (valid=False). See Flag B below. |

### 2.4 Cross-observations (all CORRECT, recorded for completeness)

- **fc units:** In the 38.901 formulas the `20log10(fc)` term uses `fc=freq_hz/1e9` (GHz), while the breakpoint uses `freq_hz` (Hz) — exactly matching the Note 1 vs Note 6 conventions.
- **d3D vs d2D:** The LOS/NLOS PL terms use d3D, while the breakpoint comparison and valid range use d2D (UMa/UMi)/d3D (InH). `_geometry()` (`:101-109`) derives d2D via Pythagoras, accurate.
- **Valid range** (`:190, :209`): frequency 0.5–100 GHz (Note 2, fH=100 GHz), UMa/UMi 10 m–5 km (2D), InH 1–150 m (3D) — matching the spec's applicability columns.
- **CFR** (`:325-360`): `H(f)=Σ a_l·exp(−j2πf·τ_l)`, `|a_l|=sqrt(linear power)` — voltage-amplitude/power conversion and the Fourier sign convention accurate.

### 2.5 3GPP measurement metrics — RSRP / RSSI / RSRQ (`channel_analysis.py:567-609`)

The wideband RSS obtained from ray tracing is placed on the OFDM resource grid of the requested subcarrier spacing (`subcarrier_spacing_khz`,
default 30 kHz = 5G NR FR1, 15 kHz = LTE) to derive 3GPP **TS 38.215**
[18]-style measurement quantities. The number of resource blocks is `N_RB = ⌊B / (12·SCS)⌋` (12
subcarriers per RB), and the occupied subcarriers are `N_sc = 12·N_RB`.

- **RSRP (Reference Signal Received Power)** = average received power per resource element (RE).
  Since the wideband RSS is distributed evenly across the occupied subcarriers,
  `RSRP[dBm] = RSS[dBm] − 10·log10(N_sc)`.
- **RSSI (Received Signal Strength Indicator)** = total received power (signal +
  interference + noise) within the measurement band, summed linearly: `RSSI = 10·log10(lin(RSS) + lin(I) + lin(noise_floor))`.
  It **now includes the interference power term `I`** (§2.1 #11b, Flag C resolved). `I` is the
  sum of the ray-traced received power that every TX other than the serving TX in the same scene produces at this RX (full-buffer assumption,
  §3 Flag C). In a single-TX scene `I=0`, so the result is identical to before.
- **RSRQ (Reference Signal Received Quality)** = `N_RB·RSRP/RSSI` (linear),
  `RSRQ[dB] = 10·log10(N_RB·lin(RSRP)/lin(RSSI))`.

**Signal-dominant upper bound (spot check):** When interference/noise are far smaller than the signal so that `RSSI → RSS`
converges, `RSRQ → 10·log10(N_RB·RSRP/RSS) = 10·log10(N_RB/N_sc) = 10·log10(1/12)
= −10.79 dB`. That is, the theoretical upper bound of RSRQ is **−10.79 dB** independent of SCS/bandwidth
(assuming exactly 100% resource occupancy and zero interference), and **with interference the RSSI denominator grows so it drops
below that.** `tests/test_channel_analysis.py` pins this
upper bound to ±0.05 dB on a high-SNR, single-TX mock link, and verifies that halving the SCS from 30→15 kHz doubles `N_sc`, so RSRP
decreases by exactly `−10·log10(N_sc15/N_sc30)` (≈ −3.01 dB, excluding grid rounding).
Additionally, the **2-TX scene test** (`test_api_two_tx_interference_lowers_sinr`) verifies that
RSRQ falls below this −10.79 dB upper bound, and that `SINR = S/(I+N) < SNR`,
`RSSI = 10log10(lin(S)+lin(I)+lin(N))` hold as defined.

### 2.6 Material impact assessment — NMSE / cosine similarity / dRSS (`services/material_impact.py`)

The same TX→RX link is solved in both a **material-assigned scene** and a **single-baseline-material scene** (rebinding every prim to
`baseline_material_id`, default `itu_concrete`), and the two channel
frequency responses `H(f)` are compared per location (Lee et al., KICS 2026). `H(f_k)=Σ_l g_l
exp(−j2πf_k τ_l)`, `|g_l|=sqrt(linear power)` is the same tap
model as the §2.4 CFR and the channel-analysis panel (`:35-46`).

| # | Metric | Implemented formula (file:line) | Reference (KICS eq.) | Verdict | Notes |
|---|------|----------------------|-----------------|------|------|
| 21 | **Per-location NMSE** | `10·log10(Σ_k|H_mat−H_base|² / Σ_k|H_mat|²)` [dB] — `:134-135` | Normalized mean-square error `NMSE = ‖H_mat−H_base‖² / ‖H_mat‖²` [KICS] | CORRECT | Both numerator and denominator are linear power sums, converted to dB. `err=0` (identical channel) → −300 dB floor (substituting for −∞). If `> sensitive_nmse_db` (default −60 dB), flagged material-sensitive. |
| 22 | **Global NMSE** | `10·log10(Σ_pos err / Σ_pos E_mat)` [dB] — `:166-168` | Linear accumulation of error/energy over all locations, then NMSE [KICS] | CORRECT | Not the mean of per-location dB values but the **linear accumulated ratio** (energy-weighted). None (undefined) if the accumulated numerator/denominator = 0. |
| 23 | **Cosine similarity** | `|H_matᴴ·H_base| / (‖H_mat‖·‖H_base‖)` — `:141-143` | CFR shape similarity (inner product/norm product), [0,1] [KICS] | CORRECT | Absolute value of `vdot` (conjugate inner product). 1.0 if the two CFRs differ only in scale. Computed only when `E_mat,E_base>0`. |
| 24 | **dRSS** | `RSS_mat − RSS_base`, `RSS=10log10(Σ linear power)` [dB] — `:147-157` | Signed received-power difference [KICS] | CORRECT | Material−baseline. Positive = the assigned material raises received power over the baseline. Only when both paths exist. |
| 25 | **Capacity proxy** | `B·mean_f log2(1 + P|h(f)|²/N) / 1e6` Mbps — `:49-56` | Shannon throughput proxy (frequency-averaged) [KICS/8][9] | CORRECT | Computed separately for material/baseline, dB→linear SNR. Same family as the §2.1 #11 Shannon (here averaged over the frequency axis). |

**Identity spot check (mock):** The mock backend only reflects the scattering term for ITU frequency-dependent materials (§rf), so it is **material-blind**. Giving the material already attached to the only reflecting prim as
`baseline_material_id` unchanged makes the two scenes RF-identical, so at every location
`H_mat≡H_base` → converging to **cosine similarity 1, dRSS 0, and global NMSE None because err=0**.
`tests/test_material_impact.py::test_material_impact_identity_three_waypoints`
pins this mathematical identity at 3 waypoints. The actual distribution of per-location NMSE (−6 to −17 dB,
`lab_room`) is verified on the Sionna backend.

---

## 3. Adopted Fixes (deviations the audit found → precise code changes)

The audit found **no calculation errors (DEVIATION).** Therefore, the following are not "wrong value → correct value" replacements but **minimal code changes (mostly comments + one optional conditional branch)** that clarify the valid range/convention. All severities are **low (advisory)**.

### Flag A — UMa effective-height hE simplification (`channel_analysis.py:93-98`) — NEEDS-CHECK / low

- **Observation:** The code uses a fixed `hE = 1.0 m` for both UMa/UMi.
- **Reference:** TR 38.901 Table 7.4.1-1 Note 1 [1]. `hE=1.0 m` is **exact for UMi**. For **UMa** it is a random variable: with probability `1/(1+C(d2D,hUT))` it is `hE=1 m`, otherwise drawn from `uniform(12,15,…,hUT−1.5)`. When `hUT<13 m`, `C=0 ⇒ hE=1 m` deterministically, so it is **identical**. Only for `hUT≥13 m` does the LOS PL1→PL2 transition point shift.
- **Impact:** The UMa LOS breakpoint shifts only for high UT antennas; negligible at typical UT heights. Many link-budget tools such as ns-3 adopt the same simplification.
- **Adopted change (comment):** Add a comment to the following effect near the breakpoint calculation around `channel_analysis.py:93`.

  ```python
  # NOTE(38.901 Note 1): hE=1.0 m is exact for UMi, and for UMa when hUT<13 m
  # (C()=0 -> hE deterministically 1 m). For UMa with hUT>=13 m, 38.901 makes hE
  # a random variable in {1 m, U(12,15,...,hUT-1.5)}; we use the common deterministic
  # hE=1 m simplification (as in ns-3). Only shifts the LOS PL1->PL2 breakpoint for
  # high UT antennas.
  ```
- **Optional code change (if desired):** In the UMa path, add a warning flag to the return value indicating that the result is approximate when `hUT >= 13 m` (without changing the calculation itself).

### Flag B — Two-ray crossover constant 4π vs 4 convention (`plugin.py:104`) — NEEDS-CHECK / low

- **Observation:** `d_c = 4π·ht·hr·f/c = 4π·ht·hr/λ`. This is **π times larger** than `4·ht·hr/λ` (Rappaport eq. 4.58 [7]).
- **Analysis:** Some textbooks define the two-ray breakpoint as `4·ht·hr/λ`, while other literature defines it as `4π·ht·hr/λ` on a first-Fresnel-zone/phase-crossover basis — the 4π vs 4 ambiguity exists across textbooks. The code's `4π·ht·hr·f/c` is internally consistent with its own docstring and both forms appear in peer-reviewed literature, so it is **not a clear error (NEEDS-CHECK)**.
- **Impact:** If the intended reference is Rappaport eq. 4.58 (`4 ht hr/λ`), the extra π enlarges d_c by about 3.14× and widens the FSPL fallback (near-field) region.
- **Adopted change (pick one):**
  1. **If the intent is Fresnel/phase-crossover (4π)** — no change needed. Add a one-line note to the docstring: "first-Fresnel-zone / phase-crossover form (4π·ht·hr/λ), not the bare 4·ht·hr/λ".
  2. **If the intent is Rappaport eq. 4.58 (4)** — replace `plugin.py:104` with the following:

     ```python
     # was: d_c = 4.0 * math.pi * ht * hr * f / c
     d_c = 4.0 * ht * hr * f / c        # Rappaport eq. 4.58: d_c = 4·ht·hr/λ
     ```
  - **Recommendation:** Choose after fixing the cited textbook edition. Until then, apply only option 1 (comment).

### Flag C — Multi-TX co-channel interference (`channel_analysis.py:535-565`) — RESOLVED

- **Previous observation:** RSSI = `lin(RSS) + lin(noise_floor)`, SINR = SNR — **there was no interference power term**.
- **Current state (implemented):** It now **models** co-channel interference. The ray-traced received power that every TX other than the serving TX produces at this RX is summed linearly to obtain the interference `I`
  (`analyze_channel` computes it with one additional solve with only the other TX enabled, cheap thanks to the scene cache), and
  the following all reflect `I`:
  - `interference_dbm = 10log10(Σ_{k≠serving} lin(P_k@rx))`, `num_interferers`,
  - `SINR = S / (I + N)` (`sinr_db`; `= SNR` if there is no interference),
  - `RSSI = 10log10(lin(S) + lin(I) + lin(N))` → therefore `RSRQ` also reflects interference,
  - Shannon capacity is SINR-based (`B·log2(1+SINR)`).
  - Same for trajectories: each waypoint carries `interference_dbm` and the true SINR,
    and the serving cell is selected via `serving_tx_id` (first TX if unspecified)
    (`trajectory.py:93-159`).
- **Reference:** TS 38.215 [18]. RSSI/RSRQ include signal + noise + **adjacent-cell interference**, and
  they now fully satisfy the definition.
- **Assumptions / remaining simplifications:** **Full-buffer worst case** — every interfering TX is assumed to transmit simultaneously at 100% load on the same
  resource (time/frequency). That is, (1) **no scheduler/load model** (partial load, resource reuse, and activity factor not reflected — more pessimistic than measurements),
  (2) **no inter-cell scrambling/orthogonalization** (interference treated as a power sum rather than coherently,
  ignoring the symbol correlation of the interfering signals). Both are future extension points.

### Summary

| Flag | file:line | Type | Required change | Severity |
|--------|-----------|------|-----------|--------|
| A | `channel_analysis.py:93-98` | Comment (+ optional warning flag) | No (documentation) | Low |
| B | `plugin.py:104` | Comment or constant correction (after confirming intent) | Intent confirmation needed | Low |
| C | `channel_analysis.py:535-565` | Interference modeling (implemented) | **Done (RESOLVED)** — remaining: scheduler/scrambling | Low |

---

## 4. Industry Validation Practices

Representative ways commercial/research tools validate RF digital twins, and their acceptance criteria. Quantitative thresholds are linked to their sources.

### 4.1 Path-loss RMSE against measurement campaigns (Remcom Wireless InSite family)

- **Method:** full 3D ray tracing (SBR + image), reflecting terrain/structures/foliage [10]. Validation is **per-link path-loss RMSE against drive/measurement campaigns**, compared after tuning material EM parameters.
- **Acceptance range (literature summary, NYURay review [11]):** ≈**5 dB RMSE** with indoor calibration (2.4/5 GHz), but a **7–10 dB** variation just from the choice of material permittivity/conductivity → material calibration dominates the error budget. Urban/scattering environments show **6.58–13.86 dB RMSE** (uncalibrated urban ~12–15 dB "practical" band).
- **Implication:** This tool's `services/calibration.py` level-offset RMSE metric matches industry practice exactly. "Good" criteria ≈ calibrated indoor **≤5–6 dB**, uncalibrated urban up to ~10–15 dB.

### 4.2 CFR/CIR ground-truth conventions (NVIDIA AODT)

- AODT specifies the channel output data model [12]: `raypaths` (geometric CIR, per-path complex gain `h`, delay `τ`), `cirs` (time-domain `h(t)=Σ h·δ(t−τ)`), `cfrs` (per-subcarrier `H(k)`).
- **Normalization convention:** AODT's UE CFR folds transmit-power/antenna-count normalization into the CFR: `⟨H^UE,H^UE⟩ = [P^RU/(n·N_pol·N_hor·N_vert)]·⟨H^ch,H^ch⟩`. This backend computes the `|a|` gains and then adds `power_dbm` separately (`sionna_backend.py:882`), summing element power as one amplitude per path (`:843-848`) — **a convention difference (per-element vs per-array) that must be stated when cross-comparing**.
- **Validation posture:** The AODT docs prescribe the CFR/CIR/raypaths DB as ground truth for ML training, with EM-solver-level accuracy. Any NVIDIA/Keysight-published "AODT vs measurement" acceptance RMSE figures are **unverified** (no official document found).

### 4.3 Sionna RT's published validation and differentiable-calibration convention

- **Important nuance:** The original Sionna RT paper [13] is **not a validation against measurement data**. It trains material parameters on **synthetic CFR** generated by its own ray tracer (normalized MSE). The paper itself warns that "exact phase is hard to predict with ray tracing, so the CFR-training approach will likely not fit measurement data well." In other words, "differentiable calibration" is a **capability**, not a measurement-validation result — cite it honestly.
- **FSPL agreement:** The ns-3 integration study [14] ("Ns3 meets Sionna") reports that Sionna RT gives **identical results** to the ns-3 Friis free-space model (qualitatively). However, the quantitative threshold "**<0.01 dB / <1 ns**" is not confirmed in the body of that reference — **(unverified)**. This tool's FSPL accuracy test aims to reproduce this agreement, but the threshold is set independently (§6 A: <0.1 dB).
- **Real-system (5G testbed) validation:** In a system-level validation injecting VNA-measured channels into an OAI 5G-NR emulator, the main error sources reported were (1) material-property mismatch and (2) breakdown of the far-field pattern assumption in the **near field (within the Fraunhofer distance)**. **Lesson:** validate at the KPI (RSRP/SNR/SINR) level, de-embed instrumentation, and mark points within the Tx Fraunhofer distance as out-of-model. (The specific arXiv ID/figures could not be confirmed from the original note — **(unverified)**.)
- **mmWave channel-sounder calibration (acceptance-criteria gold standard):** [15] EuCAP 2024. 26–30 GHz VNA sounder, SAGE MPC extraction, Volcano Flex RT. Calibrates reflection −3 dB · diffraction −2 dB offsets while maintaining the reflection+diffuse **power balance**. **Final acceptance:** mean errors of received power/delay spread/azimuth spread **< 1.5 dB / < 5 ns / < 2°**. The paper's per-parameter before/after table (mean error·standard deviation·RMSE·correlation) is a template worth replicating.
- **Upper mid-band site-specific calibration (NYURay):** [11] 6.75/16.95 GHz UMi Brooklyn, sliding-correlator sounder. **Path-loss RMSE 3.2 dB (LOS)/5.8 dB (NLOS)**, PLE deviation 0.03–0.14 (within 5% of 3GPP). Position calibration corrects an 8 dB received-power overestimate and a 4 ns delay mismatch. **Honest limitation:** RT **underestimates RMS delay spread** (RT 24.7/21.6 ns vs measured 62.8/46.5 ns) — missing diffuse/foliage scattering. This tool has the same DS-underestimation risk → documentation recommended.

### 4.4 3GPP TR 38.901 calibration (reference-CDF methodology)

- 38.901 "calibration" is not measurement validation but **cross-implementation agreement**: each party runs the agreed drop and overlays the output CDFs [16]. The prototype of this tool's "cross-engine agreement" checklist item.
- **Large-scale (§7.8.1):** fast fading OFF, UMa/UMi/InH, 6/30/70 GHz. Metrics: coupling loss (serving cell, LOS PL based), geometry (SIR) with/without noise.
- **Full (§7.8.2):** fast fading ON, 6/30/60/70 GHz. Metrics: coupling loss, wideband SIR (noiseless), delay-spread/angular-spread (ASD/ZSD/ASA/ZSA) CDF, PRB singular-value CDF (max/min/ratio, 10·log10).
- **Indoor Factory (§7.8.4):** InF-SL/DL/SH/DH, 3.5 & 28 GHz. Adds a first-path excess-delay CDF.
- **Reference results:** R1-165974/R1-165975 (large-scale/full), R1-1700990 (additional features), R1-1909704 (InF) — the source of the CDFs that must overlap.
- **Reference geometry (good spot check):** GCS/LCS θ=0 zenith/θ=90° horizontal (§7.1.1); UMa hBS=25 m, ISD 500 m, min 2D 35 m; UMi hBS=10 m, ISD 200 m, min 2D 10 m; Indoor-office 120×50×3 m, ceiling BS 3 m, hUT 1 m. This tool's UMa/UMi formulas (`:112-163`) can be spot-checked with this geometry.
- **Measurement validation of the 38.901 model itself:** [17] For InH 6.75/16.95/28/73 GHz, shadow-fading σ agrees with measurements to **< 0.6 dB** (LOS/NLOS), though high-frequency NLOS path loss is underestimated. → This tool's 38.901 overlay should reproduce σ_SF within a few tenths of a dB.

### 4.5 Acceptance-criteria summary table (what "pass" means)

| Feature / test | Metric | Acceptance threshold | Source |
|---|---|---|---|
| FSPL accuracy | PL·delay absolute error | **< 0.01 dB / < 1 ns** vs Friis/analytic **(unverified — figures not confirmed in the text)** | [14] |
| Site-specific RT vs sounder (calibrated) | received power/DS/AS mean error | **< 1.5 dB / < 5 ns / < 2°** | [15] |
| RT path loss (upper mid-band, calibrated) | path-loss RMSE | **~3.2 dB LOS / ~5.8 dB NLOS**; PLE Δ 0.03–0.14 | [11] |
| Wireless InSite indoor (calibrated) | path-loss RMSE | **~5 dB**; material choice ±7–10 dB | [11] |
| 38.901 model vs measurement (σ_SF) | shadow-fading std difference | **< 0.6 dB** | [17] |
| 38.901 cross-implementation calibration | coupling-loss·SIR·DS·AS·singular-value CDF overlap | qualitative CDF agreement vs R1-165974/165975 | [16] |
| Material-calibration convergence | level-offset RMSE reduction | sub-0.1 dB consistency across grid resolutions (NYURay) | [11] |

---

## 5. Practical Validation Checklist for This Repository

In value-for-cost order. Each item maps to an existing code seam.

**A. FSPL accuracy self-check (automated; unit test).** In a single-plane, single-material, LOS-only scene, assert that the ray-traced path loss matches `fspl_db()` (`channel_analysis.py:59`) to **< 0.1 dB** (the literature target <0.01 dB is a stretch) and the delay matches `d/c` to **< 1 ns**. Since RT already exposes `delta_vs_rt_db` (`:242, :247`) for the `fspl` model, in free-space geometry it suffices to check `abs(delta) < tol`. An analog of NYURay's 4 m free-space daily baseline.

**B. 38.901 reference-point spot check (automated; unit test).** Evaluate `_uma`/`_umi`/`_inh` (`:112-177`) at the §4.4 standard geometry (UMa hBS 25 m/ISD 500 m/35 m; UMi hBS 10 m/10 m; InH ceiling BS 3 m, hUT 1 m) and pin to the closed-form spec formula values. Assert that the **breakpoint transition is continuous at d_BP** and that **NLOS ≥ LOS** holds throughout (`max()` at `:136,:163,:177`). Check that the frequency-valid flag fires outside 0.5–100 GHz (`:190`).

**C. Cross-engine agreement report (automated; 38.901 pattern).** The architecture already supports alternative-engine venvs (`sionna_backend.py:326, 350`). Run builtin vs alternative engine on a fixed scene and report the **per-link path-loss delta + RSS/DS CDF overlap**. Pass if the median absolute delta is within tolerance (same solver version ~1 dB to start, flag >3 dB). A reflection of the 38.901 "CDF overlay" acceptance philosophy (not vs measurement).

**D. Energy / physics consistency checks (automated; low-cost invariants).**
- **FSPL monotonicity:** path loss increases monotonically with distance/frequency (guards against sign errors).
- **No power creation:** each reflection/transmission does not exceed the same-path-length LOS free-space gain (reflection coefficient ≤ 1). Checkable via per-path `|a|` (`sionna_backend.py:882`). *(Not stated as a formal acceptance criterion in the literature — a recommended invariant — **(unverified)**.)*
- **Reciprocity spot check:** in a symmetric link, swapping Tx/Rx roles gives total path gain matching within solver noise.
- **CFR↔CIR consistency:** assert that the DC bin of `compute_cfr` (`:325`) matches the coherent sum of the tap amplitudes.
- **K-factor / DS sanity:** RT typically underestimates RMS delay spread (NYURay 25 ns vs 63 ns) — flag → guide interpreting `rms_delay_spread_ns` (`:300`) as a lower bound.

**E. Measurement-calibration acceptance report (extend existing).** `services/calibration.py` already reports level-offset RMSE/MAE and a significant-improvement gate (`:71-171`). Add to it the mmWave-industrial/NYURay paper-style **per-parameter before/after table (mean error·standard deviation·RMSE·correlation)**, and expose the calibrated **material offsets (reflection/diffraction dB)** like Volcano Flex's −3/−2 dB. Documented goals: calibrated indoor path-loss RMSE **≤ ~5 dB**, received-power mean error **< 1.5 dB**.

**F. Near-field guard (documentation + warning).** Per the OAI/Sionna results, warn whenever **any Tx–Rx separation < the Fraunhofer distance `2D²/λ`** (breakdown of the far-field pattern assumption). Compute `D` from the array aperture the backend already builds (`sionna_backend.py:159-189`) and expose it as a channel-analysis warning.

**G. Metrology/unit discipline (documentation).** When comparing to measurements, require the campaign's **VNA SOLT/TRL de-embedding**, and state whether the stored CFR is per-element or per-array normalized (this tool's per-array sum `sionna_backend.py:843-848` vs AODT's per-element `P^RU/(n·N_pol·N_hor·N_vert)`). Keep the already-exported coordinate spot check (`rfdata_export.py:196`) as a geometry-frame check.

---

## Sources

- [1] 3GPP TR 38.901 V17.0.0, Table 7.4.1-1 and Notes 1–6, §7.8 — https://panel.castle.cloud/view_spec/38901-h00/pdf/
- [2] FSPL/Friis glossary — https://ib-lenhardt.com/kb/glossary/fspl
- [3] Free-space path loss (Wikipedia) — https://en.wikipedia.org/wiki/Free-space_path_loss
- [4] Thermal noise kTB — https://rfattenuator.net/tools/thermal-noise
- [5] Coherence bandwidth 1/(2πστ) — https://bpb-us-e1.wpmucdn.com/sites.gatech.edu/dist/c/488/files/2016/09/22-CoherenceBandwidth.pdf ; RMS delay spread vs Bc — https://www.mdpi.com/1424-8220/20/3/750
- [6] mmWave path-loss model survey (CI/close-in) — https://arxiv.org/pdf/1708.02557
- [7] T. S. Rappaport, *Wireless Communications: Principles and Practice* (two-ray, CI, K-factor, PDP)
- [8] D. Tse & P. Viswanath, *Fundamentals of Wireless Communication* (MRT/SVD, steering vector, Shannon)
- [9] A. Goldsmith, *Wireless Communications* (Shannon, two-ray, K-factor)
- [10] Remcom Wireless InSite — https://www.remcom.com/wireless-insite-propagation-software
- [11] NYURay calibration/validation (npj Wireless Technology) — https://www.nature.com/articles/s44459-025-00014-x
- [12] NVIDIA AODT — RAN Digital Twin (CFR/CIR/raypaths) — https://docs.nvidia.com/aerial/aerial-dt/text/ran_digital_twin.html
- [13] Sionna RT: Differentiable Ray Tracing for Radio Propagation Modeling (arXiv:2303.11103) — https://arxiv.org/abs/2303.11103
- [14] Ns3 meets Sionna: Using Realistic Channels in Network Simulation (arXiv:2412.20524) — https://arxiv.org/html/2412.20524v1
- [15] Ray-Tracing Calibration from Channel Sounding Measurements in a Millimeter-Wave Industrial Scenario, EuCAP 2024 (arXiv:2404.10590) — https://arxiv.org/abs/2404.10590
- [16] 3GPP TR 38.901 §7.8 calibration (reference results R1-165974/R1-165975/R1-1700990/R1-1909704) — https://panel.castle.cloud/view_spec/38901-h00/pdf/
- [17] 38.901 InH model vs measurement (σ_SF < 0.6 dB) (arXiv:2504.15589) — https://arxiv.org/pdf/2504.15589
- [18] 3GPP TS 38.215, "NR; Physical layer measurements" (RSRP/RSSI/RSRQ definitions) — https://www.3gpp.org/DynaReport/38215.htm

**Unverified items summary:**
- Sionna RT FSPL "<0.01 dB / <1 ns" quantitative threshold — figures not confirmed in the body of reference [14] **(unverified)**.
- NVIDIA/Keysight AODT vs measurement acceptance RMSE figures — no official document found **(unverified)**.
- Specific arXiv ID/figures for the Sionna RT vs OAI 5G testbed validation — the identifier from the original note could not be confirmed **(unverified)**.
- The arXiv mirror (2507.22027) identifier of the NYURay review PDF — the value from the original note could not be confirmed; cited via the official npj link [11] instead.
- Reflection coefficient ≤ 1 energy invariant — not stated as a formal acceptance criterion in the cited tools (recommended test) **(unverified)**.

**Repository evidence:** `backend/app/services/channel_analysis.py` (FSPL `:59-67`, 38.901 `:93-221`, CI `:75-83`, CIR/CFR/DS `:271-360`, delta-vs-RT `:247-260`, RSRP/RSSI/RSRQ `:539-609`); `backend/app/services/calibration.py:71-204`; `backend/app/services/simulation_backends/sionna_backend.py` (engine dispatch `:326,:350`, per-path power `:882`, element-power sum `:843-848`, array `:159-189`, noise `:302`, steering/codebook/MRT/SVD `:49-85, :693-699`); `plugins/example_two_ray/plugin.py:104-123`; `backend/app/services/rfdata_export.py:196-217`.
