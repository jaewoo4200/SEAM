# RF 재질

> 🌐 [English](rf_materials.md) · **한국어**

RF 재질은 RF 투영(projection)과 레이 트레이싱 백엔드가 소비하는 전자기
표면 기술(description)입니다. 이는 시각/PBR 재질과 의도적으로 분리되어
있습니다. RF 재질이 지니는 유일한 외형(cosmetic) 필드는 `preview_color`이며,
이는 프런트엔드의 RF 오버레이 모드를 구동할 뿐 전자기적 의미는 없습니다.

## 라이브러리 파일

앱은 내장 라이브러리를
`backend/app/data/default_rf_materials.yaml`에 함께 배포합니다. 프로젝트를
생성하면 이 파일이 `<project>/rf/materials.yaml`로 복사되며, 프로젝트는 이를
편집하고 확장할 수 있습니다. 그 이후로는 해당 프로젝트에 대해 프로젝트
파일이 권위를 갖습니다(이 파일이 없으면 내장 기본값이 사용됩니다).

형식(`backend/app/schemas/materials.py`의 `RFMaterialLibrary` /
`RFMaterial`):

```yaml
materials:
  - id: itu_concrete                  # ^[a-z0-9_]+$
    display_name: ITU Concrete
    category: concrete
    model: itu_frequency_dependent    # or: constant
    itu_name: itu_concrete            # Sionna RT built-in name; null for custom
    relative_permittivity: null       # used only by model: constant
    conductivity_s_per_m: null        # used only by model: constant
    thickness_m: 0.30                 # default slab thickness; null allowed
    scattering_coefficient: 0.20      # 0..1
    xpd_coefficient: 0.10             # 0..1
    transmissive: true                # can radio waves pass through?
    preview_color: "#9e9e9e"          # frontend overlay only
    notes: Default structural concrete (ITU-R P.2040).
    builtin: true                     # false for user-defined/edited materials
```

## ITU 모델 vs constant 모델

- `model: itu_frequency_dependent` — 유전율(permittivity)과 전도율
  (conductivity)은 *시뮬레이션 주파수에서* ITU-R P.2040 파라미터화로부터
  유도됩니다. 이들은 `itu_name`을 통해 Sionna RT의 내장 재질로 매핑되며
  (예: `itu_concrete`, `itu_medium_dry_ground`), 따라서 YAML에서
  `relative_permittivity` / `conductivity_s_per_m`는 `null`로 유지됩니다.
- `model: constant` — `relative_permittivity`와 `conductivity_s_per_m`이
  모든 주파수에서 주어진 값 그대로 사용됩니다. 측정값이나 문헌값
  (아스팔트, 식생, 보정된 커스텀)에 이를 사용하십시오. constant 재질은
  그 값이 취해진 주파수 범위에서만 유효합니다 — `notes`에 명시하십시오.

## 기본 라이브러리

| id | display name | category | model | itu_name | thickness_m | scat. | xpd | transmissive | preview |
|---|---|---|---|---|---|---|---|---|---|
| `itu_concrete` | ITU Concrete | concrete | itu | `itu_concrete` | 0.30 | 0.20 | 0.10 | yes | `#9e9e9e` |
| `itu_brick` | ITU Brick | brick | itu | `itu_brick` | 0.24 | 0.20 | 0.10 | yes | `#b5551d` |
| `itu_glass` | ITU Glass | glass | itu | `itu_glass` | 0.012 | 0.02 | 0.05 | yes | `#4fc3f7` |
| `itu_wood` | ITU Wood | wood | itu | `itu_wood` | 0.03 | 0.20 | 0.10 | yes | `#8d6e63` |
| `metal` | Metal | metal | itu | `itu_metal` | — | 0.05 | 0.05 | no | `#b0bec5` |
| `ground` | Ground (medium dry) | ground | itu | `itu_medium_dry_ground` | — | 0.30 | 0.15 | no | `#795548` |
| `asphalt_custom` | Asphalt (custom) | asphalt | constant (εr 5.72, σ 0.005 S/m) | — | — | 0.25 | 0.10 | no | `#37474f` |
| `vegetation_custom` | Vegetation (custom) | vegetation | constant (εr 5.0, σ 0.10 S/m) | — | 1.0 | 0.60 | 0.30 | yes | `#43a047` |
| `unknown_rf` | Unknown RF material | unknown | constant (εr 3.0, σ 0.01 S/m) | — | — | 0.20 | 0.10 | yes | `#e91e63` |

`unknown_rf`는 의도적인 자리표시자(placeholder)입니다. 표면을 아직 분류할 수
없을 때 이를 할당하면, 조용히 기본값으로 처리되는 대신 눈에 띄게 추적됩니다.

## prim별 오버라이드

prim의 `rf` 바인딩은 재질을 포크(fork)하지 않고도 재질의 기본값을
오버라이드할 수 있습니다:

```json
"rf": {
  "material_id": "itu_glass",
  "thickness_m": 0.008,
  "scattering_coefficient": null,
  "xpd_coefficient": null,
  "assignment_status": "user_confirmed",
  "assignment_sources": ["user"],
  "confidence": 1.0
}
```

오버라이드는 할당 API(`AssignRequest.overrides`, 필드 `thickness_m`,
`scattering_coefficient`, `xpd_coefficient`)를 통해 설정됩니다. `null`
오버라이드는 "0"이 아니라 "상속"을 의미합니다. 오버라이드는 prim에 저장되고
인스펙터에 표시되지만, **MVP의 Mode 2 그룹 컴파일은 이를 표현할 수
없습니다**. 내보낸 RF 투영은 라이브러리 재질의 파라미터를 사용하며,
컴파일러는 오버라이드된 prim마다 경고를 방출합니다. 컴파일 시점
오버라이드(설정되어 있으면 prim 오버라이드, 그렇지 않으면 재질 기본값)는
그룹이 파라미터 집합별로 분할될 수 있게 되면 의도된 동작입니다.

## 두께(thickness) 의미

`thickness_m`은 단일 층 슬래브(slab)로 모델링된 표면을 통과하는 투과 손실에
사용되는 슬래브 두께입니다:

- **투과성(transmissive) 재질**(유리, 벽돌, 콘크리트, 목재, 식생)은
  *어딘가로부터* 두께가 필요합니다 — 재질 기본값이나 prim별 오버라이드에서.
  투과성 재질이 결국 어디에도 두께를 갖지 못하면, 검증은
  `MISSING_THICKNESS` 경고를 방출합니다(오류가 아닌 경고입니다: 컴파일은
  계속 진행되며 백엔드가 자체 폴백 동작을 적용합니다).
- **비투과성(non-transmissive) 재질**(금속, 지면, 아스팔트)은 반사성
  반공간(half-space)으로 취급됩니다. 두께는 의미가 없으며 `null`로
  둡니다.
- 식생은 거친 유효 매질(effective-medium)입니다. 1.0 m 기본 두께는 단단한
  벽이 아니라 잎이 우거진 부피를 통과하는 전파를 대신 나타냅니다.

## 재질 편집

`PUT /api/projects/{id}/rf/materials/{material_id}`는 프로젝트 라이브러리에
재질을 업서트(upsert)합니다(`rf/materials.yaml`에 영속화됨). 편집되거나 새로
생성된 재질은 `builtin: false`를 지닙니다. prim이 여전히 참조하는 재질을
제거하는 것은 쓰기 시점에 차단되지 않습니다. 매달린(dangling) 참조는
`UNKNOWN_RF_MATERIAL` 검증 이슈로 드러납니다.
