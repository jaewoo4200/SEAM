# SionnaTwin Studio

A local-first, AI-assisted, RF-aware wireless digital twin authoring and
visualization workbench built around [Sionna RT](https://github.com/NVlabs/sionna-rt).

One unified textured 3D scene for the user; internally, every mesh prim
carries **two material bindings** — a visual/PBR material for rendering and an
RF material for electromagnetic simulation. The canonical scene compiles into
a Sionna-compatible RF projection, and ray-path / radio-map results are
visualized back onto the same scene. Everything runs on consumer hardware:
no GPU, no Sionna install, and no LLM required (all three are optional
upgrades, never dependencies).

```text
Unified RF-Visual Scene Graph          (scene.sionnatwin.json - source of truth)
  ├─ Visual Projection  →  GLB / textures / Three.js viewer
  └─ RF Projection      →  PLY material groups + Mitsuba XML → Sionna RT
```

## Repository layout

```text
backend/    FastAPI app: schemas (Pydantic v2), project store, scene validator,
            RF material assignment, RF projection compiler (trimesh),
            simulation backends (Mock + optional Sionna RT), AI providers
frontend/   React + Vite + TypeScript + react-three-fiber workbench
examples/   kaist_demo.sionnatwin demo project + generator script
docs/       architecture, scene format, RF materials, AI assistant, roadmap
HANDOFF.md  operating specification this implementation follows
```

## Requirements

- Python 3.11+ (backend)
- Node.js 20+ (frontend dev/build)
- Optional: [Ollama](https://ollama.com) for local LLM material suggestions
- Optional: `sionna-rt` for real ray tracing (the Mock backend always works).
  Sionna RT runs on Dr.Jit's CUDA backend (NVIDIA GPU) or LLVM backend (CPU);
  at least one must be available or the backend degrades to a warning.

## Setup

```bash
# Backend
python -m venv backend/.venv
backend/.venv/Scripts/pip install -e "backend[dev]"    # Windows
# backend/.venv/bin/pip install -e "backend[dev]"      # Linux/macOS

# Optional: real ray tracing (pulls mitsuba + drjit; ~200 MB)
backend/.venv/Scripts/pip install "sionna-rt>=2.0"

# Frontend
cd frontend && npm install
```

## Run

```bash
# Terminal 1 - backend on :8000
uvicorn --app-dir backend app.main:app --port 8000

# Terminal 2 - frontend on :5173 (proxies /api to the backend)
cd frontend && npm run dev
```

Open http://localhost:5173. The **KAIST Demo** project loads automatically.

## Demo flow (the MVP vertical slice)

1. **Visual mode** — orbit the textured campus scene; click objects in the
   viewer or the scene tree. The inspector shows the visual material and the
   RF material side by side (`window_01` → visual `blue_glass_pbr`, RF
   `unassigned`).
2. **AI Assist mode** — click *Suggest RF materials*. With no Ollama server
   the rule-based provider answers (`window_01 → itu_glass`, confidence 0.9,
   evidence listed). Approve/reject/edit, then *Apply decisions* — nothing is
   ever applied without your explicit action, and every decision is logged to
   `ai/suggestions.jsonl` with provider/model/prompt provenance.
3. **RF Materials mode** — color overlay by RF material; unassigned objects
   glow orange. Batch-assign walls to `itu_concrete`/`itu_brick` from the
   material panel. Assignments persist into the project folder with status
   (`user_confirmed`) and sources.
4. **Validation mode** — run validation: missing RF materials, visual/RF
   contradictions (glass named prim assigned concrete), missing thickness on
   transmissive materials, broken mesh refs, and more.
5. **Compile RF** — groups geometry by RF material (Mode 2), exports
   world-space PLY submeshes to `rf/meshes/`, and generates
   `rf/generated_scene.xml` (Mitsuba 3 XML whose `mat-*` bsdf ids resolve to
   Sionna built-in RadioMaterials) plus `rf/compile_manifest.json` for custom
   constant materials. Deterministic: recompiling an unchanged scene is
   byte-identical.
6. **Results mode** — *Simulate paths* runs the Mock backend (deterministic
   Friis + image-method bounces; no GPU/Sionna needed). Ray polylines overlay
   the 3D scene; the table shows type/power/delay; clicking a path shows its
   vertices and interactions mapped back to canonical prim ids and RF
   materials, plus a delay/power chart.

## Testing

```bash
cd backend && .venv/Scripts/python -m pytest tests -q   # 77 tests
cd frontend && npm run build                            # type-checks + builds
```

Regenerate the demo project (reproducible output):

```bash
backend/.venv/Scripts/python examples/scripts/create_demo_project.py
```

## Key design decisions

- **Dual bindings, one scene.** A prim's `visual` and `rf` blocks are separate
  objects that only meet at the prim. A texture filename is never RF truth —
  AI/rules may cite it as *evidence*, and the assignment then carries
  provenance: `unassigned → rule_suggested / ai_suggested → user_confirmed →
  measurement_calibrated`.
- **snake_case JSON end-to-end**; TypeScript types mirror the wire format
  exactly (`frontend/src/types/api.ts` ↔ `backend/app/schemas/`).
- **Z-up ENU meters everywhere** — scene JSON, GLB vertex data (world
  transforms baked), ray vertices. The viewer sets the camera up-axis instead
  of rotating the model.
- **Stable ids.** Prim ids are path-like (`/buildings/b01/walls`); device ids
  are short (`tx_001`); results always reference canonical prim ids.
- **Backend interface** (`RayTracingBackend`): `mock` is always available;
  `sionna` lazy-imports and degrades to warnings on any failure; `auto`
  resolves to Sionna when installed, else the mock. AODT result import or
  remote solvers slot in behind the same normalized result schemas.
- **Real Sionna RT path.** With `sionna-rt` installed, the compiled
  `generated_scene.xml` loads directly into Sionna RT 2.x: ITU materials
  resolve from the `mat-itu_*` bsdf ids and custom (constant) materials from
  the `radio-material` bsdf plugin. Path and radio-map results are computed on
  GPU (Dr.Jit CUDA) or CPU (Dr.Jit LLVM) and normalized into the same schema
  as the mock, with ray interactions mapped back to canonical RF materials
  (and to a prim when its material group is a single prim).
- **AI provider chain**: forced provider → Ollama text (if reachable) →
  rule-based fallback. Strict JSON schema validation; unparseable output falls
  back with a warning. `SIONNATWIN_AI_ENABLED=off` gives manual-only mode.

Configuration is environment-driven (`SIONNATWIN_PROJECT_ROOTS`,
`SIONNATWIN_OLLAMA_URL`, `SIONNATWIN_AI_TEXT_MODEL`, ...) — see
`backend/app/core/config.py` and `docs/ai_assistant.md`.

## FTC / AODT alignment

Aligned with the `sionna-rt-gui-jaewoo-examples/` reference bundle (a 28 GHz
FTC/lab-room ISAC digital twin):

- **AODT-style viewer** — dark scene, LOS cyan / reflection magenta /
  diffraction orange paths, TX red / UE blue markers, jet radio map, scale-
  aware markers.
- **28 GHz default** with the full **ITU-R P.2040** material set plus a
  `human_body` material (literature presets) for sensing targets.
- **RFData export** (`Export RFData`) writes the AODT viewer contract
  (`scenario_meta / devices / paths / trajectory.csv / radio_map.csv /
  calibration_points`) under `export/rfdata/`.
- **Trajectory metrics** — move an RX along waypoints and get per-point
  RSS / path gain / RMS delay spread (`POST /simulate/trajectory`).
- **Scene import** — `examples/scripts/import_bundle_scene.py` turns the
  bundle's Sionna/Mitsuba XML scenes into loadable projects (ships `lab_room`).
- **MIMO beamforming** (`Beamforming`) — real TX-MRT + both-ends SVD gain from
  the Sionna channel (~12 dB / ~24 dB at 4x4), matching the handoff numbers.

Full ISAC target tracking (PADP/DBSCAN/Kalman) and CV material split
(SAM2/DINOv2) are scoped in `docs/roadmap.md` — the RF/export integration
points exist; the DSP and ML-model inference are external/future.

### vs. the official NVlabs `sionna-rt-gui`

The official tool (Polyscope desktop app) loads a scene, places/animates TX/RX,
and shows paths + a raster radio map; it explicitly does **not** support
mesh radio maps, beamforming, or material editing. SionnaTwin Studio adds, on
top of the same Sionna RT engine: a unified RF-visual scene graph with **dual
material bindings**, RF material **assignment + validation + AI/rule
suggestions**, a **mock backend** (runs with no GPU/Sionna), **MIMO
beamforming** gain, the **RFData export** contract, **trajectory RF metrics**,
**measurement calibration**, and a web UI. The main things their GUI has that
we don't yet: in-viewer **device-trajectory playback** and an interactive
**move-device gizmo** — both tracked in `docs/roadmap.md`.

### Accuracy

RT-vs-measurement error and the mitigations we implement (measurement
calibration, diffuse-scattering coefficients, out-of-band guardrail) plus the
planned differentiable calibration are documented in `docs/accuracy.md`.

## Current limitations (MVP)

- Face-group splitting (Mode 2 sub-mesh granularity) and simplified RF proxy
  meshes (Mode 3) are not implemented; whole named meshes are the unit.
- Per-prim RF parameter overrides are not yet representable in the grouped
  RF export (a compile warning says so).
- The Sionna backend maps ray interactions to RF materials, but a specific
  prim only when its material group holds one prim (Mode 2 merges geometry by
  material). Finer interaction→prim mapping waits on face-group splitting.
- Results persist as JSON (Parquet/Zarr layouts are schema-ready, per
  `docs/roadmap.md`); radio-map visualization in the viewer is basic.
- Measurement calibration, mobility, mesh radio maps, and progressive
  refinement are roadmap items (`docs/roadmap.md`).
