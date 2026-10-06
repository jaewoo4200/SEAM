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
붙습니다. 일반 경로 솔브도 같은 규칙을 따릅니다(4절).

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

## 6. 시간에 따른 센싱: 탐지와 다중 스태틱 융합

센싱 솔브 한 번은 스냅샷 하나입니다. 센싱을 켠 **Simulate scenario**는 **프레임마다**
그 프레임의 액터 자세·속도(와 액터에 탄 디바이스 위치)로 센싱 솔브를 돌립니다.
에코는 링크별 탐지로 바뀌고, 탐지된 링크들을 융합해 위치·속도를 추정한 뒤 실제
액터 궤적과 비교해 오차를 냅니다.

**실행.** Results 모드에서 **Scenario playback**을 열고 *Include paths* 아래의
**Sensing (ISAC)**를 체크한 뒤 임계값, CPI, 적분 펄스 수를 정하고 실행합니다.
센싱 바인딩이 켜진 액터가 하나도 없으면 체크박스가 비활성화됩니다. API에서는
시나리오 요청에 `sensing` 블록을 더합니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/scenario \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "num_frames": 61, "dt_s": 0.5, "include_paths": false,
       "sensing": {"enabled": true, "threshold_db": 13, "cpi_s": 0.01,
                   "cpi_pulses": 4096, "measurement_noise": true}}'
```

결과는 평범한 `scenario` 결과입니다. 프레임마다 `sensing: {echoes, links, estimates}`가
붙고, 실행 요약은 `metadata.sensing`에 들어갑니다. `sensing`이 없거나
`enabled: false`이면 결과는 예전과 같습니다(프레임의 `sensing`은 `null`). 통신과
센싱은 백엔드 하나를 함께 씁니다. `auto`는 다른 시나리오와 똑같이 정해지고, 센싱이
없는 백엔드(sionna-rt 2.2 미만)는 mock으로 넘어가지 않고 **409**로 응답합니다. TX나
RX가 없으면 **400**이고, 바인딩된 액터가 없거나 `target_actor_ids`에 모르는 id가
있어도 **400**입니다. 실행 중에는 프로젝트별 솔브 잠금을 잡고 프레임마다 진행률을
알리며, **Cancel**도 됩니다.

| `sensing` 필드 | 기본값 | 의미 |
|---|---|---|
| `enabled` | `false` | 프레임별 센싱 솔브를 돌립니다. |
| `threshold_db` | 13 | 적분 후 SNR의 탐지 임계값(dB, −30…60). |
| `cpi_s` | 0.01 | 코히어런트 처리 구간(CPI, 초). 도플러 분해능 = 1 / CPI. |
| `cpi_pulses` | 1 | 코히어런트하게 적분하는 펄스 수 또는 OFDM 자원 요소 수. 이득 = 10·log10(N). |
| `mti_min_doppler_hz` | `null` | MTI 노치. \|f_D − f_nodes\|가 이보다 작으면 버립니다(TX/RX가 정지해 있으면 f_nodes = 0, 아래 MTI 참고). `null` = 1 / CPI(도플러 셀 하나), `0`이면 MTI를 끕니다. |
| `include_comm_paths` | `false` | 프레임마다 타깃을 흡수체로 둔 통신 경로도 풀어 `echoes` 뒤에 붙입니다(`target_id` null). 프레임당 솔브가 하나 늘어납니다. |
| `target_actor_ids` | `null` | 이 액터들만 봅니다(기본: 켜진 바인딩 전부). |
| `samples_per_sp` / `max_depth` | 1 000 000 / `null` | 프레임별 센싱 솔브에 그대로 넘깁니다(§3과 같음). |
| `measurement_noise` | `false` | 융합에 들어가는 거리·도플러에 가우스 잡음을 더합니다(아래). |
| `noise_seed` | 0 | 그 잡음의 시드. 시드가 같으면 숫자도 같습니다. |

**탐지.** 링크(TX → RX)와 타깃마다 가장 강한 **직접** 에코(TX → 산란점 → RX,
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

**융합.** 타깃과 프레임마다, 탐지된 직접 에코를 SNR 순으로 정렬하고 초점이 같은
링크(서로 뒤바뀐 쌍이나 반복된 링크)는 하나만 남깁니다. 기하가 서로 다른 링크가
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

- `links[]`는 TX × RX × 타깃마다 한 행이고, 이 순서를 따릅니다. `bistatic_range_m`,
  `doppler_hz`, `snr_db`, 융합에 쓴 `measured_*` 값, `range_bin`
  (floor(R / (c/B))), `doppler_bin`(round(f_D · CPI)), 그리고 `reason`이
  있습니다. `reason`은 `detected`, `below_threshold`, `mti_rejected`, `no_echo` 중
  하나입니다.
- `estimates[]`는 타깃마다 한 행입니다. `status`는 `ok`, `insufficient_links`(서로
  다른 링크가 3개 미만), `diverged`(수렴 실패, 퇴화한 기하, 모호한 링크 3개 해, 또는
  RMS 잔차가 거리 합 셀 c/B를 넘음) 중 하나입니다. 그 밖에 `links_used`, `position_est` / `velocity_est`,
  참값(`position_true` = 타깃 직육면체 중심, `velocity_true`)과 각각의 오차가
  들어갑니다. `gdop` = sqrt(trace((JᵀJ)⁻¹))이고, 위치 오차는 대략 GDOP × 거리
  오차입니다.
- `metadata.sensing`에는 상수(잡음 바닥, 적분 이득, λ, 거리·도플러 분해능, MTI 노치,
  블라인드 속도)와 타깃별 통계가 들어갑니다. 타깃별로 `detection_rate`(탐지 링크가
  하나 이상인 프레임 비율), `link_detection_rate`, `frames_ge3_links_rate`,
  `ok_frames`, 위치 오차 중앙값과 p90, 속도 오차 중앙값, GDOP 중앙값이 있습니다.

`measurement_noise`를 끄면 측정값이 솔버의 정확한 지연과 도플러 그대로라서, `ok`
추정치는 반올림 오차 수준으로 정확합니다(점검용으로 쓸모가 있습니다). 예외는 거울상과
정확히 비기는 경우뿐입니다. 융합한 링크의 노드가 모두 한 평면 위에 있으면(노드가 세
개면 늘 그렇습니다) 그 평면 반대편의 거울상도 정확히 맞고, 어느 쪽을 고를지는 위의
동률 규칙이 정합니다.
켜면 거리와
도플러에 σ = 셀 / sqrt(2 · SNR)의 잡음이 붙습니다. 셀은 거리가 c/B, 도플러가 1/CPI입니다.
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
(§9 한계 참고), §7은 실제 코드북 빔을 합성하고 §8의 `steered` 모드는 이상적인 배열
이득을 더합니다. 반대로 mmWave의 노치는 속도 기준으로 8배 좁습니다.

## 7. ISAC 트레이드오프: 빔과 슬롯 공유

TRP 패널 하나가 UE와 드론을 동시에 겨눌 수는 없습니다. ISAC 트레이드오프는 TX마다
세 가지를 답합니다. UE에 가장 좋은 코드북 빔(**통신 빔**)은 무엇인지, 타깃을 가장 잘
보는 빔(**센싱 빔**)은 무엇인지, 그리고 둘이 슬롯을 나눠 쓰면 전송률을 얼마나 잃고
탐지 확률을 얼마나 얻는지입니다.

**실행.** Results 모드에서 **ISAC trade-off** 패널을 열고 TX(아무것도 체크하지 않으면
전부), 배열 크기, 스윕, CPI 펄스 수, P_fa, 슬롯 비율, 공유 방식을 정한 뒤 실행합니다.
API로는 다음과 같습니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/isac \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "tx_rows": 4, "tx_cols": 4,
       "sweep_start_deg": -60, "sweep_stop_deg": 60, "sweep_step_deg": 5,
       "cpi_pulses": 4096, "pfa": 1e-6, "sharing_mode": "dual_function"}'
curl http://127.0.0.1:8000/api/projects/demo/results/isac   # 마지막으로 저장된 결과
```

결과는 `isac` 결과 세트로 저장됩니다(실행 기록, prune, 라벨 모두 됨).
`/simulate/sensing`과 마찬가지로 `auto`는 sionna-rt 2.2 이상이 깔려 있으면 sionna를,
아니면 경고와 함께 mock을 씁니다. RCS 솔버가 없는데 sionna를 명시하면 **409**입니다.
모르는 디바이스나 액터, UE 없음, 센싱 수신기가 없는 TX, `use_device_orientation: false`는
아무것도 풀기 전에 **400**으로 응답합니다.

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
  N0 = −174 + 10·log10(B) + NF입니다. TX 간 간섭은 넣지 않으므로 SINR = SNR입니다.
- `rate_k,u = log2(1 + SINR_k,u)`이고, `R(k) = Σ_u rate_k,u`는 이 TX가 서비스하는
  UE에 대한 합입니다.
- 타깃 q의 에코는 TX 빔 k와 RX 빔 m에 대해 t → s → q 에코를 모두 코히어런트하게
  더합니다. `E_q[k, m] = Σ_e α_e (w_kᴴ a_t(e)) (w_mᴴ a_s(e))`. TX 빔마다 가장 좋은 RX
  빔을 고르고

  `SNR_q(k, ρ) = P_t + 20·log10 max_m |E_q[k, m]| − N0 + 10·log10(ρ · cpi_pulses)`

  입니다. 빔의 센싱 SNR은 가장 약한 타깃이 정합니다.

**Pd.** 제곱 검파기로 적분 샘플을 판정하는 Swerling-1 타깃은 P_fa = e^(−T),
P_d = e^(−T / (1 + SNR))이므로

`P_d = P_fa^(1 / (1 + SNR))`

입니다. P_fa = 10⁻⁶에서 P_d = 0.9를 얻으려면 **21.1 dB**가 필요하고, 13 dB 임계값에서는
P_d = **0.517**입니다. 에코가 없으면 P_d = P_fa입니다.

**슬롯 공유.** 통신 빔은 k_c = argmax R(k)입니다. 슬롯(또는 펄스)의 비율 ρ를 빔 k로
센싱에 쓰므로 에코는 ρ · `cpi_pulses`만큼 적분됩니다.

- `time_sharing`: 센싱 슬롯은 데이터를 싣지 않습니다. rate_u = (1 − ρ) · rate_k_c,u.
- `dual_function`: 센싱 빔도 닿는 UE에게는 데이터를 실어 보냅니다.
  rate_u = (1 − ρ) · rate_k_c,u + ρ · rate_k,u.

ρ = 0은 통신만 하는 점 하나입니다(`beam_idx` null, P_d = P_fa). `dual_function`에서
ρ = 1인 행은 빔 자체와 같고(빔 하나가 둘 다 함), `time_sharing`에서는 전송률이 0입니다.

**출력 읽기.** `txs[]` 항목마다 다음이 들어 있습니다.

- `beams[]`: 코드북 각도마다 한 행. UE별 SINR, 합 전송률, 타깃별 에코 SNR과 최적 RX
  각도, 가장 약한 타깃의 SNR, P_d, `detected`가 있습니다. 패널의 빔 표는 통신 빔 행을
  시안, 센싱 빔 행을 마젠타로 칠하고, **ISAC lobes** 오버레이는 TX마다 두 빔을 같은
  색으로 그립니다(빠지는 부분은 §9 참고).
- `comm_beam_angle_deg`, `sensing_beam_angle_deg`, `angle_gap_deg`.
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
`snr_for_pd_target_db`, `pd_at_threshold`가 더해집니다. 서비스할 UE가 없는 TX는 센싱
전용 트레이드오프(전송률 모두 0)가 되고, 그 사실을 자기 `warnings`에 적습니다.

## 8. 센싱 커버리지 맵

60 m 높이의 드론은 어디서 보이고, 몇 개 링크가 볼까요? 커버리지 맵은 수평 격자의 셀마다
**가상 점 타깃**을 두고 TX × 센싱 RX 링크(모노스태틱과 바이스태틱) 전부를 계산합니다.
RCS 솔브를 돌리지 않으므로 맵 하나에 1초 정도 걸립니다.

**실행.** Results 모드에서 **Sensing coverage** 패널을 열고 높이, 셀 크기, RCS(비우면
TR 38.901 `uav-small-size`, −12.81 dBsm), 임계값, 펄스 수, P_fa, 배열 이득을 정한 뒤
실행합니다. 지표 선택기로 아래 네 레이어를 바꿔 봅니다. API로는 다음과 같습니다.

```bash
curl -X POST http://127.0.0.1:8000/api/projects/demo/simulate/sensing-coverage \
  -H "Content-Type: application/json" \
  -d '{"config_id": "default", "height_m": 60, "cell_size_m": 10,
       "threshold_db": 13, "cpi_pulses": 4096, "array_gain": "none"}'
curl http://127.0.0.1:8000/api/projects/demo/results/sensing-coverage
```

결과는 `sensing_coverage` 결과 세트로 저장됩니다. 센싱 수신기는 ISAC 규칙을 따릅니다
(선택한 TX에서 1 m 안의 RX, 또는 `sensing_rx_ids`). 수신기가 없는 TX가 있으면 **400**입니다.

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

**모델.** 링크와 셀마다, R_t = |셀 − TX|, R_r = |셀 − RX|로

`SNR = P_t + G + G_e,t + G_e,r + 10·log10(λ² σ / ((4π)³ R_t² R_r²)) − A(R_t + R_r) − N0 + 10·log10(cpi_pulses)`

를 계산하는데, **두 구간이 모두 LOS**일 때만이고 아니면 에코가 없습니다. G는 위의 배열
이득, A는 대기 흡수입니다(설정에서 켜지 않으면 0). G_e,t와 G_e,r은 TX와 RX의 소자가 셀
쪽으로 내는 이득으로, 디바이스마다 자기 `antenna.pattern`을 자기 좌표계(`orientation_deg`)
에서 Sionna와 같은 방식으로 계산합니다. `iso`는 0 dB이고 `tr38901`은 최대 8 dBi, 최소
−22 dBi(30 dB 바닥)입니다. 어떤 패턴을 썼는지는 `metadata.element_patterns`에 있습니다. `iso` 소자라면
mock 에코와 같은 식이어서, 셀 중심에 타깃을 둔 mock 센싱 솔브는 맵 값과 정확히 같습니다
(mock 에코는 늘 등방성입니다). sionna에서 LOS 판정은 캐시된 정적 씬에 대한 Mitsuba 그림자
광선 테스트이고, 액터 메시는 모두 뺍니다(맵은 실제 드론이 지금 어디 있는지가 아니라 장소
자체를 보는 것이기 때문입니다). 회귀 테스트에서 LOS 셀에 `RCSSolver`로 푼 고정 RCS 에코는
`iso`와 `tr38901` 소자 모두 맵과 0.01 dB 이내로 맞고(실측 약 10⁻⁵ dB), 건물 그림자 속
셀에는 직접 에코가 없습니다. mock에는 지오메트리 차폐가 없어서 모든 구간을 LOS로 보며,
결과의 `warnings`와 `metadata.los_model`에 그렇게 적습니다.

**레이어**(`values`, 라디오맵처럼 행 우선 [ny][nx]):

- `best_snr_db`: 가장 좋은 링크의 SNR(에코가 있는 링크가 없으면 null).
- `n_links_detected`: SNR ≥ `threshold_db`인 링크 수.
- `pd_best`: 가장 좋은 링크의 Swerling-1 P_d(§7).
- `fusion_feasible`: 탐지된 링크가 **서로 다른 기하**를 `min_links_for_fusion`개 이상
  이루면 1, 아니면 0. 서로 1 m 안에 있는 디바이스(TRP의 TX와 그 센싱 RX, ISAC의 같은 위치
  규칙)는 한 지점으로 보고, 같은 두 지점을 잇는 링크는 방향과 상관없이 하나로 셉니다.
  그래서 센싱 패널이 TX에서 1 m까지 떨어져 있어도 서로 뒤바뀐 쌍(A→B, B→A)은 기하
  하나입니다. 묶음은 링크별 `geometry_group`에서 볼 수 있습니다.

`summary`에는 셀·링크·기하 수, LOS 링크가 하나 이상인 셀, 탐지가 하나 이상인 셀, 융합
가능한 셀의 비율(%), 그리고 에코가 있는 셀에 대한 최적 SNR 중앙값이 들어갑니다.
`links[]`에는 링크별 기선 길이와 LOS 비율, 탐지 비율이 있습니다.

## 9. 한계

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
- 프레임마다 독립된 스냅샷입니다. 추적 필터는 없고, 이전 추정치는 다음 프레임 솔버의
  출발점으로만 쓰입니다.
- ISAC 트레이드오프(§7):
  - 코드북은 방위각 전용이라 세로 행은 늘 정면을 봅니다. λ/2 간격 4행 패널은 패널 면에서
    30° 벗어난 방향에 세로 널이 있고, 25°에서 35° 사이 어디서든 끝마다 정면보다 14 dB
    이상 낮습니다(28°와 33°에서는 21–23 dB). 그래서 위로 기울인 옥상 TRP는 거리의 UE나
    가파른 각도의 드론 쪽으로 배열 이득보다 더 많이 잃을 수 있고, 4×4 빔이 단일 소자
    기준값보다 낮게 나오기도 합니다.
  - 기본 `iso` 소자에서는 패널에 거울상 뒤쪽 로브가 있습니다. 빔 θ는 패널 뒤쪽의
    180° − θ 방향도 같은 이득으로 비추므로, TRP 뒤에 있는 UE나 타깃도 잘리지 않습니다.
    앞뒤 비가 필요하면 `tr38901` 소자를 쓰세요. λ/2 간격에서는 스윕 범위 밖의 끝쪽 방향
    타깃(예: ±60° 스윕에서 정면 기준 78°)을 양쪽 가장자리 빔이 그레이팅 로브 자락으로
    서로 1 dB 안팎의 차이로 보므로, 어느 쪽 가장자리가 이길지는 거의 임의입니다.
  - **ISAC lobes** 오버레이는 빔의 방위각 단면을 패널 앞쪽 반구에만 그립니다. `iso`처럼
    앞뒤가 대칭인 소자라면 TX가 뒤쪽 로브로 패널 뒤의 UE를 서비스하면서도 시안 통신
    로브는 그 UE 반대쪽을 가리킬 수 있습니다.
  - TX 간 간섭은 없고(SINR = SNR), UE마다 대역 전체를 쓴다고 보고 전송률을 계산합니다.
  - P_d는 Swerling-1 닫힌 식입니다. CFAR도, 거리·도플러 셀 경계 손실도 없습니다.
  - t = 0 스냅샷 하나입니다. 이중 편파 안테나는 첫 번째 편파 포트만 합성하고, UE는 단일
    소자입니다.
- 센싱 커버리지(§8):
  - 타깃은 고정 점 RCS입니다. TR 38.901의 각도별 로브는 없습니다.
  - 직접 LOS 구간만 셉니다(다중 경로 에코 없음). LOS 판정에서 액터 메시는 무시합니다.
  - `steered`는 모든 링크·셀에 대한 이상적인 전체 배열 이득입니다.
  - 소자 이득은 디바이스마다 자기 안테나를 씁니다. Sionna 솔브는 처음 선택한 TX(RX)의
    안테나를 모든 TX(RX)에 적용하므로, 패턴이 섞여 있으면 둘이 달라집니다. 편파 불일치는
    모델링하지 않습니다.
  - mock은 모든 구간을 LOS로 봅니다.

## 관련 문서

- [simulation.ko.md](simulation.ko.md) — 경로, 라디오맵, 빔포밍, 채널 분석
- [trajectory_uav.ko.md](trajectory_uav.ko.md) — 액터 궤적(타깃 기본 속도의 출처)
- [datasets_export.ko.md](datasets_export.ko.md) — RFData / AODT / 채널 npz 내보내기
- [../dynamic_scattering.ko.md](../dynamic_scattering.ko.md) — 일반 경로 솔브에서 움직이는 디바이스·액터의 도플러
