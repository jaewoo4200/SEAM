# Sionna 버전별 기능·재질·모델링 변천사 (검증 문헌)

본 문서는 Sionna 및 Sionna RT의 버전별 기능·재질·물리 모델링 변천사를 무선통신 연구자를 위해 정리한 검증 레퍼런스입니다. 근거는 공식 소스(NVIDIA/NVlabs GitHub 릴리스 노트·Discussion, 공식 문서, PyPI 메타데이터, 각 태그의 `pyproject.toml`, arXiv 원논문·기술보고서)로 한정했으며, 모든 클레임은 1차 소스를 재확인(re-fetch)하는 적대적 팩트체크 절차를 거쳤습니다. 검증에서 WRONG으로 판정된 클레임은 수정본만 채택했고, 공식 소스로 확인 불가능한 항목은 본문에 싣되 "(미검증)"으로 명시했습니다. 날짜는 절대날짜(YYYY-MM-DD)를 사용했으며, 릴리스 날짜는 원칙적으로 PyPI 업로드 시각을 기준으로 삼았습니다.

## 버전 요약 매트릭스

| 버전(대표) | 출시 | RT 기반 | 주요 기능 추가 | 재질 모델 | 주의점 |
|---|---|---|---|---|---|
| 0.14.0 | 2023-03-20 | TensorFlow + Mitsuba 3 | RT 최초 도입: 경로(paths)·커버리지맵(coverage map)·CIR, LoS+정반사(Fresnel) [1][2][3] | ITU-R P.2040-2, εᵣ=a·f^b / σ=c·f^d, μᵣ=1 [3] | 도입 시점엔 회절·산란·RIS·이동성 없음 [1] |
| 0.15.0 | 2023-07-11 | TensorFlow + Mitsuba 3 | 1차 회절(UTD), 확산 산란(Lambertian/Directive/Backscattering) [4] | 산란계수 S, XPD Kₓ 추가 (P.2040-2) [4][8] | 파괴적 변경: 커버리지맵 재정의, Paths가 계수 반환 [4] |
| 0.17.0 | 2024-04-25 | TensorFlow + Mitsuba 3 | 이동성/도플러(velocity·apply_doppler), 로드 후 위치·방향 수정 [5] | 변동 없음 | 이동성만 추가, 파괴적 변경 없음 [5] |
| 0.18.0 | 2024-06-11 | TensorFlow + Mitsuba 3 | RIS 최초 지원(정확 경로+커버리지맵) [6] | 변동 없음 | 메모리 사용량 증가(0.19에서 수정) [6] |
| 0.19.2 | 2025-02-25 | TensorFlow + Mitsuba 3 | (0.19.0) SINR/RSS 맵, 셀-TX 연관, CDF, bandwidth·temperature·전력(W/dBm) [7] | ITU-R P.2040 (era 전환기) | TF-era 마지막 RT 릴리스. 회절/RIS 필요 시 여기 고정 [7][9] |
| 1.0.0 (sionna-rt) | 2025-03-18 | Dr.Jit + Mitsuba 3 | 전면 재작성, 독립 패키지화, PathSolver/RadioMapSolver, 굴절/투과, 로드 후 편집, NumPy/TF/PyTorch/JAX 상호운용 [9][10] | ITU-R P.2040-3, BSDF 기반 [11] | 회절·RIS 제거("향후 릴리스"); 필요 시 0.19 사용 [9][10] |
| 1.1.0 (sionna-rt) | 2025-06-05 | Dr.Jit + Mitsuba 3 | 임의 메시 기반 라디오맵, 객체 복제, 메시 변환 유틸 [10][12] | 재질 미분성 NaN 그래디언트 수정 [10] | Mitsuba 3.6.2/Dr.Jit 1.0.3 유지 [13] |
| 1.2.0 (sionna-rt) | 2025-09-19 | Dr.Jit + Mitsuba 3 | 1차 회절 재도입(edge·lit-region 플래그), 경로 솔버 개선 [10][12] | 변동 없음 | Python 3.8/3.9 제거; deps=Mitsuba 3.7.1/Dr.Jit 1.2.0 [13] |
| 1.2.2 (sionna-rt) | 2026-03-19 | Dr.Jit + Mitsuba 3 | Mitsuba 3.8.0/Dr.Jit 1.3.1, ARM-Linux pip, 라디오맵 메모리 절감 [10][13] | 재질 미분성 개선 [10] | (동일자 2.0.0은 이 변경을 누락) [10] |
| 2.0.1 (sionna-rt) | 2026-03-31 | Dr.Jit + Mitsuba 3 | 2.0.0 회귀 복원, Mitsuba 3.8.0/Dr.Jit 1.3.1 복귀. PHY/SYS는 PyTorch 이전(2.0.0) [10][13][14] | ITU-R P.2040-3 [11] | RT 자체는 기능 변경 없음(버전 동기화); RIS는 여전히 미지원 [10][15] |

## 시대별 상세

### TensorFlow era: 0.14 – 0.19 (2023-03 ~ 2025-02)
이 시기 RT는 모놀리식 `sionna` 패키지 내부의 TensorFlow 확장으로, Mitsuba 3 렌더러 위에 삼각형 메시로 구축되었으며 미분성은 TensorFlow 자동미분에서 나옵니다. [3]

- **0.14.0 — 2023-03-20**: Sionna RT 최초 도입. "무선 전파 모델링을 위한 세계 최초의 완전 미분 가능 레이 트레이서". 출시 시점 기능: 미분 가능한 전파 경로 계산(`compute_paths`), 미분 가능한 **커버리지맵(첫날부터 존재)**, 경로→CIR 계층, 임의 위치·방향의 송수신 장치, 임의 안테나 배열·패턴, 주파수 종속 커스텀 무선재질+ITU 재질, 3D 뷰어. 도입 시점엔 LoS+정반사(Fresnel)만 지원하고 회절·산란·RIS·이동성은 없음. Dockerfile 기본 TF 2.11. [1][2][3]
- **0.15.0 — 2023-07-11**: **1차 회절**(송신기→쐐기→수신기)과 **확산 산란**(Lambertian/Directive/Backscattering) 추가. 재질에 산란계수 도입. 초기 광선 방향 샘플링을 Fibonacci lattice로 교체. 3GPP TR38901 편파 Model-1 지원. 파괴적 변경: 커버리지맵 재정의, `Paths`가 전이행렬 대신 계수 반환, `Paths.cir()` 추가. [4]
- **0.16.0 — 2023-11-28**: 경로 추적과 EM 필드 계산 분리(`trace_paths()` + `compute_fields()`, graph mode 실행 가능). 재질/산란패턴 커스터마이징용 콜러블 객체. `sim_ber()`용 다중 GPU(`tf.distribute`, PHY 측). 요구사항: TF 2.10–2.13, Python 3.9–3.11. 파괴적 변경: 지연 정규화 범위 변경, `RadioMaterial`/`Transmitter`/`Receiver`의 `trainable_*` 플래그 제거. [4] (0.16.2: 커버리지맵이 안테나 전체 에너지 합산 방식으로 변경)
- **0.17.0 — 2024-04-25**: 각 씬 객체에 **속도 벡터** 부여→경로별 도플러 시프트. `Paths.apply_doppler()`로 CIR 시간 진화. 로드 후 `position`/`orientation` 수정 가능(이동성). 파괴적 변경 없음. [5]
- **0.18.0 — 2024-06-11**: **RIS(재구성 가능 지능형 표면) 최초 지원** — 정확 경로와 커버리지맵 모두 계산 가능. 물리 기반 재복사 모델(Degli-Esposti 2022 / Vitucci 2024). [6]
- **0.19.0 — 2024-09-30**: `CoverageMap`에 **SINR·RSS** 맵 추가(경로이득 외), 셀-TX 연관·CDF 시각화. `Scene`에 `bandwidth`·`temperature`(열잡음), `Transmitter`에 송신전력(W·dBm). shoot-and-bounce 다중 실행, DFT 빔그리드, 재현성 seed 제어. 파괴적 변경: `CoverageMap.as_tensor()` → `path_gain`/`rss`/`sinr` 프로퍼티. [7]
- **0.19.1 — 2024-11-29**: Mitsuba 요구사항을 ≥3.2.0 & <3.6.0으로 조임, `Scene.mi2sionna_shift_obj_id` 제거. [7]
- **0.19.2 — 2025-02-25**: 커버리지맵 솔버의 RIS 관련 이슈 수정. **TF-era 마지막 RT 릴리스**이며, 회절/RIS가 필요한 사용자의 종착 버전. [7][9]

### 1.0 전환 (2025-03)
2025-03-18 "Announcing Sionna 1.0" 발표와 함께 모놀리식 패키지가 세 모듈로 재편되었습니다. [9]

- **패키지 분리**: **Sionna PHY**(비-RT 물리계층; 임포트가 `sionna.channel...`→`sionna.phy.channel...`로 변경), **Sionna SYS**(신규 시스템 레벨: PHY 추상화·링크 적응·전력제어·스케줄링), **Sionna RT**(독립 저장소 `sionna-rt` + PyPI `sionna-rt`). 배포: `pip install sionna`(전체)/`sionna-rt`(RT만)/`sionna-no-rt`(RT 제외). [9]
- **프레임워크 이야기(혼동 금지)**: RT는 TensorFlow를 벗어나 "Dr.Jit + Mitsuba 3로 처음부터 재작성"되었고 NumPy/TF/PyTorch/JAX와 상호운용됩니다(RT 자체는 TF/PyTorch 프로그램이 아니라 Dr.Jit/Mitsuba 래퍼). 반면 1.0 시점의 **PHY/SYS는 여전히 TensorFlow 기반**이었고, 구 Keras `Layer` 대신 프레임워크 불가지론적 Sionna `Block` 아키텍처로 교체되었습니다. [9][14]
- **신규 API(파괴적)**: `scene.compute_paths()`/`scene.coverage_map()` 제거 → **`PathSolver`/`RadioMapSolver`** 클래스로 대체. 로드 후 씬 편집, 정반사+확산반사에 더해 **굴절/투과** 1급 지원. [9][14]
- **버전 태그 주의**: `sionna-rt` 저장소에는 1.0.0 태그가 존재하나(2025-03-18), 상위 `sionna` 우산 패키지는 0.19.2→1.0.1로 점프하여 **`sionna` 저장소에는 v1.0.0 태그가 없음**. [9][14]
- **기능 회귀**: 1.0 재작성으로 **회절과 RIS가 제거**됨. 릴리스 노트: "회절과 RIS는 향후 릴리스에서 추가된다. 이 기능이 필요한 사용자는 최신 0.19 릴리스를 사용하라." [9][10]

### sionna-rt 1.x – 2.x (현재)
독립 `sionna-rt` 라인의 각 태그 `pyproject.toml` 및 릴리스 노트 기준. [10][13]

- **1.0.2 — 2025-04-03**: AoA 계산 이슈 수정, 반환 텐서 간 경로 순서 불일치 수정. [10]
- **1.1.0 — 2025-06-05**: **임의 메시 라디오맵**(임의 지형 포함), 씬 객체 복제, 메시 로드·변환 유틸. 라디오맵 미분 시 NaN 그래디언트 수정, 커스텀 재질 로드 실패(#879) 수정. deps 불변(Mitsuba 3.6.2/Dr.Jit 1.0.3). [10][12][13]
- **1.2.0 — 2025-09-19**: **★ 1차 회절 재도입** — 경로 계산과 라디오맵 모두. edge diffraction과 lit-region diffraction을 각각 켜고 끄는 플래그. 경로 솔버가 더 많은 경로를 더 적은 메모리로. 뷰어에서 좌표 확인·클리핑 슬라이더, 라디오맵 커스텀 컬러맵. **deps: Mitsuba 3.7.1, Dr.Jit 1.2.0**(릴리스 산문은 "Dr.Jit 1.1"이라 적었으나 pyproject 핀이 authoritative — 1.2.0이 정답). Python 3.8/3.9 제거(`requires-python`→`>=3.10`). [10][12][13]
- **1.2.1 — 2025-10-16**: RT 기능 변경 없음(상위 `sionna` 1.2.1과 버전 동기화). (GitHub 릴리스 페이지의 `published_at 2026-02-26`은 백필 아티팩트이며 실제 PyPI 릴리스는 2025-10-16.) [10][13]
- **1.2.2 — 2026-03-19**: **★ Mitsuba 3.8.0 + Dr.Jit 1.3.1**, ARM-Linux pip 설치 지원(DGX Spark). RX 안테나 패턴 오계산 수정, `PathBuffer.path_counter` 버그, 라디오맵 메모리 절감, 재질 미분성 개선, XML 재질 색상 보존. [10][13]
- **2.0.0 — 2026-03-19**: RT는 "버전 추적 목적, 기능 변경 없음". 2.0 메이저 범프는 상위 `sionna` **PHY/SYS의 PyTorch 이전**(TensorFlow 의존성 제거)에 기인. **★ 불량 릴리스**: 작성자가 릴리스 페이지에서 "이 릴리스는 실수로 v1.2.2 변경을 누락했다. v2.0.1을 사용하라"고 명시. deps가 Mitsuba 3.7.1/Dr.Jit 1.2.0으로 회귀. **사용 금지.** [10][14]
- **2.0.1 — 2026-03-31** (현재 최신): 2.0.0이 누락한 1.2.2 변경 전부 복원(**Mitsuba 3.8.0/Dr.Jit 1.3.1** 핀 포함). 재질 `<bsdf>` name 누락 시 명시적 오류, `Scene.render()` 컬러바 cmap 반영 수정. **이것이 올바른 설치에서 sionna-rt 2.0.1 + Mitsuba 3.8.0 + Dr.Jit 1.3.1로 나타나는 이유.** 런타임 deps(2.0.1 pyproject): `mitsuba==3.8.0`, `drjit==1.3.1`, `matplotlib>=3.10`, `scipy>=1.14.1`, `numpy>=1.26`, `ipywidgets>=8.1.5`, `pythreejs>=2.4.2`; `requires-python>=3.10`. [10][13]

## 재질·물리 모델링 상세

### ITU-R P.2040 개정판 (재현성의 핵심 차이)
- **개정판 전환**: TF-era 원논문(2023, 0.14–0.19)은 **ITU-R P.2040-2**(2021-09)를 인용하고, 현재 문서(1.x/2.x)는 **ITU-R P.2040-3**(2023-09)를 인용합니다. 동일 명칭 재질이라도 -2 vs -3 계수에 따라 εᵣ/σ가 미세하게 달라질 수 있으므로 논문에는 Sionna 버전과 P.2040 개정판을 함께 명기해야 합니다. [3][11]
- **정확한 전환 버전(미검증)**: -2→-3 전환이 일어난 정확한 0.x 마이너는 공식 소스로 특정하지 못했습니다. 양 끝점만 확인됨(원논문=-2, 현재 문서=-3)이며 0.19→1.0 전환기로 경계 지어지나, 아카이브된 0.19.2 문서 스냅샷 확인 전까지는 정확한 마이너 단위로는 미검증입니다.
- **파라미터화**: εᵣ = a·f_GHz^b, σ = c·f_GHz^d (S/m), 복소 상대유전율 η = εᵣ − j·σ/(ε₀·ω). `scene.frequency` 변경 시 자동 갱신. **비자성 재질(μᵣ=1)만** 지원. [3][11]
- **내장 재질 목록**: concrete, brick, plasterboard, wood, glass, ceiling_board, chipboard, plywood, marble, floorboard, metal, very_dry_ground, medium_dry_ground, wet_ground, vacuum. glass와 ceiling_board만 별도의 고대역(220–450 GHz) 계수 세트를 가짐. **지반 재질(very_dry/medium_dry/wet_ground)은 1–10 GHz로 제한** — mmWave 옥외 작업 시 이 대역 밖에서는 P.2040 유효범위 초과. [11]
- **계수 수치(부분 미검증)**: 대표 a,b,c,d 값(예: concrete a=5.24/c=0.0462/d=0.7822; metal c=10⁷; glass 6.31/0.0036/1.3394 및 고대역 5.79/0.0004/1.658)은 문서 요약으로 확인했으나 개별 숫자 단위는 라이브 테이블 직접 렌더 전까지 미검증이므로 게시 전 대조 권장.
- **클래스 계층**: `RadioMaterialBase`(추상) → `RadioMaterial`(εᵣ, σ, 두께 d, 산란계수 S, XPD Kₓ, 산란패턴) → `ITURadioMaterial`(주파수 파라미터화 ITU 모델). 재질은 Mitsuba의 BSDF로 취급되며 `<bsdf type="itu-radio-material">` 형태로 씬 XML에 등록, `mi.register_bsdf(...)`로 커스텀 등록. [11]

### 산란 (Lambertian / Directive / Backscattering)
- 세 확산 산란 패턴 모두 Degli-Esposti et al.(2007) 기반: Lambertian(f_s=cos(θ_s)/π), Directive(정반사 방향 집중, 지수 α_R), Backscattering(directive 로브+역산란 로브, α_I·Λ). [8]
- 산란계수 S∈[0,1]: 확산 산란 에너지 비율 = S², 정반사는 R²=1−S²로 감소. XPD_s = 10·log₁₀((1−Kₓ)/Kₓ). [8]
- 가용성: TF-era 0.15에서 도입, 1.0 재작성 이후에도 유지(정반사+확산반사+굴절). 1.x 솔버에서 InteractionType 비트마스크: `NONE=0, SPECULAR=1, DIFFUSE=2, REFRACTION=4, DIFFRACTION=8`. [4][8][10][16]

### 회절 (버전 민감도 최상)
- **드롭-복원 패턴**(연구자 필수 인지): TF-era 0.15에서 1차 회절 도입(0.16에서 NaN 버그 수정) → **1.0 재작성에서 제거** → **1.2.0(2025-09-19)에서 재도입**(경로+라디오맵, edge·lit-region 플래그). [4][9][10]
- **이론**: UTD(Kouyoumjian-Pathak 1974, 유한도전 쐐기에 대한 Luebbers 1984 확장), ITU-R P.526과 정합. 회절 광선은 Keller cone 상에 위치. 개념적 프레이밍은 GTD/Keller지만 구현 모델은 UTD(미검증: 현재 UTD 계수 정식화가 TF-era와 수치적으로 동일한지는 미확인 — 재작성 시 폐형해를 재유도). [3][11][17]
- **차수 제한**: 모든 era에서 **1차 회절만**(단일 쐐기). 한 경로는 회절 이벤트를 최대 1회만 포함하며, 회절과 확산반사를 동시에 포함할 수 없음. 고차/다중 에지 회절은 어떤 릴리스에도 없음. [11][17]
- **플래그 명칭 차이**: 0.x API(`diffraction`, `edge_diffraction`)와 1.2+ API(edge + lit-region)의 플래그 세트가 다름. [4][12]

### 투과 / 굴절
- **굴절/투과는 1.0 재작성 신규 기능**: 1.0.0에서 정반사+확산반사에 더해 굴절 지원. TF-era 원논문은 반사·회절 중심이며 지오메트리를 통한 굴절 광선 모델을 기술하지 않음(미검증: 0.19 이전 부분 투과/투과손실 모델 존재 여부는 확인 불가). [9][10][17]
- **단일층 슬래브 모델**(ITU-R P.2040-3): 반사/투과 필드는 두께 d를 반영하되(내부 다중반사 포함 Fresnel-슬래브, Jones 행렬 R(d)/T(d)), 투과 광선은 기하학적 굴절 편향 없이 단일 광선으로 추적. 벽은 단일 평면 표면으로 모델링 권장. 다층/복합 벽은 네이티브 미지원 — 하나의 유효 재질+두께로 근사. [11][17]

### 안테나·편파
- **내장 패턴(현재 2.x)**: `iso`, `dipole`(단쌍극자, Balanis 4-26a), `hw_dipole`(반파장, Balanis 4-84), `tr38901`(3GPP TR 38.901 Table 7.3-1). [18]
- **편파**: "V"(0), "H"(π/2), "VH"(듀얼), "cross"(±π/4). 두 편파 모델: `polarization_model_tr38901_1`(방향 종속 구면 회전), `..._2`(직접 슬랜트 스케일링). `register_antenna_pattern()`/`register_polarization()`으로 커스텀 확장. [18]
- TF-era 원논문도 "tr38901"·"dipole" 패턴을 참조. (미검증: `hw_dipole`와 패턴 레지스트리 API가 0.14–0.19에서 동일 명칭으로 존재했는지는 2.x만 확인.) [3][18]
- **정확도 주의**: 안테나는 원역장(far-field) 패턴만으로 모델링 — Fraunhofer 경계 내부(근역장)에서 부정확하며 검증 오차원. [18][19]

### 미분가능성 (Dr.Jit vs 구 TensorFlow)
- **TF-era**: TensorFlow 자동미분으로 재질(εᵣ, σ)·안테나 패턴·배열 지오메트리·Tx/Rx 위치·방향에 대해 미분 가능. RIS 구성은 미분 불가(원논문 future work). [3]
- **Dr.Jit era**: Dr.Jit 자동미분으로 CIR·라디오맵 등에 대해 완전 미분 가능. 지오메트리 미분성은 교점 재매개변수화(∂t/∂x, ∂t/∂φ를 Dr.Jit AD로 획득)를 사용. 재작성을 관통해 미분성을 보존하며 대폭 가속. v1.1.0·v1.2.2에서 라디오맵/재질 그래디언트(NaN) 버그가 수정되며 Dr.Jit 경로가 성숙. (미검증: 보고서에 TF-vs-DrJit 그래디언트 정면 비교는 없음.) [3][10][11]
- **캘리브레이션**(미검증 문헌 기반, 저자 제공 수치): "Learning Radio Environments by Differentiable Ray Tracing"(arXiv:2311.18558)은 εᵣ, σ, S, Kₓ와 미분 가능 안테나/산란 패턴, "neural materials"(MLP)를 DICHASUS 채널 사운더(3.438 GHz)로 검증. 평균 절대 전력오차: ITU 4.93 dB → learned 2.16 dB → neural 1.00 dB. (저자 보고 수치이므로 독립 검증은 미완.) [19]

## 연구 유형별 권장 버전 가이드

- **RIS 연구**: TF-era **0.18 – 0.19.2만** 사용. RIS는 1.0 재작성에서 제거되었고 "향후 릴리스"로 남아 있으며 **2.0.1(2026년 중반)까지 어떤 RT 릴리스 노트도 RIS 재도입을 발표하지 않음** — 현재 2.0.1 문서도 RIS를 RT 기능으로 명시하지 않음. (RIS 복귀를 긍정하는 공식 진술을 찾지 못함; 미검증으로 표기.) 종착 버전 0.19.2. [6][9][10][15]
- **미분 가능 캘리브레이션**: TF 파이프라인이면 0.19.x, 현대 Dr.Jit AD·속도·편집성이 필요하면 **≥1.1.0**(NaN 그래디언트 수정 반영), 재질 미분성 개선이 필요하면 **≥1.2.2 / 2.0.1**. 재질·안테나·배열·위치에 대한 그래디언트는 두 era 모두 지원. [3][10][11]
- **대규모 커버리지/라디오맵**: SINR·RSS·셀 연관·CDF가 필요하면 0.19.0+. 임의 지형/메시 라디오맵은 **≥1.1.0**, 메모리 효율은 **≥1.2.2**. `RadioMapSolver` 기반. [7][10][12]
- **ISAC/센싱·RCS(미검증 영역)**: RCS는 어떤 버전에서도 `RadioMaterial`의 1급 파라미터가 아니며 지오메트리+산란계수/패턴에서 창발되는 값(미검증: 내장 RCS API 없음). RCS 워크플로는 광선 샘플링 기반 커뮤니티/공식 논의(Discussion #844)와 관련 문헌(arXiv:2505.08754, 2411.03206)을 참조. 미분성·확산산란이 안정적인 **≥1.2.0**을 권장하되, RCS 프리미티브 부재를 전제로 설계할 것. [20][21]
- **레거시 재현성**: 발표된 결과를 재현하려면 정확한 Sionna 버전 + P.2040 개정판을 고정. TF-era(P.2040-2) 결과는 **0.19.2**로, 재작성 이후(P.2040-3) 결과는 해당 1.x/2.x 태그로 재현. 회절 필요 시 ≤0.19.2 또는 ≥1.2.0(1.0.0–1.1.0 회피). [3][7][11]

## 우리 툴(SionnaTwin Studio, 현재 sionna-rt 2.0.1)에의 시사점

이 변천사는 "엔진 버전 교체" 기능이 선택이 아니라 필수임을 다음 근거로 뒷받침합니다.

1. **기능 세트가 버전에 걸쳐 단조 증가하지 않음(드롭-복원)**: 회절은 0.15에서 도입 → 1.0에서 제거 → 1.2.0에서 복원되었고, RIS는 0.18–0.19.2에만 존재하고 2.0.1까지 미복귀입니다. 따라서 단일 고정 엔진으로는 특정 물리(회절·RIS)를 요구하는 연구를 커버할 수 없으며, 사용자가 연구 목적에 맞춰 엔진 버전을 선택·전환할 수 있어야 합니다. [4][6][9][10][15]
2. **RIS/회절 레거시 워크플로는 오직 0.19.2에만 존재**: 공식 가이드가 "이 기능이 필요하면 0.19 릴리스를 사용하라"고 명시합니다. RIS 연구자를 지원하려면 TF-era(TensorFlow) 엔진과 Dr.Jit-era 엔진을 나란히 운용할 수 있어야 하며, 이는 프레임워크(TF vs PyTorch/Dr.Jit)가 다른 두 스택의 병존을 의미합니다. [9][10]
3. **재현성은 정확한 (엔진 버전 + P.2040 개정판) 조합에 종속**: P.2040-2↔-3 전환으로 동일 재질의 εᵣ/σ가 달라질 수 있으므로, 발표 결과를 재현하려면 논문이 쓴 정확한 엔진 태그로 되돌릴 수 있어야 합니다. 버전 고정·전환 기능이 없으면 레거시 재현성이 깨집니다. [3][11]
4. **의존성 핀이 태그마다 다르고 회귀 사례 존재**: sionna-rt 2.0.0은 실수로 1.2.2 변경을 누락해 "v2.0.1을 사용하라"는 경고가 붙었고, Mitsuba/Dr.Jit 핀이 태그별로 상이합니다(1.0.0=3.6.2/1.0.3 … 2.0.1=3.8.0/1.3.1). 엔진 교체 기능은 각 엔진 버전을 검증된 정확한 의존성 핀과 함께 격리 설치·관리해야 함을 시사합니다. [10][13][14]
5. **플랫폼·프레임워크 요구가 시대별로 이동**: Python 하한이 상승(3.8/3.9는 1.2.0에서 제거), PHY/SYS는 2.0.0에서 PyTorch로 이전(TF 의존성 제거), ARM-Linux는 1.2.2부터 지원됩니다. 특정 사용자 환경(GPU/OS/파이썬/프레임워크)에 맞는 엔진 버전을 선택·전환하는 능력이 툴의 이식성과 수명을 좌우합니다. [13][14]

## 출처 목록

1. Sionna v0.14 발표 (Discussion #105): https://github.com/NVlabs/sionna/discussions/105
2. Sionna RT 논문 (Hoydis et al., arXiv:2303.11103): https://arxiv.org/abs/2303.11103
3. Sionna RT 논문 HTML (ar5iv): https://ar5iv.labs.arxiv.org/html/2303.11103
4. Sionna v0.15 발표 (Discussion #166): https://github.com/NVlabs/sionna/discussions/166
5. Sionna v0.17 발표 (Discussion #415): https://github.com/NVlabs/sionna/discussions/415
6. Sionna v0.18 발표 (Discussion #479): https://github.com/NVlabs/sionna/discussions/479
7. Sionna v0.19 발표 (Discussion #605) 및 릴리스: https://github.com/NVlabs/sionna/discussions/605 · https://github.com/NVlabs/sionna/releases
8. EM Primer (산란·편파): https://nvlabs.github.io/sionna/rt/em_primer.html
9. Announcing Sionna 1.0 (Discussion #776): https://github.com/NVlabs/sionna/discussions/776
10. sionna-rt 릴리스: https://github.com/NVlabs/sionna-rt/releases
11. RadioMaterial 문서 (ITU-R P.2040-3): https://nvlabs.github.io/sionna/rt/api/radio_materials.html
12. sionna-rt 태그 릴리스 페이지 (v1.1.0/v1.2.0): https://github.com/NVlabs/sionna-rt/releases/tag/v1.2.0
13. 태그별 pyproject.toml 및 PyPI JSON: https://raw.githubusercontent.com/NVlabs/sionna-rt/&lt;tag&gt;/pyproject.toml · https://pypi.org/pypi/sionna-rt/json
14. sionna v2.0.0 릴리스 (PyTorch 이전): https://github.com/NVlabs/sionna/releases
15. Sionna RT 기술보고서 (arXiv:2504.21719, RIS 각주·버전표): https://arxiv.org/abs/2504.21719 · https://arxiv.org/html/2504.21719v2
16. Paths API (InteractionType): https://nvlabs.github.io/sionna/rt/api/paths.html
17. Diffraction 튜토리얼: https://nvlabs.github.io/sionna/rt/tutorials/Diffraction.html
18. Antenna Pattern API: https://nvlabs.github.io/sionna/rt/api/antenna_pattern.html
19. 캘리브레이션 논문 (arXiv:2311.18558): https://ar5iv.labs.arxiv.org/html/2311.18558
20. RCS 계산 논의 (Discussion #844): https://github.com/NVlabs/sionna/discussions/844
21. RCS 특성화 문헌: https://arxiv.org/pdf/2505.08754 · https://arxiv.org/pdf/2411.03206

## 부록: 본 리포 로컬 실측 검증 (2026-07-03)

위 문헌 조사와 별개로, 이 저장소의 실제 설치본에서 다음을 직접 확인했다.

- `backend/.venv` (builtin): `sionna-rt 2.0.1 + mitsuba 3.8.0 + drjit 1.3.1` — 매트릭스의 2.0.1 행과 일치.
- `backend/.venv-sionna-rt-122`: `sionna-rt 1.2.2 + mitsuba 3.8.0 + drjit 1.3.1` — 1.2.2 행과 일치.
- 두 버전 모두 `PathSolver.__call__`에 `specular_reflection / diffuse_reflection / refraction / diffraction / edge_diffraction / diffraction_lit_region` 파라미터 존재(1.2.0의 회절 재도입 및 edge·lit-region 플래그 클레임과 정합). `RadioMapSolver`도 동일한 회절 플래그 3종을 노출.
- PyPI 업로드 날짜 실측(`pypi.org/pypi/sionna-rt/json`): 1.0.0=2025-03-18, 1.1.0=2025-06-05, 1.2.0=2025-09-19, 1.2.2=2026-03-19, 2.0.0=2026-03-19(동일자), 2.0.1=2026-03-31 — 매트릭스의 6개 날짜 전부 일치.
- 교차 엔진 물리 일관성: 동일 lab_room 씬(28 GHz, max_depth 3)에서 builtin 2.0.1과 1.2.2 서브프로세스 엔진이 각각 62개 경로, 최강 경로 −13.84 dBm @ 9.737 ns로 일치.

엔진 교체 사용법은 [engines.md](engines.md) 참조.
