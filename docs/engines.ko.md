# Compute engines (Sionna 버전 교체)

> [English](engines.md) · **한국어**

SEAM Studio는 경로(paths) 솔브를 실행할 **Sionna 엔진 버전을 교체**할 수
있다. 버전별 기능·재질·물리 모델 차이와 "어떤 연구에 어떤 버전이 맞는가"는
[sionna_versions.md](sionna_versions.ko.md) 참조.

## 구조

- **builtin** — 백엔드 venv(`backend/.venv`)에 설치된 sionna-rt를 프로세스
  내에서 직접 호출한다(기본값, 가장 빠름 — 씬 캐시 공유).
- **subprocess 엔진** — 별도 venv에 설치된 다른 sionna-rt 버전.
  `backend/engine_workers/sionna_rt_worker.py`가 해당 venv 인터프리터로 실행되어
  파일 기반 JSON 프로토콜(job.json → out.json)로 경로 결과를 돌려준다.
  PathSolver 호출은 **시그니처 필터링**으로 조립되므로, 버전에 없는 메커니즘
  플래그는 경고와 함께 무시된다(크래시하지 않음).

현재 엔진 선택은 **paths 솔브에 적용**된다. 라디오맵/빔포밍/채널 분석은 항상
builtin으로 계산되며(문서화된 제한), 시나리오 재생의 액터 상태 반영도 builtin
전용이다(서브프로세스는 저작 포즈로 계산 + 경고).

## 컴퓨트 디바이스 (GPU/CPU 자동 선택)

Sionna RT는 Mitsuba 3의 Dr.Jit 백엔드 위에서 돈다. 어떤 엔진이든 솔브 직전에
`cuda_ad_mono_polarized`(GPU) → `llvm_ad_mono_polarized`(CPU) 순으로 변형(variant)을
**자동 선택**한다(`sionna_backend._pick_variant`).

- **Mock 백엔드** — 아무것도 필요 없다. Dr.Jit/Mitsuba 없이 CPU만으로 동작한다.
- **Linux / Windows + NVIDIA GPU** — CUDA 변형을 자동으로 선택한다(별도 설정 불필요).
- **macOS(Apple Silicon 포함)** — Dr.Jit에 **Metal/MPS 백엔드가 없어** 항상
  **CPU/LLVM**으로 동작한다. 정상 동작하지만 GPU 대비 **느리다**. CUDA를 찾지
  못하면 자동으로 LLVM으로 폴백하며 결과 경고에 `CUDA unavailable — using LLVM
  (CPU) ray tracing …` 한 줄을 남긴다(무해).

CUDA 머신에서는 동작이 바뀌지 않는다(항상 CUDA 변형 선택, 경고 없음). 변형은
프로세스 전역이라 먼저 고정된 것이 우선하며, 이미 고정돼 있으면 그대로 존중한다.

## 엔진 추가 방법

1. venv 생성 + 원하는 sionna-rt 설치.
   **venv의 Python 버전을 대상 sionna-rt가 지원하는 버전으로 고정할 것** —
   sionna-rt ≤ 1.1은 Python 3.13까지만 나온 mitsuba/drjit 휠을 고정하므로,
   Python 3.14에서는 `pip install`은 *성공*하지만 `import sionna.rt`가 실패한다
   (엔진은 프로브 오류와 함께 사용 불가로 표시됨). Python 3.12는 1.x/2.x 전
   릴리스에서 동작한다:

   ```powershell
   py -3.12 -m venv backend\.venv-sionna-rt-110
   backend\.venv-sionna-rt-110\Scripts\pip install "sionna-rt==1.1.0"
   ```

   | sionna-rt | Python |
   |---|---|
   | 2.x (≥ 2.0) | 3.10 – 3.14 |
   | 1.x (≤ 1.2) | 3.10 – 3.13 (mitsuba 3.6.x 휠에 cp314 빌드 없음) |

2. 리포 루트 `engines.json`에 항목 추가:

   ```json
   {"id": "sionna-rt-1.1.0", "label": "Sionna RT 1.1.0",
    "python": "backend/.venv-sionna-rt-110/Scripts/python.exe",
    "adapter": "sionna_rt"}
   ```

3. `GET /api/engines?refresh=true`로 프로브 갱신(또는 백엔드 재시작).
   UI에서는 Results 모드 → Global → **Engine** 셀렉트에 나타난다.

가용성은 대상 venv에서 `import sionna.rt`를 실제로 실행해 확인한다(콜드 임포트
수십 초 가능 → 성공한 프로브만 프로세스당 캐시; 실패한 프로브는 다음 조회 때
재시도되므로 venv를 고친 뒤 `GET /api/engines?refresh=true`를 호출하거나 Engine
셀렉트를 다시 열면 반영된다). 미설치 엔진은 셀렉트에서 비활성으로 표시되며,
프로브 오류(해당 venv의 Python 버전 포함)가 상세 정보로 나온다.

## 지원 범위

| adapter | 대상 | 상태 |
|---|---|---|
| `builtin` | 백엔드 venv의 sionna-rt (현재 2.0.1) | 전 기능 |
| `sionna_rt` | 독립 sionna-rt 1.x / 2.x venv | paths 솔브 (검증: 1.2.2 vs 2.0.1 lab_room 62경로 일치) |
| (roadmap) | TF 기반 sionna ≤ 0.19 | 미구현 — Python 3.11 + TensorFlow venv와 전용 워커 필요. 0.x는 `scene.compute_paths()` API와 다른 재질 처리를 쓰므로 별도 어댑터로 작성해야 하며, 이 리포의 컴파일러 XML(`itu-radio-material` 플러그인)이 아닌 plain-bsdf 변형 XML도 필요하다. |

## 프로토콜 요약

Job: `{kind, xml_path, manifest_path, frequency_hz, max_depth, seed,
num_samples, synthetic_array, flags{...}, txs[], rxs[], material_to_prims{}}`
→ Out: `{ok, engine_version, paths[<RayPath 형태>], warnings[], error}`.
재질은 컴파일된 XML의 ITU 플러그인 + `compile_manifest.json`의 상수 재질
오버라이드를 워커가 재적용한다. 테스트는 `backend/tests/test_engines.py`
(가짜 워커로 프로토콜/디스패치/레지스트리 검증, 실제 venv 불필요).
