# 도구 확장하기 (plugins & extension seams)

연구자가 **core 코드를 건드리지 않고** SionnaTwin Studio를 확장하는 방법을
정리한다. 두 갈래가 있다:

1. **Plugin system** — `plugins/<name>/plugin.py` 파일 하나로 backend / path-loss
   model / AI provider / exporter를 추가한다 (이 문서의 대부분).
2. **File-based extension seams** — plugin 없이도 존재하는 확장 지점: sionna
   버전 교체(`engines.json`), custom RF material(materials API), solver preset
   추가(`configPresets.ts`) (맨 아래 [다른 확장 지점](#다른-확장-지점) 절).

Plugin 로더 구현: `backend/app/services/plugins.py`.
동작하는 예제: [`plugins/example_two_ray/`](../plugins/example_two_ray/).

---

## Plugin anatomy (구조)

Plugin은 리포 루트 `plugins/` 아래의 **self-contained 폴더**다. `engines.json`과
똑같이 파일 기반이며, 별도 의존성이 없다.

```
plugins/
  my_plugin/
    plugin.py     ← 필수: register(registry) 하나를 정의
    README.md     ← 선택: 문서/메모
    ...           ← 선택: 데이터 파일 등 (plugin.py가 상대경로로 로드)
```

로더는 `plugins/*/plugin.py`를 폴더 이름 순으로 스캔하고,
`importlib.util.spec_from_file_location`으로 각 module을 **격리 import**한 뒤
그 module의 `register(registry)` 함수를 호출한다. 이게 전부다.

### 두 가지 규칙

**1. `register(registry)`를 정의한다.** 단일 entry point. 안에서 registry hook을
호출해 확장을 등록한다.

```python
def register(registry):
    registry.register_path_loss_model("two_ray_ground", two_ray_ground)
```

**2. import 시점에 안전해야 한다.** `plugin.py`는 격리 import되므로, module
최상위에서 무거운/선택적 의존성(sionna, httpx 등)을 import하면 위험하다. import가
실패하면 로더가 `PluginInfo(ok=False, error=...)`로 기록하고 **넘어간다** — 앱은
절대 crash하지 않지만, 그 plugin은 로드되지 않는다. 표준 라이브러리 import는
안전하다. 무거운 의존성은 **함수 안에서 lazy import**하라 (core의 `ai_provider.py`가
`httpx`를 그렇게 쓴다).

---

## Registry hooks

`register`에 넘어오는 `registry` 객체는 네 개의 hook을 노출한다. 각 hook은
인자를 검증하며, 잘못된 인자(빈 name, non-callable)는 그 plugin을 실패시킨다
(다른 plugin에는 영향 없음).

### `register_path_loss_model(name, fn)`

경험적 path-loss model을 추가한다. `channel_analysis.py`의 built-in model(FSPL,
TR 38.901, CI)과 나란히 비교된다.

- **signature**: `fn(freq_hz, tx, rx, config) -> {path_loss_db, valid, notes}`
  - `freq_hz` (float): 주파수 Hz
  - `tx`, `rx`: link 양 끝점 (Device-like — `.position`이 `[x, y, z]` meter,
    Z-up). dict나 raw sequence도 받도록 방어적으로 짜면 좋다.
  - `config`: solver config (`SimulationConfig`)
  - **반환 dict**: `path_loss_db` (float, 항상 유한값), `valid` (bool — 유효
    범위 밖이면 False), `notes` (str — 사람이 읽을 검증/폴백 메모)

```python
import math

def my_model(freq_hz, tx, rx, config):
    d = max(math.dist(list(tx.position), list(rx.position)), 1.0)
    pl = 20.0 * math.log10(4.0 * math.pi * d * freq_hz / 299_792_458.0)
    return {"path_loss_db": round(pl, 4), "valid": True, "notes": "FSPL"}

def register(registry):
    registry.register_path_loss_model("my_model", my_model)
```

> `valid=False`는 값을 **억제하지 않는다**. built-in model과 동일하게, 항상 값을
> 반환하되 유효 범위 밖임을 flag해서 UI가 회색 처리할 수 있게 한다.

### `register_backend(name, factory)`

새 ray-tracing backend를 추가한다. built-in `mock` / `sionna`와 같은 dict
(`_BACKENDS`)와 병합될 대상이다.

- **signature**: `factory() -> RayTracingBackend`
  - factory는 **인자 없이** 호출되어 backend 인스턴스를 만든다 (built-in
    `get_backend`가 `_BACKENDS[name]()`로 호출하는 것과 동일).
  - 반환 객체는 `RayTracingBackend`
    (`backend/app/services/simulation_backends/base.py`) 계약을 따라야 한다:
    `name` 속성, `is_available()`, `simulate_paths(...)`,
    `simulate_radio_map(...)` (필수), `compile`/`simulate_beamforming`은 base
    기본 구현 재사용 가능.

```python
from app.services.simulation_backends.base import RayTracingBackend

class MyBackend(RayTracingBackend):
    name = "my_backend"
    def is_available(self): return True
    def simulate_paths(self, project_dir, scene, library, config): ...
    def simulate_radio_map(self, project_dir, scene, library, config): ...

def register(registry):
    registry.register_backend("my_backend", MyBackend)  # 클래스 = 무인자 factory
```

> 이 hook은 `app.schemas`를 import하므로, 위험을 줄이려면 backend class와 그
> import를 `register` 함수 **안에서** 처리하는 것도 방법이다.

### `register_ai_provider(factory)`

material-suggestion provider를 provider chain에 추가한다
(`ai_provider.py`의 `local_openai → ollama_text → rule_based` chain 참고).

- **signature**: `factory() -> MaterialSuggestionProvider`
  - 반환 객체는 `MaterialSuggestionProvider`
    (`backend/app/services/ai_provider.py`) 계약: `name` 속성,
    `is_available() -> bool`, `suggest(scene, library, prim_ids, screenshot=None)
    -> MaterialSuggestionResponse`.
  - **모든 실패는 내부에서 rule_based로 degrade**시키는 게 core 관례다.
    provider는 네트워크/GPU 없이도 동작해야 한다.

```python
def register(registry):
    registry.register_ai_provider(MyProvider)  # MyProvider() -> provider 인스턴스
```

> path-loss/backend/exporter는 **name → 하나**(last-writer-wins)지만, AI provider는
> **리스트**로 쌓인다. chain 내 우선순위는 core 통합 지점이 결정한다.

### `register_exporter(name, fn)`

결과 exporter를 추가한다 (`rfdata_export.export_rfdata`와 같은 모양).

- **signature**: `fn(project_dir, scene, config, **kwargs) -> dict`
  - `rfdata_export.export_rfdata(project_dir, scene, config, created_at, paths=…,
    radio_map=…, trajectory=…)`와 동일한 형태. 파일을 project 폴더에 쓰고
    `{"export_dir", "files", ...}` 요약 dict를 반환한다.

```python
def my_exporter(project_dir, scene, config, **kwargs):
    out = project_dir / "export" / "my_format"
    out.mkdir(parents=True, exist_ok=True)
    # ... 파일 쓰기 ...
    return {"export_dir": "export/my_format", "files": [...]}

def register(registry):
    registry.register_exporter("my_format", my_exporter)
```

---

## Load order (로드 순서)

- 로더는 `plugins/` 아래 폴더를 **이름 오름차순**으로 정렬해 순회한다 → 로드
  순서가 머신에 상관없이 결정적이다.
- 같은 name을 두 plugin이 등록하면(backend/model/exporter) **나중에 로드된 쪽이
  이긴다** (last-writer-wins). 덮어쓰는 쪽의 `PluginInfo.warnings`에 경고가 남는다.
- `register`가 도중에 실패해도, 실패 전까지 등록된 것은 registry에 남고 그
  plugin은 `ok=False`로 기록된다. **한 plugin의 실패는 다른 plugin을 막지 않는다.**
- `load_plugins()`는 매 호출마다 registry를 **먼저 비운다** → plugin을 수정하고
  다시 로드해도 중복 등록되지 않는다.
- `.` 또는 `_`로 시작하는 폴더는 무시된다 (스크래치/비활성 plugin용).

등록 결과는 getter로 읽는다 (core consumer가 사용, 복사본을 반환):

```python
from app.services import plugins
plugins.plugin_path_loss_models()   # {name: fn}
plugins.plugin_backends()           # {name: factory}
plugins.plugin_ai_providers()       # [factory, ...]
plugins.plugin_exporters()          # {name: fn}
plugins.list_plugins()              # 최근 load 결과 [PluginInfo, ...] (재로드 안 함)
```

---

## Testing your plugin

`backend/tests/test_plugins.py`가 참고 예제다. 핵심 패턴:

- **실제 `plugins/` 로드**: `plugins.load_plugins()` 호출 후
  `plugins.plugin_path_loss_models()`에 내 model이 있는지 확인.
- **임시 plugin 격리 테스트**: `tmp_path`에 `<name>/plugin.py`를 쓰고
  `monkeypatch.setattr(plugins, "PLUGINS_DIR", tmp_dir)`로 로더를 그쪽으로 돌린다.
  실제 `plugins/`를 건드리지 않는다.
- **깨진 plugin이 raise하지 않는지**: import/register에서 예외를 던지는 plugin을
  써 넣고, `load_plugins()`가 예외 없이 `PluginInfo(ok=False, error=...)`를
  돌려주는지 확인.
- **registry 청소**: 테스트 간 누수를 막으려면 `plugins._reset_registries()`를
  fixture에서 호출한다.

```powershell
# 리포 루트에서
backend\.venv\Scripts\python.exe -m pytest backend\tests\test_plugins.py -q
```

---

## 다른 확장 지점 (plugin 없이)

plugin 시스템 밖에도, 파일/설정으로 여는 확장 지점이 이미 존재한다.

### 1. Sionna 버전 교체 — `engines.json`

paths 솔브에 쓸 Sionna RT 엔진 버전을 리포 루트 `engines.json`으로 교체한다.
별도 venv에 다른 sionna-rt를 깔고 항목을 추가하면, `GET /api/engines?refresh=true`
후 UI Engine 셀렉트에 나타난다. 자세한 절차·지원 범위·프로토콜은
[`docs/engines.md`](engines.md) 참조 (버전별 차이는
[`docs/sionna_versions.md`](sionna_versions.md)).

```json
{"engines": [
  {"id": "sionna-rt-1.2.2", "label": "Sionna RT 1.2.2",
   "python": "backend/.venv-sionna-rt-122/Scripts/python.exe",
   "adapter": "sionna_rt"}
]}
```

### 2. Custom RF material — materials API

RF material은 EM 표면 서술이며 visual/PBR material과 분리돼 있다. 프로젝트 생성 시
built-in 라이브러리(`backend/app/data/default_rf_materials.yaml`)가
`<project>/rf/materials.yaml`로 복사되고, 이후 프로젝트 파일이 authoritative다.
새 material을 추가/수정하려면 materials API를 쓴다:

- `GET  /api/projects/{id}/rf/materials` — 라이브러리 조회
- `PUT  /api/projects/{id}/rf/materials/{material_id}` — material 추가/수정

`model: constant`(직접 `relative_permittivity`/`conductivity_s_per_m` 지정) 또는
`model: itu_frequency_dependent`(Sionna 내장 ITU material 참조)를 쓸 수 있다.
포맷·필드 정의는 [`docs/rf_materials.md`](rf_materials.md) 및
`backend/app/schemas/materials.py` 참조.

### 3. Solver preset 추가 — `frontend/src/configPresets.ts`

SolverControls의 Preset 드롭다운은 `frontend/src/configPresets.ts`의 `PRESETS`
배열에서 온다. 새 canonical 배치 시나리오를 추가하려면 여기에
`ConfigPreset` 항목을 하나 넣는다 (frequency, depth, mechanisms, samples,
bandwidth + radio-map grid cell/height). preset은 paths/radio-map config를 함께
patch하며 backend/tx/rx 선택은 건드리지 않는다.

```ts
// frontend/src/configPresets.ts 의 PRESETS 배열에 추가
{
  id: "my_scenario_28",
  label: "My Scenario (28 GHz)",
  config: { frequency_hz: 28e9, max_depth: 5, num_samples: 1_000_000, bandwidth_hz: 100e6 },
  radioMap: { cell_size_m: 1.0, height_m: 1.5 },
}
```

`ConfigPresetId` union에 새 `id`도 추가해야 타입이 통과한다. `"custom"`은 사용자가
직접 수정했을 때의 sentinel이므로 건드리지 않는다.
