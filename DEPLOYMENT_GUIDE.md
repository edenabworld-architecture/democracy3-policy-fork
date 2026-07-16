# Democracy 3.0 v1.0 배포 순서

1. 저장소 루트에 v1.0 루트 파일을 덮어쓴다.
2. `.github/workflows/update.yml`과 Issue Template 폴더를 정확한 숨김 경로에 올린다.
3. Actions에서 `Update and validate policy data`를 새로 실행한다.
4. `Fetch and build policy data` → `Sync public civic log` → `Build policy versions and implementation tracking` → `Validate public release` → `Commit verified release`가 모두 통과하는지 확인한다.
5. `release-status.json`의 상태가 `healthy`이고 `policy_lifecycle`, `civic_log`, `source_registry`, `participation_contract`가 모두 `pass`인지 확인한다.

## 현재 즉시 작동하는 참여 방식

- 사이트 안에서 정책·유형·대상·내용·출처를 작성
- 브라우저 임시저장
- 제출 JSON 내려받기
- GitHub 공개접수 창으로 전달
- Actions가 Issue를 `civic-log.json`으로 자동 동기화

## 익명 접수 전환

`participation-api`를 배포한 뒤 `participation-config.json`의 `api_endpoint`에 주소를 입력하면 같은 시민참여 화면이 API로 직접 POST한다. 별도 백엔드를 배포하지 않은 상태에서는 익명 영구접수가 작동한다고 표시하지 않는다.
