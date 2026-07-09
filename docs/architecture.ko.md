# 아키텍처

> 🌐 [English](architecture.md) · **한국어**

SEAM Studio는 Sionna RT 위에서 RF를 인식하는 디지털
트윈을 저작하기 위한 로컬 우선(local-first) 워크벤치입니다. 모든 것은 일반 소비자용 머신에서 실행됩니다: 평범한 프로젝트 폴더 위에서 동작하는 FastAPI
백엔드, React/Three.js 프런트엔드, 그리고 없을 때는 우아하게 성능이 저하되는 선택적
로컬 추가 구성요소(Sionna RT, Ollama)입니다.

## 통합 씬 그래프와 두 개의 투영(projection)

프로젝트당 진실의 원천(source of truth)은 정확히 하나입니다: 정규(canonical) 통합 씬 그래프인
`scene.seam.json`(`app.schemas.scene.Scene`)입니다. 시각과 RF
측면은 여기서 컴파일된 *투영*이며 — 따로 어긋날 수 있는 독립적인 파일이
결코 아닙니다.

> **레거시 `.sionnatwin`.** SEAM 개명 이전에 생성된 프로젝트는
> `<id>.sionnatwin` 폴더 안의 `scene.sionnatwin.json`을 사용하며, 해당 위치에서
> 계속 로드되고 저장됩니다. `scene.seam.json`이 현재의 정규 이름입니다.

```text
                 scene.seam.json  (canonical Scene)
                 prims / devices / configs / result refs
                 each prim: mesh_ref + visual binding + rf binding
                        │
        ┌───────────────┴────────────────────┐
        ▼                                    ▼
  Visual projection                    RF projection
  visual/scene.glb (+textures)         rf/generated_scene.xml (Mitsuba)
  named meshes, PBR materials          rf/meshes/*.ply grouped by RF material
  rendered by Three.js frontend        consumed by Sionna RT / mock backend
        │                                    │
        └────────────── prim ids ────────────┘
          every mesh, warning, suggestion and ray-path
          interaction maps back to a canonical prim id
```

메시 프리미티브는 두 개의 독립적인 재질 바인딩을 가집니다:

- `visual`: GLB에서 온 PBR 외관(이름, 기본 색상, 텍스처). 렌더링과
  *제안 근거(suggestion evidence)*로만 사용됩니다.
- `rf`: 전자기 재질(프로젝트의 RF 재질 라이브러리를 가리키는 `rf.material_id`)과
  출처(provenance)(`assignment_status`, `assignment_sources`, `confidence`).

시각 재질 정보는 결코 RF 진실로 사용되지 않습니다. `concrete.jpg`라는
텍스처는 추적되는 출처와 함께 `itu_concrete`를 *제안*할 수 있지만,
오직 사용자 확인(또는 향후 캘리브레이션 실행)만이 이를 승격시킵니다.

## 모듈 맵

```text
backend/app/
  main.py                     FastAPI factory; mounts routers under /api
  core/
    config.py                 env-driven Settings (project roots, AI config)
    paths.py                  repo anchors, default project roots
  schemas/                    canonical Pydantic v2 contracts (wire format)
    common.py scene.py devices.py materials.py simulation.py
    results.py ai.py validation.py compile.py projects.py
  services/
    project_store.py          project folder persistence (atomic writes)
    availability.py           import-light optional-dependency probes
    scene_validator.py        validate_scene(scene, library, project_dir)
    material_assignment.py    assign_materials(scene, request, library)
    rf_compiler.py            compile_project(project_dir, scene, library)
    ai_provider.py            suggestion providers + fallback chain
    simulation_backends/      RayTracingBackend protocol, mock + sionna
  api/
    health.py projects.py scene.py materials.py ai.py compile.py simulate.py
  data/default_rf_materials.yaml   built-in RF material library

frontend/src/
  types/api.ts                TypeScript mirror of the Pydantic schemas
  (viewer, scene tree, inspector, RF overlay, AI panel, result explorer)

examples/
  scripts/create_demo_project.py   generates examples/demo_project/sample_demo.sionnatwin
```

## 요청 흐름

### RF 재질 할당

```text
POST /api/projects/{id}/rf/assign            body: AssignRequest
  deps.get_store() -> load_scene_or_404      404 if project unknown
  store.load_materials(id)                   project rf/materials.yaml
  material_assignment.assign_materials(scene, request, library)
      mutates prims in place; UnknownMaterialError -> 404
  store.save_scene(id, scene)                atomic write
  -> AssignResponse {updated_prim_ids, skipped_prim_ids, warnings}
```

일괄 할당(`/rf/batch-assign`)은 한 번의 저장 전에 각
`AssignRequest`마다 동일한 변형(mutation)을 반복합니다.

### RF 투영 컴파일

```text
POST /api/projects/{id}/compile/sionna
  rf_compiler.compile_project(project_dir, scene, library)
      1. scene_validator.validate_scene(...)  -> ValidationReport
         warnings do NOT block; errors abort the compile
      2. group mesh prims by rf.material_id   (Mode 2 grouping)
      3. export rf/meshes/<group>.ply         world-space, Z-up ENU
      4. write rf/generated_scene.xml         Mitsuba XML for Sionna RT
         bsdf ids use the "mat-" prefix: bsdf id "mat-itu_concrete"
         binds shapes to Sionna RadioMaterial "itu_concrete"
  -> CompileResult {material_groups, generated_files, validation, warnings}
```

### 시뮬레이션

```text
POST /api/projects/{id}/simulate/paths       body: SimulateRequest
  resolve SimulationConfig (inline config wins over config_id; 404/400)
  simulation_backends.resolve_backend(config)
      "auto"  -> sionna when importable, else mock
      "sionna" when not installed -> BackendUnavailableError -> HTTP 409
  backend.simulate_paths(...) -> PathResultSet (backend-neutral schema)
  persist results/<result_id>.json
      result_id = f"{backend_name}_{kind}_{n:03d}",
      n = 1 + count of existing refs of that kind in scene.result_sets
  append ResultSetRef to scene.result_sets; save scene
  -> PathResultSet
GET /api/projects/{id}/results/paths         latest = last ref of that kind
```

`simulate/radio-map`은 `RadioMapResultSet`으로 동일한 형태를 따릅니다.

## 결과 스키마, 재현성, 이벤트

백엔드 중립적인 결과 모델(`app.schemas.results`)은 원시 전력(raw power)
이상의 정보를 담습니다. 프런트엔드가 이들로부터 읽어내는 것:

### 도착/출발 각도 (AoA / AoD)

모든 `RayPath`는 `path_gain_db`(경로별 채널 이득 = `power_dbm`에서
구성된 TX 전력을 뺀 값이므로, 링크는 송신 전력과 무관하게 비교됨)와
두 개의 각도 쌍을 가집니다:

- `aod_deg = [azimuth_deg, elevation_deg]` — TX에서의 **출발** 방향.
- `aoa_deg = [azimuth_deg, elevation_deg]` — **도착** 방향으로,
  *RX에서 광선이 온 곳을 향해* 가리킴.

방위각(azimuth)은 `+Z`를 중심으로 한 `atan2(y, x)`이고, 고도(elevation)는 XY 평면에서
위로 향한 값입니다. 백엔드가 이를 해결할 수 없을 때 둘 다 기본값 `null`입니다. 프런트엔드
`AngularPlot`은 이들을 극좌표 산점도로 렌더링합니다 — 방위각 = 극각(polar angle),
경로 전력 = 반지름(안쪽 링이 가장 약함), AoD는 채워진 마커로 AoA는 빈 마커로 —
고도는 CSV 내보내기와 각 마커의 툴팁에 담습니다.

### 다중 TX 라디오 맵: SINR과 서빙 셀

`RadioMapResultSet.metric`은 `path_gain_db | rss_dbm | sinr_db` 중 하나입니다
(`sinr_db`는 실제 동일 채널 간섭 `S/(I+N)`을 모델링하며, 단일 TX에서는
SNR로 퇴화됩니다). 다중 TX 맵은 다음도 채웁니다:

- `tx_ids: string[]` — 기여한 모든 TX를 솔버 순서로.
- `serving_tx: (number|null)[][]` — 각 셀에서 가장 강한 TX에 대해 `tx_ids`를 가리키는
  셀별 행 우선(row-major) 인덱스(다중 TX 씬에서만 채워지므로, 단일
  TX 페이로드는 작게 유지됨).

`values`는 행 우선 `[ny][nx]`이며, `null`은 계산되지 않은 셀을 표시합니다
(점진적 세분화는 값을 지어내는 대신 구멍을 남깁니다).

### 메시 라디오 맵 (표면 커버리지)

`POST /projects/{id}/simulate/mesh-radio-map`은 수평면 대신 실제 메시
표면(파사드, 바닥, 도로)에 커버리지를 칠합니다. 요청
`MeshRadioMapRequest {prim_ids (≥1), tx_id?, metric: path_gain_db|rss_dbm
(default rss_dbm), max_triangles=2000, offset_m=0.05}`은 각 삼각형 중심에 프로브 RX를
배치하며, 면 법선을 따라 `offset_m`만큼 오프셋합니다. 응답
`MeshRadioMapResultSet`은 프림당 하나의 `MeshRadioMapSurface`를 담으며 정렬된
`centers` / `normals` / `values` 리스트(그래서 뷰어는 백엔드의 삼각형 순서를
재현할 필요가 전혀 없음)와, 메시가 `max_triangles`를 초과하여 매 k번째 삼각형이
샘플링되었을 때의 `sample_stride > 1`을 가집니다. 최신 가져오기:
`GET /projects/{id}/results/mesh-radio-map?result_id=`.

### 영역 세분화

`RadioMapGridConfig`는 `center_xy` / `size_xy`(미터 단위 `[x, y]`, 둘 다
선택적이며, `null` = 씬 지오메트리에 자동 맞춤)를 추가합니다. 호출자는 전체 맵을 다시
계산하는 대신 명시적인 중심/크기를 전달하여 선택한 영역을 더 세밀한 `cell_size_m`으로
다시 계산(re-solve)합니다 — 세분화된 값은 해당 영역의 셀을 덮어씁니다.

### 재현성 해시

지속(persist)된 모든 결과의 `metadata`에는 콘텐츠 해시가 찍혀 있어 오래된(stale)
결과를 감지할 수 있습니다(`simulate.py::_provenance_hashes`):

- `scene_hash` — 정규 씬에서 `result_sets`를 **뺀** 값(결과가 자신을
  생성한 씬의 해시를 휘젓지 않아야 함).
- `rf_assignment_hash` — `(prim_id, material_id, assignment_status)`만으로, 순수한
  재질 재할당을 그 자체로 감지할 수 있음.
- `sim_config_hash` — 정확한 솔버 노브(knob); 전체 `config_snapshot`이
  함께 저장됨.

프런트엔드는 결과에 찍힌 해시를 라이브 씬과 비교하여, 솔브 이후 씬이나 할당이
바뀌었을 때 결과에 오래됨(stale) 배지를 붙입니다.

### 백엔드와 기능(capabilities)

`GET /api/backends`는 기능을 인식하는 UI를 위해 `[{name, available, detail, capabilities}]`를
반환합니다. `capabilities`는 안정적이고 가산적(additive)인 기능 맵
(`paths`, `radio_map`, `mesh_radio_map`, `cir`, `beamforming`, `doppler`,
`diffraction`, `gpu`, …)입니다; **프런트엔드는 누락된 키를 `false`로 취급합니다**. `mock`
백엔드는 항상 사용 가능하며, `sionna`는 Sionna RT를 임포트할 수 없을 때
"not installed (optional)" 상세와 함께 `available: false`를 보고합니다.

### 라이브 이벤트 (WebSocket)

`WS /ws/projects/{id}/events`(`/api` 접두사 **없이** 마운트됨)는 작업이 실행될 때
JSON 프레임을 스트리밍합니다: 일회성 `{type: "connected"}` 인사말, 이어서
`compile_started` / `compile_finished`, `simulation_started` /
`simulation_finished`(finished 프레임은 `kind`, `result_id`,
`backend`를 담음). 프런트엔드는 이를 사용해 폴링 없이 라이브 컴파일/솔브 진행 상황을
표시합니다.

### AODT 임포트

`POST /projects/{id}/results/import-aodt`는 서버 로컬의 `source_dir`에서 NVIDIA AODT parquet
내보내기를 읽고(`kinds: ["paths", "radio_map"]`), 이를 동일한 결과 스키마로 정규화하며
(tx/rx id는 parquet 열에서 바로 읽으며 기본값은 `"tx"`/`"rx"`; 오늘날
object-id→prim-id 재매핑은 없음), 공유
`_persist_result` 헬퍼를 통해 지속시킵니다 — 그래서 임포트된 세트는 로컬 솔브와 정확히
똑같이 정규 id, 출처 해시, `backend: "aodt_import"`인 `ResultSetRef`를 얻습니다.
`pyarrow`가 설치되어 있지 않으면 409를 반환합니다.

### 측정 CSV 임포트

`POST /projects/{id}/calibrate/measurements/import-csv {csv_text}`는 측정된 링크별
샘플(RX 위치 + 측정된 경로 이득)을 `MeasurementSample`(각각 선택적 `measurement_id`를 가짐)로
파싱하며, `skipped` 행과 `warnings`를 보고합니다; `GET /projects/{id}/calibrate/measurements`는
저장된 세트를 반환합니다. 이들은 재질 캘리브레이션과 RF 모호성 해소(disambiguation)에
사용됩니다(`docs/ai_assistant.md`, `docs/accuracy.md` 참조).

### 씬 번들 임포트 (Mitsuba XML / zip)

`POST /projects/import`는 단일 Mitsuba `.xml`(+ 선택적 동반 메시 업로드, 평평하게
그리고 `meshes/` 아래에 스테이징됨) 또는 **전체 씬 폴더의 `.zip`**(매직/확장자로
감지됨) 중 하나를 받습니다. zip은 상대 경로를 보존하며 추출됩니다 — 경로 순회(traversal)
항목을 거부하고, macOS `__MACOSX`/`.DS_Store`/`._*` 잡동사니를 건너뛰며, zip 폭탄에
대비해 한도를 둡니다 — 그래서 XML이 `meshes_tex/x.ply` + `textures/y.png`를 참조하는
Blender 스타일 번들은 디스크상 그대로 해석됩니다. 하나의 zip에 여러 씬 XML이 있으면,
가장 많은 PLY 참조를 해석하는 것이 이깁니다(텍스처가 있는 변형이 동점을 이김); 그 선택은
출처(`source_xml`, `textures_persisted`)에 기록됩니다.

XML이 참조하는 비트맵 텍스처(`<texture type="bitmap">`, `twosided` 래퍼를 통한 것
포함)는 `mitsuba_import`에 의해 파싱됩니다: UV가 있는 메시는 `visual/scene.glb`에 실제
`TextureVisuals`를 얻고(임베드된 사본은 512 px로 축소되며 JPEG로 뒷받침됨), 원본
전체 해상도 파일은 `visual/textures/`로 복사되며, 각 프림의 `visual.base_color_texture`는
지속된 원본을 가리킵니다. AI 텍스처 크롭 근거는 그 원본을 먼저 읽고
(서브 윈도우일 때 프림의 사용된 UV 바운딩 박스로 크롭, 256 px), GLB baseColor로 폴백하며,
멀티모달 제공자가 실제로 본 모든 크롭은 `ai/evidence/<batch>/` 아래에 지속되고 재현성을
위해 suggest 응답(`evidence_images`)과 `ai/suggestions.jsonl`에서 참조됩니다.

### 재질 분할 (다중 재질 건물)

실제 건물은 유리/콘크리트/금속을 섞습니다; `app/services/material_segmentation.py`는
FTC SAM2/DINOv2 연구의 분할 스캐폴드를 이식합니다: 텍스처 아틀라스 + UV 메시 ->
재질 마스크 -> 면별 할당(마스크는 각 면의 UV 무게중심에서 샘플링,
`y=(1-v)*(H-1)`) -> `visual/scene.glb`에 베이크된 재질별 명명 서브 메시로의
물리적(PHYSICAL) 분할, 영역당 하나의 프림(`rule_suggested`, 소스
`segmentation:<batch>`). 마스크 소스는 계층화됩니다: 즉시 색상 휴리스틱, 비동기
로컬 VLM 타일 투표(LM Studio, 타일 개수 제한/다운샘플), 또는 업로드된 id-mask
PNG(외부 SAM2급 파이프라인; 크기/id 검증됨). 엔드포인트는
`/projects/{id}/segmentation/*` 아래에 있습니다(preview / upload-mask / jobs
/ apply / undo); apply는 이전 GLB를 백업하고
(`visual/scene.pre-split-<batch>.glb`) undo는 이를 복원하며, 둘 다 출처에
기록됩니다. 사전 분할된 번들(FTC materialsplit 씬처럼 재질별 메시 + 자체 bsdf)은
추가 작업 없이 일반 zip 경로를 통해 N개의 프림으로 임포트됩니다.

AI suggest 경로는 또한 사용자 대면 모델 선택기를 얻었습니다: 모델 검색은
`GET /projects/{id}/ai/models`(LM Studio `/v1/models`, Ollama
`/api/tags`)를 통해, 알 수 없는 모델 가드레일이 있는 요청별 `model` 재정의,
그리고 `ai/suggestions.jsonl`의 `model_source` 출처. 궤적 드레이프
(`follow_terrain`)는 이제 드레이프된 이웃 사이의 표면 z를 보간하여 내부 풋프린트
구멍을 채웁니다(`drape_fill_gaps`, 기본 켜짐), 그리고 FE는 표면 픽으로 그려진 경로에
대해 기본적으로 드레이핑을 활성화합니다.

### SEAM-Agent (검색 증강 재질 저작)

`app/services/seam_agent.py` + `/projects/{id}/agent/material-assignment/*`:
하나의 건물 수준 프림을 RF 구성요소로 분할하고 재질을 제안합니다.
FE는 r3f 월드 내부에서 다중 뷰 정사영 렌더(RGB + triangle-id 버퍼,
uint24 정점 색상으로서의 faceIndex)를 캡처합니다; 백엔드는 사용자의 사이트 힌트에 대해
웹 + 이미지 근거를 선택적으로 검색하는(DuckDuckGo를
`ddgs`로, 옵트인, 출처는 ai/agent/<job>/ 아래 저장) 제한된(BOUNDED) 에이전트
루프(검색/VLM 호출/런타임에 대한 예산)를 실행하고, 로컬 VLM이 검색된 사진을 재질
주장(claim)으로 요약하게 한 다음, 각 뷰의 RF 관련 영역(박스)을 그 주장을 맥락으로
삼아 레이블링합니다. 박스는 triangle-id 버퍼를 통해 면별 투표로 역투영되며(+ 상반부
위쪽 법선 지붕 사전확률(roof prior)), 면은 신뢰도와 함께 의미론적 세그먼트로 집계되고,
사용자는 분할 베이크 기계장치(undo 포함)를 통해 검토/적용합니다. trace 엔드포인트는
관찰 가능한 활동 로그(단계, 쿼리, 근거 카드)를 노출합니다 - 원시 사고
연쇄(chain-of-thought)는 결코 노출하지 않습니다.

### OpenStreetMap 임포트

`POST /projects/import-osm {name, lat, lon, width_m, height_m, ...}`는 지리적
사각형으로부터 시뮬레이션 준비가 된 실외 프로젝트를 한 번에 구축합니다
(`app.services.osm_import`). Overpass API에서 건물 풋프린트를 가져오고, 각 way의
경도/위도 링을 중심에 대한 등장방형(equirectangular) 접평면 근사를 통해 로컬 ENU
미터로 투영하며(허용하는 ≤3 km 사각형에 대해 미터 미만 정확도), 풋프린트를
`trimesh.creation.extrude_polygon`으로 돌출시키고(높이는 OSM `height` /
`building:levels` 태그에서, 없으면 기본값), 그 아래에 얇은 지면 평면을 놓습니다.
결과는 단일 `visual/scene.glb` 안에 건물당 하나의 명명된 지오메트리
(각 프림의 `mesh_ref.mesh_name`과 일치), `building`으로 태그된 건물당 하나의 프림과
`ground`/`terrain` 프림, 모두 상태 `rule_suggested`와 소스 `osm_import`로 요청된
재질에 RF 바인딩되며, 씬을 고정하는 `coordinate_system.origin_lat_lon_alt`입니다. 두
재질 id 모두 기본 라이브러리에 대해 검증됩니다(알 수 없으면 400); 점이 세 개
미만이거나 유효하지 않은 다각형인 풋프린트는 건너뛰고 집계되며,
2000개 건물을 초과하면 면적이 가장 큰 것만 유지됩니다(경고됨). Overpass
엔드포인트는 `SEAM_OVERPASS_URL`(`SIONNATWIN_OVERPASS_URL`
폴백)로 재정의할 수 있습니다; 도달할 수 없는 엔드포인트나 잘못된 응답은 502를,
타임아웃은 504를 반환합니다.

## 시뮬레이션 백엔드 인터페이스

모든 백엔드는 하나의 추상 기반 클래스
(`app.services.simulation_backends`)를 구현합니다:

```python
class RayTracingBackend(abc.ABC):
    name: str
    @abc.abstractmethod
    def is_available(self) -> bool: ...
    @abc.abstractmethod
    def simulate_paths(...) -> PathResultSet: ...
    @abc.abstractmethod
    def simulate_radio_map(...) -> RadioMapResultSet: ...

get_backend(name: str) -> RayTracingBackend
resolve_backend(config: SimulationConfig) -> RayTracingBackend   # 409 on unavailable
available_backends() -> list[HealthBackendStatus]                # feeds /api/health
```

- **mock**은 항상 사용 가능하고 결정론적입니다: LoS 경로, prim-id 상호작용이 있는
  반사 경로, 그리고 합성 라디오 맵. 이것은 전체 앱(프런트엔드, 테스트, 결과
  탐색기)이 GPU 없이, Sionna 없이 작동하도록 존재합니다.
- **sionna**는 함수 내부에서 Sionna RT를 지연(lazily) 임포트합니다
  (`availability.sionna_available()`는 모듈 스펙만 탐색함). 임포트나 실행이
  실패하면, API는 앱을 크래시시키는 대신 409를 보고합니다.
- **AODT (향후)**는 스키마를 건드리지 않고 두 가지 방식으로 끼워집니다:
  원격 AODT 워커를 감싸는 또 다른 `RayTracingBackend`로서, 또는 AODT Parquet
  출력을 `PathResultSet` / `RadioMapResultSet`으로 정규화하는 임포터로서
  `mapping/object_map.json`을 통해 AODT 객체 id를 정규 prim id로 재매핑합니다.
  어느 쪽이든 결과는 `backend` 필드가 그 출처를 기록하는 `ResultSetRef`와 함께
  `results/`에 안착합니다.

## 주요 결정

- **snake_case 와이어 포맷, 종단 간(end-to-end).** JSON 키는 씬 파일,
  모든 HTTP 본문, 그리고 TypeScript 미러 타입
  (`frontend/src/types/api.ts`)에서 snake_case입니다. 어긋날 수 있는 camelCase 변환
  경계가 없습니다.
- **Z-up ENU 미터, 어디에서나.** 씬 JSON 위치, GLB 정점 데이터,
  RF 서브메시, 그리고 광선 경로 정점은 모두 하나의 프레임을 공유합니다. 데모 GLB는
  의도적으로 Z-up 정점을 저장합니다(glTF의 Y-up 규약은 적용되지 않음); 월드 변환은
  정점에 베이크되고 프림 변환은 항등(identity)으로 유지되므로, 어떤 소비자도 축을
  조정할 일이 없습니다.
- **짧은 디바이스 id, 경로 같은 prim id.** 프림은 주소 지정 가능한 트리 노드
  (`/buildings/b01/walls`)입니다; 디바이스는 결과 행이 직접 참조하는 평평한 시뮬레이션
  엔드포인트(`tx_001`)입니다. 프런트엔드는 합성 `/devices` 노드 아래에 디바이스를
  표시합니다.
- **중복 prim id는 파싱 시점에 거부됨.** `Scene`의 모델 검증기는 중복에 대해
  예외를 발생시키므로, 손상된 씬은 결코 로드되지 않습니다 — 하위 단계에서 검증이 이에
  대해 방어할 필요가 없습니다.
- **경고는 결코 컴파일을 막지 않음.** `ValidationReport.ok`는 "error 심각도의
  문제가 없음"을 의미합니다. 할당되지 않은 재질, 누락된 두께, 또는 시각/RF
  불일치는 사용자가 그냥 넘어갈 수 있는 경고입니다; 오직 구조적 오류만이 컴파일을
  중단시킵니다.
- **결과는 id별로 저장되고, 최신은 ref 순서로.** 모든 실행은 불변의
  `results/<result_id>.json`을 씁니다; 씬은 순서가 있는
  `result_sets` 리스트를 유지하며 "최신"은 단순히 어떤 종류의 마지막 ref입니다. 이력은
  결코 덮어쓰이지 않습니다.
- **이동 RX(UE) 궤적은 기존 디바이스를 이동시킴.** 각 웨이포인트/스텝은
  씬을 깊은 복사하고 라우팅된 UE의 `position`과 유한 차분 `velocity_m_s`만
  변형합니다; 다른 모든 필드(안테나
  패턴/행/열/편파/간격, 전력, 이름, 방향)는 변경 없이 상속되므로, `rx_001`에 대한
  궤적은 `rx_001`의 구성된 배열로 솔브됩니다. 다중 UE 실행(`routes`)은 **스텝
  우선(step-major)**입니다: 라우팅된 모든 UE에 대해 스텝당 한 번 솔브하며, 샘플은
  스텝-0의 모든-UE, 그다음 스텝-1, ... 순으로 정렬됩니다; 각 `TrajectorySample`은
  자신의 `ue_id`를 담고 메타데이터는 `ue_ids`/`num_steps` 리스트를 담습니다. 주의사항:
  Sionna는 하나의 씬 수준 `rx_array`(첫 번째로 선택된 RX 디바이스에서)를 적용하므로,
  라우팅된 UE가 동일하지 않은 안테나 구성을 가질 때 첫 번째만 존중됩니다 — routes
  경로는 무시된 UE를 명시하는 경고를 냅니다. RFData `trajectory.csv`는 항상 `ue_id`
  열(고정된 AODT 뷰어 스키마 열)을 결과의 스텝 우선 순서의 행과 함께 담으므로, 뷰어는
  이를 UE별 시퀀스로 분할합니다; 단일 UE는 퇴화된 경우(하나의 ue_id)입니다. ML
  데이터셋 생성기는 별도의, 단일 UE 스윕입니다(`TrajectoryResultSet`을 소비하지 않음):
  각 `.npz`는 하나의 UE의 시퀀스이며, 스윕된 UE는 `metadata.json`에
  `ue_id`/`source_rx_id`로 기록됩니다.
- **Sionna를 위한 `mat-` BSDF 접두사.** 생성된 Mitsuba XML은 각 BSDF를
  `mat-<rf_material_id>`로 명명하며, 이는 Sionna RT가 셰이프를
  `RadioMaterial` 인스턴스에 바인딩하는 데 사용하는 규약입니다.
- **AI 폴백 체인: ollama → rule_based.** 제안은 구성되어 있고 도달 가능할 때
  로컬 Ollama 모델을 선호합니다; 그렇지 않으면(또는 유효하지 않은 AI JSON일 때)
  결정론적 규칙 기반 제공자로 폴백합니다. 제안은 기본적으로 결코 자동 적용되지 않습니다.
- **이식 가능한 로컬 툴체인.** 전체 스택은 사용자 로컬의,
  재배치 가능한 설치본에서 실행됩니다 — `backend/.venv` 아래의 백엔드 venv와
  프런트엔드용 이식 가능한 Node 배포판. 관리자 권한, 시스템
  서비스, 또는 클라우드 의존성이 필요하지 않습니다.
