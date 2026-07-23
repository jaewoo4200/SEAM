# 설치 가이드 (INSTALL)

> [English](INSTALL.md) · **한국어**

SEAM Studio는 **로컬 우선(local-first)** 워크벤치입니다. GPU도, Sionna
설치도, LLM도 **필수가 아닙니다** — 세 가지 모두 선택적 업그레이드일 뿐이고,
기본 **Mock 백엔드는 CPU만으로 항상 동작**합니다. 먼저 기본 설치로 앱을 띄우고,
필요할 때 실제 Sionna RT 엔진과 로컬 LLM을 붙이면 됩니다.

- 그냥 실행만 하고 싶다면 → [경로 A — pip 설치](#경로-a--pip-install-seam-studio-소스-체크아웃-불필요)
- 개발하거나 리포 예제를 쓰려면 → [경로 B — 소스 빠른 설치](#경로-b--소스-빠른-설치-한-줄)
- 첫 15분 사용법은 → [TUTORIAL.md](TUTORIAL.ko.md)
- 프로젝트 소개/구조는 → [README.md](README.ko.md)

---

## 사전 요구사항 (Prerequisites)

**필수 (앱 실행에 반드시 필요):**

| 항목 | 요구 버전 | 비고 |
|---|---|---|
| Python | **3.11 – 3.14** | 백엔드(FastAPI). `python --version`으로 확인. 3.11은 CI(리눅스)에서 상시 검증, 3.12는 일상 개발 인터프리터, 3.13은 클린 venv `pip install seam-studio` 동작 확인(2026-07-23, macOS), 3.14는 클린 venv 설치 + `sionna.rt` 로드 확인(2026-07-15, Windows) |
| Node.js | **20 이상** (18+도 대체로 동작) | 프론트엔드(Vite). `node --version`으로 확인. 대부분의 PC에 기본 설치되어 있지 않음 — Windows: `winget install OpenJS.NodeJS.LTS`, macOS: `brew install node@20`, Ubuntu: NodeSource 20.x. 설치 후 **새 터미널**을 열어야 PATH에 잡힘. 이 소스 체크아웃 경로에서만 필요하고, pip 패키지는 빌드된 프론트엔드를 포함해 Node가 필요 없음 |
| OS | Windows 10/11, Linux, macOS | 스크립트는 Windows(PowerShell)/Unix(bash) 모두 제공 |

> `python`·`npm`은 **PATH에 미리 있어야** 합니다(설치 스크립트가 없으면 즉시 중단).
> 스크립트는 포터블 런타임을 설치하지 않습니다. `git`은 리포 클론에만 필요하고
> 런타임 의존성은 아닙니다.

**선택 (업그레이드 — 없어도 기본 Mock 백엔드로 전체 워크플로 동작):**

| 항목 | 무엇 | 언제 필요 |
|---|---|---|
| **`sionna-rt` 패키지** | 실제 레이 트레이싱 엔진 (Mitsuba 3 / Dr.Jit 포함, 수백 MB) | **자동 설치됨** — 소스 설치와 pip 패키지 모두의 기본 의존성 (과거의 `backend[sionna]` extra는 호환용 no-op으로 유지). 검증 버전 `sionna-rt 2.0.x`. → [아래 섹션](#실제-sionna-rt-엔진-자동-설치됨) |
| **NVIDIA GPU + 드라이버** | `sionna-rt`의 CUDA(Dr.Jit) 가속 | *패키지 설치 위에 얹는* 추가 레이어. 없으면 Sionna는 CPU/LLVM으로 동작(정상, 다만 느림). macOS는 Metal/MPS 백엔드가 없어 **항상 CPU/LLVM** |
| **로컬 LLM 서버** | LM Studio(`:1234`) 또는 Ollama(`:11434`) + (VLM) 모델 | AI 재질 어시스트 / SEAM-Agent용. 없으면 규칙 기반으로 폴백. → [로컬 LLM 설정](#선택-로컬-llmvlm--ai-재질-제안) |

> **핵심:** 세 가지 모두 **선택**입니다. Mock 백엔드는 아무 설치 없이 CPU만으로
> 항상 동작하며 결정론적 예제 경로/라디오맵을 계산합니다. "실제 레이 트레이싱"의
> 진짜 관문은 **GPU가 아니라 `sionna-rt` 패키지 설치**이며, GPU는 그 위에 얹는
> 추가 가속 레이어입니다.
>
> **네이티브 라이브러리:** 기본 의존성 중 `rtree`(libspatialindex)·`shapely`(GEOS)는
> C 라이브러리를 쓰지만, Windows/Linux/macOS 주류 환경에서는 **PyPI 휠에 번들**되어
> 별도 시스템 설치가 필요 없습니다. 휠이 없는 특이 환경(비주류 아키텍처)에서 소스
> 빌드할 때만 시스템 GEOS/libspatialindex가 필요합니다. **ffmpeg 등 외부 실행
> 바이너리는 런타임에 전혀 필요하지 않습니다.**
>
> **디스크 여유:** 기본 설치 외에 `sionna-rt`+Mitsuba/Dr.Jit ≈ 수백 MB, (선택)
> FTC 재질 오버레이 ≈ 120 MB, (선택) `reference-bundle/` 원본 씬 자산 ≈ 450 MB(git
> 미포함).

---

## 경로 A — `pip install seam-studio` (소스 체크아웃 불필요)

앱을 **실행만** 하려는 경우의 최단 경로입니다. 리포 클론도, **Node.js도 필요
없습니다** (휠에 빌드된 UI가 들어 있고, Sionna RT는 기본 의존성으로 함께
설치됩니다).

**Windows (PowerShell):**

```powershell
py -3.12 -m venv seam-env
seam-env\Scripts\pip install seam-studio
seam-env\Scripts\seam-studio         # http://127.0.0.1:8000 서빙 + 브라우저 오픈
```

**Linux / macOS:**

```bash
python3.12 -m venv seam-env
seam-env/bin/pip install seam-studio
seam-env/bin/seam-studio             # http://127.0.0.1:8000 서빙 + 브라우저 오픈
```

첫 실행 시 `~/.seam/projects/`를 만들고 **Sample Demo** 프로젝트를 생성한 뒤
브라우저를 엽니다. 유용한 플래그: `--port N`, `--project-root DIR`,
`--no-browser`.

소스 체크아웃 경로(아래 경로 B)와의 차이:

- 사전 생성 데모는 Sample Demo뿐 — Lab Room / FTC Outdoor 예제는 리포
  체크아웃에만 들어 있습니다.
- 모든 것이 리포 대신 `~/.seam/`에 앵커됩니다: 프로젝트는
  `~/.seam/projects/`, (선택) 멀티 엔진 레지스트리는 `~/.seam/engines.json`
  (엔진 venv는 동일하게 동작하고 워커 스크립트는 패키지에 번들됨).
- UI는 백엔드가 한 포트에서 함께 서빙합니다(별도 Vite 서버 없음). 프론트엔드
  수정은 소스 경로가 필요합니다.

업그레이드는 `pip install -U seam-studio`.

## 경로 B — 소스 빠른 설치 (한 줄)

리포지토리 루트에서 실행합니다. venv 생성 → 백엔드/프론트엔드 설치 → 데모
프로젝트 생성까지 한 번에 처리하며, **여러 번 실행해도 안전(idempotent)**합니다.

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1
```

**Linux / macOS:**

```bash
bash scripts/install.sh
```

설치가 끝나면 [서버 실행](#서버-실행)으로 넘어가세요.

---

## 수동 설치 (단계별)

스크립트 대신 직접 설치하려면 아래 순서를 따릅니다. 모든 명령은 **리포 루트**
기준입니다.

### 1. 백엔드 venv 생성 + 설치

이 프로젝트는 `requirements.txt`가 아니라 **`backend/pyproject.toml`**로 의존성을
관리합니다. editable 설치(`-e`)에 `dev` 엑스트라(pytest 포함)를 함께 설치합니다.

**Windows:**

```powershell
python -m venv backend\.venv
backend\.venv\Scripts\python.exe -m pip install --upgrade pip
backend\.venv\Scripts\python.exe -m pip install -e "backend[dev]"
```

**Linux / macOS:**

```bash
python3 -m venv backend/.venv
backend/.venv/bin/python -m pip install --upgrade pip
backend/.venv/bin/python -m pip install -e "backend[dev]"
```

기본 의존성(`backend/pyproject.toml` 기준): `fastapi`, `uvicorn[standard]`,
`pydantic`, `numpy`, `scipy`, `trimesh`, `rtree`, `shapely`, `mapbox-earcut`,
`pyyaml`, `httpx`, `pillow`, `ddgs`, `python-multipart`. `rtree`(libspatialindex)와
`shapely`(GEOS)는 네이티브 C 라이브러리를 쓰지만 주류 플랫폼에서는 PyPI 휠에
번들되어 별도 설치가 필요 없습니다.

### 2. 프론트엔드 설치

```bash
cd frontend
npm install
```

### 3. (선택) 데모 프로젝트 재생성

데모 3종(**sample_demo · lab_room · ftc_outdoor**)의 생성물은 이미 리포에
**커밋되어 있어**, 설치 직후 앱에서 바로 보입니다. 이 단계는 필수가 아니며,
데모를 처음부터 다시 만들고 싶을 때만 실행합니다. (한 줄 설치 스크립트도 이
스크립트들을 호출하지만, 아래 번들 임포트는 `reference-bundle/`이 있을 때만
실행하고 없으면 경고 후 건너뜁니다 — 커밋된 데모를 그대로 사용.)

`create_demo_project.py`는 번들 없이 항상 동작합니다. `import_bundle_scene.py`
계열은 `reference-bundle/`(대용량 씬 자산, ~450 MB, git 미포함)이 리포 루트에
있을 때만 필요하며, 번들을 별도로 내려받아 재임포트/재생성할 때만 씁니다.

**Windows:**

```powershell
backend\.venv\Scripts\python.exe examples\scripts\create_demo_project.py
backend\.venv\Scripts\python.exe examples\scripts\import_bundle_scene.py
backend\.venv\Scripts\python.exe examples\scripts\import_bundle_scene.py --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb"
```

**Linux / macOS:**

```bash
backend/.venv/bin/python examples/scripts/create_demo_project.py
backend/.venv/bin/python examples/scripts/import_bundle_scene.py
backend/.venv/bin/python examples/scripts/import_bundle_scene.py --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb"
```

> **참고:** 위 두 번째·세 번째 명령(`import_bundle_scene.py`)은 `reference-bundle/`이
> 리포 루트에 있어야 동작합니다. 없으면 `create_demo_project.py`만 실행하세요
> — lab_room·ftc_outdoor는 이미 커밋된 상태로 유지됩니다.

- `create_demo_project.py` → **sample_demo** (작은 야외 도심 씬: 지면/도로/건물
  2동+창문/나무, TX/RX, 차량·보행자 액터). `examples/demo_project/` 아래에 씁니다.
- `import_bundle_scene.py` (인자 없음) → **lab_room** (참조 번들의 실내 28 GHz 랩룸
  씬을 로드 가능한 프로젝트로 임포트).
- `import_bundle_scene.py --scene-id ftc_outdoor …` → **ftc_outdoor** (참조 번들의
  야외 28 GHz FTC 씬 + 재구성 맵 오버레이). 프로젝트 셀렉트에서 `FTC Outdoor`로
  전환할 수 있습니다.

> **참고:** FTC 오버레이 GLB(`FTC_OSM_ReconstructedMap_ZUp_v2.glb`)는 용량이
> 큽니다(약 120 MB). 임포트 시 프로젝트의 `visual/overlay.glb`로 복사되므로
> 디스크 여유가 필요하며, 이 한 줄만 다른 두 명령보다 다소 오래 걸립니다.

두 스크립트 모두 결과 요약(프림/디바이스 수, 재질 목록, GLB 메시명)을 출력하며,
같은 씬을 다시 생성하면 동일 출력이 나옵니다.

---

## 서버 실행

### 스크립트로 (권장)

**Windows:**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
```

**Linux / macOS:**

```bash
bash scripts/start.sh
```

백엔드(:8000)와 프론트엔드(:5173)를 함께 띄우고 URL을 출력합니다.

### 수동으로 (터미널 2개)

**터미널 1 — 백엔드 (:8000):**

```bash
backend/.venv/bin/python -m uvicorn --app-dir backend seam_studio.main:app --port 8000
```

(Windows: `backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend seam_studio.main:app --port 8000`)

**터미널 2 — 프론트엔드 (:5173):**

```bash
cd frontend
npm run dev
```

브라우저에서 **http://localhost:5173** 을 엽니다. Vite 개발 서버가 `/api` 요청을
`http://127.0.0.1:8000`으로 프록시하므로 CORS 설정이 필요 없습니다.
**Sample Demo** 프로젝트가 자동으로 로드됩니다.

백엔드 상태는 http://127.0.0.1:8000/api/health 에서 확인할 수 있습니다(Sionna·AI
제공자 가용성 포함).

---

## 실제 Sionna RT 엔진 (자동 설치됨)

`sionna-rt`(Mitsuba 3 / Dr.Jit 포함, 수백 MB)는 **기본 의존성**입니다 — 경로 A와
경로 B 모두 자동으로 함께 설치되며, 따로 실행할 것이 없습니다. 검증 버전은
`sionna-rt 2.0.x`이고, 과거의 `backend[sionna]` extra는 무해한 no-op 별칭으로
남아 있습니다.

Sionna가 정상 로드되면 툴바 우측 상태칩이 (**Mock only** 대신) **Sionna**로 표시되고,
Simulation 패널의 **Backend** 셀렉트에서 `auto`/`sionna`를 고를 수 있습니다. 임포트가
깨진 경우(예: 지원하지 않는 Python/휠 조합)에는 경고를 내고 Mock 백엔드로 계속
동작합니다 — 복구하려면 백엔드 venv에 재설치하세요:

```powershell
# Windows                                   # Linux/macOS
backend\.venv\Scripts\python.exe -m pip install --force-reinstall "sionna-rt>=2.0"
backend/.venv/bin/python -m pip install --force-reinstall "sionna-rt>=2.0"
```

> **GPU / OS별 백엔드 요약**
> - **Mock 백엔드**: 아무것도 필요 없음 — CPU만으로 항상 동작(설치 불필요).
> - **Linux / Windows + NVIDIA GPU**: `sionna-rt`가 CUDA(Dr.Jit) 백엔드를
>   **자동 선택**합니다. 드라이버만 정상이면 별도 설정이 필요 없습니다.
> - **macOS(Apple Silicon 포함)**: Dr.Jit에는 **Metal/MPS 백엔드가 없어** Sionna는
>   항상 **CPU/LLVM**으로 동작합니다. 정상 동작하지만 GPU 대비 **느립니다**.
>   CUDA를 찾지 못하면 앱이 자동으로 LLVM으로 폴백하며 결과 경고에
>   "CUDA unavailable — using LLVM (CPU) ray tracing …" 한 줄을 남깁니다(무해).
>   **macOS 1회 사전 조건**: macOS용 drjit 휠에는 LLVM 공유 라이브러리가 **동봉되지
>   않아**, 솔브 시 *"the LLVM backend is inactive because the LLVM shared library
>   (libLLVM.dylib) could not be found"* 오류가 날 수 있습니다. `DRJIT_LIBLLVM_PATH`를
>   libLLVM으로 지정하세요: Xcode CLT에 이미 있으면
>   `/Library/Developer/CommandLineTools/usr/lib/libLLVM.dylib`, 없으면
>   `brew install llvm` 후 `"$(brew --prefix llvm)/lib/libLLVM.dylib"`.
>   `~/.zshrc` 등에 export해 두고 `seam-studio`를 실행하면 됩니다.

---

## (선택) 대체 Sionna 엔진 venv — 버전 교체 (예: sionna-rt 1.2.2)

경로(paths) 솔브를 **다른 Sionna 버전**으로 돌릴 수 있습니다. 별도 venv에 원하는
버전을 설치하고 루트 `engines.json`에 등록하면, Results 모드의 **Engine**
셀렉트에 나타납니다. (리포에는 이미 `sionna-rt-1.2.2` 항목이 등록되어 있습니다.)

**1) venv 생성 + 원하는 버전 설치 (Windows 예시, 1.2.2):**

```powershell
py -3.12 -m venv backend\.venv-sionna-rt-122
backend\.venv-sionna-rt-122\Scripts\python.exe -m pip install "sionna-rt==1.2.2"
```

> **venv의 Python 버전을 고정할 것.** sionna-rt 1.x는 Python 3.13까지만 나온
> mitsuba/drjit 휠을 고정하므로, Python 3.14에서는 `pip install`이 *성공*해도
> `import sionna.rt`가 실패하고 엔진이 사용 불가로 표시된다. Python 3.12는
> 1.x/2.x 전 릴리스에서 동작한다.

**2) 루트 `engines.json`에 항목 추가** (이미 존재하면 생략):

```json
{
  "id": "sionna-rt-1.2.2",
  "label": "Sionna RT 1.2.2 (v1.x line)",
  "python": "backend/.venv-sionna-rt-122/Scripts/python.exe",
  "adapter": "sionna_rt"
}
```

**3) 프로브 갱신:** 백엔드 재시작, 또는 `GET /api/engines?refresh=true` 호출.

가용성은 대상 venv에서 실제로 `import sionna.rt`를 실행해 확인합니다(콜드 임포트에
수십 초 걸릴 수 있음, 프로세스당 캐시). 자세한 내용은
[docs/engines.md](docs/engines.ko.md), [docs/sionna_versions.md](docs/sionna_versions.ko.md)
참조.

---

## (선택) 로컬 LLM/VLM — AI 재질 제안

AI는 완전히 선택입니다. AI 서버가 없으면 **규칙 기반(rule-based) 제공자**가 항상
대신 답하므로, 아무것도 설치하지 않아도 AI Assist 모드가 동작합니다. 로컬 LLM을
붙이면 더 풍부한 제안을 받을 수 있습니다.

지원 제공자(설정은 모두 환경변수):

- **Ollama** — 기본 `http://localhost:11434`, 텍스트 모델 `qwen3:8b`, 비전 모델
  `qwen2.5vl:3b`.
- **LM Studio** (OpenAI 호환 서버) — 기본 `http://localhost:1234/v1`,
  모델 `google/gemma-4-31b`.

**LM Studio 예시 (Windows PowerShell로 백엔드 실행 시):**

```powershell
$env:SEAM_OPENAI_URL   = "http://localhost:1234/v1"
$env:SEAM_OPENAI_MODEL = "google/gemma-4-31b"
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend seam_studio.main:app --port 8000
```

**주요 환경변수:** 정식 접두사는 `SEAM_*`이며, 모든 변수에 대해 레거시
`SIONNATWIN_*` 이름도 계속 인식됩니다(둘 다 설정되면 `SEAM_*`가 우선).
전체 목록과 주석은 `backend/.env.example` 참고.

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SEAM_PROJECT_ROOTS` | (내장 기본값: `projects/`, 그다음 `examples/demo_project/`) | 프로젝트 탐색 루트(경로 구분자로 나열). 첫 번째 루트가 새 프로젝트/UI 임포트가 저장되는 곳 |
| `SEAM_AI_ENABLED` | `auto` | `auto` / `on` / `off`(수동 전용) |
| `SEAM_OLLAMA_URL` | `http://localhost:11434` | Ollama 엔드포인트 |
| `SEAM_AI_TEXT_MODEL` | `qwen3:8b` | Ollama 텍스트 모델 |
| `SEAM_AI_VISION_MODEL` | `qwen2.5vl:3b` | 스크린샷 첨부 시 비전 모델 |
| `SEAM_OPENAI_URL` | `http://localhost:1234/v1` | LM Studio(OpenAI 호환) |
| `SEAM_OPENAI_MODEL` | `google/gemma-4-31b` | LM Studio 모델 |
| `SEAM_AI_TIMEOUT_S` | `60` | 텍스트 AI 요청 타임아웃(초) |
| `SEAM_AI_VISION_TIMEOUT_S` | `300` | 멀티모달(이미지 포함) 요청 타임아웃(초). 로컬 VLM은 모델 로드 + 다중 이미지 프리필로 텍스트보다 오래 걸리므로 상한이 더 높음 |
| `SEAM_AI_AUTO_APPLY` | `false` | 향후 자동 적용 게이트용 예약 플래그. 설정으로 파싱되지만 MVP에서는 **아무 코드도 이를 사용하지 않음** |
| `SEAM_OVERPASS_URL` | `https://overpass-api.de/api/interpreter` | OSM(OpenStreetMap) 임포트에 쓰는 Overpass API 엔드포인트 |

AI 출력은 엄격한 JSON 스키마로 검증되며, 파싱 실패 시 경고와 함께 규칙 기반으로
폴백합니다. 제안은 **절대 자동 적용되지 않습니다** — 사용자가 승인 후 *Apply
decisions*를 눌러야 하며, 모든 결정은 `ai/suggestions.jsonl`에 provenance와 함께
기록됩니다. 자세한 내용은 [docs/ai_assistant.md](docs/ai_assistant.ko.md) 참조.

---

## 테스트 / 빌드 검증

```bash
# 백엔드 단위 테스트
backend/.venv/bin/python -m pytest backend/tests -q

# 프론트엔드 타입체크 + 빌드
cd frontend && npm run build
```

(Windows: `backend\.venv\Scripts\python.exe -m pytest backend\tests -q`)

---

## 문제 해결 (Troubleshooting)

| 증상 | 원인 / 해결 |
|---|---|
| **포트 8000/5173 사용 중** | 다른 프로세스가 점유 중. 백엔드는 `--port 8001`처럼 다른 포트로 실행하거나 기존 프로세스를 종료. 단, 프론트엔드 프록시는 `127.0.0.1:8000`을 가리키므로 백엔드 포트를 바꾸면 `frontend/vite.config.ts`의 프록시 대상도 함께 바꿔야 합니다. |
| **PowerShell: "스크립트 실행이 사용 안 함"** (npm/스크립트 실행 정책 오류) | 실행 정책 때문입니다. 명령 앞에 `powershell -ExecutionPolicy Bypass -File ...`를 붙이거나, 현재 세션만 `Set-ExecutionPolicy -Scope Process Bypass` 실행. |
| **GPU 미탐지 / CUDA 없음** | 정상입니다. 앱이 자동으로 **Mock 백엔드**로 동작합니다. 실제 Sionna를 쓰려면 NVIDIA 드라이버+CUDA(또는 Sionna의 LLVM CPU 백엔드)가 필요합니다. |
| **`LLVM ... ` 경고 로그** | 무해합니다. Sionna의 Dr.Jit가 CPU(LLVM) 백엔드를 초기화할 때 나오는 정보성 경고이며 동작에 영향 없습니다. |
| **macOS: 솔브 시 "the LLVM backend is inactive … libLLVM.dylib could not be found"** | macOS용 drjit 휠에는 LLVM이 동봉되지 않습니다. 실행 전에 `DRJIT_LIBLLVM_PATH`를 libLLVM으로 지정하세요: Xcode CLT에 `/Library/Developer/CommandLineTools/usr/lib/libLLVM.dylib`가 있으면 그 경로, 없으면 `brew install llvm` 후 `"$(brew --prefix llvm)/lib/libLLVM.dylib"`. |
| **상태칩이 "Mock only"** | `sionna-rt` 임포트가 깨졌거나(불완전 설치 — 백엔드 venv에서 `pip install --force-reinstall "sionna-rt>=2.0"`로 재설치), CUDA/LLVM 백엔드가 없어서 Sionna가 스스로 비활성화된 상태입니다. Mock으로 전체 워크플로는 그대로 사용 가능합니다. |
| **상태칩이 "AI off"** | AI 서버(Ollama/LM Studio)에 연결되지 않은 상태. 규칙 기반 제안은 여전히 동작합니다. 로컬 LLM을 켜려면 위 [로컬 LLM/VLM](#선택-로컬-llmvlm--ai-재질-제안) 참조. |
| **프로젝트 목록이 비어 있음** | 데모 3종은 리포에 **기본 포함**되어 있어 보통 바로 보입니다. 백엔드는 두 곳을 순서대로 탐색합니다 — 먼저 리포 루트의 `projects/`(루트 #1, UI에서 임포트한 프로젝트가 저장되는 곳; 새 클론에서는 비어 있거나 없을 수 있음), 그다음 커밋된 데모가 있는 `examples/demo_project/`. 비어 있다면 백엔드가 이 둘을 찾지 못한 것 — 리포 루트에서 서버를 실행했는지, `SEAM_PROJECT_ROOTS`(레거시 `SIONNATWIN_PROJECT_ROOTS`)를 덮어써 기본 루트를 가리지 않았는지 확인하세요. [3. (선택) 데모 프로젝트 재생성](#3-선택-데모-프로젝트-재생성) 스크립트는 데모를 *재생성*할 때만 필요합니다. |
| **`import sionna.rt` 콜드 임포트가 느림** | 대체 엔진 첫 프로브는 수십 초 걸릴 수 있습니다(프로세스당 1회 캐시). 이후는 빨라집니다. |
| **Windows에서 `localhost` 프록시 실패** | Vite 프록시는 의도적으로 `127.0.0.1:8000`을 사용합니다(Windows에서 `localhost`가 IPv6 `::1`로 먼저 해석되어 uvicorn IPv4 바인딩과 어긋나는 문제 회피). 백엔드가 IPv4 루프백에 바인딩되었는지 확인하세요. |

---

## 다음 단계

- 첫 15분 사용법: [TUTORIAL.md](TUTORIAL.ko.md)
- 엔진 버전 교체: [docs/engines.md](docs/engines.ko.md),
  [docs/sionna_versions.md](docs/sionna_versions.ko.md)
- RT 정확도와 완화책: [docs/accuracy.md](docs/accuracy.ko.md)
- 아키텍처 / 씬 포맷: [docs/architecture.md](docs/architecture.ko.md),
  [docs/scene_format.md](docs/scene_format.ko.md)

> 검증된 인터프리터: Python 3.11(CI·리눅스) / 3.12(개발·Windows) / 3.13(클린 venv pip 설치·macOS) / 3.14(클린 venv 설치 확인·Windows). Node 20+.
