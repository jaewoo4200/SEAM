"""Radar sensing (RCS) targets: schema rules, the mock radar-equation backend,
POST /simulate/sensing + GET /results/sensing, and the three exports that
carry sensing results (RFData sensing.json, AODT source="sensing", channel
npz include_sensing).

Mock backend throughout, so every number is analytic; the real RCSSolver is
exercised by test_sensing_sionna.py.
"""

import json
import math
from pathlib import Path

import numpy as np
import pytest
from pydantic import ValidationError

from seam_studio.schemas.devices import Device
from seam_studio.schemas.scene import Actor, ActorTrajectory, Scene
from seam_studio.schemas.sensing import (
    SensingSimulateRequest,
    SensingTargetSpec,
    resolve_model_type,
    resolve_object_type,
)
from seam_studio.schemas.simulation import SimulationConfig
from seam_studio.services.project_store import load_default_library
from seam_studio.services.scenario import actor_velocity_at
from seam_studio.services.sensing import (
    dbsm_to_m2,
    resolve_target,
    run_sensing,
    select_targets,
    target_from_summary,
    target_summary,
)
from seam_studio.services.simulation_backends.base import RayTracingBackend
from seam_studio.services.simulation_backends.mock_backend import MockBackend

C = 299_792_458.0
F = 28e9
LAM = C / F
PID = "sensing_api"
MOCK_CFG = {"id": "default", "backend": "mock", "frequency_hz": F}


def _car(actor_id: str = "car_01", position=(30.0, 0.0, 0.0), **sensing) -> Actor:
    spec = {"model": "tr38901", **sensing}
    return Actor(id=actor_id, kind="car", position=list(position), sensing=spec)


def _scene(
    actors: list[Actor],
    tx=(0.0, 0.0, 10.0),
    rx=(0.0, 0.0, 10.0),
    *,
    tx_velocity=None,
    scene_id: str = "sensing",
) -> Scene:
    return Scene(
        scene_id=scene_id,
        devices=[
            Device(
                id="tx_001",
                kind="tx",
                position=list(tx),
                power_dbm=30.0,
                velocity_m_s=list(tx_velocity) if tx_velocity else None,
            ),
            Device(id="rx_001", kind="rx", position=list(rx)),
        ],
        actors=actors,
    )


def _solve(scene: Scene, config: SimulationConfig | None = None, **request):
    config = config or SimulationConfig(**MOCK_CFG)
    req = SensingSimulateRequest(**request)
    return run_sensing(
        MockBackend(),
        Path("."),
        scene,
        load_default_library(),
        config,
        req,
        select_targets(scene, req.target_actor_ids),
    )


# ------------------------------------------------------------------ schema


def test_spec_defaults():
    spec = SensingTargetSpec()
    assert spec.model == "tr38901"
    assert spec.enabled is True
    assert spec.random_components is False
    for name in ("object_type", "model_type", "rcs_dbsm", "xpr_db", "size_m", "velocity_m_s"):
        assert getattr(spec, name) is None, name


def test_custom_actor_tr38901_requires_object_type():
    with pytest.raises(ValidationError, match="needs sensing.object_type"):
        Actor(id="c1", kind="custom", position=[0, 0, 0], sensing={"model": "tr38901"})
    ok = Actor(
        id="c1",
        kind="custom",
        position=[0, 0, 0],
        sensing={"model": "tr38901", "object_type": "agv-single-sp"},
    )
    assert ok.sensing.object_type == "agv-single-sp"
    assert Actor(id="c1", kind="custom", position=[0, 0, 0], sensing={"model": "constant"})


def test_object_type_literal_rejects_ambiguous():
    # Sionna only knows the single/multi scattering-point variants.
    with pytest.raises(ValidationError):
        SensingTargetSpec(object_type="vehicle")
    with pytest.raises(ValidationError):
        SensingTargetSpec(object_type="agv")


def test_model_type_pairs():
    # Kind uav derives uav-small-size, which only defines model 1.
    with pytest.raises(ValidationError, match="does not define RCS model 2"):
        Actor(id="u1", kind="uav", position=[0, 0, 5], sensing={"model_type": 2})
    assert Actor(id="h1", kind="human", position=[0, 0, 0], sensing={"model_type": 1})
    with pytest.raises(ValidationError, match="uav-large-size"):
        SensingTargetSpec(object_type="uav-large-size", model_type=1)
    uav = Actor(id="u1", kind="uav", position=[0, 0, 5], sensing={})
    ot = resolve_object_type(uav.kind, uav.sensing)
    assert (ot, resolve_model_type(ot, uav.sensing)) == ("uav-small-size", 1)
    # The derived type is never written back into the stored spec.
    assert uav.sensing.object_type is None and uav.sensing.model_type is None


def test_fields_must_match_model():
    with pytest.raises(ValidationError, match="'constant' only"):
        SensingTargetSpec(model="tr38901", rcs_dbsm=3.0)
    with pytest.raises(ValidationError, match="'tr38901' only"):
        SensingTargetSpec(model="constant", object_type="human")
    with pytest.raises(ValidationError, match="'tr38901' only"):
        SensingTargetSpec(model="constant", random_components=True)
    with pytest.raises(ValidationError, match="size_m"):
        SensingTargetSpec(size_m=[1.0, 0.0, 1.0])


def test_dbsm_to_m2():
    assert dbsm_to_m2(0.0) == pytest.approx(1.0)
    assert dbsm_to_m2(10.0) == pytest.approx(10.0)
    assert dbsm_to_m2(-10.0) == pytest.approx(0.1)
    actor = Actor(id="c1", kind="custom", position=[0, 0, 0], sensing={"model": "constant"})
    assert resolve_target(actor).rcs_dbsm == 0.0


def test_resolve_target_center_velocity_rcs():
    car = _car(position=(1.0, 2.0, 0.0))
    t = resolve_target(car)
    assert t.center == (1.0, 2.0, 0.75)  # base + height/2 (car is 1.5 m tall)
    assert t.object_type == "vehicle-multi-sp" and t.model_type == 2
    assert t.rcs_dbsm == 11.25
    assert t.velocity_m_s == (0.0, 0.0, 0.0)  # static, no trajectory

    explicit = _car(velocity_m_s=[3.0, -1.0, 0.0])
    explicit.trajectory = ActorTrajectory(waypoints=[[0, 0, 0], [10, 0, 0]], dt_s=1.0)
    assert resolve_target(explicit).velocity_m_s == (3.0, -1.0, 0.0)

    moving = _car()
    moving.trajectory = ActorTrajectory(waypoints=[[0, 0, 0], [10, 0, 0]], dt_s=1.0)
    expected = tuple(actor_velocity_at(moving, 0.0))
    assert resolve_target(moving).velocity_m_s == pytest.approx(expected)
    assert expected[0] == pytest.approx(10.0)

    sized = _car(size_m=[2.0, 1.0, 4.0])
    ts = resolve_target(sized)
    assert ts.size_m == (2.0, 1.0, 4.0)
    assert ts.center[2] == pytest.approx(2.0)


def test_trajectory_target_takes_the_t0_pose():
    # Authored at the origin facing +x; the trajectory starts elsewhere and
    # runs along +y, so playback's first frame faces +y at the first waypoint.
    car = Actor(
        id="car_01", kind="car", position=[0.0, 0.0, 0.0],
        orientation_deg=[0.0, 2.0, 3.0], sensing={"model": "tr38901"},
        trajectory=ActorTrajectory(waypoints=[[5, 5, 0], [5, 15, 0]], dt_s=1.0),
    )
    t = resolve_target(car)
    assert t.center == pytest.approx((5.0, 5.0, 0.75))
    assert t.orientation_deg == pytest.approx((90.0, 2.0, 3.0))  # pitch/roll authored
    assert t.velocity_m_s == pytest.approx((0.0, 10.0, 0.0))

    # An explicit velocity keeps the authored pose.
    car.sensing.velocity_m_s = [1.0, 0.0, 0.0]
    t = resolve_target(car)
    assert t.center == (0.0, 0.0, 0.75)
    assert t.orientation_deg == (0.0, 2.0, 3.0)

    # The stored summary carries the pose, so the target can be rebuilt.
    car.sensing.velocity_m_s = None
    resolved = resolve_target(car)
    summary = target_summary(resolved, [list(resolved.center)], 0)
    assert summary.orientation_deg == pytest.approx([90.0, 2.0, 3.0])
    rebuilt = target_from_summary(summary)
    assert rebuilt.center == pytest.approx(resolved.center)
    assert rebuilt.orientation_deg == pytest.approx(resolved.orientation_deg)
    assert (rebuilt.model, rebuilt.object_type, rebuilt.model_type) == (
        "tr38901", "vehicle-multi-sp", 2,
    )


def test_scene_roundtrip_and_legacy():
    scene = _scene([_car(velocity_m_s=[-10.0, 0.0, 0.0])])
    again = Scene.model_validate(json.loads(json.dumps(scene.model_dump(mode="json"))))
    assert again.model_dump() == scene.model_dump()
    legacy = Actor.model_validate({"id": "car_02", "kind": "car", "position": [0, 0, 0]})
    assert legacy.sensing is None


def test_unbound_actor_keeps_its_scene_hash():
    from seam_studio.api.simulate import _provenance_hashes, _sha256

    scene = _scene([Actor(id="car_01", kind="car", position=[30, 0, 0])])
    payload = scene.model_dump(mode="json")
    for key in ("result_sets", "revision"):
        payload.pop(key)
    for actor in payload["actors"]:
        del actor["sensing"]  # the pre-sensing actor layout
    assert _provenance_hashes(scene, None)["scene_hash"] == _sha256(payload)
    bound = _scene([_car()])
    assert _provenance_hashes(bound, None)["scene_hash"] != _sha256(payload)


def test_sensing_edit_does_not_stale_the_rf_projection(tmp_path: Path):
    import trimesh

    from seam_studio.schemas.scene import MeshRef, Prim, RFBinding
    from seam_studio.services.rf_compiler import compile_project, projection_is_stale

    project = tmp_path / "proj"
    (project / "visual").mkdir(parents=True)
    tm = trimesh.Scene()
    tm.add_geometry(
        trimesh.creation.box(extents=(40.0, 40.0, 0.2)), geom_name="ground", node_name="ground"
    )
    (project / "visual" / "scene.glb").write_bytes(tm.export(file_type="glb"))
    scene = _scene([Actor(id="car_01", kind="car", position=[30, 0, 0])])
    scene.prims = [
        Prim(
            id="/ground",
            name="ground",
            mesh_ref=MeshRef(mesh_name="ground"),
            rf=RFBinding(
                material_id="ground",
                assignment_status="user_confirmed",
                assignment_sources=["user"],
            ),
        )
    ]
    library = load_default_library()
    assert compile_project(project, scene, library).ok
    scene.actors[0].sensing = SensingTargetSpec(velocity_m_s=[5.0, 0.0, 0.0])
    assert projection_is_stale(project, scene, library) is False


# --------------------------------------------------------------- mock model


def test_mock_sensing_deterministic():
    scene = _scene([_car(velocity_m_s=[-10.0, 0.0, 0.0])])
    assert _solve(scene).model_dump() == _solve(scene).model_dump()


def _constant_target(position, velocity=None, rcs_dbsm=10.0) -> Actor:
    spec = {"model": "constant", "rcs_dbsm": rcs_dbsm}
    if velocity is not None:
        spec["velocity_m_s"] = list(velocity)
    return Actor(id="t1", kind="car", position=list(position), sensing=spec)


def test_mock_radar_equation():
    # Target center at z = 0.75 (car height 1.5); radar at the same height.
    # 20 dBsm = 100 m^2: 10 dBsm is a fixed point of dBsm -> m^2 and would
    # hide a missing conversion.
    mono = _scene(
        [_constant_target([30.0, 0.0, 0.0], rcs_dbsm=20.0)], tx=(0, 0, 0.75), rx=(0, 0, 0.75)
    )
    path = _solve(mono).paths[0]
    expected = 10.0 * math.log10(LAM**2 * 100.0 / ((4 * math.pi) ** 3 * 30.0**4))
    assert path.path_gain_db == pytest.approx(expected, abs=1e-9)

    def bistatic(tx, rx) -> float:
        scene = _scene([_constant_target([0.0, 0.0, 0.0])], tx=tx, rx=rx)
        return _solve(scene).paths[0].path_gain_db

    base = bistatic((30.0, 0.0, 0.75), (0.0, 30.0, 0.75))
    far_tx = bistatic((60.0, 0.0, 0.75), (0.0, 30.0, 0.75))
    far_rx = bistatic((30.0, 0.0, 0.75), (0.0, 60.0, 0.75))
    assert base - far_tx == pytest.approx(20.0 * math.log10(2.0), abs=1e-9)
    assert base - far_rx == pytest.approx(20.0 * math.log10(2.0), abs=1e-9)


def test_mock_delay_phase_doppler():
    def solve(velocity, tx_velocity=None):
        scene = _scene(
            [_constant_target([30.0, 0.0, 0.0], velocity)],
            tx=(0, 0, 0.75),
            rx=(0, 0, 0.75),
            tx_velocity=tx_velocity,
        )
        return _solve(scene).paths[0]

    receding = solve([10.0, 0.0, 0.0])
    assert receding.delay_ns == pytest.approx(60.0 / C * 1e9)
    assert receding.phase_rad == pytest.approx(
        math.remainder(-2 * math.pi * F * 60.0 / C, 2 * math.pi)
    )
    assert receding.doppler_hz == pytest.approx(-2 * 10.0 / LAM)
    assert solve([-10.0, 0.0, 0.0]).doppler_hz == pytest.approx(2 * 10.0 / LAM)
    assert solve([0.0, 10.0, 0.0]).doppler_hz == pytest.approx(0.0, abs=1e-9)
    # A TX closing on a static target: v_tx . k_ts / lambda.
    assert solve(None, tx_velocity=[5.0, 0.0, 0.0]).doppler_hz == pytest.approx(5.0 / LAM)


def test_mock_path_shape():
    scene = Scene(
        scene_id="shape",
        devices=[
            Device(id="tx_001", kind="tx", position=[0, 0, 10]),
            Device(id="tx_002", kind="tx", position=[5, 5, 10]),
            Device(id="rx_001", kind="rx", position=[0, 0, 10]),
        ],
        actors=[_car("car_01", (30, 0, 0)), _car("car_02", (0, 40, 0))],
    )
    result = _solve(scene)
    order = [(p.path_id, p.tx_id, p.target_id) for p in result.paths]
    assert order == [
        ("sensing_0001", "tx_001", "car_01"),
        ("sensing_0002", "tx_001", "car_02"),
        ("sensing_0003", "tx_002", "car_01"),
        ("sensing_0004", "tx_002", "car_02"),
    ]
    first = result.paths[0]
    assert first.path_type == "sensing"
    assert first.vertices == [[0, 0, 10], [30, 0, 0.75], [0, 0, 10]]
    assert len(first.interactions) == 1
    inter = first.interactions[0]
    assert inter.type == "sensing" and inter.prim_id == "car_01" == first.target_id
    assert inter.rf_material_id is None
    summary = {t.actor_id: t for t in result.targets}
    assert summary["car_01"].num_scattering_points == 1
    assert summary["car_01"].scattering_points == [[30.0, 0.0, 0.75]]
    assert summary["car_01"].path_count == 2 and summary["car_02"].path_count == 2
    assert result.metadata["sensing_path_count"] == 4


def test_mock_los_false_no_paths():
    scene = _scene([_car()])
    result = _solve(scene, SimulationConfig(**MOCK_CFG, los=False))
    assert result.paths == []
    assert any("los=False" in w for w in result.warnings)


def test_mock_colocated_tx_rx_has_no_los():
    # Sionna has no direct path between a co-located (monostatic) TX and RX;
    # friis_dbm's 0.1 m clamp would otherwise add a delay-0 tap that swamps
    # every echo of a combined sensing result.
    lib = load_default_library()
    config = SimulationConfig(**MOCK_CFG)
    mono = MockBackend().simulate_paths(Path("."), _scene([_car()]), lib, config)
    assert mono.paths and not [p for p in mono.paths if p.path_type == "los"]
    assert min(p.delay_ns for p in mono.paths) > 0.0
    bistatic = MockBackend().simulate_paths(
        Path("."), _scene([_car()], rx=(10.0, 0.0, 10.0)), lib, config
    )
    assert [p.path_type for p in bistatic.paths].count("los") == 1


class _CommStub(MockBackend):
    """Mock whose comm solve reports per-path Doppler (as a moving sionna
    solve does) and records the targets it was handed."""

    name = "stub"

    def __init__(self):
        self.seen_targets = None

    def simulate_paths(self, project_dir, scene, library, config):
        result = super().simulate_paths(project_dir, scene, library, config)
        result.metadata["doppler_hz"] = [12.5 * (i + 1) for i in range(len(result.paths))]
        return result

    def simulate_paths_with_targets(self, project_dir, scene, library, config, targets):
        self.seen_targets = [t.actor_id for t in targets]
        return super().simulate_paths_with_targets(project_dir, scene, library, config, targets)


def test_run_sensing_comm_paths_carry_doppler_and_see_the_targets():
    scene = _scene([_car(velocity_m_s=[-10.0, 0.0, 0.0])], rx=(10.0, 0.0, 10.0))
    backend = _CommStub()
    req = SensingSimulateRequest(include_comm_paths=True)
    result = run_sensing(
        backend, Path("."), scene, load_default_library(), SimulationConfig(**MOCK_CFG),
        req, select_targets(scene, None),
    )
    plain = MockBackend().simulate_paths(
        Path("."), scene, load_default_library(), SimulationConfig(**MOCK_CFG)
    )
    comm = [p for p in result.paths if p.target_id is None]
    assert [p.path_id for p in comm] == [p.path_id for p in plain.paths]
    assert [p.doppler_hz for p in comm] == [12.5 * (i + 1) for i in range(len(comm))]
    # The comm half is solved with the targets standing in for their actors.
    assert backend.seen_targets == ["car_01"]


def test_select_targets_rules():
    disabled = _car("car_02", enabled=False)
    scene = _scene([_car(), disabled, Actor(id="plain", kind="human", position=[1, 1, 0])])
    assert [t.actor_id for t in select_targets(scene, None)] == ["car_01"]
    # Explicit ids keep request order and drop duplicates.
    scene.actors[1].sensing.enabled = True
    picked = select_targets(scene, ["car_02", "car_01", "car_02"])
    assert [t.actor_id for t in picked] == ["car_02", "car_01"]


# ---------------------------------------------------------------------- API


def _api_scene(**sensing) -> dict:
    spec = {"velocity_m_s": [-10.0, 0.0, 0.0], **sensing}
    return _scene([_car(**spec)], scene_id=PID).model_dump(mode="json")


@pytest.fixture()
def client(api_client):
    resp = api_client.post("/api/projects", json={"name": "Sensing", "project_id": PID})
    assert resp.status_code == 201, resp.text
    put = api_client.put(f"/api/projects/{PID}/scene", json=_api_scene())
    assert put.status_code == 200, put.text
    return api_client


def _project_dir(project_id: str = PID) -> Path:
    from seam_studio.api import deps

    return deps.get_store().resolve(project_id)


def _post(client, body=None, project_id: str = PID):
    return client.post(
        f"/api/projects/{project_id}/simulate/sensing",
        json={"config": MOCK_CFG} if body is None else body,
    )


def test_api_solve_persists_and_reloads(client):
    resp = _post(client)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "sensing"
    assert body["result_id"] == "mock_sensing_001"
    assert body["metadata"]["scene_hash"].startswith("sha256:")
    assert body["metadata"]["sensing_path_count"] == 1
    path = body["paths"][0]
    assert path["path_gain_db"] == pytest.approx(-121.0067, abs=1e-4)
    assert path["delay_ns"] == pytest.approx(209.436, abs=1e-3)
    assert path["doppler_hz"] == pytest.approx(1785.034, abs=1e-3)
    assert body["targets"][0]["rcs_dbsm"] == 11.25

    refs = client.get(f"/api/projects/{PID}/scene").json()["result_sets"]
    assert [r["kind"] for r in refs] == ["sensing"]
    assert refs[0]["result_id"] == "mock_sensing_001"

    again = client.get(f"/api/projects/{PID}/results/sensing")
    assert again.status_code == 200
    assert again.json() == body
    by_id = client.get(
        f"/api/projects/{PID}/results/sensing", params={"result_id": "mock_sensing_001"}
    )
    assert by_id.json() == body
    missing = client.get(
        f"/api/projects/{PID}/results/sensing", params={"result_id": "nope"}
    )
    assert missing.status_code == 404


def test_api_404_unknown_project(api_client):
    assert _post(api_client, project_id="ghost").status_code == 404
    assert api_client.get("/api/projects/ghost/results/sensing").status_code == 404


def test_api_404_unknown_config_and_no_result(client):
    assert _post(client, {"config_id": "nope"}).status_code == 404
    assert client.get(f"/api/projects/{PID}/results/sensing").status_code == 404


def test_api_400(client):
    resp = _post(client, {"config": MOCK_CFG, "target_actor_ids": ["ghost"]})
    assert resp.status_code == 400 and "actor not found: ghost" in resp.json()["detail"]

    resp = _post(client, {"config": MOCK_CFG, "tx_ids": ["ghost_tx"]})
    assert resp.status_code == 400 and "tx device not found: ghost_tx" in resp.json()["detail"]

    client.put(f"/api/projects/{PID}/scene", json=_api_scene(enabled=False))
    resp = _post(client, {"config": MOCK_CFG, "target_actor_ids": ["car_01"]})
    assert resp.status_code == 400
    assert "has no enabled sensing binding" in resp.json()["detail"]

    unbound = _scene([Actor(id="car_01", kind="car", position=[30, 0, 0])], scene_id=PID)
    client.put(f"/api/projects/{PID}/scene", json=unbound.model_dump(mode="json"))
    resp = _post(client)
    assert resp.status_code == 400 and "no sensing targets" in resp.json()["detail"]

    no_rx = _scene([_car()], scene_id=PID)
    no_rx.devices = [d for d in no_rx.devices if d.kind == "tx"]
    client.put(f"/api/projects/{PID}/scene", json=no_rx.model_dump(mode="json"))
    resp = _post(client)
    assert resp.status_code == 400 and "at least one tx and one rx" in resp.json()["detail"]


def test_api_409_sionna_without_rcs(client, monkeypatch):
    from seam_studio.services import availability
    from seam_studio.services.simulation_backends.sionna_backend import SionnaBackend

    monkeypatch.setattr(SionnaBackend, "is_available", lambda self: True)
    monkeypatch.setattr(availability, "sionna_rcs_available", lambda: False)

    resp = _post(client, {"config": {**MOCK_CFG, "backend": "sionna"}})
    assert resp.status_code == 409
    assert "sionna-rt>=2.2" in resp.json()["detail"]

    auto = _post(client, {"config": {**MOCK_CFG, "backend": "auto"}})
    assert auto.status_code == 200, auto.text
    assert auto.json()["backend"] == "mock"
    assert any("needs >=2.2" in w for w in auto.json()["warnings"])


def test_api_422(client):
    assert _post(client, {"samples_per_sp": 0}).status_code == 422
    assert _post(client, {"target_actor_ids": []}).status_code == 422
    assert _post(client, {"bogus": 1}).status_code == 422


def test_api_put_scene_rejects_invalid_binding(client):
    bad = _scene(
        [Actor(id="c1", kind="custom", position=[0, 0, 0])], scene_id=PID
    ).model_dump(mode="json")
    bad["actors"][0]["sensing"] = {"model": "tr38901"}
    assert client.put(f"/api/projects/{PID}/scene", json=bad).status_code == 422


def test_include_comm_paths(client):
    resp = _post(client, {"config": MOCK_CFG, "include_comm_paths": True})
    assert resp.status_code == 200, resp.text
    body = resp.json()
    sensing = [p for p in body["paths"] if p["target_id"] is not None]
    comm = [p for p in body["paths"] if p["target_id"] is None]
    assert sensing and comm
    # Sensing first, then the comm paths with their own id series.
    assert body["paths"][: len(sensing)] == sensing
    assert all(p["path_id"].startswith("sensing_") for p in sensing)
    assert all(p["path_id"].startswith("path_") for p in comm)
    assert body["metadata"]["comm_path_count"] == len(comm)
    assert body["metadata"]["sensing_path_count"] == len(sensing)
    # Monostatic: no delay-0 TX->RX tap next to the echoes (Sionna has none).
    assert all(p["path_type"] != "los" and p["delay_ns"] > 0 for p in comm)


def test_request_hash_pins_sensing_only_knobs(client):
    first = _post(client, {"config": MOCK_CFG, "max_depth": 1}).json()["metadata"]
    second = _post(client, {"config": MOCK_CFG, "max_depth": 3}).json()["metadata"]
    third = _post(client, {"config": MOCK_CFG, "max_depth": 3}).json()["metadata"]
    for key in ("scene_hash", "rf_assignment_hash", "sim_config_hash"):
        assert first[key] == second[key], key
    assert first["request_hash"].startswith("sha256:")
    assert first["request_hash"] != second["request_hash"]
    assert second["request_hash"] == third["request_hash"]
    from seam_studio.api.simulate import _sha256

    assert second["request_hash"] == _sha256(second["sensing_request"])


def test_prune_accepts_sensing(client):
    assert _post(client).status_code == 200
    target = _project_dir() / "results" / "mock_sensing_001.json"
    assert target.is_file()
    resp = client.post(
        f"/api/projects/{PID}/results/prune", json={"kinds": ["sensing"], "keep_latest": 0}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["removed"] == ["mock_sensing_001"]
    assert not target.exists()
    assert client.get(f"/api/projects/{PID}/scene").json()["result_sets"] == []


def test_capabilities(api_client):
    class _Bare(RayTracingBackend):
        name = "bare"

        def is_available(self):
            return True

        def simulate_paths(self, *args):  # pragma: no cover - never called
            raise NotImplementedError

        def simulate_radio_map(self, *args):  # pragma: no cover - never called
            raise NotImplementedError

    assert _Bare().capabilities()["sensing"] is False
    assert MockBackend().capabilities()["sensing"] is True
    listing = {b["name"]: b for b in api_client.get("/api/backends").json()}
    assert listing["mock"]["capabilities"]["sensing"] is True


# ------------------------------------------------------------------ exports


def test_rfdata_writes_sensing_json(client):
    out = _project_dir() / "export" / "rfdata" / "sensing.json"
    first = client.post(f"/api/projects/{PID}/export/rfdata", json={})
    assert first.status_code == 200, first.text
    assert first.json()["has_sensing"] is False
    assert not out.exists()

    assert _post(client).status_code == 200
    resp = client.post(f"/api/projects/{PID}/export/rfdata", json={})
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_sensing"] is True
    assert "export/rfdata/sensing.json" in resp.json()["files"]
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["result_id"] == "mock_sensing_001"
    assert data["targets"][0]["actor_id"] == "car_01"
    row = data["paths_by_time"][0]["paths"][0]
    assert row["type"] == "SENSING"
    assert row["target_id"] == "car_01"
    assert row["doppler_hz"] == pytest.approx(1785.034, abs=1e-3)

    # Once the sensing result is gone, a re-export must not leave the old
    # sensing.json next to fresh files.
    pruned = client.post(
        f"/api/projects/{PID}/results/prune", json={"kinds": ["sensing"], "keep_latest": 0}
    )
    assert pruned.status_code == 200, pruned.text
    again = client.post(f"/api/projects/{PID}/export/rfdata", json={})
    assert again.status_code == 200, again.text
    assert again.json()["has_sensing"] is False
    assert "export/rfdata/sensing.json" not in again.json()["files"]
    assert not out.exists()


def test_aodt_source_sensing(client):
    pq = pytest.importorskip("pyarrow.parquet")
    missing = client.post(f"/api/projects/{PID}/export/aodt", json={"source": "sensing"})
    assert missing.status_code == 404

    assert _post(client).status_code == 200
    resp = client.post(f"/api/projects/{PID}/export/aodt", json={"source": "sensing"})
    assert resp.status_code == 200, resp.text
    out = _project_dir() / "export" / "aodt"
    rows = pq.read_table(out / "raypaths.parquet").to_pylist()
    assert len(rows) == 1
    assert rows[0]["interaction_types"] == ["emission", "diffuse", "reception"]
    assert rows[0]["object_ids"][1] == 1_000_000
    assert rows[0]["prim_ids"][1] == -1
    id_map = json.loads((out / "id_map.json").read_text(encoding="utf-8"))
    assert id_map["sensing_targets"] == {"car_01": 1_000_000}
    assert id_map["sensing_interaction_token"] == "diffuse"
    assert id_map["source"] == "sensing"


def test_channel_npz_include_sensing(client):
    request = {"config": MOCK_CFG, "ue_source": "devices", "max_paths": 8}
    missing = client.post(
        f"/api/projects/{PID}/export/channel-npz", json={**request, "include_sensing": True}
    )
    assert missing.status_code == 404

    plain = client.post(f"/api/projects/{PID}/export/channel-npz", json=request)
    assert plain.status_code == 200, plain.text
    npz = _project_dir() / "export" / "channel_npz" / "channel_dataset.npz"
    plain_nlos = np.load(npz)["is_nlos"].copy()

    assert _post(client).status_code == 200
    resp = client.post(
        f"/api/projects/{PID}/export/channel-npz", json={**request, "include_sensing": True}
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sensing_path_count"] == 1
    assert body["path_count"] == plain.json()["path_count"] + 1
    assert plain.json()["sensing_path_count"] == 0
    np.testing.assert_array_equal(np.load(npz)["is_nlos"], plain_nlos)
    meta = json.loads(
        (_project_dir() / "export" / "channel_npz" / "metadata.json").read_text(
            encoding="utf-8"
        )
    )
    assert meta["include_sensing"] is True
    assert meta["sensing_result_id"] == "mock_sensing_001"
    assert not [w for w in body["warnings"] if "sensing path" in w]


def _npz(client, **body):
    request = {"config": MOCK_CFG, "ue_source": "devices", "max_paths": 8, **body}
    return client.post(f"/api/projects/{PID}/export/channel-npz", json=request)


def _npz_arrays():
    return np.load(_project_dir() / "export" / "channel_npz" / "channel_dataset.npz")


def _put_scene(client, scene: Scene) -> None:
    put = client.put(f"/api/projects/{PID}/scene", json=scene.model_dump(mode="json"))
    assert put.status_code == 200, put.text


def test_channel_npz_bistatic_echoes_join_only_their_link(client):
    # Two TXs and two RXs at four distinct positions: an echo belongs to the
    # (UE at its RX, its TX) link only - not to the TX end's UE, not to the
    # other TX's link.
    txs = {"tx_001": [0.0, 0.0, 10.0], "tx_002": [50.0, 0.0, 10.0]}
    rxs = {"rx_001": [0.0, 30.0, 1.5], "rx_002": [20.0, -30.0, 1.5]}
    scene = Scene(
        scene_id=PID,
        devices=[Device(id=i, kind="tx", position=p, power_dbm=30.0) for i, p in txs.items()]
        + [Device(id=i, kind="rx", position=p) for i, p in rxs.items()],
        actors=[_car(velocity_m_s=[-10.0, 0.0, 0.0])],
    )
    _put_scene(client, scene)
    assert _post(client).status_code == 200

    resp = _npz(client, include_sensing=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["sensing_path_count"] == 4  # one per (tx, rx) pair
    toa = _npz_arrays()["sorted_dataset_toa"]
    sp = [30.0, 0.0, 0.75]

    def echo_delay_s(tx_id, rx_id):
        return (math.dist(txs[tx_id], sp) + math.dist(sp, rxs[rx_id])) / C

    for u, rx_id in enumerate(sorted(rxs)):  # devices UE axis is sorted by id
        for t, tx_id in enumerate(txs):
            row = toa[u, t]
            own = echo_delay_s(tx_id, rx_id)
            assert np.isclose(row, own, rtol=1e-6, atol=0.0).any(), (rx_id, tx_id)
            for other_tx in txs:
                for other_rx in rxs:
                    if (other_tx, other_rx) == (tx_id, rx_id):
                        continue
                    assert not np.isclose(
                        row, echo_delay_s(other_tx, other_rx), rtol=1e-6, atol=0.0
                    ).any(), (rx_id, tx_id, other_tx, other_rx)


def test_channel_npz_rejects_stale_sensing_echoes(client):
    assert _post(client).status_code == 200  # solved at 28 GHz, tx_001 at (0, 0, 10)

    other_band = _npz(client, include_sensing=True, config={**MOCK_CFG, "frequency_hz": 3.5e9})
    assert other_band.status_code == 400
    assert "was solved at" in other_band.json()["detail"]

    moved = client.get(f"/api/projects/{PID}/scene").json()  # keeps the result refs
    moved["devices"][0]["position"] = [200.0, 0.0, 10.0]
    assert client.put(f"/api/projects/{PID}/scene", json=moved).status_code == 200
    resp = _npz(client, include_sensing=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["sensing_path_count"] == 0
    assert any("TX moved" in w for w in body["warnings"])


def test_channel_npz_empty_sensing_result_is_recorded(client):
    # los=False: the mock sensing solve stores a result without echoes.
    assert _post(client, {"config": {**MOCK_CFG, "los": False}}).status_code == 200
    resp = _npz(client, include_sensing=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["sensing_path_count"] == 0
    assert any("has no echo paths" in w for w in resp.json()["warnings"])
    meta = json.loads(
        (_project_dir() / "export" / "channel_npz" / "metadata.json").read_text(encoding="utf-8")
    )
    assert meta["include_sensing"] is True
    assert meta["sensing_result_id"] == "mock_sensing_001"


def test_channel_npz_warns_when_the_echo_rx_differs_from_the_probe(client):
    scene = _scene([_car(velocity_m_s=[-10.0, 0.0, 0.0])], scene_id=PID)
    scene.devices.append(
        Device(id="rx_002", kind="rx", position=[5.0, 5.0, 10.0], orientation_deg=[90.0, 0.0, 0.0])
    )
    _put_scene(client, scene)
    # Echoes received by rx_002 only; the UE probe is cloned from rx_001.
    assert _post(client, {"config": MOCK_CFG, "rx_ids": ["rx_002"]}).status_code == 200
    resp = _npz(client, include_sensing=True)
    assert resp.status_code == 200, resp.text
    assert resp.json()["sensing_path_count"] == 1
    assert any("orientation/antenna" in w for w in resp.json()["warnings"])
