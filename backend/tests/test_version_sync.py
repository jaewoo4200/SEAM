"""Guard: the five places a release version lives stay in lockstep.

v0.1.5 shipped with ``backend/openapi.json`` still saying 0.1.4 because the
export script was not re-run after the version bump, and CITATION.cff plus the
README BibTeX blocks then sat at 0.1.5 for eight releases. The release
checklist is manual, so pin the invariant here instead: ``pyproject.toml``
(what PyPI ships), ``APP_VERSION`` (what /health and provenance stamps report),
the committed OpenAPI schema (what the frontend types and docs are generated
from), ``CITATION.cff`` (GitHub's "Cite this repository") and the BibTeX block
of README.md / README.ko.md must agree, or CI goes red.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO = BACKEND_DIR.parent


def _pyproject_version() -> str:
    with (BACKEND_DIR / "pyproject.toml").open("rb") as fh:
        return tomllib.load(fh)["project"]["version"]


def _openapi_version() -> str:
    with (BACKEND_DIR / "openapi.json").open(encoding="utf-8") as fh:
        return json.load(fh)["info"]["version"]


def test_app_version_matches_pyproject() -> None:
    from seam_studio.core.config import APP_VERSION

    assert APP_VERSION == _pyproject_version()


def test_committed_openapi_matches_app_version() -> None:
    """Fails when a version bump forgets ``scripts/export_openapi.py``."""
    from seam_studio.core.config import APP_VERSION

    assert _openapi_version() == APP_VERSION


def test_citation_cff_matches_pyproject() -> None:
    text = (REPO / "CITATION.cff").read_text(encoding="utf-8")
    found = re.findall(r'^version:\s*"?([^"\s]+)"?\s*$', text, flags=re.MULTILINE)
    assert found == [_pyproject_version()]


@pytest.mark.parametrize("readme", ["README.md", "README.ko.md"])
def test_readme_bibtex_matches_pyproject(readme: str) -> None:
    text = (REPO / readme).read_text(encoding="utf-8")
    found = re.findall(r"version\s*=\s*\{([^}]+)\}", text)
    assert found == [_pyproject_version()], readme
