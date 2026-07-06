"""Standalone paths-solve worker for alternate sionna-rt engine venvs.

Runs INSIDE the engine's own virtual environment (any standalone sionna-rt
1.x / 2.x), so it must not import anything from the SionnaTwin backend app.
Protocol:

    python sionna_rt_worker.py <job.json> <out.json>

Job (written by app.services.engines.run_paths_job):
    {
      "kind": "paths",
      "xml_path": "...generated_scene.xml",
      "manifest_path": "...compile_manifest.json" | null,
      "frequency_hz": float, "max_depth": int, "seed": int,
      "num_samples": int, "synthetic_array": bool,
      "flags": {"los": bool, "reflection": bool, "scattering": bool,
                 "refraction": bool, "diffraction": bool,
                 "edge_diffraction": bool, "diffraction_lit_region": bool},
      "txs": [{"id", "position", "orientation_deg", "power_dbm",
                "antenna": {"pattern", "polarization", "num_rows", "num_cols"}}],
      "rxs": [...same shape...],
      "material_to_prims": {"<rf_material_id>": ["<prim_id>", ...]}
    }

Output: {"ok": bool, "engine_version": str, "paths": [<RayPath-shaped dict>],
         "warnings": [str], "error": str|null}

Version robustness: PathSolver kwargs differ across sionna-rt releases, so the
call is built by filtering the job's flags against the solver's actual
signature; unsupported mechanisms are dropped with a warning instead of
crashing.
"""

from __future__ import annotations

import inspect
import json
import math
import sys
import traceback

_NO_OBJECT = 0xFFFFFFFF
_INTERACTION_TYPES = {1: "reflection", 2: "scattering", 3: "transmission", 4: "diffraction"}
_VALID_PATTERNS = ("iso", "dipole", "hw_dipole", "tr38901")
_VALID_POLARIZATIONS = ("V", "H", "VH", "cross")


def _planar_array(rt, antenna: dict, warnings: list) -> object:
    pattern = antenna.get("pattern") or "iso"
    pol = antenna.get("polarization") or "V"
    if pattern not in _VALID_PATTERNS:
        warnings.append(f"unknown antenna pattern '{pattern}' in engine venv; using iso")
        pattern = "iso"
    if pol not in _VALID_POLARIZATIONS:
        warnings.append(f"unknown polarization '{pol}'; using V")
        pol = "V"
    return rt.PlanarArray(
        num_rows=int(antenna.get("num_rows") or 1),
        num_cols=int(antenna.get("num_cols") or 1),
        vertical_spacing=float(antenna.get("vertical_spacing") or 0.5),
        horizontal_spacing=float(antenna.get("horizontal_spacing") or 0.5),
        pattern=pattern,
        polarization=pol,
    )


def _apply_custom_materials(scene, manifest_path, warnings: list) -> None:
    """Override constant-model material parameters like the builtin backend
    does (rf/compile_manifest.json 'custom_material' entries)."""
    if not manifest_path:
        return
    try:
        with open(manifest_path, encoding="utf-8") as f:
            manifest = json.load(f)
    except OSError as exc:
        warnings.append(f"engine worker: could not read compile manifest: {exc}")
        return
    mats = getattr(scene, "radio_materials", None)
    if mats is None:
        warnings.append("engine worker: scene has no radio_materials dict; custom overrides skipped")
        return
    for group in manifest.get("groups", []):
        custom = group.get("custom_material")
        name = group.get("rf_material_id")
        if not custom or not name:
            continue
        target = None
        for mat_name, mat in dict(mats).items():
            if name in str(mat_name):
                target = mat
                break
        if target is None:
            warnings.append(f"engine worker: material '{name}' not found in loaded scene")
            continue
        for src_key, attr in (
            ("relative_permittivity", "relative_permittivity"),
            ("conductivity_s_m", "conductivity"),
            ("scattering_coefficient", "scattering_coefficient"),
            ("xpd_coefficient", "xpd_coefficient"),
        ):
            if src_key in custom and custom[src_key] is not None:
                try:
                    setattr(target, attr, float(custom[src_key]))
                except Exception as exc:  # noqa: BLE001 - per-property tolerance
                    warnings.append(f"engine worker: {name}.{attr} not settable: {exc}")


def _solver_kwargs(solver, job, warnings: list) -> dict:
    """Map job flags onto whatever kwargs this sionna-rt release supports."""
    accepted = set(inspect.signature(solver.__call__).parameters)
    flags = job.get("flags", {})
    wanted = {
        "max_depth": job.get("max_depth", 3),
        "los": flags.get("los", True),
        "specular_reflection": flags.get("reflection", True),
        "diffuse_reflection": flags.get("scattering", False),
        "refraction": flags.get("refraction", False),
        "diffraction": flags.get("diffraction", False),
        "edge_diffraction": flags.get("edge_diffraction", False),
        "diffraction_lit_region": flags.get("diffraction_lit_region", False),
        "synthetic_array": job.get("synthetic_array", True),
        "seed": job.get("seed", 42),
        "samples_per_src": job.get("num_samples") or 1_000_000,
    }
    kwargs = {}
    for key, value in wanted.items():
        if key in accepted:
            kwargs[key] = value
        # Only warn when the user actually enabled a mechanism this engine
        # version cannot express; silently dropping defaults is fine.
        elif value not in (False, None):
            warnings.append(
                f"engine worker: this sionna-rt version has no '{key}' "
                f"(requested {value}); dropped"
            )
    return kwargs


def run(job: dict) -> dict:
    import numpy as np
    import sionna.rt as rt

    warnings: list = []
    version = getattr(rt, "__version__", "unknown")

    scene = rt.load_scene(job["xml_path"])
    scene.frequency = job["frequency_hz"]
    txs, rxs = job["txs"], job["rxs"]
    scene.tx_array = _planar_array(rt, txs[0].get("antenna") or {}, warnings)
    scene.rx_array = _planar_array(rt, rxs[0].get("antenna") or {}, warnings)
    for dev in txs:
        scene.add(rt.Transmitter(
            name=dev["id"],
            position=list(dev["position"]),
            orientation=[math.radians(a) for a in dev.get("orientation_deg") or [0, 0, 0]],
            power_dbm=dev.get("power_dbm", 30.0),
        ))
    for dev in rxs:
        scene.add(rt.Receiver(
            name=dev["id"],
            position=list(dev["position"]),
            orientation=[math.radians(a) for a in dev.get("orientation_deg") or [0, 0, 0]],
        ))
    _apply_custom_materials(scene, job.get("manifest_path"), warnings)

    # shape-<rf_material_id> naming convention from the SionnaTwin compiler.
    objid_to_material: dict = {}
    for name, obj in scene.objects.items():
        mat_id = name[len("shape-"):] if str(name).startswith("shape-") else str(name)
        objid_to_material[int(obj.object_id)] = mat_id
    material_to_prims = job.get("material_to_prims", {})

    solver = rt.PathSolver()
    solved = solver(scene, **_solver_kwargs(solver, job, warnings))

    def to_np(x):
        return np.asarray(x.numpy() if hasattr(x, "numpy") else x)

    tau = to_np(solved.tau)
    a_raw = solved.a
    if isinstance(a_raw, (tuple, list)) and len(a_raw) == 2:
        a = to_np(a_raw[0]) + 1j * to_np(a_raw[1])
    else:
        a = to_np(a_raw)
    if a.ndim == 5:
        # Reference-element (port 0/0) reduction - same convention as the
        # builtin backend. Summing raw power over all rx_ant x tx_ant port
        # pairs inflated multi-array links by 10*log10(N_rx*N_tx) dB and
        # erased the per-path phase (audit B2).
        a = a[:, 0, :, 0, :]
    else:
        while a.ndim > 3:
            squeeze = tuple(i for i, s in enumerate(a.shape) if s == 1)
            if not squeeze:
                warnings.append(f"engine worker: unexpected coefficient shape {a.shape}")
                return {"ok": True, "engine_version": version, "paths": [],
                        "warnings": warnings, "error": None}
            a = a.squeeze(axis=squeeze[0])

    vertices = to_np(solved.vertices) if hasattr(solved, "vertices") else None
    valid = to_np(solved.valid) if hasattr(solved, "valid") else None
    objects = to_np(solved.objects) if hasattr(solved, "objects") else None
    itypes = to_np(solved.interactions) if hasattr(solved, "interactions") else None

    num_rx = min(tau.shape[0], len(rxs))
    num_tx = min(tau.shape[1], len(txs))
    paths, counter = [], 0
    for r in range(num_rx):
        for t in range(num_tx):
            for p in range(tau.shape[-1]):
                if valid is not None and valid.ndim == 3 and not bool(valid[r, t, p]):
                    continue
                tau_s = float(tau[r, t, p])
                if tau_s < 0:
                    continue
                amp = complex(a[r, t, p]) if a.ndim == 3 else 0j
                if abs(amp) <= 0:
                    continue
                power_dbm = 20.0 * math.log10(max(abs(amp), 1e-30)) + txs[t].get("power_dbm", 30.0)

                bounce, interactions = [], []
                if vertices is not None and vertices.ndim == 5:
                    for d in range(vertices.shape[0]):
                        obj_id = int(objects[d, r, t, p]) if objects is not None else _NO_OBJECT
                        if obj_id == _NO_OBJECT:
                            continue
                        v = vertices[d, r, t, p]
                        if not np.all(np.isfinite(v)):
                            continue
                        point = [float(x) for x in v]
                        bounce.append(point)
                        code = int(itypes[d, r, t, p]) if itypes is not None else 1
                        mat_id = objid_to_material.get(obj_id)
                        prims = material_to_prims.get(mat_id, []) if mat_id else []
                        interactions.append({
                            "type": _INTERACTION_TYPES.get(code, "reflection"),
                            "prim_id": prims[0] if len(prims) == 1 else None,
                            "rf_material_id": mat_id,
                            "point": point,
                        })

                if not interactions:
                    path_type = "los"
                elif any(i["type"] == "diffraction" for i in interactions):
                    path_type = "diffraction"
                elif any(i["type"] == "scattering" for i in interactions):
                    path_type = "scattering"
                elif any(i["type"] == "transmission" for i in interactions):
                    path_type = "transmission"
                else:
                    path_type = "reflection"

                counter += 1
                paths.append({
                    "path_id": f"path_{counter:04d}",
                    "tx_id": txs[t]["id"],
                    "rx_id": rxs[r]["id"],
                    "path_type": path_type,
                    "vertices": [list(txs[t]["position"])] + bounce + [list(rxs[r]["position"])],
                    "power_dbm": power_dbm,
                    "delay_ns": tau_s * 1e9,
                    "phase_rad": math.atan2(amp.imag, amp.real),
                    "interactions": interactions,
                })
    return {"ok": True, "engine_version": version, "paths": paths,
            "warnings": warnings, "error": None}


def main() -> int:
    job_path, out_path = sys.argv[1], sys.argv[2]
    with open(job_path, encoding="utf-8") as f:
        job = json.load(f)
    try:
        result = run(job)
    except Exception:  # noqa: BLE001 - worker reports, caller decides
        result = {"ok": False, "engine_version": None, "paths": [],
                  "warnings": [], "error": traceback.format_exc(limit=8)}
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(result, f)
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
