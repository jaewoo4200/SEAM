# 씬 불러오기: Mitsuba XML · OpenStreetMap · 디바이스 JSON

> [English](scene_import.md) · **한국어**

SEAM Studio에 지오메트리를 넣는 방법은 세 가지입니다. **Mitsuba/Sionna XML
씬**(단일 `.xml` + 메시 파일, 또는 씬 폴더 전체를 묶은 `.zip`),
**OpenStreetMap 영역**(실제 건물을 3D로 돌출), 그리고 지오메트리가 아니라
무선 디바이스·UE 경로를 넣는 **JSON 포인트 임포트**입니다. 이 가이드에서 세
가지를 모두 따라 해 봅니다. 전부 Mock 백엔드만으로 동작하므로 Sionna RT가
없어도 그대로 진행할 수 있습니다.

| 가진 것 | 사용할 경로 | 결과 |
|---|---|---|
| Mitsuba/Sionna 씬 (`.xml` + 메시, 또는 `.zip`) | **`Import`** → **`Mitsuba XML`** | 씬이 새 프로젝트로; zip이면 텍스처 유지 |
| 실제 주소·지역 | **`Import`** → **`OpenStreetMap`** | 기본 RF 재질이 지정된 돌출 건물들 |
| JSON으로 된 TX/RX 위치·UE 경로 | **`⤓ JSON`**(Devices) / **`⤓ Import JSON`**(궤적) | 열린 프로젝트에 디바이스/웨이포인트 추가 |

---

## 1. Import 버튼과 Import scene 다이얼로그

상단 툴바 왼쪽(프로젝트 셀렉트 옆)의 **`Import`** 버튼을 누르세요. 소스 탭
두 개 — **`Mitsuba XML`** 과 **`OpenStreetMap`** — 가 있는 작은
**Import scene** 다이얼로그가 열립니다. 아직 프로젝트가 하나도 없다면 빈
화면의 **`Import a scene`** 버튼이 같은 다이얼로그를 띄웁니다.

![Mitsuba XML 탭이 활성화된 Import scene 다이얼로그 — 파일 선택기와 Name, Project id, Environment 필드](../images/16_import_dialog.png)

*Import scene 다이얼로그 — Mitsuba XML 탭이 활성 상태이고 옆에 OpenStreetMap 탭이 있습니다.*

어느 탭을 쓰든 아래쪽 세 필드는 공통입니다:

- **Name** — 프로젝트 셀렉트에 표시될 이름(예: `My Scene`).
- **Project id** — 폴더에 쓸 수 있는 id. 직접 고치기 전까지는 이름에서 자동
  생성됩니다. 소문자·숫자·`-`·`_` 만 허용되며, 중복 id는 그 자리에서
  거부됩니다.
- **Environment** — `Auto` / `Indoor` / `Outdoor`
  ([4절](#4-환경-모드-auto--indoor--outdoor) 참고).

파란 **Import** 버튼이 임포트를 시작하고, **Cancel** 은 다이얼로그를 닫습니다.

## 2. Mitsuba XML 탭

**`Mitsuba XML`** 탭은 Mitsuba/Sionna RT 씬을 새 프로젝트로 불러옵니다.
**Scene XML or .zip bundle** 파일 선택기에 넣을 수 있는 것은 두 가지입니다:

1. **단일 `.xml` 파일.** XML이 외부 메시를 참조한다면 두 번째 선택기
   **Mesh files (optional .ply/.obj)** 로 함께 올리세요(다중 선택,
   `.ply`/`.obj`/`.stl`).
2. **씬 폴더 전체를 묶은 `.zip`** — XML과 `meshes/`, `textures/` 디렉터리를
   원래 상대 경로 그대로 압축한 것. zip을 고르면 메시 선택기가 사라집니다.
   번들 안에 이미 다 들어 있기 때문입니다.

텍스처가 있는 씬이라면 zip을 권합니다. 다이얼로그의 힌트 그대로, zip 번들은
텍스처를 유지하므로 뷰어에 그대로 보이고 **AI 재질 제안의 입력이 됩니다** —
**AI Assist** 탭의 비전 지원 제공자가 메시 이름만 보고 추측하는 대신 실제
표면 사진을 보고 RF 재질 후보를 제안할 수 있습니다.

진행 순서:

1. 툴바의 **`Import`** 를 누르고 **`Mitsuba XML`** 탭에 그대로 둡니다.
2. **Scene XML or .zip bundle** 에서 `.xml` 또는 `.zip` 을 고릅니다.
   **Name** 필드가 파일 이름으로 자동 채워집니다.
3. (단일 XML일 때만) **Mesh files (optional .ply/.obj)** 로 동반 메시를
   추가합니다.
4. **Name** / **Project id** / **Environment** 를 확인하고 **Import** 를
   누릅니다.
5. 임포트는 백그라운드 잡으로 돌아갑니다 — 진행 행(`Importing…` + 현재
   단계와 스텝 수)이 표시되고, 그동안 다른 작업을 계속할 수 있습니다. 끝나면
   새 프로젝트가 자동으로 열리고, 치명적이지 않은 경고(건너뛴 메시, 재매핑된
   재질)는 알림으로 표시됩니다.

## 3. OpenStreetMap 탭

**`OpenStreetMap`** 탭은 실제 건물 풋프린트로 실외 씬을 만듭니다. OSM
Overpass API를 조회하므로 인터넷 연결이 필요합니다.

1. 다이얼로그를 **`OpenStreetMap`** 탭으로 전환합니다. 다이얼로그가 넓어지며
   검색창(**Search a place…**)이 있는 지도가 나타납니다 — 장소 이름을
   검색하거나 Google Maps에서 복사한 좌표를 붙여넣으세요.
2. **`▭ Select area on map`** 을 누른 뒤 지도 위에서 사각형을 드래그합니다
   (누르는 동안 버튼이 `Drag a rectangle on the map…` 으로 바뀝니다).
   **Latitude**, **Longitude**, **Width (m, E–W)**, **Height (m, N–S)**
   필드가 자동으로 채워지고, 직접 입력해도 됩니다. 영역 한 변은
   50–3000 m 로 제한됩니다.
3. **Default bldg height (m)** 를 정합니다 — OSM 데이터에 높이/층수 태그가
   없는 건물에 쓰이는 기본 높이입니다(최소 3 m).
4. **Name** / **Project id** 를 채우고 **Import** 를 누릅니다(실행 중에는
   `Fetching OSM…` 으로 표시됩니다).

임포터는 사각형 안의 모든 건물 풋프린트를 받아 OSM `height`/`levels` 태그로
(없으면 기본 높이로) 돌출시키고 지면 평면을 깔아 줍니다. 모든 프림에는 RF
재질이 미리 바인딩되어 들어옵니다: 건물은 **`itu_concrete`**, 지면은
**`ground_28ghz`**(28 GHz 대역에 안전한 지면 재질)입니다. 사각형 중심의
측지 앵커도 씬에 저장되므로, 이후 위도/경도로 디바이스·웨이포인트를 불러올
수 있습니다(5절 참고).

![OSM 임포트 결과: 수십 개의 돌출 건물이 모두 itu_concrete로 들어오고 환경은 Outdoor로 해석된 모습](../images/15_osm_import.png)

*임포트된 OSM 영역 — 돌출 건물 전부 `itu_concrete`, ENV Outdoor.*

전부 `itu_concrete` 인 것은 의도된 출발점이지 정답이 아닙니다. 이후
**RF Materials** 탭에서(개별 또는 일괄 지정) 다듬거나, **AI Assist** 탭의
**Suggest RF materials** 로 더 나은 후보를 제안받으세요 —
[RF 재질](../rf_materials.ko.md)과 [AI 어시스턴트](../ai_assistant.ko.md)를
참고하세요.

## 4. 환경 모드 (Auto / Indoor / Outdoor)

임포트 다이얼로그의 **Environment** 필드 — 그리고 이후 툴바의 **`Env`**
셀렉트 — 는 SEAM Studio에 씬의 스케일을 알려 줍니다:

- **Auto** 는 씬의 공간 범위로 실내/실외를 추론합니다(최대 스팬이 25 m
  미만이면 실내로 판단). 툴바에 추론 결과가 함께 표시됩니다
  (예: `Auto (outdoor)`).
- **Indoor** 는 좁은 공간용 솔버 기본값을 적용합니다: 경로 깊이 5 + 굴절
  켬, 라디오맵 그리드 0.25 m(높이 1.2 m).
- **Outdoor** 는 광역 기본값을 적용합니다: 경로 깊이 3, 굴절 끔, 라디오맵
  그리드 2 m(높이 1.5 m).

환경은 뷰어 크기 조정에도 반영됩니다 — 실외에서는 디바이스 마커의 최소
크기가 커져 캠퍼스 규모 씬에서도 잘 보이고, 실내에서는 경로 상호작용 점이
작아지며, 실외 씬은 기본 카메라가 더 넓게 잡힙니다. 나중에 **`Env`** 를
바꾸면 해당 솔버 기본값이 Paths/Radio map 섹션에 세션 한정으로 적용됩니다
(직접 조정한 값은 실제로 환경을 전환할 때만 덮어써지고, 명시적으로 저장하지
않는 한 프로젝트 기본값으로 저장되지 않습니다).

## 5. 디바이스·궤적 JSON 불러오기

씬 임포트는 지오메트리를 가져오고, 무선 디바이스와 UE 경로는 열려 있는 어느
프로젝트에서든 쓸 수 있는 별도의 JSON 임포트가 있습니다:

- **디바이스** — 씬 트리 **Devices** 섹션 헤더에서 **`+TX`** / **`+RX`**
  옆의 **`⤓ JSON`** 을 누르고 JSON 파일을 고르세요. 디바이스는 씬에
  업서트되고(기존 id는 제자리에서 갱신), 몇 개가 추가/갱신됐는지와 경고가
  알림으로 표시됩니다.
- **궤적 웨이포인트** — **Results** 모드의 궤적 경로 편집기에서
  **`⤓ Import JSON`** 을 누르면 뷰포트에서 직접 그리는 대신 파일로 UE
  경로를 불러올 수 있습니다.

둘 다 같은 포인트 스키마를 받습니다: **직교(cartesian)** 포인트
(`x`/`y`/`z`, 로컬 ENU 미터, Z-up) 또는 **지리(geographic)** 포인트
(`lat`/`lon` + `alt_m` 또는 `agl_m`)를 항목마다 자동 감지하며, 한 파일 안에
섞어 써도 됩니다. 지리 포인트는 씬에 측지 앵커가 있어야 하는데 — OSM
임포트가 자동으로 만들어 줍니다. `agl_m`(지면 위 높이) 의미론, 방향 필드,
바로 쓸 수 있는 템플릿까지 포함한 전체 포맷은
[포인트/디바이스/궤적 임포트](../point_import.ko.md)에 있습니다.

## 6. 드론 스캔 텍스처 번들은 어떤 모습인가

내장 예제 **FTC Outdoor** 는 텍스처가 있는 zip 임포트가 어디까지 갈 수
있는지 보여 줍니다: 포토그래메트리(드론 스캔) 건물의 사진 텍스처가 뷰어에
그대로 렌더링되고, 주변에는 OSM으로 불러온 컨텍스트 건물과 지형이 깔리며,
UAV 액터가 노란 비행 경로를 따라 부지 위를 날아다닙니다.

![임포트된 드론 스캔 번들: 사진 텍스처 건물과 OSM 컨텍스트 건물·지형, 노란 비행 경로를 가진 UAV 액터](../images/11_ftc_textured.png)

*FTC Outdoor 씬 — 사진 텍스처 드론 스캔 건물 + OSM 컨텍스트, UAV 비행 경로.*

텍스처가 임포트를 거쳐도 살아 있기 때문에 이런 씬은 전체 파이프라인을 그대로
탑니다: **AI Assist** 탭이 눈에 보이는 표면(콘크리트/유리/금속 분할)을 근거로
추론할 수 있고, 이후의 경로·라디오맵·궤적 시뮬레이션이 모두 같은 지오메트리
위에서 돌아갑니다. 아무것도 임포트하지 않고 구경만 하려면 툴바의 프로젝트
셀렉트에서 `FTC Outdoor` 프로젝트를 여세요.

## 문제 해결

- **Project id가 중복**이면 그 자리에서 거부됩니다(`a project "…" already
  exists`) — 다른 id를 넣어야 Import 버튼이 활성화됩니다.
- **OpenStreetMap 임포트가 실패하거나 오래 걸리면** — Overpass API는
  인터넷이 필요하고 아주 큰 사각형에서는 타임아웃될 수 있습니다. 오류는
  다이얼로그 안에 표시됩니다. 다시 시도하거나 선택 영역을 줄이세요.
- **임포트 도중 다이얼로그를 닫으면**(XML 탭) 진행 표시는 멈추지만 잡
  자체는 서버에서 계속 돌아갑니다 — 끝나면 프로젝트 셀렉트에 새 프로젝트가
  나타납니다.

---

## 관련 문서

- [포인트/디바이스/궤적 임포트](../point_import.ko.md) — 5절에서 쓰는 JSON 포맷
- [RF 재질](../rf_materials.ko.md) — 기본 `itu_concrete` 지정 다듬기
- [AI 어시스턴트](../ai_assistant.ko.md) — 텍스처를 활용한 재질 제안
- [씬 포맷](../scene_format.ko.md) — 임포트된 프로젝트의 디스크 구조
- [15분 튜토리얼](../../TUTORIAL.ko.md) — 첫 세션 전체 흐름
