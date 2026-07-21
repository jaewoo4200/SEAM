# 시뮬레이션: 경로, 라디오맵, 빔포밍, 채널 분석

> [English](simulation.md) · **한국어**

SEAM Studio의 모든 계산은 **Results** 모드에서 이뤄집니다: 레이 경로,
커버리지 라디오맵, MIMO 빔포밍, 링크별 채널 분석. 이 가이드는 각 솔버와
핵심 노브, 결과를 읽는 법을 차례로 안내합니다. Sionna RT가 설치되어 있지
않아도 전 과정이 **Mock 백엔드**로 동작하므로 GPU 없이 그대로 따라 할 수
있습니다.

---

## 1. Results 모드 화면 구성

툴바에서 **Results** 탭을 클릭하세요. 오른쪽 사이드바에 **Global**,
**Paths**, **Radio map** 세 개의 접이식 섹션을 가진 **Simulation** 패널과,
경로 테이블·오버레이 토글이 있는 **Results** 패널이 놓입니다.
**Channel analysis**, **Metrics dashboard**, **UE trajectory**,
**Scenario playback**, **ML dataset** 패널은 도킹 가능합니다 — 어느 모드에서든
툴바의 **Panels ▾** 메뉴에서 플로팅하거나 좌/우 사이드바로 옮길 수 있습니다.

**Global** 섹션은 모든 솔버(경로/라디오맵/빔포밍/채널)가 공유하는 설정입니다:

| 필드 | 하는 일 |
|---|---|
| **Preset** | 대표 시나리오 설정을 Paths와 Radio map에 한 번에 적용: `28 GHz Indoor Lab`, `28 GHz Outdoor Campus`, `3.5 GHz Urban Macro`, `60 GHz Indoor`, `28 GHz UAV A2G`. 노브를 손으로 바꾸면 `Custom`으로 전환됩니다. |
| **Backend** | `auto` / `mock` / `sionna`. `auto`는 Sionna RT가 설치되어 있으면 사용하고, 없으면 mock 솔버로 대체합니다. |
| **Engine** | 등록된 Sionna RT 버전이 둘 이상일 때만 표시 — paths 솔브를 돌릴 버전을 고릅니다(예: `Sionna RT 2.0.1`). [../engines.ko.md](../engines.ko.md) 참고. |
| **Frequency** (GHz) | 반송파 주파수(기본 28 GHz). |
| **Bandwidth** (MHz) | 잡음·CFR·용량 계산에 쓰는 채널 대역폭. |
| **Noise figure** (dB) | SNR/SINR용 수신기 잡음지수. |
| **Seed** | 난수 시드 — 시드가 같으면 레이도 같습니다. |

Global 섹션에는 **Beamforming array** 설정(4절)과 **Live sync** 토글도
함께 있습니다.

---

## 2. 경로 (Paths)

![3D 씬 위에 TX에서 RX로 그려진 다섯 개의 레이 경로와 오른쪽에 열린 Simulation 패널](../images/06_paths.png)
*경로 솔브 결과: 상호작용 종류별로 색이 다른 TX→RX 폴리라인과 Simulation 패널의 Global·Paths 섹션.*

같은 솔브를 실행하는 버튼이 두 개 있습니다:

- 툴바 오른쪽의 파란 **Simulate paths** 버튼(어느 모드에서든 원클릭),
- Simulation 패널 **Paths** 섹션 안의 **Compute paths**(노브를 먼저
  조정하고 실행할 때).

1. 둘 중 하나를 누르면 토스트가 결과를 알려줍니다(예:
   `Simulated 5 path(s) via sionna backend`).
2. 3D 씬 위에 TX→RX 폴리라인이 오버레이됩니다. 상호작용별 색상:
   **LOS 시안, 반사 마젠타, 회절 주황, 산란 초록**. TX 마커는 빨강,
   RX 마커는 파랑입니다.
3. **Results** 패널의 테이블에 경로가 나열됩니다(**type / dBm / ns / #int**
   열). 행을 클릭하면 정점과 상호작용이 prim id·RF 재질로 매핑되어
   표시되고, **Delay vs power** 산점도와 **AoA / AoD** 플롯이 함께
   나타납니다.

### 정확도 프리셋 (Accuracy)

레이 수를 하나씩 만지는 대신 Paths 섹션의 **Accuracy** 세그먼트 버튼을
쓰세요:

| 프리셋 | Samples/it | Max depth |
|---|---|---|
| **Preview** | 10^5 | 2 |
| **Balanced** | 10^6 | 3 |
| **High** | 5×10^6 | 5 |

**Max depth**(0–12)나 **Samples / it (log 10)**(10^4–10^7) 슬라이더를
직접 움직이면 **Custom**으로 표시됩니다. 그 아래 메커니즘 체크박스
(**Line of sight**, **Specular reflection**, **Diffuse reflection**,
**Refraction**, **Diffraction**, **Edge diffraction**,
**Lit-region diffraction**)로 솔버가 추적할 상호작용을 고릅니다.

### 오버레이 토글과 필터

Results 패널의 **Show:** 줄에서 오버레이를 각각 켜고 끕니다 — **Rays**,
**Radio map**, **Beamforming**, **Trajectory rays**, **Scenario**.
뷰어 컨트롤로 그려지는 경로를 거를 수 있습니다: **Strongest N**(또는
**All**), **Min power**(dBm), **Color by** `type`/`power`/`depth`,
**Line width by power**. 레이 오버레이를 완전히 지우려면 Paths 섹션의
**Remove**를 누르세요. **Auto update**를 켜 두면 씬이 바뀔 때마다 경로가
자동으로 다시 계산됩니다.

---

## 3. 라디오맵 (Radio map)

![jet 컬러맵 커버리지 히트맵과 Path gain 범례, 결과 토스트](../images/07_radio_map.png)
*씬에 깔린 라디오맵: jet 컬러맵, "Path gain" dB 컬러바, 완료 토스트.*

라디오맵은 씬 위에 수신 격자를 깔고 수신 세기를 히트맵으로 칠합니다:

1. Simulation 패널의 **Radio map** 섹션에서 **Compute radio map**을
   누릅니다(Results 패널의 **Simulate radio map**도 동일).
2. **jet** 컬러맵(파랑=약함, 빨강=강함) 히트맵이 나타나고, 뷰포트에
   **Path gain**(dB) — 또는 **RSS**(dBm) — 라벨이 붙은 컬러바 범례가 값
   범위를 보여줍니다.
3. 옵션: **Cell size**(m), **Height**(m), **Metric** — `Path gain (dB)`,
   `RSS (dBm)`, `SINR (multi-TX)`. 섹션에는 별도의 **Accuracy** /
   **Max depth** / **Samples** / 메커니즘 노브도 있습니다.
4. **Remove**로 오버레이를 지웁니다. **Auto update**는 씬 편집 시
   재계산하고, **every** 셀렉트(10 s / 30 s / 1 min / 5 min)는 라이브
   센싱 씬을 위해 고정 주기로 다시 풉니다.

### 셀 맵 vs 메시 맵

위의 맵은 고정 높이의 수평 격자, 즉 **평면 셀 맵**입니다. 커버리지를
**실제 표면**(벽·바닥·도로)에 칠하려면 대상 프림을 선택한 뒤 Results
패널의 **Mesh radio map** 접이식 섹션을 열고 **Run mesh radio map**을
누르세요. 삼각형마다 탐침을 하나씩 풀어 색을 지오메트리 위에 입히고,
전용 **Mesh map** 체크박스로 오버레이를 토글하며, 값 범위는 예를 들어
`Path gain range −95.2 … −61.4 dB (jet: low → high)`처럼 표시됩니다.

---

## 4. 빔포밍 어레이 (Beamforming array)

![빔포밍 결과 토스트와 Global 섹션의 어레이 설정](../images/08_beamforming.png)
*코드북 스윕 결과: Global 섹션의 어레이 설정과 요약 토스트.*

1. **Global** 섹션에서 **Beamforming array**를 설정합니다:
   **TX rows × cols**, **RX rows × cols**(각각 1/2/4/8, 예: 4 × 4).
2. **Mode**를 고릅니다:
   - **codebook sweep** — 양단의 빔 각도를 스윕합니다.
     **Sweep start / Sweep stop / Sweep step**(°) 필드가 나타납니다.
   - **TX-MRT** — TX 측 최대비 전송.
   - **SVD** — 양단 SVD 빔포밍.
3. **Beamforming** 버튼을 누릅니다(Results 패널과 툴바
   **Actions ▾ → Beamforming**에서도 실행 가능).
4. 토스트가 실행 결과를 요약합니다(예:
   `Beamforming 4x4→4x4 (mock) · codebook 25.0 dB · best TX 0° / RX 0°`).
   결과 카드에는 단일 소자 기준값과 이득이, 코드북 스윕에서는 TX/RX
   각도별 이득의 jet 히트맵이 함께 표시됩니다. 버튼 옆 **Auto update**
   체크박스를 켜면 씬이 바뀔 때 자동으로 다시 실행됩니다.

---

## 5. 채널 분석 (Channel analysis)

![링크 버짓 KPI와 CIR/CFR 차트가 있는 플로팅 Channel analysis 패널](../images/09_channel_analysis.png)
*뷰포트 위에 플로팅한 Channel analysis 패널: 링크 버짓, CIR 전력-지연 프로파일, CFR 크기.*

**Channel analysis** 패널은 도킹 가능합니다 — **Panels ▾**에서 열고,
3D 뷰와 나란히 보고 싶으면 플로팅하세요.

1. **TX**와 **RX** 디바이스를 고르고 **CFR points**(대역 내 주파수 표본
   수, 기본 128)를 설정합니다.
2. 필요하면 **Live parameters**를 조정합니다: **Frequency (GHz)**,
   **Bandwidth (MHz)**, **TX power (dBm)**, **Noise figure (dB)**,
   **SCS (kHz)**(15/30/60/120). SCS 옆에는 파생값 **N_RB**(자원블록 수 =
   ⌊BW/(12·SCS)⌋)가 실시간으로 갱신되고, **Reset to config**는 값을 현재
   솔버 설정과 TX 디바이스 값으로 되돌립니다.
3. **Analyze**를 누릅니다. 결과 줄에 TX→RX 쌍이 **fixed link** 배지와
   함께 표시됩니다(현재 위치에서의 스냅샷 — 이동 수신기 스윕은 UE
   trajectory 패널 담당). 백엔드, 주파수, 3D 거리, 경로 수도 함께
   나옵니다.

그 아래:

- **Link budget** — **RSS**, **SNR**, **SINR**, **Interference**(다중 TX
  씬에서는 간섭 TX 수 포함), **Shannon** 용량, **K-factor**, **RMS DS**
  (지연확산), **Coh. BW**, 그리고 시변 채널이면 **Doppler spread**·
  **Coh. time**.
- **CIR (power delay profile)** — 탭마다 경로 종류 색으로 그린 스템 플롯.
- **CFR magnitude** — 대역에 걸친 |H| (dB).
- **Path-loss models vs RT** — 3GPP 모델 예측과 레이 트레이싱 기준 대비
  차이.

한 번 분석한 뒤 Live parameter를 바꾸면 같은 쌍이 자동으로 재분석됩니다
(디바운스). **Auto update** 체크박스는 씬이 바뀔 때마다 마지막 분석을
다시 실행합니다(먼저 수동 **Analyze**가 한 번 필요). **Clear**는 결과를
지웁니다.

접혀 있는 세 개의 확장 섹션도 있습니다: **Channel sweep**(설정 필드
하나를 스윕하며 링크 지표를 차트로 — **Run sweep**), **Doppler-time
spectrogram**(**Compute spectrogram** — 시변 채널의 STFT 히트맵),
**Flight-log validation**(**Validate** — 경로를 따라 실측 vs 예측
path gain 비교).

---

## 6. 백엔드, 결과 보존, stale 표시

- Sionna RT가 없으면 **모든 솔버가 Mock 백엔드로 대체**됩니다(툴바 칩이
  **Mock only**로 표시). mock은 결정적이고 결과 형식이 동일하므로, 어느
  노트북에서든 전체 워크플로를 만들어 시험한 뒤 실 장비에서 **Backend**를
  `sionna`로 바꾸면 됩니다.
- **mock 레이는 물리적으로 정확하지 않습니다.** UI 파이프라인 확인용으로
  고정된 데모 경로(LOS + 지면 반사 + 벽 반사 1개)를 내보낼 뿐, 반사·굴절
  지점을 씬 지오메트리에 대해 실제로 추적하지 않습니다. 레이의 물리적
  타당성은 Sionna 백엔드에서만 판단하세요.
- 결과는 프로젝트별로 보존됩니다: 경로 결과는 프로젝트 폴더의
  `results/{backend}_paths_{NNN}.json`에 쌓이고, 종류별 최신 결과는
  프로젝트를 열 때 함께 로드됩니다.
- Results 패널의 **Run history** 접이식 섹션이 결과 탐색기입니다: 지난
  실행을 종류별로 묶어 보여주고, 클릭 한 번으로 다시 로드하며, 라벨을
  붙이고, 정리할 수 있습니다(**Keep latest** N per kind →
  **Prune old results**; 라벨 붙은 실행은 보존).
- 계산 후 씬이 바뀌면 결과에 **⚠ stale** 배지가 붙습니다 — 오래된 숫자를
  조용히 보여주는 일은 없으니, 다시 실행해 갱신하세요.

---

## 관련 문서

- [시작 가이드](getting_started.ko.md) — 설치, 첫 프로젝트, 모드 둘러보기
- [TUTORIAL](../../TUTORIAL.ko.md) — 15분 첫 세션 전체 흐름
- [엔진](../engines.ko.md) — 다른 Sionna RT 버전으로 paths 솔브
- [Sionna 버전](../sionna_versions.ko.md) — 버전별 기능·재질 변천사
- [정확도](../accuracy.ko.md) — RT 정확도의 한계와 측정 보정
- [ML 데이터셋](../ml_datasets.ko.md) — 솔브 결과를 학습 데이터로
