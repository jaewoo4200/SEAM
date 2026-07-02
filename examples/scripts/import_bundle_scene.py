"""Import a bundle Sionna/Mitsuba XML scene into a SionnaTwin project.

Default: the indoor lab_room scene from the reference bundle. Produces a
loadable project (canonical scene + combined visual GLB + material library +
28 GHz sim config + indoor TX/RX defaults from the 1124 handoff).

Run from the repo root:

    backend\\.venv\\Scripts\\python.exe examples/scripts/import_bundle_scene.py
    backend\\.venv\\Scripts\\python.exe examples/scripts/import_bundle_scene.py \
        --xml sionna-rt-gui-jaewoo-examples/outdoor_material_assigned_cv_28ghz_safe.xml \
        --scene-id ftc_outdoor --name "FTC Outdoor 28 GHz"
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parents[1]
sys.path.insert(0, str(REPO_ROOT / "backend"))

from app.schemas.devices import Antenna, Device  # noqa: E402
from app.schemas.simulation import SimulationConfig  # noqa: E402
from app.services.mitsuba_import import import_mitsuba_scene  # noqa: E402
from app.services.project_store import ProjectStore, load_default_library  # noqa: E402

BUNDLE = REPO_ROOT / "sionna-rt-gui-jaewoo-examples"

# Indoor lab-room TX/RX defaults (1124_HANDOFF.md), meters, Z-up.
LAB_ROOM_DEVICES = [
    Device(id="tx_001", name="Lab TX", kind="tx", position=[3.43, 2.66, 2.40],
           power_dbm=30.0, color="#ff0000",
           antenna=Antenna(pattern="tr38901", polarization="V", num_rows=4, num_cols=4)),
    Device(id="rx_001", name="Lab RX", kind="rx", position=[5.60, 4.20, 1.20],
           color="#2e9bff",
           antenna=Antenna(pattern="iso", polarization="cross", num_rows=4, num_cols=4)),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--xml", default=str(BUNDLE / "indoor" / "lab_room.xml"))
    ap.add_argument("--scene-id", default="lab_room")
    ap.add_argument("--name", default="Lab Room (imported, 28 GHz)")
    ap.add_argument("--out", default=str(REPO_ROOT / "examples" / "demo_project"))
    ap.add_argument(
        "--environment", choices=["auto", "indoor", "outdoor"], default="indoor"
    )
    args = ap.parse_args()

    xml_path = Path(args.xml).resolve()
    if not xml_path.is_file():
        raise SystemExit(f"scene XML not found: {xml_path}")

    library = load_default_library()
    scene, tm_scene, warnings = import_mitsuba_scene(
        xml_path, args.scene_id, library, scene_name=args.name
    )
    for w in warnings:
        print("warning:", w)

    if not scene.devices and args.scene_id == "lab_room":
        scene.devices = LAB_ROOM_DEVICES
    elif not scene.devices and args.scene_id == "ftc_outdoor":
        # FTC repro guide placement: TX on the FTC roof (+10 m), RX at
        # ground level (+1.5 m over terrain).
        scene.devices = [
            Device(id="tx_001", name="FTC Roof TX", kind="tx",
                   position=[65.415, -50.712, 44.7286], power_dbm=30.0,
                   antenna=Antenna(pattern="tr38901", polarization="V")),
            Device(id="rx_001", name="Ground RX", kind="rx",
                   position=[87.690, -89.711, 9.1668],
                   antenna=Antenna(pattern="dipole", polarization="cross")),
        ]
    scene.environment = args.environment
    scene.simulation_configs = [
        SimulationConfig(id="default", name="Default 28 GHz", backend="auto",
                         frequency_hz=28e9, max_depth=3)
    ]

    project_dir = Path(args.out) / f"{args.scene_id}.sionnatwin"
    if project_dir.exists():
        shutil.rmtree(project_dir)
    for sub in ("visual", "rf/meshes", "mapping", "ai", "results"):
        (project_dir / sub).mkdir(parents=True, exist_ok=True)

    (project_dir / "visual" / "scene.glb").write_bytes(tm_scene.export(file_type="glb"))
    (project_dir / "scene.sionnatwin.json").write_text(
        scene.model_dump_json(indent=2), encoding="utf-8"
    )
    ProjectStore.save_materials_to_dir(project_dir, library)
    (project_dir / "provenance.json").write_text(
        json.dumps(
            {
                "created_at": datetime.now(timezone.utc).isoformat(),
                "created_by": "import_bundle_scene.py",
                "source_xml": str(xml_path),
                "events": [],
            },
            indent=2,
        ),
        encoding="utf-8",
    )

    # Independent verification via the store.
    store = ProjectStore(roots=[Path(args.out)])
    loaded = store.load_scene(args.scene_id)
    print(f"project: {project_dir}")
    print(f"prims: {len(loaded.prims)} | devices: {len(loaded.devices)}")
    print("rf materials:", sorted({p.rf.material_id for p in loaded.prims if p.rf.material_id}))
    reloaded_meshes = set(__import__("trimesh").load(project_dir / "visual" / "scene.glb").geometry.keys())
    print("GLB meshes:", sorted(reloaded_meshes))


if __name__ == "__main__":
    main()
