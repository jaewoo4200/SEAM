# 동적 산란 (Dynamic Scattering)

본 문서는 (1) 실제 채널 물리에서 "동적 산란" 이 무엇인지(출처 기반), (2) 상용/연구 도구가 이를 어떻게 모델링하는지, (3) 본 리포지토리(SionnaTwin Studio)에 sionna-rt 2.0.1 정확 API로 구현하는 단계별 계획(효과/비용 순위 포함), 그리고 (4) 실측 검증된 Doppler how-to 스니펫을 담는다.

모든 sionna-rt 2.0.1 API 주장은 (a) 설치 소스 `backend/.venv/Lib/site-packages/sionna/rt/` 를 읽어, (b) `backend/.venv/Scripts/python.exe` 로 라이브 프로브를 돌려 검증했다. 패키지 버전 `2.0.1` 확인(`sionna/rt/__init__.py:9`). 공식/피어리뷰 출처로 확인되지 않은 항목은 **(미검증)** 으로 표기한다.

---

## 1. 동적 산란이란 무엇인가 (물리)

"Dynamic scattering" 은 단일 정의어가 아니라, RF 채널 문헌에서 **Tx/Rx 대비 상대운동하는 물체(산란체)로 인한 전파 채널의 시변 성분** 을 가리킨다. 물리적으로 구분되는 3가지 하위효과가 있다.

1. **이동 산란체 → 시변 다중경로 기하.** 차량/보행자/수목이 움직이면 프레임마다 레이 상호작용점 집합이 바뀌어, 경로 지연·각도·진폭이 시간에 따라 변한다. "기하 재계산(geometry re-solve)" 효과.
2. **산란체 운동에 의한 Doppler 확산.** 이동하는 단일 상호작용점은 바운스당 Doppler `f_Δ = (1/λ)·vᵀ(k_out − k_in)` 를 부여한다. 다수 이동 산란체 클러스터는 0 Hz 주변으로 Doppler **확산(spread)** 을 만들어 Doppler 파워스펙트럼을 넓히고 채널 간섭성 시간을 단축한다(Tx/Rx가 정지해도). 고전 Clarke/Jakes 스펙트럼(이동 Rx)의 산란체-운동 아날로그.
3. **시변 확산 다중경로(diffuse).** 거친 표면은 비정반사(diffuse) 성분을 재방사한다. 거친 표면 또는 조사 패치가 움직이면 확산 구름이 요동친다 — 밀집 산란환경(수목·군중·차량)의 지배적 페이딩 기구이며, "dynamic diffuse scattering" 이 특정하는 대상.

**측정 캠페인의 특성화 방식(피어리뷰):**
- **Effective Roughness (ER) 모델** — Degli-Esposti 외, IEEE TAP 2007 [1]. 산란계수 S ∈ [0,1] 과 Lambertian/directive/backscatter 로브 패턴 정의 → Sionna가 구현하고 인용(`cite:p:Degli-Esposti07`).
- **mmWave directive 모델 파라미터화** — 건축재료 측정 α_R, Int. J. Antennas Propag. 2020 [2].
- **ITU-R P.2040**(건축재료·구조 영향), **ITU-R P.1411**(단거리 실외)은 S·유전율 시드에 쓰는 재료 전기 파라미터·확산 산란 가이드 제공 — *절(clause) 단위 원문은 본 작업에서 미인출* **(미검증)**.
- Doppler 확산/간섭성 시간은 고전 **Clarke/Jakes** 프레임(이동 단말)을 이동 산란체로 확장; T_c ≈ 0.42/f_d,max.

---

## 2. 도구들은 어떻게 모델링하는가

### 2.1 Sionna RT 2.0.1 (설치 패키지 대조 검증)

아래 주장은 모두 `backend/.venv/Lib/site-packages/sionna/rt/` 에 대해 검증됨.

**(A) 확산 산란 모델** — `radio_materials/scattering_pattern.py`:
- `LambertianPattern` (`fs = cos θ_o / π`, `:201-232`).
- `DirectivePattern(alpha_r)` — 정반사 방향 주변 로브(`:394-416`).
- `BackscatteringPattern(alpha_r, alpha_i, lambda_)` — 정규화 이중 로브(`:234-392`).
- 이름 등록: `"lambertian"`, `"directive"`, `"backscattering"`; 직접 import 가능: `from sionna.rt import LambertianPattern, DirectivePattern, BackscatteringPattern`.
- `RadioMaterial`(`radio_material.py`): `scattering_coefficient`(S, `:220-233`), `xpd_coefficient`(K_x ∈ [0,1], `:235-249`, 범위검증 후 XPD Jones 행렬 재구성), `scattering_pattern`(`ScatteringPattern` 인스턴스여야, `:251-263`). 확산 에너지는 `scattering_coefficient > 0` **그리고** 솔버의 `diffuse_reflection=True` 일 때만 생성.

**(B) 물체 속도 → 경로별 Doppler (핵심 동적산란 API)**:
- `SceneObject.velocity` — settable `mi.Vector3f` [m/s] (`scene_object.py:252-283`); 메쉬의 `"velocity"` `rawconstant` 텍스처 속성으로 저장. 기본 `(0,0,0)`.
- `Transmitter`/`Receiver` 도 `velocity=` 인자(`radio_devices/radio_device.py:50,109-119`).
- 필드 계산기가 상호작용 물체 속도를 `shape.eval_attribute_3("velocity", …)` 로 읽어 바운스당 Doppler `v_effective = (k_out − k_in)·v_world; doppler += v_effective/wavelength` 누적(`field_calculator.py:526-562`). **정반사·확산·굴절 모두** 적용.
- Tx/Rx 종단항은 `paths.py:_finalize_doppler_shift_compute`(`:1215-1246`)에서: `doppler = paths_buffer.doppler + tx_doppler − rx_doppler`.
- 결과는 `Paths.doppler` [Hz/path] (`paths.py:336-385`), 다중바운스 `f_Δ = (1/λ)[v₀ᵀk₀ − v_{n+1}ᵀk_n + Σ vᵢᵀ(kᵢ−k_{i-1})]`.

**(C) Doppler로부터 채널 시간전개**:
- `Paths.cir(sampling_frequency=1.0, num_time_steps=1, normalize_delays=True, reverse_direction=False, out_type="drjit")` (`paths.py:387-393`). `num_time_steps>1` 이면 경로별 `aᵇ_i(t) = aᵇ_i · e^{j2π f_Δ,i t}` 적용(`paths.py:506-516`) — **한 번의 solve 안에서** Doppler만으로 시계열 합성. `Paths.cfr(...)` 는 주파수영역 동일. 2.x 에는 **별도 `apply_doppler` 없음** — `cir`/`cfr` 에 접힘.

**(D) 솔버 플래그** — `PathSolver.__call__`(`path_solvers/path_solver.py:144-157`): `diffuse_reflection: bool = False`, `specular_reflection`, `refraction`, `diffraction`, `edge_diffraction`, `max_depth=3`, `samples_per_src=1_000_000`, `synthetic_array=True`, `seed=42`. 확산 경로는 몬테카를로 샘플 → seed 의존, 충분한 `samples_per_src` 필요.

문서: 산란 튜토리얼 [4], radio materials [5], Paths API [6], 기술보고서 [7].

### 2.2 Remcom Wireless InSite
동일 **ER 계열**(Lambertian, directive, directive-with-backscatter) 을 산란계수 S·교차편파 분율로 구현 — Degli-Esposti 모델이 검증된 참조 구현. 동적 씬은 이동 물체에 대해 시간스텝마다 전파를 재실행; 운동 Doppler는 프레임 간 경로기하 변화에서 유도 [8].

### 2.3 NVIDIA AODT (Aerial Omniverse Digital Twin)
명시적 **"Enable Dynamic Scatterers"** 플래그로 이동 차량의 EM 산란을 켬. 일관된 UE+차량 모빌리티, Omniverse 기하+운동 데이터로부터 시변 채널/Doppler 계산. 본 설계목표에 정신적으로 가장 근접 — 프레임별 재계산 + 물체속도, GPU 가속 [9][10].

### 2.4 MATLAB (Communications/Antenna Toolbox)
`raytrace`(SBR/image)는 반사/회절/확산 산란 지원; `comm.RayTracingChannel` 은 **Tx/Rx 속도를 Doppler로** 적용, 모빌리티 트랙에서 레이셋 재사용. 다만 경로별 *산란체* 속도 Doppler는 Sionna의 `SceneObject.velocity` 처럼 일급 입력이 아님 [11][12].

---

## 3. 본 도구의 현재 상태 (저장소 검증)

- **이동 액터 프레임별 재계산은 이미 연결됨.** `apply_actor_states()`(`sionna_backend.py:226-292`)가 시나리오 프레임마다 `SceneObject.position`·`.orientation` 설정; 솔버는 프레임당 1회 실행(`:555-572`). → 효과 #1(시변 기하) 이미 생성.
- **확산 플래그 전달됨:** `diffuse_reflection=config.scattering`(`:564`).
- **재료:** `_apply_custom_materials` 는 `scattering_coefficient` 만 설정(`:797-802`). `xpd_coefficient` / `scattering_pattern` 은 미설정.
- **누락 물리(Gap):**
  - `apply_actor_states` 가 `obj.velocity` 를 절대 설정하지 않음 → **오늘자로 이동 액터의 Doppler는 항등적으로 0**.
  - `.cir(num_time_steps=…)` 미호출 → 프레임내 시간전개 없음; 경로는 곧장 `RayPath` 로 변환.
  - Lambertian/directive `alpha_r`·XPD 미노출.
- **시간 기반 존재:** `dt_s` 가 액터 궤적 스키마(`schemas/scene.py:149`)·시뮬 설정(`schemas/simulation.py:109`) 양쪽에 있고 `TrajectorySample.time_s = i*dt_s`(`services/trajectory.py:98`) — 프레임별 속도 `(pₙ₊₁ − pₙ)/dt_s` 직접 도출 가능.

---

## 4. 구현 계획 (효과/비용 순위)

권장 순서: **A → B → C** (A가 Doppler를 여는 최소작업, B는 패턴/XPD 최소작업, C가 둘을 합쳐 완전한 dynamic-diffuse 산출물 구성).

### Design A — 이동 액터의 속도기반 경로별 Doppler (최고가치 / 최저비용)

**새 물리:** 효과 #2 — 액터 운동의 진짜 경로별 Doppler 시프트, 간섭성 시간·Doppler 확산·시간전개 CIR 출력 가능. 오늘 최대 갭(Doppler=0) 해소.

**정확 API 변경(모두 2.0.1 검증):**
1. `apply_actor_states`(`sionna_backend.py:226`) 에서 액터별 속도 계산·설정. 속도 = `(state.position − prev_state.position)/dt_s`, 또는 궤적 접선 × 속력. 이후:
   ```python
   import mitsuba as mi
   obj.velocity = mi.Vector3f(vx, vy, vz)   # m/s, world frame (scene_object.py:266)
   ```
   `dt_s` 는 이미 시나리오에 존재; 직전 프레임 액터 위치(또는 브래킷 웨이포인트 2개)를 `apply_actor_states` 로 전달.
2. (선택) 궤적 추종 Tx/Rx도 동일: `Transmitter(..., velocity=...)` / `rx.velocity = ...`(`radio_device.py:109`).
3. Solve 후 `solved.doppler`(`paths.py:336`)를 읽어 `RayPath` 로 전달(`doppler_hz` 필드 추가), 또는 시간전개 CIR 방출:
   ```python
   a_real, a_imag, tau = solved.cir(
       sampling_frequency=config.bandwidth_hz or 1/dt_s,
       num_time_steps=N, out_type="numpy")
   ```

**비용:** ~0.5–1일. 신규 의존성 없음(속도는 뺄셈). 주작업은 직전프레임 위치를 `apply_actor_states` 로 스레딩 + `doppler_hz` 스키마 필드/변환 1줄.

**주의:** `SceneObject.velocity` 는 액터가 **개별 주소화**(동일재료 지오메트리와 병합되지 않음)여야 함 — 코드가 이미 병합 액터에 경고(`sionna_backend.py:266-270`), 동일 가드 적용.

### Design B — 산란패턴 + XPD 노출 (중간가치 / 저비용)

**새 물리:** 확산 로브의 *형상*·*편파* 제어(효과 #3). Lambertian(완전확산)~directive(α_R 큰 준정반사) 범위 + 교차편파 결합. 실제 mmWave 거친표면 거동과 측정 α_R 보정에 필요.

**정확 API 변경(2.0.1 검증):** 커스텀 재료 블록(`sionna_backend.py:797`)·재료 스키마(`schemas/materials.py`) 확장:
```python
from sionna.rt import LambertianPattern, DirectivePattern, BackscatteringPattern
pat = custom.get("scattering_pattern")          # "lambertian" | "directive" | "backscattering"
if pat == "directive":
    rt_mat.scattering_pattern = DirectivePattern(alpha_r=int(custom["alpha_r"]))
elif pat == "backscattering":
    rt_mat.scattering_pattern = BackscatteringPattern(
        alpha_r=int(custom["alpha_r"]), alpha_i=int(custom["alpha_i"]),
        lambda_=float(custom["lambda_"]))
else:
    rt_mat.scattering_pattern = LambertianPattern()
xpd = custom.get("xpd_coefficient")
if xpd is not None:
    rt_mat.xpd_coefficient = float(xpd)          # radio_material.py:243, validates [0,1]
```
Setter 검증: `scattering_pattern`(`radio_material.py:259`, `ScatteringPattern` 타입체크), `xpd_coefficient`(`:243`).

**비용:** ~0.5일. 순수 재료 배관 + 신규 스키마 3필드(`scattering_pattern`, `alpha_r/alpha_i/lambda_`, `xpd_coefficient`). 기존 `scattering_coefficient`·`diffuse_reflection` 과 결합.

### Design C — 프레임별 확산 solve + 코히런트 시계열로 시변 확산채널 (고가치 / 고비용)

**새 물리:** 완전한 "dynamic diffuse scattering" — 효과 #1+#2+#3 동시. 프레임간 *및* 프레임내 시변 채널, 다수 이동 확산산란체의 현실적 Doppler 확산. AODT "dynamic scatterers"·Remcom dynamic diffuse 가 제공하는 것.

**구성(A+B 기반):**
1. 전 프레임 확산 활성화(`diffuse_reflection=True`, `scattering_coefficient>0` 및 Design B 패턴 지정).
2. 프레임마다 액터 **속도** 설정(Design A) → 확산 상호작용점이 Doppler 운반(`field_calculator.py:550` 이 `InteractionType.DIFFUSE` 포함 모든 상호작용 유형의 velocity 읽음, `:256-300` 검증).
3. 프레임마다 짧은 코히런트 시계열 방출: `solved.cir(sampling_frequency=fs, num_time_steps=N)`(`paths.py:387`) → 프레임내 Doppler 전개; 프레임 간 연접해 전체 시변 CIR.
4. 확산경로 안정화를 위해 `samples_per_src`(`path_solver.py:148`) 상향, 궤적 재현성 위해 프레임별 `seed` 고정.
5. `solved.doppler` 에서 Doppler 확산/간섭성 시간 지표 도출(RMS Doppler = |a|² 가중 경로별 `doppler` 표준편차), 기존 RMS 지연확산처럼 `TrajectorySample`(`services/trajectory.py:104`)에 노출.

**비용:** ~2–4일. 비용 동인: 확산 몬테카를로가 프레임당 solve 시간·메모리 증가(`samples_per_src`/`max_depth` 튜닝); 연접 시계열 출력 스키마·Doppler 확산 집계 설계; 확산 경로수가 `[:100]` 경로 캡(`trajectory.py:93`)을 넘지 않는지 검증. 가장 많은 물리, 가장 무거운 연산.

### 순위 요약

| Design | 새 물리 효과 | 가치 | 비용 | 선행 |
|--------|-------------|------|------|------|
| **A** 속도기반 Doppler | #2 | 최고 | ~0.5–1일 | 없음 |
| **B** 산란패턴+XPD | #3 | 중간 | ~0.5일 | 없음 |
| **C** 시변 확산채널 | #1+#2+#3 | 최고(종합) | ~2–4일 | A, B |

---

## 5. 검증된 Doppler How-To (sionna-rt 2.0.1)

**실측 검증:** 빈 씬 순수 LOS 프로브, 3.5 GHz, RX가 TX로 30 m/s 접근 → `paths.doppler == 350.2423 Hz`, 정확히 `v/λ = 30/0.085655 = 350.2423`. 접근 → 양(+) Doppler. 이 머신에서 Mitsuba는 `cuda_ad_mono_polarized` variant로 해결(CUDA 존재); CPU 폴백 시 나오는 LLVM-init stderr 경고는 무해. Doppler 수식은 Wiffen 외 2018 인용(`paths.py:363`).

### 5.1 속도 설정 지점 (독립 3소스)

| 엔티티 | API | 소스 (file:line) |
|--------|-----|------------------|
| Transmitter/Receiver (기저 `RadioDevice`) | `velocity` 생성자 인자 **및** `.velocity` get/set; `mi.Vector3f` [m/s] | `radio_devices/radio_device.py:35,50,109-119` |
| Scene object (임의 메쉬, 예: 차량) | `SceneObject.velocity` get/set; `mi.Vector3f` [m/s] | `scene_object.py:253-283` |

라이브 검증 노트:
- `RadioDevice.velocity` 기본 `Vector3f(0,0,0)`(`radio_device.py:68-69`). setter가 입력을 `mi.Vector3f` 로 래핑 → 파이썬 리스트 허용: `Transmitter(..., velocity=[10,0,0])`, `tx.velocity = mi.Vector3f(5,0,0)` 모두 성공.
- `SceneObject.velocity` 는 `"velocity"` `rawconstant` 텍스처 속성으로 지연 백킹(`scene_object.py:274-283`). 최초 설정 전엔 `Vector3f(0.)` 반환(`:261-262`). 물체당 **단일** 벡터만 허용(`assert dr.width(v)==1`, `:269`).
- 생성자 시그니처(프로브): `Transmitter.__init__(self, name, position, orientation=None, look_at=None, velocity=None, power_dbm=..., color=..., display_radius=None)`; `Receiver.__init__(self, name, position, orientation=None, look_at=None, velocity=None, color=..., display_radius=None)`.

### 5.2 PathSolver가 속도로부터 계산하는 것

- 경로당 1개 Doppler → **`Paths.doppler`**(`paths.py:335-385`).
- **속도 설정은 tx/rx 정적위치·기하/레이트레이싱을 바꾸지 않음** — Doppler 채널에만 영향(기하 스냅샷; 시간전개는 Doppler로 해석적 합성).
- 두 기여: (1) 이동 물체 — 필드계산에서 각 상호작용이 물체 `"velocity"` 속성 읽어 `v·(k_out − k_in)/λ` 누적(`field_calculator.py:526-562`, `_update_doppler_shift`); 정적물체는 0. (2) 이동 tx/rx — `paths.py:1215-1246`: `f_Δ = paths_buffer.doppler + (k_tx·v_tx)/λ − (k_rx·v_rx)/λ`, `k_tx=r̂(θ_t,φ_t)`(출발), `k_rx=−r̂(θ_r,φ_r)`(도착). 디바이스 속도는 `scene.sources()/targets()` 가 `.velocity` 에서 취득.
- **`paths.doppler` 형상**(`mi.TensorXf`, Hz): synthetic array(기본) `[num_rx, num_tx, num_paths]`, non-synthetic `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths]`. 프로브: 단일안테나 tx/rx over `simple_street_canyon_with_cars` → `(1,1,10)`.

### 5.3 시간전개 CIR — 주 산출 API

`Paths.cir(*, sampling_frequency=1.0, num_time_steps=1, normalize_delays=True, reverse_direction=False, out_type="drjit")`(`paths.py:387-524`).

경로·시간스텝별 기저대역 계수(`paths.py:404-405`):
```
a^b_i(t) = a_i · e^{−j2π f τ_i} · e^{ j2π f_Δ,i t},   t = n/sampling_frequency,  n = 0..N−1
```
Doppler 위상항은 **`num_time_steps > 1` 일 때만** 적용(`paths.py:505-520`); 기본 `num_time_steps=1` 이면 정적 스냅샷이라 velocity가 `cir` 출력에 안 보임(단 `paths.doppler` 는 항상 채워짐).

- **반환** `(a, tau)`: `a` = real/imag 쌍(drjit) 또는 단일 복소 배열(numpy/tf/torch/jax), 형상 `[num_rx, num_rx_ant, num_tx, num_tx_ant, num_paths, num_time_steps]`. `tau` = 지연[s], `[num_rx, num_tx, num_paths]`(synthetic).
- 프로브: 정적 `a=(1,1,1,1,10,1)`; `num_time_steps=16` → `a=(1,1,1,1,10,16)`, `complex64`; 크기 일정·위상 회전(마지막 스텝이 첫 스텝과 다름 확인).
- `out_type`: `"drjit"`(기본)/`"numpy"`/`"jax"`/`"tf"`/`"torch"`.

관련 헬퍼(둘 다 `sampling_frequency`/`num_time_steps` 로 Doppler 전개 동일):
- **`Paths.taps(bandwidth, l_min, l_max, sampling_frequency=None, num_time_steps=1, ...)`** — 이산시간(TDL) CIR, `[..., num_time_steps, l_max−l_min+1]`(`paths.py:526-`). 프로브 `(1,1,1,1,16,11)`, complex64.
- **`Paths.cfr(frequencies, sampling_frequency=1.0, num_time_steps=1, ...)`** — 채널 주파수응답 `[..., num_time_steps, num_frequencies]`(`paths.py:660-`).

파라미터 선택: `sampling_frequency` 는 CIR 리샘플링율. 최고 Doppler를 에일리어싱 없이 해상하려면 `sampling_frequency ≥ 2·max|f_Δ|`; 전개 창 길이 = `num_time_steps / sampling_frequency` [s]. OFDM 슬롯이면 `sampling_frequency = subcarrier_spacing`(또는 `1/slot_duration`) 관례.

### 5.4 최소 실행 스니펫 (백엔드 venv 대조 검증)

```python
import sionna.rt as rt
from sionna.rt import Transmitter, Receiver, PlanarArray, PathSolver, load_scene
import mitsuba as mi
import numpy as np

scene = load_scene(rt.scene.simple_street_canyon_with_cars)
scene.frequency = 3.5e9  # Hz; scene.wavelength 자동 설정

scene.tx_array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization="V")
scene.rx_array = PlanarArray(num_rows=1, num_cols=1, pattern="iso", polarization="V")

# (a) 이동 라디오 디바이스 — 속도 m/s, Z-up world frame
tx = Transmitter("tx", position=[-30, 0, 10], velocity=[0, 0, 0])
rx = Receiver("rx", position=[30, 0, 1.5], velocity=[30, 0, 0])  # RX 30 m/s +x
scene.add(tx); scene.add(rx)

# (b) 이동 씬 물체(예: 차량 메쉬) — velocity 속성 설정
for name, obj in scene.objects.items():
    if "car" in name.lower():
        obj.velocity = mi.Vector3f(20.0, 0.0, 0.0)

# 경로 solve (velocity는 기하 불변, Doppler만 영향)
paths = PathSolver()(scene, max_depth=3)

# 경로별 Doppler [Hz], 형상 [num_rx, num_tx, num_paths] (synthetic array)
doppler_hz = paths.doppler.numpy()
print("Doppler [Hz]:", doppler_hz.ravel())

# 시간전개 기저대역 CIR: a[num_rx,num_rx_ant,num_tx,num_tx_ant,num_paths,num_time_steps]
fs = 1000.0          # CIR sampling frequency [Hz] -> window = num_time_steps/fs [s]
num_time_steps = 16
a, tau = paths.cir(sampling_frequency=fs, num_time_steps=num_time_steps, out_type="numpy")
print("a:", a.shape, a.dtype, "| tau:", tau.shape)  # a: (...,16) complex64
```

**프로젝트 불변식(Z-up, m/s):** 속도 벡터는 위치와 동일 world frame, `[vx, vy, vz]` 에서 `vz` 수직. solve 전 `scene.frequency` 설정해야 λ(따라서 Doppler) 정확 — `scene.wavelength` 는 파생·읽기전용.

### 5.5 함정 (검증/소스 기반)

- 기본 `num_time_steps=1` ⇒ `cir`/`taps`/`cfr` 에 Doppler 위상 미적용(`paths.py:505`); 디바이스가 움직여도 시간전개 보려면 `num_time_steps > 1` 필수.
- `paths.doppler` 는 `num_time_steps` 무관하게 항상 계산 — 전개 없이 원 시프트 읽기 가능.
- `SceneObject.velocity` 는 최소 1회 설정해야 저장/미분가능(`scene_object.py:256-257`); 이전엔 `Vector3f(0.)`.
- 비상대론 근사: 1차 테일러(‖v‖ ≪ c); 정확식·유도는 `Paths.doppler` docstring(`paths.py:365-381`).
- `reverse_direction=True` 는 `cir`/`taps`/`cfr` 에서 tx/rx 역할 교환(uplink/downlink 재사용).

**관련 파일(절대경로):**
- `C:/Users/jaewoo/custom-dt-tool/backend/.venv/Lib/site-packages/sionna/rt/radio_devices/radio_device.py`
- `C:/Users/jaewoo/custom-dt-tool/backend/.venv/Lib/site-packages/sionna/rt/scene_object.py`
- `C:/Users/jaewoo/custom-dt-tool/backend/.venv/Lib/site-packages/sionna/rt/path_solvers/paths.py`
- `C:/Users/jaewoo/custom-dt-tool/backend/.venv/Lib/site-packages/sionna/rt/path_solvers/field_calculator.py`
- `C:/Users/jaewoo/custom-dt-tool/backend/.venv/Lib/site-packages/sionna/rt/scene.py`

---

## 출처

- [1] Degli-Esposti 외, "Measurement and Modelling of Scattering From Buildings," IEEE TAP 2007 (Sionna ER 인용 `cite:p:Degli-Esposti07`) — DOI 10.1109/TAP.2007.897329
- [2] "Diffuse Scattering Directive Model Parameterization Method for Construction Materials at mmWave Frequencies," Int. J. Antennas Propag. 2020 — https://www.hindawi.com/journals/ijap/2020/1583854/
- [3] ITU-R P.2040(재료 EM 파라미터) · ITU-R P.1411(단거리 실외) — *절 단위 원문 미인출* **(미검증)**
- [4] Sionna RT 산란 튜토리얼 — https://nvlabs.github.io/sionna/rt/tutorials/Scattering.html
- [5] Sionna RT Radio Materials API — https://nvlabs.github.io/sionna/rt/api/radio_materials.html
- [6] Sionna RT Paths API (doppler, cir/taps/cfr) — https://nvlabs.github.io/sionna/rt/api/paths.html ; Radio devices — https://nvlabs.github.io/sionna/rt/api/radio_devices.html
- [7] Sionna RT Technical Report, arXiv:2504.21719 — https://arxiv.org/pdf/2504.21719
- [8] Remcom Wireless InSite — Diffuse Scattering — https://www.remcom.com/wireless-insite-em-propagation-software/diffuse-scattering
- [9] NVIDIA AODT — RAN Digital Twin (Dynamic Scatterers) — https://docs.nvidia.com/aerial/aerial-dt/text/ran_digital_twin.html
- [10] Simulate an Accurate Radio Environment using NVIDIA AODT — https://developer.nvidia.com/blog/simulate-an-accurate-radio-environment-using-nvidia-aerial-omniverse-digital-twin/
- [11] MATLAB `comm.RayTracingChannel` — https://www.mathworks.com/help/comm/ref/comm.raytracingchannel-system-object.html
- [12] Mobility Modeling with Ray Tracing Channel — https://www.mathworks.com/help/comm/ug/mobility-modeling-with-ray-tracing-channel.html

**저장소 근거(설치 Sionna RT 2.0.1, repo 검증):** `backend/.venv/Lib/site-packages/sionna/rt/scene_object.py:252-283`, `radio_devices/radio_device.py:50-119`, `radio_materials/scattering_pattern.py:201-416`, `radio_materials/radio_material.py:220-263`, `path_solvers/path_solver.py:144-157`, `path_solvers/field_calculator.py:256-300,526-562`, `path_solvers/paths.py:336-524,660-,1215-1246`, `scene.py:1055,1073-1078`. **통합 지점:** `backend/app/services/simulation_backends/sionna_backend.py:226-292,555-572,748-805`; `backend/app/services/trajectory.py:75-108`; `backend/app/schemas/scene.py:147-177`; `backend/app/schemas/simulation.py:109`.

**미검증 항목:** ITU-R P.2040/P.1411 절 단위 원문(재료/확산 가이드 참조로만 인용, 미인출) **(미검증)**.

---

## 구현 완료 (Design A — 속도기반 경로별 Doppler + 시변 CIR)

본 절의 계획 중 **Design A**(효과 #2: 속도기반 경로별 Doppler)를 구현했다. Design B(산란패턴/XPD)·C(프레임내 확산 시계열)는 후속 작업으로 남긴다. 아래 API 는 모두 설치된 sionna-rt 2.0.1 에 대해 라이브 프로브로 재검증했다.

### 사용한 정확 API (재검증)

- `Transmitter(..., velocity=[vx,vy,vz])` / `Receiver(..., velocity=...)` 생성자 인자, 및 `.velocity` 세터(`mi.Vector3f` 래핑) — `radio_devices/radio_device.py:45-71,108-119`. 라이브 확인: 빈 씬 순수 LOS, 3.5 GHz, RX 가 TX 로 30 m/s 접근 → `paths.doppler == 350.2423 Hz == v/λ = 30/0.085655`. 접근 → 양(+) Doppler.
- `SceneObject.velocity = mi.Vector3f(...)` (액터 메쉬) — `scene_object.py:252-283`. 물체당 단일 벡터, 기하 불변.
- `Paths.doppler` — 경로별 Doppler [Hz], synthetic array 형상 `[num_rx, num_tx, num_paths]`(`paths.py:335-385`). `num_time_steps` 무관하게 항상 채워짐.
- `Paths.cir(*, sampling_frequency, num_time_steps, out_type="numpy")` — 시변 CIR(`paths.py:387-524`). `num_time_steps>1` 일 때만 `a·e^{j2π f_Δ t}` 적용. 프로브: `a` 형상 `(...,num_time_steps)` complex64, 크기 일정·위상 회전, 스텝당 위상 = `2π f_Δ/fs` 정확 일치.

### 구현 내역 (파일별)

- **`backend/app/schemas/devices.py`** — `Device.velocity_m_s: Optional[Vec3] = None`(world frame m/s, Z-up). None=정지, 기하/레이트레이싱 불변.
- **`backend/app/schemas/channel.py`** — `ChannelAnalysisRequest` 에 `num_time_steps: int(1..64, 기본 1)`, `sampling_frequency_hz: Optional[float]`(None→Nyquist=2·max|f_Δ|, 무운동시 1 kHz) 추가. `CirTap.doppler_hz: Optional[float]`. `ChannelAnalysisResult` 에 `doppler_spread_hz`, `mean_doppler_hz`, `max_doppler_hz`, `coherence_time_ms`(≈0.42/max|f_Δ|), `cir_time_s`, `cir_time_envelope_db`(시변 페이딩 포락 `|Σ_i a_i e^{j2π f_Δ,i t}|` dB) 추가.
- **`backend/app/services/simulation_backends/sionna_backend.py`** — Transmitter/Receiver 생성 시 `velocity_m_s` 통과. `apply_actor_states(..., velocities=)` 인자로 액터별 `obj.velocity` 설정. `simulate_paths`/`_simulate_paths_impl` 에 `actor_velocities` 옵션 kwarg. `_convert_paths` 가 `solved.doppler` 를 읽어 유지된 경로와 1:1 정렬된 리스트 반환 → 무언가 움직일 때만 `PathResultSet.metadata["doppler_hz"]` 로 노출(정적 solve 는 바이트 동일 유지). RayPath 스키마는 소유 밖이라 metadata 로 운반.
- **`backend/app/services/channel_analysis.py`** — `doppler_metrics()`(파워가중 mean/spread/max, coherence time), `doppler_time_envelope()`(백엔드 무관, per-path power/phase/doppler 로 시변 포락 합성 — `paths.cir` 와 동일 모델). `build_cir(paths, doppler_by_path_id)` 로 탭별 `doppler_hz` 충전. `analyze_channel` 이 `metadata["doppler_hz"]` 를 path_id 로 매핑해 링크 필터·지연 정렬을 건너뛰어 정렬 유지.
- **`backend/app/services/trajectory.py`** — 웨이포인트 유한차분 `(wp[i+1]-wp[i])/dt`(마지막점은 후방차분)로 UE 속도 도출→ 이동 RX 의 `velocity_m_s` 설정. 웨이포인트별 Doppler 확산을 `metadata["doppler_spread_hz"]`(samples 정렬 리스트)로 노출.
- **`backend/app/services/scenario.py`** — `actor_velocity_at()`(궤적 접선 중앙차분 = 접선×속력) 추가. 프레임별 액터 속도 + 부착 디바이스 속도(액터 속도 상속)를 solve 로 전달. 프레임별 Doppler 확산을 `ScenarioResultSet.metadata["doppler_spread_hz"]` 로 노출. (LinkMetrics/ScenarioFrame/TrajectorySample 스키마는 소유 밖이라 metadata 채널 사용.)
- **`backend/tests/test_doppler.py`** (신규) — 스키마 속도 필드, Doppler 스펙트럼 수식(손계산), 시변 포락 리플, 서비스 속도 배관(캡처 페이크 백엔드로 sionna 불요), sionna-guarded 실솔브(이동 RX Doppler ≈ v/λ, 정적 링크는 doppler_hz 미노출, 채널분석 Doppler 지표 충전) 18 케이스.

### 추가된 스키마 필드 (프론트엔드 타입 미러링용)

- `Device.velocity_m_s: [vx,vy,vz] | null` (m/s, Z-up world frame).
- `ChannelAnalysisRequest.num_time_steps: int`, `.sampling_frequency_hz: float | null`.
- `CirTap.doppler_hz: float | null`.
- `ChannelAnalysisResult`: `doppler_spread_hz`, `mean_doppler_hz`, `max_doppler_hz`, `coherence_time_ms`, `cir_time_s: float[]`, `cir_time_envelope_db: float[]` (모두 이동체 없으면 None/[]).
- `TrajectoryResultSet.metadata.doppler_spread_hz: (float|null)[]` (samples 정렬), `ScenarioResultSet.metadata.doppler_spread_hz: (float|null)[]` (frames 정렬), `PathResultSet.metadata.doppler_hz: float[]` (paths 정렬, 이동시에만).

### 테스트 결과

`tests/test_doppler.py tests/test_sionna_backend.py tests/test_channel_analysis.py tests/test_scenario.py` → 전부 통과(57 passed). 전체 스위트는 `test_render.py`(본 작업 무관, Mitsuba 전역 플러그인 상태 오염으로 후행 sionna GPU 테스트를 무너뜨리는 기존 테스트격리 문제)를 제외하면 **249 passed, 2 skipped, 0 failed**. `test_render.py` 는 velocity/doppler/simulate_paths/channel/trajectory/scenario 코드를 전혀 참조하지 않으며, 신규 테스트 파일을 제외해도 동일 6건이 실패하므로 본 변경의 회귀가 아니다.
