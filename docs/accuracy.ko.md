# RT 정확도: 우려 사항과 완화 방안

> 🌐 [English](accuracy.md) · **한국어**

통신 분야 연구에 따르면 레이 트레이싱(Sionna RT 포함)은 보정하지 않으면
측정값과 5–15 dB까지 어긋날 수 있습니다. 이 문서는 주요 오차 원인과
이에 대한 SEAM Studio의 대응을 정리합니다.

## 주요 오차 원인 (문헌 기반)

1. **재질 EM 파라미터** — 가장 지배적이며 상관관계가 없는 오차. ITU-R P.2040
   프리셋은 제한된 대역에서만 정의된 모집단 평균값이라 실제 표면은
   이 값에서 벗어납니다. 보정 전 경로 손실 오차는 ~5–15 dB가 흔합니다.
2. **확산 산란(diffuse scattering)** — 대부분의 RT에서 기본으로 꺼져 있지만, 28 GHz에서는
   확산 에너지가 수신 전력의 ~20–40%에 달할 수 있습니다. 이를 무시하면 NLOS를 과소 추정합니다.
3. **기하 충실도(geometry fidelity)** — 가구/차량/식생 누락과 거친 메시는
   TX/RX 근처에서 큰 국소 오차(수십 dB, 수백 ns)를 유발합니다.
4. **위상/코히런스(phase/coherence)** — RT 위상은 파장 이하의 기하 정밀도(28 GHz에서 ~1 mm)를
   요구하며, 벽 위치 오차는 간섭을 뒤집고 크기 피팅을 편향시킵니다.
5. **안테나 모델** — 등방성으로 가정하면 패턴 이득/롤오프와
   교차 편파가 누락됩니다.
6. **굴절/투과 및 회절** — 벽 투과 경로와
   고차 회절은 종종 꺼져 있거나 1차까지만 고려됩니다.
7. **몬테카를로 분산** — `samples_per_src`가 너무 적으면 확산 경로가 낮게 편향됩니다.

## 이 도구가 현재 구현하는 것

- **확산 산란 대응 재질.** `RFMaterial.scattering_coefficient`는
  측정 기반 값(콘크리트/벽돌 ~0.2)으로 설정되어 Sionna `RadioMaterial`에
  반영되며, 실행마다 `SimulationConfig.scattering`으로 활성화합니다(→ `diffuse_reflection=True`).
- **대역 외 가드레일.** ITU 지면 재질을 ~10 GHz 이상에서 사용하면
  `validate_scene`가 `MATERIAL_OUT_OF_BAND`를 발생시켜 `ground_28ghz`를 가리키고,
  Sionna 백엔드도 솔브 시점에 경고합니다(`_frequency_warnings`).
- **측정 보정** (`POST /calibrate/materials`,
  `services/calibration.py`). 측정된 링크별 경로 이득을 임포트하면 도구가
  동일한 링크를 시뮬레이션해 **레벨 오프셋**(미지의 절대 TX 전력을 흡수)과
  잔차 **RMSE/MAE**(재질 피팅으로 보정할 수 있는 형상 오차)를 계산하고,
  RMSE를 최소화하도록 **재질 파라미터 하나를 그리드 탐색**
  (`scattering_coefficient` / `relative_permittivity` / `conductivity_s_per_m`)한
  뒤 전/후를 보고합니다. `apply=true`이면 피팅 값이
  라이브러리에 기록되고 프림은
  `assignment_status: measurement_calibrated`로 승격됩니다.
- **주파수 인식 기본값** — 기본 28 GHz, ITU 재질과 상수 재질 구분.

## 솔버 / 정확도 프리셋

솔버 노브를 제대로 맞추는 것은 재질 선택만큼이나 중요한 정확도 지렛대입니다.
샘플이 너무 적으면 확산 경로가 낮게 편향되고, `max_depth`가 너무 얕으면 NLOS
바운스가 누락되며, 메커니즘을 잘못 고르면(산란/굴절/회절 꺼짐)
커버리지가 수십 dB까지 어긋날 수 있습니다. 모든 노브를 사용자에게 맡기는 대신,
`SolverControls`는 대표적인 배치 시나리오에 맞춰 일관된 솔버 노브 세트와 라디오맵
그리드를 묶은 **명명된 프리셋**(`frontend/src/configPresets.ts`)을
제공합니다:

| 프리셋 | 주파수 | max_depth | 메커니즘 | 그리드 셀 |
|---|---|---|---|---|
| **28 GHz Indoor Lab** | 28 GHz | 5 | reflection + refraction + scattering | 0.25 m |
| **28 GHz Outdoor Campus** | 28 GHz | 3 | reflection + scattering | 2.0 m |
| **3.5 GHz Urban Macro** | 3.5 GHz | 4 | reflection + refraction + diffraction | 5.0 m |
| **60 GHz Indoor** | 60 GHz | 4 | reflection + refraction | 0.25 m |

프리셋을 선택하면 경로 설정과 라디오맵 그리드에 **모두** 패치가 적용되고,
사용자의 백엔드/TX/RX 선택은 건드리지 않습니다. 프리셋은 고정된 `SimulationConfig`
와이어 타입에 이미 존재하는 키만 설정하므로 드리프트가 발생할 여지가 없습니다.
프리셋이 다루는 노브를 직접 편집하면 드롭다운이 **Custom**
("명명된 프리셋과 일치하지 않음"을 뜻하는 센티넬)으로 바뀝니다. 실내 프리셋은
의도적으로 굴절을 켜고(벽 투과 전송이 실내 NLOS를 지배하기 때문) 더
세밀한 0.25 m 그리드를 쓰며, 실외/도심 프리셋은 넓은 면적을 커버하기 위해
깊이와 그리드 해상도를 희생합니다. 이 프리셋들은 출발점일 뿐 보정된 그라운드 트루스가 아닙니다.
남은 재질 오차는 측정 보정(위)을 실행해 좁히십시오.

## 계획된 다음 단계

- **미분 가능(Adam) 보정.** Sionna RT는
  `relative_permittivity`, `conductivity`, `scattering_coefficient`,
  `xpd_coefficient`에 대해 미분 가능합니다. 이들을 Dr.Jit로 학습 가능하도록 표시하고 PathSolver를 실행한 뒤,
  전력 + RMS 지연 확산 손실(SMAPE/NMSE)을 Adam으로 최소화합니다. 발표된 결과에서는
  경로 손실 오차가 ~4.9 dB → ~1.0 dB로, 지연 확산 오차가 ~54% → ~13%로 줄었습니다.
  Mode-2 재질 그룹화는 이미 재질별 파라미터 공유를 기본 제공하므로,
  홀드아웃 오차를 측정할 학습/검증 분할만 추가하면 됩니다.
- **지향성 산란 패턴** — 재질마다 `scattering_pattern`
  (Lambertian / Directive / Backscattering + `alpha_r`)을 추가합니다. mmWave에서 지향성
  로브는 Lambertian보다 건물 표면에 훨씬 잘 맞습니다.
- **현실적인 안테나 패턴** — 장치별 `antenna.pattern`
  (`tr38901`/`dipole`)과 편파를 솔버 배열에 연결합니다(장치에는 이미
  설정이 있음).
- **검증 리포트** — 측정 CIR과 비교한 경로 이득 오차·RMS 지연 확산·Rician K·CDF,
  그리고 몬테카를로 분산을 한정하기 위한 수렴 검사(`samples_per_src`를
  두 배 / 두 번째 시드).
