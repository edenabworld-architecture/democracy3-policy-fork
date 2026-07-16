# 검증 원장 데이터 스키마

`review_overrides.json`은 자동생성 결과보다 우선하는 것이 아니라, 검증된 인간 작업을 별도로 보존하고 공개 데이터에 병합하는 원장입니다.

## official_documents

필수: `title`, `document_type`, `url`, `source_agency`, `status`

허용 상태: 위치 확인 / 파일 확보 / 본문 추출 / 조문 대조 완료 / 사람 검토 완료

## external_evidence

필수: `title`, `publisher`, `url`, `claim_ids`, `status`, `summary`

허용 상태: 후보 / 출처 확인 / 내용 검토 / 주장 입증 / 주장 부분입증 / 주장 반증 / 판정 유보

확정 판정 상태에는 `reviewed_at`, `reviewed_by`가 필요합니다.

## claim_verdicts

주장 ID를 입증 / 부분입증 / 반증 / 판정 유보 중 하나로 연결합니다. 외부근거 없이 확정판정을 기록하면 배포검사가 실패합니다.

## clause_comparisons

필수: `law_name`, `clause_id`, `current_text`, `proposed_text`, `source_url`, `status`

허용 상태: 초안 / 원문 확인 / 문장 대조 완료 / 운영자 검토 / 전문가 검토

## fork_drafts

필수: `label`, `title`, `status`

조문초안 이상에는 `target_clause`, `exact_clause_text`, `drafted_by`, `drafted_at`이 필요합니다.

## reviews

필수: `role`, `status`, `reviewer`, `reviewed_at`, `summary`

전문가 완료기록에는 `affiliation`, `expertise`가 필요합니다.

## corrections

필수: `correction_id`, `status`, `issue`, `reported_at`

반영 또는 기각에는 `decision_reason`이 필요합니다.
