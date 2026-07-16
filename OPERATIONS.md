# 운영자 작업 지침

## 절대로 직접 고치지 않는 파일

- `bills.json`
- `bills-index.json`
- `reports/*.json`
- `release-status.json`

위 파일은 자동생성됩니다.

## 사람이 고치는 파일

- `pilot_manifest.json`: 시범검토 대상의 고정·교체
- `review_overrides.json`: 검증된 공식문서·외부근거·조문·포크·검토·정정
- `PILOT_REVIEW_PROTOCOL.md`: 방법론
- 공개 설명 문서와 사이트 화면

## 검토 절차

1. 공식문서의 법안별 연결 여부를 확인합니다.
2. 조문 원문을 현행법과 개정안으로 나누어 기록합니다.
3. 각 주장 ID에 독립근거를 연결합니다.
4. 입증·부분입증·반증·판정유보를 기록합니다.
5. 레드팀 공격과 포크가 실제로 연결되는지 확인합니다.
6. 포크 조문을 작성하고 새 위험을 기록합니다.
7. 운영자 검토기록을 완료로 남깁니다.
8. 외부 전문가 검토는 소속·전문분야까지 기록합니다.
9. 자동검증을 통과한 뒤만 공개합니다.

## 시범대상 교체

자동추천 결과만으로 교체하지 않습니다. 교체하려면 `pilot_manifest.json`의 ID를 명시적으로 수정하고, 변경 이유를 `CHANGELOG.md`에 남깁니다.

## 공식문서 진단

국회 상세페이지에서 문서가 자동 발견되지 않으면 Actions에서 `Probe official bill documents`를 실행합니다. 생성된 아티팩트의 `document-diagnostics.json`에서 onclick, 폼, 스크립트와 다운로드 후보를 확인합니다.
