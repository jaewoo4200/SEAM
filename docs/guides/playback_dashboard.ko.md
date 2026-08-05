# 멀티모달 재생: 실측 현실 vs 디지털 트윈

> [English](playback_dashboard.md) · **한국어**

실제 주행/비행 기록 — 카메라 프레임, LiDAR 스윕, 실측 빔 파워 프로파일,
트래킹된 UE 포즈 — 이 있다면, SEAM Studio는 그것을 프레임 단위로 **디지털
트윈과 맞대어** 재생할 수 있습니다: 기록된 프레임마다 RX를 실측 포즈로 옮겨
레이 트레이싱 솔브와 코드북 빔 스윕을 돌리고, 결과를 하나의 *재생 팩*으로
저장해 영상처럼 스크럽합니다. Results 모드의 재생 패널은 그라운드 트루스와
트윈을 나란히 보여주고(월드 방위축을 공유하는 빔 곡선, 카메라, LiDAR, KPI),
3D 뷰포트는 그 프레임의 레이와 함께 **실측 스윕 곡선 그 자체가 모양이 되는
빔 로브**를 재생합니다 — 파워가 집중된 스윕은 좁은 스파이크로, 퍼진 스윕은
넓은 로브로 그려집니다. 로브에 미리 정해진 모양은 없습니다.

여기의 모든 것은 **mock 백엔드**로도 동작합니다 (값은 mock 솔버가 만들지만
매니페스트→빌드→재생→로브 파이프라인 전체가 Sionna RT 없이 끝까지 돕니다).

## 1. 기록 등록: `sensor_data/manifest.json`

기록을 *프로젝트 폴더 안에* 두고 매니페스트 하나로 기술합니다 (파일은 일반
에셋 라우트로 서빙되므로 프로젝트 상대 경로가 필수입니다):

```
my_project.seam/
  sensor_data/
    manifest.json
    camera/2764.jpg        # 브라우저가 렌더하는 이미지
    lidar/2764.pcd         # PCD v0.7, ascii 또는 binary, xyz [+ packed rgb]
    gt_beam/2764.json      # {"azimuth_deg": [...], "power_dbm": [...]}
```

`manifest.json` (전체 스키마: `backend/seam_studio/schemas/sensors.py`):

```json
{
  "version": 1,
  "entity_id": "ue",
  "channels": [
    {"key": "camera",  "kind": "image",        "label": "전방 카메라"},
    {"key": "lidar",   "kind": "pointcloud",   "label": "루프 LiDAR"},
    {"key": "gt_beam", "kind": "beam_profile", "label": "실측 빔 파워"}
  ],
  "frames": [
    {
      "index": 2764,
      "time_s": 276.4,
      "files": {"camera": "sensor_data/camera/2764.jpg",
                "lidar": "sensor_data/lidar/2764.pcd",
                "gt_beam": "sensor_data/gt_beam/2764.json"},
      "pose": {"position": [-44.7, 94.3, 1.68], "orientation_deg": [0, 0, 0]},
      "gt": {"rss_dbm": -93.2, "rss_coherent_dbm": -95.1,
             "tau_rms_ns": 12.4, "n_paths": 18, "best_beam_deg": -51.5}
    }
  ]
}
```

- `index`는 데이터셋 고유의 프레임 키입니다 (DeepVerse `scene_N`, rosbag
  시퀀스 번호 등). `time_s`가 중요합니다: 주행 데이터셋은 보통 여러 세그먼트의
  연결이고, 서버는 프레임 시간 점프에서 재생 **시퀀스**를 분할합니다 — 시간이
  없으면 전체가 하나의 시퀀스가 됩니다.
- `pose`는 씬 좌표(Z-up ENU 미터)의 실측 UE 포즈입니다. 포즈 없는 프레임은
  팩 빌더가 건너뜁니다 (경고와 함께).
- `gt` 스칼라와 `beam_profile` 곡선은 선택입니다 — 그라운드 트루스가 없는
  칸은 패널에 대시(—)로 표시됩니다. 빔 프로파일의 방위각은 **월드 방위각
  도 단위**입니다 (+Z축 기준 atan2(y, x)).

`GET /projects/{id}/sensors`가 매니페스트와 감지된 시퀀스를 돌려주며,
프로젝트를 열면 자동으로 로드되어 Results 모드에 재생 패널이 나타납니다.

## 2. 재생 팩 빌드

재생 패널의 **Build playback pack** 버튼(또는 POST
`/projects/{id}/simulate/playback`)을 누르면, 프레임마다 RX를 기록된 포즈로
*메모리에서만* 옮기고(저장된 씬은 절대 수정되지 않음) paths 솔브와 코드북
스윕을 실행해 월드 방위로 재정사영된 빔 곡선, RSS(논코히런트+코히런트),
τ_rms, 경로 수, 뷰포트용 최강 레이를 남깁니다. 진행률과 취소는 다른 솔브와
동일하게 동작합니다.

정확도에 중요한 노브 두 개:

- **`use_device_orientation` (여기서는 기본 true).** 고정 방위 기지국은 배열
  브로드사이드 기준으로 스윕합니다 — TX 디바이스의 `orientation_deg` yaw를
  배열 방위로 설정하세요. 기본 look-at 동작으로는 스윕 축이 매 프레임 UE를
  다시 조준해 빔 *궤적*이 물리적으로 무의미해집니다.
- **흡수/주파수**는 시뮬레이션 config에서 옵니다 — 60 GHz라면
  `atmospheric_absorption`을 켜세요 (accuracy 문서 참조). 안 켜면 GT 비교에
  거리 비례 편향이 들어갑니다.

팩은 일반 결과셋(`kind: "playback"`)으로 영속됩니다 — 런 히스토리, 라벨,
프루닝이 전부 적용됩니다. 팩은 기록된 포즈에 대한 솔브의 *역사적* 기록이라
씬을 편집해도 스테일로 표시되지 않습니다.

## 3. 대시보드 읽는 법

- **빔 차트** — GT(청록)와 SEAM(호박색)을 하나의 월드 방위축에 겹침 + 각자의
  best-beam 마커: 스크럽하는 동안 두 피크가 함께 움직이면 빔 추적이 맞는
  것입니다.
- **LiDAR 패널** — 탑다운 + 사용자 토글: 프레이밍(전체/UE 중심 줌),
  색(저고도 구간을 넓힌 높이 음영 — 0.5 m 지물이 살아남습니다 — 또는 PCD에
  있으면 시맨틱 색).
- **KPI** — GT vs SEAM: RSS, 코히런트 RSS, τ_rms, 경로 수, best beam, 그리고
  양쪽이 다 있는 행의 Δ.
- **뷰포트** — 프레임의 레이, 움직이는 RX 마커, 동적 TX 빔 로브. 로브는 재생
  밖에서도 동작합니다: 코드북 스윕 빔포밍 결과가 있으면 오버레이 행의
  Beam lobe 토글로 그려집니다.
