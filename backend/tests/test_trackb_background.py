"""Track B #21: H0 / H1 background paths of a sensing scenario (mock).

The mock background is deterministic: LoS + ground (+ wall) bounce per
sensing link; H1 drops every path whose leg crosses a target cuboid (the
absorber), H0 keeps them all. The reference side (segment-box clipping, LoS
Doppler, path powers) is recomputed here with plain numpy.
"""

import math
from pathlib import Path

import numpy as np
import pytest

from seam_studio.schemas.actors import ScenarioSimulateRequest
from seam_studio.schemas.devices import Device
from seam_studio.schemas.results import BackgroundHypothesisMeta, RayPath, SensingFrame
from seam_studio.schemas.scene import Actor, ActorTrajectory, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.sensing import BackgroundOptions
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import run_scenario
from seam_studio.services.sensing import select_targets
from seam_studio.services.sensing_background import (
    background_cache_key,
    background_config,
    link_power_db,
    mechanisms,
    resolve_background,
    solve_background,
)
from seam_studio.services.simulation_backends.mock_backend import MockBackend

C = 299_792_458.0
F1 = 3.5e9
LAM = C / F1
MOCK_CFG = {
    "id": "default", "backend": "mock", "frequency_hz": F1,
    "bandwidth_hz": 2e7, "noise_figure_db": 7.0, "max_depth": 1,
}
TX = (0.0, 0.0, 10.0)
RX = (300.0, 0.0, 10.0)


def _config(**kw) -> SimulationConfig:
    return SimulationConfig(**{**MOCK_CFG, **kw})


def _ground() -> Prim:
    return Prim(
        id="/ground", name="ground", semantic_tags=["ground"],
        mesh_ref=MeshRef(mesh_name="ground"),
        rf=RFBinding(material_id="asphalt_custom", assignment_status="user_confirmed",
                     assignment_sources=["user"]),
    )


def _uav(position=(150.0, 0.0, 9.5), trajectory=None) -> Actor:
    return Actor(
        id="uav_01", kind="uav", position=list(position), trajectory=trajectory,
        sensing={"model": "constant", "rcs_dbsm": -10.0, "size_m": [1.0, 1.0, 1.0]},
    )


def _scene(actors=None, rx=RX, scene_id="bg") -> Scene:
    return Scene(
        scene_id=scene_id,
        prims=[_ground()],
        devices=[
            Device(id="tx_a", kind="tx", position=list(TX), power_dbm=43.0),
            Device(id="rx_b", kind="rx", position=list(rx)),
        ],
        actors=[_uav()] if actors is None else actors,
    )


def _run(scene: Scene, num_frames: int = 3, **sensing):
    request = ScenarioSimulateRequest(
        num_frames=num_frames, dt_s=0.5, include_paths=False,
        sensing={"enabled": True, "cpi_pulses": 4096, **sensing},
    )
    return run_scenario(
        MockBackend(), Path("."), scene, load_default_library(), _config(), request
    )


def _ref_hits_box(a, b, center, size) -> bool:
    """Axis-aligned slab test (the test targets have zero yaw/pitch/roll)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    lo, hi = np.asarray(center) - np.asarray(size) / 2, np.asarray(center) + np.asarray(size) / 2
    t0, t1 = 0.0, 1.0
    d = b - a
    for k in range(3):
        if d[k] == 0.0:
            if not lo[k] <= a[k] <= hi[k]:
                return False
            continue
        u, v = sorted(((lo[k] - a[k]) / d[k], (hi[k] - a[k]) / d[k]))
        t0, t1 = max(t0, u), min(t1, v)
    return t0 <= t1


def _key(p: RayPath) -> tuple:
    return (p.tx_id, p.rx_id, p.path_type, round(p.delay_ns, 9))


# ------------------------------------------------------------- cache


BOTH = {"mode": "both", "store": "both"}  # H0 and H1 stored (pre-store behaviour)


def test_static_nodes_solve_once_and_resolve_to_frame_0():
    result = _run(_scene(), background=BOTH)
    frames = result.frames
    first = frames[0].sensing
    assert first.background_meta.h0.solved and first.background_meta.h1.solved
    assert first.background_h0 and first.background_h1 is not None
    for i, frame in enumerate(frames[1:], start=1):
        s = frame.sensing
        for hyp in ("h0", "h1"):
            meta = getattr(s.background_meta, hyp)
            assert (meta.solved, meta.cache_hit, meta.source_frame) == (False, True, 0)
            assert meta.cache_key == getattr(first.background_meta, hyp).cache_key
            assert getattr(s, f"background_{hyp}") is None
            assert resolve_background(frames, i, hyp) == getattr(first, f"background_{hyp}")
        dumped = frame.model_dump(mode="json")["sensing"]
        assert "background_h0" not in dumped and "background_h1" not in dumped
        assert dumped["background_meta"]["h0"]["cache_hit"] is True
    summary = result.metadata["sensing"]["background"]
    assert (summary["solves_h0"], summary["solves_h1"]) == (1, 1)
    assert (summary["cache_hits_h0"], summary["cache_hits_h1"]) == (2, 2)
    assert summary["mechanisms"] == ["los", "specular_reflection", "diffuse_reflection"]
    assert summary["cache"] == "static_nodes" and summary["deterministic"] is True
    assert result.metadata["sensing"]["sensing_tx_ids"] == ["tx_a"]

    per_frame = _run(_scene(), background={**BOTH, "cache": "per_frame"})
    for frame in per_frame.frames:
        s = frame.sensing
        assert s.background_meta.h0.solved and not s.background_meta.h0.cache_hit
        assert s.background_h0 == first.background_h0
        assert s.background_h1 == first.background_h1
    assert per_frame.metadata["sensing"]["background"]["solves_h0"] == 3


def test_h1_drops_exactly_the_paths_through_the_target():
    result = _run(_scene(), num_frames=1, background=BOTH)
    s = result.frames[0].sensing
    h0, h1 = s.background_h0, s.background_h1
    # Target cuboid (1 m) centred on the TX -> RX LoS line at x = 150 m.
    center, size = (150.0, 0.0, 10.0), (1.0, 1.0, 1.0)
    through = [
        p for p in h0
        if any(_ref_hits_box(a, b, center, size) for a, b in zip(p.vertices[:-1], p.vertices[1:]))
    ]
    assert [p.path_type for p in through] == ["los"]
    assert {_key(p) for p in h1} == {_key(p) for p in h0} - {_key(p) for p in through}
    assert any(p.path_type == "reflection" for p in h1)  # the ground bounce stays
    for p in h0 + h1:
        assert p.target_id is None and isinstance(p.doppler_hz, float)
        assert p.doppler_hz == 0.0  # static nodes
    # Totals: 10 log10 sum |a|^2 over each hypothesis.
    for hyp, paths in (("h0", h0), ("h1", h1)):
        total = sum(10 ** (p.path_gain_db / 10) for p in paths)
        meta = getattr(s.background_meta, hyp)
        assert meta.total_power_db["tx_a>rx_b"] == pytest.approx(10 * math.log10(total), abs=1e-12)
        assert meta.num_paths == len(paths)
    # Off the line, the target shadows nothing: H0 == H1.
    off = _run(_scene([_uav((150.0, 40.0, 30.0))]), num_frames=1, background=BOTH)
    assert off.frames[0].sensing.background_h0 == off.frames[0].sensing.background_h1


def test_default_store_keeps_h1_out_of_the_result():
    """store "h0" (default): H1 is solved and feeds the CFAR map but is not
    in the frames; "both" stores it (and maps identically); "none" stores
    neither. The store option is not part of the cache key."""
    assert BackgroundOptions().store == "h0"
    target = _uav(trajectory=ActorTrajectory(waypoints=[[150.0, 0.0, 10.0], [150.0, 60.0, 10.0]],
                                             speed_m_s=10.0))
    scene = _scene([target])
    grid_s = {"scs_hz": 15_625.0, "n_fft": 2048, "n_active_sc": 1280,
              "symbol_duration_s": 1.0 / 14_400,
              "pilot": {"subcarrier_step": 5, "symbol_step": 9}}
    cfar = {"mode": "cfar", "map": {"clutter_removal": "mean_subtract"}}
    runs = {
        store: _run(scene, background={"mode": "both", "store": store}, waveform=grid_s,
                    detection=cfar)
        for store in ("h0", "both", "none")
    }
    default, both, none = runs["h0"], runs["both"], runs["none"]
    for i, frame in enumerate(default.frames):
        s = frame.sensing
        assert s.background_h1 is None and s.background_meta.h1.solved  # moving target
        assert s.background_meta.h1.stored is False and s.background_meta.h0.stored is True
        dumped = frame.model_dump(mode="json")["sensing"]
        assert "background_h1" not in dumped
        assert ("background_h0" in dumped) == (i == 0)
        assert resolve_background(default.frames, i, "h1") is None
        assert resolve_background(default.frames, i, "h0") == default.frames[0].sensing.background_h0
        b = both.frames[i].sensing
        assert b.background_h1 is not None and b.background_meta.h1.stored
        assert s.background_meta.h1.cache_key == b.background_meta.h1.cache_key
        # The map used the same in-memory H1 either way.
        assert s.detections == b.detections and s.links == b.links
        n = none.frames[i].sensing
        assert n.background_h0 is None and n.background_h1 is None
        assert n.background_meta.h0.stored is False and n.detections == s.detections
    summary = default.metadata["sensing"]["background"]
    assert summary["store"] == "h0" and summary["solves_h1"] == 3
    assert summary["paths_stored"] == len(default.frames[0].sensing.background_h0)
    assert none.metadata["sensing"]["background"]["paths_stored"] == 0
    assert default.metadata["sensing"]["detection"] == both.metadata["sensing"]["detection"]


def test_max_paths_per_link_defaults_to_256_and_warns_once():
    assert BackgroundOptions().max_paths_per_link == 256
    result = _run(_scene(), num_frames=3, background={"mode": "h0_removed", "max_paths_per_link": 1})
    meta = result.frames[0].sensing.background_meta.h0
    dropped, kept = meta.dropped_paths, meta.num_paths
    assert dropped >= 1 and kept == 1
    expected = (
        "sensing: background: max_paths_per_link 1 kept the strongest 1 paths per link: "
        f"{dropped} of {dropped + kept} solved background paths dropped over the run (set "
        "sensing.background.max_paths_per_link null to keep every path)"
    )
    assert [w for w in result.warnings if "max_paths_per_link" in w] == [expected]
    summary = result.metadata["sensing"]["background"]
    assert summary["paths_dropped"] == dropped and summary["max_paths_per_link"] == 1
    plain = _run(_scene(), num_frames=1, background={"mode": "h0_removed"})
    assert not [w for w in plain.warnings if "max_paths_per_link" in w]


class _FlakyBackend(MockBackend):
    """Fails the first ``fail`` background solves like the sionna backend's
    graceful degradation (no paths, a warning, metadata solve_failed)."""

    def __init__(self, fail=1):
        super().__init__()
        self.fail = fail

    def simulate_background(self, *args, **kwargs):
        result = super().simulate_background(*args, **kwargs)
        if self.fail > 0:
            self.fail -= 1
            result.paths = []
            result.warnings = ["sionna backend failed: CUDA out of memory; see logs"]
            result.metadata = {**result.metadata, "solve_failed": True}
        return result


def test_failed_solve_is_not_cached():
    scene = _scene()
    request = ScenarioSimulateRequest(
        num_frames=3, dt_s=0.5, include_paths=False,
        sensing={"enabled": True, "cpi_pulses": 4096, "background": {"mode": "h0_removed"}},
    )
    result = run_scenario(_FlakyBackend(), Path("."), scene, load_default_library(), _config(),
                          request)
    m0, m1, m2 = (f.sensing.background_meta.h0 for f in result.frames)
    assert m0.error and "CUDA out of memory" in m0.error and not m0.solved and not m0.stored
    assert result.frames[0].sensing.background_h0 is None
    assert resolve_background(result.frames, 0, "h0") is None
    assert m1.solved and not m1.cache_hit and m1.num_paths > 0  # solved again
    assert m2.cache_hit and m2.source_frame == 1
    summary = result.metadata["sensing"]["background"]
    assert summary["failed_solves_h0"] == 1 and summary["solves_h0"] == 1
    assert any("H0 solve failed at frame 0" in w for w in result.warnings)
    assert "error" not in result.frames[1].model_dump(mode="json")["sensing"]["background_meta"]["h0"]


def test_moving_rx_misses_the_cache_and_carries_los_doppler():
    car = Actor(
        id="car_01", kind="car", position=[300.0, 0.0, 0.0],
        trajectory=ActorTrajectory(waypoints=[[300.0, 0.0, 0.0], [300.0, 100.0, 0.0]],
                                   speed_m_s=10.0, mode="once"),
        attached_device_ids=["rx_b"],
    )
    scene = _scene([_uav((150.0, 40.0, 30.0)), car])
    result = _run(scene, background={"mode": "h0_removed"})
    v_rx = np.array([0.0, 10.0, 0.0])
    for i, frame in enumerate(result.frames):
        s = frame.sensing
        meta = s.background_meta.h0
        assert meta.solved and not meta.cache_hit and meta.source_frame == i
        assert s.background_meta.h1 is None and s.background_h1 is None
        rx = np.array([300.0, 10.0 * 0.5 * i, 10.0])
        u = (rx - np.array(TX)) / np.linalg.norm(rx - np.array(TX))
        los = [p for p in s.background_h0 if p.path_type == "los"]
        assert len(los) == 1
        assert los[0].doppler_hz == pytest.approx(float((0.0 - v_rx) @ u) / LAM, abs=1e-9)
        assert los[0].vertices[-1] == pytest.approx(list(rx))
    keys = {f.sensing.background_meta.h0.cache_key for f in result.frames}
    assert len(keys) == 3


def test_cache_key_inputs():
    scene = _scene()
    targets = select_targets(scene, None)
    cfg = background_config(_config(), BackgroundOptions(mode="both"))
    nodes = {"tx_a": (list(TX), [0.0] * 3), "rx_b": (list(RX), [0.0] * 3)}
    actors = [("car_01", [1.0, 2.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0])]

    def key(hyp, **kw):
        args = dict(backend_name="mock", rf_fp="sha256:x", cfg=cfg,
                    options=BackgroundOptions(mode="both"), nodes=nodes, actors=actors,
                    targets=targets)
        return background_cache_key(hyp, **{**args, **kw})

    base0, base1 = key("h0"), key("h1")
    assert base0.startswith("sha256:") and base0 != base1
    moved_actor = [("car_01", [1.0, 2.5, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0])]
    assert key("h0", actors=moved_actor) != base0
    moved_node = {**nodes, "rx_b": ([300.0, 1.0, 10.0], [0.0] * 3)}
    assert key("h0", nodes=moved_node) != base0
    # H0 ignores where the (removed) target is; H1 does not.
    moved = [t.__class__(**{**t.__dict__, "center": (151.0, 0.0, 10.0)}) for t in targets]
    assert key("h0", targets=moved) == base0 and key("h1", targets=moved) != base1
    assert key("h0", options=BackgroundOptions(mode="both", cache="per_frame")) == base0
    assert key("h0", options=BackgroundOptions(mode="both", num_samples=10)) != base0
    assert key("h0", rf_fp="sha256:y") != base0


def test_background_config_and_mechanisms():
    cfg = background_config(
        _config(max_depth=2, refraction=True, engine="x"),
        BackgroundOptions(mode="h0_removed", diffuse_reflection=False, diffraction=True,
                          max_depth=4, num_samples=1000),
    )
    assert (cfg.max_depth, cfg.num_samples, cfg.engine, cfg.scattering) == (4, 1000, None, False)
    assert mechanisms(cfg) == ["los", "specular_reflection", "refraction", "diffraction"]
    kept = background_config(_config(max_depth=2), BackgroundOptions(mode="both"))
    assert (kept.max_depth, kept.num_samples, kept.scattering) == (2, 1_000_000, True)


def test_max_paths_per_link_keeps_the_strongest():
    scene = _scene()
    full = _run(scene, num_frames=1, background={"mode": "h0_removed"})
    capped = _run(scene, num_frames=1, background={"mode": "h0_removed", "max_paths_per_link": 1})
    h0 = full.frames[0].sensing.background_h0
    kept = capped.frames[0].sensing.background_h0
    strongest = max(h0, key=lambda p: p.power_dbm)
    assert kept == [strongest]
    assert capped.frames[0].sensing.background_meta.h0.dropped_paths == len(h0) - 1


def test_mock_material_scales_and_names():
    backend = MockBackend()
    scene = _scene()
    library = load_default_library()
    assert backend.radio_material_names(Path("."), scene, library) == ["asphalt_custom", "metal"]
    cfg = _config(rx_ids=["rx_b"])
    opts = BackgroundOptions(mode="h0_removed")
    nominal, warns, _, _ = solve_background(
        backend, Path("."), scene, library, cfg, opts, [], "h0"
    )
    scaled, warns2, _, _ = solve_background(
        backend, Path("."), scene, library, cfg, opts, [], "h0",
        material_scales={"asphalt_custom": (2.0, 0.5), "nope": (1.1, 1.0)},
    )
    assert any("'nope'" in w for w in warns2) and not warns
    ground = [p for p in nominal if p.path_type == "reflection"][0]
    ground_s = [p for p in scaled if p.path_type == "reflection"][0]

    def fresnel_db(eps):
        r = ((math.sqrt(eps) - 1) / (math.sqrt(eps) + 1)) ** 2
        return -10 * math.log10(r)

    expected = fresnel_db(5.72) - fresnel_db(5.72 * 2.0)
    assert ground_s.power_dbm - ground.power_dbm == pytest.approx(expected, abs=1e-9)
    los = [p for p in nominal if p.path_type == "los"]
    assert los == [p for p in scaled if p.path_type == "los"]
    # The library itself is untouched.
    assert library.get("asphalt_custom").relative_permittivity == 5.72
    assert link_power_db(nominal, "tx_a", "rx_b") == pytest.approx(
        10 * math.log10(sum(10 ** (p.path_gain_db / 10) for p in nominal)), abs=1e-12
    )
    assert link_power_db(nominal, "tx_a", "nope") is None


# ------------------------------------------------------- compatibility


def test_new_fields_stay_out_of_the_json_while_unset():
    """Field(exclude_if=...) needs pydantic >= 2.12 (2.11 ignores it: the
    fields dump as null and the JSON schema holds a function)."""
    frame = SensingFrame.model_validate({"echoes": [], "links": [], "estimates": []})
    assert set(frame.model_dump(mode="json")) == {"echoes", "links", "estimates", "nodes"}
    assert "detections" in SensingFrame.model_json_schema()["properties"]
    meta = BackgroundHypothesisMeta(solved=True, source_frame=0, cache_key="k")
    assert "error" not in meta.model_dump(mode="json") and meta.stored is True
    pyproject = (Path(__file__).resolve().parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert '"pydantic>=2.12"' in pyproject


def test_background_leaves_the_v0115_fields_alone():
    plain = _run(_scene(), include_comm_paths=True)
    with_bg = _run(_scene(), include_comm_paths=True, background=BOTH)
    v0115 = {"echoes", "links", "estimates", "nodes"}
    for i, (a, b) in enumerate(zip(plain.frames, with_bg.frames)):
        da = a.sensing.model_dump(mode="json")
        db = b.sensing.model_dump(mode="json")
        for key in v0115:
            assert da[key] == db[key], key
        assert set(da) == v0115
        stored = {"background_h0", "background_h1"} if i == 0 else set()
        assert set(db) == v0115 | stored | {"background_meta"}
        # include_comm_paths keeps appending the H1-style comm paths to echoes.
        assert [p for p in b.sensing.echoes if p.target_id is None]
    plain_meta = dict(plain.metadata["sensing"])
    bg_meta = dict(with_bg.metadata["sensing"])
    assert set(bg_meta) - set(plain_meta) == {"background", "sensing_tx_ids"}
    for key in plain_meta:
        if key != "options":
            assert bg_meta[key] == plain_meta[key], key
    off = _run(_scene(), background={"mode": "off"})
    assert "background" not in off.metadata["sensing"]
    assert all(f.sensing.background_meta is None for f in off.frames)


def test_targetless_run_needs_the_background():
    scene = _scene(actors=[])
    result = _run(scene, background={"mode": "h0_removed"})
    for frame in result.frames:
        assert frame.sensing.links == [] and frame.sensing.estimates == []
        assert frame.sensing.echoes == []
    assert result.frames[0].sensing.background_h0
    assert result.metadata["sensing"]["target_ids"] == []
    with pytest.raises(ValueError, match="no sensing targets"):
        _run(scene)


@pytest.fixture()
def client(api_client):
    pid = "bg_api"
    assert api_client.post("/api/projects", json={"name": "BG", "project_id": pid}).status_code == 201
    scene = _scene(actors=[], scene_id=pid).model_dump(mode="json")
    assert api_client.put(f"/api/projects/{pid}/scene", json=scene).status_code == 200
    return api_client, pid


def test_api_targetless_background_200_and_off_400(client):
    api, pid = client
    body = {"config": MOCK_CFG, "num_frames": 2, "dt_s": 0.5, "include_paths": False}
    ok = api.post(
        f"/api/projects/{pid}/simulate/scenario",
        json={**body, "sensing": {"enabled": True, "background": {"mode": "h0_removed"}}},
    )
    assert ok.status_code == 200, ok.text
    frames = ok.json()["frames"]
    assert frames[0]["sensing"]["links"] == [] and frames[0]["sensing"]["estimates"] == []
    assert frames[0]["sensing"]["background_h0"]
    assert frames[1]["sensing"]["background_meta"]["h0"]["source_frame"] == 0
    stored = api.get(f"/api/projects/{pid}/results/scenario").json()
    assert stored == ok.json()
    bad = api.post(
        f"/api/projects/{pid}/simulate/scenario", json={**body, "sensing": {"enabled": True}}
    )
    assert bad.status_code == 400 and "no sensing targets" in bad.json()["detail"]
