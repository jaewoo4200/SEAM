# 포인트 임포트 예제 파일

> [English](README.md) · **한국어**

두 임포트 버튼에 바로 넣을 수 있는 JSON 예제입니다 — 씬 트리 DEVICES 헤더의
**`⤓ JSON`**, 그리고 Results 모드 **UE trajectory** 패널의 **`⤓ Import JSON`**.
형식 설명: [docs/point_import.md](../../docs/point_import.ko.md).

| 파일 | 사용할 프로젝트 | 보여주는 것 | 기대 결과 |
| --- | --- | --- | --- |
| `devices_sample_demo.json` | Sample Demo | 직교 좌표 디바이스: 명시적 `z`, `agl_m`, `position` 배열 세 가지 형태 + `orientation_deg`, `power_dbm` 전달 | 토스트 **“Imported devices — 3 added, 0 updated”**; 같은 파일 재임포트 → **“0 added, 3 updated”** (`upsert`) |
| `devices_geographic_hyu.json` | **OSM으로 임포트한** 프로젝트 (기본 한양대 영역) | 지리 좌표 `lat`/`lon` + `agl_m` — 씬의 측지 앵커로 변환 | 임포트 영역 안에 TX 1 + RX 2 배치. 앵커가 **없는** 씬(예: Sample Demo)에서는 *“scene has no geodetic anchor …”* 오류가 떠야 정상 — 이 오류 자체가 네거티브 테스트 |
| `trajectory_sample_demo.json` | Sample Demo (UE trajectory 패널, RX 선택 상태) | 웨이포인트 객체 + 배열 형태 혼용, `agl_m` 높이 | 토스트 **“Imported 5 waypoint(s) for rx_001”**; 데모 씬을 가로지르는 5점 라우트 행 생성 |
| `trajectory_oriented.json` | Sample Demo | 웨이포인트별 `orientation_deg` (경로를 따라 안테나 방향 회전) | 토스트에 **“… (with orientation)”** + 라우트 행에 **`· oriented`** 칩 |
| `trajectory_underground_warning.json` | Sample Demo | 지표 아래의 명시적 `z`는 자동 보정 없이 경고만 | 임포트는 성공하되 가운데 웨이포인트가 지표 아래 ≈3 m라는 ⚠ 경고가 라우트 행에 남음 |

팁

- 디바이스 버튼은 `{"mode": …, "devices": …}` 래퍼 없이 순수 배열도 받습니다.
  궤적 버튼도 마찬가지로 포인트 배열 `[...]`만 넣어도 됩니다.
- 디바이스에서 `"id"`를 생략하면 `tx_00N`/`rx_00N`이 자동 부여됩니다.
- `GET /api/import/templates`가 전체 필드 레퍼런스와 함께 같은 예제를 제공합니다.
