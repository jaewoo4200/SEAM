"""Tests for the user plugin system (app.services.plugins).

Three concerns, per the plugin-system spec:

    (a) load_plugins discovers the shipped example and registers its model;
    (b) a broken plugin is reported ok=False and never raises;
    (c) the two_ray_ground model returns sane values (monotone in distance,
        correct valid-flag logic around the crossover distance).

The broken-plugin and isolation tests write plugin folders into tmp_path and
point the loader there via a monkeypatched PLUGINS_DIR, so they never touch the
real plugins/ directory.
"""

import pytest

from seam_studio.services import plugins


@pytest.fixture(autouse=True)
def _clean_registries():
    """Leave the module-level registries clean before and after each test.

    load_plugins() resets them on entry, but tests that call it against a temp
    dir (or not at all) could otherwise leak the real example plugin's model
    into a later test's view of the registry.
    """
    plugins._reset_registries()
    plugins._last_loaded.clear()
    yield
    plugins._reset_registries()
    plugins._last_loaded.clear()


# ------------------------------------------------------------- helpers


def _write_plugin(plugins_dir, name: str, body: str) -> None:
    """Create <plugins_dir>/<name>/plugin.py with the given source."""
    folder = plugins_dir / name
    folder.mkdir(parents=True)
    (folder / "plugin.py").write_text(body, encoding="utf-8")


GOOD_PLUGIN = '''
def register(registry):
    registry.register_path_loss_model(
        "unit_test_model",
        lambda freq_hz, tx, rx, config: {
            "path_loss_db": 100.0, "valid": True, "notes": "ok"
        },
    )
'''

# Raises at import time (before register is ever reached).
BROKEN_IMPORT_PLUGIN = '''
raise RuntimeError("boom at import")

def register(registry):
    pass
'''

# Imports fine but register() blows up partway through.
BROKEN_REGISTER_PLUGIN = '''
def register(registry):
    registry.register_exporter("half_registered", lambda **kw: {})
    raise ValueError("boom in register")
'''

# Imports fine but has no register() at all.
NO_REGISTER_PLUGIN = '''
THIS_IS_NOT_A_PLUGIN = 1
'''


# ------------------------------------------------------- (a) discovery


def test_load_discovers_example_and_registers_model():
    # Uses the REAL plugins/ dir (the shipped example_two_ray plugin).
    infos = plugins.load_plugins()

    by_name = {i.name: i for i in infos}
    assert "example_two_ray" in by_name, "shipped example plugin not discovered"
    example = by_name["example_two_ray"]
    assert example.ok is True
    assert example.error is None
    assert example.registered.get("path_loss_model") == 1

    models = plugins.plugin_path_loss_models()
    assert "two_ray_ground" in models
    assert callable(models["two_ray_ground"])

    # list_plugins() reflects the most recent load without re-loading.
    assert {i.name for i in plugins.list_plugins()} == {i.name for i in infos}


def test_getters_return_copies_not_live_registry():
    plugins.load_plugins()
    models = plugins.plugin_path_loss_models()
    models.clear()  # mutating the copy must not affect the registry
    assert "two_ray_ground" in plugins.plugin_path_loss_models()


# ------------------------------------------------------- (b) broken plugins


def test_broken_import_plugin_reported_not_raised(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "broken_import", BROKEN_IMPORT_PLUGIN)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", plugins_dir)

    infos = plugins.load_plugins()  # must NOT raise

    assert len(infos) == 1
    info = infos[0]
    assert info.name == "broken_import"
    assert info.ok is False
    assert info.error is not None and "boom at import" in info.error
    assert info.traceback is not None  # full stack captured for logs


def test_broken_register_plugin_reported_not_raised(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "broken_register", BROKEN_REGISTER_PLUGIN)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", plugins_dir)

    infos = plugins.load_plugins()

    info = infos[0]
    assert info.ok is False
    assert "boom in register" in info.error
    # The exporter it managed to register before failing is counted...
    assert info.registered.get("exporter") == 1
    # ...but a failed plugin's partial registrations still live in the registry
    # (last-writer-wins semantics); we only assert the failure was contained.


def test_missing_register_plugin_reported(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "no_register", NO_REGISTER_PLUGIN)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", plugins_dir)

    infos = plugins.load_plugins()
    assert infos[0].ok is False
    assert "register" in infos[0].error.lower()


def test_one_broken_plugin_does_not_stop_the_others(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "a_broken", BROKEN_IMPORT_PLUGIN)
    _write_plugin(plugins_dir, "b_good", GOOD_PLUGIN)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", plugins_dir)

    infos = plugins.load_plugins()
    by_name = {i.name: i for i in infos}

    assert by_name["a_broken"].ok is False
    assert by_name["b_good"].ok is True
    # The good plugin's model is registered despite the sibling failure.
    assert "unit_test_model" in plugins.plugin_path_loss_models()


def test_missing_plugins_dir_is_empty_not_error(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "PLUGINS_DIR", tmp_path / "does_not_exist")
    assert plugins.load_plugins() == []


def test_reload_does_not_double_register(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(plugins_dir, "b_good", GOOD_PLUGIN)
    monkeypatch.setattr(plugins, "PLUGINS_DIR", plugins_dir)

    plugins.load_plugins()
    plugins.load_plugins()  # second load must reset, not accumulate
    assert list(plugins.plugin_path_loss_models()) == ["unit_test_model"]


def test_invalid_registration_arguments_fail_the_plugin(tmp_path, monkeypatch):
    plugins_dir = tmp_path / "plugins"
    plugins_dir.mkdir()
    _write_plugin(
        plugins_dir,
        "bad_args",
        'def register(registry):\n'
        '    registry.register_path_loss_model("", lambda *a: {})\n',
    )
    monkeypatch.setattr(plugins, "PLUGINS_DIR", plugins_dir)
    infos = plugins.load_plugins()
    assert infos[0].ok is False
    assert "non-empty string" in infos[0].error


# ------------------------------------------------- (c) two_ray model sanity


def _two_ray():
    """Load the shipped example and return its two_ray_ground fn."""
    plugins.load_plugins()
    return plugins.plugin_path_loss_models()["two_ray_ground"]


class _Dev:
    """Minimal Device-like stand-in with a .position (x, y, z) in meters."""

    def __init__(self, position):
        self.position = position


# 900 MHz with ht=10 m, hr=1.5 m puts the crossover at ~566 m, so distances of
# a few km sit firmly in the two-ray d^4 far field (the regime the model is
# valid in). At mmWave the crossover is many km out, which is physically why
# the model degrades to FSPL over short indoor/campus links.
FAR_FIELD_FREQ = 9e8


def test_two_ray_increases_with_distance():
    fn = _two_ray()
    tx = _Dev([0.0, 0.0, 10.0])
    # All well beyond the ~566 m crossover so we compare within the d^4 regime.
    prev = None
    for dx in (800.0, 1600.0, 3200.0, 6400.0):
        rx = _Dev([dx, 0.0, 1.5])
        out = fn(FAR_FIELD_FREQ, tx, rx, None)
        assert out["valid"] is True
        if prev is not None:
            assert out["path_loss_db"] > prev, "PL must grow with distance"
        prev = out["path_loss_db"]


def test_two_ray_d4_slope_adds_12db_per_octave():
    # In the d^4 regime, doubling distance adds 40*log10(2) ~= 12.04 dB.
    import math

    fn = _two_ray()
    tx = _Dev([0.0, 0.0, 10.0])
    pl_d = fn(FAR_FIELD_FREQ, tx, _Dev([1000.0, 0.0, 1.5]), None)["path_loss_db"]
    pl_2d = fn(FAR_FIELD_FREQ, tx, _Dev([2000.0, 0.0, 1.5]), None)["path_loss_db"]
    assert pl_2d - pl_d == pytest.approx(40.0 * math.log10(2.0), abs=1e-3)


def test_two_ray_near_field_falls_back_to_fspl_invalid():
    fn = _two_ray()
    tx = _Dev([0.0, 0.0, 10.0])
    # 5 m separation is well below the crossover for these heights.
    out = fn(FAR_FIELD_FREQ, tx, _Dev([5.0, 0.0, 1.5]), None)
    assert out["valid"] is False
    assert "fspl" in out["notes"].lower() or "fell back" in out["notes"].lower()


def test_two_ray_valid_flag_flips_across_crossover():
    import math

    fn = _two_ray()
    freq = FAR_FIELD_FREQ
    ht, hr = 10.0, 1.5
    d_cross = 4.0 * math.pi * ht * hr * freq / 299_792_458.0
    tx = _Dev([0.0, 0.0, ht])
    below = fn(freq, tx, _Dev([d_cross * 0.5, 0.0, hr]), None)
    above = fn(freq, tx, _Dev([d_cross * 2.0, 0.0, hr]), None)
    assert below["valid"] is False
    assert above["valid"] is True


def test_two_ray_accepts_dict_and_sequence_endpoints():
    # The model's endpoint reader tolerates Device-like, dict, and raw sequence.
    fn = _two_ray()
    freq = FAR_FIELD_FREQ
    a = fn(freq, _Dev([0.0, 0.0, 10.0]), _Dev([2000.0, 0.0, 1.5]), None)
    b = fn(freq, {"position": [0.0, 0.0, 10.0]}, {"position": [2000.0, 0.0, 1.5]}, None)
    c = fn(freq, [0.0, 0.0, 10.0], [2000.0, 0.0, 1.5], None)
    assert a["path_loss_db"] == b["path_loss_db"] == c["path_loss_db"]
