# Point / Device / Trajectory Import

> 🌐 [English](point_import.md) · **한국어**

> **요약 (Korean)**
> 표준 JSON 하나로 디바이스(TX/RX/UE)와 UE 궤적 웨이포인트를 가져온다.
> 각 점이 **직교(로컬 ENU, Z-up, 미터)** 인지 **지리(WGS84 lat/lon)** 인지 점
> 단위로 자동 감지한다. 지리 좌표를 쓰려면 씬에 측지 기준점
> (`coordinate_system.origin_lat_lon_alt`, OSM import가 설정)이 반드시 있어야
> 하며, WGS84 geodetic→ECEF→ENU로 변환된다. `agl_m`은 "지표면 위 높이"로,
> 시각 메시에 수직으로 레이캐스트해 `z = 표면 + agl_m`가 된다. 명시적 `z`가 그
> 아래 표면보다 낮으면 경고만 남기고 값은 그대로 둔다(자동 보정 안 함).
> 엔드포인트: `POST .../import/devices`, `POST .../import/trajectory`,
> `GET /api/import/templates`.

사용자가 손으로 직접 작성하거나 GPS 도구에서 내보낸 단일 JSON 스키마로 무선
디바이스와 UE 궤적 웨이포인트를 가져온다. 각 점은 **직교**(로컬 ENU 미터, Z-up)
또는 **지리**(WGS84 위도/경도)일 수 있고, 점 단위로 감지되며 두 형식을 한 파일
안에 섞어 쓸 수 있다.

스키마: `backend/app/schemas/point_import.py`. 해석 로직:
`backend/app/services/point_import.py`. 라우트:
`backend/app/api/point_import.py`.

## Coordinate rules

- **직교** 점은 정규 씬 프레임이다: **로컬 ENU 미터,
  Z-up** — `x` = East, `y` = North, `z` = Up. 앵커가 필요 없다.
- **지리** 점은 **WGS84** 도(degree) 단위다. 이런 점에는 씬의
  측지 앵커 `coordinate_system.origin_lat_lon_alt`(`[lat_deg, lon_deg,
  alt_m]`)가 **반드시 있어야 하며**, 이 앵커는 OSM import가 설정한다. 앵커가 없으면 요청은 **400**으로 실패한다:

  > `scene has no geodetic anchor (coordinate_system.origin_lat_lon_alt); use
  > cartesian x/y/z or import the scene via OSM`

  변환은 앵커를 기준으로 한 표준 닫힌 형식(closed-form) **geodetic → ECEF → local ENU**
  다(WGS84 타원체, 직접 구현, 추가 의존성 없음). 검산 삼아
  보면, 앵커에서 북쪽으로 위도 0.001°는 `y`에서 ≈ +111.32 m에 놓인다.
  절대 고도는 `z = alt_m - origin_alt`로 ENU up에 매핑된다.

## Point forms

점을 받는 곳이면 어디서든 다음 형식 중 하나로 점을 지정할 수 있다:

| Form | Example | Notes |
| --- | --- | --- |
| array `[x, y]` | `[12.0, -4.0]` | 직교; `z`는 기본값 0(또는 기본 AGL) |
| array `[x, y, z]` | `[30.0, 5.0, 1.5]` | 직교 |
| `{x, y, z?}` | `{"x": 12, "y": -4, "z": 1.5}` | 직교 |
| `{x, y, agl_m?}` | `{"x": 0, "y": 0, "agl_m": 1.5}` | 직교 XY, AGL 높이 |
| `{lat, lon, alt_m?}` | `{"lat": 37.5563, "lon": 127.0448, "alt_m": 45.2}` | 지리, 절대 높이 |
| `{lat, lon, agl_m?}` | `{"lat": 37.5561, "lon": 127.0451, "agl_m": 1.5}` | 지리, AGL 높이 |

자동 감지: **`lat`과 `lon`이 둘 다** 있으면 지리로 해석하고,
그렇지 않으면 직교로 본다. 한 점에서 `x`/`y`와 `lat`/`lon`을
섞거나 `lat`/`lon` 중 하나만 주면 **400**이다.

## AGL semantics

`agl_m`은 **씬 표면 위 높이**다. 각 AGL 점은 시각 메시
(`app.services.terrain.snap_to_terrain`)에 수직으로 레이캐스트해
`z = surface + agl_m`로 해석한다. 덕분에 디바이스나
웨이포인트가 경사진 지면 위에서도 일정한 안테나 높이를 유지한다.

- 점 아래에 아무것도 없으면(메시 풋프린트 밖이거나, 씬에 시각
  메시가 없는 경우) `agl_m` 값을 **절대 z**로 그대로 두고 경고를
  남긴다.
- `agl_m`과 `z`/`alt_m`은 점마다 **상호 배타적**이다. 둘 다
  주면 **AGL이 우선**하고 경고를 남긴다.
- 궤적 import는 `z`도 `agl_m`도 주지 않은 웨이포인트에 **기본 AGL**
  (요청 본문의 `agl_m`, 기본값 `1.5`)을 적용한다. 그런 웨이포인트를 `z = 0`에 두려면
  `"agl_m": null`을 전달한다.

## Underground warnings

**명시적 `z`**(또는 지리 `alt_m`)로 준 점도 그 아래 표면
z를 어차피 계산한다. 점이 그 표면 아래에 있으면
(`z < surface - 0.05 m`) 경고를 덧붙인다:

> `device 'ue_01' sits 3.0 m below the surface under it`

명시적 `z`는 **절대 자동 보정하지 않고** 표시만 한다. (AGL 점은
정의상 표면 위에 있으므로 지하로 내려가지 않는다.)

## Endpoints

### `POST /api/projects/{project_id}/import/devices`

씬에 디바이스를 upsert하거나 추가한다. `id`를 생략하면 (`tx_00N` / `rx_00N`)
형식으로 자동 생성하며, `kind`는 기본값이 `rx`다.

Request:

```jsonc
{
  "mode": "upsert",          // "upsert" (default) | "add"
  "devices": [
    { "id": "tx_001", "kind": "tx", "position": [0, 0, 10], "power_dbm": 30 },
    { "kind": "rx", "x": 12, "y": -4, "agl_m": 1.5, "name": "car UE" },
    { "id": "ue_geo", "lat": 37.5563, "lon": 127.0448, "alt_m": 45.2 }
  ]
}
```

- `mode: "upsert"`는 `id`가 일치하는 디바이스를 제자리에서(position,
  orientation, power 등) 갱신하고 경고를 남긴다. `mode: "add"`는 id가 충돌하면 **409**를
  반환한다.
- 각 디바이스는 위치를 `position`(임의의 점 형식)이나
  최상위 좌표 필드(`x`/`y`/`z` 또는 `lat`/`lon`/`alt_m`/`agl_m`)로 지정한다.
- 선택적 패스스루 필드: `name`, `orientation_deg`(`[yaw, pitch,
  roll]`), `power_dbm`, `velocity_m_s`, `antenna`, `color`.

Response:

```json
{ "added_ids": ["tx_001", "rx_001", "ue_geo"], "updated_ids": [], "warnings": [] }
```

`device_import` 프로비넌스(provenance) 이벤트를 덧붙인다
(`{"type": "device_import", "count": N, "warnings": [...]}`).

```bash
curl -X POST http://localhost:8000/api/projects/my_project/import/devices \
  -H 'Content-Type: application/json' \
  -d '{
        "mode": "upsert",
        "devices": [
          { "id": "tx_001", "kind": "tx", "position": [0, 0, 10], "power_dbm": 30 },
          { "kind": "rx", "x": 12, "y": -4, "agl_m": 1.5, "name": "car UE" }
        ]
      }'
```

### `POST /api/projects/{project_id}/import/trajectory`

궤적 라우트 UI를 위해 UE 궤적 웨이포인트를 완전한 직교 `[x, y, z]`로
해석한다. **씬을 변경하지 않는다.**

Request:

```jsonc
{
  "ue_id": "ue_01",          // optional; echoed back, not resolved against the scene
  "agl_m": 1.5,              // optional default height for points lacking z/agl (null => z = 0)
  "points": [
    { "x": 0, "y": 0, "agl_m": 1.5, "orientation_deg": [0, 0, 0] },   // per-waypoint antenna aim
    { "lat": 37.5560, "lon": 127.0450, "agl_m": 1.5, "orientation_deg": [90, 0, 0] },
    [30.0, 5.0, 1.5]         // bare arrays carry no orientation
  ]
}
```

객체 웨이포인트는 `orientation_deg`(`[yaw, pitch, roll]` 도)를 함께 실을 수 있다.
해석된 각 스텝은 이동 중인 UE의 안테나를 **가장 가까운 웨이포인트의**
방향으로 겨냥하므로(웨이포인트 사이에서는 조각별 상수), 방향을 트는 UE의 빔도
함께 돈다 — Sionna는 이를 반영하고, Mock 백엔드는 등방성이라 영향이 없다.
방향이 없는 웨이포인트는 디바이스에 설정된 방향을 그대로 유지한다.

Response:

```json
{ "ue_id": "ue_01",
  "waypoints": [[0,0,1.5],[12.3,-4.1,1.5],[30,5,1.5]],
  "orientations_deg": [[0,0,0],[90,0,0],null],
  "warnings": [] }
```

`orientations_deg`는 `waypoints`와 같은 순서로 나열되며(방향을 주지 않은 점은 null),
어떤 웨이포인트에도 방향이 없으면 통째로 생략된다. 프런트엔드는
궤적 해석을 위해 이를 `UERoute.orientations_deg`에 넣는다.

```bash
curl -X POST http://localhost:8000/api/projects/my_project/import/trajectory \
  -H 'Content-Type: application/json' \
  -d '{
        "ue_id": "ue_01",
        "points": [ {"x":0,"y":0,"agl_m":1.5}, [30.0, 5.0, 1.5] ]
      }'
```

### `GET /api/import/templates`

두 엔드포인트의 예시 페이로드, 손으로 작성한 결합 파일 예시,
모든 필드를 설명하는 `field_reference`를 담은 정적 JSON이다(프로젝트 불필요). 덕분에
프런트엔드가 다운로드 가능한 자기 설명형 템플릿을 제공할 수 있다.

```bash
curl http://localhost:8000/api/import/templates
```

## Template (combined file)

```jsonc
{
  "devices": [
    { "id": "ue_01", "kind": "rx",
      "position": [12.0, -4.0, 1.5],
      "orientation_deg": [90, 0, 0],
      "power_dbm": 23.0, "name": "car UE" },
    { "id": "ue_02", "lat": 37.5563, "lon": 127.0448, "alt_m": 45.2 },
    { "id": "ue_03", "lat": 37.5561, "lon": 127.0451, "agl_m": 1.5 }
  ],
  "trajectories": [
    { "ue_id": "ue_01",
      "points": [
        {"x": 0, "y": 0, "agl_m": 1.5},
        {"lat": 37.5560, "lon": 127.0450, "agl_m": 1.5},
        [30.0, 5.0, 1.5]
      ] }
  ]
}
```

`devices` 배열은 `POST .../import/devices`로 보낸다. `trajectories`의 각
항목은 하나의 `POST .../import/trajectory` 호출(`ue_id` + `points`)로 들어간다.
