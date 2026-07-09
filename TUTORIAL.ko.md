# 15분 첫 세션 튜토리얼 (TUTORIAL)

> [English](TUTORIAL.md) · **한국어**

SEAM Studio를 처음 켠 사용자가 **15분 안에** 씬 탐색 → 재질 지정 → 경로
시뮬레이션 → 라디오맵 → 빔포밍 → 채널 분석 → 디바이스 이동 → 액터 시나리오 →
궤적 라이브 레이 → ML 데이터셋 생성까지 한 바퀴 돌아보는 실습 가이드입니다.

시작 전에 [INSTALL.md](INSTALL.ko.md)로 설치를 마치고 서버를 띄우세요:

```bash
# Windows
powershell -ExecutionPolicy Bypass -File scripts\start.ps1
# Linux/macOS
bash scripts/start.sh
```

> 이 튜토리얼의 **버튼/탭 이름은 실제 UI 라벨 그대로**입니다(예: `Compute paths`,
> `Beamforming`, `Analyze`, `Simulate scenario`). Mock 백엔드만으로 전 과정이
> 동작하므로 GPU/Sionna 없이 그대로 따라 할 수 있습니다.

---

## 0. 앱 열기 (1분)

브라우저에서 **http://localhost:5173** 을 엽니다. 상단 툴바 왼쪽에
**SEAM Studio** 타이틀과 프로젝트 셀렉트가 있고, **Sample Demo**
프로젝트가 자동 로드됩니다. (셀렉트에서 `Lab Room`, `FTC Outdoor`로도 전환 가능.)

툴바 오른쪽 상태칩 두 개를 확인하세요:

- **Sionna** / **Mock only** — 레이 트레이싱 백엔드 가용성.
- 제공자 이름 / **AI off** — AI 제안 제공자 상태.

둘 다 Mock/off여도 튜토리얼 전체를 진행할 수 있습니다.

---

## 1. 다섯 가지 모드 둘러보기 (2분)

툴바 가운데 **모드 탭**이 5개 있습니다. 클릭하며 좌/우 패널이 어떻게 바뀌는지
보세요.

| 탭 | 하는 일 |
|---|---|
| **Visual** | 텍스처 3D 씬 궤도(orbit)/이동/줌, 오브젝트 픽킹, 씬 트리, 인스펙터. |
| **RF Materials** | RF 재질별 색 오버레이. 미지정 오브젝트는 경고색(주황)으로 빛남. 드롭다운으로 지정/일괄 지정. |
| **Validation** | 씬 검증 경고(미지정 RF 재질, 시각/RF 모순, 두께 누락, 잘못된 메시 참조 등). |
| **AI Assist** | *Suggest RF materials* 로 재질 후보 제안 → 승인/거절/편집 → *Apply decisions*. |
| **Results** | 경로/라디오맵/빔포밍/채널/궤적/시나리오/ML 데이터셋 등 모든 시뮬레이션. |

**Visual** 모드에서 씬을 한 바퀴 돌려봅니다: 마우스 좌드래그로 궤도 회전, 휠로
줌. 창문(`window_01`)을 클릭하면 인스펙터에 **visual 재질(`blue_glass_pbr`)**과
**RF 재질(unassigned)**이 나란히 표시됩니다.

### 도킹 가능한 패널 (dockable panels)

각 패널 헤더의 오른쪽에 있는 작은 버튼으로 패널 배치를 바꿀 수 있습니다:

- **◧ / ◨** — 패널을 왼쪽/오른쪽 사이드바로 이동(다른 쪽 사이드바로 도킹).
- **⧉** — 패널을 뷰포트 위에 떠 있는 **플로팅 창(float)** 으로 분리. 다시 누르면
  사이드바로 되돌립니다. 플로팅 창은 드래그로 옮길 수 있습니다.

플로팅한 패널은 **모드 탭을 바꿔도 그대로 유지**됩니다 — 예를 들어 Simulation
패널을 띄워 두고 Visual↔Results 를 오가도 계속 떠 있습니다. 좁은 화면에서
필요한 패널만 골라 배치하거나, 결과 테이블을 크게 띄워 보고 싶을 때 유용합니다.

---

## 2. 재질 하나 지정하기 (2분)

1. **RF Materials** 탭으로 전환합니다. 아직 RF 재질이 없는 오브젝트(건물 벽,
   창문 등)가 주황색으로 강조됩니다.
2. 씬에서 **`/buildings/b01/walls`**(건물 1 벽)를 클릭하거나 씬 트리에서 선택합니다.
3. 왼쪽 RF 재질 패널의 드롭다운에서 벽에 맞는 재질(예: `itu_concrete`)을 고릅니다.
4. 지정하면 즉시 오버레이 색이 바뀌고, 인스펙터에 상태가
   **`user_confirmed`**, 소스가 사용자 지정으로 기록됩니다. 값은 프로젝트 폴더에
   영구 저장됩니다.

> 여러 오브젝트를 한 번에 지정하려면 씬 트리에서 다중 선택 후 일괄 지정하세요.

(선택) **AI Assist** 탭에서 *Suggest RF materials (all unassigned)* 를 누르면
규칙 기반 제공자가 후보를 제안합니다(예: `window_01 → itu_glass`, evidence 표시).
각 카드에서 **Approve / Reject / Edit** 후 **Apply decisions (N)** 을 눌러야
비로소 적용됩니다. 아무것도 자동 적용되지 않습니다.

**(선택) 자연어 규칙으로 일괄 지정하기.** 프림 하나씩 제안받는 대신, "창문은 유리,
이름에 concrete 가 들어간 벽은 itu_concrete" 같은 **자연어 지시 한 문장**을 AI Assist
의 규칙 입력란에 넣으면(`POST /api/projects/{id}/ai/generate-rules`) 검토 가능한
**지정 규칙** 목록으로 바뀝니다. 각 규칙은 `이름에 포함될 문자열들 → RF 재질 id`
형태이고(대소문자 무시 부분일치), 프로젝트 라이브러리에 없는 재질을 가리키는 규칙은
경고와 함께 버려집니다(모델이 없는 재질을 만들 수 없음). 규칙을 확인·수정한 뒤
**Apply rules**(`POST …/ai/apply-rules`)를 누르면, 일반 제안과 **완전히 같은
검토·적용 화면**에 매칭된 프림들이 `rule_assigned` 근거로 올라오고, 승인하기 전까지는
씬이 바뀌지 않습니다. 이미 **reject** 한 프림은 그대로 유지되고 다시 제안되지 않습니다.

**(선택) 검증 결과 설명 듣기.** **Validate** 로 나온 경고들이 무슨 뜻이고 무엇을
해야 하는지 헷갈리면 *Explain validation*(`POST …/ai/explain-validation`)을 누르세요.
씬 검증을 돌린 뒤 각 이슈를 평문으로 풀어 설명해 줍니다(예: "프림 3개가 미지정이며,
28 GHz 에서 ITU 지면 재질이 대역 밖이라 `ground_28ghz` 로 바꾸세요"). 씬을 절대
바꾸지 않는 읽기 전용 기능이고, 각 이슈에는 UI 가 원클릭으로 보여 주는
`suggested_actions`(권장 조치)가 함께 붙습니다. AI 서버가 없으면 규칙 기반 제공자가
이슈 코드로 만든 템플릿 설명을 돌려줍니다.

**(선택) 측정값 CSV 불러오기.** 실측 링크 데이터(수신 위치 + 측정 path gain)를
CSV 로 붙여넣어 `POST /api/projects/{id}/calibrate/measurements/import-csv` 로
불러오면 `MeasurementSample` 목록으로 파싱되고(건너뛴 행 수·경고 함께), 이후
`GET …/calibrate/measurements` 로 다시 조회됩니다. 이 측정값이 위의 재질 보정과
RF 판별의 입력이 됩니다.

**(선택) RF 판별 — 측정으로 시각적으로 같은 재질 구분하기.** 유리처럼 눈으로는
똑같아 보여도 RF 관통손실이 크게(mmWave 에서 약 2.5–23.6 dB) 갈리는 재질은
카메라·텍스처 이름만으로 못 가립니다. 후보 재질 몇 개와 측정된 링크별 path gain 을
넣어 `POST /api/projects/{id}/calibrate/disambiguate` 를 호출하면, 각 후보를 해당
프림에 바인딩·재시뮬레이션해 측정과의 RMSE 가 가장 낮은 후보(`best_material_id`)를
돌려줍니다. 후보들의 RMSE 차가 0.05 dB 미만이면 그 위치에서는 구분 불가로 판단해
`best_material_id` 를 비우고 "indistinguishable" 경고를 반환합니다(프림에 더 가까운
측정을 추가하세요). 실제 재질 분리는 Sionna 백엔드에서 이뤄지며, mock 은 흐름
테스트용입니다.

**(선택) 임팩트 평가 — 이 재질이 링크에 얼마나 중요한가.**
`POST /api/projects/{id}/analyze/material-impact` 는 같은 TX→RX 를 지정재질 씬과
단일 기준재질 씬(`baseline_material_id`, 기본 `itu_concrete`)에서 각각 풀어
위치별 **NMSE / 코사인 유사도 / dRSS / 용량(Mbps)** 을 돌려줍니다(KICS 2026). NMSE 가
0 dB 에 가깝고 dRSS 가 크면 그 위치는 재질을 제대로 지정하는 것이 중요하다는 뜻이고,
NMSE 가 매우 낮고 코사인≈1·dRSS≈0 이면 기하/LoS 가 링크를 지배해 재질 영향이 작다는
뜻입니다(`sensitive_nmse_db`, 기본 −60 dB 초과 위치는 material-sensitive 로 표시).

---

## 3. 경로 시뮬레이션 (Simulate paths) (2분)

1. 툴바 오른쪽의 파란 **Simulate paths** 버튼을 누릅니다. (또는 **Results** 모드
   → 오른쪽 **Simulation** 패널 → **Paths** 섹션의 **Compute paths**.)
2. 잠시 후 3D 씬에 TX→RX 레이 폴리라인이 오버레이됩니다. AODT 스타일 범례:
   **LOS 시안 / 반사 마젠타 / 회절 주황**, TX 빨강 / RX 파랑 마커.
3. 아래 결과 테이블에 경로별 **type / power / delay**가 나옵니다. 한 경로를
   클릭하면 정점(vertices)과 상호작용(interaction)이 캐노니컬 prim id·RF 재질로
   매핑되어 표시되고, delay/power 차트가 나타납니다.

**Simulation** 패널의 **Global** 섹션에서 **Backend**(auto/mock/sionna),
**Frequency**(기본 28 GHz), **Seed** 등을 조정할 수 있습니다. **Paths** 섹션에는
**Max depth**, **Samples / it (log 10)** 슬라이더와 메커니즘 체크박스(Line of
sight, Specular reflection, Diffuse reflection, Refraction, Diffraction, Edge
diffraction, Lit-region diffraction)가 있습니다. 레이를 지우려면 **Remove**.

**정확도 프리셋(Preset).** 솔버 노브를 하나씩 만지는 대신, **Preset** 드롭다운에서
대표 배치 시나리오를 고르면 관련 노브가 한 번에 세팅됩니다: **28 GHz Indoor Lab**
(depth 5, 반사+굴절+산란, 격자 0.25 m), **28 GHz Outdoor Campus**(depth 3, 반사+산란,
2 m), **3.5 GHz Urban Macro**(depth 4, 반사+굴절+회절, 5 m), **60 GHz Indoor**(depth 4,
반사+굴절, 0.25 m). 프리셋은 경로 설정과 라디오맵 격자를 함께 바꾸며 백엔드·TX·RX
선택은 건드리지 않습니다. 노브를 손으로 바꾸면 자동으로 **Custom** 으로 전환됩니다.
프리셋은 출발점일 뿐 정답이 아니므로, 실측이 있으면 재질 보정으로 잔차를 줄이세요.

---

## 4. 오버레이 토글 & 라디오맵 (2분)

**오버레이 토글:** 뷰포트 패널에서 표시 옵션(경로/마커/라디오맵 등)을 켜고 끄며
씬이 어떻게 보이는지 확인합니다. 조명/표시를 초기화하려면 뷰포트 패널의
초기화 버튼을 누릅니다.

**라디오맵:** **Results** 모드 → **Simulation** 패널 → **Radio map** 섹션에서
**Compute radio map** 을 누릅니다. 격자 위 수신 세기가 jet 컬러맵으로 표면에
깔립니다. 조정 옵션:

- **Cell size** (m) — 격자 셀 크기,
- **Height** (m) — 측정 평면 높이,
- **Metric** — `path_gain_db`, `rss_dbm`, 또는 `sinr_db`.

지우려면 **Remove**.

**다중 TX SINR·서빙 셀 지도.** 씬에 TX 가 여러 개면 **Metric** 을 `sinr_db` 로 두어
동일채널 간섭이 반영된 참 SINR(`S/(I+N)`) 지도를 그릴 수 있습니다. 이때 결과에는 각
셀의 **서빙 TX**(그 셀에서 가장 센 TX)가 함께 담겨(`serving_tx`), 어느 셀을 어느 기지국이
담당하는지 색으로 구분됩니다(단일 TX 씬에서는 SINR 이 SNR 로 되돌아갑니다).

**영역 정밀화(region refinement).** 관심 영역만 더 촘촘히 보고 싶으면 전체 맵을 다시
풀 필요 없이, 그 영역의 중심·크기(`center_xy`/`size_xy`)를 지정하고 더 작은 **Cell
size** 로 다시 계산하면 됩니다. 계산되지 않은 셀은 값을 지어내지 않고 빈칸으로 둡니다.

**메시 라디오맵(표면 커버리지).** 수평 평면 대신 실제 **벽면·바닥·도로 표면 위**에
커버리지를 칠하려면, 대상 프림을 선택하고 **Mesh radio map**
(`POST /api/projects/{id}/simulate/mesh-radio-map`)을 실행하세요. 각 삼각형 중심에
탐침 수신기를 면 법선 방향으로 살짝 띄워 놓고 풀며, 삼각형이 예산(`max_triangles`,
기본 2000)을 넘으면 k 개마다 하나씩 샘플링합니다. 3D 뷰가 파사드/바닥을 그대로
색으로 덮어 커버리지 사각지대를 표면 위에서 바로 확인할 수 있습니다.

---

## 5. 빔포밍 — 코드북 스윕 (Beamforming) (2분)

1. **Simulation** 패널 **Global** 섹션에서 **Beamforming array**를 설정합니다
   (**TX rows × cols**, **RX rows × cols**; 예: 4 × 4).
2. **Mode**를 **codebook sweep**로 두면 **Sweep start / stop / step** (°) 필드가
   나타납니다. 각도 범위를 스윕하며 최적 빔을 찾습니다. (다른 모드: **TX-MRT**,
   **SVD**.)
3. **Beamforming** 버튼을 누릅니다. (툴바 **Actions ▾ → Beamforming**으로도 가능.)
4. 결과로 TX-MRT / 양단 SVD 빔포밍 이득이 나옵니다(4×4에서 약 12 dB / 약 24 dB
   수준). 코드북 스윕 히트맵으로 각도별 이득 분포를 볼 수 있습니다.

---

## 6. 채널 분석 (Analyze) (1분)

**Results** 모드의 **Channel** 패널에서:

1. **TX**와 **RX**를 고르고 **CFR points**(주파수 응답 표본 수)를 설정합니다.
2. **Analyze** 를 누릅니다.
3. **Link budget**, **CIR(power delay profile)**, **CFR magnitude**, 그리고
   **Path-loss models vs RT**(경로손실 모델 대비 레이 트레이싱) 비교 표가
   나옵니다. 결과를 지우려면 채널 지우기 버튼을 누릅니다.

### Live parameters — 파라미터 즉시 조정 → 자동 재분석

Channel 패널의 **Live parameters** 섹션에서 다음 값을 슬라이더/입력으로 바로
조정할 수 있습니다: **Frequency (GHz)**, **Bandwidth (MHz)**, **TX power (dBm)**,
**Noise figure (dB)**, **SCS (kHz)**(부반송파 간격; 15=LTE, 30=5G FR1 기본,
60/120도 선택). 값을 바꾸면 **현재 TX↔RX 쌍이 이미 분석된 상태일 때 자동으로
재분석**되어(디바운스), Link budget·지표가 즉시 갱신됩니다. SCS 옆에는 현재
대역폭에서의 **N_RB**(자원블록 수 = ⌊BW/(12·SCS)⌋)가 함께 표시됩니다.
**Reset** 버튼은 이 값들을 현재 솔버 설정과 TX 디바이스 값으로 되돌립니다.

### 3GPP 측정 지표 (한 줄 요약)

Link budget에는 RSS·SNR·SINR·경로손실과 함께 3GPP TS 38.215 스타일 측정량이 나옵니다:

- **RSRP** (Reference Signal Received Power): 자원요소(RE)당 평균 수신전력 =
  `RSS − 10log10(N_sc)`. 광대역 RSS를 점유 부반송파에 균등 분배한 값.
- **RSSI** (Received Signal Strength Indicator): 대역 내 총 수신전력 = 신호+간섭+잡음
  선형 합(다중 TX 씬에서는 간섭 전력도 포함).
- **RSRQ** (Reference Signal Received Quality): 링크 품질 = `N_RB·RSRP/RSSI`;
  간섭·잡음 무시 신호지배 상한은 `10log10(1/12) = −10.79 dB`(간섭이 있으면 그 아래).

### 다중 TX 동일채널 간섭 (SINR)

씬에 TX가 여러 개면 **서빙 TX 이외의 모든 TX가 RX에 만드는 레이트레이싱 수신전력의
합**을 동일채널 간섭 `I`로 잡습니다. 이때 Link budget의 **SINR = S/(I+N)** 은
잡음만 쓰는 SNR보다 낮아지고, **간섭 전력**·**간섭원 수(num_interferers)**가 함께
표시되며, RSSI·RSRQ·Shannon 용량도 모두 이 간섭을 반영합니다. 간섭 TX가 없으면
`SINR = SNR`로 되돌아갑니다. 모든 간섭 TX가 같은 자원에서 동시에 송신하는
**풀버퍼(full-buffer) 최악조건** 가정이며(스케줄러/부분부하 모델 없음), 서빙 셀은
TX 선택으로 바꿀 수 있습니다.

---

## 6.5 Metrics dashboard + 논문용 그림 내보내기 (2분)

### Metrics dashboard 패널

**Metrics dashboard** 패널(도킹 가능 — ◧/◨/⧉ 로 사이드바 이동·플로팅)은 마지막
채널 분석의 **모든 지표를 한눈에** 보여줍니다. 상단에 KPI 그리드(RSS/RSRP/RSSI/
RSRQ/경로손실/SNR/Shannon 용량/K-factor/지연확산/도플러/N_RB @ SCS 등, 각 셀에
정의 툴팁), 아래에 **Power-delay profile(CIR)**, **CFR magnitude**, **Doppler
fading envelope**, **Path-loss model 비교** 차트가 깔립니다.

- 모든 그림은 **흰 배경 · Times New Roman(serif) 논문 스타일**로 렌더됩니다.
- 각 차트 프레임에는 **PNG / SVG / CSV export** 버튼이 내장되어, 그림을 그대로
  비트맵/벡터로 저장하거나 원 데이터를 CSV로 뽑을 수 있습니다.
- KPI 표 전체를 `metric,value,unit` CSV로 받는 **export-all** 버튼도 있습니다.

### 뷰포트 내보내기 버튼 — Snapshot vs Render

뷰포트 우측 버튼 두 개로 씬 이미지를 저장합니다:

- **Snapshot**(카메라 아이콘) — **보이는 그대로(WYSIWYG) PNG 저장.** 현재 화면(레이·마커·오버레이
  포함)을 캔버스 전체 해상도로 그대로 내보냅니다. 논문/슬라이드용 스냅샷에 적합.
- **Render**(필름 아이콘) — **Mitsuba 오프라인 렌더.** 물리 기반 경로추적으로 (느리지만) 물리적
  으로 셰이딩된 이미지를 별도로 렌더합니다. 화면에 보이는 실시간 뷰가 아니라
  오프라인 렌더 결과라는 점에 유의하세요.

---

## 7. 디바이스 이동 → 자동 업데이트 (1분)

1. 씬 트리나 뷰포트에서 **`rx_001`**(수신기)을 선택합니다.
2. 인스펙터의 **X / Y / Z** 위치 필드를 편집하고 저장합니다.
3. **Auto update** 체크박스(Paths / Radio map / Beamforming 섹션에 각각 있음)를
   켜 두면, 디바이스를 옮길 때마다 해당 결과가 자동으로 다시 계산됩니다.

> 참고: 뷰포트 내 드래그 기즈모(gizmo)는 로드맵 항목이며, 현재는 인스펙터의
> 위치 필드로 정밀 이동합니다.

---

## 8. 액터 + 시나리오 재생 (Simulate scenario) (1분)

Sample Demo에는 움직이는 **액터**가 있습니다: 도로를 달리는 **차량(car_001)**과
건물 앞을 걷는 **보행자(human_001)**. 각 액터는 자체 RF 형상으로 컴파일되어 프레임
마다 이동합니다.

1. **Results** 모드에서 **Scenario (V2X)** 섹션으로 갑니다.
2. **Num frames**(예: 20), **dt**(s), 필요하면 **Include paths (per frame)**를
   설정합니다.
3. **Simulate scenario** 를 누릅니다.
4. 재생 트랜스포트(▶ / ⏸, 프레임 슬라이더, ⟳ 반복, 속도 0.5×~4×)로 타임라인을
   재생하며, 프레임별 **Link metrics**(RSS / SINR / 경로 수)를 확인합니다.

---

## 9. 궤적 라이브 레이 (Simulate trajectory) (1분)

1. **Results** 모드의 **Trajectory** 섹션에서 이동 구간(**start / end**),
   **num_points**, **dt**를 설정합니다. start/end 는 숫자로 직접 입력하거나,
   **⌖ Pick start → end in viewport** 를 눌러 **뷰포트에서 두 점을 클릭**해
   지정합니다 — 첫 클릭이 start, 둘째 클릭이 end 가 되고, 씬 지오메트리를 보며
   경로를 그리듯 찍을 수 있습니다.
2. 픽킹하는 동안 start→end 를 잇는 **점선 미리보기 선(dashed preview line)** 이
   뷰포트에 그려져 실제 이동 구간을 눈으로 확인할 수 있습니다.
3. (선택) **Follow terrain** 체크박스를 켜면 각 웨이포인트의 z 가 그 아래 지형
   표면에 스냅된 뒤 높이 오프셋만큼 올라갑니다 — 경사 실외 지형에서 RX 높이를
   일정하게 유지할 때 씁니다(실내 씬은 끄세요).
4. **Simulate trajectory** 를 누릅니다.
5. RX가 웨이포인트를 따라 이동하며 지점별 **RSS / path gain / RMS delay
   spread**가 계산되고, ▶로 재생하면 궤적을 따라 레이가 실시간으로 갱신됩니다.
   씬에 TX가 여러 개면 지점별 **interference / SINR = S/(I+N)** 도 함께 나오며
   (서빙 TX 이외 모든 TX의 수신전력이 동일채널 간섭, 풀버퍼 가정), 서빙 셀은
   **serving TX** 로 지정합니다(미지정 시 첫 TX).

(선택) **Global** 섹션의 **Live sync**를 켜면 씬을 2초마다 폴링해 디바이스/액터
위치를 뷰어에 반영합니다.

---

## 10. ML 데이터셋 생성 (Generate dataset) (1분)

1. **Results** 모드의 **ML dataset** 섹션에서 **Name**, **Sampling mode**
   (`random` / `grid` / `trajectory`), **Num samples**, **CFR points**,
   **Height** 등을 설정합니다.
2. **Generate dataset** 를 누릅니다.
3. 생성이 끝나면 데이터셋 목록에 이름/샘플 수/생성 시각/크기와 다운로드 링크
   (**npz**, **json**)가 나타납니다.

### 파일이 저장되는 위치

프로젝트 폴더 아래에 결과물이 쌓입니다(예: `examples/demo_project/sample_demo.sionnatwin/`):

| 산출물 | 경로 |
|---|---|
| ML 데이터셋 | `export/datasets/<dataset_id>/dataset.npz` + `metadata.json` |
| RFData 내보내기 (툴바 **Actions ▾ → Export RFData**) | `export/rfdata/` (scenario_meta, devices, paths, trajectory.csv, radio_map.csv, calibration_points) |
| 경로 결과 | `results/{backend}_paths_{NNN}.json` (예: `results/mock_paths_001.json`, `results/sionna_paths_001.json`) |
| 컴파일된 RF 프로젝션 (**Actions ▾ → Compile RF**) | `rf/generated_scene.xml`, `rf/meshes/`, `rf/compile_manifest.json` |
| AI 제안 로그 | `ai/suggestions.jsonl` |

---

## 다음 단계

- 엔진 버전 교체 (Sionna RT 버전별 paths 솔브): [docs/engines.md](docs/engines.ko.md)
- Sionna 버전별 기능·재질·모델 변천사: [docs/sionna_versions.md](docs/sionna_versions.ko.md)
- RT 정확도의 한계와 완화책(측정 보정, 확산 산란 등): [docs/accuracy.md](docs/accuracy.ko.md)
- 아키텍처 / 씬·프로젝트 포맷: [docs/architecture.md](docs/architecture.ko.md),
  [docs/scene_format.md](docs/scene_format.ko.md)
- RF 재질 라이브러리와 AI 어시스턴트: [docs/rf_materials.md](docs/rf_materials.ko.md),
  [docs/ai_assistant.md](docs/ai_assistant.ko.md)
- 로드맵(메시 라디오맵, 이동성, 측정 보정, 확장 포인트): [docs/roadmap.md](docs/roadmap.md)
