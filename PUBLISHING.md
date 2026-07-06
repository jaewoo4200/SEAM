# GitHub 공개 체크리스트

신규 클론 설치 드릴(백엔드 설치 → 부팅 → 데모 로드 → mock 분석 → 프론트 빌드)은
**전부 통과**했습니다. 다만 공개 push 전에 아래 항목을 반드시 처리해야 합니다.

## 반드시 (push 전)

1. **git 히스토리 축소** — 과거 커밋에 대용량 씬 번들(453 MB, 그중 115 MB GLB)이
   포함되어 있어 `.git` pack이 ~200 MB입니다. GitHub는 100 MB 초과 파일 push를
   **거부**하므로, 현재 트리는 정리되었어도 히스토리에 남은 blob 때문에 push가
   실패합니다. 두 가지 방법 중 하나:
   - `git filter-repo --path reference-bundle --path sionna-rt-gui-jaewoo-examples --invert-paths`
     로 히스토리에서 번들 blob 제거 (백업 후 실행), 또는
   - 새 orphan 브랜치/새 리포에 현재 스냅샷만 커밋해서 공개 (가장 단순).
2. **라이선스 확정** — `LICENSE`에 Apache-2.0을 넣어 두었습니다
   (Sionna RT가 Apache-2.0이라 호환 목적의 권장 기본값). 다른 라이선스를
   원하면 push 전에 교체하고 README의 License 절도 함께 수정하세요.
3. **reference-bundle 배포 채널** — 번들은 이제 git 미추적입니다. FTC 데모는
   임포트된 결과물이 리포에 포함되어 그대로 동작하지만, 재임포트가 필요한
   사용자를 위해 번들을 GitHub Release 자산(또는 외부 링크)으로 올리고
   INSTALL.md의 다운로드 위치를 채워 넣으세요.

## 권장

- **Windows 장경로**: 히스토리를 정리하면 145자 경로도 사라져 `git clone`이
  기본 설정으로 통과합니다. 히스토리를 유지한 채 공개한다면 README 사전 요구
  사항에 `git config --global core.longpaths true`를 명시하세요.
- **README 영문판**(또는 이중 언어) + 대표 스크린샷 1장 — 공개 리포 도달률.
- `frontend/package.json`·`backend/pyproject.toml`에 `repository` URL 채우기
  (리포 주소 확정 후).
- 검증된 스택 명시: Python 3.11/3.12, Node 20+ (드릴은 3.12/Node 24에서 통과).

## 드릴에서 확인된 것 (조치 완료)

- ~~LICENSE 부재~~ → Apache-2.0 추가.
- ~~`scipy` 미선언~~ → base 의존성에 추가 (씬 임포트 경로가 요구; 미설치 시
  문서의 pytest 명령이 4건 실패했음).
- ~~절대경로 PII~~ (provenance.json ×2, docs, `.claude/launch.json`) →
  상대경로화 + `.claude/` 미추적.
- ~~데모 미포함 오기재~~ → INSTALL.md 트러블슈팅 교정 (데모 3종은 기본 포함).
- ~~개인 사용자명이 든 번들 디렉터리명~~ → `reference-bundle/`로 개명, 참조 8곳 갱신.
