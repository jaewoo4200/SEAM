# Extending the tool (plugins & extension seams)

> 🌐 **English** · [한국어](extending.ko.md)

This document summarizes how researchers can extend SEAM Studio **without touching
core code**. There are two tracks:

1. **Plugin system** — add a backend / path-loss model / AI provider / exporter
   with a single `plugins/<name>/plugin.py` file (most of this document).
2. **File-based extension seams** — extension points that exist even without a
   plugin: swapping the sionna version (`engines.json`), custom RF material
   (materials API), adding a solver preset (`configPresets.ts`) (the
   [Other extension points](#other-extension-points-without-a-plugin) section at the bottom).

Plugin loader implementation: `backend/app/services/plugins.py`.
Working example: [`plugins/example_two_ray/`](../plugins/example_two_ray/).

---

## Plugin anatomy (structure)

A plugin is a **self-contained folder** under the repo-root `plugins/` directory.
Just like `engines.json`, it is file-based and has no separate dependencies.

```
plugins/
  my_plugin/
    plugin.py     ← required: defines a single register(registry)
    README.md     ← optional: docs/notes
    ...           ← optional: data files, etc. (loaded by plugin.py via relative path)
```

The loader scans `plugins/*/plugin.py` in folder-name order, **imports each module
in isolation** with `importlib.util.spec_from_file_location`, and then calls that
module's `register(registry)` function. That's all.

### Two rules

**1. Define `register(registry)`.** A single entry point. Inside it, call registry
hooks to register your extensions.

```python
def register(registry):
    registry.register_path_loss_model("two_ray_ground", two_ray_ground)
```

**2. Be import-safe.** Since `plugin.py` is imported in isolation, importing heavy /
optional dependencies (sionna, httpx, etc.) at the module top level is risky. If the
import fails, the loader records `PluginInfo(ok=False, error=...)` and **skips it** —
the app never crashes, but that plugin does not load. Standard-library imports are
safe. **Lazy-import heavy dependencies inside functions** (core's `ai_provider.py`
uses `httpx` that way).

---

## Registry hooks

The `registry` object passed to `register` exposes four hooks. Each hook validates
its arguments, and invalid arguments (empty name, non-callable) fail that plugin
(with no effect on other plugins).

> **Current wiring status (important)**: All four hooks register successfully, but
> the only one actually connected to the core runtime today that takes end-to-end
> effect is `register_path_loss_model` (`channel_analysis.py` consumes
> `plugin_path_loss_models()`). Things registered with `register_backend` /
> `register_ai_provider` / `register_exporter` are also queryable via getters
> (`plugin_backends()`/`plugin_ai_providers()`/`plugin_exporters()`) and register
> correctly, but they are **not yet wired into the runtime chain** —
> `get_backend`/`resolve_backend` only look at the static `_BACKENDS = {mock, sionna}`,
> and provider selection (`_select_provider`) and the export path do not read the
> plugin getters either. To actually consume these three hooks, you must wire up
> additional core integration points.

### `register_path_loss_model(name, fn)`

Adds an empirical path-loss model. It is compared side by side with
`channel_analysis.py`'s built-in models (FSPL, TR 38.901, CI).

- **signature**: `fn(freq_hz, tx, rx, config) -> {path_loss_db, valid, notes}`
  - `freq_hz` (float): frequency in Hz
  - `tx`, `rx`: the two endpoints of the link (Device-like — `.position` is
    `[x, y, z]` in meters, Z-up). It's good to write it defensively so it also
    accepts a dict or a raw sequence.
  - `config`: solver config (`SimulationConfig`)
  - **returned dict**: `path_loss_db` (float, always finite), `valid` (bool — False
    if outside the valid range), `notes` (str — human-readable validation/fallback
    note)

```python
import math

def my_model(freq_hz, tx, rx, config):
    d = max(math.dist(list(tx.position), list(rx.position)), 1.0)
    pl = 20.0 * math.log10(4.0 * math.pi * d * freq_hz / 299_792_458.0)
    return {"path_loss_db": round(pl, 4), "valid": True, "notes": "FSPL"}

def register(registry):
    registry.register_path_loss_model("my_model", my_model)
```

> `valid=False` does **not** suppress the value. Just like the built-in models, it
> always returns a value but flags that it is outside the valid range, so the UI can
> gray it out.

### `register_backend(name, factory)`

Adds a new ray-tracing backend. It is the target to be merged with the same dict
(`_BACKENDS`) as the built-in `mock` / `sionna`.

- **signature**: `factory() -> RayTracingBackend`
  - The factory is called **with no arguments** to create a backend instance (same
    as the built-in `get_backend` calling `_BACKENDS[name]()`).
  - The returned object must follow the `RayTracingBackend`
    (`backend/app/services/simulation_backends/base.py`) contract: a `name`
    attribute, `is_available()`, `simulate_paths(...)`, `simulate_radio_map(...)`
    (required); `compile`/`simulate_beamforming` can reuse the base default
    implementations.

```python
from app.services.simulation_backends.base import RayTracingBackend

class MyBackend(RayTracingBackend):
    name = "my_backend"
    def is_available(self): return True
    def simulate_paths(self, project_dir, scene, library, config): ...
    def simulate_radio_map(self, project_dir, scene, library, config): ...

def register(registry):
    registry.register_backend("my_backend", MyBackend)  # 클래스 = 무인자 factory
```

> This hook imports `app.schemas`, so to reduce risk, one option is to handle the
> backend class and its import **inside** the `register` function.

### `register_ai_provider(factory)`

Adds a material-suggestion provider to the provider chain (see the
`local_openai → ollama_text → rule_based` chain in `ai_provider.py`).

- **signature**: `factory() -> MaterialSuggestionProvider`
  - The returned object must follow the `MaterialSuggestionProvider`
    (`backend/app/services/ai_provider.py`) contract: a `name` attribute,
    `is_available() -> bool`, `suggest(scene, library, prim_ids, screenshot=None)
    -> MaterialSuggestionResponse`.
  - The core convention is that **all failures degrade internally to rule_based**.
    The provider must work even without network/GPU.

```python
def register(registry):
    registry.register_ai_provider(MyProvider)  # MyProvider() -> provider 인스턴스
```

> path-loss/backend/exporter are **name → one** (last-writer-wins), but AI providers
> stack up as a **list**. The priority within the chain is decided by the core
> integration point.

### `register_exporter(name, fn)`

Adds a result exporter (same shape as `rfdata_export.export_rfdata`).

- **signature**: `fn(project_dir, scene, config, **kwargs) -> dict`
  - Same shape as `rfdata_export.export_rfdata(project_dir, scene, config,
    created_at, paths=…, radio_map=…, trajectory=…)`. It writes files into the
    project folder and returns a `{"export_dir", "files", ...}` summary dict.

```python
def my_exporter(project_dir, scene, config, **kwargs):
    out = project_dir / "export" / "my_format"
    out.mkdir(parents=True, exist_ok=True)
    # ... 파일 쓰기 ...
    return {"export_dir": "export/my_format", "files": [...]}

def register(registry):
    registry.register_exporter("my_format", my_exporter)
```

---

## Load order

- The loader iterates over folders under `plugins/` sorted in **ascending name
  order** → the load order is deterministic regardless of machine.
- If two plugins register the same name (backend/model/exporter), **the one loaded
  later wins** (last-writer-wins). A warning is left in the overwriting side's
  `PluginInfo.warnings`.
- Even if `register` fails partway through, whatever was registered before the
  failure remains in the registry and that plugin is recorded as `ok=False`. **One
  plugin's failure does not block other plugins.**
- `load_plugins()` **clears the registry first** on every call → modifying a plugin
  and reloading does not cause duplicate registration.
- Folders starting with `.` or `_` are ignored (for scratch/inactive plugins).

Registration results are read via getters (used by core consumers, returning a copy):

```python
from app.services import plugins
plugins.plugin_path_loss_models()   # {name: fn}
plugins.plugin_backends()           # {name: factory}
plugins.plugin_ai_providers()       # [factory, ...]
plugins.plugin_exporters()          # {name: fn}
plugins.list_plugins()              # 최근 load 결과 [PluginInfo, ...] (재로드 안 함)
```

---

## Testing your plugin

`backend/tests/test_plugins.py` is the reference example. Key patterns:

- **Load the real `plugins/`**: after calling `plugins.load_plugins()`, verify your
  model is present in `plugins.plugin_path_loss_models()`.
- **Isolated temp-plugin test**: write `<name>/plugin.py` into `tmp_path` and point
  the loader there with `monkeypatch.setattr(plugins, "PLUGINS_DIR", tmp_dir)`. It
  does not touch the real `plugins/`.
- **A broken plugin must not raise**: drop in a plugin that throws an exception in
  import/register, and verify `load_plugins()` returns
  `PluginInfo(ok=False, error=...)` without raising.
- **Registry cleanup**: to prevent leakage between tests, call
  `plugins._reset_registries()` in a fixture.

```powershell
# 리포 루트에서
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_plugins.py -q
```

---

## Other extension points (without a plugin)

Outside the plugin system too, extension points opened by files/config already exist.

### 1. Swap the Sionna version — `engines.json`

Swap the Sionna RT engine version used for the paths solve via the repo-root
`engines.json`. Install a different sionna-rt into a separate venv and add an entry,
and after `GET /api/engines?refresh=true` it appears in the UI Engine select. For the
detailed procedure, supported range, and protocol, see
[`docs/engines.md`](engines.md) (for version-by-version differences, see
[`docs/sionna_versions.md`](sionna_versions.md)).

```json
{"engines": [
  {"id": "sionna-rt-1.2.2", "label": "Sionna RT 1.2.2",
   "python": "backend/.venv-sionna-rt-122/Scripts/python.exe",
   "adapter": "sionna_rt"}
]}
```

### 2. Custom RF material — materials API

An RF material is an EM surface description and is separate from the visual/PBR
material. When a project is created, the built-in library
(`backend/app/data/default_rf_materials.yaml`) is copied to
`<project>/rf/materials.yaml`, and after that the project file is authoritative. To
add/modify a new material, use the materials API:

- `GET  /api/projects/{id}/rf/materials` — query the library
- `PUT  /api/projects/{id}/rf/materials/{material_id}` — add/modify a material

You can use `model: constant` (directly specify
`relative_permittivity`/`conductivity_s_per_m`) or `model: itu_frequency_dependent`
(reference a Sionna built-in ITU material). For the format and field definitions, see
[`docs/rf_materials.md`](rf_materials.md) and
`backend/app/schemas/materials.py`.

### 3. Add a solver preset — `frontend/src/configPresets.ts`

The Preset dropdown in SolverControls comes from the `PRESETS` array in
`frontend/src/configPresets.ts`. To add a new canonical deployment scenario, put one
`ConfigPreset` entry here (frequency, depth, mechanisms, samples, bandwidth +
radio-map grid cell/height). A preset patches the paths/radio-map config together and
does not touch the backend/tx/rx selection.

```ts
// frontend/src/configPresets.ts 의 PRESETS 배열에 추가
{
  id: "my_scenario_28",
  label: "My Scenario (28 GHz)",
  config: { frequency_hz: 28e9, max_depth: 5, num_samples: 1_000_000, bandwidth_hz: 100e6 },
  radioMap: { cell_size_m: 1.0, height_m: 1.5 },
}
```

You must also add the new `id` to the `ConfigPresetId` union for the type to pass.
`"custom"` is a sentinel for when the user has edited things directly, so don't touch
it.
