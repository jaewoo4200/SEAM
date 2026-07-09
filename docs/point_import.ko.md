# Point / Device / Trajectory Import

> 🌐 [English](point_import.md) · **한국어**

> **요약 (Korean)**
> 표준 JSON 하나로 디바이스(TX/RX/UE)와 UE 궤적 웨이포인트를 가져온다.
> 좌표는 **직교(로컬 ENU, Z-up, 미터)** 와 **지리(WGS84 lat/lon)** 를 점 단위로
> 자동 감지한다. 지리 좌표는 씬의 측지 기준점
> (`coordinate_system.origin_lat_lon_alt`, OSM import가 설정)이 반드시 있어야
> 하며, WGS84 geodetic→ECEF→ENU로 변환된다. `agl_m`은 "지표면 위 높이"로,
> 시각 메시로 수직 레이캐스트해 `z = 표면 + agl_m`가 된다. 명시적 `z`가 그
> 아래 표면보다 낮으면 경고만 남기고 값은 그대로 둔다(자동 보정 안 함).
> 엔드포인트: `POST .../import/devices`, `POST .../import/trajectory`,
> `GET /api/import/templates`.

사용자가 직접 손으로 작성하거나 GPS 도구에서 내보낼 수 있는 단일 JSON 스키마로부터
무선 디바이스와 UE 궤적 웨이포인트를 가져온다. 점은 **직교**(로컬 ENU 미터, Z-up)
또는 **지리**(WGS84 위도/경도)일 수 있으며, 점 단위로 감지되고 두 형식을 하나의
파일 안에서 섞어 쓸 수 있다.

스키마: `backend/app/schemas/point_import.py`. 해석 로직:
`backend/app/services/point_import.py`. 라우트:
`backend/app/api/point_import.py`.

## Coordinate rules

- **직교** 점은 정규 씬 프레임이다: **로컬 ENU 미터,
  Z-up** — `x` = East, `y` = North, `z` = Up. 앵커가 필요 없다.
- **지리** 점은 **WGS84** 도(degree) 단위다. 이 점들은 씬의
  측지 앵커 `coordinate_system.origin_lat_lon_alt`(`[lat_deg, lon_deg,
  alt_m]`)를 **필요로 하며**, 이는 OSM import가 설정한다. 이것이 없으면 요청은 **400**으로 실패한다:

  > `scene has no geodetic anchor (coordinate_system.origin_lat_lon_alt); use
  > cartesian x/y/z or import the scene via OSM`

  변환은 앵커를 기준으로 한 표준 닫힌 형식(closed-form) **geodetic → ECEF → local ENU**
  이다(WGS84 타원체, 직접 구현, 추가 의존성 없음). 온전성
  점검으로, 앵커에서 북쪽으로 위도 0.001°는 `y`에서 ≈ +111.32 m에 위치한다.
  절대 고도는 `z = alt_m - origin_alt`로 ENU up에 매핑된다.

## Point forms

점이 기대되는 어느 위치에서든 다음 형식 중 하나로 점이 허용된다:

| Form | Example | Notes |
| --- | --- | --- |
| array `[x, y]` | `[12.0, -4.0]` | 직교; `z`는 기본값 0(또는 기본 AGL) |
| array `[x, y, z]` | `[30.0, 5.0, 1.5]` | 직교 |
| `{x, y, z?}` | `{"x": 12, "y": -4, "z": 1.5}` | 직교 |
| `{x, y, agl_m?}` | `{"x": 0, "y": 0, "agl_m": 1.5}` | 직교 XY, AGL 높이 |
| `{lat, lon, alt_m?}` | `{"lat": 37.5563, "lon": 127.0448, "alt_m": 45.2}` | 지리, 절대 높이 |
| `{lat, lon, agl_m?}` | `{"lat": 37.5561, "lon": 127.0451, "agl_m": 1.5}` | 지리, AGL 높이 |

자동 감지: **`lat`과 `lon`이 모두** 존재하면 지리 해석을
선택하고, 그렇지 않으면 점은 직교다. 하나의 점에서 `x`/`y`와 `lat`/`lon`을
섞거나, `lat`/`lon` 중 하나만 주면 **400**이다.

## AGL semantics

`agl_m`은 **씬 표면 위 높이**다. 각 AGL 점은 시각 메시
(`app.services.terrain.snap_to_terrain`)로 곧장 아래로 레이캐스트하여
`z = surface + agl_m`를 취함으로써 해석되므로, 디바이스나
웨이포인트가 경사진 지면 위에서 일정한 안테나 높이를 유지한다.

- 점 아래에 아무것도 없으면(메시 풋프린트 밖이거나, 씬에 시각
  메시가 없는 경우) `agl_m` 값은 **절대 z**로 유지되고 경고가
  발생한다.
- `agl_m`과 `z`/`alt_m`은 점마다 **상호 배타적**이다. 둘 다
  주어지면 **AGL이 우선**하고 경고가 발생한다.
- 궤적 import는 `z`도 `agl_m`도 주지 않은 웨이포인트에 **기본 AGL**
  (요청 본문의 `agl_m`, 기본값 `1.5`)을 적용한다. 그러한 웨이포인트를 대신 `z = 0`에 두려면
  `"agl_m": null`을 전달한다.

## Underground warnings

**명시적 `z`**(또는 지리 `alt_m`)로 주어진 점의 경우, 그 아래 표면
z가 어쨌든 계산된다. 점이 그 표면 아래에 위치할 때
(`z < surface - 0.05 m`) 경고가 추가된다:

> `device 'ue_01' sits 3.0 m below the surface under it`

명시적 `z`는 **결코 자동 보정되지 않고** — 표시만 될 뿐이다. (AGL 점은
구성상 표면 위에 있으며 결코 지하에 있지 않다.)

## Endpoints

### `POST /api/projects/{project_id}/import/devices`

씬에 디바이스를 upsert하거나 추가한다. `id`가 생략되면 id(`tx_00N` / `rx_00N`)를
자동 생성하며, `kind`는 기본값이 `rx`다.

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

- `mode: "upsert"`는 일치하는 `id`를 가진 디바이스를 제자리에서(position,
  orientation, power 등) 갱신하며 경고를 남긴다. `mode: "add"`는 id 충돌 시 **409**를
  반환한다.
- 각 디바이스는 위치를 `position`(임의의 점 형식)으로 주거나
  최상위 좌표 필드(`x`/`y`/`z` 또는 `lat`/`lon`/`alt_m`/`agl_m`)로 준다.
- 선택적 패스스루 필드: `name`, `orientation_deg`(`[yaw, pitch,
  roll]`), `power_dbm`, `velocity_m_s`, `antenna`, `color`.

Response:

```json
{ "added_ids": ["tx_001", "rx_001", "ue_geo"], "updated_ids": [], "warnings": [] }
```

`device_import` 프로비넌스(provenance) 이벤트가 추가된다
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

객체 웨이포인트는 `orientation_deg`(`[yaw, pitch, roll]` 도)를 가질 수 있다.
해석된 각 스텝은 이동하는 UE의 안테나를 **가장 가까운 웨이포인트의**
방향으로 겨냥하며(웨이포인트 사이에서 조각별 상수), 회전하는 UE의 빔이
그와 함께 회전한다 — Sionna는 이를 존중하고, Mock 백엔드는 등방성이라 영향받지 않는다.
방향이 없는 웨이포인트는 디바이스에 작성된 방향을 유지한다.

Response:

```json
{ "ue_id": "ue_01",
  "waypoints": [[0,0,1.5],[12.3,-4.1,1.5],[30,5,1.5]],
  "orientations_deg": [[0,0,0],[90,0,0],null],
  "warnings": [] }
```

`orientations_deg`는 `waypoints`와 병렬이며(점이 아무것도 주지 않은 곳은 null),
어떤 웨이포인트도 방향을 갖지 않으면 전체가 생략된다. 프런트엔드는
이를 궤적 해석을 위해 `UERoute.orientations_deg`에 넣는다.

```bash
curl -X POST http://localhost:8000/api/projects/my_project/import/trajectory \
  -H 'Content-Type: application/json' \
  -d '{
        "ue_id": "ue_01",
        "points": [ {"x":0,"y":0,"agl_m":1.5}, [30.0, 5.0, 1.5] ]
      }'
```

### `GET /api/import/templates`

두 엔드포인트를 위한 예시 페이로드, 결합된 손 작성 파일 예시, 그리고
모든 필드를 설명하는 `field_reference`를 담은 정적 JSON(프로젝트 불필요) — 그래서
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

`devices` 배열은 `POST .../import/devices`에 공급된다. `trajectories`의 각
항목은 하나의 `POST .../import/trajectory` 호출(`ue_id` + `points`)에 공급된다.
