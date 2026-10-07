# 레이더 센싱: RCS 타깃, 에코 솔브, 도플러

> [English](sensing.md) · **한국어**

SEAM Studio에서는 어떤 액터(차량, 보행자, UAV, 커스텀 객체)든 **레이더 센싱
타깃**으로 지정할 수 있습니다. 지정한 타깃에 대해 Sionna RT의 레이더 단면적
솔버(`sionna.rt.rcs.RCSSolver`, sionna-rt 2.2에서 추가)로 TX → 타깃 → RX 에코
경로를 계산합니다. 에코마다 도플러 편이가 따로 붙으므로, 움직이는 타깃은
뷰포트에서 색으로 구분되는 속도 신호로 나타납니다. sionna-rt 2.2가 없으면 전체
흐름이 **Mock 백엔드**(바이스태틱 레이더 방정식)로 돌아가므로 GPU 없이도 써 볼 수
있습니다.

**Sample Demo**(v0.1.14 이상)는 바로 센싱을 해 볼 수 있게 되어 있습니다. 드론 타깃
(`uav_001`, TR 38.901 `uav-small-size`, 40 m 높이에서 10 m/s로 90 m짜리 L자 경로를
난 뒤 제자리 비행)과 옥상 TX와 같은 위치의 센싱 수신기(`tx_001_rx`, "TX 1 sensing RX")가
들어 있어서, 아래 API 예제는 `sample_demo`에서 그대로 돌아갑니다. v0.1.15부터는
가로등 마스트 위의 센싱 사이트 두 개(`tx_002`와 `tx_002_rx`, `tx_003`와 `tx_003_rx`)와
두 번째 저장 구성 `sensing_fr1`(3.5 GHz, 20 MHz)도 들어 있어서, 6절의 시나리오가 별도
설정 없이 드론을 탐지하고 추적합니다. 이전 버전이 만든 프로젝트는 디바이스·액터·구성이
그대로입니다(업그레이드가 프로젝트를 고쳐 쓰지는 않습니다). 지금의 데모를 쓰려면 옆에 새
데모를 만들고, 페이지를 새로 고친 뒤 프로젝트 셀렉트에서 **Sample Demo v2**를 고르세요
(API 예제에서는 `sample_demo` 대신 `sample_demo_v2`).

```bash
curl -X POST http://127.0.0.1:8000/api/projects -H "Content-Type: application/json" \
  -d '{"name": "Sample Demo v2", "template": "demo", "project_id": "sample_demo_v2"}'
```

소스 체크아웃이라면 `projects/sample_demo.seam`을 지우고 다시 시작해도 됩니다. 추가된
내용을 모두 담아 다시 복사됩니다. 아니면 내 프로젝트에 타깃과 같은 위치의 RX를 직접
추가하세요(2절, 3절).

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
붙습니다. 일반 경로 솔브도 같은 규칙을 따릅니다(4절).

## 3. 센싱 솔브 실행

TX와 RX를 최소 하나씩 배치합니다. **모노스태틱** 센싱이면 RX를 TX 위치에 두고,
다른 위치의 RX는 **바이스태틱** 구성이 됩니다. Results 모드에서
**Actions ▾ → Sensing solve**를 고르면, 바인딩이 켜진 모든 액터를 대상으로 솔브가
돌고 결과가 `sensing` 결과 세트로 저장됩니다(다른 솔브처럼 실행 이력에 나타남).

같은 솔브를 API로 실행하려면:

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/sensing \
  -H "Content-Type: application/json" \
  -d '{"config": {"backend": "auto", "frequency_hz": 28e9}, "include_comm_paths": false}'
curl http://127.0.0.1:8000/api/projects/sample_demo/results/sensing   # 가장 최근 저장 결과
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
차폐 없음). 여기에 디바이스마다 타깃 쪽 소자 이득과 §8의 구간별 편파 항을 더합니다
(v0.1.14부터. 그 전의 mock 에코는 등방성이었습니다). `auto`는 sionna-rt 2.2 이상이
설치돼 있고 Dr.Jit 백엔드가 동작하면(CUDA GPU, 또는 CPU의 LLVM.
[INSTALL](../../INSTALL.ko.md#실제-sionna-rt-엔진-자동-설치됨) 참고) sionna를 고르고,
아니면 경고와 함께 mock으로 넘어갑니다. 구버전 sionna-rt에서 `"backend": "sionna"`를
명시하면 **409**(`sensing requires sionna-rt>=2.2`)로 응답합니다. 바인딩된 액터가
없거나, 알 수 없는 액터·디바이스 id를 주거나, TX/RX가 없으면 **400**입니다.

**안테나.** Sionna는 처음 선택한 TX(RX)의 안테나를 모든 TX(RX)에 적용하고, 안테나가
서로 다르면 경고합니다. mock은 디바이스마다 자기 소자 패턴과 방향(그리고 §8의 편파 항)을
적용합니다.

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

**통신 경로.** Sionna 백엔드에서는 경로 솔브도 액터에 속도를 줍니다. 그래서
TX와 RX가 정지해 있어도, 움직이는 액터에서 반사되는 통신 경로에는 도플러 편이가
생깁니다. 속도 v로 움직이는 액터에서 한 번 반사될 때마다
`v · (k̂_out − k̂_in) / λ`가 더해지는데, k̂_in은 반사점으로 들어오는 방향, k̂_out은
반사점에서 나가는 방향의 단위 벡터입니다. 예를 들어 8 m 떨어진 정지 TX/RX 쌍에서
20 m 앞에 있는 차량이 10 m/s로 멀어지면, 3.5 GHz에서 차량 면 반사 경로는 약
−228 Hz, LoS와 지면 반사 경로는 0 Hz가 됩니다. 액터가 받는 속도는 다음과 같습니다.

- **Simulate scenario**: 프레임마다 그 시점의 액터 속도.
- **그 밖의 모든 경로 솔브**(Simulate paths, 채널 분석, 데이터셋, UE 궤적, GT 재생,
  `include_comm_paths`): t = 0의 궤적 속도. 이 솔브들은 단계 사이에 액터를
  움직이지 않으므로, 실행 내내 액터가 작성한 자세에 머뭅니다.
- **궤적이 없는 액터**는 정지 상태입니다.
- **데이터셋이 경로를 샘플링하는 액터**(`sampling.actor_id`)도 정지 상태입니다.
  UE는 그 경로를 따라 움직이지만, 액터의 메시는 작성한 자세에 세워 둔 채로
  남습니다.

움직이는 액터에 붙은 디바이스(`attached_device_ids`)는 위의 모든 솔브와 센싱
솔브에서 액터와 같은 속도로 함께 움직입니다. Simulate scenario가 아닌 솔브에서는
디바이스에 직접 지정한 `velocity_m_s`가 우선합니다. 그래서 차량에 탄 UE는 LoS를
포함한 모든 경로에 도플러 편이가 생깁니다.

일반 솔브는 이 도플러를 `paths`와 순서가 맞는 리스트 `metadata.doppler_hz`로
알려 줍니다. 이 리스트는 TX나 RX에 속도가 있거나 씬의 액터가 하나라도 t = 0에
움직이면 붙습니다. 그 액터에 닿는 경로가 없어도 붙으며, 이때 경로들은 0 Hz입니다.
전부 정지한 솔브에는 예전처럼 도플러 필드가 없습니다. 센싱 결과의 통신 경로에는 경로마다
`doppler_hz`로도 붙습니다. mock 백엔드는 경로 솔브에 도플러가 없고, 다른
`engine`(서브프로세스 워커)은 속도를 적용하지 않습니다.

## 5. 내보내기

- **RFData** (`POST /export/rfdata`) — 센싱 결과가 있으면
  `export/rfdata/sensing.json`을 씁니다. 타깃 요약과 에코 경로(`type: "SENSING"`,
  `doppler_hz`, `target_id`)가 들어가고, 요약에 `has_sensing`이 표시됩니다.
- **AODT parquet** (`POST /export/aodt`, `"source": "sensing"`) — 저장된 센싱
  결과를 스냅샷 하나로 씁니다. 각 행은 `"emission"` … `"reception"`으로
  이어집니다. AODT에는 센싱 토큰이 없어 타깃 꼭짓점은 AODT 문서의 확산 산란
  토큰인 `"diffuse"`로 기록하고, 그 `object_ids` 항목은 1 000 000 + **액터 id 정렬
  순서**에서의 위치입니다(`targets` 순서가 아님). 액터 id로 되돌릴 때는
  `id_map.json`의 `sensing_targets`를 쓰세요.
- **채널 npz** (`POST /export/channel-npz`, `"include_sensing": true`) — 각 에코를
  RX 위치에 UE가 있고 TX가 여전히 에코의 TX 위치에 있는 UE × TX 링크에 붙인 뒤
  (1 mm 일치) 강한 순으로 정렬합니다. `is_nlos`는 에코를 무시하고,
  `sensing_path_count`가 매칭된 개수를 알려 줍니다. UE별 통신 솔브는
  (`include_comm_paths`처럼) 결과의 타깃을 흡수체로 둡니다. 다른 주파수에서 푼
  센싱 결과는 **400**으로 거부되고, 그 뒤로 TX가 움직인 에코는 경고와 함께
  빠집니다.
- **센싱 데이터셋** (`POST /export/sensing-dataset`) — 시나리오 결과의 센싱
  프레임을 links와 echoes 표로 펼칩니다(§10).

## 6. 시간에 따른 센싱: 탐지와 다중 스태틱 융합

센싱 솔브 한 번은 스냅샷 하나입니다. 센싱을 켠 **Simulate scenario**는 **프레임마다**
그 프레임의 액터 자세·속도(와 액터에 탄 디바이스 위치)로 센싱 솔브를 돌립니다.
에코는 링크별 탐지로 바뀌고, 탐지된 링크들을 융합해 위치·속도를 추정한 뒤 실제
액터 궤적과 비교해 오차를 냅니다.

**실행.** Results 모드에서 **Scenario playback**을 열고 *Include paths* 아래의
**Sensing (ISAC)**를 체크한 뒤 임계값, CPI, 적분 펄스 수(v0.1.15부터 폼 기본값 4096)와
**Sensing receivers**(기본 **Auto**, 아래 참고)를 정하고 실행합니다. 센싱 바인딩이
켜진 액터가 하나도 없으면 체크박스가 비활성화됩니다. 폼은 Simulation 패널의 구성으로
푸는데, 프로젝트를 열 때 이 구성은 첫 번째 저장 구성(Sample Demo에서는 `default`)에서
옵니다. 그래서 아래 데모 실행을 폼에서 하려면 먼저 그 패널의 **Preset** 드롭다운에서 저장
구성 **Sensing demo (3.5 GHz)** 를 고르세요. 그러면 `sensing_fr1`이 그대로 쓰이고, 결과의
`simulation_config_id`도 아래 API 호출처럼 `sensing_fr1`이 됩니다. API에서는 시나리오
요청에 `sensing` 블록을 더합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/scenario \
  -H "Content-Type: application/json" \
  -d '{"config_id": "sensing_fr1", "num_frames": 19, "dt_s": 0.5, "include_paths": false,
       "sensing": {"enabled": true, "threshold_db": 13, "cpi_s": 0.01,
                   "cpi_pulses": 4096, "measurement_noise": true,
                   "tracking": {"enabled": true}}}'
```

v0.1.15 이전에 만든 Sample Demo에는 사이트가 하나뿐이고 `sensing_fr1`도 없습니다(이 요청은
404를 돌려주고, Preset 드롭다운에 저장 구성이 나오지 않습니다). 도입부처럼 새 데모를 만들거나,
사이트 두 개를 직접 추가한 뒤 `"config_id": "sensing_fr1"`을 인라인
`"config": {"frequency_hz": 3.5e9, "bandwidth_hz": 2e7, "noise_figure_db": 7, "max_depth": 2}`로
바꾸세요.

Sample Demo에서는 이것으로 드론의 9초 비행을 덮습니다. 데모에는 TRP가 세 개 있고, 각각
30 dBm iso TX와 같은 위치의 센싱 RX로 이루어집니다. 건물 b01 옥상 (−9, 7, 10.5) m의
`tx_001`, 가로등 마스트 위 (30, −30, 10) m의 `tx_002`와 (35, 35, 10) m의 `tx_003`입니다.
Auto가 이 세 RX를 레이더 수신기로 잡으므로 프레임마다 링크가 9개(모노스태틱 3, 바이스태틱
6) 생기고, 서로 다른 기하는 6개입니다. 예제는 데모의 두 번째 저장 구성 `sensing_fr1`(3.5 GHz,
20 MHz, NF 7 dB, 최대 깊이 2)을 씁니다. 28 GHz / 100 MHz에서는 소자 하나로 받는 드론
에코가 4096 펄스(+36 dB)를 적분해도 13 dB 임계값에 못 미칩니다. λ²에서 18 dB를 잃고(아래
*FR1 대 mmWave*), 대역폭이 5배라 잡음도 7 dB 더 들어오기 때문입니다. 3.5 GHz에서는 비행
내내 모든 링크가 15–33 dB이고, 건물과 나무에 RF 재질이 있든 없든 모든 프레임의 모든 링크에
직접 에코가 있습니다. 측정값(잡음 시드 0):

| `metadata.sensing.targets.uav_001` | mock | sionna (sionna-rt 2.2.0, CUDA) |
|---|---|---|
| `detection_rate` | 0.947 (19프레임 중 18) | 0.947 (19프레임 중 18) |
| `link_detection_rate` | 0.439 | 0.439 |
| `frames_ge3_links` | 15 | 15 |
| `ok_frames` (융합) | 10 | 10 |
| `median_position_error_m` (융합) | 0.43 m | 0.79 m |
| `median_gdop` | 1.62 | 1.62 |
| `tracked_frames` / `lost_frames` | 19 / 0 | 19 / 0 |
| `median_track_position_error_m` (EKF) | 0.36 m | 0.38 m |
| `p90_track_position_error_m` | 2.89 m | 2.98 m |
| `median_track_velocity_error_m_s` | 0.66 m/s | 0.66 m/s |

놓친 링크는 모두 `mti_rejected`입니다. 3.5 GHz에서 100 Hz 노치는 모노스태틱 링크의 시선
방향 속도 4.28 m/s 미만을 보지 못하는데, 드론 속도 10 m/s에 비하면 작지 않은 몫입니다.
t = 9 s에는 드론이 제자리 비행이라 9개 링크가 모두 걸러집니다(탐지가 없는 유일한
프레임). t = 1.5–3 s에는 TX 2와 TX 3 사이의 링크만 남고(t = 5 s에는 TX 1과 TX 3 사이),
사이트가 둘뿐이라 융합은 `diverged`를 냅니다(*융합* 참고). t = 4–4.5 s에는 TX 1의
모노스태틱 링크 하나만 남습니다. 이런 프레임은 EKF가 트랙을 이어 갑니다
(`frames_track_without_fusion` = 9). 트랙은 t = 0에 7.6 m 빗나간 링크 5개짜리 융합에서
시작하고(GDOP 2.8: 드론이 세 사이트가 이루는 삼각형 밖에서 출발), t = 1.5–3 s에는
1.3–2.5 m, t = 3.5 s부터는 0.4 m 오차를 보이며, 모서리(t = 5.5 s, `init`)에서 다시 시작한
뒤 두 번째 구간 내내 0.1–0.4 m 안에 머뭅니다.

이 가이드의 다른 예제는 `"config_id": "default"`(28 GHz)를 그대로 씁니다. 이 구성에서
4096 펄스로는 가장 좋은 드론 링크가 7.7 dB에 그쳐 아무것도 탐지하지 못하지만, 10 ms CPI
전체인 B · cpi_s = 10⁶ 샘플(+60 dB)을 적분하면 드론을 탐지합니다. 위 요청에서
`"config_id": "default"`, `"cpi_pulses": 1000000`으로 바꾸면 Sionna에서 19프레임 중
18프레임을 탐지하고, `ok` 융합 18회, 융합 오차 중앙값 0.11 m, 트랙 오차 중앙값 0.08 m가
나옵니다(100 MHz의 거리 셀은 15 m가 아니라 1.5 m). 그 대역폭에서 10 ms CPI가 적분할 수
있는 최대가 10⁶입니다.

결과는 평범한 `scenario` 결과입니다. 프레임마다 `sensing: {echoes, links, estimates}`가
붙고, 실행 요약은 `metadata.sensing`에 들어갑니다. `sensing`이 없거나
`enabled: false`이면 결과는 예전과 같습니다(프레임의 `sensing`은 `null`). 통신과
센싱은 백엔드 하나를 함께 씁니다. `auto`는 다른 시나리오와 똑같이 정해지고, 센싱이
없는 백엔드(sionna-rt 2.2 미만)는 mock으로 넘어가지 않고 **409**로 응답합니다. TX나
RX가 없으면 **400**이고, 바인딩된 액터가 없거나 `target_actor_ids`에 모르는 id가
있거나 `sensing_rx_ids`에 선택된 RX가 아닌 id가 있어도 **400**입니다. 실행 중에는
프로젝트별 솔브 잠금을 잡고 프레임마다 진행률을 알리며, **Cancel**도 됩니다.

센싱을 켠 시나리오는 `include_paths`와 상관없이 모든 프레임의 에코를 저장합니다.
sionna에서 링크 16개면 프레임당 약 45 KB(61프레임에 약 2.6 MB, 1 000프레임에 약
45 MB)이고, 브라우저도 이를 불러옵니다. (v0.1.14부터 결과를 압축 JSON으로 저장합니다.
이전 버전의 들여쓰기 파일은 이 크기의 약 두 배였습니다.)

| `sensing` 필드 | 기본값 | 의미 |
|---|---|---|
| `enabled` | `false` | 프레임별 센싱 솔브를 돌립니다. |
| `threshold_db` | 13 | 적분 후 SNR의 탐지 임계값(dB, −30…60). |
| `cpi_s` | 0.01 | 코히어런트 처리 구간(CPI, 초). 도플러 분해능 = 1 / CPI. |
| `cpi_pulses` | 1 (폼: 4096) | 코히어런트하게 적분하는 펄스 수 또는 OFDM 자원 요소 수. 이득 = 10·log10(N). |
| `mti_min_doppler_hz` | `null` | MTI 노치. \|f_D − f_nodes\|가 이보다 작으면 버립니다(TX/RX가 정지해 있으면 f_nodes = 0, 아래 MTI 참고). `null` = 1 / CPI(도플러 셀 하나), `0`이면 MTI를 끕니다. |
| `include_comm_paths` | `false` | 프레임마다 타깃을 흡수체로 둔 통신 경로도 풀어 `echoes` 뒤에 붙입니다(`target_id` null). 프레임당 솔브가 하나 늘어납니다. |
| `target_actor_ids` | `null` | 이 액터들만 봅니다(기본: 켜진 바인딩 전부). |
| `sensing_rx_ids` | `null` | 바이스태틱 레이더 수신기. `null` = 자동: 선택한 TX에서 1 m 안에 있는 선택된 RX(§7/§8과 같음), 없으면 선택된 RX 전부(v0.1.13). 통신 링크는 여전히 모든 RX를 덮습니다. 모르는 id는 400. |
| `samples_per_sp` / `max_depth` | 1 000 000 / `null` | 프레임별 센싱 솔브에 그대로 넘깁니다(§3과 같음). |
| `measurement_noise` | `false` | 융합에 들어가는 거리·도플러에 가우스 잡음을 더합니다(아래). |
| `noise_seed` | 0 | 그 잡음의 시드. 시드가 같으면 숫자도 같습니다. |
| `tracking` | `null` | 프레임에 걸친 EKF 추적(아래 *추적(EKF)* 참고). `{"enabled": true}`에 `process_accel_sigma_m_s2`(2), `gate_chi2`(16), `coast_max_frames`(10), `max_position_std_m`(25), `use_as_prior`(`true`)를 골라 더합니다. `null`이나 `enabled: false`면 추적하지 않습니다. |
| `pfa` | `null` | 링크별 `pd`의 오경보 확률(§9). `null`이면 링크 보고에 Pd가 붙지 않습니다. |
| `detector` | Swerling 1, 몬테카를로 없음 | 그 `pd`의 모델, 그리고 에코가 있는 링크마다 몬테카를로 `pd_mc`를 낼 `monte_carlo_trials`(§9, `pfa` 필요). 탐지 판정 자체는 그대로 `SNR ≥ threshold_db`입니다. |

**센싱 수신기.** 레이더 링크는 센싱 수신기만 이루고, 선택된 RX는 모두 같은 프레임에서
통신 링크를 그대로 유지합니다. 폼에서 **Auto**(기본)를 두면 아무것도 보내지 않고, 목록에서
디바이스를 고르면 `sensing_rx_ids`를 보냅니다. Auto는 §7/§8의 규칙입니다. 선택한 TX에서
1 m 안에 있는 선택된 RX(모노스태틱/TRP 수신기)가 하나라도 있으면 그것들, 없으면 선택된 RX
전부입니다. 정해진 목록과 규칙(`explicit`, `colocated`, `all_rx`)은
`metadata.sensing.sensing_rx_ids`와 `sensing_rx_rule`에 저장됩니다. **v0.1.14:** 같은
위치의 RX가 있는 프로젝트는 이제 기본으로 그 RX만 레이더 수신기로 씁니다. 예전에는 선택된
RX 전부(UE 포함)가 바이스태틱 레이더 수신기였습니다. 같은 위치의 RX가 없는 프로젝트는
v0.1.13과 똑같이 돕니다.

**탐지.** 링크(TX → 센싱 RX)와 타깃마다 가장 강한 **직접** 에코(TX → 산란점 → RX,
다른 반사 없음)를 보고합니다. 직접 에코가 없으면 종류와 상관없이 가장 강한 에코를
보고하고 `multipath`로 표시합니다. SNR은 다음과 같습니다.

`SNR = P_echo − (−174 + 10·log10(B) + NF) + 10·log10(cpi_pulses)`  [dB]

B는 `config.bandwidth_hz`, NF는 `config.noise_figure_db`입니다. `cpi_pulses`는
CPI 동안 코히어런트하게 적분하는 샘플 수로, 레이더 펄스나 OFDM 자원 요소(부반송파 ×
심볼)를 뜻합니다. CPI 전체를 써도 `B · cpi_s`가 상한입니다(20 MHz × 10 ms = 2·10⁵,
+53 dB). `SNR ≥ threshold_db`이고 `|f_D − f_nodes| ≥ mti_min_doppler_hz`(아래)이면
그 링크는 **탐지**됩니다.

**MTI.** 노치 기본값은 도플러 셀 하나, 즉 1 / CPI(10 ms에서 100 Hz)입니다. 그래서
모노스태틱 **블라인드 속도** λ · f_min / 2보다 느린 것은 모두 걸러집니다. 10 ms CPI
기준으로 3.5 GHz에서 4.28 m/s, 28 GHz에서 0.54 m/s입니다. TX와 RX가 정지해 있으면
정지한 씬의 반사는 0 Hz에 있으므로, 드론을 건물과 갈라 주는 것이 MTI입니다. TX나
RX가 움직이는 액터에 실려 있으면 정지한 씬의 도플러도 함께 밀리므로, 노치를 그만큼
옮겨 잡습니다. f_nodes = (v_tx · k̂_dep − v_rx · k̂_arr) / λ는 에코 자신의 경로 위에
정지한 산란체가 있을 때 받을 도플러입니다(k̂_dep = TX → 첫 반사점, k̂_arr = 마지막
반사점 → RX, 정지한 노드면 0). 그래서 움직이는 레이더에서 본 정지 비행 드론은 걸러지고,
레이더와 나란히 나는 드론은 탐지됩니다. 보고되는 `doppler_hz`는 원래의 f_D 그대로입니다.
통신 경로와 클러터는 애초에 타깃 에코가 아니어서 어느 쪽이든 탐지로 세지 않습니다.
MTI를 끄려면 `mti_min_doppler_hz: 0`으로 두세요.

**융합.** 타깃과 프레임마다, 탐지된 직접 에코를 SNR 순으로 정렬하고 같은 두 지점을
잇는 링크(방향과 상관없이, 서로 뒤바뀐 쌍이나 반복된 링크)는 가장 강한 것 하나만
남깁니다. 지점은 1 m 안으로 이어지는 TX/RX 위치의 묶음으로, §7과 §8이 쓰는 같은 위치
규칙입니다. 그래서 TX에서 1 m까지 떨어진 센싱 RX는 같은 초점입니다(v0.1.12는 0.5 m
안의 초점을 묶었습니다). 기하가 서로 다른 링크가
**3개** 이상이면 바이스태틱 거리 합

`|p − TX_i| + |p − RX_i| = R_i`,  R_i = c · τ_i

을 가우스–뉴턴으로 풀어 위치 p를 구합니다. R_i 하나하나는 초점이 TX_i, RX_i인
타원체입니다(모노스태틱 링크면 구). 옥상 TRP들은 거의 한 평면에 있어서, 타원체들이
그 평면 반대편 거울상 위치에서도 만납니다. 그래서 솔버는 이전 프레임 추정치(속도만큼
앞으로 옮긴 값)와 TRP 위·아래의 점에서 각각 출발하고, 이전 추정치가 없으면 링크마다
중점 위의 점에서도 출발합니다. 비용이 같으면 이전 추정치에 가까운 후보를 고릅니다.
이전 추정치가 없으면 `car`나 `human` 타깃은 지면(z = 0)에 가장 가까운 후보를, 그
밖에는 가장 높은 후보를 고릅니다(드론은 TRP보다 위에 있다고 가정). `measurement_noise`를
켜면 비용이 최솟값에서 Δχ² ≤ 9 이내인 후보(½·Σr²의 차가 4.5σ̄² 이내, σ̄²는 링크별
σ² = ((c/B)/√(2·SNR))²의 평균)도 비용이 같은 것으로 봅니다. 거리에 잡음이 있으면
거울상이 우연히 조금 더 잘 맞는 경우가 많기 때문입니다. 링크가 정확히 **3개**이면 모든
근이 정확히 맞아서 비용으로는 고를 수 없습니다. 이전 추정치가 없는데 지하가 아닌
(z ≥ −2 m) 서로 다른 근이 둘 이상이면 추정치는 `diverged`(모호함)가 됩니다. 속도는
측정 도플러를 선형 최소제곱으로 맞춰 구하며, 부호 규약은 §4와 같습니다(양수 = 접근).

`λ · f_D,i + v_rx · k̂₂ − v_tx · k̂₁ = v_t · (k̂₂ − k̂₁)`

노드 **두 개**만 쓰는 링크 세 개(TRP A 모노스태틱, B 모노스태틱, A→B)는
퇴화합니다. 세 타원체가 모두 A–B 축에 대해 대칭이라 해가 원을 이루므로, 추정치는
`diverged`가 됩니다.

**출력 읽기.**

- `links[]`는 TX × 센싱 RX × 타깃마다 한 행이고, 이 순서를 따릅니다. `bistatic_range_m`,
  `doppler_hz`, `snr_db`, 융합에 쓴 `measured_*` 값, `range_bin`
  (floor(R / (c/B))), `doppler_bin`(round(f_D · CPI)), 그리고 `reason`이
  있습니다. `reason`은 `detected`, `below_threshold`, `mti_rejected`, `no_echo` 중
  하나입니다. `pfa`를 정하면 `pd`(와 `pd_mc`)가 `snr_db`의 탐지 확률을 줍니다(§9).
  에코가 없으면 `pd = pfa`입니다.
- `estimates[]`는 타깃마다 한 행입니다. `status`는 `ok`, `insufficient_links`(서로
  다른 링크가 3개 미만), `diverged`(수렴 실패, 퇴화한 기하, 모호한 링크 3개 해, 또는
  RMS 잔차가 거리 합 셀 c/B를 넘음) 중 하나입니다. 그 밖에 `links_used`, `position_est` / `velocity_est`,
  참값(`position_true` = 타깃 직육면체 중심, `velocity_true`)과 각각의 오차가
  들어갑니다. `gdop` = sqrt(trace((JᵀJ)⁻¹))이고, 위치 오차는 대략 GDOP × 거리
  오차입니다. 추적을 켜면 `track_*` 필드도 붙습니다(아래).
- `nodes[]`에는 선택한 TX 전부, 이어서 센싱 RX 전부가 그 프레임에서 쓴 위치·속도와
  함께 들어갑니다(액터에 탄 디바이스는 액터와 함께 움직입니다). v0.1.13 이전
  결과에서는 `null`입니다.
- `metadata.sensing`에는 상수(잡음 바닥, 적분 이득, λ, 거리·도플러 분해능, MTI 노치,
  블라인드 속도), 그 실행의 `sensing_rx_ids`와 `sensing_rx_rule`, 타깃별 통계가
  들어갑니다. 타깃별로 `detection_rate`(탐지 링크가 하나 이상인 프레임 비율),
  `link_detection_rate`, `frames_ge3_links_rate`, `ok_frames`, 위치 오차 중앙값과
  p90, 속도 오차 중앙값, GDOP 중앙값이 있습니다.

**속도 참값.** `velocity_true`(와 솔버가 쓰는 도플러)는 궤적 접선 × 속력입니다. 정확히
경유점 시각이면 나가는 구간의 속도, `once` 궤적의 끝에서는 0(액터가 멈춤), pingpong
반환점에서는 반대 방향 구간의 속도입니다. v0.1.14 이전에는 이런 프레임이 두 구간의
평균(10 m/s로 90° 꺾이면 7.07 m/s)이나 속력의 절반을 보고했으므로, 경유점 프레임의
도플러와 `velocity_true`는 v0.1.14에서 바뀌었습니다.

`measurement_noise`를 끄면 측정값이 솔버의 정확한 지연과 도플러 그대로라서, `ok`
추정치는 반올림 오차 수준으로 정확합니다(점검용으로 쓸모가 있습니다). 예외는 거울상과
정확히 비기는 경우뿐입니다. 융합한 링크의 노드가 모두 한 평면 위에 있으면(노드가 세
개면 늘 그렇습니다) 그 평면 반대편의 거울상도 정확히 맞고, 어느 쪽을 고를지는 위의
동률 규칙이 정합니다. `measurement_noise`를 켜면 거리와 도플러에
σ = 셀 / sqrt(2 · SNR)의 잡음이 붙습니다. 셀은 거리가 c/B, 도플러가 1/CPI입니다.
잡음 시드는 프레임·링크·타깃마다 정해지고, 탐지 판정은 항상 정확한 값으로 합니다.

**FR1 대 mmWave.** 에코 SNR은 다음과 같습니다.

`SNR = P_t G_t G_r λ² σ N_int / ((4π)³ R_t² R_r² · kTB · NF)`

안테나·전력·대역폭이 같으면 λ² 항 때문에 28 GHz가 3.5 GHz보다 20·log10(8) =
**18.06 dB** 손해를 봅니다. 모노스태틱, P_t = 43 dBm, 등방성 안테나,
`uav-small-size`(σ = −12.81 dBsm), B = 20 MHz, NF 7 dB, `cpi_pulses` 4096(+36.12 dB)
조건에서는 다음과 같습니다.

| f | λ (m) | 200 m에서 P_r | 200 m에서 SNR | 13 dB 도달 거리 | MTI 블라인드 속도(10 ms) |
|---|---|---|---|---|---|
| 3.5 GHz | 0.0857 | −116.17 dBm | 13.94 dB(탐지) | 211 m | 4.28 m/s |
| 28 GHz | 0.0107 | −134.23 dBm | −4.12 dB(놓침) | 74.6 m | 0.54 m/s |

그래서 28 GHz의 모노스태틱 탐지 거리는 10^(−18.06/40) = ×0.354로 줄어듭니다. 안테나
배열이 이 손실을 되찾아 줍니다. 이 절의 프레임별 탐지는 여전히 단일 소자로 계산하지만
(§11 한계 참고), §7은 실제 코드북 빔을 합성하고 §8의 `steered` 모드는 이상적인 배열
이득을 더합니다. 반대로 mmWave의 노치는 속도 기준으로 8배 좁습니다.

### 추적(EKF)

융합에는 한 프레임 안에 서로 다른 링크 세 개가 필요합니다. `sensing` 블록에
`"tracking": {"enabled": true}`를 넣으면(폼에서는 **Tracking (EKF)**) 타깃마다 등속
확장 칼만 필터가 추정치를 프레임에서 프레임으로 이어 갑니다. 탐지 링크가 한두 개뿐인
프레임에서도 추정치를 다듬고, 하나도 없는 프레임에서도 예측은 이어집니다.

- **운동.** 상태 x = [p, v]이고 월드 좌표계입니다. 프레임 사이(dt = 프레임 간격)에는
  x ← F·x, P ← F·P·Fᵀ + Q입니다. Q는 이산 백색 잡음 가속도 모델
  Q = σ_a²·[[dt⁴/4·I, dt³/2·I], [dt³/2·I, dt²·I]]이고 σ_a = `process_accel_sigma_m_s2`
  (2 m/s²)입니다.
- **측정.** 타깃의 탐지된 직접 링크가 강한 순서대로 스칼라 두 개씩을 냅니다. 거리 합
  `measured_range_m`(h = |p − TX| + |p − RX|)과 도플러 `measured_doppler_hz`
  (h = (v_tx · k̂₁ − v_rx · k̂₂ + v · (k̂₂ − k̂₁)) / λ, §4 부호)입니다. 여기서는 기하로
  묶지 않습니다. 한 타원체의 두 링크도 수신기 입장에서는 서로 독립인 측정이기
  때문입니다. σ_R = (c/B)/√(2·SNR), σ_f = (1/CPI)/√(2·SNR)(각각 최소 1 mm, 1 mHz)로,
  잡음을 켰든 껐든 `measurement_noise` 규칙을 그대로 씁니다. 움직이는 TX나 RX는 그
  프레임의 위치·속도로 들어갑니다.
- **갱신.** 스칼라를 하나씩, 매번 현재 상태에서 다시 선형화해 반영합니다.
  ν = z − h(x), S = H·P·Hᵀ + σ²입니다. ν²/S > `gate_chi2`(16 = 4σ, 자유도 1 카이제곱)인
  스칼라는 버리고 `track_gated`에 셉니다. 아니면 Joseph 형식으로 갱신하고
  `track_updates`에 셉니다.
- **시작.** 융합이 처음 `ok`인 프레임에서 융합한 위치·속도(도플러 속도가 없으면 0)로
  트랙을 시작합니다. P는 위치 축마다 max(GDOP²·σ̄_R²/3, 0.25 m²)(σ̄_R²는 융합한 링크의
  σ_R² 평균, GDOP가 없으면 100 m²), 속도 축마다 25 (m/s)²(속도가 없으면 400)에서
  출발합니다. 같은 데이터로 그 프레임에서 또 갱신하지는 않습니다.
- **재시작.** 두 조건이 함께 맞을 때 그 프레임의 융합에서 트랙을 다시 시작합니다.
  게이트가 그 프레임의 스칼라를 하나 이상 버렸고, 융합이 `ok`인데 예측한 트랙의 3차원
  게이트 밖에 있을 때입니다. 이 게이트는 P_pred + 시작 공분산에 대한 마할라노비스
  거리²이고, 문턱은 `gate_chi2`와 꼬리 확률이 같은 자유도 3 카이제곱 값입니다(16이면
  22.1). 흔한 경우가 경유점 모서리입니다. 예측은 옛 속도를 유지하고, 그 속도를 바로잡을
  도플러 스칼라는 게이트에 걸리며, 거리 합만으로는 방향을 틀지 못합니다. 융합 조건만으로는
  부족합니다. 융합 오차의 꼬리는 그 공분산이 말하는 것보다 두꺼워서, 직선 경로에서도
  가끔 튀는 융합 하나가 좋은 트랙을 갈아 치울 수 있습니다. 그 프레임의 원시 스칼라가 모두
  트랙과 맞는데도 말입니다. 그런 프레임은 트랙을 갱신합니다.
- **관성 비행과 소실.** 받아들인 스칼라가 없는 프레임은 `coasting`입니다(예측만, P가
  커짐). 이런 프레임이 `coast_max_frames`(10)개 넘게 이어지면 트랙을 버립니다(`lost`).
  사후 `track_position_std_m`이 `max_position_std_m`(25 m)을 넘어도 버립니다. 링크 한두
  개뿐인 구간이 길면 트랙은 `tracking`인 채로 그 링크가 보지 못하는 방향으로 표류하기
  때문입니다. 어느 쪽이든 다음 `ok` 융합에서 다시 시작합니다.
- **융합 사전값.** `use_as_prior`(기본)이면 그 프레임의 가우스–뉴턴 융합이 마지막 `ok`
  추정치를 속도만큼 옮긴 값 대신 트랙의 예측에서 출발합니다. `false`면 융합 필드는
  추적이 없을 때와 정확히 같습니다.

그러면 `estimates[]`의 각 행에 다음이 붙습니다.

| 필드 | 의미 |
|---|---|
| `track_status` | `none`(아직 트랙 없음), `init`(이 프레임의 융합에서 시작 또는 재시작), `tracking`(스칼라를 하나 이상 받음), `coasting`(하나도 못 받음), `lost`(버려짐, 다음 시작까지) |
| `track_position`, `track_velocity` | 사후 상태(`none`과 `lost`에서는 `null`) |
| `track_position_error_m`, `track_velocity_error_m_s` | `position_true`, `velocity_true`와의 오차 |
| `track_position_std_m` | sqrt(trace(P_pos)). 필터가 기대하는 3차원 RMS 오차로, `track_position_error_m`과 견줄 수 있습니다 |
| `track_updates`, `track_gated` | 이 프레임에서 받은 스칼라와 게이트에 걸린 스칼라 수(`init`에서는 0과 0, 트랙을 버린 프레임까지는 세고 그 뒤로는 `null`) |

추적을 끄면 모두 `null`입니다. `metadata.sensing`의 타깃별 통계에는 `tracked_frames`
(`init`, `tracking`, `coasting`), `coasting_frames`, `lost_frames`,
`lost_by_std_frames`(`max_position_std_m` 상한으로 버린 횟수. 그 프레임의 융합이 곧바로
트랙을 다시 시작하면 그 프레임은 `lost`가 아니라 `init`입니다), 트랙 위치 오차
중앙값과 p90, 트랙 속도 오차 중앙값, `frames_improved_over_fusion`(융합이 `ok`이고
트랙이 참값에 더 가까운 프레임), `frames_track_without_fusion`(융합이 `ok`가 아닌
프레임의 트랙)이 더해지고, 실행 전체에는 `median_track_position_error_m`과
`tracking_model`이 더해집니다.

## 7. ISAC 트레이드오프: 빔과 슬롯 공유

TRP 패널 하나가 UE와 드론을 동시에 겨눌 수는 없습니다. ISAC 트레이드오프는 TX마다
세 가지를 답합니다. UE에 가장 좋은 코드북 빔(**통신 빔**)은 무엇인지, 타깃을 가장 잘
보는 빔(**센싱 빔**)은 무엇인지, 그리고 둘이 슬롯을 나눠 쓰면 전송률을 얼마나 잃고
탐지 확률을 얼마나 얻는지입니다.

**실행.** Results 모드에서 **ISAC trade-off** 패널을 열고 TX(아무것도 체크하지 않으면
전부), 배열 크기, 스윕, CPI 펄스 수, P_fa, 슬롯 비율, 공유 방식을 정한 뒤 실행합니다.
API로는 다음과 같습니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/isac \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "tx_rows": 4, "tx_cols": 4,
       "sweep_start_deg": -60, "sweep_stop_deg": 60, "sweep_step_deg": 5,
       "cpi_pulses": 4096, "pfa": 1e-6, "sharing_mode": "dual_function"}'
curl http://127.0.0.1:8000/api/projects/sample_demo/results/isac   # 마지막으로 저장된 결과
```

결과는 `isac` 결과 세트로 저장됩니다(실행 기록, prune, 라벨 모두 됨).
`/simulate/sensing`과 마찬가지로 `auto`는 sionna-rt 2.2 이상이 깔려 있으면 sionna를,
아니면 경고와 함께 mock을 씁니다. RCS 솔버가 없는데 sionna를 명시하면 **409**입니다.
모르는 디바이스나 액터, UE 없음, 센싱 수신기가 없는 TX, `use_device_orientation: false`는
아무것도 풀기 전에 **400**으로 응답합니다.

Sample Demo에서 `"config_id": "sensing_fr1"`로 돌리면(Sionna, 드론은 t = 0 위치), 기본
방위각 전용 4×4 코드북은 `rx_001`을 TX 2(통신 빔 55°)가 서비스하게 하고, 드론은 TX 1(센싱
빔 −35°, 31.9 dB, P_d 0.99)과 TX 3(−40°, 23.4 dB, P_d 0.94)로는 보지만 TX 2로는 보지
못합니다. 10 m 마스트에서 보면 드론이 28° 위에 있어서, 4행 패널의 앙각 0° 빔이 수직
널에 걸리기 때문입니다(−1.0 dB). 앙각 스윕
`"elevation_start_deg": -10, "elevation_stop_deg": 60, "elevation_step_deg": 5`를 더하면
모든 TRP가 탐지하는 센싱 빔을 얻고(앙각 40°, 30°, 20°에서 51.5, 44.9, 39.4 dB), UE는
TX 1이 서비스합니다(방위각 −20°, 앙각 −10°).

| 요청 필드 | 기본값 | 의미 |
|---|---|---|
| `tx_ids` | `null` | 평가할 TX(기본: 모든 TX). |
| `ue_rx_ids` / `sensing_rx_ids` | `null` | 역할 분담. 아래 *역할* 참고. |
| `target_actor_ids` | `null` | 타깃(기본: 켜진 센싱 바인딩 전부). |
| `ue_association` | `serving` | `serving`: UE마다 최적 빔 RSS가 가장 강한 TX에 속합니다. `all`: 모든 TX가 모든 UE를 서비스합니다(TX별 단일 셀 관점). |
| `tx_rows` × `tx_cols` | 4 × 4 | TX 패널. 소자 간격은 디바이스 안테나 설정을 따릅니다. |
| `rx_rows` × `rx_cols` | `null` | 센싱 RX 패널(기본: TX와 같은 크기). UE는 늘 단일 소자입니다. |
| `use_device_orientation` | `true` | 패널이 자기 `orientation_deg`를 유지합니다. `false`는 거부됩니다. `look_at`으로는 패널 하나를 UE와 타깃 양쪽에 동시에 맞출 수 없기 때문입니다. |
| `sweep_start_deg` / `sweep_stop_deg` / `sweep_step_deg` | −60 / 60 / 5 | 패널 로컬 좌표계의 방위각 코드북. `/simulate/beamforming`의 스윕과 똑같습니다(빔 최대 361개, TX당 빔 수 × 0이 아닌 슬롯 비율 수는 최대 4096). |
| `cpi_pulses` | 4096 | ρ = 1일 때 코히어런트하게 적분하는 펄스 수 / OFDM 자원 요소 수. |
| `cpi_s` | 0.01 | 메타데이터 전용(분해능 계산). |
| `threshold_db` | 13 | 빔별 `detected` 판정 기준. |
| `pfa` | 1e-6 | Pd 모델의 오경보 확률. |
| `pd_target` | 0.9 | 파레토 요약의 동작점. |
| `slot_ratios` | 0, .05, .1, .2, .3, .5, .7, 1 | 센싱에 쓰는 슬롯(펄스) 비율 ρ. 정렬하고 중복을 없앱니다. |
| `sharing_mode` | `dual_function` | `time_sharing` 또는 `dual_function`(아래). |
| `samples_per_sp` / `max_depth` | 1 000 000 / `null` | 에코 솔브에 그대로 넘깁니다(§3과 같음). |
| `include_paths` | `false` | 솔브한 경로를 결과에 저장합니다. 에코가 먼저, 통신 경로가 뒤에 옵니다. |
| `elevation_start_deg` / `elevation_stop_deg` / `elevation_step_deg` | `null` | 패널 로컬 좌표계에서 2차원 코드북의 고도 스윕(셋 다 주거나 셋 다 비움. 고도는 최대 361개, 빔은 모두 합쳐 최대 4096개). `null`이면 방위각 코드북만 씁니다. *고도 코드북* 참고. |
| `interference` | `false` | UE SINR에 TX 간 간섭을 넣습니다(`ue_association: serving`일 때만). *간섭* 참고. |
| `detector` | Swerling 1, 몬테카를로 없음 | P_d 모델, 몬테카를로 시행 수, 경험적 임계값, 시드(§9). |

**역할.** `ue_rx_ids`와 `sensing_rx_ids`를 둘 다 비워 두면, 선택한 TX에서 1 m 안에 있는
RX가 그 TX의 센싱 수신기(모노스태틱)가 됩니다. 씬의 어느 TX에서도 1 m 안에 있지 않은
RX는 모두 UE입니다. 그래서 TRP 일부만 골라 평가해도 나머지 TRP의 레이더 수신기가
서비스 대상 UE로 바뀌지 않습니다. TX마다 센싱 후보 중 가장 가까운 RX를 고르므로,
`sensing_rx_ids`를 직접 주면 여러 TX가 수신기 하나를 나눠 쓸 수 있습니다(바이스태틱).
`ue_rx_ids`를 주면 UE 규칙 대신 그 목록을 씁니다. 한 RX가 UE이면서 센싱 수신기일 수는
없습니다.

**모델.** t = 0에서 솔브 두 번을 돌립니다. 에코 솔브(TX → 타깃 → 센싱 RX, §3)와,
타깃을 흡수체로 둔 통신 솔브(TX → UE, `include_comm_paths`와 같은 방식)입니다. 두 솔브
모두 안테나를 단일 소자로 바꿔서 돌리므로, 저장된 경로는 패널 중심을 기준으로 합니다.
빔은 그다음 경로의 월드 좌표 출발각·도래각으로부터 Sionna 합성 배열 규약에 따라
합성합니다. 방향이 o인 패널의 n번 소자가 월드 방향 k̂로 진행하는 파에 대해 보이는 응답은

`a_n = exp(+j 2π p_n · R(o)ᵀ k̂)`

이고, p_n은 파장 단위의 PlanarArray 소자 위치입니다. 빔 w는 양쪽 끝에서 w^H a로 점수를
매기는데, `/simulate/beamforming`과 같은 켤레 가중치 정합 필터입니다. sionna에서 합성한
TX 코드북은 `/simulate/beamforming`(`codebook_sweep`, `use_device_orientation: true`)을
빔 단위로 재현합니다. 회귀 테스트에서 최적 빔이 같고, 최고점에서 20 dB 안에 드는 빔은 모두
0.1 dB 이내로 맞습니다(실측 0.01 dB 미만). TX t, 코드북 빔 k, UE u에 대해 다음과 같습니다.

- `h_k,u = Σ_l α_l · w_kᴴ a_t(l)`, t → u 경로에 대한 합입니다(α_l은 `path_gain_db`와
  `phase_rad`에서 얻음). `SINR_k,u = P_t + 20·log10|h_k,u| − N0`이고
  N0 = −174 + 10·log10(B) + NF입니다. `interference`를 켜지 않으면 TX 간 간섭이
  없으므로 SINR = SNR입니다(*간섭* 참고).
- `rate_k,u = log2(1 + SINR_k,u)`이고, `R(k) = Σ_u rate_k,u`는 이 TX가 서비스하는
  UE에 대한 합입니다.
- 타깃 q의 에코는 TX 빔 k와 RX 빔 m에 대해 t → s → q 에코를 모두 코히어런트하게
  더합니다. `E_q[k, m] = Σ_e α_e (w_kᴴ a_t(e)) (w_mᴴ a_s(e))`. TX 빔마다 가장 좋은 RX
  빔을 고르고

  `SNR_q(k, ρ) = P_t + 20·log10 max_m |E_q[k, m]| − N0 + 10·log10(ρ · cpi_pulses)`

  입니다. 빔의 센싱 SNR은 가장 약한 타깃이 정합니다.

**고도 코드북.** `elevation_*` 세 필드를 주면 코드북은 스윕의 모든 방위각을 모든 고도에서
조합한 격자가 되고, 고도가 바깥 순서입니다. 빔 번호는 k = i_el · n_az + i_az입니다.
빔 (φ, θ)는 패널 로컬 방향 [cos θ cos φ, cos θ sin φ, sin θ]를 향합니다.

`w_n(φ, θ) = exp(+j 2π (y_n cos θ sin φ + z_n sin θ)) / sqrt(N)`

평면 격자에서는 kron(w_z(θ), w_y(φ, θ))와 같습니다. 노름은 1이고 자기 방향으로
10·log10(N)을 모두 내며, θ = 0이면 위의 방위각 빔과 같습니다. 센싱 RX도 같은 격자를 쓰고
최적 RX 빔을 격자 전체에서 고릅니다. 4×4 패널에서 로컬 고도 32.6°에 있는 드론을 보면
효과가 드러납니다. 방위각 빔 중 최선은 한쪽 끝당 −9.84 dB이지만 −10…40° / 5° 격자는
11.98 dB여서, 한쪽 끝마다 약 22 dB를 되찾습니다. sionna에서는 4×1 수직 ULA의 고도 빔
(TX 쪽과 RX 쪽)이, 같은 조향 벡터를 Sionna 자체 합성 배열 채널에 적용한 값과 맞습니다.
회귀 테스트에서 최적 빔이 같고 LoS 고도에 놓이며, 최고점에서 20 dB 안의 빔은 모두
0.1 dB 이내로 맞습니다(실측 0.005 dB 미만). 이 필드를 비우면 방위각 코드북 코드가
그대로 돕니다.

**간섭.** `interference: true`이면(`ue_association: serving`일 때만) TX t가 서비스하는
UE는, 그 UE까지 통신 경로가 있는 다른 선택 TX의 신호를 모두 간섭으로 받습니다. 모든
TX가 ρ와 슬롯 타이밍을 공유합니다(동기 슬롯). 통신 슬롯에서는 다른 TX가 각자 통신 빔 c를
쏘고(서비스할 UE가 없으면 쏘지 않음), 센싱 슬롯에서는 센싱 빔 s를 쏩니다(에코가 없으면
쏘지 않음). TX t2의 RSS_t2,u(k) = P_t2 + 20·log10|h_k,u|로 쓰면(mW 단위)

`I_comm(u) = Σ_t2 10^(RSS_t2,u(c_t2)/10)`, `I_sens(u) = Σ_t2 10^(RSS_t2,u(s_t2)/10)`

`SINR_k,u = RSS_t,u(k) − 10·log10(10^(N0/10) + I_comm(u))`

입니다. TX의 통신 빔은 다른 TX의 통신 빔에 따라 달라지므로 반복해서 고릅니다. 0라운드는
간섭이 없을 때의 선택입니다. 이후 라운드마다 TX가 차례로, 다른 TX의 현재 빔을 고정하고
간섭을 반영한 합 전송률이 가장 큰 빔을 고릅니다. 한 라운드 동안 아무것도 바뀌지 않으면
멈추고, 10라운드를 넘기면 경고와 함께 마지막 라운드의 빔을 보고합니다. 센싱 빔은 간섭과
무관하고 에코 SNR은 계속 잡음 제한입니다. TX를 하나만 고르면 아무것도 바뀌지 않아
SINR = SNR이 정확히 성립합니다.

**Pd.** 제곱 검파기로 적분 샘플을 판정하는 Swerling-1 타깃은 P_fa = e^(−T),
P_d = e^(−T / (1 + SNR))이므로

`P_d = P_fa^(1 / (1 + SNR))`

입니다. P_fa = 10⁻⁶에서 P_d = 0.9를 얻으려면 **21.1 dB**가 필요하고, 13 dB 임계값에서는
P_d = **0.517**입니다. 에코가 없으면 P_d = P_fa입니다. Swerling 1은 `detector.model`의
기본값이고, Swerling 0·3과 몬테카를로는 §9에 있습니다. `detector.monte_carlo_trials` > 0
이면 에코가 있는 모든 빔과 ρ > 0인 모든 파레토 점에 `pd_mc`가 붙습니다. 실행 한 번에 쓸 수
있는 시행은 TX 수 × 빔 수 × (1 + 0이 아닌 슬롯 비율 수) × 시행 수로 세어 최대 2·10⁸입니다
(TX 4개의 기본 요청이면 시행 250 000). 이를 넘는 요청은 아무것도 풀기 전에 **400**입니다.

**슬롯 공유.** 통신 빔은 k_c = argmax R(k)입니다. 슬롯(또는 펄스)의 비율 ρ를 빔 k로
센싱에 쓰므로 에코는 ρ · `cpi_pulses`만큼 적분됩니다.

- `time_sharing`: 센싱 슬롯은 데이터를 싣지 않습니다. rate_u = (1 − ρ) · rate_k_c,u.
- `dual_function`: 센싱 빔도 닿는 UE에게는 데이터를 실어 보냅니다.
  rate_u = (1 − ρ) · rate_k_c,u + ρ · rate_k,u.

ρ = 0은 통신만 하는 점 하나입니다(`beam_idx` null, P_d = P_fa). `dual_function`에서
ρ = 1인 행은 빔 자체와 같고(빔 하나가 둘 다 함), `time_sharing`에서는 전송률이 0입니다.
`interference`를 켜면 슬롯마다 그 슬롯에 다른 TX가 쏘는 빔을 간섭으로 받습니다. S는 간섭
없는 신호 전력입니다.

- `time_sharing`: rate_u = (1 − ρ) · log2(1 + S_k_c,u / (N0 + I_comm)).
- `dual_function`: rate_u = (1 − ρ) · log2(1 + S_k_c,u / (N0 + I_comm)) +
  ρ · log2(1 + S_k,u / (N0 + I_sens)).

이렇게 슬롯을 맞추면 ρ가 커질수록 전송률이 오를 수도 있습니다. 다른 TX의 센싱 빔이 UE에
통신 빔보다 약하게 닿으면(I_sens < I_comm) `dual_function` 점이 통신만 하는 전송률을
넘고, 통신 전용 대비 손실이 음수로 나옵니다. 모델이 그렇게 정의된 것이지 버그가
아닙니다.

**출력 읽기.** `txs[]` 항목마다 다음이 들어 있습니다.

- `beams[]`: 코드북 각도마다 한 행. UE별 SINR, 합 전송률, 타깃별 에코 SNR과 최적 RX
  각도, 가장 약한 타깃의 SNR, P_d, `detected`가 있습니다. 패널의 빔 표는 통신 빔 행을
  시안, 센싱 빔 행을 마젠타로 칠하고, **ISAC lobes** 오버레이는 TX마다 두 빔을 같은
  색으로 그립니다(빠지는 부분은 §11 참고). 고도 스윕이 있으면 빔마다 `elevation_deg`와
  타깃별 `target_best_rx_elevation_deg`가 붙고, 오버레이는 통신(센싱) 빔이 속한 고도
  단면의 방위각 단면을 그립니다. `interference`를 켜면 빔마다 `ue_snr_db`(간섭 없음)와
  `ue_interference_dbm`(I_comm, 다른 TX가 그 UE에 닿지 않으면 null)이 붙습니다.
- `comm_beam_angle_deg`, `sensing_beam_angle_deg`, `angle_gap_deg`(방위각 차이).
  `angles_deg`는 `beams`와 순서가 같습니다(방위각이 고도마다 반복). 고도 스윕이 있으면
  `elevations_deg`도 같은 순서로 붙고, `comm_beam_elevation_deg`,
  `sensing_beam_elevation_deg`, `elevation_gap_deg`가 짝을 채웁니다. `interference`를
  켜면 `ue_interference_sensing_dbm`이 UE별 I_sens를 줍니다.
  `ue_single_element_rss_dbm`과 `target_single_element_snr_db`는 손 계산용 1×1
  기준값입니다. 타깃에 대한 빔의 배열 이득은 `target_snr_db − target_single_element_snr_db`
  입니다. UE 기준값은 SINR이 아니라 dBm 단위 RSS이므로, UE에 대한 배열 이득은
  `ue_sinr_db + noise_floor_dbm − ue_single_element_rss_dbm`입니다.
- `points[]`: (ρ, 센싱 빔) 동작점마다 합 전송률, SNR, P_d, `pareto` 표시가 있습니다.
  **파레토 차트**는 전송률을 P_d에 대해 그립니다. 프런트는 두 축 모두에서 다른 점에
  지지 않는 점들입니다(중복은 하나로 셉니다).
- `pareto`: `comm_only_rate_bps_hz`(ρ = 0), `max_pd`,
  `rate_at_pd_target_bps_hz` / `rho_at_pd_target` / `beam_idx_at_pd_target`
  (P_d ≥ `pd_target`인 점 중 가장 높은 전송률, 도달하는 점이 없으면 null),
  `rate_loss_at_pd_target_bps_hz`, `pd_at_95pct_rate`(통신 전용 전송률의 95 %를 지키는
  점 중 가장 높은 P_d). 전송률 차이가 10⁻¹² 이내면 같은 값으로 보고 P_d가 높은 점을
  고르므로, 요약이 가리키는 점은 늘 프런트 위에 있습니다. `dual_function`에서는 통신 빔이
  모든 ρ에서 전송률을 그대로 유지하므로, 통신 빔이 목표에 닿으면 기준을 넘는 가장 작은
  ρ가 아니라 ρ = 1로 표시됩니다. 센싱 전용 TX(전송률 모두 0)는 P_d가 가장 높은 점을
  가리킵니다.

최상위에는 `ue_serving_tx`와 `noise_floor_dbm`이 있고, `metadata`에는 §6의 탐지 상수와
`snr_for_pd_target_db`, `pd_at_threshold`(둘 다 `detector.model` 기준)가 더해집니다.
서비스할 UE가 없는 TX는 센싱 전용 트레이드오프(전송률 모두 0)가 되고, 그 사실을 자기
`warnings`에 적습니다. 옵션을 켰을 때만 생기는 키도 있습니다. 고도 스윕이면
`codebook_shape`([n_el, n_az]), `interference`이면 `interference`, `ue_interferers`(UE별로
닿는 다른 TX), `interference_rounds`, `interference_converged`, `interference_model`,
기본값이 아닌 `detector`이면 `detector`(모델, 임계값, 몬테카를로의 실측 P_fa)입니다. 새
필드를 모두 기본값으로 둔 요청은 v0.1.12와 같은 `metadata.request`와 `request_hash`를
저장합니다.

## 8. 센싱 커버리지 맵

60 m 높이의 드론은 어디서 보이고, 몇 개 링크가 볼까요? 커버리지 맵은 수평 격자의 셀마다
**가상 점 타깃**을 두고 TX × 센싱 RX 링크(모노스태틱과 바이스태틱) 전부를 계산합니다.
RCS 솔브를 돌리지 않으므로 맵 하나에 1초 정도 걸립니다.

**실행.** Results 모드에서 **Sensing coverage** 패널을 열고 높이, 셀 크기, RCS(비우면
TR 38.901 `uav-small-size`, −12.81 dBsm), 임계값, 펄스 수, P_fa, 배열 이득을 정한 뒤
실행합니다. 지표 선택기로 아래 네 레이어를 바꿔 봅니다. API로는 다음과 같습니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/simulate/sensing-coverage \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "height_m": 60, "cell_size_m": 10,
       "threshold_db": 13, "cpi_pulses": 4096, "array_gain": "none"}'
curl http://127.0.0.1:8000/api/projects/sample_demo/results/sensing-coverage
```

결과는 `sensing_coverage` 결과 세트로 저장됩니다. 센싱 수신기는 ISAC 규칙을 따릅니다
(선택한 TX에서 1 m 안의 RX, 또는 `sensing_rx_ids`). 수신기가 없는 TX가 있으면 **400**입니다.

Sample Demo에서 `"config_id": "sensing_fr1"`, `height_m` 40, `cell_size_m` 5로 돌리면
(Sionna) 셀 441개, 링크 9개, 기하 6개가 나오고, 모든 셀(100 %)이 LOS·탐지·융합 가능이며
최적 SNR 중앙값은 29.3 dB입니다. 이 맵에는 MTI가 없으므로, 6절의 시나리오는 실제 비행에서
여전히 도플러 노치 때문에 링크를 잃습니다.

| 요청 필드 | 기본값 | 의미 |
|---|---|---|
| `tx_ids` / `sensing_rx_ids` | `null` | 디바이스(기본: 모든 TX와 같은 위치의 RX). |
| `rcs_dbsm` / `object_type` | `null` | 점 타깃의 σ. 값을 직접 주거나 TR 38.901 유형의 평균 σ_M을 씁니다(둘 다는 안 됨). 둘 다 비우면 `uav-small-size`. |
| `height_m` | 60 | 격자 평면의 높이(타깃 중심). ±100 000 m 이내. |
| `cell_size_m` | 10 | 셀 크기. 최대 10 000 m. 셀이 40 000개를 넘으면 경고와 함께 키웁니다. |
| `center_xy` / `size_xy` | `null` | 범위를 직접 지정(둘 다 주거나 둘 다 비움. 중심은 ±10⁷ m 이내, 변 길이는 10⁶ m까지). 기본은 씬 경계(비주얼 메시, 디바이스, 액터)에 min(15 m, max(3 m, 긴 변의 15 %))만큼 여백을 둔 것으로, sionna 라디오맵과 같습니다. |
| `threshold_db` / `cpi_pulses` / `pfa` | 13 / 4096 / 1e-6 | 탐지 조건. §6, §7과 같습니다. |
| `array_gain` | `none` | `steered`는 `tx_rows`×`tx_cols` / `rx_rows`×`rx_cols` 패널의 10·log10(N_tx) + 10·log10(N_rx)를 더합니다. 모든 링크가 모든 셀에 빔을 맞춘다는 상한입니다. |
| `min_links_for_fusion` | 3 | 셀이 융합 가능으로 판정되는 데 필요한 서로 다른 기하의 수. |
| `detector` | Swerling 1, 몬테카를로 없음 | `pd_best`의 모델(§9). `monte_carlo_trials` > 0이면 셀 5개의 몬테카를로 점검을 더합니다. |

**모델.** 링크와 셀마다, R_t = |셀 − TX|, R_r = |셀 − RX|로

`SNR = P_t + G + G_e,t + G_e,r + L_pol + 10·log10(λ² σ / ((4π)³ R_t² R_r²)) − A(R_t + R_r) − N0 + 10·log10(cpi_pulses)`

를 계산하는데, **두 구간이 모두 LOS**일 때만이고 아니면 에코가 없습니다. G는 위의 배열
이득, A는 대기 흡수입니다(설정에서 켜지 않으면 0). G_e,t와 G_e,r은 TX와 RX의 소자가 셀
쪽으로 내는 이득으로, 디바이스마다 자기 `antenna.pattern`을 자기 좌표계(`orientation_deg`)
에서 계산합니다. `iso`는 0 dB이고 `tr38901`은 최대 8 dBi, 최소 −22 dBi(30 dB
바닥)입니다. 어떤 패턴을 썼는지는 `metadata.element_patterns`에 있습니다. L_pol은 아래의
편파 항입니다(v0.1.14).

**편파.** 소자는 선형 전계 하나를 냅니다(Sionna 에코처럼 첫 번째 포트). `V`와 `VH`는
로컬 θ̂ 방향, `H`는 φ̂ 방향, `cross`는 θ̂를 −45° 돌린 방향입니다. pitch나 roll을 주면
이 전계가 월드에서 기울어집니다. Sionna의 RCS 산란은 입사 방향 k_i(TX → 타깃)와 산란
방향 k_s(타깃 → RX)의 월드 θ̂/φ̂ 기저에서 항등 행렬이므로, p_t를 타깃 쪽으로 향한 TX
소자의 월드 전계, p_r을 타깃 쪽으로 향한 RX 소자의 전계라 하면

`F = (p_t · θ̂(k_i)) (p_r · θ̂(k_s)) + (p_t · φ̂(k_i)) (p_r · φ̂(k_s))`,  `L_pol = 20·log10|F|` (≤ 0 dB)

입니다. θ̂(k)와 φ̂(k)는 방향 k의 월드 구면 단위 벡터입니다. 여기서 다음이 나옵니다.

- pitch·roll이 0이고 양쪽 편파가 같은 `V`(또는 `H`)인 소자는 F = ±1이 정확히 성립하므로
  L_pol = 0이고, 이런 맵은 v0.1.13과 같습니다(`metadata.polarization_model`도 붙지
  않습니다).
- 모노스태틱 링크에서는 두 구간 사이에 φ̂의 부호가 뒤집히므로, 에코는 셀 쪽으로 θ̂에서
  ψ만큼 기울어진 전계를 내는 소자 하나의 20·log10|cos 2ψ|만 남깁니다. 아래로 기울인
  옥상 TRP는 대부분의 셀 쪽으로 에코 전력을 잃습니다.
- 기울이지 않은 `cross`(±45°, 첫 번째 포트) 패널은 에코를 교차 편파로 받습니다. 같은
  위치에 둔 패널의 자기 에코도 마찬가지입니다. F = 0이므로 그런 링크는 모노스태틱이든
  바이스태틱이든 구간이 막힌 것처럼 그 셀에서 **에코가 없고**(Sionna 에코는 약 150 dB
  아래의 float32 잡음), mock 솔브에도 그 에코 경로가 없습니다(`no_echo`). 센싱 링크에는
  `V`(또는 `H`) 소자를 쓰세요.

L2 회귀 테스트 장소(TRP (0, 0, 10) m, 3.5 GHz, 30 m 높이의 10 dBsm 고정 타깃, `iso`
소자, V 편파. 모노스태틱 = RX가 TX 위치, 바이스태틱 = 같은 방향의 RX가 (0, 40, 10) m)에서
LOS 셀 5개(바이스태틱 링크는 4개)에 대해 잰 Sionna 에코 − v0.1.13 맵은 다음과 같습니다.

| 링크 | pitch | 에코 손실(dB) |
|---|---|---|
| 모노스태틱 | 0° | 0.000 |
| 모노스태틱 | −15° | −0.35 … −1.51 |
| 모노스태틱 | +15° | −0.42 … −1.52 |
| 모노스태틱 | −45° | −3.99 … −29.03 |
| 바이스태틱 | −15° | −0.12 … −1.88 |
| 바이스태틱 | −45° | −1.20 … −18.69 |
| 모노스태틱, `cross` | −15° | −5.32 … −6.61 |

그래서 −13…−16° 기울인 옥상 TRP는 모노스태틱 링크마다 약 0.4–1.5 dB를 잃습니다.
v0.1.13의 맵과 mock 에코는 이 손실을 보여 주지 않아 그만큼 낙관적이었고, Sionna 에코가
맞았습니다. L_pol을 넣은 맵은 V, H, VH, `cross` 소자, pitch 0, ±15°, −45°,
모노스태틱과 바이스태틱 모두에서 `RCSSolver` 에코와 0.01 dB 이내로 맞습니다(실측
3·10⁻⁵ dB 이하). 어느 링크든 손실이 0이 아니면 `metadata.polarization_model`이 모델
이름을 적습니다.

**mock과 LOS.** mock 에코도 같은 소자 이득과 L_pol을 적용하므로(v0.1.14), 셀 중심에
타깃을 둔 mock 센싱 솔브는 `iso`와 `tr38901` 소자, 어떤 방향에서든 맵 값과 같습니다.
`xpr_db`를 정한 고정 타깃에는 mock이 L_pol을 넣지 않습니다(편파 변환은 모델링하지
않습니다). sionna에서 LOS 판정은 캐시된 정적 씬에 대한 Mitsuba 그림자
광선 테스트이고, 액터 메시는 모두 뺍니다(맵은 실제 드론이 지금 어디 있는지가 아니라 장소
자체를 보는 것이기 때문입니다). 회귀 테스트에서 LOS 셀에 `RCSSolver`로 푼 고정 RCS 에코는
`iso`와 `tr38901` 소자 모두 맵과 0.01 dB 이내로 맞고(실측 약 10⁻⁵ dB), 건물 그림자 속
셀에는 직접 에코가 없습니다. mock에는 지오메트리 차폐가 없어서 모든 구간을 LOS로 보며,
결과의 `warnings`와 `metadata.los_model`에 그렇게 적습니다.

**레이어**(`values`, 라디오맵처럼 행 우선 [ny][nx]):

- `best_snr_db`: 가장 좋은 링크의 SNR(에코가 있는 링크가 없으면 null).
- `n_links_detected`: SNR ≥ `threshold_db`인 링크 수.
- `pd_best`: 가장 좋은 링크의 `detector.model` 기준 P_d(기본 Swerling 1, §7과 §9).
- `fusion_feasible`: 탐지된 링크가 **서로 다른 기하**를 `min_links_for_fusion`개 이상
  이루면 1, 아니면 0. 서로 1 m 안에 있는 디바이스(TRP의 TX와 그 센싱 RX, ISAC의 같은 위치
  규칙)는 한 지점으로 보고, 같은 두 지점을 잇는 링크는 방향과 상관없이 하나로 셉니다.
  그래서 센싱 패널이 TX에서 1 m까지 떨어져 있어도 서로 뒤바뀐 쌍(A→B, B→A)은 기하
  하나입니다. 묶음은 링크별 `geometry_group`에서 볼 수 있습니다.

`summary`에는 셀·링크·기하 수, LOS 링크가 하나 이상인 셀, 탐지가 하나 이상인 셀, 융합
가능한 셀의 비율(%), 그리고 에코가 있는 셀에 대한 최적 SNR 중앙값이 들어갑니다.
`links[]`에는 링크별 기선 길이와 LOS 비율, 탐지 비율이 있습니다.
`detector.monte_carlo_trials` > 0이면 `summary.mc_spot_check`에 에코가 있는 셀의
`best_snr_db` 분위수 0/25/50/75/100 %에 해당하는 서로 다른 셀 5개가 들어갑니다(최근접
순위. 같은 값을 가진 셀이 여럿이면 아직 고르지 않은 셀 중 번호가 가장 작은 셀. 에코가 있는
셀이 적으면 그만큼 적음). 셀마다 `cell` [ix, iy], `snr_db`, 해석적 `pd`, `pd_mc`와 그
95 % 구간(`ci_low`, `ci_high`)이 있습니다. 기본값이 아닌 `detector`이면 `metadata.detector`
가 붙습니다.

## 9. 탐지기 모델과 Pd/Pfa 몬테카를로

SEAM이 보고하는 P_d는 모두 코히어런트 적분 샘플에 대한 제곱 검파기를 적분 후 SNR(§6의
링크 `snr_db`, §7의 빔 SNR, §8의 `best_snr_db`)로 모델링한 값입니다. 적분된 잡음 샘플을
CN(0, 1)로 정규화하므로 잡음만 있으면 y = |z|² ~ Exp(1)이고, 임계값 T = −ln P_fa는 정확히
P_fa를 줍니다. S = 10^(SNR/10)은 타깃 진폭 A의 평균 |A|²입니다. A는 CPI 동안 일정하고
스캔마다 `detector.model`에 따라 요동합니다.

| `model` | 타깃 | P_d | P_fa 10⁻⁶에서 P_d 0.9 / 0.5에 필요한 SNR |
|---|---|---|---|
| `swerling0` | 요동 없음 | Q₁(√(2S), √(2T)) (Marcum Q) | 13.18 / 11.24 dB |
| `swerling1`(기본) | 레일리, \|A\|² ~ Exp(S) | P_fa^(1/(1 + S)) | 21.14 / 12.77 dB |
| `swerling3` | 지배적 산란체 하나, \|A\|² ~ Gamma(2, S/2) | e^(−T/(1 + S/2)) · (1 + T·(S/2)/(1 + S/2)²) | 17.30 / 11.95 dB |

요동하는 타깃은 높은 P_d에서 SNR이 더 필요합니다. P_d 0.9에서 Swerling 1은 요동 없는
타깃보다 8 dB가 더 듭니다.

**유도.** |A|² = s로 고정하면 P(y > T) = P(X ≤ K)이고, K ~ Pois(s)와 X ~ Pois(T)는
서로 독립입니다(비중심 카이제곱의 포아송 혼합). Swerling 0은 이 급수를 로그 공간에서
S ± (40√S + 40) 구간에 걸쳐 더합니다. P_fa 0.1부터 10⁻³⁰⁰까지 scipy의 `ncx2.sf`와 약
10⁻¹³ 이내로 맞습니다. s를 Gamma(2, θ)로 섞으면 K는 음이항 분포가 되어
P(K = k) = (k + 1) p² qᵏ이고, p = 1/(1 + θ), q = θ/(1 + θ)입니다. 그러면
Σ_{k≥x} (k + 1) p² qᵏ = qˣ((x + 1)p + q)이고, X에 대해 평균하면 e^(−Tp)(1 + Tpq),
곧 θ = S/2인 Swerling 3이 나옵니다. 같은 과정을 Gamma(1, S)로 밟으면
e^(−T/(1 + S)), 곧 Swerling 1입니다. Swerling 1은 v0.1.12의 식을 그대로 쓰므로 기본
숫자는 바뀌지 않습니다.

**모델이 쓰이는 곳.** ISAC(§7): 모든 빔과 점의 `pd`, `snr_for_pd_target_db`,
`pd_at_threshold`. 커버리지(§8): `pd_best`. 시나리오 센싱(§6): `sensing.pfa`를 정하면
링크 보고의 `pd`. 거기서 탐지 판정은 그대로 `snr_db ≥ threshold_db`입니다.

**몬테카를로.** `detector.monte_carlo_trials` > 0이면 해석적 `pd` 옆에 몬테카를로 추정
`pd_mc`가 붙습니다. 시행마다 모델에 맞는 타깃 진폭을 뽑고(Swerling 0: √S·e^(jφ),
Swerling 1: CN(0, S), Swerling 3: √Gamma(2, S/2)·e^(jφ), φ는 균등 분포) 적분 샘플
z = A + CN(0, 1)을 뽑고, |z|²가 임계값을 넘으면 탐지로 셉니다. 이는 N = `cpi_pulses`개의
펄스 A/√N + n_i를 코히어런트하게 더해 √N으로 나눈 것과 분포가 같습니다(독립인
CN(0, 1) N개를 더해 √N으로 나누면 CN(0, 1)). 그래서 시행 하나는 N과 상관없이 샘플 하나만
들고, 아래 상한이 실행 시간을 묶어 둡니다. 펄스를 하나하나 뽑아도 같은 수치가 나오지만
비용은 N배입니다. 회귀 테스트가 두 방식이 맞는지 확인합니다.

- **시드**는 위치로 정해지는 스트림이어서 결과가 계산 순서에 좌우되지 않습니다. Pd 곡선은
  `[seed, 모델 번호, SNR 번호]`, ISAC는 `[seed, TX 번호, 빔 번호]`와
  `[seed, TX 번호, 10⁶ + 점 번호]`, 시나리오는 `[seed, 프레임 번호, 링크 번호]`,
  커버리지는 `[seed, 셀 번호]`입니다.
- **임계값.** −ln P_fa이고, `empirical_threshold`이면 잡음만 있는 실행의 (1 − P_fa)
  분위수입니다. 이 경우 시행 수 ≥ 20 / P_fa(초과 사례 20개 이상)가 필요하므로 P_fa 10⁻⁶
  에서는 시행 상한 2·10⁶으로 닿지 않습니다. Pd 곡선, ISAC, 커버리지는 임계값과 실측 P_fa를
  요청마다 잡음 전용 실행 한 쌍(스트림 `[seed, 999, 0, 1]`)에서 얻습니다. 1번 실행이 경험적
  임계값을 주고, 1번과 독립인 2번 실행이 쓰이는 임계값에 대한 오경보를 셉니다. 키가 네
  자리인 까닭은 numpy가 짧은 키 뒤를 0으로 채우기 때문입니다(`[seed, 999]`와
  `[seed, 999, 0]`은 같은 스트림). 마지막 자리가 0이 아니므로 어떤 추정의 스트림과도
  겹치지 않습니다. `empirical_threshold`를 켠 시나리오 링크는 자기 임계값을 위한 잡음 전용
  실행을 따로 뽑습니다(링크 스트림에서 먼저 뽑고, 그다음 신호 실행). 시나리오 실행은
  P_fa를 재지도 보고하지도 않습니다.
- **구간**은 95 % Wilson 점수 구간입니다. 회귀 테스트에서 점당 200 000 시행은 모델마다
  SNR 다섯 곳에서 해석식과 맞습니다(고정 시드 테스트가 흔들리지 않도록 99.9 % 구간으로
  확인).
- **상한.** 추정 하나에 시행 최대 2·10⁶, 요청 하나에 시행 × 추정 수로 세어 최대 2·10⁸입니다.
  Pd 곡선은 점 수 × 모델 수(**422**), ISAC는 TX 수 × 빔 수 × (1 + 0이 아닌 슬롯 비율 수)
  (**400**), 시나리오는 프레임 수 × TX 수 × 센싱 RX 수 × 타깃 수(**400**, `empirical_threshold`
  이면 링크마다 잡음 실행이 붙으므로 시행을 두 배로 셈), 커버리지는 5입니다.

**Pd 곡선.** `POST /analysis/pd-curve`는 솔브 없이 SNR에 대한 P_d를 그리고 아무것도
저장하지 않습니다. UI에서는 Results ▸ **Detector (Pd curve)**입니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/analysis/pd-curve \
  -H "Content-Type: application/json" \
  -d '{"pfa": 1e-6, "cpi_pulses": 4096, "monte_carlo_trials": 200000}'
```

| 필드 | 기본값 | 의미 |
|---|---|---|
| `pfa` | 1e-6 | 오경보 확률. |
| `snr_min_db` / `snr_max_db` / `step_db` | −5 / 30 / 0.5 | SNR 축(71점, 최대 1001점). |
| `models` | 세 모델 전부 | 중복은 없애고 순서는 유지합니다. |
| `monte_carlo_trials` | 0 | 점당 시행 수(0이면 해석식만). |
| `empirical_threshold` | `false` | 잡음 전용 실행에서 임계값을 얻습니다(시행 수 ≥ 20 / P_fa 필요). |
| `seed` | 0 | 몬테카를로 스트림의 기준값. |
| `cpi_pulses` | 1 | 시행당 펄스 수. SNR 축은 적분 후 값이고 몬테카를로는 적분 샘플을 바로 뽑으므로, 어떤 수치도 이 값에 좌우되지 않습니다. |
| `pd_target` | 0.9 | 모델별 `snr_for_pd_target_db`를 정합니다. |

응답에는 `snr_db`와 `threshold`(−ln P_fa)가 있습니다. 모델마다 `pd`와
`snr_for_pd_target_db`가 있고, 시행 수 > 0이면 `pd_mc`와 `pd_mc_ci_low` /
`pd_mc_ci_high`, `mc_max_abs_deviation`, `mc_within_ci_fraction`도 붙습니다. 이때
최상위에는 `empirical_threshold`, `pfa_measured`, `pfa_measured_ci`가 더해지고,
`metadata.mc_method`가 어떤 방식으로 뽑았는지 알려 줍니다. 모르는 프로젝트는 **404**,
시행 예산 초과를 포함한 잘못된 요청은 **422**입니다.

## 10. 센싱 데이터셋 내보내기

`POST /export/sensing-dataset`는 저장된 시나리오 결과의 프레임별 센싱(§6)을 학습이나
오프라인 분석용 표로 펼쳐 `export/sensing_dataset/` 아래 zip 하나로 씁니다. 솔브는
돌리지 않습니다. UI에서는 Toolbar ▸ Actions ▸ **Sensing dataset (.npz)**입니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/sample_demo/export/sensing-dataset \
  -H "Content-Type: application/json" \
  -d '{"formats": ["npz", "csv"], "include_echo_paths": true,
       "split": {"train": 0.8, "val": 0.1, "test": 0.1, "seed": 0}}'
```

| 필드 | 기본값 | 의미 |
|---|---|---|
| `result_ids` | `null` | 내보낼 시나리오 결과, 이 순서대로. `null`이면 센싱 프레임이 있는 저장된 시나리오 결과 전부. |
| `skip_without_sensing` | `false` | `result_ids`와 함께: 나열한 결과에 센싱 프레임이 없으면 400 대신 건너뛰고 `warnings`에 이름을 남깁니다. UI는 일부만 고를 때 이 값을 보냅니다. |
| `formats` | `["npz"]` | `npz`, `csv`, `parquet` 중 아무것이나(parquet은 pyarrow, 즉 `parquet` extra가 필요: `pip install "seam-studio[parquet]"`). |
| `include_echo_paths` | `false` | `echoes` 표도 씁니다. |
| `split` | `null` | 프레임 단위 분할 `{train, val, test, seed}`(비율 합은 1). `null`이면 모든 행이 `all`입니다. |

응답에는 `zip_name`과 `download_url`(프로젝트 assets 경로), zip 안의 `files`, 행 수
(`num_rows`, `rows_per_result`, `rows_per_split`, `num_echo_rows`), `detected_fraction`,
`size_bytes`, `warnings`가 있습니다. zip에는 다음이 들어 있습니다.

- `links.<fmt>`: 결과 × 프레임 × TX → 센싱 RX 링크 × 타깃마다 한 행, 프레임의
  `links[]` 순서(결과마다 프레임 × TX × 센싱 RX × 타깃 행).
- `echoes.<fmt>`(`include_echo_paths`): 프레임마다 에코 경로 하나에 한 행.
- `manifest.json`: 열마다 dtype, 단위, 역할, 설명. 결과마다 레이블, 백엔드, 주파수,
  프레임 수, dt, 행 수, `scene_hash`(와 지금의 해시), `kinematics_source`, 그리고
  `metadata.sensing`의 실행 상수(타깃별 통계 제외). 그 밖에 분할, 분할별 행 수, 레이블
  균형(`detected_fraction`, `reason_counts`), 규약.
- `README.txt`: 파일별 설명, 불러오는 법, 주의할 점.

열은 늘 모두 있어서 스키마가 바뀌지 않습니다. 값이 없으면 float 열은 NaN, 문자열 열은
`""`입니다. **npz**는 열마다 1차원 배열 하나입니다(문자열은 `<U`라
`np.load(..., allow_pickle=False)`로 읽힙니다). **csv**는 헤더가 있고, float는 왕복
변환이 정확한 가장 짧은 형식, NaN은 빈 칸, 불리언은 `true`/`false`입니다. **parquet**은
NaN을 NaN 그대로 둡니다.

**links 열**

| 열 | dtype | 단위 | 역할 | 출처 |
|---|---|---|---|---|
| `result_id`, `split` | str | | meta | |
| `frame_index` | int64 | | meta | `frames` 안의 인덱스 |
| `time_s` | float64 | s | meta | `frame.time_s` |
| `tx_id`, `rx_id`, `target_id` | str | | meta | 링크 보고 |
| `tx_pos_x/y/z`, `rx_pos_x/y/z` | float64 | m | feature | `frame.sensing.nodes` |
| `tx_vel_x/y/z`, `rx_vel_x/y/z` | float64 | m/s | feature | `frame.sensing.nodes` |
| `bistatic_range_m`, `doppler_hz`, `echo_power_dbm`, `snr_db`, `measured_range_m`, `measured_doppler_hz` | float64 | m, Hz, dBm, dB | feature | 링크 보고 |
| `range_bin`, `doppler_bin` | float64(NaN = 없음) | 셀 | feature | 링크 보고 |
| `num_echoes` | int64 | | feature | 링크 보고 |
| `multipath` | bool | | feature | 링크 보고 |
| `aod_az_deg`, `aod_el_deg`, `aoa_az_deg`, `aoa_el_deg` | float64 | deg | feature | 보고된 에코(`frame.sensing.echoes`에서 `path_id`로 찾음) |
| `pd`, `pd_mc` | float64 | | feature | 링크 보고(실행에 `pfa`가 없으면 NaN) |
| `detected` | bool | | label | |
| `reason` | str | | label | `detected` / `no_echo` / `below_threshold` / `mti_rejected` |
| `position_true_x/y/z`, `velocity_true_x/y/z` | float64 | m, m/s | label | 그 타깃의 `estimates[]` 행 |
| `est_status` | str | | estimate | `ok` / `insufficient_links` / `diverged` |
| `n_links_detected`, `n_links_used` | int64 | | estimate | |
| `position_est_x/y/z`, `velocity_est_x/y/z`, `position_error_m`, `velocity_error_m_s`, `gdop` | float64 | m, m/s | estimate | |
| `track_status` | str | | track | 추적을 껐으면 `""` |
| `track_position_x/y/z`, `track_velocity_x/y/z`, `track_position_error_m`, `track_velocity_error_m_s`, `track_position_std_m`, `track_updates`, `track_gated` | float64 | m, m/s | track | 없으면 NaN |

estimate와 track 열은 타깃·프레임마다 값이 하나이고, 그 타깃의 링크 행마다 되풀이됩니다.
`velocity_true_*` 레이블은 §6을 따릅니다. 정확히 경유점 시각이면 나가는 구간의 속도이고,
`once` 궤적의 끝에서는 0입니다. v0.1.14 이전 결과는 그런 프레임에 두 구간의 평균(또는
속력의 절반)을 담고 있으므로, 예전 결과와 새 결과를 섞은 데이터셋은 그 프레임에서 두
규약이 섞입니다.

**echoes 열**: `result_id`, `split`, `frame_index`, `time_s`, `path_id`, `tx_id`,
`rx_id`, `target_id`(`""`면 `include_comm_paths`의 통신 경로), `path_type`, `delay_ns`,
`bistatic_range_m`(c·τ), `power_dbm`, `path_gain_db`, `phase_rad`, `doppler_hz`,
`aod_az_deg`, `aod_el_deg`, `aoa_az_deg`, `aoa_el_deg`, `num_interactions`(int64),
`direct`(bool: 상호작용이 [sensing]뿐), `reported`(bool: 링크 보고가 고른 에코).

**분할.** 단위는 프레임입니다. (결과, 프레임) 쌍을 내보내는 순서대로 놓고
`numpy.random.default_rng(seed)`로 섞은 뒤, 앞의 n_train개가 `train`, 다음 n_val개가
`val`, 나머지가 `test`입니다. 개수는 train·F, val·F, test·F를 최대 잉여 방식으로 반올림한
값입니다(동률이면 작은 비율 쪽, 그다음 train, val, test 순). 그래서 합이 F이고, 각각 정확한
몫과 한 프레임 넘게 차이 나지 않으며, 비율이 0인 분할에는 프레임이 하나도 가지 않습니다. 한
프레임의 행은 links든 echoes든 모두 같은 분할에 들어가고, 시드가 같으면 분할도 같습니다.

**예전 결과.** v0.1.13 이전 결과에는 `nodes`가 없습니다. 이때 TX/RX 위치는 프레임의
`device_states`, 없으면 지금의 씬에서, 속도는 지금 씬의 궤적에서 가져옵니다(실행
당시 계산한 방식 그대로이되, 정확히 경유점 시각에서는 §6의 v0.1.14 규칙을 따릅니다).
그러면 "predates v0.1.13" 경고가 붙고(씬 해시가 다르면
"scene changed since the run"도), 매니페스트에는 `"kinematics_source": "current_scene"`이
적힙니다.

**오류.** 모르는 프로젝트, 그리고 모르거나 파일이 없거나 읽을 수 없는(올바른 시나리오
결과가 아닌 경우도 포함) `result_ids` 항목은 **404**입니다. `result_ids`가 null이면 그런
파일은 건너뛰고 `warnings`에 이름을 남깁니다. 나열한 결과에 센싱 프레임이 없을 때
(`skip_without_sensing`이 아니면), 남는 결과가 하나도 없을 때, pyarrow 없이 parquet을
요청했을 때, 행이 500 000개(links + echoes)를 넘을 때는 **400**이고 아무것도 쓰지
않습니다. 모르는 형식은 **422**입니다.

**메모리.** 행은 메모리에서 만들어집니다. Python 리스트로 행당 약 1.9 KB, 배열과 인코딩한
파일로 행당 약 0.6 KB가 더 들어서, 500 000행 상한에서 내보내면 RAM을 최대 약 1.3 GB
씁니다. 메모리가 작은 기기에서는 결과를 적게 골라 내보내세요.

## 11. 한계

- 타깃은 강체 평행 이동만 합니다. 회전이나 마이크로 도플러는 없습니다.
- 직육면체 크기는 액터 박스 크기이므로 메시 액터는 `size_m`을 직접 지정해야 합니다.
- 센싱 솔브 동안 액터 자신의 메시는 씬에서 빠졌다가(산란 모델이 그 액터를
  전부 기술하므로) 끝나면 복원됩니다.
- 확산 산란·회절 구간은 없고, 센싱(에코와 `include_comm_paths` 모두)은 항상 내장
  sionna-rt 엔진에서 돕니다.
- mock은 타깃 중심의 LoS 산란점 하나와 σ_M만 씁니다. 각도별 로브, 다중 산란점
  배치, 차폐는 없습니다. Sionna처럼, 같은 위치에 놓인(모노스태틱) TX와 RX 사이에는
  LoS 통신 경로가 없습니다. v0.1.14부터 mock 에코에는 디바이스마다 소자 이득과 §8의
  편파 항이 들어갑니다(그 전에는 등방성이었습니다).
- 안테나가 섞인 경우: Sionna는 처음 선택한 TX(RX)의 안테나를 모든 TX(RX)에 적용하고,
  다르면 경고합니다. mock은 디바이스마다 자기 소자 패턴과 방향(그리고 §8의 편파 항)을
  적용합니다. 그래서 안테나가 섞여 있으면 두 백엔드의 결과가 다릅니다.
- 랜덤 성분은 기본으로 꺼져 있습니다. 솔버는 GPU에서 float32, `deterministic=False`로
  돕니다. 그래서 입력이 같아도 강한 경로에서 약 1e-6 dB, 빔 널 근처에서 최대 약 0.1 dB까지
  달라질 수 있고, 실행마다 경로 순서와 `path_id`가 바뀔 수 있습니다. 결과는 `path_id`나
  정확한 dB 값이 아니라 기하나 경로 종류로 비교하세요. 같은 `noise_seed`는 측정 잡음을
  정확히 재현합니다.
- 시간에 따른 센싱(§6)은 **오라클 연관**을 씁니다. 레이 트레이서가 에코마다 어느
  타깃인지, 직접 에코인지 다중 경로인지를 알려 줍니다. 실제 수신기라면 둘 다 추정해야
  합니다.
- 산란점이 여러 개인 타깃(`vehicle-multi-sp`, `agv-multi-sp`): 링크마다 가장 강한 직접
  에코를 보고하는데, 그 에코는 링크마다 다른 산란점에서 올 수 있습니다. 반면
  `position_true`는 직육면체 중심입니다. 그래서 `position_error_m`에 타깃 크기의 절반
  (자동차면 약 2.5 m)까지 편향이 섞일 수 있습니다. mock은 중심의 점 하나만 쓰므로 이
  편향이 없습니다.
- 탐지는 에코 단위의 잡음 한계 판정입니다. CFAR, 거리–도플러 맵, 클러터나 자기 간섭
  전력은 없습니다.
- 시간에 따른 센싱(§6)은 안테나를 단일 소자로 계산합니다(배열 이득 없음). 그래서 처리
  이득은 적분 이득뿐입니다.
- `measurement_noise`는 경험칙(셀 / sqrt(2·SNR))이지 크라메르–라오 하한이 아닙니다.
- `tracking`이 없으면 프레임마다 독립된 스냅샷이고, 이전 추정치는 다음 프레임 솔버의
  출발점으로만 쓰입니다.
- 추적(§6)은 타깃마다 등속 EKF 하나이고 오라클 연관을 씁니다. 기동 모델이나 IMM은
  없습니다(방향 전환은 융합에서 다시 시작하는 것으로 처리하므로, 급한 모서리 뒤 한두
  프레임은 융합 정확도입니다). 거짓 트랙도, 트랙 간 융합도 없고, 측정 σ는
  `measurement_noise`의 경험칙입니다. 한 프레임에 링크가 한두 개뿐이면 거리 합이 위치를
  타원체 위에서 자유롭게 두므로, 링크 세 개가 없는 구간이 길어지면 표류합니다. 그런
  구간에서는 `track_position_std_m`이 오차를 몇 배나 작게 말할 수 있습니다. 그래서
  `max_position_std_m` 상한은 실제 오차가 요구하는 것보다 늦게 트랙을 버리고,
  `coast_max_frames`는 프레임마다 스칼라가 하나라도 받아들여지는 한 트랙을 버리지
  않습니다.
- 센싱 데이터셋(§10): 레이블은 레이 트레이서가 줍니다(오라클 연관). 분할이 프레임
  단위라서 한 실행의 이웃 프레임끼리는 분할을 넘어 상관이 있습니다. 엄밀하게 시험하려면
  결과를 통째로 떼어 두세요.
- ISAC 트레이드오프(§7):
  - 고도 스윕이 없으면 코드북은 방위각 전용이라 세로 행은 늘 정면을 봅니다. λ/2 간격
    4행 패널은 패널 면에서 30° 벗어난 방향에 세로 널이 있고, 25°에서 35° 사이 어디서든
    끝마다 정면보다 14 dB 이상 낮습니다(28°와 33°에서는 21–23 dB). 그래서 위로 기울인
    옥상 TRP는 거리의 UE나 가파른 각도의 드론 쪽으로 배열 이득보다 더 많이 잃을 수 있고,
    4×4 빔이 단일 소자 기준값보다 낮게 나오기도 합니다. `elevation_*` 스윕을 주면 세로
    행도 조향합니다. 다만 여전히 고정 격자여서, 격자 고도 사이로 오는 경로는 격자 간격만큼의
    스캘럽 손실을 봅니다.
  - 기본 `iso` 소자에서는 패널에 거울상 뒤쪽 로브가 있습니다. 빔 θ는 패널 뒤쪽의
    180° − θ 방향도 같은 이득으로 비추므로, TRP 뒤에 있는 UE나 타깃도 잘리지 않습니다.
    앞뒤 비가 필요하면 `tr38901` 소자를 쓰세요. λ/2 간격에서는 스윕 범위 밖의 끝쪽 방향
    타깃(예: ±60° 스윕에서 정면 기준 78°)을 양쪽 가장자리 빔이 그레이팅 로브 자락으로
    서로 1 dB 안팎의 차이로 보므로, 어느 쪽 가장자리가 이길지는 거의 임의입니다.
  - **ISAC lobes** 오버레이는 빔의 방위각 단면을 패널 앞쪽 반구에만 그립니다(고도 스윕이
    있으면 통신 또는 센싱 빔의 고도 단면). `iso`처럼 앞뒤가 대칭인 소자라면 TX가 뒤쪽
    로브로 패널 뒤의 UE를 서비스하면서도 시안 통신 로브는 그 UE 반대쪽을 가리킬 수
    있습니다.
  - `interference`가 없으면 SINR = SNR입니다. 켜면 모든 TX가 늘 데이터가 차 있어 동기
    슬롯에서 통신 빔이나 센싱 빔을 항상 쏜다고 보고, 간섭은 UE에만 닿습니다(센싱 수신기는
    계속 잡음 제한이며, TX 간 에코나 직접 경로 누설은 없습니다). 통신 빔은 최적 응답
    평형이지 공동 최적해가 아닙니다. UE마다 대역 전체를 쓴다고 보고 전송률을 계산합니다.
  - P_d는 고정 임계값의 닫힌 식(Swerling 0, 1, 3, §9)입니다. CFAR도, 거리·도플러 셀 경계
    손실도, 펄스 간 요동(Swerling 2와 4)도 없습니다.
  - t = 0 스냅샷 하나입니다. 이중 편파 안테나는 첫 번째 편파 포트만 합성하고, UE는 단일
    소자입니다.
- 센싱 커버리지(§8):
  - 타깃은 고정 점 RCS입니다. TR 38.901의 각도별 로브는 없습니다.
  - 직접 LOS 구간만 셉니다(다중 경로 에코 없음). LOS 판정에서 액터 메시는 무시합니다.
  - `steered`는 모든 링크·셀에 대한 이상적인 전체 배열 이득입니다.
  - 소자 이득은 디바이스마다 자기 안테나를 씁니다. Sionna 솔브는 처음 선택한 TX(RX)의
    안테나를 모든 TX(RX)에 적용하고 경고하므로, 패턴이 섞여 있으면 둘이 달라집니다.
  - 편파: 배열마다 첫 번째 포트만 봅니다(Sionna 에코와 같음). 바이스태틱 링크의
    TR 38.901 타깃은 이 투영에서 15° 기울기에서 최대 0.2 dB, 45°에서 최대 2.5 dB
    벗어납니다. `xpr_db`를 정한(편파를 바꾸는) 타깃은 맵에서도 mock에서도 모델링하지
    않습니다.
  - mock은 모든 구간을 LOS로 봅니다.
- 탐지기 모델(§9): 몬테카를로는 닫힌 식이 기술하는 것과 같은 이상화된 모델(CPI 동안 일정한
  진폭, 백색 가우스 잡음, 이상적인 코히어런트 적분)을 뽑습니다. 계산을 검증하고 표본
  흩어짐을 보여 줄 뿐, 파형 수준의 시뮬레이션은 아닙니다.

## 관련 문서

- [simulation.ko.md](simulation.ko.md) — 경로, 라디오맵, 빔포밍, 채널 분석
- [trajectory_uav.ko.md](trajectory_uav.ko.md) — 액터 궤적(타깃 기본 속도의 출처)
- [datasets_export.ko.md](datasets_export.ko.md) — RFData / AODT / 채널 npz 내보내기
- [../dynamic_scattering.ko.md](../dynamic_scattering.ko.md) — 일반 경로 솔브에서 움직이는 디바이스·액터의 도플러
