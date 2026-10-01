# 레이더 센싱: RCS 타깃, 에코 솔브, 도플러

> [English](sensing.md) · **한국어**

SEAM Studio에서는 어떤 액터(차량, 보행자, UAV, 커스텀 객체)든 **레이더 센싱
타깃**으로 지정할 수 있습니다. 지정한 타깃에 대해 Sionna RT의 레이더 단면적
솔버(`sionna.rt.rcs.RCSSolver`, sionna-rt 2.2에서 추가)로 TX → 타깃 → RX 에코
경로를 계산합니다. 에코마다 도플러 편이가 따로 붙으므로, 움직이는 타깃은
뷰포트에서 색으로 구분되는 속도 신호로 나타납니다. sionna-rt 2.2가 없으면 전체
흐름이 **Mock 백엔드**(바이스태틱 레이더 방정식)로 돌아가므로 GPU 없이도 써 볼 수
있습니다.

---

## 1. RCS란

- **레이더 단면적** σ(m², 또는 dBsm = 10·log10(σ / 1 m²))는 실제 타깃과 같은
  전력을 되돌려 보내는 등방성 재방사체의 면적입니다.
- 수신 에코는 바이스태틱 레이더 방정식
  `Pr / Pt = Gt · Gr · λ² · σ / ((4π)³ · d1² · d2²)`을 따릅니다. d1은 TX→타깃,
  d2는 타깃→RX 거리이고, 모노스태틱(RX가 TX 위치)이면 익숙한 `d⁴` 법칙이 됩니다.
- 3GPP **TR 38.901 §7.9**는 여기서 더 나아가, 타깃 유형마다 하나 이상의 산란점을
  두고 각 산란점의 RCS가 입사·산란 방향에 따라 달라지게 합니다(각도별 로브).
  랜덤 성분도 선택할 수 있습니다. Sionna RT 2.2가 이 모델을 구현하며, SEAM은 이를
  액터 단위로 노출합니다.

## 2. 액터 바인딩

액터를 선택하고 **Inspector → Sensing target**을 엽니다. 바인딩은 액터의
`actor.sensing`에 저장되며([../scene_format.ko.md](../scene_format.ko.md) 참고),
`null`이면 타깃이 아닙니다.

**Model**은 둘 중 하나입니다.

- **TR 38.901** (`"tr38901"`) — 아래 3GPP 타깃 모델입니다. `object_type`의
  기본값은 액터 kind에서 정해집니다(car → `vehicle-multi-sp`, human → `human`,
  UAV → `uav-small-size`). `custom` 액터는 직접 골라야 합니다. `model_type`의
  기본값은 해당 타입이 정의하는 가장 높은 모델입니다.
- **Constant RCS** (`"constant"`) — 타깃 중심에 등방성 산란점 하나를 두고
  `rcs_dbsm`(비우면 0 dBsm)과 선택적인 교차편파비 `xpr_db`(비우면 편파 변환 없음)를
  씁니다.

| `object_type` | 모델 타입 | 기본 적용 kind | 평균 모노스태틱 σ_M (dBsm) | 산란점 | TR 38.901 기본 크기 L×W×H (m) |
|---|---|---|---|---|---|
| `uav-small-size` | 1 | uav | −12.81 | 1 | 0.3 × 0.4 × 0.2 |
| `uav-large-size` | 2 | — | −5.85 | 1 | 1.6 × 1.5 × 0.7 |
| `human` | 1, 2 | human | −1.37 | 1 | 0.5 × 0.5 × 1.75 |
| `vehicle-single-sp` | 2 | — | 11.25 | 1 | 5.0 × 2.0 × 1.6 |
| `vehicle-multi-sp` | 2 | car | 11.25 | 5(좌, 뒤, 우, 앞, 지붕) | 5.0 × 2.0 × 1.6 |
| `agv-single-sp` | 2 | — | −4.25 | 1 | 0.5 × 1.0 × 0.5 |
| `agv-multi-sp` | 2 | — | −4.25 | 5 | 0.5 × 1.0 × 0.5 |

`"vehicle"`나 `"agv"`만 쓰면 유효하지 않습니다. Sionna는 단일 산란점 또는 다중
산란점 변형 중 하나를 요구합니다. 타입이 정의하지 않는 모델 타입(예:
`uav-small-size`에 모델 2)은 저장할 때 422로 거부됩니다.

그 밖의 필드(따로 적지 않으면 두 모델 공통):

| 필드 | 의미 |
|---|---|
| `size_m` | 타깃 직육면체 `[length, width, height]` m. 비우면 위 표의 TR 38.901 기본값이 아니라 액터 자신의 박스 크기(`shape.size_m`)를 씁니다. 메시 액터는 직접 지정해야 합니다. |
| `velocity_m_s` | 타깃의 월드 좌표계 속도. 비우면 t = 0에서 액터 궤적의 접선 속도를 쓰고, 정지 액터는 정지 상태입니다. |
| `random_components` | TR 38.901 전용: 방향 쌍마다 σ_S, XPR, 초기 위상을 무작위로 뽑습니다(끄면 결정론적 RCS). |
| `enabled` | 끄면 바인딩은 유지하되 솔브에서 제외합니다. |

타깃 직육면체의 중심은 액터 바닥 위치에 높이의 절반을 더한 지점이고, 방향은
액터의 `[yaw, pitch, roll]`을 따릅니다. 속도를 궤적에서 가져오는 경우(웨이포인트가
있는 액터에서 `velocity_m_s`를 비운 경우)에는 자세도 궤적에서 가져옵니다. 바닥
위치는 t = 0의 궤적 위치, yaw는 진행 방향이 되어 재생(playback) 첫 프레임과
같아지므로, 산란 로브와 도플러가 같은 움직임을 기술합니다. `velocity_m_s`를 직접
지정하면 작성한 자세를 그대로 씁니다. 궤적이 있는 다른 액터들은 솔브에서 작성한
자세에 머물지만 t = 0의 속도로 움직이므로, 그 액터에서 반사되는 구간에도 도플러가
붙습니다.

## 3. 센싱 솔브 실행

TX와 RX를 최소 하나씩 배치합니다. **모노스태틱** 센싱이면 RX를 TX 위치에 두고,
다른 위치의 RX는 **바이스태틱** 구성이 됩니다. Results 모드에서
**Actions ▾ → Sensing solve**를 고르면, 바인딩이 켜진 모든 액터를 대상으로 솔브가
돌고 결과가 `sensing` 결과 세트로 저장됩니다(다른 솔브처럼 실행 이력에 나타남).

같은 솔브를 API로 실행하려면:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/sensing \
  -H "Content-Type: application/json" \
  -d '{"config": {"backend": "auto", "frequency_hz": 28e9}, "include_comm_paths": false}'
curl http://127.0.0.1:8000/api/projects/demo/results/sensing   # 가장 최근 저장 결과
```

| 요청 필드 | 의미 |
|---|---|
| `config_id` / `config` | 솔버 설정. `/simulate/paths`와 똑같습니다. |
| `tx_ids` / `rx_ids` | 설정의 디바이스 선택을 덮어씁니다. |
| `target_actor_ids` | 솔브 대상을 이 액터들로 제한합니다(기본: 켜진 바인딩 전부). |
| `max_depth` | RCS 깊이. **산란 이벤트 자체도 하나로 셉니다.** `1`이면 직접 경로만 있고, 각 구간(leg)은 최대 `max_depth − 1`번 반사될 수 있습니다. 기본값 `max(1, config.max_depth)`. |
| `samples_per_sp` | 산란점 하나당 발사하는 레이 수(기본 1 000 000). |
| `include_comm_paths` | 일반 경로 솔브도 함께 돌려 그 경로들을 에코 뒤에 붙입니다(`target_id` null, id `path_…`). 이 솔브에서는 각 타깃이 흡수체 직육면체로 액터를 대신하므로(Sionna `PathSolver`가 센싱 타깃을 다루는 방식), 통신 경로가 타깃 자신의 메시에서 또 반사되는 일이 없습니다. |

**백엔드.** `sionna`는 `RCSSolver`(LoS, 정반사, 투과 구간)를 실행합니다. `mock`은
타깃 중심의 산란점 하나에 대해 바이스태틱 레이더 방정식을 계산합니다(LoS 구간만,
차폐 없음). `auto`는 sionna-rt 2.2 이상이 설치돼 있으면 sionna를 고르고, 아니면
경고와 함께 mock으로 넘어갑니다. 구버전 sionna-rt에서 `"backend": "sionna"`를
명시하면 **409**(`sensing requires sionna-rt>=2.2`)로 응답합니다. 바인딩된 액터가
없거나, 알 수 없는 액터·디바이스 id를 주거나, TX/RX가 없으면 **400**입니다.

## 4. 도플러 읽기

에코 경로마다 `doppler_hz`가 붙으며, **경로가 짧아지는 중(타깃 접근)이면
양수**입니다. 산란점이 하나일 때 Sionna는 다음과 같이 계산합니다.

`f_D = (v_tx · k̂₁ − v_rx · k̂₂ + v_t · (k̂₂ − k̂₁)) / λ`

k̂₁은 TX → 타깃, k̂₂는 타깃 → RX 단위 벡터입니다. 모노스태틱 레이더에서 속도 v로
멀어지는 타깃은 −2v/λ가 됩니다(28 GHz, 10 m/s에서 −1868 Hz).

뷰포트에서는 Results 패널의 **Show: Sensing**을 체크합니다. 에코 경로가 도플러
색(접근 빨강, 정지 회색, 이탈 파랑)으로 그려지고 범례가 붙으며, 산란점마다 마커가
표시됩니다. **센싱 카드**에는 타깃별 정보(모델, 산란점, σ, 경로 수)와 가장 강한
에코들(전력, 지연, 도플러)이 나옵니다.

## 5. 내보내기

- **RFData** (`POST /export/rfdata`) — 센싱 결과가 있으면
  `export/rfdata/sensing.json`을 씁니다. 타깃 요약과 에코 경로(`type: "SENSING"`,
  `doppler_hz`, `target_id`)가 들어가고, 요약에 `has_sensing`이 표시됩니다.
- **AODT parquet** (`POST /export/aodt`, `"source": "sensing"`) — 저장된 센싱
  결과를 스냅샷 하나로 씁니다. AODT에는 센싱 토큰이 없어 타깃 꼭짓점은
  `"scattering"`으로 기록하고, 그 `object_ids` 항목은 1 000 000 + **액터 id 정렬
  순서**에서의 위치입니다(`targets` 순서가 아님). 액터 id로 되돌릴 때는
  `id_map.json`의 `sensing_targets`를 쓰세요.
- **채널 npz** (`POST /export/channel-npz`, `"include_sensing": true`) — 각 에코를
  RX 위치에 UE가 있고 TX가 여전히 에코의 TX 위치에 있는 UE × TX 링크에 붙인 뒤
  (1 mm 일치) 강한 순으로 정렬합니다. `is_nlos`는 에코를 무시하고,
  `sensing_path_count`가 매칭된 개수를 알려 줍니다. UE별 통신 솔브는
  (`include_comm_paths`처럼) 결과의 타깃을 흡수체로 둡니다. 다른 주파수에서 푼
  센싱 결과는 **400**으로 거부되고, 그 뒤로 TX가 움직인 에코는 경고와 함께
  빠집니다.

## 6. 한계

- 타깃은 강체 평행 이동만 합니다. 회전이나 마이크로 도플러는 없습니다.
- 직육면체 크기는 액터 박스 크기이므로 메시 액터는 `size_m`을 직접 지정해야 합니다.
- 센싱 솔브 동안 액터 자신의 메시는 씬에서 빠졌다가(산란 모델이 그 액터를
  전부 기술하므로) 끝나면 복원됩니다.
- 확산 산란·회절 구간은 없고, 센싱(에코와 `include_comm_paths` 모두)은 항상 내장
  sionna-rt 엔진에서 돕니다.
- mock은 타깃 중심의 LoS 산란점 하나와 σ_M만 씁니다. 각도별 로브, 다중 산란점
  배치, 차폐는 없습니다. Sionna처럼, 같은 위치에 놓인(모노스태틱) TX와 RX 사이에는
  LoS 통신 경로가 없습니다.
- 랜덤 성분은 기본으로 꺼져 있습니다. 솔버가 `deterministic=False`로 돌기 때문에
  같은 시드면 실제로는 재현되지만 Sionna가 보장하지는 않습니다.

## 관련 문서

- [simulation.ko.md](simulation.ko.md) — 경로, 라디오맵, 빔포밍, 채널 분석
- [trajectory_uav.ko.md](trajectory_uav.ko.md) — 액터 궤적(타깃 기본 속도의 출처)
- [datasets_export.ko.md](datasets_export.ko.md) — RFData / AODT / 채널 npz 내보내기
- [../dynamic_scattering.ko.md](../dynamic_scattering.ko.md) — 일반 경로 솔브에서 움직이는 디바이스·액터의 도플러
