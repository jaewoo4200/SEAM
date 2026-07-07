# SEAM Studio

**SEAM** — Scene-to-Electromagnetic Authoring and Mapping for Wireless Digital Twins

> 🇰🇷 **한국어 README: [README.ko.md](README.ko.md)** · 🌐 Project page: <https://jaewoo4200.github.io/SEAM/en/>

A **local-first RF digital twin workbench on Sionna RT**. In one textured 3D
scene, every mesh carries **two material bindings** — visual/PBR for rendering
and RF for electromagnetic simulation. The canonical scene compiles into a
Sionna-compatible RF projection, and ray paths / radio maps come back as
overlays on the very same viewport.

No GPU, no Sionna, no LLM required — all three are **optional upgrades**; the
built-in **Mock backend runs everything on CPU**.

```text
Unified RF-Visual Scene Graph          (scene.sionnatwin.json - source of truth)
  ├─ Visual Projection  →  GLB / textures / Three.js viewer
  └─ RF Projection      →  PLY material groups + Mitsuba XML → Sionna RT
```

![Hanyang University campus digital twin — aerial-textured import render](website/assets/hero_campus.jpg)

| | |
|---|---|
| ![Sionna RT ray tracing — LOS and reflection paths drawn on the scene](website/assets/rays_demo.jpg) | ![Top-down orthographic campus view](website/assets/campus_top.jpg) |
| ![Sample demo — TX/RX placement with height-above-surface editing](website/assets/sample_demo.jpg) | ![SEAM-Agent multi-view capture of a drone-mapped building](website/assets/ftc_building.jpg) |

---

## Quickstart (3 commands)

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # 1. install + demos
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # 2. run backend + frontend
# 3. open http://localhost:5173 (the Sample Demo loads automatically)
```

**Linux / macOS:**

```bash
bash scripts/install.sh   # 1. install + demos
bash scripts/start.sh     # 2. run backend + frontend
# 3. open http://localhost:5173 (the Sample Demo loads automatically)
```

Manual install, engine options and troubleshooting: **[INSTALL.md](INSTALL.md)**.
A 15-minute first session: **[TUTORIAL.md](TUTORIAL.md)**.

---

## How it differs from the official RT GUI

The official NVlabs `sionna-rt-gui` (a Polyscope desktop app) loads scenes,
places/animates TX/RX and shows paths plus raster radio maps — but material
editing, mesh radio maps and beamforming are explicitly out of scope. SEAM
Studio builds on the same Sionna RT engine and adds:

| Feature | `sionna-rt-gui` (official) | SEAM Studio |
|---|:---:|:---:|
| Paths + raster radio map | ✅ | ✅ |
| Unified RF-visual scene graph (**dual material bindings**) | ❌ | ✅ |
| RF material **assignment + validation + AI/rule suggestions** | ❌ | ✅ |
| **Mock backend** (runs without GPU/Sionna) | ❌ | ✅ |
| **MIMO beamforming** gain (codebook sweep / TX-MRT / SVD) | ❌ | ✅ |
| **Channel analysis** (link budget, CIR/CFR, PL models vs RT, multi-TX **SINR**) | ❌ | ✅ |
| **Trajectory RF metrics** (RSS / path gain / RMS delay / interference·SINR) | ❌ | ✅ |
| **RFData export** (AODT viewer contract) | ❌ | ✅ |
| **ML dataset** generation (npz + metadata) | ❌ | ✅ |
| **Swappable Sionna engine versions** (separate venvs) | ❌ | ✅ |
| Web UI (browser) | ❌ (desktop) | ✅ |
| In-viewer device trajectory playback / move gizmo | ✅ | 🚧 roadmap |

---

## Feature highlights

- **One scene, two materials.** A prim's `visual` and `rf` blocks are separate
  objects that only meet at the prim. A texture filename is never RF truth —
  AI/rules cite it as *evidence* only, and assignments evolve with provenance:
  `unassigned → rule_suggested / ai_suggested → user_confirmed → measurement_calibrated`.
- **Five-mode UI** — Visual / RF Materials / Validation / AI Assist / Results.
  Click an object and its visual + RF materials, assignment sources, validation
  warnings and result overlays all resolve to the same object.
- **Click-to-place & viewport picking** — place TX/RX devices, trajectory
  waypoints and dataset sampling regions by clicking in the viewport instead of
  typing coordinates; scene bounds pre-fill sensible defaults.
- **Dockable panels** — move panels between sidebars or float them over the
  viewport (◧/◨/⧉); a "Panels" launcher opens any panel from any mode.
- **Metrics dashboard + paper-ready export** — link KPIs (RSS/RSRP/RSSI/RSRQ/
  SNR/Shannon capacity/delay spread/Doppler…) and CIR·CFR·Doppler·path-loss
  charts in one panel; every figure is white-background Times New Roman with
  built-in **PNG/SVG/CSV export**. Viewport 📸 (WYSIWYG PNG) / 🎞 (offline
  Mitsuba render) capture the scene itself.
- **Live channel tuning + 3GPP measurements** — adjust frequency/bandwidth/TX
  power/noise figure/SCS live with auto re-analysis, including **TS 38.215-style
  RSRP/RSSI/RSRQ** over the requested OFDM resource grid.
- **Multi-TX co-channel interference (SINR)** — with several TXs, all non-serving
  ray-traced powers sum into co-channel interference: SINR = S/(I+N) feeds RSSI,
  RSRQ and capacity (full-buffer worst case). Works for channel analysis and
  trajectories; the serving cell is selectable.
- **Deterministic Mock backend** — Friis + image-method reflections compute
  example paths/radio maps with no GPU/Sionna, so the frontend and tests run
  anywhere.
- **Real Sionna RT path** — with `sionna-rt` installed (validated on 2.0.x) the
  compiled `generated_scene.xml` loads directly on GPU (Dr.Jit CUDA) or CPU
  (LLVM) and results normalize into the same schema.
- **AODT alignment** — 28 GHz defaults, ITU-R P.2040 material set (+`human_body`),
  AODT-style dark viewer (LOS cyan / reflection magenta / diffraction orange),
  RFData export contract.
- **Optional local AI** — forced provider → Ollama → rule-based fallback chain,
  strict JSON schema validation, suggestions never auto-apply and always leave
  provenance. Multi-view captures and per-prim texture crops sharpen the
  suggestions.
- **Natural-language rules + validation explains** — turn a sentence like
  "windows are glass, concrete walls are itu_concrete" into reviewable
  assignment rules (`/ai/generate-rules` → `/ai/apply-rules`, scene untouched
  until approval), and get plain-language explanations of validation warnings
  (`/ai/explain-validation`).
- **RF disambiguation + material impact** — tell visually identical materials
  apart from measured link path gains (`/calibrate/disambiguate`), and quantify
  how much an assignment matters by comparing against a single-material baseline
  (NMSE / cosine similarity / ΔRSS / capacity, `/analyze/material-impact`).
- **AoA/AoD angle analytics** — every path carries departure/arrival
  `[azimuth, elevation]` plus per-path gain, rendered as paper-style polar
  scatter plots.
- **Mesh radio maps + region refinement** — paint coverage per triangle on real
  surfaces (walls/floors/roads) instead of a horizontal plane, re-solve only a
  region of interest at a finer cell size, with multi-TX `sinr_db` maps and a
  per-cell **serving-TX** map.
- **Accuracy presets** — pick a representative deployment (28 GHz indoor/outdoor,
  3.5 GHz urban macro, 60 GHz indoor) and every solver knob snaps to a vetted
  configuration.
- **Result reproducibility + live events** — every result is stamped with
  `scene_hash`/`rf_assignment_hash`/`sim_config_hash` + a config snapshot, so
  stale results get badges after any scene/assignment change; a WebSocket
  streams compile/simulation progress.
- **External results & measurements** — import NVIDIA AODT parquet results into
  the same schema (`/results/import-aodt`) and measured link CSVs
  (`/calibrate/measurements/import-csv`) for calibration and disambiguation.
- **Scene bundle import (zip / OSM)** — import a whole scene folder (XML +
  meshes + textures) as one zip with relative paths preserved; textures persist
  both viewer-side (GLB) and full-resolution for AI evidence. Or pull real
  buildings from OpenStreetMap by dragging a rectangle on a map (or searching).
- **Material segmentation + connected-parts split** — split a monolithic
  building mesh into per-material faces from a texture mask (color heuristic /
  local-VLM tile vote / uploaded SAM2-grade mask), or split a merged multi-
  building mesh into its connected components. Every split keeps a GLB backup
  and is **undoable**.
- **SEAM-Agent (retrieval-augmented local AI material authoring)** — give one
  hint like "this is the Hanyang FTC building" and the agent retrieves real
  exterior photos from the web, fuses them with multi-view mesh observations
  (triangle-id back-projection), and proposes per-segment RF materials
  (wall/window/roof/frame) with confidence and evidence cards. Everything is an
  observable activity trace, and nothing applies without your approval.
- **Blender-grade viewport** — zoom-to-cursor, 1/3/7 view snaps, orbit around
  selection, infinite grid, distance fog, and an unlit-texture toggle for
  photo-textured scenes.
- **Terrain following** — UE trajectories drape onto terrain/rooftops (no more
  tunneling through hills), and the device inspector's **height-above-surface
  (AGL)** field places a device "N meters above whatever is below it" in one
  step.
- **AI model picker** — models loaded in LM Studio / Ollama are auto-discovered
  and switchable in the UI; the responding model is recorded in provenance.

See [TUTORIAL.md](TUTORIAL.md) for the full demo flow.

---

## Programmatic API (endpoints without UI buttons)

Most features are driven from the web UI; these two endpoints are meant for
curl/scripts (backend defaults to `http://127.0.0.1:8000`):

- **`POST /api/projects/{id}/live/state`** — **inject real-world positions.**
  Push device/actor positions from GPS/mocap/logs into the loaded scene. The
  UI's *Live sync* polling mirrors this state, so a steady stream of posts makes
  the viewer follow in real time. `persist=true` writes to the scene,
  `resimulate=true` re-solves paths immediately for a
  measure → sync → predict loop.

- **`POST /api/projects/{id}/calibrate/materials`** — **measurement-driven
  material calibration.** Provide measured per-link path gains and one RF
  material parameter is grid-search fitted to reduce RT-vs-measurement error,
  returning a before/after report. With `apply=true` the fitted value is written
  to the library and affected prims are promoted to `measurement_calibrated`.

---

## Docs index

| Doc | Contents |
|---|---|
| [INSTALL.md](INSTALL.md) | prerequisites, install (scripts/manual), engine & LLM options, troubleshooting |
| [TUTORIAL.md](TUTORIAL.md) | 15-minute first session (scene → materials → sim → dataset) |
| [docs/architecture.md](docs/architecture.md) | unified scene graph & dual-projection architecture |
| [docs/scene_format.md](docs/scene_format.md) | scene/project folder format and schemas |
| [docs/rf_materials.md](docs/rf_materials.md) | RF material library and models |
| [docs/ai_assistant.md](docs/ai_assistant.md) | AI suggestion providers, rules, provenance |
| [docs/engines.md](docs/engines.md) | swapping Sionna engine versions (separate venvs) |
| [docs/rtgui_parity.md](docs/rtgui_parity.md) | NVlabs Sionna RT GUI feature-parity matrix |
| [docs/model_validation.md](docs/model_validation.md) | verification of every implemented comms model |
| [docs/ml_datasets.md](docs/ml_datasets.md) | ML ground-truth dataset format & training examples |
| [docs/extending.md](docs/extending.md) | plugin architecture & extension guide |
| [docs/accuracy.md](docs/accuracy.md) | RT-vs-measurement error and mitigations |
| [docs/roadmap.md](docs/roadmap.md) | post-MVP roadmap and extension points |
| [HANDOFF.md](HANDOFF.md) | the operating specification this implementation follows |

---

## Architecture (one-liner)

A canonical Pydantic v2 scene (`scene.sionnatwin.json`) is the single source of
truth; the FastAPI backend compiles it into Visual (GLB) and RF (Mitsuba XML +
PLY groups) projections, and the React + react-three-fiber frontend mirrors the
snake_case wire format, drawing results back onto the same Z-up ENU-meters
scene.

**Stack:** Python 3.11+ / FastAPI / Pydantic v2 / NumPy / trimesh backend;
React + Vite + TypeScript + react-three-fiber + Zustand frontend; optional
`sionna-rt` (Dr.Jit / Mitsuba 3) backend.

---

## Testing

```bash
backend/.venv/bin/python -m pytest backend/tests -q   # backend unit tests
cd frontend && npm run build                          # typecheck + build
```

(Windows: `backend\.venv\Scripts\python.exe -m pytest backend\tests -q`)

---

## License / Credits

Distributed under the [Apache License 2.0](LICENSE) (third-party notices:
[NOTICE](NOTICE)). Built on [Sionna RT](https://github.com/NVlabs/sionna-rt)
(NVlabs); AODT viewer alignment follows the `reference-bundle/` reference
bundle (28 GHz FTC / lab-room ISAC digital twins).

**Map data attribution** — the OSM import fetches building footprints via the
[Overpass API](https://overpass-api.de/) and geocoding via Nominatim. That data
is **© OpenStreetMap contributors**, licensed under
[ODbL 1.0](https://www.openstreetmap.org/copyright) — scenes built with the OSM
import carry the same attribution requirement when redistributed. The import
dialog's map uses [Leaflet](https://leafletjs.com/) (BSD-2) with standard OSM
tiles.

Developed by **Jaewoo Lee (이재우)** at the
**Wireless Systems Laboratory (WSL), Hanyang University** ·
**BEYOND-G Global Innovation Center**.
GitHub: <https://github.com/jaewoo4200/SEAM>
