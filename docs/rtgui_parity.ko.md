# Sionna RT GUI 대비 기능 패리티 매트릭스

> [English](rtgui_parity.md) · **한국어**

기준: NVlabs/sionna-rt-gui **v0.1.1** 전수 소스 분석 (2026-07-03, 커밋
`6d37a26`). **RT GUI의 엔진은 `sionna-rt==1.2.2` 정확 핀**
(requirements.txt:5, pyproject.toml:29) — 본 툴은 엔진 스위처로 **동일한
sionna-rt 1.2.2를 그대로 실행 가능**하고(builtin은 2.0.1), 따라서 물리 레벨
동등성은 버전 선택으로 보장된다 ([engines.md](engines.ko.md)).

## 패리티 표 (RT GUI 기능 → 본 툴 상태)

| RT GUI 기능 | 본 툴 | 비고 |
|---|---|---|
| 씬 로드 (내장/XML) | ✅ | 프로젝트 모델 + import 스크립트; UI 드래그앤드롭은 로드맵 |
| Z-up 좌표/카메라 궤도 | ✅ | 동일 컨벤션 |
| 카메라 리셋(R)/씬 맞춤(F) | ✅ | 단축키 R/F |
| 커서 위치 TX/RX 추가 (K/L, 표면+1.5m) | ✅ | 단축키 K/L, 표면 법선 스냅 동일 컨벤션 |
| 디바이스 선택/색상/삭제/전체 삭제 | ✅ | + 수치 위치/방향 입력 (RT GUI에는 없음) |
| 안테나 배열: 패턴/편파/행열/간격(λ) | ✅ | 간격 노출 완료; **디바이스별** 배열(RT GUI는 씬 공용) |
| PathSolver 전 파라미터 (depth/samples/synthetic/메커니즘 6종+lit-region) | ✅ | 시드도 노출 (RT GUI는 12345 하드코딩) |
| Auto-update | ✅ | paths/radio map + 빔포밍/채널까지 4종 (RT GUI는 2종) |
| 경로 시각화 (타입별 색) | ✅ | + 필터/강도별/선택 검사 (RT GUI 미지원) |
| 라디오맵: cell size/samples/메커니즘 | ✅ | 동일 |
| 라디오맵 colormap/vmin/vmax/컬러바 | ✅ | jet/viridis/plasma/turbo + 수동 범위 |
| 슬라이스 플레인 (S 토글) | ✅ | 씬 메시만 클리핑, Z 슬라이더 |
| 궤적 애니메이션/재생 속도/루프 모드 | ✅ | once/loop/pingpong + 프레임별 레이 재계산 (RT GUI는 표시만) |
| 도플러(속도 벡터) | ⚠ 로드맵 | RT GUI는 애니메이션 중 속도 설정; 본 툴 궤적은 위치만 |
| 포토리얼 레이트레이스 뷰 (Mitsuba) | ⚠ 대체 | 텍스처 오버레이 백드롭 + 조명 패널; 서버측 Mitsuba 렌더-투-파일은 로드맵 |
| 라이브 리로드/설정 YAML | ✅ 상응 | vite HMR + 프리셋/localStorage/프로젝트 저장 |
| 도움말/단축키 표 | ✅ | TUTORIAL.md + 툴팁 |

## 본 툴이 RT GUI를 상회하는 기능 (발췌)

CIR/CFR **표시·내보내기**(RT GUI는 계산만 하고 미표시), 채널 분석(K-factor,
지연 확산, 38.901 비교), 코드북 빔 스윕, 재질 에디터/ITU 피커/프림별 할당(RT
GUI 미지원), AI 재질 제안(+VLM), 측정 캘리브레이션, 액터/V2X 시나리오,
라이브 싱크, **ML 데이터셋 파이프라인**, RFData 내보내기, 멀티 sionna-rt
엔진, 플러그인 시스템, 프로젝트/프로버넌스.

## 남은 갭 (정직한 목록)

1. **도플러/속도**: 궤적 재생 시 객체 속도 → 경로별 도플러. sionna-rt 2.x
   velocity API로 구현 예정.
2. **UI 드래그앤드롭 XML 임포트**: 현재는 `examples/scripts/import_bundle_scene.py`.
3. **Mitsuba 패스트레이스 렌더-투-파일**: 백엔드 mitsuba로 카메라 포즈 렌더
   엔드포인트 추가 예정 (RT GUI도 파일 저장은 없음 — 구현 시 상회).
4. **기즈모 이동**(RT GUI 방식): 수치 입력은 있음; three.js TransformControls
   추가 예정 (AODT 클라이언트의 X/Y/Z 화살표 참고).
