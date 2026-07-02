"""Regression tests for defects found in the adversarial review.

Each test pins the corrected behavior:
- the compiler emits radio-material bsdfs for constant materials (Sionna 1.x
  refuses to load shapes whose bsdf is not a radio material);
- SionnaBackend._apply_custom_materials parses the manifest shape the
  compiler actually writes ("groups" + nested "custom_material");
- result ids never reuse a live id after refs are pruned via PUT /scene;
- sionna_available() treats a broken Sionna install as unavailable;
- approving the rule provider's own demo suggestions does not trigger
  VISUAL_RF_MISMATCH.
"""

import xml.etree.ElementTree as ET
from pathlib import Path

import trimesh

from app.schemas.materials import AssignRequest
from app.schemas.scene import MeshRef, Prim, RFBinding, Scene, VisualBinding
from app.services import rf_compiler
from app.services.material_assignment import assign_materials
from app.services.project_store import load_default_library
from app.services.scene_validator import validate_scene
from app.services.simulation_backends.sionna_backend import SionnaBackend


def _scene_with(prims: list[Prim]) -> Scene:
    return Scene(scene_id="regress", name="regress", prims=prims)


def _write_glb(project_dir: Path, mesh_names: list[str]) -> None:
    """Minimal visual asset so the compiler has geometry to group/export."""
    tm_scene = trimesh.Scene()
    for i, name in enumerate(mesh_names):
        box = trimesh.creation.box(extents=(2.0, 2.0, 2.0))
        box.apply_translation((i * 5.0, 0.0, 1.0))
        tm_scene.add_geometry(box, geom_name=name, node_name=name)
    out = project_dir / "visual" / "scene.glb"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(tm_scene.export(file_type="glb"))


def _mesh_prim(prim_id: str, mesh: str, material: str | None, **kwargs) -> Prim:
    rf = (
        RFBinding(
            material_id=material,
            assignment_status=kwargs.pop("status", "user_confirmed"),
            assignment_sources=["user"],
        )
        if material
        else RFBinding()
    )
    return Prim(
        id=prim_id,
        name=prim_id.rsplit("/", 1)[-1],
        mesh_ref=MeshRef(mesh_name=mesh),
        rf=rf,
        **kwargs,
    )


# ------------------------------------------------------- compiler XML output


def test_constant_materials_emit_radio_material_bsdf(tmp_path: Path):
    library = load_default_library()
    scene = _scene_with(
        [
            _mesh_prim("/roads/r01/surface", "road", "asphalt_custom"),
            _mesh_prim("/buildings/b01/walls", "wall", "itu_concrete"),
        ]
    )
    _write_glb(tmp_path, ["road", "wall"])
    result = rf_compiler.compile_project(tmp_path, scene, library)
    assert result.ok

    root = ET.parse(tmp_path / "rf" / "generated_scene.xml").getroot()
    bsdfs = {b.get("id"): b for b in root.findall("bsdf")}

    # Custom constant material: radio-material plugin with its parameters.
    asphalt = bsdfs["mat-asphalt_custom"]
    assert asphalt.get("type") == "radio-material"
    props = {f.get("name"): float(f.get("value")) for f in asphalt.findall("float")}
    assert props["relative_permittivity"] == 5.72
    assert props["conductivity"] == 0.005

    # ITU-backed material: diffuse bsdf whose id resolves to the built-in.
    concrete = bsdfs["mat-itu_concrete"]
    assert concrete.get("type") == "diffuse"


# ------------------------------------------ sionna manifest parameter apply


class _FakeRadioMaterial:
    def __init__(self):
        self.relative_permittivity = 1.0
        self.conductivity = 0.0
        self.scattering_coefficient = 0.0


class _FakeScene:
    def __init__(self, names: list[str]):
        self.radio_materials = {n: _FakeRadioMaterial() for n in names}


def test_apply_custom_materials_parses_compiler_manifest(tmp_path: Path):
    """Feed a manifest produced by the real compiler through the backend."""
    library = load_default_library()
    scene = _scene_with([_mesh_prim("/roads/r01/surface", "road", "asphalt_custom")])
    _write_glb(tmp_path, ["road"])
    rf_compiler.compile_project(tmp_path, scene, library)

    rt_scene = _FakeScene(["mat-asphalt_custom"])
    warnings: list[str] = []
    SionnaBackend._apply_custom_materials(tmp_path, rt_scene, warnings)

    mat = rt_scene.radio_materials["mat-asphalt_custom"]
    assert mat.relative_permittivity == 5.72
    assert mat.conductivity == 0.005
    assert warnings == []


def test_apply_custom_materials_warns_when_material_missing(tmp_path: Path):
    library = load_default_library()
    scene = _scene_with([_mesh_prim("/roads/r01/surface", "road", "asphalt_custom")])
    _write_glb(tmp_path, ["road"])
    rf_compiler.compile_project(tmp_path, scene, library)

    rt_scene = _FakeScene(["something_else"])
    warnings: list[str] = []
    SionnaBackend._apply_custom_materials(tmp_path, rt_scene, warnings)
    assert any("asphalt_custom" in w for w in warnings)


def test_apply_custom_materials_accepts_unprefixed_name(tmp_path: Path):
    library = load_default_library()
    scene = _scene_with([_mesh_prim("/roads/r01/surface", "road", "asphalt_custom")])
    _write_glb(tmp_path, ["road"])
    rf_compiler.compile_project(tmp_path, scene, library)

    rt_scene = _FakeScene(["asphalt_custom"])
    SionnaBackend._apply_custom_materials(tmp_path, rt_scene, [])
    assert rt_scene.radio_materials["asphalt_custom"].relative_permittivity == 5.72


# ------------------------------------------------------- availability probe


def test_sionna_available_never_raises(monkeypatch):
    from app.services import availability

    availability.sionna_available.cache_clear()
    monkeypatch.setattr(
        "app.services.availability.util.find_spec",
        lambda name: (_ for _ in ()).throw(OSError("broken DLL")),
    )
    assert availability.sionna_available() is False
    availability.sionna_available.cache_clear()


# ---------------------------------------------------- result id collisions


def test_result_id_never_reuses_live_id_after_ref_pruning(api_client):
    """simulate x2 -> prune ref 001 via PUT /scene -> simulate must NOT
    reuse mock_paths_002 (which would overwrite the surviving file)."""
    api_client.post("/api/projects", json={"name": "Collide", "project_id": "collide"})
    scene = api_client.get("/api/projects/collide/scene").json()
    scene["devices"] = [
        {"id": "tx_001", "kind": "tx", "position": [0, 0, 10]},
        {"id": "rx_001", "kind": "rx", "position": [20, 0, 1.5]},
    ]
    assert api_client.put("/api/projects/collide/scene", json=scene).status_code == 200

    first = api_client.post("/api/projects/collide/simulate/paths", json={}).json()
    second = api_client.post("/api/projects/collide/simulate/paths", json={}).json()
    assert [first["result_id"], second["result_id"]] == [
        "mock_paths_001",
        "mock_paths_002",
    ]

    # Prune the first ref through the documented scene API.
    scene = api_client.get("/api/projects/collide/scene").json()
    scene["result_sets"] = [
        r for r in scene["result_sets"] if r["result_id"] != "mock_paths_001"
    ]
    assert api_client.put("/api/projects/collide/scene", json=scene).status_code == 200

    third = api_client.post("/api/projects/collide/simulate/paths", json={}).json()
    assert third["result_id"] == "mock_paths_003"  # not a reused 002

    # The survivor is still retrievable and untouched.
    kept = api_client.get(
        "/api/projects/collide/results/paths", params={"result_id": "mock_paths_002"}
    ).json()
    assert kept["created_at"] == second["created_at"]


# ------------------------------------------- suggester/validator consistency


def test_approving_rule_suggestions_yields_no_mismatch_warnings():
    """suggest -> approve -> validate must never contradict itself."""
    from app.services.ai_provider import RuleBasedProvider

    library = load_default_library()
    scene = _scene_with(
        [
            _mesh_prim(
                "/vegetation/tree_01/trunk",
                "tree_01_trunk",
                None,
                semantic_tags=["vegetation", "tree"],
                visual=VisualBinding(material_name="bark_pbr"),
            ),
            _mesh_prim(
                "/buildings/b02/walls",
                "building_02_walls",
                None,
                semantic_tags=["building", "wall"],
                visual=VisualBinding(material_name="red_brick_pbr"),
            ),
        ]
    )
    provider = RuleBasedProvider()
    response = provider.suggest(
        scene, library, [p.id for p in scene.prims]
    )
    assert len(response.suggestions) == 2

    for suggestion in response.suggestions:
        assign_materials(
            scene,
            AssignRequest(
                prim_ids=[suggestion.prim_id],
                rf_material_id=suggestion.recommended_rf_material_id,
                assignment_status="user_confirmed",
                sources=["ai:rule_based", "user"],
                confidence=suggestion.confidence,
            ),
            library,
        )

    report = validate_scene(scene, library)
    mismatches = [i for i in report.issues if i.code == "VISUAL_RF_MISMATCH"]
    assert mismatches == []
