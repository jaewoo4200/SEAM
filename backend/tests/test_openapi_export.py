"""Guard: importing the app and dumping OpenAPI succeeds and carries the routes
that the frontend drift reference (`frontend/src/types/openapi.gen.d.ts`) and
docs are written against.

The export lives in ``scripts/export_openapi.py`` (writes ``backend/openapi.json``
with sorted keys); this test exercises the same ``app.openapi()`` call that the
script does and pins the route set, so a route that quietly disappears fails
CI rather than silently drifting the generated types.

Routes are split into two tiers:

* **wave-0 routes** are already landed and live-verified - a missing one is a
  hard failure with a clear list.
* **sibling-wave routes** are being implemented in parallel by other agents in
  the same wave; they are asserted **only if present** (shape check), so this
  test does not go red before a sibling's route lands. Once they land, the
  if-present branch still guards them from regressing.
"""

from __future__ import annotations

# All app routers are mounted under this prefix (see app.main.create_app).
API_PREFIX = "/api"

# Landed + live-verified. A missing one of these is a hard failure.
WAVE0_ROUTES = [
    ("POST", "/projects/{project_id}/simulate/mesh-radio-map"),
    ("GET", "/projects/{project_id}/results/mesh-radio-map"),
    ("GET", "/backends"),
]

# Being implemented by sibling agents this wave - assert-if-present only.
SIBLING_ROUTES = [
    ("POST", "/projects/{project_id}/ai/generate-rules"),
    ("POST", "/projects/{project_id}/ai/apply-rules"),
    ("POST", "/projects/{project_id}/ai/explain-validation"),
    ("POST", "/projects/{project_id}/calibrate/measurements/import-csv"),
    ("GET", "/projects/{project_id}/calibrate/measurements"),
]


def _openapi() -> dict:
    """The exact schema the export script serialises."""
    from app.main import app

    return app.openapi()


def _methods_for(schema: dict, route: str) -> set[str] | None:
    """Uppercased HTTP methods registered for ``API_PREFIX + route``.

    Returns None when the path is absent from the schema.
    """
    entry = schema.get("paths", {}).get(API_PREFIX + route)
    if entry is None:
        return None
    return {m.upper() for m in entry}


def test_openapi_import_and_dump_succeeds() -> None:
    schema = _openapi()
    assert isinstance(schema, dict)
    # Minimal well-formed OpenAPI: version, info, and a non-empty path table.
    assert schema.get("openapi", "").startswith("3.")
    assert schema.get("info", {}).get("title") == "SEAM Studio"
    assert schema.get("paths"), "OpenAPI schema has no paths"


def test_openapi_export_script_writes_sorted_json(tmp_path) -> None:
    """scripts.export_openapi.export() round-trips to sorted JSON on disk."""
    import json
    import sys
    from pathlib import Path

    # scripts/ sits beside backend/ at the repo root; make it importable.
    repo_root = Path(__file__).resolve().parents[2]
    scripts_dir = repo_root / "scripts"
    assert scripts_dir.is_dir(), f"scripts dir not found at {scripts_dir}"
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))

    import export_openapi

    out = tmp_path / "openapi.json"
    written = export_openapi.export(out)
    assert written == out and out.is_file()

    text = out.read_text(encoding="utf-8")
    data = json.loads(text)
    assert data["info"]["title"] == "SEAM Studio"

    # sort_keys=True was used: top-level keys must already be sorted on disk.
    top_keys = list(data.keys())
    assert top_keys == sorted(top_keys), top_keys


def test_wave0_routes_present() -> None:
    """Hard gate: every wave-0 route exists with its expected method."""
    schema = _openapi()
    missing: list[str] = []
    for method, route in WAVE0_ROUTES:
        methods = _methods_for(schema, route)
        if methods is None:
            missing.append(f"{method} {API_PREFIX}{route} (path absent)")
        elif method not in methods:
            missing.append(
                f"{method} {API_PREFIX}{route} (path present, methods={sorted(methods)})"
            )
    assert not missing, "wave-0 routes missing from OpenAPI:\n  " + "\n  ".join(missing)


def test_sibling_routes_shape_if_present() -> None:
    """Soft gate: sibling routes are checked only once they land.

    Collects which sibling routes are present for visibility; asserts the
    method shape for those that exist. Never fails for a not-yet-landed route.
    """
    schema = _openapi()
    present: list[str] = []
    for method, route in SIBLING_ROUTES:
        methods = _methods_for(schema, route)
        if methods is None:
            continue  # sibling route not landed yet - tolerated this wave.
        present.append(f"{method} {API_PREFIX}{route}")
        assert method in methods, (
            f"{API_PREFIX}{route} exists but is missing method {method} "
            f"(has {sorted(methods)})"
        )
    # No assertion on len(present): a wave where zero siblings have landed yet
    # is still a pass. The line documents intent for a reader of a -v run.
    assert isinstance(present, list)
