# ML Ground-Truth Datasets (AODT-style 연구 루프)

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

## 배열 레이아웃 (`dataset.npz`)

| key | shape/dtype | 의미 |
|---|---|---|
| `positions_m` | `[N,3] f32` | UE 위치 (Z-up, m) |
| `tx_position_m` | `[3] f32` | 고정 TX 위치 |
| `cfr` | `[N,K] c64` | 채널 주파수 응답 H(f_k) |
| `cfr_freq_offset_hz` | `[K] f64` | [-B/2, +B/2] 오프셋 |
| `cir_gain` | `[N,P] c64` | 경로별 복소 전압 게인 (0 패딩) |
| `cir_delay_ns` | `[N,P] f32` | 경로별 지연 (NaN 패딩) |
| `num_paths` | `[N] i16` | 유효 경로 수 |
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

## 훈련 예제

`examples/ml/train_channel_estimator.py` — 데이터셋을 로드해 파일럿 기반
채널 추정 태스크를 구성하고, LS 베이스라인(numpy)과 소형 MLP(PyTorch 설치
시)를 비교한다:

```
backend\.venv\Scripts\python.exe examples/ml/train_channel_estimator.py ^
    examples/demo_project/kaist_demo.sionnatwin/export/datasets/<id>/dataset.npz
```

PyTorch가 없으면 LS/LMMSE 베이스라인만 실행된다(`pip install torch`로 활성화).
스크립트 상단의 `PILOT_SPACING`, `SNR_DB` 를 바꿔 실험 조건을 조정한다.

## 재현성 체크리스트

논문에 쓸 때는 metadata.json의 `engine`(sionna-rt 버전 — [sionna_versions.md](sionna_versions.md) 참조),
`config`(메커니즘 플래그·시드·샘플 수), `sampling.seed` 를 함께 보고할 것.
