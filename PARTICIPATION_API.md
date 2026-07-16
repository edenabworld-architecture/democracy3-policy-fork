# Democracy 3.0 시민참여 API 계약 v1.0

정적 GitHub Pages는 익명 제출을 영구 저장할 수 없으므로, 사이트는 `participation-config.json`의 `api_endpoint`가 비어 있을 때 공개 GitHub 접수와 JSON 내려받기를 사용합니다. 운영 서버가 연결되면 같은 화면이 아래 API를 사용합니다.

## POST /submissions

필수: `policy_id`, `policy_title`, `type`, `title`, `content`, `created_at`

선택: `source_url`, `target`, `author_display`, `contact_private`

허용 유형: annotation / correction / evidence / red_team / fork

응답: `submission_id`, `status=접수`, `public_log_url`

## GET /submissions

필터: `policy_id`, `type`, `status`, `cursor`

공개 필드만 반환하며 연락처와 내부 검토 메모는 반환하지 않습니다.

## PATCH /submissions/{id}

운영자 인증이 필요합니다. 상태는 접수 / 검토 중 / 근거 확인 / 반영 / 부분반영 / 기각 / 보류입니다. 반영·부분반영·기각에는 결정 이유와 결정자를 남겨야 합니다.

## 불변 원칙

- 제출 원문을 조용히 삭제하거나 덮어쓰지 않습니다.
- 수정은 새 버전 이벤트로 추가합니다.
- 개인정보와 공개 정책내용을 분리 저장합니다.
- 포크와 정정은 어떤 정책 버전에 대한 것인지 명시합니다.
- 운영자 결정도 시민 반론의 대상이 됩니다.
