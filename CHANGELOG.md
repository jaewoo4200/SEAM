# Changelog

All notable changes to SEAM Studio are documented in this file. The format
follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/), and
the project uses [semantic versioning](https://semver.org/) (0.x: minor
releases may add API fields). Item numbers (#NN) refer to the feature
requests of the blind-spot paper session. Releases up to v0.1.15 are
described in the GitHub release notes.

## [Unreleased]

## [0.1.16] - 2026-10-14

Track B: signal-level drone-echo detection (P0). Every new option is off by
default, and a run without it returns and stores exactly what v0.1.15 did.

### Added

- **#23 `sensing.waveform` (OFDM numerology).** `WaveformOptions` (`scs_hz`,
  `n_fft`, `n_active_sc`, `cp`, `symbol_duration_s`, `symbols_per_cpi`,
  pilot lattice `subcarrier_step` × `symbol_step`) on scenario sensing, ISAC,
  sensing coverage, pd-curve and channel analysis. With a waveform the
  coherent integration is the pilot REs per CPI, `cpi_pulses = K × N`
  (`10log10(K N)` dB, so it follows `cpi_s`: +6.99 dB from 10 to 50 ms), the
  noise floor is taken over `B_occ = n_active_sc × scs_hz`, and the range
  (c / B_occ bistatic) and Doppler (1 / (N T_p)) resolutions come from the
  grid. `detection_constants` adds `b_occ_hz`, `n_re_per_cpi`,
  `symbols_per_cpi`, `symbol_duration_s`, `pilot_fraction`, `grid_k`,
  `grid_n`, `n0_dbm_per_re` and `numerology`. Presets: NR 30 kHz 51 PRB
  (612 × 280 = 171,360 RE, 52.34 dB) and Grid S (256 × 16 = 4096 RE,
  36.12 dB). An explicit `cpi_pulses` that differs from the grid is
  overridden with a warning; without a waveform, an explicit `cpi_pulses`
  with a non-default `cpi_s` warns that the gain does not follow `cpi_s`.
  Sensing coverage and pd-curve gain a `cpi_s` request field (default
  0.01 s) for the numerology. A waveform whose symbols do not fit `cpi_s`,
  or with more than 10⁸ pilot REs per CPI, answers 400 before any solve on
  every route (scenario, ISAC, coverage, pd-curve). Channel analysis has no
  `cpi_s`: its numerology covers `symbols_per_cpi` symbols (else 10 ms).
- **#21 `sensing.background` (H0 / H1 background paths).** Per frame, a
  PathSolver solve of the TX → sensing-RX links with the target actors
  removed (H0, `background_h0`) and/or as absorbers (H1, `background_h1`),
  diffuse reflection on by default (the RCS solver traces none), plus
  `background_meta` (mechanisms, seed, cache hit, per-link power, `stored`,
  `error`). `cache: "static_nodes"` solves once while nodes and non-target
  actors stay put; `deterministic: true` by default; `max_paths_per_link`
  (default 256) keeps the strongest paths per link, dropped before the
  solver paths become rows, with one run warning counting the rest;
  `store` (default `"h0"`) keeps H0 in the result while H1 feeds the CFAR
  map in memory only (`"both"`, `"none"`). A failed background solve is
  never cached. Scenarios without a sensing target are allowed when the
  background is on.
- **#22 `include_channel` / `include_background_paths` (sensing dataset
  export).** `channel.npz` with complex64 slow-time CFR tensors
  `H[row, symbol, subcarrier]` per (frame, sensing link) and component
  (`echo`, `background_h0`, `background_h1`, `total`) on the pilot or active
  grid, an optional thermal-noise array keyed by seed (the same noise the
  run's CFAR map used) and the LS estimate. Echo rows gain `a_re`, `a_im`,
  `tx_power_dbm`, `interaction_types`, `rf_material_ids`, `prim_ids`
  (manifest `schema_version` 2 only for these opt-in exports); `background.*`
  tables hold the stored H0 / H1 rows. The response counts
  `num_background_rows`, `num_channel_rows` and `channel_elements`.
  Components that need an H1 the run did not store (`background_h1`,
  `total`, `pilot_ls`) answer 400.
- **#24 `sensing.detection` (delay-Doppler map and CFAR).** `mode: "cfar"`
  builds each link's delay-Doppler map from echo + background + thermal
  noise on the waveform's pilot grid (Hann or rect window, zero padding,
  optional mean subtraction and zero-Doppler notch) and runs 2-D CA- or
  OS-CFAR with an analytic threshold from `pfa_cell` or one calibrated on H0
  frames (`h0_result_id`, per cell or per frame with `system_pfa`; the
  calibration noise has its own stream, 0x5EA031, and `system_pfa` needs an
  H0 source covering every link of the run). Notched rows, and with
  `mean_subtract` its 0 Hz row (notched) and every row it empties, do not
  train the CFAR; a cell with fewer training cells is mapped onto the N_t
  scale. Every detection is stored in `SensingFrame.detections`; a link
  detects when a detection falls inside the oracle gate around the cell
  nearest its echo (`detection_index`, reason `cfar_miss` otherwise), and
  the run reports the achieved cell, link-frame and system P_FA with
  Clopper–Pearson intervals (the cell one at the effective sample of the
  per-map dispersion). Maps over 2²⁴ cells, a Hann axis of one pilot, a
  notch over every row or a tested row without training cells answer 400.
- **#30 `POST /simulate/sensing-background-ensemble` (H0 calibration
  ensemble).** Re-solves the H0 background of one stored scenario frame per
  realization: realization 0 nominal, later ones with seeded radio-material
  eps_r / sigma scales, one position offset per node site and a CFO per
  bistatic sensing RX. Stores the paths and the seed only; H0 noise frames
  are redrawn on demand bit for bit. New result kind
  `sensing_background_ensemble` (listed, labelled and pruned like the
  others), read back with `GET /results/sensing-background-ensemble`
  (`summary=true` leaves out the paths). A realization whose solve fails
  answers 400 instead of storing an empty H0.
- **Frontend.** Scenario ▸ Sensing gains a Waveform preset (None, NR 30 kHz
  51 PRB, Grid S), Background (Off, H0, H1, both), Detection (Threshold,
  CFAR) with P_FA per cell, Clutter removal (Auto = mean subtract when a
  background is on) and the CFAR threshold source (analytic, an H0
  ensemble or a background-only H0 run); the run readouts show the
  numerology, the background solves and the achieved P_FA, and the run
  history lists the `sensing_background_ensemble` kind.

### Changed

- `pydantic>=2.12` (was 2.7): the stored v0.1.16 fields use
  `Field(exclude_if=...)`, which pydantic 2.11 ignores (it would dump the
  fields as null and break `/openapi.json`). `pip install` upgrades an
  older pydantic to satisfy the floor.
- Sensing can be enabled on a scene without a sensing target when a
  background is solved; the run is then background-only (H0), and the UI
  switches Background to H0 for such a scene.
- Stored results: the new `SensingFrame` and `SensingLinkReport` fields are
  left out of the JSON while unset, so results of runs without the new
  options are byte-identical to v0.1.15.

### Notes

- The pilot lattice (`subcarrier_step`, `symbol_step`) replaces a scalar
  pilot fraction: Grid S (comb-5 in frequency, every 9th symbol) needs both.
- `WaveformOptions.symbol_duration_s` exists for grids whose symbol is not
  an NR one (Grid S: 1/14,400 s).
- `background.deterministic` and `max_paths_per_link` come with the
  background solve (#32(a) for the background only; the RCS solver is
  unchanged).
- A cached background frame stores no paths; `background_meta` names the
  frame that holds them.
- CFAR detections live in `SensingFrame.detections` (they belong to a map,
  not a target); a link report points at its match with `detection_index`.
- The held-out P_FA acceptance test is #30's, with the beta-binomial
  interval (n_cal 10,000, P_FA 1e-2: 74–129 alarms); calibration reports use
  Clopper–Pearson.
- The calibrated threshold is `sorted(values)[n - floor(n p)]`.
- Ensemble material perturbations are applied in memory for one solve and
  restored; the stored project and its RF projection are never written.
  `scope: "prim"` answers 400 (material groups are merged meshes).
- The channel-export noise seed defaults to each result's
  `sensing.noise_seed`, so the exported noise is the noise its CFAR map used.
- The extended echo columns appear only in exports with `include_channel` or
  `include_background_paths`; default exports keep schema version 1.
- The existing CFR helpers (`channel_analysis.compute_cfr`,
  `material_impact._cfr`, `aodt_export._cfr`) are unchanged; new code
  synthesizes through `channel_synthesis.synthesize_cfr`.
- In CFAR mode the measured range and Doppler come from the map, and
  `measurement_noise` is ignored with a warning.
- `detection.mode "cfar"` requires `sensing.waveform`.
- The background ensemble solves with the source run's backend, and a
  realization without a material or position draw (nominal, or CFO only)
  reuses realization 0's paths instead of solving again.
- A moving target re-solves H1 every frame. On the `isac_drone_fr1` demo
  (16 links, mode `both`, CFAR, 8 frames) the defaults (256 paths per link,
  H1 not stored) take 1.2 s per frame for a 4.6 MB result, against 11.1 s
  and 403 MB with every path kept and H1 stored.
