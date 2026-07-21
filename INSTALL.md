# Installation Guide (INSTALL)

> **English** · [한국어](INSTALL.ko.md)

SEAM Studio is a **local-first** workbench. A GPU, a Sionna
installation, and an LLM are all **not required** — all three are merely optional
upgrades, and the default **Mock backend always runs on CPU alone**. Start by bringing
up the app with the basic install, and attach the real Sionna RT engine and a local LLM
when you need them.

- Just want to run it → [Route A — pip install](#route-a--pip-install-seam-studio-no-source-checkout)
- Developing / using the repo examples → [Route B — Quick Install from source](#route-b--quick-install-from-source-one-line)
- For the first-15-minutes usage → [TUTORIAL.md](TUTORIAL.md)
- For the project intro/structure → [README.md](README.md)

---

## Prerequisites

**Required (absolutely needed to run the app):**

| Item | Required version | Notes |
|---|---|---|
| Python | **3.11 / 3.12 / 3.14** | Backend (FastAPI). Check with `python --version`. 3.11 is exercised by CI on Linux, 3.12 is the everyday dev interpreter, and 3.14 passed a clean-venv install + `sionna.rt` load on Windows (2026-07-15). **3.13 is untested** but its Mitsuba/Dr.Jit wheels exist, so it is expected to work |
| Node.js | **20 or higher** (18+ generally works too) | Frontend (Vite). Check with `node --version`. Not preinstalled on most machines — Windows: `winget install OpenJS.NodeJS.LTS`, macOS: `brew install node@20`, Ubuntu: NodeSource 20.x. Open a **new** terminal after installing. Only needed for this source-checkout route; the pip package ships a pre-built frontend and needs no Node |
| OS | Windows 10/11, Linux, macOS | Scripts are provided for both Windows (PowerShell) and Unix (bash) |

> `python` and `npm` **must already be on PATH** (the install script aborts immediately if they are missing).
> The script does not install a portable runtime. `git` is only needed to clone the repo and
> is not a runtime dependency.

**Optional (upgrades — the entire workflow works with the default Mock backend even without them):**

| Item | What | When needed |
|---|---|---|
| **`sionna-rt` package** | The real ray tracing engine (includes Mitsuba 3 / Dr.Jit, several hundred MB) | **Installed automatically** — it is a base dependency of both the source install and the pip package (the old `backend[sionna]` extra remains as a no-op alias). Verified version `sionna-rt 2.0.x`. → [section below](#the-real-sionna-rt-engine-installed-automatically) |
| **NVIDIA GPU + driver** | CUDA (Dr.Jit) acceleration for `sionna-rt` | An additional layer *on top of the package install*. Without it Sionna runs on CPU/LLVM (works fine, just slower). macOS has no Metal/MPS backend, so it is **always CPU/LLVM** |
| **Local LLM server** | LM Studio (`:1234`) or Ollama (`:11434`) + a (VLM) model | For AI material assist / SEAM-Agent. Falls back to rule-based without it. → [Local LLM setup](#optional-local-llmvlm--ai-material-suggestions) |

> **Key point:** All three are **optional**. The Mock backend
> always runs on CPU alone with no installation, computing deterministic example paths/radiomaps.
> The real gate for "real ray tracing" is
> **installing the `sionna-rt` package, not a GPU**, and the GPU is an
> additional acceleration layer on top of it.
>
> **Native libraries:** Among the base dependencies, `rtree` (libspatialindex) and `shapely` (GEOS)
> use C libraries, but on mainstream Windows/Linux/macOS environments they are **bundled into the
> PyPI wheels**, so no separate system installation is needed. A system GEOS/libspatialindex is only
> needed when building from source on an unusual environment (non-mainstream architecture) where a
> wheel is missing. **External executable binaries such as ffmpeg are not needed at runtime at all.**
>
> **Disk headroom:** Beyond the basic install, `sionna-rt`+Mitsuba/Dr.Jit ≈ several hundred MB, (optional)
> FTC material overlay ≈ 120 MB, (optional) `reference-bundle/` original scene assets ≈ 450 MB (not
> included in git).

---

## Route A — `pip install seam-studio` (no source checkout)

The shortest path if you just want to **run** the app: no repo clone and **no
Node.js** (the wheel ships a pre-built UI, and Sionna RT installs as a base
dependency).

**Windows (PowerShell):**

```powershell
py -3.12 -m venv seam-env
seam-env\Scripts\pip install seam-studio
seam-env\Scripts\seam-studio         # serves + opens http://127.0.0.1:8000
```

**Linux / macOS:**

```bash
python3.12 -m venv seam-env
seam-env/bin/pip install seam-studio
seam-env/bin/seam-studio             # serves + opens http://127.0.0.1:8000
```

The first run creates `~/.seam/projects/` and seeds the **Sample Demo**
project, then opens the browser. Useful flags: `--port N`, `--project-root DIR`,
`--no-browser`.

Differences vs the source-checkout route (Route B below):

- Only the generated Sample Demo is preseeded — the Lab Room / FTC Outdoor
  examples ship with the repo checkout, not the wheel.
- Everything anchors to `~/.seam/` instead of the repo: projects in
  `~/.seam/projects/`, the optional multi-engine registry at
  `~/.seam/engines.json` (engine venvs work the same; the worker script is
  bundled in the package).
- The UI is served by the backend on one port (no separate Vite dev server),
  and modifying the frontend requires the source route.

Upgrade with `pip install -U seam-studio`.

## Route B — Quick Install from source (one line)

Run from the repository root. It handles venv creation → backend/frontend install → demo
project creation all at once, and is **safe to run multiple times (idempotent)**.

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

**Linux / macOS:**

```bash
bash scripts/install.sh
```

Once the install finishes, move on to [Run the server](#run-the-server).

---

## Manual Install (step by step)

To install directly instead of using the script, follow the steps below. All commands are
relative to the **repo root**.

### 1. Create the backend venv + install

This project manages dependencies with **`backend/pyproject.toml`**, not a `requirements.txt`.
It performs an editable install (`-e`) along with the `dev` extra (which includes pytest).

**Windows:**

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install --upgrade pip
backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
```

**Linux / macOS:**

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install --upgrade pip
backend/.venv/bin/python -m pip install -e "backend[dev]"
```

Base dependencies (per `backend/pyproject.toml`): `fastapi`, `uvicorn[standard]`,
`pydantic`, `numpy`, `scipy`, `trimesh`, `rtree`, `shapely`, `mapbox-earcut`,
`pyyaml`, `httpx`, `pillow`, `ddgs`, `python-multipart`. `rtree` (libspatialindex) and
`shapely` (GEOS) use native C libraries, but on mainstream platforms they are bundled into the
PyPI wheels, so no separate installation is needed.

### 2. Install the frontend

```bash
cd frontend
npm install
```

### 3. (Optional) Regenerate the demo projects

The generated artifacts of the 3 demos (**sample_demo · lab_room · ftc_outdoor**) are already
**committed** to the repo, so they appear in the app right after install. This step is not required
and is only run when you want to rebuild the demos from scratch. (The one-line install script also calls
these scripts, but the bundle import below only runs when `reference-bundle/` is present, and
otherwise warns and skips it — using the committed demos as-is.)

`create_demo_project.py` always works without the bundle. The `import_bundle_scene.py`
family is only needed when `reference-bundle/` (the large scene assets, ~450 MB, not included in git) is
present at the repo root, and is only used when you download the bundle separately and want to
re-import/regenerate it.

**Windows:**

```powershell
backend\.venv\Scripts\python.exe examples\scripts\create_demo_project.py
backend\.venv\Scripts\python.exe examples\scripts\import_bundle_scene.py
backend\.venv\Scripts\python.exe examples\scripts\import_bundle_scene.py --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb"
```

**Linux / macOS:**

```bash
backend/.venv/bin/python examples/scripts/create_demo_project.py
backend/.venv/bin/python examples/scripts/import_bundle_scene.py
backend/.venv/bin/python examples/scripts/import_bundle_scene.py --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb"
```

> **Note:** The second and third commands above (`import_bundle_scene.py`) require `reference-bundle/`
> to be present at the repo root. Without it, run only `create_demo_project.py`
> — lab_room and ftc_outdoor remain in their already-committed state.

- `create_demo_project.py` → **sample_demo** (a small outdoor urban scene: ground/road/2 buildings
  +windows/trees, TX/RX, vehicle·pedestrian actors). Written under `examples/demo_project/`.
- `import_bundle_scene.py` (no args) → **lab_room** (imports the reference bundle's indoor 28 GHz lab-room
  scene into a loadable project).
- `import_bundle_scene.py --scene-id ftc_outdoor …` → **ftc_outdoor** (the reference bundle's
  outdoor 28 GHz FTC scene + reconstructed-map overlay). You can switch to it as `FTC Outdoor`
  in the project select.

> **Note:** The FTC overlay GLB (`FTC_OSM_ReconstructedMap_ZUp_v2.glb`) is large
> (about 120 MB). On import it is copied to the project's `visual/overlay.glb`, so
> disk headroom is needed, and this single line takes somewhat longer than the other two commands.

Both scripts print a result summary (prim/device counts, material list, GLB mesh names), and
regenerating the same scene produces identical output.

---

## Run the server

### With the script (recommended)

**Windows:**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

**Linux / macOS:**

```bash
bash scripts/start.sh
```

It brings up the backend (:8000) and the frontend (:5173) together and prints the URLs.

### Manually (2 terminals)

**Terminal 1 — backend (:8000):**

```bash
backend/.venv/bin/python -m uvicorn --app-dir backend seam_studio.main:app --port 8000
```

(Windows: `backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend seam_studio.main:app --port 8000`)

**Terminal 2 — frontend (:5173):**

```bash
cd frontend
npm run dev
```

Open **http://localhost:5173** in your browser. The Vite dev server proxies `/api` requests
to `http://127.0.0.1:8000`, so no CORS configuration is needed.
The **Sample Demo** project loads automatically.

You can check the backend status at http://127.0.0.1:8000/api/health (including Sionna·AI
provider availability).

---

## The real Sionna RT engine (installed automatically)

`sionna-rt` (includes Mitsuba 3 / Dr.Jit, several hundred MB) is a **base
dependency** — both Route A and Route B install it for you; there is nothing
extra to run. The verified version is `sionna-rt 2.0.x`, and the old
`backend[sionna]` extra remains as a harmless no-op alias.

When Sionna loaded correctly, the status chip at the top-right of the toolbar shows
**Sionna** (instead of **Mock only**) and you can choose `auto`/`sionna` in the Simulation
panel's **Backend** select. If the import ever breaks (e.g. an unsupported Python/wheel
combination), the app emits a warning and keeps running on the Mock backend — to repair,
reinstall into the backend venv:

```powershell
# Windows                                   # Linux/macOS
backend\.venv\Scripts\python.exe -m pip install --force-reinstall "sionna-rt>=2.0"
backend/.venv/bin/python -m pip install --force-reinstall "sionna-rt>=2.0"
```

> **GPU / OS backend summary**
> - **Mock backend**: needs nothing — always runs on CPU alone (no install required).
> - **Linux / Windows + NVIDIA GPU**: `sionna-rt` **auto-selects** the CUDA (Dr.Jit)
>   backend. As long as the driver is fine, no extra configuration is needed.
> - **macOS (including Apple Silicon)**: Dr.Jit has **no Metal/MPS backend**, so Sionna
>   always runs on **CPU/LLVM**. It works fine but is **slower** than a GPU.
>   If it cannot find CUDA, the app automatically falls back to LLVM and leaves one line in the result
>   warnings: "CUDA unavailable — using LLVM (CPU) ray tracing …" (harmless).

---

## (Optional) Alternative Sionna engine venv — swapping versions (e.g. sionna-rt 1.2.2)

You can run the paths solve with a **different Sionna version**. Install the desired
version into a separate venv and register it in the root `engines.json`, and it appears in the
**Engine** select in Results mode. (The repo already has a `sionna-rt-1.2.2` entry registered.)

**1) Create a venv + install the desired version (Windows example, 1.2.2):**

```powershell
py -3.12 -m venv backend\.venv-sionna-rt-122
backend\.venv-sionna-rt-122\Scripts\python.exe -m pip install "sionna-rt==1.2.2"
```

> **Pin the venv's Python.** sionna-rt 1.x pins mitsuba/drjit wheels that stop at
> Python 3.13 — on Python 3.14 `pip install` *succeeds* but `import sionna.rt`
> fails, and the engine shows as unavailable. Python 3.12 works for every
> 1.x/2.x release.

**2) Add an entry to the root `engines.json`** (skip if it already exists):

```json
{
  "id": "sionna-rt-1.2.2",
  "label": "Sionna RT 1.2.2 (v1.x line)",
  "python": "backend/.venv-sionna-rt-122/Scripts/python.exe",
  "adapter": "sionna_rt"
}
```

**3) Refresh the probe:** restart the backend, or call `GET /api/engines?refresh=true`.

Availability is checked by actually running `import sionna.rt` in the target venv (a cold import
can take tens of seconds, cached per process). For details see
[docs/engines.md](docs/engines.md), [docs/sionna_versions.md](docs/sionna_versions.md).

---

## (Optional) Local LLM/VLM — AI material suggestions

AI is entirely optional. Without an AI server, a **rule-based provider** always
answers instead, so AI Assist mode works even with nothing installed. Attaching a local LLM
lets you get richer suggestions.

Supported providers (all configured via environment variables):

- **Ollama** — default `http://localhost:11434`, text model `qwen3:8b`, vision model
  `qwen2.5vl:3b`.
- **LM Studio** (OpenAI-compatible server) — default `http://localhost:1234/v1`,
  model `google/gemma-4-31b`.

**LM Studio example (when running the backend with Windows PowerShell):**

```powershell
$env:SEAM_OPENAI_URL   = "http://localhost:1234/v1"
$env:SEAM_OPENAI_MODEL = "google/gemma-4-31b"
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend seam_studio.main:app --port 8000
```

**Key environment variables:** the canonical prefix is `SEAM_*`, and for every variable the legacy
`SIONNATWIN_*` name is still recognized (if both are set, `SEAM_*` takes precedence).
For the full list and comments see `backend/.env.example`.

| Variable | Default | Description |
|---|---|---|
| `SEAM_PROJECT_ROOTS` | (built-in default: `projects/`, then `examples/demo_project/`) | Project discovery roots (listed with the path separator). The first root is where new projects/UI imports are saved |
| `SEAM_AI_ENABLED` | `auto` | `auto` / `on` / `off` (manual only) |
| `SEAM_OLLAMA_URL` | `http://localhost:11434` | Ollama endpoint |
| `SEAM_AI_TEXT_MODEL` | `qwen3:8b` | Ollama text model |
| `SEAM_AI_VISION_MODEL` | `qwen2.5vl:3b` | Vision model when a screenshot is attached |
| `SEAM_OPENAI_URL` | `http://localhost:1234/v1` | LM Studio (OpenAI-compatible) |
| `SEAM_OPENAI_MODEL` | `google/gemma-4-31b` | LM Studio model |
| `SEAM_AI_TIMEOUT_S` | `60` | Text AI request timeout (seconds) |
| `SEAM_AI_VISION_TIMEOUT_S` | `300` | Multimodal (image-included) request timeout (seconds). A local VLM takes longer than text due to model load + multi-image prefill, so the cap is higher |
| `SEAM_AI_AUTO_APPLY` | `false` | Reserved flag for a future auto-apply gate. It is parsed into settings but **no code uses it** in the MVP |
| `SEAM_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | The Overpass API endpoint used for OSM (OpenStreetMap) import |

AI output is validated against a strict JSON schema, and on a parse failure falls back to rule-based
with a warning. Suggestions are **never auto-applied** — the user must approve and then press *Apply
decisions*, and every decision is recorded in `ai/suggestions.jsonl` along with provenance.
For details see [docs/ai_assistant.md](docs/ai_assistant.md).

---

## Test / build verification

```bash
# Backend unit tests
backend/.venv/bin/python -m pytest backend/tests -q

# Frontend typecheck + build
cd frontend && npm run build
```

(Windows: `backend\.venv\Scripts\python.exe -m pytest backend\tests -q`)

---

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| **Port 8000/5173 in use** | Another process is holding it. Run the backend on a different port like `--port 8001`, or terminate the existing process. Note that the frontend proxy points to `127.0.0.1:8000`, so if you change the backend port you must also change the proxy target in `frontend/vite.config.ts`. |
| **PowerShell: "running scripts is disabled"** (npm/script execution policy error) | This is due to the execution policy. Prefix the command with `powershell -ExecutionPolicy Bypass -File ...`, or run `Set-ExecutionPolicy -Scope Process Bypass` for the current session only. |
| **GPU not detected / no CUDA** | This is normal. The app automatically runs on the **Mock backend**. To use real Sionna you need an NVIDIA driver+CUDA (or Sionna's LLVM CPU backend). |
| **`LLVM ... ` warning log** | Harmless. It is an informational warning emitted when Sionna's Dr.Jit initializes the CPU (LLVM) backend, and does not affect operation. |
| **Status chip shows "Mock only"** | The `sionna-rt` import failed (broken/partial install — reinstall with `pip install --force-reinstall "sionna-rt>=2.0"` in the backend venv) or Sionna disabled itself because there is no CUDA/LLVM backend. The entire workflow remains usable with Mock. |
| **Status chip shows "AI off"** | Not connected to an AI server (Ollama/LM Studio). Rule-based suggestions still work. To turn on a local LLM see [Local LLM/VLM](#optional-local-llmvlm--ai-material-suggestions) above. |
| **Project list is empty** | The 3 demos are **included by default** in the repo, so they usually appear right away. The backend searches two locations in order — first the repo root's `projects/` (root #1, where projects imported from the UI are saved; may be empty or absent in a fresh clone), then `examples/demo_project/` which has the committed demos. If it is empty, the backend did not find these two — check that you ran the server from the repo root and did not override `SEAM_PROJECT_ROOTS` (legacy `SIONNATWIN_PROJECT_ROOTS`) in a way that hides the default roots. The [3. (Optional) Regenerate the demo projects](#3-optional-regenerate-the-demo-projects) scripts are only needed to *regenerate* the demos. |
| **`import sionna.rt` cold import is slow** | The first probe of an alternative engine can take tens of seconds (cached once per process). Subsequent ones are fast. |
| **`localhost` proxy fails on Windows** | The Vite proxy deliberately uses `127.0.0.1:8000` (to avoid the issue where, on Windows, `localhost` resolves to IPv6 `::1` first and diverges from uvicorn's IPv4 binding). Check that the backend is bound to the IPv4 loopback. |

---

## Next steps

- First-15-minutes usage: [TUTORIAL.md](TUTORIAL.md)
- Swapping engine versions: [docs/engines.md](docs/engines.md),
  [docs/sionna_versions.md](docs/sionna_versions.md)
- RT accuracy and mitigations: [docs/accuracy.md](docs/accuracy.md)
- Architecture / scene format: [docs/architecture.md](docs/architecture.md),
  [docs/scene_format.md](docs/scene_format.md)

> Verified interpreters: Python 3.11 (CI, Linux) / 3.12 (dev, Windows) / 3.14 (clean-venv install check, Windows); 3.13 untested. Node 20+.
