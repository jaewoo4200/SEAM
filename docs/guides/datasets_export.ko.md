# ML 데이터셋과 내보내기

> [English](datasets_export.md) · **한국어**

SEAM Studio는 뷰어로 끝나지 않습니다 — 시뮬레이션한 모든 것을 파일로 꺼낼 수
있습니다: ML 모델 학습용 NumPy `.npz` 그라운드트루스 데이터셋, AODT 뷰어 호환
RFData 번들, 보이는 그대로(WYSIWYG)의 뷰포트 캡처, 차트별 CSV/PNG/SVG
내보내기까지. 이 가이드에서 각 내보내기 경로를 하나씩 살펴봅니다.

여기 나오는 모든 기능은 **Mock 백엔드**만으로도 동작합니다 — Sionna RT가
설치되어 있지 않아도 데이터셋 생성과 모든 내보내기가 끝까지 실행됩니다(값은
mock 솔버가 만들지만, 파이프라인 테스트에는 충분합니다).

---

## 1. ML dataset 패널

데이터셋 생성기는 **ML dataset** 패널에 있습니다(헤더에는 `ML DATASET`으로
표시됩니다). 두 가지 방법으로 열 수 있습니다:

- **Results** 모드 — 오른쪽의 도킹 가능한 카드 중 하나입니다.
- 어느 모드에서든 툴바의 **Panels ▾** 메뉴 — **ML dataset** 행을 클릭하면
  뷰포트 위에 플로팅되고, **◧ / ◨** 버튼으로 좌/우 사이드바에 도킹할 수도
  있습니다. 플로팅한 패널은 모드를 바꿔도 그대로 유지됩니다.

![라디오맵 히트맵 위에 플로팅된 ML DATASET 패널 — 샘플링 컨트롤, 영역 필드, Generate dataset 버튼, npz/json 링크가 있는 기존 데이터셋 행이 보입니다](../images/10_dataset.png)
*계산된 라디오맵 위에 띄운 ML DATASET 패널 — 위쪽에 샘플링 컨트롤, 가운데에 영역 픽커, 아래에 `npz` / `json` 다운로드가 있는 데이터셋 목록이 있습니다.*

### 데이터셋 생성 단계별 안내

1. **Name** — 데이터셋 표시 이름입니다(아래 목록에도 나타납니다).
2. **Sampling mode** — UE 위치를 고르는 방식:
   - `random` — 영역 안 균일 랜덤 위치,
   - `grid` — 영역 위 정규 격자(**Grid spacing** 필드가 추가됩니다, 단위 m),
   - `trajectory` — 직선 start→end 구간 위의 점들.
3. **Actor flight path** — 씬 액터(웨이포인트를 가진 차량·보행자·UAV)의
   저작된 궤적을 따라 샘플링하는 옵션입니다. 여기서 액터를 고르면 아래의
   영역 / start-end 를 **덮어씁니다**; `— none —` 으로 두면 샘플링 모드
   자체의 기하를 사용합니다. 궤적이 있는 액터가 없으면 패널이 알려 주니
   먼저 Visual 모드에서 지정하세요. 필드 아래 힌트에 우선순위가 그대로
   나옵니다: *waypoints > actor flight path > start/end (or region)*.
4. **dt** (s) — 백엔드가 이동 샘플에 붙이는 속도/도플러 라벨의
   유한차분(finite-difference) 시간 간격입니다.
5. **Num samples** — 풀 UE 위치 수(1–20000).
   **CFR points** — 위치당 주파수 응답 표본 수(2–4096).
   **Height** (m) — UE 샘플링 높이.
6. 영역을 설정합니다(`random` / `grid` 모드):
   - **⌖ Pick region in viewport** — 버튼이 `Click 2 corners… (Esc)` 로
     바뀌면, 씬 표면에서 영역의 마주 보는 두 모서리를 클릭하세요. XY 필드가
     채워집니다(Esc 취소).
   - **Fit to scene** — 영역과 높이를 씬 전체를 덮도록 설정합니다(프로젝트를
     열 때 패널이 실제 씬 경계로 자동 시드되므로, 엉뚱한 값에서 시작하는
     일은 드뭅니다).
   - **Region min** / **Region max** — XY 모서리를 숫자로 직접 입력.
   - 힌트 줄을 눈여겨보세요. 예: *"Scene spans [-40.0, -40.0]…[40.0, 40.0] m
     — samples outside it get zero paths."* 기하 밖을 샘플링하는 것이 쓸모없는
     데이터셋이 나오는 1순위 원인입니다.

   `trajectory` 모드에서는 대신 **⌖ Pick path in viewport** 버튼과
   **Start** / **End** XYZ 필드가 나타납니다.
7. **Seed** — 랜덤 샘플링을 재현 가능하게 만듭니다. 논문에는 함께 기록하세요.
8. **Include paths** — 각 샘플의 전체 레이 경로(정점 + 상호작용)를
   `paths.jsonl` 로 추가 덤프합니다. 용량이 크므로 기본은 꺼져 있습니다.
9. **Follow terrain** — 각 샘플의 높이를 그 아래 표면에 스냅한 뒤 높이
   오프셋을 더합니다. 경사 실외 씬에서 쓰고, 실내에서는 끄세요(지붕에
   스냅됩니다).
10. **Generate dataset** 을 누릅니다. 솔버가 위치들을 도는 동안 버튼에
    `Generating…` 이 표시됩니다.

### 데이터셋 목록

생성이 끝난 데이터셋은 버튼 아래 표에 한 행씩 나타납니다:

- **name / # / created / size** — 이름, 샘플 수, 생성 시각, 파일 크기.
- **files** — **npz**(`dataset.npz`, 배열 본체)와 **json**(`metadata.json`,
  설정 에코 + 좌표/단위 규약) 다운로드 링크.
- 이름 옆의 **⚠ N zero-path** 플래그는 N개 샘플에서 경로가 하나도 안 나왔다는
  뜻입니다(UE가 씬 밖이거나 완전히 가려짐) — 영역을 다시 확인하세요.
- **×** 버튼으로 데이터셋을 삭제합니다. 한 번 누르면 **✓?** 로 바뀌고, 다시
  눌러야 확정됩니다(몇 초 뒤 자동 해제).

디스크에서는 프로젝트 폴더의 `export/datasets/<dataset_id>/` 아래에 쌓입니다.

### 라벨에는 무엇이 들어 있나

`.npz` 에는 샘플별 위치, 복소 CFR, 경로별 CIR 이득·지연, LOS 플래그, RSS,
분산 지표가 들어 있습니다 — 정확한 배열 스키마, AODT 필드 매핑, 바로 실행할
수 있는 학습 예제(`examples/ml/train_channel_estimator.py`)는
[ML 그라운드트루스 데이터셋](../ml_datasets.ko.md) 문서에 정리되어 있습니다.

---

## 2. RFData 내보내기 (AODT 뷰어 번들)

결과를 외부 AODT 스타일 뷰어나 자체 파이프라인에 넘기려면 툴바의
**Actions ▾ → Export RFData** 를 사용하세요. 프로젝트 폴더 안
`export/rfdata/` 에 번들이 기록됩니다:

| 파일 | 내용 |
|---|---|
| `scenario_meta.json` | 단위, 주파수, 좌표 변환, 시간 창 |
| `devices.json` | 송신기 + 수신기(위치, m 단위) |
| `paths.json` | 시간 인덱스가 붙은 레이 경로 |
| `trajectory.csv` | 웨이포인트별 UE 지표(`time_s, ue_id, x_m, y_m, z_m, rss_dbm, sinr_db, path_gain_db`) |
| `radio_map.csv` | 평면 히트맵 샘플 |
| `calibration_points.json` | 좌표 검증용 기준점 3개 |

내보내기가 끝나면 **Results** 에 닫을 수 있는 행이 나타납니다 — *"Exported
RFData to `export/rfdata`"* — 파일별 다운로드 링크가 붙어 있어 프로젝트
폴더를 뒤질 필요가 없습니다.

---

## 3. 뷰포트 캡처 — Snapshot 과 Render

뷰포트 우측 하단 버튼 묶음의 아이콘 버튼 두 개가 씬 이미지를 저장합니다:

- **Snapshot**(카메라 아이콘) — 보이는 *그대로*(WYSIWYG) 저장합니다: 현재
  카메라 포즈, 레이, 마커, 라디오맵 오버레이를 캔버스 전체 해상도의 PNG로
  내보냅니다. 툴팁은 *"Save this exact view as a PNG (what you see, full
  resolution — paper-ready)"* 입니다. 논문·슬라이드 그림에는 이 버튼을 쓰세요.
- **Render**(필름 아이콘) — Mitsuba 를 통한 오프라인 물리 기반 경로추적
  렌더입니다. 더 느리고, 의도적으로 화면 뷰와 *다릅니다* — 레이나 오버레이
  없이 셰이딩된 씬만 렌더합니다.

디바이스/액터 시점의 실시간 1인칭 뷰인 **POV 인셋**에도 자체 카메라 버튼이
있어, POV 프레임을 전체 해상도 PNG로 저장할 수 있습니다.

---

## 4. 대시보드의 CSV·그림 내보내기

- **Metrics dashboard** 패널의 모든 차트(그리고 다른 논문 스타일 차트들)는
  헤더에 **PNG / SVG / CSV** 버튼이 있는 프레임 안에 있습니다 — 3× 비트맵,
  벡터, 또는 원 데이터 CSV. 그림은 보이는 그대로(흰 배경, Times New Roman)
  내보내집니다.
- 대시보드 헤더의 **Export all (CSV)** 버튼은 KPI 표 전체를
  `metric,value,unit` 행으로 다운로드합니다.
- **Results** 의 경로 테이블에는 **Export filtered CSV (N)** 이 있습니다 —
  현재 필터된 경로 집합을 그대로, 경로당 한 행(type, power, delay, 상호작용
  재질 포함)으로 내보냅니다.

---

## 관련 문서

- [ML 그라운드트루스 데이터셋](../ml_datasets.ko.md) — `.npz` 스키마,
  zero-path 경고, AODT 필드 매핑, 학습 예제 스크립트.
- [시작하기](getting_started.ko.md) — 설치, 첫 프로젝트, 모드 탭.
- [시뮬레이션 가이드](simulation.ko.md) — 경로, 라디오맵, 그리고 데이터셋이
  물려받는 솔버 설정.
- [씬·프로젝트 포맷](../scene_format.ko.md) — 프로젝트 폴더 안 파일들의 위치.
- [Sionna 버전](../sionna_versions.ko.md) — 재현성을 위해 `metadata.json` 에
  기록되는 `engine`.
- [15분 튜토리얼](../../TUTORIAL.ko.md) — 데이터셋 생성을 포함한 첫 세션
  전체 흐름.
