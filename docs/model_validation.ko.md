# 모델 검증 (Model Validation)

> [English](model_validation.md) · **한국어**

본 문서는 SEAM Studio에 구현된 통신 채널/전파 모델을 공식 표준·교과서·피어리뷰 문헌과 대조 검증한 결과, 업계 표준 검증 관행, 그리고 본 리포지토리에 바로 적용 가능한 실무 검증 체크리스트를 정리한다.

검증 근거는 (a) 3GPP TR 38.901 V17.0.0 공식 스펙(Table 7.4.1-1, Note 1~6)에서 축자 추출한 수식, (b) 표준 교과서(Rappaport, Tse & Viswanath, Goldsmith), (c) 저장소 소스 코드 `file:line`이다. 공식/피어리뷰 출처로 확인되지 않은 항목은 **(미검증)** 으로 표기한다.

---

## 1. 검증 대상 및 감사 총평

- **대상 파일:** `backend/app/services/channel_analysis.py`, `backend/app/services/simulation_backends/sionna_backend.py`, `plugins/example_two_ray/plugin.py`
- **1차 기준:** 3GPP TR 38.901 V17.0.0 (2022-03) Table 7.4.1-1 [1] (V16/V17 수식 동일). 교과서 [7][8][9].

**총평:** 감사한 20개 수식이 모두 인용 기준과 일치했다(**CORRECT**). 모든 3GPP 상수가 스펙과 문자 단위로 일치한다. **명백한 편차(DEVIATION)는 발견되지 않았다.** 다만 유효 범위/관례상 주석이 필요한 2개 항목을 **NEEDS-CHECK(낮음)** 으로 플래그한다. 즉, 아래 "채택할 수정" 은 계산 버그 수정이 아니라 **주석/문서화 수준의 보완**이다.

---

## 2. 검증 표 — 구현 모델 vs 기준

### 2.1 경로손실 / 채널 지표 (`channel_analysis.py`)

| # | 모델 | 구현 수식 (file:line) | 기준 | 판정 | 비고 |
|---|------|----------------------|------|------|------|
| 1 | **FSPL** | `20·log10(4π·d·f/c)`, d는 1 m로 하한 — `:67` | Friis/FSPL; `32.45 + 20log10(f_MHz) + 20log10(d_km)` 와 등가 [2][3] | CORRECT | 닫힌형, 단위 독립. 32.45 상수 등가성 설명 정확. |
| 2 | **CI (close-in) n=2/3** | `PL = FSPL(1m) + 10·n·log10(d)`, d0=1 m — `:75-83` | Rappaport CI: `PL(d)=FSPL(d0)+10n·log10(d/d0)`, d0=1 m; n=2에서 자유공간 복원 [6] | CORRECT | d0=1 m 표준 앵커, n=2가 FSPL 기울기로 정확히 수렴. |
| 3 | **UMa LOS** | `PL1=28.0+22log10(d3D)+20log10(fc)`; `PL2=28.0+40log10(d3D)+20log10(fc)−9log10(dBP²+(hBS−hUT)²)` — `:112-122` | TR 38.901 Table 7.4.1-1 [1] | CORRECT | 28.0/22/20/40/−9 전부 일치. |
| 4 | **UMa NLOS** | `PL'=13.54+39.08log10(d3D)+20log10(fc)−0.6(hUT−1.5)`; `max(LOS, NLOS')` — `:130-136` | TR 38.901 [1] | CORRECT | 4개 상수 및 `max()` 결합 규칙 정확. |
| 5 | **UMi LOS** | `PL1=32.4+21log10(d3D)+20log10(fc)`; `PL2=…40log10…−9.5log10(dBP²+(hBS−hUT)²)` — `:139-149` | TR 38.901 [1] | CORRECT | UMi −9.5 vs UMa −9.0 계수 구분 정확. |
| 6 | **UMi NLOS** | `PL'=35.3log10(d3D)+22.4+21.3log10(fc)−0.3(hUT−1.5)`; `max(LOS,NLOS')` — `:157-163` | TR 38.901 [1] | CORRECT | 35.3/22.4/21.3/−0.3 일치. |
| 7 | **InH LOS** | `32.4+17.3log10(d3D)+20log10(fc)` — `:166-168` | TR 38.901 [1] | CORRECT | 일치. |
| 8 | **InH NLOS** | `PL'=38.3log10(d3D)+17.30+24.9log10(fc)`; `max(LOS,NLOS')` — `:171-177` | TR 38.901 [1] | CORRECT | 38.3/17.30/24.9 일치, `max()` 규칙 정확. |
| 9 | **Breakpoint 거리** | `d'BP = 4·h'BS·h'UT·fc/c`, h'=h−1.0 (hE=1.0 m), fc [Hz], c=2.998e8 — `:93-98` | TR 38.901 Note 1: `d'BP=4·h'BS·h'UT·fc/c`, fc는 **Hz**, hE=1.0 m (UMi) [1] | CORRECT (플래그 A) | 수식·계수4·유효높이 차감·fc[Hz] 관례 정확. UMa hE는 아래 플래그 A 참고. |
| 10 | **잡음 플로어** | `−174 + 10log10(B) + NF` — `sionna_backend.py:302` | 290 K에서 kTB = −174 dBm/Hz [4] | CORRECT | 표준 kTB+NF. 이제 **SINR = S/(I+N)** (동일 씬의 다른 TX 간섭을 레이트레이싱으로 합산); 간섭이 없으면 `SINR = SNR`. |
| 11 | **Shannon 용량** | `B·log2(1+SINR_lin)/1e6` Mbps — `:562-565` | Shannon–Hartley `C=B·log2(1+SINR)` [8][9] | CORRECT | SINR을 dB→선형 변환 후 사용. 간섭 없으면 SNR 기반과 동일. |
| 11a | **RSRP** | `RSS − 10log10(N_sc)`, `N_sc=N_RB·12` — `:577-578` | 3GPP TS 38.215 [18]: RSRP = 자원요소(RE)당 평균 수신전력 | CORRECT | 광대역 RSS를 점유 부반송파에 균등 분배(RE당 전력). |
| 11b | **RSSI** | `10log10(lin(RSS)+lin(I)+lin(noise_floor))` — `:579-580` | TS 38.215 [18]: RSSI = 대역 내 총 수신전력(신호+잡음+간섭) | CORRECT | 이제 **간섭 전력 항 포함**(플래그 C 해소). 단일 TX 씬에서는 `I=0` 이라 이전과 동일. |
| 11c | **RSRQ** | `10log10(N_RB·lin(RSRP)/lin(RSSI))` — `:581` | TS 38.215 [18]: RSRQ = N_RB·RSRP/RSSI | CORRECT | 간섭·잡음 무시 신호지배 한계에서 `10log10(1/12)=−10.79 dB` 상한; 간섭이 있으면 그 아래로 낮아짐. |
| 12 | **RMS 지연 확산** | power-weighted `sqrt(Σw(τ−τ̄)²/Σw)`, 가중치=선형 전력 — `:300-313` | PDP 2차 중심 모멘트(선형 전력 가중) [7] | CORRECT | 평균지연·분산 모두 선형 전력 가중, 정확. |
| 13 | **간섭성 대역폭** | `Bc = 1/(2π·στ)` [MHz] — `:316-322` | Jakes 이론형(0.5 상관): `Bc=1/(2πστ)` [5] | CORRECT | 이론(Jakes) 50% 경계. Rappaport/Lee 경험식 `1/(5στ)`·`1/(50στ)` 은 다른 관례일 뿐 더 정확한 것은 아님. docstring에 "이론형" 명시 권장. |
| 14 | **K-factor** | `10log10(P_LoS / ΣP_NLoS)` — `:286-297` | Rician K = 지배(LOS) 전력 / 산란(NLOS) 전력 [7][9] | CORRECT | LOS/NLOS 부재 시 None 반환(정의 불가/∞) 처리 정확. |

### 2.2 빔포밍 / 코드북 (`sionna_backend.py`)

| # | 모델 | 구현 수식 (file:line) | 기준 | 판정 | 비고 |
|---|------|----------------------|------|------|------|
| 15 | **방위각 스티어링 벡터** | `w = exp(j·2π·y·sin(θ))`, y=파장단위 소자 오프셋, 정규화 — `:49-51` | ULA/평면배열: 소자 위상 `2π·(d/λ)·sin(θ)`, y가 이미 λ 단위 [8] | CORRECT | 실제 `normalized_positions`(λ) 사용, 소자 순서에 강건. 부호 관례 코드에서 probe 검증. 단위노름 정규화 정확. |
| 16 | **DFT 코드북 스윕** | 방위각 스캔, gain=`|w_r^H · H · w_t|²`, 최적 쌍 선택 — `:54-85` | 각도 코드북 빔트레이닝, 수신전력=`|w_r^H H w_t|²` [8] | CORRECT | 전체 [rx][tx] gain 맵·argmax 쌍 선택. 단일소자 `h00` 정규화(상대 배열이득) 일관. |
| 17 | **TX-MRT 이득** | `‖H[0,:]‖² / h00` [dB] — `:693-696` | MRT: 배열이득 `‖h‖²`, 코히런트 결합 상한 10log10(Nt) [8] | CORRECT | `vdot(h0,h0)=‖h0‖²`. 첫 RX 안테나 방향(SIMO 행) 정의 유효, h00 상대화로 배열이득. |
| 18 | **SVD 상한** | `σ_max² / h00` [dB] — `:697-699` | SVD/고유빔포밍: 최적이득=최대특이값 σ_max, 전력이득=σ_max² [8] | CORRECT | `svd(H)[0]=σ_max` 제곱→전력이득. MRT·코드북 이상의 진짜 MIMO 상한, 순서 정확. |

### 2.3 Two-ray 플러그인 (`plugins/example_two_ray/plugin.py`)

| # | 모델 | 구현 수식 (file:line) | 기준 | 판정 | 비고 |
|---|------|----------------------|------|------|------|
| 19 | **Two-ray 원거리장 PL** | `40log10(d) − 10log10(Gt) − 10log10(Gr) − 20log10(ht) − 20log10(hr)` — `:117-123` | Two-ray 지면반사 원거리장: `PL=40log10(d)−20log10(ht·hr)−10log10(Gt·Gr)`, d⁴ 기울기, 주파수 독립 [7][9] | CORRECT | d⁴/40log10 기울기·높이항·주파수 독립성 일치. Gt=Gr=1(0 dBi) 문서화, 실제 이득은 호출부에서 별도 반영. |
| 20 | **교차 거리** | `d_c = 4π·ht·hr·f/c` — `:104` | Two-ray breakpoint `d_c = 4·ht·hr/λ = 4π·ht·hr·f/c` [7][9] | CORRECT (플래그 B) | `4/λ = 4πf/c` 대수 정확. d_c 미만은 FSPL로 폴백(valid=False). 아래 플래그 B 참고. |

### 2.4 교차 관찰 (전부 CORRECT, 완결성 위해 기록)

- **fc 단위:** 38.901 수식은 `20log10(fc)` 항에 `fc=freq_hz/1e9`(GHz), breakpoint는 `freq_hz`(Hz) — Note 1 vs Note 6 관례와 정확히 일치.
- **d3D vs d2D:** LOS/NLOS PL 항은 d3D, breakpoint 비교·유효범위는 d2D(UMa/UMi)/d3D(InH). `_geometry()` (`:101-109`)가 피타고라스로 d2D 유도, 정확.
- **유효 범위** (`:190, :209`): 주파수 0.5–100 GHz(Note 2, fH=100 GHz), UMa/UMi 10 m–5 km(2D), InH 1–150 m(3D) — 스펙 적용 열과 일치.
- **CFR** (`:325-360`): `H(f)=Σ a_l·exp(−j2πf·τ_l)`, `|a_l|=sqrt(선형전력)` — 전압진폭/전력 변환 및 푸리에 부호 관례 정확.

### 2.5 3GPP 측정 지표 — RSRP / RSSI / RSRQ (`channel_analysis.py:567-609`)

레이트레이싱으로 얻은 광대역 RSS를, 요청한 부반송파 간격(`subcarrier_spacing_khz`,
기본 30 kHz = 5G NR FR1, 15 kHz = LTE)의 OFDM 자원격자 위에 얹어 3GPP **TS 38.215**
[18] 스타일 측정량을 유도한다. 자원블록 수는 `N_RB = ⌊B / (12·SCS)⌋`(RB당 12
부반송파), 점유 부반송파는 `N_sc = 12·N_RB` 이다.

- **RSRP (Reference Signal Received Power)** = 자원요소(RE)당 평균 수신전력.
  광대역 RSS를 점유 부반송파에 균등 분배하므로
  `RSRP[dBm] = RSS[dBm] − 10·log10(N_sc)`.
- **RSSI (Received Signal Strength Indicator)** = 측정 대역 내 총 수신전력(신호 +
  간섭 + 잡음)을 선형으로 합산: `RSSI = 10·log10(lin(RSS) + lin(I) + lin(noise_floor))`.
  이제 **간섭 전력 항 `I` 을 포함한다**(§2.1 #11b, 플래그 C 해소). `I` 는 동일 씬의
  서빙 TX 이외 모든 TX 가 이 RX 에 만드는 레이트레이싱 수신전력의 합(풀버퍼 가정,
  §3 플래그 C). 단일 TX 씬에서는 `I=0` 이라 이전과 결과가 동일하다.
- **RSRQ (Reference Signal Received Quality)** = `N_RB·RSRP/RSSI`(선형),
  `RSRQ[dB] = 10·log10(N_RB·lin(RSRP)/lin(RSSI))`.

**신호지배 상한(스팟체크):** 간섭·잡음이 신호보다 훨씬 작아 `RSSI → RSS` 로
수렴하면 `RSRQ → 10·log10(N_RB·RSRP/RSS) = 10·log10(N_RB/N_sc) = 10·log10(1/12)
= −10.79 dB`. 즉 RSRQ 의 이론적 상한은 SCS·대역폭과 무관하게 **−10.79 dB** 이며
(정확히 100% 자원 점유·간섭 0 가정), **간섭이 있으면 RSSI 분모가 커져 그 아래로
낮아진다.** `tests/test_channel_analysis.py` 는 고 SNR·단일 TX mock 링크에서 이
상한을 ±0.05 dB 로 핀하고, SCS 를 30→15 kHz 로 반감하면 `N_sc` 가 배가되어 RSRP 가
정확히 `−10·log10(N_sc15/N_sc30)`(≈ −3.01 dB, 격자 반올림 제외)만큼 감소함을
검증한다. 또한 **2-TX 씬 테스트**(`test_api_two_tx_interference_lowers_sinr`)는
RSRQ 가 이 −10.79 dB 상한 아래로 내려가고, `SINR = S/(I+N) < SNR`,
`RSSI = 10log10(lin(S)+lin(I)+lin(N))` 가 정의대로 성립함을 검증한다.

### 2.6 재질 임팩트 평가 — NMSE / 코사인 유사도 / dRSS (`services/material_impact.py`)

동일 TX→RX 링크를 **재질 지정 씬**과 **단일 기준재질 씬**(모든 프림을
`baseline_material_id`, 기본 `itu_concrete` 로 재바인딩)에서 각각 풀어, 두 채널
주파수응답 `H(f)` 을 위치별로 비교한다(Lee et al., KICS 2026). `H(f_k)=Σ_l g_l
exp(−j2πf_k τ_l)`, `|g_l|=sqrt(선형전력)` 은 §2.4 CFR 및 채널분석 패널과 동일한 탭
모델이다(`:35-46`).

| # | 지표 | 구현 수식 (file:line) | 기준 (KICS eq.) | 판정 | 비고 |
|---|------|----------------------|-----------------|------|------|
| 21 | **위치별 NMSE** | `10·log10(Σ_k|H_mat−H_base|² / Σ_k|H_mat|²)` [dB] — `:134-135` | 정규화 평균제곱오차 `NMSE = ‖H_mat−H_base‖² / ‖H_mat‖²` [KICS] | CORRECT | 분자·분모 모두 선형전력합, dB 변환. `err=0`(동일 채널) 시 −300 dB 하한(−∞ 대체). `> sensitive_nmse_db`(기본 −60 dB) 이면 material-sensitive 플래그. |
| 22 | **글로벌 NMSE** | `10·log10(Σ_pos err / Σ_pos E_mat)` [dB] — `:166-168` | 전 위치 오차·에너지 선형 누적 후 NMSE [KICS] | CORRECT | 위치별 dB 값의 평균이 아니라 **선형 누적비**(에너지 가중). 누적 분자/분모=0 이면 None(정의 불가). |
| 23 | **코사인 유사도** | `|H_matᴴ·H_base| / (‖H_mat‖·‖H_base‖)` — `:141-143` | CFR 형상 유사도(내적/노름곱), [0,1] [KICS] | CORRECT | `vdot`(켤레내적)의 절댓값. 두 CFR 이 스케일만 다르면 1.0. `E_mat,E_base>0` 일 때만 산출. |
| 24 | **dRSS** | `RSS_mat − RSS_base`, `RSS=10log10(Σ 선형전력)` [dB] — `:147-157` | 부호 있는 수신전력 차 [KICS] | CORRECT | 재질−기준. 양수=지정재질이 기준보다 수신전력 상승. 양쪽 경로 존재 시에만. |
| 25 | **용량 프록시** | `B·mean_f log2(1 + P|h(f)|²/N) / 1e6` Mbps — `:49-56` | Shannon 처리량 프록시(주파수 평균) [KICS/8][9] | CORRECT | 재질/기준 각각 산출, dB→선형 SNR. §2.1 #11 Shannon 과 동일 계열(여기선 주파수축 평균). |

**항등식 스팟체크(mock):** mock 백엔드는 ITU 주파수의존 재질에 대해 산란항만
반영(§rf) 하므로 **material-blind** 이다. 유일한 반사 프림에 이미 붙은 재질을
`baseline_material_id` 로 그대로 주면 두 씬이 RF-동일해져, 모든 위치에서
`H_mat≡H_base` → **코사인 유사도 1, dRSS 0, err=0 이라 글로벌 NMSE None** 으로
수렴한다. `tests/test_material_impact.py::test_material_impact_identity_three_waypoints`
가 3 웨이포인트에서 이 수학적 항등을 핀한다. 위치별 NMSE 의 실제 분포(−6~−17 dB,
`lab_room`)는 Sionna 백엔드에서 검증된다.

---

## 3. 채택할 수정 (감사가 발견한 편차 → 정확한 코드 변경)

감사 결과 **계산 오류(DEVIATION)는 없었다.** 따라서 아래는 "잘못된 값 → 올바른 값" 교체가 아니라, 유효범위/관례를 명확히 하는 **최소 코드 변경(주로 주석 + 1개 선택적 조건 분기)** 이다. 심각도 모두 **낮음(advisory)**.

### 플래그 A — UMa 유효높이 hE 단순화 (`channel_analysis.py:93-98`) — NEEDS-CHECK / 낮음

- **현상:** 코드가 UMa/UMi 모두에 고정 `hE = 1.0 m` 사용.
- **기준:** TR 38.901 Table 7.4.1-1 Note 1 [1]. `hE=1.0 m` 은 **UMi에서는 정확**. **UMa** 에서는 확률변수: 확률 `1/(1+C(d2D,hUT))` 로 `hE=1 m`, 아니면 `uniform(12,15,…,hUT−1.5)` 에서 추출. `hUT<13 m` 이면 `C=0 ⇒ hE=1 m` 로 결정적이라 **동일**. `hUT≥13 m` 에서만 LOS PL1→PL2 전환점이 이동.
- **영향:** 고지대 UT 안테나에서만 UMa LOS breakpoint 이동, 통상 UT 높이에서는 무시 가능. ns-3 등 다수 링크버짓 도구가 동일 단순화 채택.
- **채택할 변경 (주석):** `channel_analysis.py:93` 부근 breakpoint 계산 지점에 다음 취지의 주석 추가.

  ```python
  # NOTE(38.901 Note 1): hE=1.0 m is exact for UMi, and for UMa when hUT<13 m
  # (C()=0 -> hE deterministically 1 m). For UMa with hUT>=13 m, 38.901 makes hE
  # a random variable in {1 m, U(12,15,...,hUT-1.5)}; we use the common deterministic
  # hE=1 m simplification (as in ns-3). Only shifts the LOS PL1->PL2 breakpoint for
  # high UT antennas.
  ```
- **선택적 코드 변경(원할 경우):** UMa 경로에서 `hUT >= 13 m` 일 때 결과가 근사임을 알리는 경고 플래그를 반환값에 추가(계산 자체는 변경하지 않음).

### 플래그 B — Two-ray 교차 상수 4π vs 4 관례 (`plugin.py:104`) — NEEDS-CHECK / 낮음

- **현상:** `d_c = 4π·ht·hr·f/c = 4π·ht·hr/λ`. 이는 `4·ht·hr/λ`(Rappaport eq. 4.58 [7])보다 **π배 큼**.
- **분석:** 일부 교과서는 two-ray breakpoint를 `4·ht·hr/λ`, 다른 문헌은 1차 프레넬존/위상교차 기준으로 `4π·ht·hr/λ` 로 정의 — 4π vs 4 모호성이 교과서마다 존재. 코드의 `4π·ht·hr·f/c` 는 자체 docstring과 내적으로 일관되며 피어리뷰 문헌에도 두 형태가 모두 등장하므로 **명백한 오류가 아님(NEEDS-CHECK)**.
- **영향:** 만약 의도한 기준이 Rappaport eq. 4.58(`4 ht hr/λ`)이라면, 여분의 π가 d_c를 약 3.14배 키워 FSPL 폴백(근거리) 영역을 넓힘.
- **채택할 변경 (택1):**
  1. **의도가 프레넬/위상교차(4π)면** — 변경 불필요. docstring에 "first-Fresnel-zone / phase-crossover form (4π·ht·hr/λ), not the bare 4·ht·hr/λ" 를 1줄 명시.
  2. **의도가 Rappaport eq. 4.58(4)이면** — `plugin.py:104` 를 다음으로 교체:

     ```python
     # was: d_c = 4.0 * math.pi * ht * hr * f / c
     d_c = 4.0 * ht * hr * f / c        # Rappaport eq. 4.58: d_c = 4·ht·hr/λ
     ```
  - **권장:** 인용 교과서 판(edition)을 확정한 뒤 택일. 확정 전까지는 옵션 1(주석)만 적용.

### 플래그 C — 다중 TX 동일채널 간섭 (`channel_analysis.py:535-565`) — RESOLVED

- **이전 현상:** RSSI = `lin(RSS) + lin(noise_floor)`, SINR = SNR — **간섭 전력 항이 없었다**.
- **현재 상태(구현됨):** 동일 채널(co-channel) 간섭을 **모델링한다.** 서빙 TX 이외의
  모든 TX 가 이 RX 에 만드는 레이트레이싱 수신전력을 선형 합산해 간섭 `I` 를 얻고
  (`analyze_channel` 이 다른 TX 들만 켠 1회 추가 solve 로 계산, 씬 캐시로 저렴),
  다음이 모두 `I` 를 반영한다:
  - `interference_dbm = 10log10(Σ_{k≠serving} lin(P_k@rx))`, `num_interferers`,
  - `SINR = S / (I + N)` (`sinr_db`; 간섭 없으면 `= SNR`),
  - `RSSI = 10log10(lin(S) + lin(I) + lin(N))` → 따라서 `RSRQ` 도 간섭 반영,
  - Shannon 용량이 SINR 기반(`B·log2(1+SINR)`).
  - 궤적(trajectory)에서도 동일: 각 웨이포인트가 `interference_dbm` 과 참 SINR 을
    운반하며, `serving_tx_id` 로 서빙 셀을 선택(미지정 시 첫 TX)한다
    (`trajectory.py:93-159`).
- **기준:** TS 38.215 [18]. RSSI/RSRQ 는 신호 + 잡음 + **인접셀 간섭**을 모두 포함하며,
  이제 정의를 온전히 만족한다.
- **가정·남은 단순화:** **풀버퍼(full-buffer) 최악조건** — 모든 간섭 TX 가 동일
  자원(시간·주파수) 위에서 동시에 100% 부하로 송신한다고 가정한다. 즉 (1) **스케줄러
  /부하 모델 없음**(부분 부하·자원 재사용·활성계수 미반영, 실측보다 비관적),
  (2) **셀 간 스크램블링/직교화 없음**(간섭을 코히런트가 아닌 전력 합으로 처리,
  간섭 신호의 심볼 상관 무시). 두 항목 모두 향후 확장 지점이다.

### 정리

| 플래그 | 파일:line | 유형 | 필수 변경 | 심각도 |
|--------|-----------|------|-----------|--------|
| A | `channel_analysis.py:93-98` | 주석(+선택적 경고 플래그) | 아니오(문서화) | 낮음 |
| B | `plugin.py:104` | 주석 또는 상수 정정(의도 확인 후) | 의도 확인 필요 | 낮음 |
| C | `channel_analysis.py:535-565` | 간섭 모델링(구현 완료) | **완료(RESOLVED)** — 남은 것은 스케줄러/스크램블링 | 낮음 |

---

## 4. 업계 검증 관행 (Industry Validation Practices)

상용/연구 도구가 RF 디지털트윈을 검증하는 대표 방식과 수용 기준. 정량 임계값은 출처에 연결한다.

### 4.1 측정 캠페인 대비 경로손실 RMSE (Remcom Wireless InSite 계열)

- **방식:** full 3D 레이트레이싱(SBR + image), 지형·구조물·수목 반영 [10]. 검증은 **드라이브/측정 캠페인 대비 링크별 경로손실 RMSE**, 재료 EM 파라미터 튜닝 후 비교.
- **수용 범위(문헌 요약, NYURay 리뷰 [11]):** 실내 보정 시 ≈**5 dB RMSE**(2.4/5 GHz), 단 재료 유전율/도전율 선택만으로 **7–10 dB** 변동 → 재료 보정이 오차예산 지배. 도심/산란환경은 **6.58–13.86 dB RMSE**(미보정 도심 ~12–15 dB "practical" 대역).
- **시사점:** 본 도구의 `services/calibration.py` level-offset RMSE 지표가 업계 관례와 정확히 일치. "좋음" 기준 ≈ 보정 실내 **≤5–6 dB**, 미보정 도심 최대 ~10–15 dB.

### 4.2 CFR/CIR 그라운드트루스 관례 (NVIDIA AODT)

- AODT는 채널 출력 데이터모델을 명시 [12]: `raypaths`(기하 CIR, 경로별 복소이득 `h`, 지연 `τ`), `cirs`(시간영역 `h(t)=Σ h·δ(t−τ)`), `cfrs`(부반송파별 `H(k)`).
- **정규화 관례:** AODT의 UE CFR은 송신전력/안테나수 정규화를 CFR에 접음: `⟨H^UE,H^UE⟩ = [P^RU/(n·N_pol·N_hor·N_vert)]·⟨H^ch,H^ch⟩`. 본 백엔드는 `|a|` 이득 계산 후 `power_dbm` 을 별도로 더하고(`sionna_backend.py:882`), 소자 전력을 경로당 1진폭으로 합산(`:843-848`) — **교차비교 시 명시해야 할 관례 차이(per-element vs per-array)**.
- **검증 태세:** AODT 문서는 CFR/CIR/raypaths DB를 ML 학습용 그라운드트루스로 규정, EM 솔버 수준 정확도. NVIDIA/Keysight 발행 "AODT vs 측정" 수용 RMSE 수치는 **미검증** (공식 문서 미발견).

### 4.3 Sionna RT의 발표된 검증과 미분보정 관례

- **중요 뉘앙스:** 원 Sionna RT 논문 [13] 은 **측정 데이터 대비 검증이 아님**. 자체 레이트레이서가 생성한 **합성 CFR** 에 재료 파라미터를 학습(정규화 MSE). 논문 자체가 "정확한 위상은 레이트레이싱으로 예측하기 어려워, CFR 학습 방식이 측정 데이터에는 잘 안 맞을 것" 이라 경고. 즉 "미분 보정" 은 **역량(capability)** 이지 측정검증 결과가 아님 — 정직하게 인용할 것.
- **FSPL 정합:** ns-3 연동 연구 [14] ("Ns3 meets Sionna")는 Sionna RT가 ns-3 Friis 자유공간 모델과 **동일 결과** 임을 보고(정성). 다만 "**<0.01 dB / <1 ns**" 라는 정량 임계값은 해당 문헌 본문에서 확인되지 않음 — **(미검증)**. 본 도구의 FSPL 정확성 테스트는 이 정합을 재현하는 것을 목표로 하되, 임계값은 자체 설정(§6 A: <0.1 dB).
- **실시스템(5G 테스트베드) 검증:** VNA 측정 채널을 OAI 5G-NR 에뮬레이터에 주입한 시스템레벨 검증에서, (1) 재료 물성 불일치, (2) **근거리장(Fraunhofer 거리 내)** 에서 원거리장 패턴 가정 붕괴가 주 오차원으로 보고됨. **교훈:** KPI(RSRP/SNR/SINR) 레벨에서 검증, 계측기 de-embedding, Tx Fraunhofer 거리 내 지점은 out-of-model 로 표시. (구체 arXiv ID·수치는 원 노트에서 미확인 — **(미검증)**.)
- **mmWave 채널사운더 보정(수용기준 골드스탠다드):** [15] EuCAP 2024. 26–30 GHz VNA 사운더, SAGE MPC 추출, Volcano Flex RT. 반사 −3 dB·회절 −2 dB 오프셋을 보정하며 반사+확산 **전력 균형** 유지. **최종 수용:** 수신전력/지연확산/방위각확산 평균오차 **< 1.5 dB / < 5 ns / < 2°**. 논문의 파라미터별 before/after 표(평균오차·표준편차·RMSE·상관)는 복제할 가치가 있는 템플릿.
- **Upper mid-band 사이트특정 보정(NYURay):** [11] 6.75/16.95 GHz UMi Brooklyn, 슬라이딩상관 사운더. **경로손실 RMSE 3.2 dB(LOS)/5.8 dB(NLOS)**, PLE 편차 0.03–0.14(3GPP 대비 5% 이내). 위치보정으로 8 dB 수신전력 과대추정·4 ns 지연불일치 교정. **정직한 한계:** RT가 **RMS 지연확산을 과소추정**(RT 24.7/21.6 ns vs 측정 62.8/46.5 ns) — 확산/수목 산란 누락. 본 도구도 동일 DS 과소추정 리스크 → 문서화 권장.

### 4.4 3GPP TR 38.901 캘리브레이션(레퍼런스-CDF 방법론)

- 38.901 "calibration" 은 측정검증이 아니라 **구현 간 합의(cross-implementation agreement)**: 합의된 drop을 각사가 돌려 출력 CDF를 겹침 [16]. 본 도구의 "cross-engine agreement" 체크리스트 항목의 원형.
- **Large-scale (§7.8.1):** fast fading OFF, UMa/UMi/InH, 6/30/70 GHz. 지표: 커플링손실(서빙셀, LOS PL 기반), geometry(SIR) with/without noise.
- **Full (§7.8.2):** fast fading ON, 6/30/60/70 GHz. 지표: 커플링손실, wideband SIR(무잡음), 지연확산·각확산(ASD/ZSD/ASA/ZSA) CDF, PRB 특이값 CDF(최대/최소/비, 10·log10).
- **Indoor Factory (§7.8.4):** InF-SL/DL/SH/DH, 3.5 & 28 GHz. first-path excess delay CDF 추가.
- **레퍼런스 결과:** R1-165974/R1-165975(large-scale/full), R1-1700990(추가기능), R1-1909704(InF) — 겹쳐야 할 CDF의 출처.
- **레퍼런스 지오메트리(좋은 스팟체크):** GCS/LCS θ=0 천정/θ=90° 수평(§7.1.1); UMa hBS=25 m, ISD 500 m, min 2D 35 m; UMi hBS=10 m, ISD 200 m, min 2D 10 m; Indoor-office 120×50×3 m, 천장 BS 3 m, hUT 1 m. 본 도구 UMa/UMi 수식(`:112-163`)을 이 지오메트리로 스팟체크 가능.
- **38.901 모델 자체의 측정검증:** [17] InH 6.75/16.95/28/73 GHz에서 shadow-fading σ가 측정과 **< 0.6 dB** 일치(LOS/NLOS), 단 고주파 NLOS 경로손실은 과소추정. → 본 도구 38.901 오버레이는 σ_SF를 십분의 몇 dB 이내로 재현해야 함.

### 4.5 수용기준 요약표 ("통과" 의 의미)

| 기능 / 테스트 | 지표 | 수용 임계값 | 출처 |
|---|---|---|---|
| FSPL 정확성 | PL·지연 절대오차 | **< 0.01 dB / < 1 ns** vs Friis/analytic **(미검증 — 본문 수치 미확인)** | [14] |
| 사이트특정 RT vs 사운더(보정) | 수신전력/DS/AS 평균오차 | **< 1.5 dB / < 5 ns / < 2°** | [15] |
| RT 경로손실(upper mid-band, 보정) | 경로손실 RMSE | **~3.2 dB LOS / ~5.8 dB NLOS**; PLE Δ 0.03–0.14 | [11] |
| Wireless InSite 실내(보정) | 경로손실 RMSE | **~5 dB**; 재료선택 ±7–10 dB | [11] |
| 38.901 모델 vs 측정(σ_SF) | shadow-fading std 차 | **< 0.6 dB** | [17] |
| 38.901 구현 간 캘리브레이션 | 커플링손실·SIR·DS·AS·특이값 CDF 겹침 | 정성적 CDF 일치 vs R1-165974/165975 | [16] |
| 재료 보정 수렴 | level-offset RMSE 감소 | 격자 해상도 간 sub-0.1 dB 일관성 (NYURay) | [11] |

---

## 5. 본 리포지토리용 실무 검증 체크리스트

비용 대비 가치 순. 각 항목은 기존 코드 이음새에 매핑된다.

**A. FSPL 정확성 자기검증 (자동화; 단위테스트).** 단일 평면·단일 재료·LOS-only 씬에서 레이트레이싱 경로손실이 `fspl_db()`(`channel_analysis.py:59`)와 **< 0.1 dB** (문헌 목표 <0.01 dB는 스트레치)로, 지연이 `d/c` 와 **< 1 ns** 로 일치하는지 assert. RT가 `fspl` 모델에 대해 `delta_vs_rt_db`(`:242, :247`)를 이미 노출하므로, 자유공간 지오메트리에서 `abs(delta) < tol` 만 확인하면 됨. NYURay의 4 m 자유공간 일일 기준의 아날로그.

**B. 38.901 레퍼런스포인트 스팟체크 (자동화; 단위테스트).** `_uma`/`_umi`/`_inh`(`:112-177`)를 §4.4 표준 지오메트리(UMa hBS 25 m/ISD 500 m/35 m; UMi hBS 10 m/10 m; InH 천장 BS 3 m, hUT 1 m)에서 평가해 닫힌형 스펙 수식값에 핀. **breakpoint 전환이 d_BP에서 연속**이고 **NLOS ≥ LOS** 가 전 구간 성립(`max()` at `:136,:163,:177`)함을 assert. 주파수 유효 플래그가 0.5–100 GHz 밖에서 발화(`:190`)하는지 확인.

**C. 크로스엔진 합의 리포트 (자동화; 38.901 패턴).** 아키텍처가 대체 엔진 venv를 이미 지원(`sionna_backend.py:326, 350`). 고정 씬에서 builtin vs 대체 엔진을 돌려 **링크별 경로손실 델타 + RSS/DS CDF 겹침** 을 리포트. 중앙값 절대델타가 허용치(동일 솔버 버전 ~1 dB 시작, >3 dB 플래그) 이내면 통과. 38.901 "CDF 오버레이" 수용 철학의 반영(측정 대비 아님).

**D. 에너지 / 물리 일관성 체크 (자동화; 저비용 불변식).**
- **FSPL 단조성:** 경로손실이 거리·주파수 증가에 대해 순증(부호오류 방지).
- **전력 무생성:** 각 반사/투과가 동일 경로길이 LOS 자유공간 이득을 초과하지 않음(반사계수 ≤ 1). 경로별 `|a|`(`sionna_backend.py:882`)로 검사 가능. *(형식적 수용기준으로 문헌에 명시된 바는 아님 — 권장 불변식 — **(미검증)**.)*
- **상반성 스팟체크:** 대칭 링크에서 Tx/Rx 역할 교환 시 총 경로이득이 솔버 노이즈 이내 일치.
- **CFR↔CIR 일관성:** `compute_cfr`(`:325`)의 DC 빈 = 탭 진폭의 코히런트 합과 일치하는지 assert.
- **K-factor / DS 새너티:** RT가 RMS 지연확산을 통상 과소추정(NYURay 25 ns vs 63 ns)함을 플래그 → `rms_delay_spread_ns`(`:300`)를 하한으로 해석하도록 안내.

**E. 측정보정 수용 리포트 (기존 확장).** `services/calibration.py`가 이미 level-offset RMSE/MAE·유의개선 게이트 보고(`:71-171`). 여기에 mmWave-industrial·NYURay 논문 스타일 **파라미터별 before/after 표(평균오차·표준편차·RMSE·상관)** 를 추가하고, 보정된 **재료 오프셋(반사/회절 dB)** 을 Volcano Flex −3/−2 dB처럼 노출. 문서 목표: 보정 실내 경로손실 RMSE **≤ ~5 dB**, 수신전력 평균오차 **< 1.5 dB**.

**F. 근거리장 가드 (문서화 + 경고).** OAI/Sionna 결과에 따라, **임의의 Tx–Rx 이격 < Fraunhofer 거리 `2D²/λ`** 이면 경고(원거리장 패턴 가정 붕괴). 백엔드가 이미 만드는 배열 개구(`sionna_backend.py:159-189`)에서 `D` 계산해 채널분석 경고로 노출.

**G. 계측/단위 규율 (문서화).** 측정 비교 시 캠페인의 **VNA SOLT/TRL de-embedding** 요구, 저장 CFR이 per-element인지 per-array 정규화인지 명시(본 도구 per-array 합 `sionna_backend.py:843-848` vs AODT per-element `P^RU/(n·N_pol·N_hor·N_vert)`). 이미 내보내는 좌표 스팟체크(`rfdata_export.py:196`)를 지오메트리-프레임 체크로 유지.

---

## 출처

- [1] 3GPP TR 38.901 V17.0.0, Table 7.4.1-1 및 Note 1~6, §7.8 — https://panel.castle.cloud/view_spec/38901-h00/pdf/
- [2] FSPL/Friis 용어집 — https://ib-lenhardt.com/kb/glossary/fspl
- [3] Free-space path loss (Wikipedia) — https://en.wikipedia.org/wiki/Free-space_path_loss
- [4] 열잡음 kTB — https://rfattenuator.net/tools/thermal-noise
- [5] 간섭성 대역폭 1/(2πστ) — https://bpb-us-e1.wpmucdn.com/sites.gatech.edu/dist/c/488/files/2016/09/22-CoherenceBandwidth.pdf ; RMS 지연확산 vs Bc — https://www.mdpi.com/1424-8220/20/3/750
- [6] mmWave 경로손실 모델 서베이 (CI/close-in) — https://arxiv.org/pdf/1708.02557
- [7] T. S. Rappaport, *Wireless Communications: Principles and Practice* (two-ray, CI, K-factor, PDP)
- [8] D. Tse & P. Viswanath, *Fundamentals of Wireless Communication* (MRT/SVD, 스티어링 벡터, Shannon)
- [9] A. Goldsmith, *Wireless Communications* (Shannon, two-ray, K-factor)
- [10] Remcom Wireless InSite — https://www.remcom.com/wireless-insite-propagation-software
- [11] NYURay 보정/검증 (npj Wireless Technology) — https://www.nature.com/articles/s44459-025-00014-x
- [12] NVIDIA AODT — RAN Digital Twin (CFR/CIR/raypaths) — https://docs.nvidia.com/aerial/aerial-dt/text/ran_digital_twin.html
- [13] Sionna RT: Differentiable Ray Tracing for Radio Propagation Modeling (arXiv:2303.11103) — https://arxiv.org/abs/2303.11103
- [14] Ns3 meets Sionna: Using Realistic Channels in Network Simulation (arXiv:2412.20524) — https://arxiv.org/html/2412.20524v1
- [15] Ray-Tracing Calibration from Channel Sounding Measurements in a Millimeter-Wave Industrial Scenario, EuCAP 2024 (arXiv:2404.10590) — https://arxiv.org/abs/2404.10590
- [16] 3GPP TR 38.901 §7.8 캘리브레이션 (레퍼런스 결과 R1-165974/R1-165975/R1-1700990/R1-1909704) — https://panel.castle.cloud/view_spec/38901-h00/pdf/
- [17] 38.901 InH 모델 vs 측정 (σ_SF < 0.6 dB) (arXiv:2504.15589) — https://arxiv.org/pdf/2504.15589
- [18] 3GPP TS 38.215, "NR; Physical layer measurements" (RSRP/RSSI/RSRQ 정의) — https://www.3gpp.org/DynaReport/38215.htm

**미검증 항목 정리:**
- Sionna RT FSPL "<0.01 dB / <1 ns" 정량 임계값 — 참조 문헌 [14] 본문에서 수치 미확인 **(미검증)**.
- NVIDIA/Keysight AODT vs 측정 수용 RMSE 수치 — 공식 문서 미발견 **(미검증)**.
- Sionna RT vs OAI 5G 테스트베드 검증의 구체 arXiv ID/수치 — 원 노트의 식별자 미확인 **(미검증)**.
- NYURay 리뷰 PDF의 arXiv 미러(2507.22027) 식별자 — 원 노트 값 미확인, npj 정식 링크 [11] 로 대체 인용.
- 반사계수 ≤ 1 에너지 불변식 — 인용 도구에 형식적 수용기준으로 명시된 바 없음(권장 테스트) **(미검증)**.

**저장소 근거:** `backend/app/services/channel_analysis.py` (FSPL `:59-67`, 38.901 `:93-221`, CI `:75-83`, CIR/CFR/DS `:271-360`, delta-vs-RT `:247-260`, RSRP/RSSI/RSRQ `:539-609`); `backend/app/services/calibration.py:71-204`; `backend/app/services/simulation_backends/sionna_backend.py` (엔진 디스패치 `:326,:350`, 경로별 전력 `:882`, 소자전력 합 `:843-848`, 배열 `:159-189`, 잡음 `:302`, 스티어링/코드북/MRT/SVD `:49-85, :693-699`); `plugins/example_two_ray/plugin.py:104-123`; `backend/app/services/rfdata_export.py:196-217`.
