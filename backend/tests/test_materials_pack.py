"""RF material pack export/import API tests (library transfer between projects)."""


def _create_project(api_client, name: str) -> str:
    created = api_client.post("/api/projects", json={"name": name})
    assert created.status_code == 201, created.text
    return created.json()["project_id"]


def _make_material(material_id: str = "lab_glass", **overrides) -> dict:
    material = {
        "id": material_id, "display_name": "Lab Glass", "category": "custom",
        "model": "constant", "itu_name": None, "relative_permittivity": 5.5,
        "conductivity_s_per_m": 0.02, "thickness_m": 0.008,
        "scattering_coefficient": 0.1, "xpd_coefficient": 0.0,
        "transmissive": True, "preview_color": "#88ccee", "notes": "",
        "builtin": False,
    }
    material.update(overrides)
    return material


def _put_material(api_client, pid: str, material: dict) -> None:
    put = api_client.put(
        f"/api/projects/{pid}/rf/materials/{material['id']}", json=material
    )
    assert put.status_code == 200, put.text


def _library_ids(api_client, pid: str) -> set[str]:
    library = api_client.get(f"/api/projects/{pid}/rf/materials").json()
    return {m["id"] for m in library["materials"]}


class TestExport:
    def test_default_export_is_non_builtin_only(self, api_client):
        pid = _create_project(api_client, name="Pack Source")
        # Fresh project: the library is all builtin, so the pack is empty.
        empty = api_client.get(f"/api/projects/{pid}/rf/materials/export")
        assert empty.status_code == 200, empty.text
        assert empty.json() == {"materials": []}

        _put_material(api_client, pid, _make_material("lab_glass"))
        _put_material(
            api_client, pid, _make_material("lab_steel", display_name="Lab Steel")
        )
        pack = api_client.get(f"/api/projects/{pid}/rf/materials/export").json()
        assert {m["id"] for m in pack["materials"]} == {"lab_glass", "lab_steel"}
        assert all(m["builtin"] is False for m in pack["materials"])

    def test_explicit_ids_selects_and_drops_builtin(self, api_client):
        pid = _create_project(api_client, name="Pack Ids")
        _put_material(api_client, pid, _make_material("lab_glass"))
        _put_material(
            api_client, pid, _make_material("lab_steel", display_name="Lab Steel")
        )
        # Only the requested id is exported.
        one = api_client.get(
            f"/api/projects/{pid}/rf/materials/export", params={"ids": "lab_steel"}
        )
        assert one.status_code == 200, one.text
        assert [m["id"] for m in one.json()["materials"]] == ["lab_steel"]
        # A builtin id is a valid request but never exported.
        mixed = api_client.get(
            f"/api/projects/{pid}/rf/materials/export",
            params={"ids": "itu_glass,lab_glass"},
        )
        assert mixed.status_code == 200, mixed.text
        assert [m["id"] for m in mixed.json()["materials"]] == ["lab_glass"]

    def test_explicit_missing_id_404(self, api_client):
        pid = _create_project(api_client, name="Pack Missing")
        response = api_client.get(
            f"/api/projects/{pid}/rf/materials/export", params={"ids": "ghost"}
        )
        assert response.status_code == 404
        assert "ghost" in response.json()["detail"]

    def test_unknown_project_404(self, api_client):
        assert (
            api_client.get("/api/projects/nope/rf/materials/export").status_code
            == 404
        )


class TestImport:
    def test_merge_persists_across_reload(self, api_client):
        src = _create_project(api_client, name="Pack Src")
        dst = _create_project(api_client, name="Pack Dst")
        _put_material(api_client, src, _make_material("lab_glass"))

        pack = api_client.get(f"/api/projects/{src}/rf/materials/export").json()
        response = api_client.post(
            f"/api/projects/{dst}/rf/materials/import", json=pack
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "imported": ["lab_glass"], "renamed": {}, "skipped": [],
        }
        # Fresh GET reloads the library from disk: the import persisted.
        library = api_client.get(f"/api/projects/{dst}/rf/materials").json()
        stored = next(m for m in library["materials"] if m["id"] == "lab_glass")
        assert stored["builtin"] is False
        assert stored["relative_permittivity"] == 5.5

    def test_collision_renames_with_numeric_suffix(self, api_client):
        pid = _create_project(api_client, name="Pack Rename")
        _put_material(api_client, pid, _make_material("lab_glass"))
        incoming = _make_material("lab_glass", relative_permittivity=7.0)
        response = api_client.post(
            f"/api/projects/{pid}/rf/materials/import",
            json={"materials": [incoming]},
        )
        assert response.status_code == 200, response.text
        assert response.json() == {
            "imported": ["lab_glass_2"],
            "renamed": {"lab_glass": "lab_glass_2"},
            "skipped": [],
        }
        library = api_client.get(f"/api/projects/{pid}/rf/materials").json()
        by_id = {m["id"]: m for m in library["materials"]}
        # Original untouched, renamed copy carries the incoming values.
        assert by_id["lab_glass"]["relative_permittivity"] == 5.5
        assert by_id["lab_glass_2"]["relative_permittivity"] == 7.0

    def test_rename_skips_taken_suffixes(self, api_client):
        pid = _create_project(api_client, name="Pack Suffix")
        _put_material(api_client, pid, _make_material("lab_glass"))
        _put_material(
            api_client, pid, _make_material("lab_glass_2", relative_permittivity=6.0)
        )
        incoming = _make_material("lab_glass", relative_permittivity=7.0)
        response = api_client.post(
            f"/api/projects/{pid}/rf/materials/import",
            json={"materials": [incoming]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["renamed"] == {"lab_glass": "lab_glass_3"}

    def test_identical_material_is_skipped(self, api_client):
        src = _create_project(api_client, name="Pack Skip Src")
        dst = _create_project(api_client, name="Pack Skip Dst")
        _put_material(api_client, src, _make_material("lab_glass"))
        pack = api_client.get(f"/api/projects/{src}/rf/materials/export").json()

        first = api_client.post(f"/api/projects/{dst}/rf/materials/import", json=pack)
        assert first.json()["imported"] == ["lab_glass"]
        again = api_client.post(f"/api/projects/{dst}/rf/materials/import", json=pack)
        assert again.status_code == 200, again.text
        assert again.json() == {
            "imported": [], "renamed": {}, "skipped": ["lab_glass"],
        }
        # No duplicate entry was appended.
        library = api_client.get(f"/api/projects/{dst}/rf/materials").json()
        assert [m["id"] for m in library["materials"]].count("lab_glass") == 1

    def test_unmodified_builtin_is_skipped_not_duplicated(self, api_client):
        pid = _create_project(api_client, name="Pack Builtin")
        library = api_client.get(f"/api/projects/{pid}/rf/materials").json()
        itu_glass = next(m for m in library["materials"] if m["id"] == "itu_glass")
        assert itu_glass["builtin"] is True
        response = api_client.post(
            f"/api/projects/{pid}/rf/materials/import",
            json={"materials": [itu_glass]},
        )
        assert response.status_code == 200, response.text
        # builtin is forced off on import, so the pack copy matches the
        # shipped material value-for-value: skip, never an itu_glass_2.
        assert response.json() == {
            "imported": [], "renamed": {}, "skipped": ["itu_glass"],
        }
        assert "itu_glass_2" not in _library_ids(api_client, pid)

    def test_import_forces_builtin_false(self, api_client):
        pid = _create_project(api_client, name="Pack Force")
        incoming = _make_material("smuggled", builtin=True)
        response = api_client.post(
            f"/api/projects/{pid}/rf/materials/import",
            json={"materials": [incoming]},
        )
        assert response.status_code == 200, response.text
        assert response.json()["imported"] == ["smuggled"]
        library = api_client.get(f"/api/projects/{pid}/rf/materials").json()
        stored = next(m for m in library["materials"] if m["id"] == "smuggled")
        assert stored["builtin"] is False

    def test_unknown_project_404(self, api_client):
        response = api_client.post(
            "/api/projects/nope/rf/materials/import",
            json={"materials": [_make_material()]},
        )
        assert response.status_code == 404
