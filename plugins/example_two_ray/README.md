# example_two_ray — plugin anatomy walkthrough

A **working** example plugin. It registers one empirical path-loss model,
`two_ray_ground` (the classic two-ray ground-reflection model), and doubles as
a reference for how to write your own plugin.

Full authoring guide (Korean): [`docs/extending.md`](../../docs/extending.md).

## What a plugin is

A plugin is a **self-contained folder** under the repo-root `plugins/`
directory:

```
plugins/
  example_two_ray/
    plugin.py     ← required: defines register(registry)
    README.md     ← optional: docs / notes (this file)
```

The loader (`backend/app/services/plugins.py`) scans `plugins/*/plugin.py`,
imports each module in isolation via `importlib`, and calls its
`register(registry)` function. That is the entire contract.

## The two rules

1. **Define `register(registry)`.** It is the single entry point. Inside it you
   call the registry hooks to add your extensions:

   ```python
   def register(registry):
       registry.register_path_loss_model("two_ray_ground", two_ray_ground)
   ```

2. **Import only what is safe to import in isolation.** `plugin.py` must import
   with no project dependency (standard library is fine — this example only
   imports `math`). If your module raises on import, the loader records the
   failure in `PluginInfo(ok=False, error=...)` and moves on — it never crashes
   the app, but your plugin will not load.

## Registry hooks

The `registry` object passed to `register` exposes four hooks:

| Hook | Signature | Adds |
|---|---|---|
| `register_path_loss_model(name, fn)` | `fn(freq_hz, tx, rx, config) -> {path_loss_db, valid, notes}` | an empirical path-loss model |
| `register_backend(name, factory)` | `factory() -> RayTracingBackend` | a ray-tracing backend |
| `register_ai_provider(factory)` | `factory() -> MaterialSuggestionProvider` | an AI material-suggestion provider |
| `register_exporter(name, fn)` | `fn(project_dir, scene, config, **kw) -> dict` | a results exporter |

This plugin uses only the first.

## The model: two-ray ground reflection

Beyond the crossover (breakpoint) distance the direct and ground-reflected rays
combine so received power falls off as `d^4`, giving a 40·log10(d) slope that is
independent of frequency:

```
PL(dB) = 40*log10(d) - 10*log10(Gt) - 10*log10(Gr) - 20*log10(ht) - 20*log10(hr)
```

- `d`  — 3D TX↔RX distance (m)
- `ht`, `hr` — TX/RX height above the ground plane (m, Z-up frame)
- `Gt`, `Gr` — linear antenna gains (unity / 0 dBi here; the link budget folds
  in real gains elsewhere)

Below the crossover distance `d_c = 4·π·ht·hr·f / c` the far-field `d^4`
approximation does not hold, so the model **falls back to free-space path loss
(FSPL)** and returns `valid=False` with an explanatory `notes` string. Callers
(and UIs) use `valid` to grey out extrapolated points — exactly like the
built-in TR 38.901 models in `channel_analysis.py`.

## Return contract

`two_ray_ground(...)` returns a dict with:

- `path_loss_db` — always a finite number (distances/heights are floored so the
  logs never blow up)
- `valid` — `True` in the `d^4` far field, `False` below the crossover
- `notes` — a short human-readable string (crossover distance, fallback reason)

## Testing this plugin

See `backend/tests/test_plugins.py`. It loads the real `plugins/` directory,
asserts this plugin registers `two_ray_ground`, and checks the model is sane
(path loss increases with distance; `valid` flips correctly around the
crossover distance).
