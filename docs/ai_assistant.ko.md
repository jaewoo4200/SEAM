# AI 어시스턴트

> [English](ai_assistant.md) · **한국어**

AI 어시스턴트는 시각적 증거 — 객체 이름, GLB 재질 이름, 시맨틱 태그, 텍스처
이름 — 를 근거로 prim의 RF 재질을 제안합니다. 어디까지나 선택 기능이라 앱은
수동만으로도 완전히 동작하며, 씬을 스스로 바꾸는 일은 절대 없습니다.

## 프로바이더 추상화

`app.services.ai_provider`는 API 계층에서 사용하는 두 진입점을 노출합니다:

```python
get_provider_statuses() -> list[AIProviderStatus]
suggest_materials(scene, library, request: SuggestMaterialsRequest) -> MaterialSuggestionResponse
```

이 뒤에는 프로바이더 체인이 있습니다:

| provider | needs | behavior |
|---|---|---|
| `rule_based` | nothing | 이름 / 시각 재질 이름 / 태그를 대상으로 한 결정론적 키워드 규칙 (window→`itu_glass`, brick→`itu_brick`, road→`asphalt_custom`, ...) |
| `local_openai` | 도달 가능한 OpenAI 호환 서버 (LM Studio 등, `SIONNATWIN_OPENAI_URL`) | prim 증거를 담아 로컬 LLM에 프롬프트를 보내고 엄격한 JSON을 돌려받음. **비전 지원**: 스크린샷이 있으면 요청이 멀티모달(`image_url`)이 됨; 로드된 모델이 이미지를 거부하면 폴백 전에 텍스트 전용으로 재시도함 |
| `ollama_text` | 도달 가능한 Ollama 서버 + 텍스트 모델 | Ollama chat API를 통한 동일한 계약. **비전 지원**: 스크린샷이 base64 `images`로 첨부되고 호출이 `SIONNATWIN_AI_VISION_MODEL`로 전환됨 (모델 교체는 경고로 알림); 이미지 거부 시 텍스트 전용으로 재시도함 |
| `disabled` | — | 제안을 반환하지 않음 (AI 꺼짐) |

선택: `SuggestMaterialsRequest.provider`를 지정하면 특정 프로바이더를 강제하고,
지정하지 않으면 사용 가능한 것 중 가장 좋은 프로바이더를 씁니다 (`local_openai` →
`ollama_text` → `rule_based`). 서버에 도달할 수 없거나, 타임아웃이 나거나,
스키마 검증에 실패하는 JSON이 돌아오면 응답은 체인을 따라 폴백하고, 그 과정을
`warnings`에 남깁니다. 응답의 `provider`/`model` 필드는 언제나 *실제로* 결과를
만들어낸 주체를 가리킵니다.

**라이브 검증 완료**: LM Studio + `google/gemma-4-31b`로 텍스트·멀티모달 모두
엔드투엔드 확인했으며, 모든 제안에 대해 library-id 검증을 거칩니다 — 모델은
프로젝트 RF 라이브러리에 없는 재질 id를 만들어낼 수 없습니다.

모든 Ollama 접근은 지연(lazy) 방식입니다(함수 안에서 import·probe). AI 서버도,
GPU도, 호환 모델도 나머지 기능을 쓰는 데는 전혀 필요 없으며, `/api/health`는
`get_provider_statuses()`로 각 프로바이더의 가용성을 보고합니다.

## 구성(Configuration)

환경 변수 (`app.core.config.get_settings()`가 한 번만 읽습니다). 이름은 정규
`SEAM_*` 접두사를 쓰며, 모든 변수에서 레거시 `SIONNATWIN_*` 이름도 여전히
인정됩니다 (둘 다 설정하면 `SEAM_*`가 우선).

| variable | default | meaning |
|---|---|---|
| `SEAM_AI_ENABLED` | `auto` | `auto` (도달 가능하면 사용) \| `on` \| `off` |
| `SEAM_OLLAMA_URL` | `http://localhost:11434` | Ollama 엔드포인트 |
| `SEAM_AI_TEXT_MODEL` | `qwen3:8b` | Ollama 텍스트 모델 |
| `SEAM_AI_VISION_MODEL` | `qwen2.5vl:3b` | 스크린샷이 첨부될 때 쓰는 Ollama 비전 모델 (예: LLaVA를 쓰려면 `llava`로 설정) |
| `SEAM_OPENAI_URL` | `http://localhost:1234/v1` | OpenAI 호환 엔드포인트 (LM Studio 기본 포트) |
| `SEAM_OPENAI_MODEL` | `google/gemma-4-31b` | OpenAI 호환 서버가 제공하는 모델 id |
| `SEAM_AI_TIMEOUT_S` | `60` | 텍스트 전용 호출의 요청 타임아웃 (초) |
| `SEAM_AI_VISION_TIMEOUT_S` | `300` | 멀티모달(이미지 포함) 호출의 요청 타임아웃 (초) — 로컬 VLM은 모델 로드 + 다중 이미지 프리필이 필요해 텍스트보다 상한이 높음 |
| `SEAM_AI_AUTO_APPLY` | `false` | 향후 자동 적용 게이트용으로 예약됨; 설정으로 파싱은 되지만 **MVP에서는 이를 처리하는 코드가 없음** |

## 엄격한 JSON 계약

모델 출력은 `MaterialSuggestionResponse`
(`backend/app/schemas/ai.py`) 스키마를 통과해야 합니다. 자유 형식 AI 텍스트가 씬에 도달하는 일은 절대 없습니다.

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

파싱 시점에 강제되는 제약: `confidence`는 [0, 1] 범위여야 하고, 알 수 없는 키는
거부됩니다. 프로젝트 라이브러리에 없는 `recommended_rf_material_id`는 그대로
통과시키지 않고 경고와 함께 버립니다.

## 제안 적용

제안은 제안일 뿐입니다. 이를 실제로 적용하려면 사용자가 명시적으로 결정해 apply
엔드포인트(`ApplySuggestionsRequest`)로 보내야 합니다. 각 결정은 `approve`
(제안된 재질 사용), `edit` (사용자가 다른 재질을 선택), `reject` 중 하나입니다.
승인·편집된 결정은 수동 할당과 똑같은 `assign_materials` 경로를 거쳐 RF 바인딩을
만드는데, 이때 `assignment_status: "ai_suggested"`가 `"user_confirmed"`로
승격되고 `assignment_sources`에 체인이 기록됩니다 (예:
`["ai:ollama/qwen3:8b", "user"]`).

**자동 적용 절대 금지 규칙:** 사용자가 직접 조치하지 않는 한 어떤 제안도 씬을
바꾸지 않습니다. MVP에는 자동 적용 코드 경로 자체가 없습니다.
`SIONNATWIN_AI_AUTO_APPLY`는 향후 옵트인용으로 예약해 둔 플래그이며, 설령
도입되더라도 provenance에는 그 할당이 AI에서 나왔다는 사실이 그대로 기록됩니다.

## Provenance 로그

모든 제안 배치와 모든 사용자 결정은 `<project>/ai/suggestions.jsonl`에 한 줄당
JSON 객체 하나씩 추가됩니다:

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

로그는 추가 전용(`ProjectStore.append_jsonl`)이고 프로젝트 폴더에 함께 담겨
배포됩니다. 그래서 각 재질을 누가/무엇이 제안했고 — 사용자가 거기에 어떻게
대응했는지 — 그 전체 이력이 프로젝트를 공유하거나 다시 열어도 그대로 남습니다.

## 자연어 규칙 생성

prim별 제안이 한 축이라면, 다른 축은 의도 기반 *일괄* 할당입니다.
`POST /projects/{id}/ai/generate-rules`는 평범한 말로 쓴 지시
("glass windows are `itu_glass`, everything with 'concrete' in the name is
`itu_concrete`")를 결정론적이고 검사 가능한 **할당 규칙(assignment rules)** 목록으로 바꿉니다:

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
rf_material_id, note?}` 형태이며, prim 이름을 대소문자 구분 없이 부분 문자열로
OR-매치합니다. 제안과 똑같은 library-id 가드가 걸립니다: 프로젝트 라이브러리에
없는 `rf_material_id`를 지정한 규칙은 경고와 함께 버려지므로, 모델이 재질을
지어낼 수는 없습니다. 규칙은 어디까지나 **제안**이지 할당이 아닙니다.

규칙 적용은 명시적인 두 번째 단계입니다. `POST /projects/{id}/ai/apply-rules`는
(사용자가 편집했을 수도 있는) 규칙 목록을 받아 현재 씬과 매칭한 뒤
`MaterialSuggestionResponse`를 반환합니다 — prim별 제안기가 내놓는 것과 형태가
완전히 같아서 검토·적용 UI도 그대로 재사용합니다. 매칭된 prim은
`assignment_status: "rule_assigned"` 증거를 단 제안으로 돌아오며, 사용자가
평소의 apply-suggestions 경로로 승인하기 전까지는 아무것도 씬을 건드리지
않습니다. 사용자가 이미 거부한 prim은 `rejected` (material id null) 상태로 남아
다시 제안되지 않습니다.

## 검증 설명

`POST /projects/{id}/ai/explain-validation`은 씬 검증기를 돌린 뒤, 거기서 나온
이슈들을 프로바이더에게 쉬운 말로 설명하게 합니다 — 각 `ValidationIssue`가
정확도 관점에서 무슨 뜻이고 무엇을 해야 하는지:

```json
POST /projects/{id}/ai/explain-validation
->
{ "explanation": "3 prims are unassigned … an ITU ground material is used at
   28 GHz, which is out of band; switch it to `ground_28ghz` …",
  "provider": "ollama_text", "model": "qwen3:8b", "warnings": [] }
```

이 기능은 읽기 전용입니다. 씬이나 할당은 절대 건드리지 않고 체크리스트를
설명하기만 합니다. 이제 각 `ValidationIssue`에는 `suggested_actions: string[]`
(UI가 원클릭 수정 버튼으로 보여주는 구체적인 다음 단계)도 함께 담기므로,
자연어 설명과 구조화된 액션이 서로 어긋나지 않습니다. 늘 그렇듯 이 기능도 무리
없이 폴백합니다 — 도달 가능한 AI 서버가 없으면 `rule_based` 프로바이더가 이슈
코드로 조립한 템플릿 설명을 반환합니다.

## RF 구별(disambiguation)

RF 관점에서 전혀 다른 두 재질이라도 겉보기가 *똑같으면* 비전만으로는 구분할 수
없습니다. 유리가 대표적입니다: Dai et al. (Qualcomm, JSTEAP 2025)에 따르면
눈으로는 구별되지 않는 유리판이 mmWave에서 투과 손실이 대략 **2.5–23.6 dB**까지
벌어집니다 — 링크 버짓을 좌우하면서도 카메라나 텍스처 이름으로는 잡아낼 시각적
흔적을 전혀 남기지 않는 차이입니다. 규칙 기반이든 비전 기반이든 제안기는 이들을
모조리 `itu_glass`로 붙여 버립니다.

RF 구별은 픽셀이 아니라 *측정값*으로 이 동점을 가립니다. prim 하나, 후보 재질
목록(제안과 그 대안들), 그리고 측정된 링크별 경로 이득 몇 개가 주어지면,
서비스는 후보를 하나씩 prim에 바인딩해 씬을 다시 컴파일하고, 측정된 링크를 다시
시뮬레이션한 뒤 레벨 정렬 RMSE로 점수를 매깁니다 — 파라미터 보정과 같은
지표입니다 (`services/calibration.py::disambiguate_materials`). RMSE가 가장 낮은
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

**구별 불가 경고.** 후보들의 RMSE 편차가 0.05 dB보다 작으면 그 위치의 측정값으로는
후보를 갈라낼 수 없습니다 — 이때 서비스는 노이즈를 고르는 대신
`best_material_id: null`을 반환하고
`candidates are indistinguishable at these positions (RMSE spread … dB); add
measurements nearer the prims`라고 경고합니다. 결정론적 목(mock) 백엔드는 ITU
주파수 의존 후보 둘에 대해 바로 이 동작을 합니다 (그 반사 손실은 산란 항만 담고
있어 `S`가 같은 재질은 예측이 똑같이 나옵니다). 구별은 **Sionna 백엔드 정확도
기능**이고, 목은 재질을 갈라내려는 게 아니라 흐름을 테스트할 수 있게 하려고
존재합니다.

## 할당 영향 평가

재질을 할당하고 나면, *이 링크에서 재질이 실제로 얼마나 중요할까?* 영향 평가는
Lee et al. (KICS 2026)의 CFR 프레임워크를 구현합니다. 각 TX→RX 위치를 두 번
풉니다 — 한 번은 씬에 할당된 재질 그대로, 한 번은 **모든** prim을 단일 기준
재질(기본값 `itu_concrete`)로 다시 바인딩해서 — 그런 다음 두 채널 주파수 응답을
비교합니다 (`services/material_impact.py`). 위치마다 다음을 보고합니다:

- **NMSE (dB)** — `Σ|H_mat − H_base|² / Σ|H_mat|²`. 기준 채널이 재질을 반영한
  채널에서 얼마나 벗어나 있는지. 값이 더 음수일수록 = 재질이 채널을 거의 바꾸지
  않음, 0 dB에 가까울수록 = 재질이 채널을 좌우함. `sensitive_nmse_db`
  (KICS는 −60 dB 사용)를 넘는 위치는 **material-sensitive**로 표시됩니다.
- **cosine similarity** — `|H_matᴴ H_base| / (‖H_mat‖‖H_base‖)`, [0, 1] 범위. 두 CFR의
  형상이 얼마나 일치하는지. 1.0이면 스케일 차이를 빼면 완전히 같습니다.
- **dRSS (dB)** — 부호 있는 `RSS_mat − RSS_base`. 할당된 재질이 기준 대비
  수신 전력을 올리는지 내리는지.
- **capacity proxy (Mbps)** — 각 변형의 Shannon `B·mean_f log₂(1+SNR(f))`
  처리량. 이로써 재질 효과가 엔드투엔드 KPI에까지 반영됩니다.

해석은 이렇습니다. **NMSE가 0에 가깝고 + cos-sim ≈ 1 + dRSS ≈ 0이면 "여기서는
재질이 중요하지 않다"**는 뜻입니다 (기하 구조와 LoS가 링크를 좌우). 반대로 위치별
NMSE가 높으면서 dRSS도 크면, 그 자리는 재질을 제대로 맞추는 것이 필수인
위치입니다. Sionna 라이브(`lab_room`)에서 위치별 NMSE는 −6에서 −17 dB 사이로
나옵니다. 재질 무관 목에서는, 반사하는 prim에 이미 걸린 재질과 같은 재질로
기준을 바인딩하면 모든 지표가 항등식으로 무너집니다 (cos-sim 1, dRSS 0,
글로벌 NMSE 정의 안 됨) — 이 또한 테스트용 목 스텁을 갖춘 **Sionna 정확도
기능**입니다. 엔드포인트: `POST /projects/{id}/analyze/material-impact`.

## 다중 뷰 캡처 & 텍스처 크롭

**다중 뷰 캡처** *(정확도 기능).* 스크린샷 한 장은 각 표면을 한 각도에서, 하나의
눈부심·가림 조건에서만 담습니다. 그래서 `SuggestMaterialsRequest`는
`screenshot_data_urls` (최대 6개 뷰; 레거시 단일 `screenshot_data_url`도 한
항목짜리 리스트로 여전히 인정)를 받습니다. Dai et al.의 다중 뷰 다수결 병합을
따라, 비전 프로바이더는 여러 뷰에 걸쳐 카테고리별 프롬프트 변형을 모으고 다수
라벨을 채택합니다. 그래서 6개 뷰 중 4개에서 "glass"로 읽히는 창은 반사 탓에
metal로 보이는 나머지 두 프레임 때문에 흔들리지 않습니다.

**텍스처 크롭** *(정확도 기능).* 뷰포트 전체 스크린샷은 모델에게 대부분 빈 공간을
넘기면서, 어느 픽셀이 대상 prim의 것인지 추측하게 만듭니다. 대신 prim별로 딱
맞게 자른 텍스처 크롭(KICS SAM2.1 + DINOv2 삼각형별 경로)을 넘기면 표면 고유의
외관을 전체 해상도로 모델에 전달하는데, 이것이 바로 삼각형별 투표와 뒤단의
CFR/NMSE 평가가 실제로 입력으로 쓰는 자료입니다.
