# SEAM Studio 릴리스 절차

> [English](releasing.md) · **한국어**

릴리스(`vX.Y.Z`)를 낼 때의 체크리스트입니다. 명령은 리포 루트에서 백엔드 venv로
실행합니다(Windows는 `backend\.venv\Scripts\python.exe`, 그 밖에는
`backend/.venv/bin/python`. 아래에서는 `python`으로 씁니다).

## 1. 버전 올리기 (다섯 곳 + 날짜)

| 위치 | 바꿀 것 |
|---|---|
| `backend/pyproject.toml` | `version = "X.Y.Z"` |
| `backend/seam_studio/core/config.py` | `APP_VERSION = "X.Y.Z"` |
| `backend/openapi.json` | 다시 생성: `python scripts/export_openapi.py` (`APP_VERSION`으로 `info.version`을 씀) |
| `CITATION.cff` | `version: X.Y.Z`와 `date-released: "YYYY-MM-DD"`(릴리스 날짜) |
| `README.md`, `README.ko.md` | BibTeX의 `version = {X.Y.Z},` 줄 |

`backend/tests/test_version_sync.py`가 다섯 곳이 모두 `pyproject.toml`과 같은지
확인하므로, 한 곳이라도 빠뜨리면 테스트가 실패합니다. API가 바뀌었다면
`openapi.json`을 다시 만든 뒤 `frontend/`에서 `npm run gen:api-types`도 실행하세요.

알릴 만한 릴리스라면 `website/en/index.html`과 `website/ko/index.html` 히어로에
뉴스 필(news pill)을 추가합니다.

## 2. 게이트 (태그 전에 모두 통과)

```bash
python -m pip install -e "backend[dev,parquet]"
ruff check backend
python -m pytest backend/tests -rs        # 건너뛰는 테스트: 라이브 Overpass 테스트 하나뿐
cd frontend && npm run build              # tsc -b + vite, 청크 크기 경고 없음
```

커밋을 푸시하고 `master`에서 **CI**(`.github/workflows/ci.yml`: 리눅스 Python 3.11과
3.14, ruff, 프런트엔드 빌드)가 통과할 때까지 기다립니다.

## 3. 태그 → PyPI

```bash
git tag vX.Y.Z
git push origin vX.Y.Z
```

태그가 `.github/workflows/release.yml`을 시작합니다. 먼저 3.11과 3.14에서 백엔드
테스트를 돌리고(실패하면 PyPI에 아무것도 올라가기 전에 릴리스가 멈춤), 그다음
프런트엔드와 휠을 빌드하고, 휠을 임포트해 점검하고, 휠 버전이 태그와 같은지 확인한 뒤
PyPI에 게시합니다. 그 뒤 태그에 대한 GitHub 릴리스를 릴리스 노트와 함께 만듭니다.

## 4. 웹사이트

사이트는 `gh-pages` 브랜치에 손으로 배포하며, git이 추적하는 파일만 올라갑니다
(`website/assets/SEAM_*` 같은 무시된 파일은 올라가지 않고, `gh-pages`에만 있는
파일은 지워집니다).

1. 마크다운 가이드가 바뀌었다면 가이드 페이지를 다시 만듭니다:
   `python scripts/build_website_guides.py`(`--out <dir>`이면 시험 실행, `--strict`면
   깨진 링크에서 실패). 결과를 검토하고 커밋합니다.
2. 시험 실행 후 배포:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\deploy_website.ps1 -DryRun
powershell -ExecutionPolicy Bypass -File scripts\deploy_website.ps1
```

```bash
bash scripts/deploy_website.sh --dry-run
bash scripts/deploy_website.sh            # --no-push, --remote NAME 옵션도 있음
```

시험 실행은 커밋하지 않고 배포 트리의 `git status --short`만 보여 줍니다. 실제 실행은
`gh-pages`에 `website: deploy <short sha>`를 커밋하고(바뀐 게 없으면 건너뜀),
`-NoPush` / `--no-push`가 아니면 푸시합니다. `website/`에 커밋하지 않은 변경이 없어야
합니다.

## 5. 계정 작업 (관리자만)

리포지토리 소유자의 GitHub / PyPI / Zenodo 계정이 필요한 일이라 스크립트나 에이전트가
하지 않습니다.

- **Zenodo DOI:** 태그를 달기 *전에* 리포지토리의 GitHub → Zenodo 연동을 켭니다(DOI는
  연동을 켠 뒤 게시한 릴리스에만 발급됨). 그다음 DOI 배지와 `CITATION.cff`·두 BibTeX
  블록의 `doi` 필드를 추가합니다.
- **PyPI Trusted Publishing:** `seam-studio`에 신뢰할 수 있는 게시자를 설정하고, 게시
  단계를 OIDC(`id-token: write`)로 바꾼 뒤 `PYPI_API_TOKEN` 시크릿을 없앱니다
  (`release.yml`의 TODO 참고).
- **브랜치 보호:** 필요하면 `master`에 CI 검사를 필수로 겁니다.
