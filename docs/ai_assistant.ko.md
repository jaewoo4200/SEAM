# AI 어시스턴트

> 🌐 [English](ai_assistant.md) · **한국어**

AI 어시스턴트는 시각적 증거로부터 prim에 대한 RF 재질을 제안합니다:
객체 이름, GLB 재질 이름, 시맨틱 태그, 텍스처 이름. 이는
철저히 선택 사항이며 — 앱은 수동 전용으로도 완전히 사용 가능합니다 — 스스로
씬을 변경하는 일은 절대 없습니다.

## 프로바이더 추상화

`app.services.ai_provider`는 API 계층에서 사용하는 두 개의 진입점을 노출합니다:

```python
get_provider_statuses() -> list[AIProviderStatus]
suggest_materials(scene, library, request: SuggestMaterialsRequest) -> MaterialSuggestionResponse
```

이들 뒤에는 프로바이더 체인이 자리합니다:

| provider | needs | behavior |
|---|---|---|
| `rule_based` | nothing | 이름 / 시각 재질 이름 / 태그에 대한 결정론적 키워드 규칙 (window→`itu_glass`, brick→`itu_brick`, road→`asphalt_custom`, ...) |
| `local_openai` | 도달 가능한 OpenAI 호환 서버 (LM Studio 등, `SIONNATWIN_OPENAI_URL`) | prim 증거로 로컬 LLM에 프롬프트를 보내고 엄격한 JSON을 돌려받음. **비전 지원**: 스크린샷이 있으면 요청이 멀티모달(`image_url`)이 됨; 로드된 모델이 이미지를 거부하면 폴백 전에 텍스트 전용으로 재시도함 |
| `ollama_text` | 도달 가능한 Ollama 서버 + 텍스트 모델 | Ollama chat API를 통한 동일한 계약. **비전 지원**: 스크린샷이 base64 `images`로 첨부되고 호출이 `SIONNATWIN_AI_VISION_MODEL`로 전환됨 (경고가 모델 교체를 알림); 이미지 거부 시 텍스트 전용으로 재시도함 |
| `disabled` | — | 제안을 반환하지 않음 (AI 꺼짐) |

선택: `SuggestMaterialsRequest.provider`는 특정 프로바이더를 강제하며,
그렇지 않으면 사용 가능한 최선의 프로바이더가 사용됩니다 (`local_openai` → `ollama_text` →
`rule_based`). 서버가 도달 불가능하거나, 타임아웃되거나, 스키마 검증에
실패하는 JSON을 반환하면, 응답은 체인을 따라 폴백하며 무슨 일이
있었는지를 `warnings`에 기록합니다. 응답의 `provider`/`model` 필드는
항상 *실제로* 결과를 만들어낸 것을 명시합니다.

**라이브 검증됨**: LM Studio + `google/gemma-4-31b` 엔드투엔드 (텍스트 및
멀티모달), 모든 제안의 library-id 검증 포함 — 모델은
프로젝트의 RF 라이브러리에 없는 재질 id를 도입할 수 없습니다.

모든 Ollama 접근은 지연(lazy) 방식입니다(함수 내부에서 import/probe): AI 서버,
GPU, 호환 모델 그 어느 것도 다른 기능이 동작하는 데 필요하지 않으며,
`/api/health`는 `get_provider_statuses()`를 통해 각 프로바이더의 가용성을
보고합니다.

## 구성(Configuration)

환경 변수 (`app.core.config.get_settings()`가 한 번 읽음).
이름은 정규 `SEAM_*` 접두사를 사용하며, 레거시 `SIONNATWIN_*` 이름도
여전히 모든 변수에 대해 허용됩니다 (`SEAM_*`는 둘 다 설정되면 우선함).

| variable | default | meaning |
|---|---|---|
| `SEAM_AI_ENABLED` | `auto` | `auto` (도달 가능하면 사용) \| `on` \| `off` |
| `SEAM_OLLAMA_URL` | `http://localhost:11434` | Ollama 엔드포인트 |
| `SEAM_AI_TEXT_MODEL` | `qwen3:8b` | Ollama 텍스트 모델 |
| `SEAM_AI_VISION_MODEL` | `qwen2.5vl:3b` | 스크린샷이 첨부될 때 사용되는 Ollama 비전 모델 (예: LLaVA를 사용하려면 `llava`로 설정) |
| `SEAM_OPENAI_URL` | `http://localhost:1234/v1` | OpenAI 호환 엔드포인트 (LM Studio 기본 포트) |
| `SEAM_OPENAI_MODEL` | `google/gemma-4-31b` | OpenAI 호환 서버가 제공하는 모델 id |
| `SEAM_AI_TIMEOUT_S` | `60` | 텍스트 전용 호출의 요청 타임아웃 (초) |
| `SEAM_AI_VISION_TIMEOUT_S` | `300` | 멀티모달(이미지 포함) 호출의 요청 타임아웃 (초) — 로컬 VLM은 모델 로드 + 다중 이미지 프리필이 필요하므로 텍스트보다 더 높은 상한을 가짐 |
| `SEAM_AI_AUTO_APPLY` | `false` | 향후 자동 적용 게이트를 위해 예약됨; 설정으로 파싱되지만 **MVP에서는 어떤 코드도 이를 처리하지 않음** |

## 엄격한 JSON 계약

모델 출력은 `MaterialSuggestionResponse`
(`backend/app/schemas/ai.py`)에 대해 검증되어야 합니다. 자유 형식의 AI 텍스트는 절대 씬에 도달하지 않습니다.

```json
{
  "suggestions": [
    {
      "prim_id": "/buildings/b01/window_01",
      "recommended_rf_material_id": "itu_glass",
      "confidence": 0.86,
      "evidence": [
        "object name contains 'window'",
        "visual material name contains 'glass'"
      ],
      "alternatives": [{"rf_material_id": "metal", "confidence": 0.11}],
      "needs_user_confirmation": true
    }
  ],
  "provider": "ollama_text",
  "model": "qwen3:8b",
  "prompt_version": "v2",
  "warnings": []
}
```

파싱 시점에 강제되는 제약: `confidence`는 [0, 1] 범위, 알 수 없는 키는
거부됨. 프로젝트 라이브러리에 없는 `recommended_rf_material_id`는
그대로 통과시키지 않고 경고와 함께 폐기됩니다.

## 제안 적용

제안은 제안일 뿐입니다. 이를 적용하는 것은 apply 엔드포인트로 전송되는
명시적인 사용자 결정입니다 (`ApplySuggestionsRequest`): 각 결정은
`approve` (제안된 재질 사용), `edit` (사용자가 다른 재질을 선택함),
또는 `reject`입니다. 승인/편집된 결정은 수동 할당과 동일한
`assign_materials` 경로를 거쳐,
`assignment_status: "ai_suggested"`가 `"user_confirmed"`로 승격된 RF 바인딩을
생성하며, `assignment_sources`가 체인을 기록합니다 (예:
`["ai:ollama/qwen3:8b", "user"]`).

**절대 자동 적용 안 함 규칙:** 사용자가 조치하지 않는 한 어떤 제안도
씬을 변경하지 않습니다. MVP에는 자동 적용 코드 경로가 전혀 없으며;
`SIONNATWIN_AI_AUTO_APPLY`는 향후 옵트인을 위해 예약된 플래그이고, 심지어
그때에도 provenance는 할당이 AI에서 왔음을 여전히 기록합니다.

## Provenance 로그

모든 제안 배치와 모든 사용자 결정은
`<project>/ai/suggestions.jsonl`에 한 줄에 하나의 JSON 객체로 추가됩니다:

```json
{"timestamp": "2026-07-02T09:14:03+00:00",
 "event": "suggest",
 "provider": "ollama_text",
 "model": "qwen3:8b",
 "prompt_version": "v2",
 "input_prim_ids": ["/buildings/b01/window_01"],
 "suggestions": [ ...MaterialSuggestion objects... ],
 "warnings": []}

{"timestamp": "2026-07-02T09:15:40+00:00",
 "event": "decision",
 "provider": "ollama_text",
 "model": "qwen3:8b",
 "prim_id": "/buildings/b01/window_01",
 "action": "approve",
 "final_rf_material_id": "itu_glass"}
```

로그는 추가 전용(`ProjectStore.append_jsonl`)이며 프로젝트 폴더와 함께
배포되므로, 각 재질을 누가/무엇이 제안했는지 —
그리고 사용자가 그에 대해 무엇을 했는지 — 의 전체 이력이 공유 및 재개방에도
살아남습니다.

## 자연어 규칙 생성

Prim별 제안이 한 축이라면, 다른 축은 의도에 의한 *일괄* 할당입니다.
`POST /projects/{id}/ai/generate-rules`는 평이한 언어의 지시
("glass windows are `itu_glass`, everything with 'concrete' in the name is
`itu_concrete`")를 결정론적이고 검사 가능한 **할당 규칙(assignment rules)** 목록으로 변환합니다:

```json
POST /projects/{id}/ai/generate-rules
{ "instruction": "windows are glass, walls with 'concrete' → itu_concrete" }
->
{ "rules": [
    { "id": "r1", "match_name_contains": ["window", "glass"],
      "rf_material_id": "itu_glass", "note": "glazing" },
    { "id": "r2", "match_name_contains": ["concrete"],
      "rf_material_id": "itu_concrete", "note": null } ],
  "provider": "local_openai", "model": "…", "warnings": [] }
```

`AssignmentRule`은 `{id, match_name_contains: string[] (≥1),
rf_material_id, note?}`입니다 — prim 이름에 대한 대소문자 구분 없는 부분 문자열
OR-매치. 제안과 동일한 library-id 가드가 적용됩니다: 프로젝트 라이브러리
바깥의 `rf_material_id`를 명시하는 규칙은 경고와 함께 폐기되므로,
모델은 재질을 지어낼 수 없습니다. 규칙은 **제안**이며, 할당이 아닙니다.

이를 적용하는 것은 명시적인 두 번째 단계입니다: `POST /projects/{id}/ai/apply-rules`는
(사용자가 편집했을 수도 있는) 규칙 목록을 받아 현재 씬과 매칭하고,
`MaterialSuggestionResponse`를 반환합니다 — prim별 제안기가 반환하는
것과 정확히 동일한 형태이므로, 검토-및-적용 UI가 동일합니다. 매칭된
prim은 `assignment_status: "rule_assigned"` 증거를 가진 제안으로 돌아오며;
사용자가 일반적인 apply-suggestions 경로를 통해 승인하기 전까지는 아무것도
씬을 건드리지 않습니다. 사용자가 이미 거부한 prim은
`rejected` (material id null) 상태로 유지되며 다시 제안되지 않습니다.

## 검증 설명

`POST /projects/{id}/ai/explain-validation`은 씬 검증기를 실행하고
프로바이더에게 그 결과 이슈들을 평이한 언어로 설명하도록 요청합니다 — 각
`ValidationIssue`가 정확도 측면에서 무엇을 의미하며 그에 대해 무엇을 해야 하는지:

```json
POST /projects/{id}/ai/explain-validation
->
{ "explanation": "3 prims are unassigned … an ITU ground material is used at
   28 GHz, which is out of band; switch it to `ground_28ghz` …",
  "provider": "ollama_text", "model": "qwen3:8b", "warnings": [] }
```

이것은 읽기 전용입니다: 씬이나 할당을 절대 변경하지 않고, 체크리스트만
설명합니다. 각 `ValidationIssue`는 이제
`suggested_actions: string[]` (UI가 원클릭 수정으로 표시하는 구체적인 다음 단계)도
함께 가지므로, 자연어 설명과 구조화된 액션이 동기화된 상태를 유지합니다.
언제나 그렇듯 이것은 우아하게 성능 저하합니다 — 도달 가능한 AI 서버가
없으면 `rule_based` 프로바이더가 이슈 코드로부터 구성된 템플릿 설명을 반환합니다.

## RF 구별(disambiguation)

RF 관점에서 서로 다른 두 재질이 *똑같이* 보일 때 비전만으로는 그것들을
구별할 수 없습니다. Glass가 대표적인 경우입니다: Dai et al. (Qualcomm, JSTEAP 2025)은
시각적으로 구별 불가능한 유리판이 mmWave에서 대략 **2.5–23.6 dB**의
투과 손실 범위에 걸쳐 있다고 보고합니다 — 링크 버짓을 좌우하지만
카메라나 텍스처 이름이 포착할 만한 시각적 흔적은 남기지 않는 차이입니다.
규칙 또는 비전 기반 제안기는 이들을 모두 기꺼이 `itu_glass`로 라벨링할 것입니다.

RF 구별은 픽셀 대신 *측정값*으로 그 동점을 해소합니다. prim,
후보 재질 목록(제안 및 그 대안들), 그리고 몇 개의 측정된 링크별 경로
이득이 주어지면, 서비스는 각 후보를 차례로 prim에 바인딩하고, 씬을
재컴파일하며, 측정된 링크를 재시뮬레이션하고, 레벨 정렬된 RMSE로 점수를
매깁니다 — 파라미터 보정과 동일한 지표
(`services/calibration.py::disambiguate_materials`). RMSE가 가장 낮은
후보가 이깁니다.

```
POST /projects/{id}/calibrate/disambiguate
{ "config": {"backend": "sionna"},
  "prim_ids": ["/buildings/b01/window_12"],
  "candidate_material_ids": ["itu_glass", "itu_glass_thick", "metal"],
  "measurements": [ {"rx_position": [10,5,1.5], "measured_path_gain_db": -92.0}, ... ] }
->
{ "prim_ids": [...],
  "candidates": [ {"material_id": "itu_glass", "rmse_db": 1.8, "n_links": 6}, ... ],
  "best_material_id": "itu_glass_thick",
  "backend": "sionna", "warnings": [] }
```

**구별 불가 경고.** 후보들의 RMSE 편차가 0.05 dB 미만이면 그 위치에서의
측정값으로는 그것들을 분리할 수 없습니다 — 서비스는
`best_material_id: null`을 반환하고 노이즈를 고르는 대신
`candidates are indistinguishable at these positions (RMSE spread … dB); add
measurements nearer the prims`라고 경고합니다. 이것은 결정론적 목(mock)
백엔드가 두 개의 ITU 주파수 의존 후보에 대해 정확히 하는 일입니다 (그 반사
손실은 산란 항만 담고 있으므로, `S`가 같은 재질은 동일하게 예측합니다) —
구별은 **Sionna 백엔드 정확도 기능**이며; 목은 흐름을 테스트 가능하게 만들기
위해 존재하는 것이지 재질을 분리하기 위한 것이 아닙니다.

## 할당 영향 평가

일단 재질이 할당되면, *이 링크에서 그것들이 실제로 얼마나 중요한가?*
영향 평가는 Lee et al. (KICS 2026)의 CFR 프레임워크를 구현합니다:
각 TX→RX 위치를 두 번 풀어냅니다 — 한 번은 씬의 할당된 재질로, 한 번은
**모든** prim을 단일 기준 재질(기본값
`itu_concrete`)로 재바인딩하여 — 그리고 두 채널 주파수 응답을 비교합니다
(`services/material_impact.py`). 위치마다 다음을 보고합니다:

- **NMSE (dB)** — `Σ|H_mat − H_base|² / Σ|H_mat|²`. 기준 채널이
  재질 인식 채널로부터 얼마나 떨어져 있는지. 더 음수일수록 = 재질이 채널을
  거의 움직이지 않음; 0 dB에 가까울수록 = 재질이 채널을 지배함. `sensitive_nmse_db`
  (KICS는 −60 dB 사용)를 초과하는 위치는 **material-sensitive**로 표시됩니다.
- **cosine similarity** — `|H_matᴴ H_base| / (‖H_mat‖‖H_base‖)`, [0, 1] 범위. 두 CFR의
  형상 일치도; 1.0은 스케일까지 동일함을 의미합니다.
- **dRSS (dB)** — 부호 있는 `RSS_mat − RSS_base`. 할당된 재질이 기준 대비
  수신 전력을 올리는지 내리는지.
- **capacity proxy (Mbps)** — 각 변형에 대한 Shannon `B·mean_f log₂(1+SNR(f))`
  처리량으로, 재질 효과가 엔드투엔드 KPI에 반영되도록 합니다.

이렇게 읽으세요: **NMSE가 0에 가깝고 + cos-sim ≈ 1 + dRSS ≈ 0이면 "여기서는
재질이 중요하지 않다"**는 뜻입니다 (기하 구조/LoS가 링크를 좌우함); 큰 dRSS를
동반한 높은 위치별 NMSE는 재질을 제대로 맞추는 것이 필수적인 위치입니다.
Sionna 라이브(`lab_room`)에서 위치별 NMSE는 −6에서 −17 dB로 나옵니다.
재질 무관 목에서는, 반사하는 prim에 이미 있는 재질과 동일한 재질에 기준을
바인딩하면 모든 지표가 항등식으로 붕괴합니다 (cos-sim 1, dRSS 0,
글로벌 NMSE 정의 안 됨) — 다시 한번 테스트 가능한 목 스텁을 가진 **Sionna
정확도 기능**입니다. 엔드포인트: `POST /projects/{id}/analyze/material-impact`.

## 다중 뷰 캡처 & 텍스처 크롭

**다중 뷰 캡처** *(정확도 기능).* 단일 스크린샷은 각 표면을 하나의 각도,
하나의 눈부심/가림 조건에서만 봅니다. 따라서 `SuggestMaterialsRequest`는
`screenshot_data_urls` (최대 6개 뷰; 레거시 단일 `screenshot_data_url`은
여전히 한 항목짜리 리스트로 인정됨)를 받습니다. Dai et
al.의 다중 뷰 다수결 병합을 따라, 비전 프로바이더는 뷰들에 걸쳐 카테고리별
프롬프트 변형을 집계하고 다수 라벨을 유지하므로, 6개 뷰 중 4개에서 "glass"로
읽히는 창은 반사 때문에 metal로 보이는 나머지 두 프레임에 의해 탈선하지
않습니다.

**텍스처 크롭** *(정확도 기능).* 뷰포트 전체 스크린샷은 모델에게 대부분
빈 공간을 건네주고, 어느 픽셀이 문제의 prim에 속하는지 추측하도록 강요합니다.
대신 prim별 타이트한 텍스처 크롭(KICS SAM2.1 + DINOv2 삼각형별 경로)을
전달하면 모델에게 표면 고유의 외관을 전체 해상도로 제공하며, 이것이 바로
삼각형별 투표와 하위의 CFR/NMSE 평가가 실제로 소비하는 것입니다.
