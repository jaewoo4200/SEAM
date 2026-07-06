# 설치 가이드 (INSTALL)

SionnaTwin Studio는 **로컬 우선(local-first)** 워크벤치입니다. GPU도, Sionna
설치도, LLM도 **필수가 아닙니다** — 세 가지 모두 선택적 업그레이드일 뿐이고,
기본 **Mock 백엔드는 CPU만으로 항상 동작**합니다. 먼저 기본 설치로 앱을 띄우고,
필요할 때 실제 Sionna RT 엔진과 로컬 LLM을 붙이면 됩니다.

- 최단 경로만 원한다면 → [빠른 설치 (한 줄)](#빠른-설치-한-줄)
- 첫 15분 사용법은 → [TUTORIAL.md](TUTORIAL.md)
- 프로젝트 소개/구조는 → [README.md](README.md)

---

## 사전 요구사항 (Prerequisites)

| 항목 | 요구 버전 | 비고 |
|---|---|---|
| Python | **3.11 이상** | 백엔드(FastAPI). `python --version`으로 확인 |
| Node.js | **20 이상** (18+도 대체로 동작) | 프론트엔드(Vite). `node --version`으로 확인 |
| OS | Windows 10/11, Linux, macOS | 스크립트는 Windows(PowerShell)/Unix(bash) 모두 제공 |
| NVIDIA GPU + 드라이버 | **선택** | 실제 `sionna-rt` 엔진의 CUDA(Dr.Jit) 백엔드에만 필요. 없으면 Mock 백엔드 사용, 또는 Sionna의 LLVM(CPU) 백엔드 사용 |

> **GPU가 없어도 됩니다.** GPU가 없으면 앱은 자동으로 Mock 백엔드로 동작하며,
> 결정론적(deterministic) 예제 경로/라디오맵을 계산합니다. 실제 Sionna RT는
> CUDA GPU 또는 LLVM CPU 백엔드 중 하나만 있으면 됩니다.

---

## 빠른 설치 (한 줄)

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

기본 의존성: `fastapi`, `uvicorn[standard]`, `pydantic`, `numpy`, `trimesh`,
`pyyaml`, `httpx`, `pillow`, `networkx`, `python-multipart`(씬 임포트의 멀티파트
업로드에 필요).

### 2. 프론트엔드 설치

```bash
cd frontend
npm install
```

### 3. 데모 프로젝트 생성

데모 프로젝트의 생성물(GLB, 씬 JSON 등)은 리포에 커밋되지 않으므로, 최초 1회
생성 스크립트를 실행해야 앱에서 프로젝트가 보입니다.

**Windows:**

```powershell
backend\.venv\Scripts\python.exe examples\scripts\create_demo_project.py
backend\.venv\Scripts\python.exe examples\scripts\import_bundle_scene.py
> **참고**: `reference-bundle/`(대용량 씬 자산, ~450 MB)은 git에 포함되지 않습니다.
> FTC outdoor 데모는 이미 임포트된 상태로 리포에 포함되어 있으므로 이 단계는
> 번들을 별도로 내려받아 리포 루트의 `reference-bundle/`에 두었을 때만 필요합니다
> (재임포트/재생성 용도).

backend\.venv\Scripts\python.exe examples\scripts\import_bundle_scene.py --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb"
```

**Linux / macOS:**

```bash
backend/.venv/bin/python examples/scripts/create_demo_project.py
backend/.venv/bin/python examples/scripts/import_bundle_scene.py
backend/.venv/bin/python examples/scripts/import_bundle_scene.py --xml "reference-bundle/outdoor_material_assigned_cv_28ghz_safe.xml" --scene-id ftc_outdoor --name "FTC Outdoor (28 GHz)" --environment outdoor --visual-overlay "reference-bundle/outdoor_visual/FTC_OSM_ReconstructedMap_ZUp_v2.glb"
```

- `create_demo_project.py` → **kaist_demo** (야외 캠퍼스 코너 씬: 지면/도로/건물
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
backend/.venv/bin/python -m uvicorn --app-dir backend app.main:app --port 8000
```

(Windows: `backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend app.main:app --port 8000`)

**터미널 2 — 프론트엔드 (:5173):**

```bash
cd frontend
npm run dev
```

브라우저에서 **http://localhost:5173** 을 엽니다. Vite 개발 서버가 `/api` 요청을
`http://127.0.0.1:8000`으로 프록시하므로 CORS 설정이 필요 없습니다.
**KAIST Demo** 프로젝트가 자동으로 로드됩니다.

백엔드 상태는 http://127.0.0.1:8000/api/health 에서 확인할 수 있습니다(Sionna·AI
제공자 가용성 포함).

---

## (선택) 실제 Sionna RT 엔진 설치

Mock 백엔드로 전체 워크플로를 쓸 수 있지만, 실제 레이 트레이싱을 원하면
`sionna` 엑스트라를 백엔드 venv에 설치합니다(Mitsuba 3 / Dr.Jit 포함, 수백 MB).
검증 버전은 `sionna-rt 2.0.x`입니다.

**Windows:**

```powershell
backend\.venv\Scripts\python.exe -m pip install -e "backend[sionna]"
```

**Linux / macOS:**

```bash
backend/.venv/bin/python -m pip install -e "backend[sionna]"
```

설치 후 백엔드를 재시작하면 툴바 우측 상태칩이 **Mock only → Sionna**로 바뀌고,
Simulation 패널의 **Backend** 셀렉트에서 `auto`/`sionna`를 고를 수 있습니다.
Sionna는 CUDA GPU(Dr.Jit CUDA) 또는 CPU(Dr.Jit LLVM) 중 하나가 필요하며, 둘 다
없으면 경고를 내고 Mock으로 되돌아갑니다(앱은 절대 죽지 않습니다).

---

## (선택) 대체 Sionna 엔진 venv — 버전 교체 (예: sionna-rt 1.2.2)

경로(paths) 솔브를 **다른 Sionna 버전**으로 돌릴 수 있습니다. 별도 venv에 원하는
버전을 설치하고 루트 `engines.json`에 등록하면, Results 모드의 **Engine**
셀렉트에 나타납니다. (리포에는 이미 `sionna-rt-1.2.2` 항목이 등록되어 있습니다.)

**1) venv 생성 + 원하는 버전 설치 (Windows 예시, 1.2.2):**

```powershell
python -m venv backend\.venv-sionna-rt-122
backend\.venv-sionna-rt-122\Scripts\python.exe -m pip install "sionna-rt==1.2.2"
```

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
[docs/engines.md](docs/engines.md), [docs/sionna_versions.md](docs/sionna_versions.md)
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
$env:SIONNATWIN_OPENAI_URL   = "http://localhost:1234/v1"
$env:SIONNATWIN_OPENAI_MODEL = "google/gemma-4-31b"
backend\.venv\Scripts\python.exe -m uvicorn --app-dir backend app.main:app --port 8000
```

**주요 환경변수:**

| 변수 | 기본값 | 설명 |
|---|---|---|
| `SIONNATWIN_AI_ENABLED` | `auto` | `auto` / `on` / `off`(수동 전용) |
| `SIONNATWIN_OLLAMA_URL` | `http://localhost:11434` | Ollama 엔드포인트 |
| `SIONNATWIN_AI_TEXT_MODEL` | `qwen3:8b` | Ollama 텍스트 모델 |
| `SIONNATWIN_AI_VISION_MODEL` | `qwen2.5vl:3b` | 스크린샷 첨부 시 비전 모델 |
| `SIONNATWIN_OPENAI_URL` | `http://localhost:1234/v1` | LM Studio(OpenAI 호환) |
| `SIONNATWIN_OPENAI_MODEL` | `google/gemma-4-31b` | LM Studio 모델 |
| `SIONNATWIN_AI_TIMEOUT_S` | `60` | AI 요청 타임아웃(초) |
| `SIONNATWIN_PROJECT_ROOTS` | (내장 기본값) | 프로젝트 탐색 루트(경로 구분자로 나열) |

AI 출력은 엄격한 JSON 스키마로 검증되며, 파싱 실패 시 경고와 함께 규칙 기반으로
폴백합니다. 제안은 **절대 자동 적용되지 않습니다** — 사용자가 승인 후 *Apply
decisions*를 눌러야 하며, 모든 결정은 `ai/suggestions.jsonl`에 provenance와 함께
기록됩니다. 자세한 내용은 [docs/ai_assistant.md](docs/ai_assistant.md) 참조.

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
| **상태칩이 "Mock only"** | `sionna-rt`가 설치되지 않았거나(→ `backend[sionna]` 설치), CUDA/LLVM 백엔드가 없어서 Sionna가 스스로 비활성화된 상태입니다. Mock으로 전체 워크플로는 그대로 사용 가능합니다. |
| **상태칩이 "AI off"** | AI 서버(Ollama/LM Studio)에 연결되지 않은 상태. 규칙 기반 제안은 여전히 동작합니다. 로컬 LLM을 켜려면 위 [로컬 LLM/VLM](#선택-로컬-llmvlm--ai-재질-제안) 참조. |
| **프로젝트 목록이 비어 있음** | 데모 3종은 리포에 **기본 포함**되어 있어 보통 바로 보입니다. 비어 있다면 백엔드가 `examples/demo_project/`를 찾지 못한 것 — 리포 루트에서 서버를 실행했는지, `SIONNATWIN_PROJECT_ROOTS`를 덮어쓰지 않았는지 확인하세요. [3. 데모 프로젝트 생성](#3-데모-프로젝트-생성) 스크립트는 데모를 *재생성*할 때만 필요합니다. |
| **`import sionna.rt` 콜드 임포트가 느림** | 대체 엔진 첫 프로브는 수십 초 걸릴 수 있습니다(프로세스당 1회 캐시). 이후는 빨라집니다. |
| **Windows에서 `localhost` 프록시 실패** | Vite 프록시는 의도적으로 `127.0.0.1:8000`을 사용합니다(Windows에서 `localhost`가 IPv6 `::1`로 먼저 해석되어 uvicorn IPv4 바인딩과 어긋나는 문제 회피). 백엔드가 IPv4 루프백에 바인딩되었는지 확인하세요. |

---

## 다음 단계

- 첫 15분 사용법: [TUTORIAL.md](TUTORIAL.md)
- 엔진 버전 교체: [docs/engines.md](docs/engines.md),
  [docs/sionna_versions.md](docs/sionna_versions.md)
- RT 정확도와 완화책: [docs/accuracy.md](docs/accuracy.md)
- 아키텍처 / 씬 포맷: [docs/architecture.md](docs/architecture.md),
  [docs/scene_format.md](docs/scene_format.md)

> 검증된 인터프리터: Python 3.11/3.12 (3.13+는 미검증), Node 20+.
