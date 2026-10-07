# Releasing SEAM Studio

> **English** · [한국어](releasing.ko.md)

The checklist for cutting a release (`vX.Y.Z`). Commands run from the repo
root with the backend venv (`backend\.venv\Scripts\python.exe` on Windows,
`backend/.venv/bin/python` elsewhere; written `python` below).

## 1. Bump the version (five places + a date)

| Place | What to change |
|---|---|
| `backend/pyproject.toml` | `version = "X.Y.Z"` |
| `backend/seam_studio/core/config.py` | `APP_VERSION = "X.Y.Z"` |
| `backend/openapi.json` | regenerate: `python scripts/export_openapi.py` (writes `info.version` from `APP_VERSION`) |
| `CITATION.cff` | `version: X.Y.Z` and `date-released: "YYYY-MM-DD"` (the release day) |
| `README.md`, `README.ko.md` | the BibTeX `version = {X.Y.Z},` line |

`backend/tests/test_version_sync.py` checks that all five agree with
`pyproject.toml`, so a forgotten place turns the suite red. If the API changed,
also run `npm run gen:api-types` in `frontend/` after regenerating
`openapi.json`.

Add a news pill for the release to the hero of `website/en/index.html` and
`website/ko/index.html` when the release is worth announcing.

## 2. Gates (all green before tagging)

```bash
python -m pip install -e "backend[dev,parquet]"
ruff check backend
python -m pytest backend/tests -rs        # skips: the live Overpass test only
cd frontend && npm run build              # tsc -b + vite, no chunk-size warning
```

Push the commit and wait for **CI** (`.github/workflows/ci.yml`: Python 3.11
and 3.14 on Linux, ruff, the frontend build) to pass on `master`.

## 3. Tag → PyPI

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

The tag starts `.github/workflows/release.yml`: it runs the backend suite on
3.11 and 3.14 first (a red suite stops the release before anything reaches
PyPI), then builds the frontend and the wheel, smoke-imports the wheel,
checks that the wheel version equals the tag, and publishes to PyPI. Then
create the GitHub release for the tag with the release notes.

## 4. Website

The site is deployed by hand to the `gh-pages` branch, from tracked files
only (ignored drops such as `website/assets/SEAM_*` never ship, and files
that exist only on `gh-pages` are removed).

1. Regenerate the guide pages from the Markdown guides when any changed:
   `python scripts/build_website_guides.py` (add `--out <dir>` for a dry run,
   `--strict` to fail on a broken link), review and commit them.
2. Dry run, then deploy:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_website.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\deploy_website.ps1
```

```bash
bash scripts/deploy_website.sh --dry-run
bash scripts/deploy_website.sh            # --no-push, --remote NAME also exist
```

The dry run prints `git status --short` of the deploy tree without
committing. The real run commits `website: deploy <short sha>` on `gh-pages`
(skipped when nothing changed) and pushes it unless `-NoPush` / `--no-push`.
`website/` must have no uncommitted changes.

## 5. Account actions (maintainer only)

These need the repository owner's GitHub / PyPI / Zenodo accounts; no script
or agent does them:

- **Zenodo DOI:** enable the GitHub → Zenodo integration for the repository
  *before* tagging (DOIs are minted only for releases published after it is
  on), then add the DOI badge and a `doi` field to `CITATION.cff` and both
  BibTeX blocks.
- **PyPI Trusted Publishing:** configure a trusted publisher for
  `seam-studio`, switch the publish step to OIDC (`id-token: write`) and
  retire the `PYPI_API_TOKEN` secret (see the TODO in `release.yml`).
- **Branch protection:** optionally require the CI checks on `master`.
