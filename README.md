# SionnaTwin Studio

**Sionna RT 기반 로컬 디지털 트윈 워크벤치** — 하나의 텍스처 3D 씬에서 모든
메시 프림이 **두 개의 재질 바인딩**(렌더링용 visual/PBR + 전자기 시뮬레이션용
RF)을 동시에 지니고, 캐노니컬 씬을 Sionna 호환 RF 프로젝션으로 컴파일해 레이
경로·라디오맵 결과를 다시 같은 씬 위에 시각화합니다.

GPU도, Sionna 설치도, LLM도 **필수가 아닙니다** — 세 가지 모두 선택적 업그레이드일
뿐, 기본 **Mock 백엔드는 CPU만으로 항상 동작**합니다.

```text
Unified RF-Visual Scene Graph          (scene.sionnatwin.json - source of truth)
  ├─ Visual Projection  →  GLB / textures / Three.js viewer
  └─ RF Projection      →  PLY material groups + Mitsuba XML → Sionna RT
```

---

## Quickstart (3 commands)

**Windows (PowerShell):**

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install.ps1   # 1. 설치 + 데모 생성
powershell -ExecutionPolicy Bypass -File scripts\start.ps1     # 2. 백엔드+프론트 실행
# 3. 브라우저에서 http://localhost:5173 열기 (KAIST Demo 자동 로드)
```

**Linux / macOS:**

```bash
bash scripts/install.sh   # 1. 설치 + 데모 생성
bash scripts/start.sh     # 2. 백엔드+프론트 실행
# 3. 브라우저에서 http://localhost:5173 열기 (KAIST Demo 자동 로드)
```

수동 설치·엔진 옵션·문제 해결은 **[INSTALL.md](INSTALL.md)**, 첫 15분 실습은
**[TUTORIAL.md](TUTORIAL.md)** 를 보세요.

---

## RT GUI 대비 차별점

공식 NVlabs `sionna-rt-gui`(Polyscope 데스크톱 앱)는 씬을 로드하고 TX/RX를
배치·애니메이션하며 경로 + 래스터 라디오맵을 보여주지만, **메시 라디오맵·빔포밍·
재질 편집은 명시적으로 지원하지 않습니다.** SionnaTwin Studio는 같은 Sionna RT
엔진 위에 다음을 더합니다.

| 기능 | `sionna-rt-gui` (공식) | SionnaTwin Studio |
|---|:---:|:---:|
| 경로 + 래스터 라디오맵 | ✅ | ✅ |
| 통합 RF-Visual 씬 그래프 (**이중 재질 바인딩**) | ❌ | ✅ |
| RF 재질 **지정 + 검증 + AI/규칙 제안** | ❌ | ✅ |
| **Mock 백엔드** (GPU/Sionna 없이 동작) | ❌ | ✅ |
| **MIMO 빔포밍** 이득 (코드북 스윕 / TX-MRT / SVD) | ❌ | ✅ |
| **채널 분석** (링크버짓, CIR/CFR, PL 모델 vs RT) | ❌ | ✅ |
| **궤적 RF 지표** (RSS / path gain / RMS delay) | ❌ | ✅ |
| **RFData 내보내기** (AODT 뷰어 컨트랙트) | ❌ | ✅ |
| **ML 데이터셋** 생성 (npz + metadata) | ❌ | ✅ |
| **Sionna 엔진 버전 교체** (별도 venv) | ❌ | ✅ |
| 웹 UI (브라우저) | ❌ (데스크톱) | ✅ |
| 인뷰어 디바이스-궤적 재생 / 이동 기즈모 | ✅ | 🚧 로드맵 |

---

## Feature highlights

- **하나의 씬, 두 개의 재질.** 프림의 `visual`/`rf` 블록은 프림에서만 만나는
  별개 객체입니다. 텍스처 파일명은 RF 진실이 아니며, AI/규칙은 이를 *증거*로만
  인용하고, 지정은 provenance를 지닌 채 진화합니다:
  `unassigned → rule_suggested / ai_suggested → user_confirmed → measurement_calibrated`.
- **다섯 가지 모드 UI** — Visual / RF Materials / Validation / AI Assist /
  Results. 오브젝트를 클릭하면 시각/RF 재질·지정 소스·검증 경고·결과 오버레이가
  모두 같은 오브젝트에 묶여 표시됩니다.
- **클릭 배치 & 뷰포트 픽킹** — TX/RX 디바이스 배치, 궤적 start/end, 데이터셋
  샘플링 영역을 좌표 입력 대신 **뷰포트에서 직접 클릭**해 지정합니다. 씬 경계
  (`GET /scene/bounds`)로 영역 기본값을 미리 채우고, 점선 미리보기로 확인합니다.
- **도킹 가능한 패널** — 패널 헤더의 ◧/◨/⧉ 버튼으로 사이드바 간 이동 또는
  뷰포트 위 플로팅 창으로 분리할 수 있으며, 플로팅 상태는 모드 탭 전환에도
  유지됩니다.
- **Metrics dashboard + 논문용 내보내기** — 링크 KPI(RSS/RSRP/RSSI/RSRQ/SNR/
  Shannon 용량/지연확산/도플러…)와 CIR·CFR·도플러·경로손실 차트를 한 패널에서
  한눈에. 모든 그림은 흰 배경 Times New Roman(serif) 논문 스타일이며, 차트마다
  **PNG/SVG/CSV export** 버튼이 내장됩니다. 뷰포트 **📸**(보이는 그대로 PNG) /
  **🎞**(Mitsuba 오프라인 렌더)로 씬 이미지도 저장.
- **라이브 채널 파라미터 튜닝 + 3GPP 측정량** — Channel 패널의 Live parameters
  에서 주파수/대역폭/TX 파워/잡음지수/SCS(부반송파 간격)를 즉시 조정하면 자동
  재분석되고, **TS 38.215 스타일 RSRP/RSSI/RSRQ**(요청 SCS의 OFDM 자원격자 기준)
  가 함께 산출됩니다.
- **결정론적 Mock 백엔드** — GPU/Sionna 없이 Friis + 이미지법 반사로 예제
  경로/라디오맵을 계산. 프론트엔드·테스트가 하드웨어 없이 돌아갑니다.
- **실제 Sionna RT 경로** — `sionna-rt`(검증 2.0.x) 설치 시 컴파일된
  `generated_scene.xml`이 그대로 로드되어 GPU(Dr.Jit CUDA) 또는 CPU(LLVM)에서
  경로/라디오맵을 계산하고, 같은 스키마로 정규화됩니다.
- **AODT 정렬** — 28 GHz 기본 + ITU-R P.2040 재질 세트(+`human_body`), AODT 스타일
  다크 뷰어(LOS 시안 / 반사 마젠타 / 회절 주황), RFData 내보내기 컨트랙트.
- **선택적 로컬 AI** — 강제 제공자 → Ollama → 규칙 기반 폴백 체인. 엄격한 JSON
  스키마 검증, 제안은 절대 자동 적용되지 않고 provenance가 남습니다.

전체 데모 흐름은 [TUTORIAL.md](TUTORIAL.md) 참조.

---

## 프로그래매틱 API (UI 없는 엔드포인트)

대부분의 기능은 웹 UI로 쓰지만, 다음 두 엔드포인트는 **전용 UI 버튼이 없고
curl/스크립트로 프로그래매틱하게** 호출한다(백엔드는 기본 `http://127.0.0.1:8000`).

- **`POST /api/projects/{id}/live/state`** — **외부 실세계 위치 주입.**
  GPS/모캡/로그의 디바이스·액터 위치를 로드된 씬에 밀어 넣는다. UI의 *Live sync*
  폴링이 이 상태를 그대로 반영하므로, 외부 소스가 이 엔드포인트로 계속 밀면 뷰어가
  실시간으로 따라 움직인다. `persist=true` 로 씬에 저장, `resimulate=true` 로 즉시
  경로를 다시 풀어 최신 링크 지표를 돌려받아 measure → sync → predict 루프를 돌릴 수
  있다.

  ```bash
  curl -X POST http://127.0.0.1:8000/api/projects/kaist_demo/live/state \
    -H "Content-Type: application/json" \
    -d '{"devices":[{"id":"rx_001","position":[10.0,5.0,1.5]}],"actors":[{"id":"veh_001","position":[20.0,0.0,0.0],"orientation_deg":[0.0,0.0,90.0]}],"resimulate":true,"persist":false}'
  ```

- **`POST /api/projects/{id}/calibrate/materials`** — **측정 기반 재질 캘리브레이션.**
  측정된 링크별 path gain 을 넣으면 한 개의 RF 재질 파라미터를 그리드 서치로 피팅해
  RT-측정 오차를 줄이고 before/after 리포트를 돌려준다. `apply=true` 면 피팅값을
  재질 라이브러리에 쓰고 해당 프림을 `measurement_calibrated` 로 승격한다.

  ```bash
  curl -X POST http://127.0.0.1:8000/api/projects/kaist_demo/calibrate/materials \
    -H "Content-Type: application/json" \
    -d '{"measurements":[{"rx_position":[10.0,5.0,1.5],"measured_path_gain_db":-92.0}],"target_material_id":"concrete","param":"scattering_coefficient","apply":false}'
  ```

---

## Docs index

| 문서 | 내용 |
|---|---|
| [INSTALL.md](INSTALL.md) | 사전 요구사항, 설치(스크립트/수동), 엔진·LLM 옵션, 문제 해결 |
| [TUTORIAL.md](TUTORIAL.md) | 15분 첫 세션 실습 (씬 → 재질 → 시뮬 → 데이터셋) |
| [docs/architecture.md](docs/architecture.md) | 통합 씬 그래프와 이중 프로젝션 아키텍처 |
| [docs/scene_format.md](docs/scene_format.md) | 씬·프로젝트 폴더 포맷과 스키마 |
| [docs/rf_materials.md](docs/rf_materials.md) | RF 재질 라이브러리와 모델 |
| [docs/ai_assistant.md](docs/ai_assistant.md) | AI 제안 제공자, 규칙, provenance |
| [docs/engines.md](docs/engines.md) | Sionna 엔진 버전 교체(별도 venv, `engines.json`) |
| [docs/sionna_versions.md](docs/sionna_versions.md) | Sionna 버전별 기능·재질·모델 변천사(검증 문헌) |
| [docs/accuracy.md](docs/accuracy.md) | RT-측정 오차와 완화책 |
| [docs/roadmap.md](docs/roadmap.md) | MVP 이후 로드맵과 확장 포인트 |
| [docs/research_ideas.md](docs/research_ideas.md) | 논문화 가능한 연구 방향 |
| [HANDOFF.md](HANDOFF.md) | 이 구현이 따르는 운영 명세 |

---

## Architecture (one-liner)

Pydantic v2 스키마의 캐노니컬 씬(`scene.sionnatwin.json`)을 진실의 원천으로 삼아,
FastAPI 백엔드가 이를 Visual(GLB) / RF(Mitsuba XML + PLY 그룹) 두 프로젝션으로
컴파일하고, React + react-three-fiber 프론트엔드가 snake_case 와이어 포맷을
그대로 미러링하며 결과를 같은 Z-up ENU 미터 좌표계 씬 위에 되돌려 그립니다.

**스택:** 백엔드 Python 3.11+ / FastAPI / Pydantic v2 / NumPy / trimesh,
프론트엔드 React + Vite + TypeScript + react-three-fiber + Zustand, 선택적
`sionna-rt`(Dr.Jit/Mitsuba 3) 백엔드.

---

## Repository layout

```text
backend/    FastAPI app: schemas (Pydantic v2), project store, scene validator,
            RF material assignment, RF projection compiler (trimesh),
            simulation backends (Mock + optional Sionna RT), AI providers
frontend/   React + Vite + TypeScript + react-three-fiber workbench
examples/   demo project generators (kaist_demo, lab_room import)
scripts/    install / start scripts (PowerShell + bash)
docs/       architecture, scene format, RF materials, AI, engines, accuracy, roadmap
HANDOFF.md  operating specification this implementation follows
```

---

## Testing

```bash
backend/.venv/bin/python -m pytest backend/tests -q   # 백엔드 단위 테스트
cd frontend && npm run build                          # 타입체크 + 빌드
```

(Windows: `backend\.venv\Scripts\python.exe -m pytest backend\tests -q`)

---

## License / Credits

라이선스는 추후 명시 예정입니다(placeholder). [Sionna RT](https://github.com/NVlabs/sionna-rt)
(NVlabs) 위에 구축되었으며, AODT 뷰어 정렬은 `sionna-rt-gui-jaewoo-examples/`
참조 번들(28 GHz FTC/랩룸 ISAC 디지털 트윈)을 따릅니다.
