"""User plugin loader (file-based, dependency-free — mirrors engines.json).

Researchers extend the tool WITHOUT touching core code by dropping a
self-contained module at ``<repo>/plugins/<name>/plugin.py``. Each such module
exposes a single ``register(registry)`` function; the ``registry`` object it
receives lets it add:

    * ray-tracing backends        registry.register_backend(name, factory)
    * empirical path-loss models  registry.register_path_loss_model(name, fn)
    * AI material providers        registry.register_ai_provider(factory)
    * RFData-style exporters      registry.register_exporter(name, fn)

Design invariants (HANDOFF.md spirit — the app must never crash on optional
extensions):

    * A broken plugin (import error, bad register(), exception mid-register)
      is CAUGHT and recorded in ``PluginInfo(ok=False, error=...)``. Loading
      one bad plugin never aborts the others and never raises to the caller.
    * Loading is idempotent-ish: :func:`load_plugins` clears the registries
      first, so re-running it (e.g. after editing a plugin) does not
      accumulate stale registrations.
    * Registrations are stored in module-level dicts and exposed through
      getters (:func:`plugin_backends`, :func:`plugin_path_loss_models`,
      :func:`plugin_ai_providers`, :func:`plugin_exporters`) so core consumers
      read them without importing every plugin.

Nothing here imports Sionna, httpx, or any optional dependency; a plugin may,
but its failure is contained.
"""

from __future__ import annotations

import importlib.util
import traceback
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Optional

# ``plugins.py`` lives at backend/app/services/plugins.py, so parents[3] is the
# repo root — the same anchor engines.py uses. The plugins directory sits at the
# repo root next to engines.json, so a user manages both the same way.
REPO_ROOT = Path(__file__).resolve().parents[3]
PLUGINS_DIR = REPO_ROOT / "plugins"

# A path-loss model plugin fn: (freq_hz, tx, rx, config) -> result dict with at
# least {"path_loss_db": float, "valid": bool, "notes": str}. ``tx``/``rx`` are
# the two link endpoints and ``config`` the solver config; kept loosely typed
# (plain objects) so a plugin needs no import from app.schemas.
PathLossModelFn = Callable[[float, object, object, object], dict]
# An exporter fn: (project_dir, scene, config, **kwargs) -> summary dict. Shape
# mirrors rfdata_export.export_rfdata; kept loose for the same reason.
ExporterFn = Callable[..., dict]
BackendFactory = Callable[[], object]
AIProviderFactory = Callable[[], object]


# --------------------------------------------------------------- registries
#
# Module-level so core code reads them via the getters below without importing
# any plugin. Insertion order is preserved (dict), so plugin load order (sorted
# by folder name) determines precedence when a core consumer iterates.

_backends: dict[str, BackendFactory] = {}
_path_loss_models: dict[str, PathLossModelFn] = {}
_ai_providers: list[AIProviderFactory] = []
_exporters: dict[str, ExporterFn] = {}


def _reset_registries() -> None:
    _backends.clear()
    _path_loss_models.clear()
    _ai_providers.clear()
    _exporters.clear()


class PluginRegistrationError(ValueError):
    """A register() call passed something invalid (empty name, non-callable).

    Raised inside register_* so the failure is attributed to the offending
    plugin (caught by the loader and recorded in its PluginInfo.error), rather
    than corrupting a shared registry with a bad entry.
    """


class Registry:
    """The object handed to each plugin's ``register(registry)``.

    Every ``register_*`` method validates its arguments and records the
    registration both in the shared module-level dicts (for core consumers) and
    in ``self.counts`` (for this plugin's PluginInfo). Duplicate names across
    plugins: last writer wins for backends/models/exporters, and the collision
    is surfaced as a warning on the plugin doing the overwrite.
    """

    def __init__(self, plugin_name: str) -> None:
        self._plugin_name = plugin_name
        self.counts: dict[str, int] = {
            "backend": 0,
            "path_loss_model": 0,
            "ai_provider": 0,
            "exporter": 0,
        }
        self.warnings: list[str] = []

    # -- backends --------------------------------------------------------
    def register_backend(self, name: str, factory: BackendFactory) -> None:
        name = _require_name(name, "backend")
        if not callable(factory):
            raise PluginRegistrationError(
                f"backend {name!r} factory is not callable"
            )
        if name in _backends:
            self.warnings.append(f"backend {name!r} overrides an earlier registration")
        _backends[name] = factory
        self.counts["backend"] += 1

    # -- path-loss models ------------------------------------------------
    def register_path_loss_model(self, name: str, fn: PathLossModelFn) -> None:
        name = _require_name(name, "path_loss_model")
        if not callable(fn):
            raise PluginRegistrationError(
                f"path-loss model {name!r} fn is not callable"
            )
        if name in _path_loss_models:
            self.warnings.append(
                f"path-loss model {name!r} overrides an earlier registration"
            )
        _path_loss_models[name] = fn
        self.counts["path_loss_model"] += 1

    # -- AI providers ----------------------------------------------------
    def register_ai_provider(self, factory: AIProviderFactory) -> None:
        if not callable(factory):
            raise PluginRegistrationError("ai provider factory is not callable")
        _ai_providers.append(factory)
        self.counts["ai_provider"] += 1

    # -- exporters -------------------------------------------------------
    def register_exporter(self, name: str, fn: ExporterFn) -> None:
        name = _require_name(name, "exporter")
        if not callable(fn):
            raise PluginRegistrationError(f"exporter {name!r} fn is not callable")
        if name in _exporters:
            self.warnings.append(f"exporter {name!r} overrides an earlier registration")
        _exporters[name] = fn
        self.counts["exporter"] += 1


def _require_name(name: object, kind: str) -> str:
    if not isinstance(name, str) or not name.strip():
        raise PluginRegistrationError(f"{kind} name must be a non-empty string")
    return name.strip()


# ------------------------------------------------------------------ getters
#
# Core consumers call these. Copies are returned so a consumer cannot mutate the
# live registry by accident.


def plugin_backends() -> dict[str, BackendFactory]:
    """name -> factory for every plugin-registered ray-tracing backend."""
    return dict(_backends)


def plugin_path_loss_models() -> dict[str, PathLossModelFn]:
    """name -> fn(freq_hz, tx, rx, config) for every plugin path-loss model."""
    return dict(_path_loss_models)


def plugin_ai_providers() -> list[AIProviderFactory]:
    """Factories for every plugin-registered AI material provider."""
    return list(_ai_providers)


def plugin_exporters() -> dict[str, ExporterFn]:
    """name -> fn for every plugin-registered exporter."""
    return dict(_exporters)


# ------------------------------------------------------------- plugin info


@dataclass
class PluginInfo:
    """Outcome of loading one plugin folder.

    ``ok`` is False whenever the module failed to import or its ``register``
    raised / was missing; ``error`` then carries a human-readable reason (and
    ``traceback`` the full stack for logs). ``registered`` counts what the
    plugin successfully added before any failure.
    """

    name: str
    path: str
    ok: bool
    error: Optional[str] = None
    registered: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    traceback: Optional[str] = None


# The result of the most recent load_plugins() call, so a future
# GET /api/plugins endpoint can report status without re-importing.
_last_loaded: list[PluginInfo] = []


def list_plugins() -> list[PluginInfo]:
    """Status of every plugin from the most recent :func:`load_plugins` run.

    Never triggers a load itself (safe for a health endpoint). Returns an empty
    list if plugins were never loaded this process.
    """
    return list(_last_loaded)


def _iter_plugin_files(plugins_dir: Path) -> list[Path]:
    """Every ``<plugins_dir>/<name>/plugin.py``, sorted by folder name.

    Sorted so load order (and therefore override precedence) is deterministic
    across machines. A stray top-level ``plugin.py`` directly under plugins_dir
    is ignored — plugins are folders, so each can ship a README and assets.
    """
    if not plugins_dir.is_dir():
        return []
    found: list[Path] = []
    for child in sorted(plugins_dir.iterdir(), key=lambda p: p.name):
        if child.is_dir() and not child.name.startswith((".", "_")):
            candidate = child / "plugin.py"
            if candidate.is_file():
                found.append(candidate)
    return found


def _load_one(plugin_file: Path) -> PluginInfo:
    """Import one plugin module and run its register(); never raises.

    The module is loaded under a unique synthetic name so two plugins that both
    define a ``plugin`` module do not collide in ``sys.modules``, and so a
    re-load picks up edits rather than a cached module.
    """
    name = plugin_file.parent.name
    path_str = str(plugin_file)
    registry = Registry(name)
    try:
        module_name = f"stw_plugin_{name}_{uuid.uuid4().hex}"
        spec = importlib.util.spec_from_file_location(module_name, plugin_file)
        if spec is None or spec.loader is None:
            return PluginInfo(
                name=name, path=path_str, ok=False,
                error="could not create import spec for plugin.py",
            )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)  # may raise: caught below
        register = getattr(module, "register", None)
        if not callable(register):
            return PluginInfo(
                name=name, path=path_str, ok=False,
                error="plugin.py has no callable register(registry)",
            )
        register(registry)  # may raise: caught below
    except Exception as exc:  # noqa: BLE001 — a plugin must never crash the app
        reason = str(exc) or exc.__class__.__name__
        return PluginInfo(
            name=name, path=path_str, ok=False,
            error=f"{exc.__class__.__name__}: {reason}",
            registered=dict(registry.counts),
            warnings=list(registry.warnings),
            traceback=traceback.format_exc(),
        )
    return PluginInfo(
        name=name, path=path_str, ok=True,
        registered=dict(registry.counts),
        warnings=list(registry.warnings),
    )


def load_plugins(app_hooks: Optional[object] = None) -> list[PluginInfo]:
    """Discover and load every plugin under :data:`PLUGINS_DIR`.

    Clears the registries first so repeated calls do not double-register.
    Returns one :class:`PluginInfo` per discovered plugin folder (ok or not);
    the result is also cached for :func:`list_plugins`.

    ``app_hooks`` is reserved for a future richer integration (e.g. passing the
    FastAPI app or settings to plugins); it is accepted now so the startup
    call-site — ``load_plugins(app)`` in main.py — is stable. It is not used by
    the current registry-only contract.
    """
    global _last_loaded
    _reset_registries()
    infos = [_load_one(f) for f in _iter_plugin_files(PLUGINS_DIR)]
    _last_loaded = infos
    return infos
