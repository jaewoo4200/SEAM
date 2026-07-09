# 아키텍처

> 🌐 [English](architecture.md) · **한국어**

SEAM Studio는 Sionna RT 위에서 RF 인식형 디지털 트윈을 저작하는 로컬 우선(local-first)
워크벤치입니다. 전체 스택이 일반 소비자용 머신에서 돌아갑니다. 평범한 프로젝트 폴더 위에서
동작하는 FastAPI 백엔드, React/Three.js 프런트엔드, 그리고 없어도 자연스럽게 기능이 축소되는
선택적 로컬 추가 구성요소(Sionna RT, Ollama)로 이뤄집니다.

## 통합 씬 그래프와 두 개의 투영(projection)

프로젝트마다 source of truth는 정확히 하나, 정규(canonical) 통합 씬 그래프인
`scene.seam.json`(`app.schemas.scene.Scene`)뿐입니다. 시각·RF 양면은 여기서 컴파일해 낸
*투영*일 뿐, 따로 놀며 어긋날 수 있는 독립 파일이 아닙니다.

> **레거시 `.sionnatwin`.** SEAM으로 이름을 바꾸기 전에 만든 프로젝트는
> `<id>.sionnatwin` 폴더 안의 `scene.sionnatwin.json`을 쓰며, 그 자리에서 그대로
> 로드·저장됩니다. 현재 정규 이름은 `scene.seam.json`입니다.

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

메시 프리미티브는 서로 독립적인 재질 바인딩을 두 개 지닙니다.

- `visual`: GLB에서 가져온 PBR 외관(이름, 기본 색상, 텍스처). 렌더링과
  *제안 근거(suggestion evidence)*로만 씁니다.
- `rf`: 전자기 재질(프로젝트의 RF 재질 라이브러리를 가리키는 `rf.material_id`)과
  출처(provenance)(`assignment_status`, `assignment_sources`, `confidence`).

시각 재질 정보는 절대 RF 진실이 되지 않습니다. `concrete.jpg`라는 텍스처가 출처를
남기며 `itu_concrete`를 *제안*할 수는 있지만, 이를 실제로 승격하는 것은 사용자 확인
(또는 향후 캘리브레이션 실행)뿐입니다.

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

일괄 할당(`/rf/batch-assign`)은 각 `AssignRequest`마다 같은 변형(mutation)을 반복한 뒤
한 번에 저장합니다.

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

`simulate/radio-map`도 `RadioMapResultSet`으로 같은 형태를 따릅니다.

## 결과 스키마, 재현성, 이벤트

백엔드 중립적 결과 모델(`app.schemas.results`)은 원시 전력(raw power) 이상을 담습니다.
프런트엔드가 여기서 읽어내는 정보는 다음과 같습니다.

### 도착/출발 각도 (AoA / AoD)

모든 `RayPath`는 `path_gain_db`(경로별 채널 이득 = `power_dbm`에서 설정된 TX 전력을
뺀 값이라, 링크를 송신 전력과 무관하게 비교할 수 있음)와 각도 쌍 두 개를 지닙니다.

- `aod_deg = [azimuth_deg, elevation_deg]` — TX에서의 **출발** 방향.
- `aoa_deg = [azimuth_deg, elevation_deg]` — **도착** 방향으로,
  *RX에서 광선이 날아온 쪽을 가리킴*.

방위각(azimuth)은 `+Z`를 중심으로 한 `atan2(y, x)`이고, 고도(elevation)는 XY 평면에서
위로 향한 값입니다. 백엔드가 이를 구하지 못하면 둘 다 기본값 `null`입니다. 프런트엔드
`AngularPlot`은 이를 극좌표 산점도로 그립니다. 방위각 = 극각(polar angle),
경로 전력 = 반지름(안쪽 링일수록 약함), AoD는 채운 마커, AoA는 빈 마커로 표시하며,
고도는 CSV 내보내기와 각 마커 툴팁에 담습니다.

### 다중 TX 라디오 맵: SINR과 서빙 셀

`RadioMapResultSet.metric`은 `path_gain_db | rss_dbm | sinr_db` 중 하나입니다
(`sinr_db`는 실제 동일 채널 간섭 `S/(I+N)`을 모델링하며, 단일 TX에서는 SNR로
퇴화됩니다). 다중 TX 맵은 다음 항목도 채웁니다.

- `tx_ids: string[]` — 기여한 모든 TX를 솔버 순서로.
- `serving_tx: (number|null)[][]` — 각 셀에서 가장 강한 TX를 `tx_ids`로 가리키는
  셀별 행 우선(row-major) 인덱스(다중 TX 씬에서만 채우므로 단일 TX 페이로드는 작게 유지됨).

`values`는 행 우선 `[ny][nx]`이고, `null`은 계산하지 않은 셀을 뜻합니다
(점진적 세분화는 값을 지어내지 않고 구멍으로 남겨 둡니다).

### 메시 라디오 맵 (표면 커버리지)

`POST /projects/{id}/simulate/mesh-radio-map`은 수평면 대신 실제 메시 표면
(파사드, 바닥, 도로) 위에 커버리지를 칠합니다. 요청
`MeshRadioMapRequest {prim_ids (≥1), tx_id?, metric: path_gain_db|rss_dbm
(default rss_dbm), max_triangles=2000, offset_m=0.05}`은 각 삼각형 중심에 프로브 RX를
두고, 면 법선을 따라 `offset_m`만큼 띄웁니다. 응답 `MeshRadioMapResultSet`은 프림마다
`MeshRadioMapSurface` 하나를 담습니다. 서로 정렬된 `centers` / `normals` / `values`
리스트(덕분에 뷰어가 백엔드의 삼각형 순서를 재현할 필요가 없음)와, 메시가 `max_triangles`를
넘어 매 k번째 삼각형만 샘플링됐을 때의 `sample_stride > 1`이 함께 들어 있습니다.
최신 결과 가져오기: `GET /projects/{id}/results/mesh-radio-map?result_id=`.

### 영역 세분화

`RadioMapGridConfig`에는 `center_xy` / `size_xy`(미터 단위 `[x, y]`, 둘 다 선택적,
`null`이면 씬 지오메트리에 자동 맞춤)가 추가됩니다. 호출자는 전체 맵을 다시 계산하는 대신
중심·크기를 명시해, 원하는 영역만 더 촘촘한 `cell_size_m`으로 다시 풉니다(re-solve).
세분화한 값이 해당 영역의 셀을 덮어씁니다.

### 재현성 해시

저장(persist)된 모든 결과의 `metadata`에는 콘텐츠 해시가 찍혀 있어, 오래된(stale)
결과를 감지할 수 있습니다(`simulate.py::_provenance_hashes`).

- `scene_hash` — 정규 씬에서 `result_sets`를 **뺀** 값(결과가 자기를 만들어 낸
  씬의 해시를 흔들어서는 안 되므로).
- `rf_assignment_hash` — `(prim_id, material_id, assignment_status)`만 담아, 순수한
  재질 재할당만으로도 변화를 감지할 수 있음.
- `sim_config_hash` — 정확한 솔버 노브(knob); 전체 `config_snapshot`이
  함께 저장됨.

프런트엔드는 결과에 찍힌 해시를 라이브 씬과 대조해, 솔브 이후 씬이나 할당이 바뀌었으면
결과에 오래됨(stale) 배지를 붙입니다.

### 백엔드와 기능(capabilities)

`GET /api/backends`는 기능 인식형 UI를 위해 `[{name, available, detail, capabilities}]`를
반환합니다. `capabilities`는 안정적이고 가산적(additive)인 기능 맵
(`paths`, `radio_map`, `mesh_radio_map`, `cir`, `beamforming`, `doppler`,
`diffraction`, `gpu`, …)이며, **프런트엔드는 없는 키를 `false`로 간주합니다**. `mock`
백엔드는 항상 사용할 수 있고, `sionna`는 Sionna RT를 임포트할 수 없으면
"not installed (optional)" 상세와 함께 `available: false`를 보고합니다.

### 라이브 이벤트 (WebSocket)

`WS /ws/projects/{id}/events`(`/api` 접두사 **없이** 마운트됨)는 작업이 진행되는 동안
JSON 프레임을 스트리밍합니다. 일회성 `{type: "connected"}` 인사에 이어
`compile_started` / `compile_finished`, `simulation_started` /
`simulation_finished`가 오고, finished 프레임에는 `kind`, `result_id`,
`backend`가 담깁니다. 프런트엔드는 이를 이용해 폴링 없이 컴파일·솔브 진행 상황을
실시간으로 보여줍니다.

### AODT 임포트

`POST /projects/{id}/results/import-aodt`는 서버 로컬 `source_dir`에서 NVIDIA AODT parquet
내보내기를 읽어(`kinds: ["paths", "radio_map"]`) 같은 결과 스키마로 정규화하고
(tx/rx id는 parquet 열에서 곧바로 읽으며 기본값은 `"tx"`/`"rx"`, 현재로선
object-id→prim-id 재매핑 없음), 공용 `_persist_result` 헬퍼로 저장합니다. 덕분에
임포트한 세트도 로컬 솔브와 똑같이 정규 id와 출처 해시, `backend: "aodt_import"`인
`ResultSetRef`를 얻습니다. `pyarrow`가 설치돼 있지 않으면 409를 반환합니다.

### 측정 CSV 임포트

`POST /projects/{id}/calibrate/measurements/import-csv {csv_text}`는 측정된 링크별
샘플(RX 위치 + 측정 경로 이득)을 `MeasurementSample`(각각 선택적 `measurement_id` 보유)로
파싱하고, `skipped` 행과 `warnings`를 보고합니다. `GET /projects/{id}/calibrate/measurements`는
저장된 세트를 돌려줍니다. 이 데이터는 재질 캘리브레이션과 RF 모호성 해소(disambiguation)에
쓰입니다(`docs/ai_assistant.md`, `docs/accuracy.md` 참조).

### 씬 번들 임포트 (Mitsuba XML / zip)

`POST /projects/import`는 단일 Mitsuba `.xml`(+ 선택적 동반 메시 업로드, `meshes/` 아래에
평평하게 스테이징됨) 또는 **전체 씬 폴더를 담은 `.zip`**(매직 넘버/확장자로 감지) 중 하나를
받습니다. zip은 상대 경로를 그대로 살려 추출합니다. 경로 순회(traversal) 항목은 거부하고,
macOS `__MACOSX`/`.DS_Store`/`._*` 잡동사니는 건너뛰며, zip 폭탄에 대비해 한도를 둡니다.
그래서 XML이 `meshes_tex/x.ply` + `textures/y.png`를 참조하는 Blender 스타일 번들도
디스크에 있던 그대로 해석됩니다. zip 하나에 씬 XML이 여러 개면 PLY 참조를 가장 많이 해석해
내는 쪽이 선택됩니다(텍스처가 있는 변형이 동점에서 우선). 그 선택은 출처
(`source_xml`, `textures_persisted`)에 기록됩니다.

XML이 참조하는 비트맵 텍스처(`<texture type="bitmap">`, `twosided` 래퍼를 거친 것
포함)는 `mitsuba_import`가 파싱합니다. UV가 있는 메시는 `visual/scene.glb`에 실제
`TextureVisuals`를 얻고(임베드 사본은 512 px로 축소해 JPEG로 저장), 원본 전체 해상도 파일은
`visual/textures/`로 복사되며, 각 프림의 `visual.base_color_texture`는 저장된 원본을
가리킵니다. AI 텍스처 크롭 근거는 그 원본을 먼저 읽고(서브 윈도우면 프림이 실제로 쓰는 UV
바운딩 박스로 256 px 크롭), 없으면 GLB baseColor로 폴백합니다. 멀티모달 제공자가 실제로 본
크롭은 모두 `ai/evidence/<batch>/` 아래에 저장되고, 재현성을 위해 suggest 응답
(`evidence_images`)과 `ai/suggestions.jsonl`에서 참조됩니다.

### 재질 분할 (다중 재질 건물)

실제 건물은 유리·콘크리트·금속이 섞여 있습니다. `app/services/material_segmentation.py`는
FTC SAM2/DINOv2 연구의 분할 스캐폴드를 이식한 것으로, 텍스처 아틀라스 + UV 메시 ->
재질 마스크 -> 면별 할당(마스크는 각 면의 UV 무게중심에서 샘플링,
`y=(1-v)*(H-1)`) -> `visual/scene.glb`에 베이크되는 재질별 명명 서브 메시로의
물리적(PHYSICAL) 분할 순으로, 영역마다 프림 하나(`rule_suggested`, 소스
`segmentation:<batch>`)를 만듭니다. 마스크 소스는 계층으로 나뉩니다. 즉석 색상 휴리스틱,
비동기 로컬 VLM 타일 투표(LM Studio, 타일 수 제한·다운샘플), 업로드된 id-mask
PNG(외부 SAM2급 파이프라인, 크기·id 검증) 중 하나입니다. 엔드포인트는
`/projects/{id}/segmentation/*` 아래에 있습니다(preview / upload-mask / jobs
/ apply / undo). apply는 이전 GLB를 백업하고
(`visual/scene.pre-split-<batch>.glb`) undo는 이를 복원하며, 둘 다 출처에
기록됩니다. 이미 분할돼 있는 번들(FTC materialsplit 씬처럼 재질별 메시 + 자체 bsdf를 가진
것)은 추가 작업 없이 일반 zip 경로로 N개 프림으로 임포트됩니다.

AI suggest 경로에는 사용자용 모델 선택기도 생겼습니다. 모델 검색은
`GET /projects/{id}/ai/models`(LM Studio `/v1/models`, Ollama
`/api/tags`)로, 요청마다 `model`을 재정의할 수 있으며(알 수 없는 모델은 가드레일로 차단),
어떤 모델을 썼는지는 `ai/suggestions.jsonl`의 `model_source`에 남습니다. 궤적 드레이프
(`follow_terrain`)는 이제 드레이프된 이웃 사이의 표면 z를 보간해 내부 풋프린트
구멍을 메웁니다(`drape_fill_gaps`, 기본 켜짐). 또 FE는 표면 픽으로 그린 경로에는
기본적으로 드레이핑을 켭니다.

### SEAM-Agent (검색 증강 재질 저작)

`app/services/seam_agent.py` + `/projects/{id}/agent/material-assignment/*`.
건물 수준 프림 하나를 RF 구성요소로 분할하고 재질을 제안합니다.
FE는 r3f 월드 안에서 다중 뷰 정사영 렌더(RGB + triangle-id 버퍼,
uint24 정점 색상으로 인코딩한 faceIndex)를 캡처합니다. 백엔드는 제한된(BOUNDED) 에이전트
루프(검색·VLM 호출·런타임 예산)를 돌립니다. 사용자의 사이트 힌트로 웹·이미지 근거를
선택적으로 검색하고(DuckDuckGo를 `ddgs`로, 옵트인, 출처는 ai/agent/<job>/ 아래 저장),
로컬 VLM이 검색된 사진을 재질 주장(claim)으로 요약한 다음, 그 주장을 맥락 삼아 각 뷰의
RF 관련 영역(박스)에 레이블을 붙입니다. 박스는 triangle-id 버퍼를 거쳐 면별 투표로
역투영되고(+ 상반부 위쪽 법선 지붕 사전확률(roof prior)), 면은 신뢰도와 함께 의미론적
세그먼트로 모입니다. 사용자는 분할 베이크 파이프라인(undo 포함)으로 검토·적용합니다.
trace 엔드포인트는 관찰 가능한 활동 로그(단계, 쿼리, 근거 카드)를 노출하며, 원시 사고
연쇄(chain-of-thought)는 절대 드러내지 않습니다.

### OpenStreetMap 임포트

`POST /projects/import-osm {name, lat, lon, width_m, height_m, ...}`는 지리적 사각형
하나로 시뮬레이션 바로 가능한 실외 프로젝트를 한 번에 만듭니다
(`app.services.osm_import`). Overpass API에서 건물 풋프린트를 가져와, 각 way의
경위도 링을 중심 기준 등장방형(equirectangular) 접평면 근사로 로컬 ENU 미터에 투영하고
(허용 범위인 ≤3 km 사각형에서 미터 미만 정확도), `trimesh.creation.extrude_polygon`으로
풋프린트를 돌출시키며(높이는 OSM `height` / `building:levels` 태그에서, 없으면 기본값),
그 아래에 얇은 지면 평면을 깝니다. 결과는 단일 `visual/scene.glb` 안에 건물마다 명명된
지오메트리 하나(각 프림의 `mesh_ref.mesh_name`과 일치)를 담고, `building`으로 태그된
건물당 프림 하나와 `ground`/`terrain` 프림을 두며, 이 프림들은 모두 상태 `rule_suggested`·
소스 `osm_import`로 요청된 재질에 RF 바인딩되고, `coordinate_system.origin_lat_lon_alt`가
씬을 고정합니다. 두 재질 id는 모두 기본 라이브러리에 대해 검증됩니다(알 수 없으면 400).
점이 세 개 미만이거나 다각형이 유효하지 않은 풋프린트는 건너뛰며 개수를 집계하고,
건물이 2000개를 넘으면 면적이 큰 것만 남깁니다(경고 발생). Overpass
엔드포인트는 `SEAM_OVERPASS_URL`(폴백은 `SIONNATWIN_OVERPASS_URL`)로 재정의할 수
있습니다. 도달할 수 없는 엔드포인트나 잘못된 응답은 502를, 타임아웃은 504를 반환합니다.

## 시뮬레이션 백엔드 인터페이스

모든 백엔드는 단일 추상 기반 클래스
(`app.services.simulation_backends`)를 구현합니다.

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

- **mock**은 항상 사용 가능하며 결정론적입니다. LoS 경로, prim-id 상호작용이 있는
  반사 경로, 합성 라디오 맵을 냅니다. 전체 앱(프런트엔드, 테스트, 결과
  탐색기)이 GPU도 Sionna도 없이 돌아가도록 존재합니다.
- **sionna**는 함수 안에서 Sionna RT를 지연(lazy) 임포트합니다
  (`availability.sionna_available()`는 모듈 스펙만 확인). 임포트나 실행이
  실패하면 API는 앱을 죽이는 대신 409를 보고합니다.
- **AODT (향후)**는 스키마를 건드리지 않고 두 가지 방식으로 붙습니다.
  원격 AODT 워커를 감싸는 또 다른 `RayTracingBackend`로, 또는 AODT Parquet
  출력을 `PathResultSet` / `RadioMapResultSet`으로 정규화하는 임포터로 붙어
  `mapping/object_map.json`으로 AODT 객체 id를 정규 prim id에 재매핑합니다.
  어느 쪽이든 결과는 출처를 `backend` 필드에 기록한 `ResultSetRef`와 함께
  `results/`에 놓입니다.

## 주요 결정

- **snake_case 와이어 포맷, 종단 간(end-to-end).** JSON 키는 씬 파일,
  모든 HTTP 본문, TypeScript 미러 타입
  (`frontend/src/types/api.ts`)에서 모두 snake_case입니다. 어긋날 여지가 있는
  camelCase 변환 경계가 없습니다.
- **Z-up ENU 미터, 어디서나.** 씬 JSON 위치, GLB 정점 데이터,
  RF 서브메시, 광선 경로 정점은 모두 하나의 프레임을 공유합니다. 데모 GLB는
  일부러 Z-up 정점을 저장하며(glTF의 Y-up 규약은 적용하지 않음), 월드 변환은
  정점에 베이크하고 프림 변환은 항등(identity)으로 두므로, 어떤 소비자도 축을
  다시 맞출 일이 없습니다.
- **짧은 디바이스 id, 경로형 prim id.** 프림은 주소 지정이 가능한 트리 노드
  (`/buildings/b01/walls`)이고, 디바이스는 결과 행이 직접 참조하는 평평한 시뮬레이션
  엔드포인트(`tx_001`)입니다. 프런트엔드는 합성 `/devices` 노드 아래에 디바이스를
  보여줍니다.
- **중복 prim id는 파싱 시점에 거부.** `Scene`의 모델 검증기가 중복에
  예외를 던지므로 손상된 씬은 아예 로드되지 않습니다. 하위 단계의 검증이 이를 따로
  방어할 필요가 없습니다.
- **경고는 컴파일을 막지 않음.** `ValidationReport.ok`는 "error 심각도
  문제가 없음"을 뜻합니다. 미할당 재질, 누락된 두께, 시각/RF
  불일치는 사용자가 그냥 넘겨도 되는 경고이고, 컴파일을 중단시키는 것은 구조적
  오류뿐입니다.
- **결과는 id별로 저장, 최신은 ref 순서로.** 모든 실행은 불변의
  `results/<result_id>.json`을 씁니다. 씬은 순서가 있는
  `result_sets` 리스트를 유지하고, "최신"이란 특정 종류의 마지막 ref일 뿐입니다. 이력은
  절대 덮어쓰지 않습니다.
- **이동 RX(UE) 궤적은 기존 디바이스를 옮김.** 각 웨이포인트/스텝은
  씬을 깊은 복사한 뒤 라우팅된 UE의 `position`과 유한 차분 `velocity_m_s`만
  변형합니다. 나머지 필드(안테나
  패턴/행/열/편파/간격, 전력, 이름, 방향)는 그대로 상속되므로, `rx_001`의
  궤적은 `rx_001`에 설정된 배열로 솔브됩니다. 다중 UE 실행(`routes`)은 **스텝
  우선(step-major)**입니다. 스텝마다 라우팅된 모든 UE를 한 번에 솔브하고, 샘플은
  스텝-0의 전체 UE, 그다음 스텝-1, ... 순으로 정렬됩니다. 각 `TrajectorySample`은
  자신의 `ue_id`를 담고, 메타데이터는 `ue_ids`/`num_steps` 리스트를 담습니다. 주의:
  Sionna는 씬 수준 `rx_array` 하나(처음 선택된 RX 디바이스의 것)만 적용하므로,
  라우팅된 UE들의 안테나 구성이 서로 다르면 첫 번째 것만 반영됩니다. routes
  경로는 무시된 UE를 알려 주는 경고를 냅니다. RFData `trajectory.csv`는 항상 `ue_id`
  열(AODT 뷰어 스키마의 고정 열)을 두고 행을 결과의 스텝 우선 순서로 담으므로, 뷰어가
  이를 UE별 시퀀스로 나눕니다. 단일 UE는 그 특수한 경우(ue_id 하나)일 뿐입니다. ML
  데이터셋 생성기는 이와 별개인 단일 UE 스윕입니다(`TrajectoryResultSet`을 쓰지 않음).
  각 `.npz`는 UE 하나의 시퀀스이고, 스윕한 UE는 `metadata.json`에
  `ue_id`/`source_rx_id`로 기록됩니다.
- **Sionna용 `mat-` BSDF 접두사.** 생성된 Mitsuba XML은 각 BSDF를
  `mat-<rf_material_id>`로 이름 짓는데, 이는 Sionna RT가 셰이프를
  `RadioMaterial` 인스턴스에 바인딩할 때 쓰는 규약입니다.
- **AI 폴백 체인: ollama → rule_based.** 제안은 로컬 Ollama 모델이 구성돼 있고
  도달 가능하면 이를 우선합니다. 그렇지 않거나 AI JSON이 유효하지 않으면
  결정론적 규칙 기반 제공자로 폴백합니다. 제안은 기본적으로 절대 자동 적용되지 않습니다.
- **이식 가능한 로컬 툴체인.** 전체 스택은 사용자 로컬의 재배치 가능한 설치본,
  즉 `backend/.venv` 아래 백엔드 venv와 프런트엔드용 이식형 Node 배포판에서
  돌아갑니다. 관리자 권한도, 시스템 서비스도, 클라우드 의존성도 필요 없습니다.
