"""Track B #21 on the real sionna-rt PathSolver (sionna-rt >= 2.2).

A ground slab and one box building, both constant radio materials (the XML
alone defines them), a bistatic TX/RX pair 300 m apart and a constant-RCS
target on the TX -> wall leg of the wall reflection. The reference side is
an independent sionna.rt.load_scene of the compiled XML with the target
actor mesh removed and (H1) a ConstantRCSSensingTarget absorber added,
solved by PathSolver(deterministic=True) with the same flags, seed and
samples; nothing from seam_studio touches its paths.
"""

import math
from pathlib import Path

import numpy as np
import pytest
import trimesh

from seam_studio.schemas.actors import ScenarioSimulateRequest
from seam_studio.schemas.devices import Device
from seam_studio.schemas.materials import RFMaterial, RFMaterialLibrary
from seam_studio.schemas.scene import Actor, ActorShape, MeshRef, Prim, RFBinding, Scene
from seam_studio.schemas.sensing import BackgroundOptions
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import run_scenario
from seam_studio.services.sensing import select_targets
from seam_studio.services.sensing_background import _keep_strongest, solve_background
from seam_studio.services.simulation_backends import sionna_backend
from seam_studio.services.simulation_backends.sionna_backend import (
    SionnaBackend,
    _actor_object_key,
    _load_scene_cached,
)

from .conftest import requires_sionna_rcs

pytestmark = requires_sionna_rcs

FREQ = 3.5e9
SAMPLES = 400_000
SEED = 7
TX = (0.0, 0.0, 10.0)
RX = (300.0, 0.0, 10.0)
# On the TX -> (150, 50, 10) leg of the y = 50 wall reflection.
TARGET_ON_LEG = (75.0, 25.0, 8.5)
# Above every scene object: no ray that meets it could have met anything else.
TARGET_ABOVE = (150.0, -60.0, 80.0)
TARGET_SIZE = 3.0
_TYPES = {"reflection": 1, "scattering": 2, "transmission": 4, "diffraction": 8}


def _library() -> RFMaterialLibrary:
    lib = load_default_library()
    extra = [
        RFMaterial(id="trackb_ground", display_name="ground", category="ground",
                   model="constant", relative_permittivity=5.0, conductivity_s_per_m=0.01,
                   thickness_m=1.0, scattering_coefficient=0.3),
        RFMaterial(id="trackb_wall", display_name="wall", category="concrete",
                   model="constant", relative_permittivity=5.24, conductivity_s_per_m=0.0462,
                   thickness_m=0.3, scattering_coefficient=0.2),
        RFMaterial(id="trackb_actor", display_name="actor", category="metal",
                   model="constant", relative_permittivity=1.0, conductivity_s_per_m=1e7,
                   scattering_coefficient=0.0),
    ]
    return RFMaterialLibrary(materials=lib.materials + extra)


def _write_geometry(project_dir: Path) -> None:
    (project_dir / "visual").mkdir(parents=True, exist_ok=True)
    (project_dir / "rf").mkdir(exist_ok=True)
    tm = trimesh.Scene()
    ground = trimesh.creation.box(extents=(500.0, 300.0, 0.2))
    ground.apply_translation((150.0, 0.0, -0.1))
    tm.add_geometry(ground, geom_name="ground", node_name="ground")
    building = trimesh.creation.box(extents=(20.0, 20.0, 30.0))
    building.apply_translation((150.0, 60.0, 15.0))
    tm.add_geometry(building, geom_name="building", node_name="building")
    (project_dir / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))


@pytest.fixture()
def project(tmp_path: Path, monkeypatch) -> Path:
    # The periodic full flush would drop the cached scene the tests inspect.
    monkeypatch.setattr(sionna_backend, "_FULL_FLUSH_EVERY", 10**9)
    proj = tmp_path / "trackb_bg.seam"
    _write_geometry(proj)
    return proj


def _prim(pid: str, mesh: str, material: str, tag: str) -> Prim:
    return Prim(
        id=pid, name=mesh, semantic_tags=[tag], mesh_ref=MeshRef(mesh_name=mesh),
        rf=RFBinding(material_id=material, assignment_status="user_confirmed",
                     assignment_sources=["user"]),
    )


def _scene(target=TARGET_ON_LEG) -> Scene:
    return Scene(
        scene_id="trackb_bg",
        prims=[
            _prim("/ground", "ground", "trackb_ground", "ground"),
            _prim("/building", "building", "trackb_wall", "building"),
        ],
        devices=[
            Device(id="tx_a", kind="tx", position=list(TX), power_dbm=43.0),
            Device(id="rx_b", kind="rx", position=list(RX)),
        ],
        actors=[
            Actor(
                id="tgt_01", kind="custom", position=list(target),
                shape=ActorShape(size_m=[TARGET_SIZE] * 3), rf_material_id="trackb_actor",
                sensing={"model": "constant", "rcs_dbsm": 0.0},
            )
        ],
    )


def _config(**kw) -> SimulationConfig:
    base = dict(backend="sionna", frequency_hz=FREQ, bandwidth_hz=2e7, max_depth=2,
                reflection=True, num_samples=SAMPLES, seed=SEED)
    return SimulationConfig(**{**base, **kw})


def _run(project: Path, scene: Scene, background: dict, num_frames: int = 3):
    """Solver-fidelity runs: H1 stored and every path kept (no cap)."""
    background = {"store": "both", "max_paths_per_link": None, **background}
    request = ScenarioSimulateRequest(
        num_frames=num_frames, dt_s=0.5, include_paths=False,
        sensing={"enabled": True, "cpi_pulses": 4096, "samples_per_sp": 100_000,
                 "background": background},
    )
    return run_scenario(SionnaBackend(), project, scene, _library(), _config(), request)


# ------------------------------------------------------------ reference


def _reference(project: Path, hypothesis: str, *, diffuse: bool, target=TARGET_ON_LEG):
    """Independent Sionna solve of the compiled XML: (interaction codes,
    vertices, tau_s, gain_db) per valid path."""
    from sionna.rt import PathSolver, PlanarArray, Receiver, Transmitter, load_scene
    from sionna.rt.rcs import ConstantRCSSensingTarget

    scene = load_scene(str(project / "rf" / "generated_scene.xml"))
    scene.frequency = FREQ
    scene.tx_array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization="V")
    scene.rx_array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization="V")
    scene.add(Transmitter(name="tx_a", position=list(TX), orientation=[0.0, 0.0, 0.0],
                          power_dbm=43.0))
    scene.add(Receiver(name="rx_b", position=list(RX), orientation=[0.0, 0.0, 0.0]))
    actor = next(obj for name, obj in scene.objects.items() if "tgt_01" in name)
    add = []
    if hypothesis == "h1":
        h = TARGET_SIZE
        add.append(ConstantRCSSensingTarget(
            name="ref-target", sigma=1.0, length=h, width=h, height=h,
            position=[target[0], target[1], target[2] + h / 2], orientation=[0.0, 0.0, 0.0],
            velocity=[0.0, 0.0, 0.0],
        ))
    scene.edit(add=add or None, remove=[actor])
    paths = PathSolver(deterministic=True)(
        scene, max_depth=2, los=True, specular_reflection=True, diffuse_reflection=diffuse,
        refraction=False, diffraction=False, edge_diffraction=False, synthetic_array=True,
        seed=SEED, samples_per_src=SAMPLES, max_num_paths_per_src=1_000_000,
    )
    a_re, a_im = (np.asarray(x.numpy()) for x in paths.a)
    a = (a_re + 1j * a_im)[0, 0, 0, 0, :]
    tau = np.asarray(paths.tau.numpy())[0, 0, :]
    valid = np.asarray(paths.valid.numpy())[0, 0, :]
    inter = np.asarray(paths.interactions.numpy())[:, 0, 0, :]
    verts = np.asarray(paths.vertices.numpy())[:, 0, 0, :, :]
    out = []
    for p in range(tau.shape[0]):
        if not valid[p] or tau[p] < 0 or abs(a[p]) <= 0:
            continue
        codes = [int(c) for c in inter[:, p] if int(c) != 0]
        pts = [verts[d, p] for d in range(len(codes))]
        out.append((codes, pts, float(tau[p]), 20 * math.log10(abs(a[p]))))
    return out


def _seam_rows(paths):
    rows = []
    for p in paths:
        codes = [_TYPES[i.type] for i in p.interactions]
        pts = [np.asarray(i.point) for i in p.interactions]
        rows.append((codes, pts, p.delay_ns * 1e-9, p.path_gain_db))
    return rows


def _specular(rows):
    return [r for r in rows if 2 not in r[0]]


def _match_one_to_one(seam, ref):
    """Pair every SEAM path with the reference path of the same interaction
    sequence and nearest vertices (< 1 cm); every pair within 0.1 ns and
    0.1 dB, and nothing left over on either side."""
    left = list(range(len(ref)))
    worst = (0.0, 0.0)
    for codes, pts, tau, gain in seam:
        best, best_d = None, math.inf
        for j in left:
            rc, rp, _, _ = ref[j]
            if rc != codes:
                continue
            d = max((float(np.linalg.norm(a - b)) for a, b in zip(pts, rp)), default=0.0)
            if d < best_d:
                best, best_d = j, d
        assert best is not None and best_d < 0.01, (codes, pts, best_d)
        left.remove(best)
        d_tau, d_db = abs(tau - ref[best][2]), abs(gain - ref[best][3])
        assert d_tau < 0.1e-9 and d_db < 0.1, (codes, d_tau, d_db)
        worst = (max(worst[0], d_tau), max(worst[1], d_db))
    assert not left, [ref[j][0] for j in left]
    return worst


def _total_db(rows) -> float:
    return 10 * math.log10(sum(10 ** (r[3] / 10) for r in rows))


# ---------------------------------------------------------------- tests


def test_h0_h1_specular_paths_match_an_independent_path_solver(project: Path):
    result = _run(project, _scene(), {"mode": "both", "diffuse_reflection": False}, num_frames=2)
    assert not [w for w in result.warnings if "failed" in w], result.warnings
    s = result.frames[0].sensing
    meta = s.background_meta
    assert meta.mechanisms == ["los", "specular_reflection"] and meta.deterministic
    for hyp in ("h0", "h1"):
        seam = _seam_rows(getattr(s, f"background_{hyp}"))
        ref = _reference(project, hyp, diffuse=False)
        assert len(seam) == len(ref) > 0
        d_tau, d_db = _match_one_to_one(seam, ref)
        print(f"{hyp}: {len(seam)} paths, max |dtau| {d_tau * 1e9:.2e} ns, max |dP| {d_db:.2e} dB")
    # The wall bounce leaves through the target in H1 only.
    walls = {
        hyp: [p for p in getattr(s, f"background_{hyp}")
              if [i.prim_id for i in p.interactions] == ["/building"]]
        for hyp in ("h0", "h1")
    }
    assert len(walls["h0"]) == 1 and walls["h1"] == []
    # Static nodes: frame 1 reuses frame 0.
    assert result.frames[1].sensing.background_meta.h0.cache_hit


def test_diffuse_background_power_matches_within_half_db(project: Path):
    result = _run(project, _scene(), {"mode": "both"}, num_frames=1)
    s = result.frames[0].sensing
    assert "diffuse_reflection" in s.background_meta.mechanisms
    for hyp in ("h0", "h1"):
        seam_paths = getattr(s, f"background_{hyp}")
        seam = _seam_rows(seam_paths)
        ref = _reference(project, hyp, diffuse=True)
        assert any(2 in r[0] for r in seam), "no diffuse path"
        diff = _total_db(seam) - _total_db(ref)
        print(f"{hyp}: SEAM {_total_db(seam):.4f} dB ref {_total_db(ref):.4f} dB, "
              f"{len(seam)} / {len(ref)} paths")
        assert abs(diff) < 0.5
        assert getattr(s.background_meta, hyp).total_power_db["tx_a>rx_b"] == pytest.approx(
            _total_db(seam), abs=1e-9
        )
        # Specular subset still pairs one to one.
        _match_one_to_one(_specular(seam), _specular(ref))
        assert all(p.target_id is None and p.doppler_hz == 0.0 for p in seam_paths)


def _hits_box(a, b, center, half) -> bool:
    a, b = np.asarray(a, float), np.asarray(b, float)
    lo, hi = np.asarray(center) - half, np.asarray(center) + half
    t0, t1, d = 0.0, 1.0, b - a
    for k in range(3):
        if abs(d[k]) < 1e-15:
            if not lo[k] <= a[k] <= hi[k]:
                return False
            continue
        u, v = sorted(((lo[k] - a[k]) / d[k], (hi[k] - a[k]) / d[k]))
        t0, t1 = max(t0, u), min(t1, v)
    return t0 <= t1


def test_h0_and_h1_differ_only_on_paths_through_the_target(project: Path):
    result = _run(project, _scene(), {"mode": "both"}, num_frames=1)
    s = result.frames[0].sensing
    h0, h1 = s.background_h0, s.background_h1

    def index(pool):
        """prim-id sequence -> sorted delays [ns]."""
        out: dict[tuple, list[float]] = {}
        for q in pool:
            out.setdefault(tuple(i.prim_id for i in q.interactions), []).append(q.delay_ns)
        return {k: np.sort(v) for k, v in out.items()}

    def matched(p, idx):
        taus = idx.get(tuple(i.prim_id for i in p.interactions))
        if taus is None:
            return False
        j = np.searchsorted(taus, p.delay_ns)
        near = taus[max(j - 1, 0):j + 1]
        return bool(np.any(np.abs(near - p.delay_ns) < 0.1))

    idx0, idx1 = index(h0), index(h1)
    assert all(matched(p, idx0) for p in h1)  # H1 is a subset of H0
    center = (TARGET_ON_LEG[0], TARGET_ON_LEG[1], TARGET_ON_LEG[2] + TARGET_SIZE / 2)
    only_h0 = [p for p in h0 if not matched(p, idx1)]
    assert only_h0
    for p in only_h0:
        assert any(
            _hits_box(a, b, center, TARGET_SIZE / 2 + 0.01)
            for a, b in zip(p.vertices[:-1], p.vertices[1:])
        ), [i.prim_id for i in p.interactions]

    far = _run(project, _scene(TARGET_ABOVE), {"mode": "both"}, num_frames=1)
    fs = far.frames[0].sensing
    assert fs.background_h0 == fs.background_h1


def test_static_runs_cache_and_repeat_bit_identically(project: Path):
    first = _run(project, _scene(), {"mode": "both"})
    second = _run(project, _scene(), {"mode": "both"})
    for run in (first, second):
        for i, frame in enumerate(run.frames):
            meta = frame.sensing.background_meta
            assert meta.h0.cache_hit == (i > 0) and meta.h1.cache_hit == (i > 0)
        bg = run.metadata["sensing"]["background"]
        assert (bg["solves_h0"], bg["cache_hits_h0"]) == (1, 2)
    a, b = first.frames[0].sensing, second.frames[0].sensing
    assert a.model_dump(mode="json")["background_h0"] == b.model_dump(mode="json")["background_h0"]
    assert a.model_dump(mode="json")["background_h1"] == b.model_dump(mode="json")["background_h1"]
    assert a.background_meta.h0.cache_key == b.background_meta.h0.cache_key


def test_early_cap_keeps_exactly_the_strongest_paths(project: Path):
    """max_paths_per_link drops paths before they become RayPath rows; the
    kept set equals the strongest-k cut of the uncapped solve."""
    scene, library, backend = _scene(), _library(), SionnaBackend()
    cfg = _config(rx_ids=["rx_b"])
    targets = select_targets(scene, None)
    full, _, _, none_dropped = solve_background(
        backend, project, scene, library, cfg,
        BackgroundOptions(mode="h0_removed", max_paths_per_link=None), targets, "h0",
    )
    capped, _, _, dropped = solve_background(
        backend, project, scene, library, cfg,
        BackgroundOptions(mode="h0_removed", max_paths_per_link=50), targets, "h0",
    )
    assert none_dropped == 0 and len(full) > 50
    expected, n_cut = _keep_strongest(full, 50)
    assert len(capped) == 50 and dropped == n_cut == len(full) - 50

    def rows(paths):
        return [p.model_dump(mode="json", exclude={"path_id"}) for p in paths]

    assert rows(capped) == rows(expected)


def test_cached_scene_is_clean_after_background_and_material_scales(project: Path):
    scene = _scene()
    library = _library()
    _run(project, scene, {"mode": "both"}, num_frames=1)
    backend = SionnaBackend()
    names = backend.radio_material_names(project, scene, library)
    assert "trackb_ground" in names and "trackb_wall" in names
    assert not [n for n in names if n.startswith("seam-st-")]

    rt_scene = _load_scene_cached(project / "rf" / "generated_scene.xml", [])

    def eps_sigma(name):
        m = rt_scene.radio_materials[name]
        return (float(np.asarray(m.relative_permittivity).reshape(-1)[0]),
                float(np.asarray(m.conductivity).reshape(-1)[0]))

    nominal = eps_sigma("trackb_ground")
    cfg = _config(rx_ids=["rx_b"])
    opts = BackgroundOptions(mode="h0_removed", diffuse_reflection=False)
    targets = select_targets(scene, None)
    base, _, _, _ = solve_background(backend, project, scene, library, cfg, opts, targets, "h0")
    scaled, warns, _, _ = solve_background(
        backend, project, scene, library, cfg, opts, targets, "h0",
        material_scales={"trackb_ground": (3.0, 2.0), "ghost": (1.0, 1.0)},
    )
    assert any("'ghost'" in w for w in warns)
    assert eps_sigma("trackb_ground") == nominal == (5.0, pytest.approx(0.01, rel=1e-6))

    def ground_db(paths):
        return [p.path_gain_db for p in paths
                if [i.prim_id for i in p.interactions] == ["/ground"]]

    assert ground_db(base) and ground_db(scaled) != ground_db(base)
    los = [p.path_gain_db for p in base if not p.interactions]
    assert los == [p.path_gain_db for p in scaled if not p.interactions]

    assert rt_scene.sensing_targets == {}
    assert not [m for m in rt_scene.radio_materials if m.startswith("seam-st-")]
    assert not getattr(rt_scene, "_seam_hidden_actor_objects", [])
    assert _actor_object_key(rt_scene, "tgt_01") is not None
