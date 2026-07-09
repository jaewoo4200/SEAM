# 씬 및 프로젝트 형식

> 🌐 [English](scene_format.md) · **한국어**

SEAM 프로젝트는 압축하고, 공유하고, 재현할 수 있는 일반 폴더입니다(관례상
`<project_id>.seam`로 명명하며, 기존 `<project_id>.sionnatwin` 폴더도 계속 로드됩니다).
그 안에 있는 정식 씬 파일이 단일 진실 공급원(single source of truth)이며,
나머지는 모두 입력 에셋이거나 생성된 출력입니다.

## 프로젝트 폴더 레이아웃

```text
<project_id>.seam/
├─ scene.seam.json            canonical unified scene (source of truth)
├─ visual/
│  ├─ scene.glb               visual projection source (named meshes, PBR)
│  └─ textures/               optional
├─ rf/
│  ├─ materials.yaml          project RF material library (see rf_materials.md)
│  ├─ generated_scene.xml     compiled Mitsuba/Sionna projection (generated)
│  └─ meshes/                 compiled RF submeshes (generated)
├─ mapping/
│  └─ object_map.json         prim_id -> {"mesh_name": ...} for mesh prims
├─ ai/
│  └─ suggestions.jsonl       AI suggestion + decision provenance log
├─ results/
│  └─ <result_id>.json        normalized simulation results
└─ provenance.json            project-level event log
```

백엔드는 구성된 루트(`SEAM_PROJECT_ROOTS`, 기존 `SIONNATWIN_PROJECT_ROOTS`;
기본값은 `projects/`와 `examples/demo_project/`)를 스캔하여
`scene.seam.json`(또는 기존 `scene.sionnatwin.json`)을 포함한 폴더를 찾아
프로젝트를 발견합니다. 프로젝트 id는 `.seam`(또는 기존 `.sionnatwin`) 접미사를
제외한 폴더 이름입니다. 모든 쓰기는 원자적(임시 파일 + 이름 변경)이므로,
충돌이 발생해도 씬이 손상되지 않습니다.

### 기존 `.sionnatwin` 레이아웃

SEAM 이름 변경 이전에 생성된 프로젝트는 내부에 `scene.sionnatwin.json`이 있는
`<project_id>.sionnatwin` 폴더를 사용합니다. 이러한 프로젝트는 완전히 지원됩니다 —
스토어가 해당 위치에서 로드하고 저장합니다 — 다만 새 프로젝트는 위에 표시된 대로
`.seam` / `scene.seam.json`을 사용합니다.

## scene.seam.json

직렬화된 `Scene` 모델(`backend/app/schemas/scene.py`). 모든 모델은 알 수 없는 키를
거부하므로, 스키마 드리프트는 로드 시점에 명확히 실패합니다. 모든 좌표는
Z-up ENU 미터 단위입니다.

### 최상위

| field | type | notes |
|---|---|---|
| `schema_version` | str | 현재 `"0.1.0"` |
| `scene_id` | str | 안정적인 씬 식별자, 보통 프로젝트 id와 동일 |
| `name` | str | 표시 이름 |
| `coordinate_system` | object | 아래 참조 |
| `assets` | object | 아래 참조 |
| `prims` | Prim[] | `parent_id` 링크로 평탄화된 씬 그래프 |
| `devices` | Device[] | 송신기/수신기 |
| `simulation_configs` | SimulationConfig[] | 저장된 재사용 가능한 실행 구성 |
| `result_sets` | ResultSetRef[] | 저장된 결과에 대한 정렬된 포인터 |

중복된 prim id 또는 device id는 검증에서 단순히 플래그되는 것이 아니라
파싱 시점에 거부됩니다(`Scene.model_validate` 오류).

### coordinate_system

| field | type | notes |
|---|---|---|
| `type` | `"local_enu"` | 현재는 고정 |
| `origin_lat_lon_alt` | [lat, lon, alt] \| null | 지오레퍼런싱될 때의 측지 앵커(향후 3D Tiles 경로) |
| `units` | `"meters"` | 고정 |

### assets

| field | type | notes |
|---|---|---|
| `visual_scene_uri` | str \| null | 프로젝트 상대 경로 GLB, 기본값 `"visual/scene.glb"` |
| `tileset_uri` | str \| null | 향후 3D Tiles 타일셋 |

### Prim

객체, 서브메시, 또는 그룹화 노드마다 하나의 항목. Id는 절대적이며
경로 형태입니다(`/buildings/b01/window_01`); 맨 앞 세그먼트는 구조 외에
특별한 의미를 갖지 않습니다.

| field | type | notes |
|---|---|---|
| `id` | str | `/`로 시작해야 하며, 끝에 `/` 없음, `//` 없음 |
| `name` | str | 표시 이름, 보통 경로의 마지막 세그먼트 |
| `type` | `"mesh_primitive"` \| `"group"` | 그룹은 지오메트리를 갖지 않음 |
| `parent_id` | str \| null | 부모 prim의 id; 최상위인 경우 null |
| `semantic_tags` | str[] | 예: `["building", "window"]`; 규칙/AI에서 사용 |
| `mesh_ref` | MeshRef \| null | 실제로는 mesh_primitive에 필수 |
| `transform` | Transform | translation/rotation_quat_xyzw/scale; 변환이 GLB에 베이크된 경우 항등(데모 관례) |
| `visual` | VisualBinding \| null | PBR 외관 증거 |
| `rf` | RFBinding | 항상 존재하며, 미할당일 수 있음 |

### MeshRef — 세 가지 모드

| field | type | notes |
|---|---|---|
| `asset_uri` | str | 프로젝트 상대 경로, 예: `"visual/scene.glb"` |
| `mesh_name` | str | 에셋 내부의 정확한 명명된 메시 |
| `primitive_index` | int | 메시 내부의 glTF 프리미티브, 기본값 0 |
| `face_group` | str \| null | 명명된 면 부분집합; null = 전체 메시 |

- **모드 1 — 전체 명명된 메시**(`face_group: null`): GLB 메시당 하나의 prim,
  전체에 대한 이중 재질 바인딩. 데모 프로젝트는 이 방식만 사용합니다.
- **모드 2 — 면 그룹 분할**: 여러 prim이 하나의 `mesh_name`을 공유하고
  `face_group`으로 분할합니다(예: 하나의 건물 메시 내 벽과 창문).
  이 필드는 저장되고 왕복(round-trip)됩니다(`mapping/face_group_map.json`)만,
  **면 부분집합 추출은 MVP에서 구현되지 않습니다**: 컴파일러는 전체 명명된 메시를
  사용하고 경고를 발생시키므로, 메시를 공유하는 prim은 현재
  하나의 RF 재질을 공유해야 합니다.
- **모드 3 — RF 프록시 메시(향후)**: 하이폴리 비주얼 메시와 단순화된 RF 프록시를
  쌍으로 구성. MeshRef에 `rf_proxy_uri`를 추가하게 됨; MVP에서는
  구현되지 않았습니다.

### VisualBinding

| field | type | notes |
|---|---|---|
| `material_id` | str \| null | 앱 수준 비주얼 재질 id, 있는 경우 |
| `material_name` | str \| null | GLB에 작성된 대로의 재질 이름 |
| `base_color_texture` | str \| null | 프로젝트 상대 경로 텍스처 경로 |
| `base_color_rgba` | [r,g,b,a] \| null | 0–1 float |

비주얼 데이터는 렌더링 + 제안 증거일 뿐이며, RF 입력으로 사용되는 일은
결코 없습니다.

### RFBinding

| field | type | notes |
|---|---|---|
| `material_id` | str \| null | `rf/materials.yaml`로의 id |
| `thickness_m` | float \| null | prim별 재정의(> 0) |
| `scattering_coefficient` | float \| null | prim별 재정의(0–1) |
| `xpd_coefficient` | float \| null | prim별 재정의(0–1) |
| `assignment_status` | enum | 아래 라이프사이클 참조 |
| `assignment_sources` | str[] | 정렬된 출처, 예: `["rule_based"]`, `["ai:ollama/qwen3:8b", "user"]` |
| `confidence` | float \| null | 0–1 |

모델에 의해 강제되는 불변식: `assignment_status in {"unassigned", "rejected"}`인 경우에
한하여(if and only if) `material_id == null`입니다.

### 할당 상태 라이프사이클

```text
unassigned
   │  rule engine / AI proposes (never auto-applied by default),
   │  or a deterministic rule assigns a material outright → rule_assigned
   ▼
rule_suggested | ai_suggested
   │  user approves or edits in the UI (or assigns manually from unassigned);
   │  declining a suggestion → rejected (no material)
   ▼
user_confirmed
   │  future measurement-calibration run refines parameters
   ▼
measurement_calibrated
```

순서는 신뢰도가 높아지는 것을 반영합니다. 제안되었지만 미확인 상태인 바인딩은
사용 가능하지만(컴파일러가 이를 허용함) `UNCONFIRMED_SUGGESTION` 검증 경고를
발생시킵니다. 데모 씬은 흥미로운 각 상태에 대한 예를 하나씩 제공합니다:
`/terrain/ground`는 `user_confirmed`, `/roads/r01/surface`는
`rule_suggested`, 그리고 건물/창문/나무는 `unassigned`입니다.

### Device

| field | type | notes |
|---|---|---|
| `id` | str | 짧은 id(`tx_001`), 패턴 `[a-z0-9_\-]+` |
| `name` | str | 표시 이름 |
| `kind` | `"tx"` \| `"rx"` | |
| `position` | [e,n,u] | Z-up ENU 미터 |
| `orientation_deg` | [yaw,pitch,roll] | 도(degree), ENU 프레임 |
| `power_dbm` | float | 송신 전력; rx에서는 무시됨 |
| `antenna` | Antenna | `pattern`(Sionna 이름), `polarization`, `num_rows`, `num_cols` |
| `color` | `#rrggbb` | 뷰어 마커 색상 |

### SimulationConfig

| field | type | notes |
|---|---|---|
| `id`, `name` | str | |
| `backend` | `"auto"` \| `"mock"` \| `"sionna"` | auto = 설치되어 있으면 sionna, 아니면 mock |
| `frequency_hz` | float | 예: `3.5e9` |
| `max_depth` | int 0–12 | 최대 상호작용 깊이 |
| `tx_ids`, `rx_ids` | str[] \| null | null = 해당 종류의 모든 디바이스 |
| `los`, `reflection`, `diffraction`, `scattering` | bool | 활성화된 상호작용 유형 |
| `num_samples` | int | 광선 발사(ray-launching) 예산 |
| `radio_map` | object | `cell_size_m`, `height_m`, `metric` |

### ResultSetRef

| field | type | notes |
|---|---|---|
| `result_id` | str | `{backend}_{kind}_{n:03d}`, 예: `mock_paths_001` |
| `kind` | `"paths"` \| `"radio_map"` \| `"mesh_radio_map"` \| `"trajectory"` \| `"scenario"` | |
| `backend` | str | 이를 생성한 백엔드 |
| `simulation_config_id` | str | |
| `uri` | str | 프로젝트 상대 경로, `results/<result_id>.json` |
| `created_at` | str \| null | ISO 8601 UTC |

결과 파일은 불변입니다; 목록은 추가 전용(append-only)이며 정렬되어 있고,
어떤 종류의 "최신" 결과는 해당 종류의 마지막 ref입니다.

## 데모 프로젝트

`examples/scripts/create_demo_project.py`는
`examples/demo_project/sample_demo.sionnatwin`을 결정론적으로 재생성합니다:
`visual/scene.glb` 내 8개의 명명된 메시(월드 변환이 정점에 베이크됨), 13개의
prim(5개 그룹 + 8개 메시 프리미티브), 2개의 디바이스, 그리고 저장된 시뮬레이션
구성 하나. 이는 이 페이지의 모든 관례에 대한 참조 예시 역할도 겸합니다.
