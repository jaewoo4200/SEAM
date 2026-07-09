# 15-Minute First Session Tutorial (TUTORIAL)

> 🌐 **English** · [한국어](TUTORIAL.ko.md)

A hands-on guide that takes a first-time SEAM Studio user through a full loop
**in 15 minutes**: scene exploration → material assignment → path
simulation → radio map → beamforming → channel analysis → device movement →
actor scenarios → trajectory live rays → ML dataset generation.

Before you start, finish the installation via [INSTALL.md](INSTALL.md) and bring up the server:

```bash
# Windows
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# Linux/macOS
bash scripts/start.sh
```

> The **button/tab names in this tutorial are exactly the real UI labels** (e.g. `Compute paths`,
> `Beamforming`, `Analyze`, `Simulate scenario`). The entire flow works
> with the Mock backend alone, so you can follow along without GPU/Sionna.

---

## 0. Open the app (1 min)

Open **http://localhost:5173** in your browser. On the left of the top toolbar
there is the **SEAM Studio** title and a project select, and the **Sample Demo**
project loads automatically. (You can also switch to `Lab Room` or `FTC Outdoor` from the select.)

Check the two status chips on the right of the toolbar:

- **Sionna** / **Mock only** — ray tracing backend availability.
- Provider name / **AI off** — AI suggestion provider status.

Even if both are Mock/off, you can complete the entire tutorial.

---

## 1. Tour the five modes (2 min)

There are 5 **mode tabs** in the center of the toolbar. Click through them and
watch how the left/right panels change.

| Tab | What it does |
|---|---|
| **Visual** | Orbit/pan/zoom the textured 3D scene, object picking, scene tree, inspector. |
| **RF Materials** | Color overlay per RF material. Unassigned objects glow in a warning color (orange). Assign/bulk-assign via dropdown. |
| **Validation** | Scene validation warnings (unassigned RF materials, visual/RF contradictions, missing thickness, invalid mesh references, etc.). |
| **AI Assist** | Propose material candidates with *Suggest RF materials* → approve/reject/edit → *Apply decisions*. |
| **Results** | All simulations: paths/radio map/beamforming/channel/trajectory/scenario/ML dataset, etc. |

In **Visual** mode, take the scene for a spin: left-drag the mouse to orbit,
scroll wheel to zoom. Click the window (`window_01`) and the inspector shows the
**visual material (`blue_glass_pbr`)** and the **RF material (unassigned)** side by side.

### Dockable panels

Small buttons on the right of each panel header let you rearrange panel layout:

- **◧ / ◨** — move the panel to the left/right sidebar (dock to the other sidebar).
- **⧉** — detach the panel into a **floating window (float)** hovering over the
  viewport. Press again to return it to the sidebar. Floating windows can be dragged around.

A floated panel **stays put even when you switch mode tabs** — for example, you can
float the Simulation panel and keep it visible while moving between Visual↔Results.
Useful on a narrow screen when you want to lay out only the panels you need, or when
you want to view a results table in a large floating window.

---

## 2. Assign one material (2 min)

1. Switch to the **RF Materials** tab. Objects that don't yet have an RF material
   (building walls, windows, etc.) are highlighted in orange.
2. In the scene, click **`/buildings/b01/walls`** (building 1 walls) or select it from the scene tree.
3. In the dropdown of the RF material panel on the left, pick a material that fits the wall (e.g. `itu_concrete`).
4. Once assigned, the overlay color changes immediately, and the inspector records the status as
   **`user_confirmed`** with the source noted as user-assigned. The value is
   saved permanently to the project folder.

> To assign multiple objects at once, multi-select in the scene tree and then bulk-assign.

(Optional) In the **AI Assist** tab, pressing *Suggest RF materials (all unassigned)*
makes a rule-based provider propose candidates (e.g. `window_01 → itu_glass`, with evidence shown).
On each card, only after you **Approve / Reject / Edit** and then press **Apply decisions (N)**
does it actually apply. Nothing is auto-applied.

**(Optional) Bulk-assign with a natural-language rule.** Instead of getting a proposal
one prim at a time, if you put a **single natural-language instruction** such as "windows are glass,
walls whose name contains concrete are itu_concrete" into the rule input of AI Assist
(`POST /api/projects/{id}/ai/generate-rules`), it turns into a reviewable list of
**assignment rules**. Each rule is of the form `strings to include in name → RF material id`
(case-insensitive substring match), and rules pointing to a material not in the project library
are discarded with a warning (the model cannot create a material that doesn't exist). After you
check and edit the rules, pressing **Apply rules** (`POST …/ai/apply-rules`) brings the matched
prims into the **exact same review/apply screen** as ordinary proposals, with `rule_assigned` as the
rationale, and the scene does not change until you approve. Prims you already **rejected** are kept
as-is and are not proposed again.

**(Optional) Get an explanation of validation results.** If you're unsure what the warnings from
**Validate** mean and what to do about them, press *Explain validation* (`POST …/ai/explain-validation`).
It runs scene validation and then explains each issue in plain language (e.g. "3 prims are unassigned,
and at 28 GHz the ITU ground material is out of band, so change it to `ground_28ghz`"). It is a
read-only feature that never changes the scene, and each issue comes with `suggested_actions`
(recommended actions) that the UI shows with one click. If there is no AI server, the rule-based
provider returns a template explanation built from the issue code.

**(Optional) Import a measurement CSV.** Paste measured link data (receive position + measured path gain)
as CSV and import it via `POST /api/projects/{id}/calibrate/measurements/import-csv` to parse it into a
`MeasurementSample` list (with the number of skipped rows and warnings), which you can later query again
via `GET …/calibrate/measurements`. These measurements become the input for the material calibration
and RF disambiguation above.

**(Optional) RF disambiguation — distinguishing visually identical materials via measurements.**
Materials that look identical to the eye, like glass, yet differ greatly in RF penetration loss
(about 2.5–23.6 dB at mmWave) cannot be told apart by camera/texture name alone. If you feed in
a few candidate materials and the measured per-link path gain and call
`POST /api/projects/{id}/calibrate/disambiguate`, it binds each candidate to the relevant prim,
re-simulates, and returns the candidate with the lowest RMSE against the measurements
(`best_material_id`). If the RMSE difference between candidates is less than 0.05 dB, it judges
that location as indistinguishable, leaves `best_material_id` empty, and returns an "indistinguishable"
warning (add a measurement closer to the prim). The actual material separation happens on the Sionna
backend; the mock is for testing the flow.

**(Optional) Impact evaluation — how important is this material to the link.**
`POST /api/projects/{id}/analyze/material-impact` solves the same TX→RX in both the assigned-material
scene and a single baseline-material scene (`baseline_material_id`, default `itu_concrete`) and returns
per-location **NMSE / cosine similarity / dRSS / capacity (Mbps)** (KICS 2026). If NMSE is close to 0 dB
and dRSS is large, that location means getting the material right matters, whereas if NMSE is very low and
cosine≈1·dRSS≈0, it means the geometry/LoS dominates the link and the material effect is small
(locations exceeding `sensitive_nmse_db`, default −60 dB, are marked material-sensitive).

---

## 3. Path simulation (Simulate Paths) (2 min)

1. Press the blue **Simulate Paths** button on the right of the toolbar. (Or **Results** mode
   → **Simulation** panel on the right → **Compute paths** in the **Paths** section.)
2. After a moment, TX→RX ray polylines are overlaid on the 3D scene. AODT-style legend:
   **LOS cyan / reflection magenta / diffraction orange**, TX red / RX blue markers.
3. The results table below shows **type / power / delay** per path. Clicking a path
   maps and displays the vertices and interactions to canonical prim ids and RF materials,
   and a delay/power chart appears.

In the **Global** section of the **Simulation** panel you can adjust **Backend** (auto/mock/sionna),
**Frequency** (default 28 GHz), **Seed**, and so on. The **Paths** section has
**Max depth**, a **Samples / it (log 10)** slider, and mechanism checkboxes (Line of
sight, Specular reflection, Diffuse reflection, Refraction, Diffraction, Edge
diffraction, Lit-region diffraction). To clear the rays, use **Remove**.

**Accuracy presets (Preset).** Instead of tweaking solver knobs one by one, picking a
representative deployment scenario from the **Preset** dropdown sets the relevant knobs at once:
**28 GHz Indoor Lab** (depth 5, reflection+refraction+scattering, 0.25 m grid), **28 GHz Outdoor Campus**
(depth 3, reflection+scattering, 2 m), **3.5 GHz Urban Macro** (depth 4, reflection+refraction+diffraction, 5 m),
**60 GHz Indoor** (depth 4, reflection+refraction, 0.25 m). A preset changes both the path settings and the
radio-map grid together, and does not touch the backend/TX/RX selection. If you change a knob by hand it
automatically switches to **Custom**. A preset is only a starting point, not the right answer, so if you have
measurements, reduce the residual with material calibration.

---

## 4. Overlay toggles & radio map (2 min)

**Overlay toggles:** In the viewport panel, toggle display options (paths/markers/radio map, etc.)
on and off and see how the scene looks. To reset lighting/display, press the reset button in the viewport panel.

**Radio map:** **Results** mode → **Simulation** panel → in the **Radio map** section press
**Compute radio map**. Received strength over the grid is laid onto the surface with a jet colormap.
Adjustable options:

- **Cell size** (m) — grid cell size,
- **Height** (m) — measurement plane height,
- **Metric** — `path_gain_db`, `rss_dbm`, or `sinr_db`.

To clear it, use **Remove**.

**Multi-TX SINR·serving-cell map.** If there are multiple TX in the scene, set **Metric** to `sinr_db`
to draw a true-SINR (`S/(I+N)`) map that reflects co-channel interference. In this case the result also
carries the **serving TX** of each cell (the strongest TX at that cell) (`serving_tx`), so you can
color-distinguish which cell is served by which base station (in a single-TX scene, SINR falls back to SNR).

**Region refinement.** If you want to look at just a region of interest more finely, you don't need to
re-solve the whole map — specify that region's center/size (`center_xy`/`size_xy`) and recompute with a
smaller **Cell size**. Cells that weren't computed are left blank rather than fabricating a value.

**Mesh radio map (surface coverage).** To paint coverage **onto actual wall/floor/road surfaces** instead
of a horizontal plane, select the target prims and run **Mesh radio map**
(`POST /api/projects/{id}/simulate/mesh-radio-map`). It places a probe receiver at each triangle's center,
lifted slightly along the face normal, and solves; if the triangles exceed the budget (`max_triangles`,
default 2000) it samples one every k. The 3D view drapes the color directly over the facades/floors, so
you can spot coverage blind spots right on the surface.

---

## 5. Beamforming — codebook sweep (Beamforming) (2 min)

1. In the **Global** section of the **Simulation** panel, set the **Beamforming array**
   (**TX rows × cols**, **RX rows × cols**; e.g. 4 × 4).
2. Setting **Mode** to **codebook sweep** reveals the **Sweep start / stop / step** (°) fields.
   It sweeps the angle range to find the best beam. (Other modes: **TX-MRT**, **SVD**.)
3. Press the **Beamforming** button. (Also possible via toolbar **Actions ▾ → Beamforming**.)
4. The result gives the TX-MRT / both-ends SVD beamforming gains (about 12 dB / about 24 dB
   at 4×4). The codebook sweep heatmap lets you see the gain distribution by angle.

---

## 6. Channel analysis (Analyze) (1 min)

In the **Channel** panel of **Results** mode:

1. Pick **TX** and **RX** and set **CFR points** (the number of frequency-response samples).
2. Press **Analyze**.
3. **Link budget**, **CIR (power delay profile)**, **CFR magnitude**, and a
   **Path-loss models vs RT** (path-loss models vs ray tracing) comparison table
   appear. To clear the results, press the channel clear button.

### Live parameters — adjust parameters instantly → auto re-analysis

In the **Live parameters** section of the Channel panel, you can directly adjust the following
values via sliders/inputs: **Frequency (GHz)**, **Bandwidth (MHz)**, **TX power (dBm)**,
**Noise figure (dB)**, **SCS (kHz)** (subcarrier spacing; 15=LTE, 30=5G FR1 default,
60/120 also selectable). When you change a value, **if the current TX↔RX pair is already analyzed,
it re-analyzes automatically** (debounced), and the Link budget and metrics refresh immediately. Next to
SCS, the **N_RB** (number of resource blocks = ⌊BW/(12·SCS)⌋) at the current bandwidth is also shown.
The **Reset** button restores these values to the current solver settings and TX device values.

### 3GPP measurement metrics (one-line summary)

Along with RSS·SNR·SINR·path loss, the Link budget shows 3GPP TS 38.215 style measurement quantities:

- **RSRP** (Reference Signal Received Power): average received power per resource element (RE) =
  `RSS − 10log10(N_sc)`. The wideband RSS distributed evenly over the occupied subcarriers.
- **RSSI** (Received Signal Strength Indicator): total in-band received power = signal+interference+noise
  linear sum (in a multi-TX scene it also includes interference power).
- **RSRQ** (Reference Signal Received Quality): link quality = `N_RB·RSRP/RSSI`;
  the signal-dominated upper bound ignoring interference/noise is `10log10(1/12) = −10.79 dB` (with interference, below that).

### Multi-TX co-channel interference (SINR)

If there are multiple TX in the scene, it takes **the sum of the ray-traced received power that every TX
other than the serving TX creates at the RX** as co-channel interference `I`. In this case the Link budget's
**SINR = S/(I+N)** becomes lower than the noise-only SNR, the **interference power** and the
**number of interferers (num_interferers)** are also shown, and RSSI·RSRQ·Shannon capacity all reflect this
interference. If there is no interfering TX, it falls back to `SINR = SNR`. It assumes a
**full-buffer worst case** where all interfering TX transmit simultaneously on the same resources
(no scheduler/partial-load model), and the serving cell can be changed by TX selection.

---

## 6.5 Metrics dashboard + exporting figures for papers (2 min)

### Metrics dashboard panel

The **Metrics dashboard** panel (dockable — move to sidebar/float with ◧/◨/⧉) shows **all metrics of the
last channel analysis at a glance**. At the top a KPI grid (RSS/RSRP/RSSI/RSRQ/path loss/SNR/Shannon
capacity/K-factor/delay spread/Doppler/N_RB @ SCS, etc., each cell with a definition tooltip), and below it
**Power-delay profile (CIR)**, **CFR magnitude**, **Doppler fading envelope**, and **Path-loss model
comparison** charts are laid out.

- All figures render in a **white background · Times New Roman (serif) paper style**.
- Each chart frame has built-in **PNG / SVG / CSV export** buttons, so you can save the figure directly
  as a bitmap/vector or extract the raw data as CSV.
- There is also an **export-all** button that gets the entire KPI table as a `metric,value,unit` CSV.

### Viewport export buttons — 📸 vs 🎞

The two buttons on the right of the viewport save scene images:

- **📸** — **save PNG as you see it (WYSIWYG).** Exports the current screen (including rays·markers·overlays)
  at the full canvas resolution. Suitable for paper/slide snapshots.
- **🎞** — **Mitsuba offline render.** Renders (slowly, but) a physically shaded image separately via
  physically based path tracing. Note that this is not the real-time view you see on screen but an
  offline render result.

---

## 7. Move a device → auto update (1 min)

1. Select **`rx_001`** (the receiver) in the scene tree or viewport.
2. Edit the **X / Y / Z** position fields in the inspector and save.
3. If you keep the **Auto update** checkbox (present in each of the Paths / Radio map / Beamforming sections)
   on, the corresponding results are automatically recomputed every time you move the device.

> Note: the in-viewport drag gizmo is a roadmap item; for now use the inspector's
> position fields for precise movement.

---

## 8. Actors + scenario playback (Simulate scenario) (1 min)

The Sample Demo has moving **actors**: a **car (car_001)** driving on the road and a
**pedestrian (human_001)** walking in front of the building. Each actor is compiled into
its own RF geometry and moves per frame.

1. In **Results** mode, go to the **Scenario (V2X)** section.
2. Set **Num frames** (e.g. 20), **dt** (s), and if needed **Include paths (per frame)**.
3. Press **Simulate scenario**.
4. With the playback transport (▶ / ⏸, frame slider, ⟳ loop, speed 0.5×–4×), play the timeline
   and check the per-frame **Link metrics** (RSS / SINR / number of paths).

---

## 9. Trajectory live rays (Simulate trajectory) (1 min)

1. In the **Trajectory** section of **Results** mode, set the movement segment (**start / end**),
   **num_points**, **dt**. You can enter start/end directly as numbers, or press
   **🎯 Pick start → end in viewport** to specify them by **clicking two points in the viewport** —
   the first click becomes start, the second becomes end, and you can plot the path while looking
   at the scene geometry as if drawing it.
2. While picking, a **dashed preview line** connecting start→end is drawn in the viewport
   so you can visually confirm the actual movement segment.
3. (Optional) Turning on the **Follow terrain** checkbox snaps each waypoint's z to the terrain
   surface below it and then raises it by the height offset — use it to keep the RX height constant
   on sloped outdoor terrain (turn it off for indoor scenes).
4. Press **Simulate trajectory**.
5. As the RX moves along the waypoints, the per-point **RSS / path gain / RMS delay spread**
   is computed, and playing with ▶ updates the rays in real time along the trajectory.
   If there are multiple TX in the scene, the per-point **interference / SINR = S/(I+N)** also appears
   (the received power of every TX other than the serving TX is co-channel interference, full-buffer assumption),
   and the serving cell is designated as the **serving TX** (the first TX if unspecified).

(Optional) Turning on **Live sync** in the **Global** section polls the scene every 2 seconds and
reflects the device/actor positions in the viewer.

---

## 10. ML dataset generation (Generate dataset) (1 min)

1. In the **ML dataset** section of **Results** mode, set **Name**, **Sampling mode**
   (`random` / `grid` / `trajectory`), **Num samples**, **CFR points**,
   **Height**, and so on.
2. Press **Generate dataset**.
3. When generation finishes, the dataset list shows the name/number of samples/creation time/size
   and download links (**npz**, **json**).

### Where files are saved

Outputs accumulate under the project folder (e.g. `examples/demo_project/sample_demo.sionnatwin/`):

| Artifact | Path |
|---|---|
| ML dataset | `export/datasets/<dataset_id>/dataset.npz` + `metadata.json` |
| RFData export (toolbar **Actions ▾ → Export RFData**) | `export/rfdata/` (scenario_meta, devices, paths, trajectory.csv, radio_map.csv, calibration_points) |
| Path results | `results/{backend}_paths_{NNN}.json` (e.g. `results/mock_paths_001.json`, `results/sionna_paths_001.json`) |
| Compiled RF projection (**Actions ▾ → Compile RF**) | `rf/generated_scene.xml`, `rf/meshes/`, `rf/compile_manifest.json` |
| AI suggestion log | `ai/suggestions.jsonl` |

---

## Next steps

- Swapping the engine version (per-Sionna-RT-version paths solve): [docs/engines.md](docs/engines.md)
- Feature/material/model evolution by Sionna version: [docs/sionna_versions.md](docs/sionna_versions.md)
- Limits of RT accuracy and mitigations (measurement calibration, diffuse scattering, etc.): [docs/accuracy.md](docs/accuracy.md)
- Architecture / scene·project format: [docs/architecture.md](docs/architecture.md),
  [docs/scene_format.md](docs/scene_format.md)
- RF material library and AI assistant: [docs/rf_materials.md](docs/rf_materials.md),
  [docs/ai_assistant.md](docs/ai_assistant.md)
- Roadmap (mesh radio map, mobility, measurement calibration, extension points): [docs/roadmap.md](docs/roadmap.md)
