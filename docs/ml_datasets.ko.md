# ML Ground-Truth Datasets (AODT-style 연구 루프)

> [English](ml_datasets.md) · **한국어**

통신 연구자가 AODT를 쓰는 핵심 이유 — **시뮬레이션 ground truth로 AI 알고리즘
(채널 추정, 빔 예측, LOS 분류, 위치추정)을 훈련·검증** — 를 이 툴에서
그대로 수행한다. UE를 씬 위 위치들로 스윕하면서 레이트레이싱 ground truth를
수집해 NumPy `.npz`로 내보낸다.

## 생성 방법

- **UI**: Results 모드 → "ML dataset" 섹션 → 샘플링(random/grid/trajectory),
  샘플 수, CFR 포인트, 영역 설정 → Generate. 완료 후 목록에서
  `dataset.npz` / `metadata.json` 다운로드.
- **API**: `POST /api/projects/{pid}/datasets/generate`
  (스키마: `backend/app/schemas/datasets.py`). 백엔드는 mock(GPU 불필요,
  테스트용)과 sionna(실물, `engine` 필드로 sionna-rt 버전 선택 가능) 모두 지원.
- 파일 위치: `<project>/export/datasets/<dataset_id>/`.

## 샘플링 영역 설정

- **씬 경계에서 자동 시드**: 샘플링 영역(`region_min`/`region_max`)을 비워 두면
  더 이상 ±25 m 를 추측하지 않고 **실제 씬의 AABB**(`GET
  /api/projects/{pid}/scene/bounds`, 서비스: `scene_bounds.compute_scene_bounds`)
  를 사용한다. AABB 는 시각 GLB 의 월드 경계에 디바이스/액터 위치까지 병합한
  값이라, 작은 실내 씬에서 영역이 통째로 지오메트리 밖으로 벗어나던
  all-zero 데이터셋 문제(감사 F3)를 없앤다. UI 는 이 경계를 미리 채워 준다.
- **Fit to scene**: dataset 패널의 *Fit to scene* 버튼은 위 경계 API 를 호출해
  `region_min`/`region_max` 를 씬 전체로 채운다.
- **Pick region in viewport**: *Pick region in viewport* 는 뷰포트에서 두
  점을 클릭해(대각 코너) 영역을 지정한다 — 클릭한 XY 로 AABB 를 만들고 z 는
  `height_m` 로 둔다. 씬 지오메트리를 보면서 관심 영역만 좁혀 잡을 때 쓴다.
- **Follow terrain**: `sampling.follow_terrain=true` 이면 각 샘플 위치의 z 를
  그 아래 지형 표면(시각 메시로 수직 레이캐스트)에 스냅한 뒤 `height_m` 를
  더한다(서비스: `terrain.snap_to_terrain`). 경사 지형(예: FTC 실외)에서 안테나
  높이를 일정하게 유지할 때 켠다. **실내 씬은 끄는 게 맞다** — 지붕/여러 층이
  있으면 가장 높은 히트(지붕)에 스냅된다. 메시 밖(발밑에 표면 없음)인 점은 z 를
  그대로 두고 요약 경고 하나를 남긴다.

## 배열 레이아웃 (`dataset.npz`)

| key | shape/dtype | 의미 |
|---|---|---|
| `positions_m` | `[N,3] f32` | UE 위치 (Z-up, m) |
| `tx_position_m` | `[3] f32` | 고정 TX 위치 |
| `cfr` | `[N,K] c64` | 채널 주파수 응답 H(f_k) |
| `cfr_freq_offset_hz` | `[K] f64` | [-B/2, +B/2] 오프셋 |
| `cir_gain` | `[N,P] c64` | 경로별 복소 전압 게인 (0 패딩) |
| `cir_delay_ns` | `[N,P] f32` | 경로별 지연 (NaN 패딩) |
| `num_paths` | `[N] i32` | 유효 경로 수 |
| `los` | `[N] bool` | LOS 경로 존재 여부 (분류 라벨) |
| `rss_dbm` | `[N] f32` | 총 수신 전력 |
| `mean_delay_ns`, `rms_delay_spread_ns`, `k_factor_db` | `[N] f32` | 분산/라이시안 지표 (NaN=미정의) |

`H(f_k) = Σ_l g_l·exp(-j2πf_k·τ_l)` — 인터랙티브 채널 분석 패널과 동일한 탭
모델이라 **패널에서 본 값과 데이터셋 샘플이 구성상 일치**한다.
`metadata.json`에는 config 에코, 백엔드/엔진(sionna-rt 버전), 샘플링 스펙,
좌표/단위 규약, 그리고 **AODT ClickHouse 스키마(cirs/cfrs/raypaths) 필드
매핑**(`aodt_field_map`)이 기록된다. AODT와 필드 의미를 맞춰 두었으므로
(cir_re/cir_im ↔ cir_gain 실부/허부 등) AODT용 파이프라인 재사용이 쉽다.
안테나 엘리먼트별 텐서(`[N_time, N_tx_ant, N_rx_ant, N_freq]`, AODT
`ru_ant_el/ue_ant_el` 구조)는 로드맵이다 — 현재는 링크당(안테나 축 집약)
ground truth를 내보낸다.

## 경로 0개(zero-path) 경고

경로가 하나도 없는 샘플로 가득 찬 데이터셋은 **성공처럼 보이지만(200 + 파일
생성) 사실상 쓰레기**다 — 대개 샘플링 영역이 씬 지오메트리 밖으로 벗어난 것이
원인이다. 생성기는 이를 감지해 크게 경고한다:

- **전부 0개**: `ALL {n} samples produced zero paths — ...` 경고를 남기고,
  scene bounds(`GET /scene/bounds` 또는 UI 의 *Fit to scene*)로 영역을 다시
  잡아 재생성하라고 안내한다.
- **일부 0개**: `{k}/{n} samples produced zero paths (...)` — 해당 샘플의
  cfr/라벨은 0/NaN 이다.

두 경우 모두 `metadata.json` 과 `DatasetInfo.metadata` 에
**`num_zero_path_samples`**(0-경로 샘플 개수)가 기록되므로, 파이프라인에서
프로그램적으로 검사해 나쁜 데이터셋을 걸러낼 수 있다.

## 훈련 예제

`examples/ml/train_channel_estimator.py` — 데이터셋을 로드해 파일럿 기반
채널 추정 태스크를 구성하고, LS 베이스라인(numpy)과 소형 MLP(PyTorch 설치
시)를 비교한다:

```powershell
backend\.venv\Scripts\python.exe examples/ml/train_channel_estimator.py examples/demo_project/sample_demo.seam/export/datasets/<dataset_id>/dataset.npz
```

`<dataset_id>` 는 UI(Results 모드의 ML dataset 목록) 또는 생성된
`metadata.json` 에 표시된 실제 데이터셋 id 로 바꿔서 실행한다.

PyTorch가 없으면 LS/LMMSE 베이스라인만 실행된다(`pip install torch`로 활성화).
스크립트 상단의 `PILOT_SPACING`, `SNR_DB` 를 바꿔 실험 조건을 조정한다.

## 재현성 체크리스트

논문에 쓸 때는 metadata.json의 `engine`(sionna-rt 버전 — [sionna_versions.md](sionna_versions.ko.md) 참조),
`config`(메커니즘 플래그·시드·샘플 수), `sampling.seed` 를 함께 보고할 것.
