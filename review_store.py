"""Democracy 3.0 검증된 인간 작업 영속화·병합 모듈."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


SCHEMA_VERSION = "1.0"
METHODOLOGY_VERSION = "verified-review-store-v2"

OFFICIAL_DOCUMENT_STATUSES = {
    "위치 확인",
    "파일 확보",
    "본문 추출",
    "조문 대조 완료",
    "사람 검토 완료",
}
EVIDENCE_STATUSES = {
    "후보",
    "출처 확인",
    "내용 검토",
    "주장 입증",
    "주장 부분입증",
    "주장 반증",
    "판정 유보",
}
CLAIM_VERDICTS = {
    "입증",
    "부분입증",
    "반증",
    "판정 유보",
}
REVIEW_ROLES = {"operator", "expert", "legal", "domain", "citizen"}
REVIEW_STATUSES = {"검토 중", "완료", "재검토 필요"}
FORK_STATUSES = {"설계초안", "조문초안", "운영자 검토", "전문가 검토", "확정안"}
CLAUSE_STATUSES = {"초안", "원문 확인", "문장 대조 완료", "운영자 검토", "전문가 검토"}
CORRECTION_STATUSES = {"접수", "검토 중", "반영", "부분반영", "기각", "보류"}
CIVIC_STATUSES = {"접수", "검토 중", "근거 확인", "반영", "부분반영", "기각", "보류"}
IMPLEMENTATION_STATUSES = {"계획", "추진 중", "지연", "완료", "중단", "평가 중", "재검토"}


class ReviewStoreError(ValueError):
    pass


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return deepcopy(default)
    except json.JSONDecodeError as exc:
        raise ReviewStoreError(
            f"JSON 형식 오류: {path} line={exc.lineno} column={exc.colno}"
        ) from exc


def tidy(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReviewStoreError(message)


def valid_http_url(value: Any) -> bool:
    url = tidy(value)
    if not url:
        return False
    parsed = urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def unique_strings(items: list[Any], limit: int = 100) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        value = tidy(item)
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def load_manifest(path: Path) -> dict[str, Any]:
    manifest = read_json(path, {})
    require(isinstance(manifest, dict), "pilot_manifest.json 최상위는 객체여야 합니다.")
    require(
        manifest.get("schema_version") == SCHEMA_VERSION,
        "pilot_manifest.json schema_version이 지원 버전과 다릅니다.",
    )
    locked_ids = unique_strings(list(manifest.get("locked_ids") or []), 10)
    require(len(locked_ids) == 10, "pilot_manifest.json의 locked_ids는 정확히 10건이어야 합니다.")
    require(len(set(locked_ids)) == 10, "pilot_manifest.json에 중복 법안 ID가 있습니다.")
    manifest["locked_ids"] = locked_ids
    return manifest


def default_case_store() -> dict[str, Any]:
    return {
        "official_documents": [],
        "external_evidence": [],
        "claim_verdicts": {},
        "clause_comparisons": [],
        "fork_drafts": [],
        "reviews": [],
        "corrections": [],
        "annotations": [],
        "citizen_forks": [],
        "decisions": [],
        "implementation_tracking": [],
        "operator_notes": "",
    }


def load_overrides(path: Path, manifest: dict[str, Any]) -> dict[str, Any]:
    default = {
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "updated_at": "",
        "updated_by": "박찬성",
        "cases": {},
    }
    store = read_json(path, default)
    require(isinstance(store, dict), "review_overrides.json 최상위는 객체여야 합니다.")
    require(
        store.get("schema_version") == SCHEMA_VERSION,
        "review_overrides.json schema_version이 지원 버전과 다릅니다.",
    )
    cases = store.get("cases")
    require(isinstance(cases, dict), "review_overrides.json cases는 객체여야 합니다.")

    locked_ids = set(manifest["locked_ids"])
    unknown_ids = sorted(set(cases) - locked_ids)
    require(
        not unknown_ids,
        "review_overrides.json에 고정대상이 아닌 ID가 있습니다: " + ", ".join(unknown_ids),
    )

    normalized_cases: dict[str, dict[str, Any]] = {}
    for bill_id in manifest["locked_ids"]:
        raw = cases.get(bill_id) or {}
        require(isinstance(raw, dict), f"{bill_id} 검토 원장은 객체여야 합니다.")
        normalized = default_case_store()
        normalized.update(raw)
        for field in (
            "official_documents",
            "external_evidence",
            "clause_comparisons",
            "fork_drafts",
            "reviews",
            "corrections",
            "annotations",
            "citizen_forks",
            "decisions",
            "implementation_tracking",
        ):
            require(isinstance(normalized.get(field), list), f"{bill_id}.{field}는 목록이어야 합니다.")
        require(
            isinstance(normalized.get("claim_verdicts"), dict),
            f"{bill_id}.claim_verdicts는 객체여야 합니다.",
        )
        normalized_cases[bill_id] = normalized

    store["cases"] = normalized_cases
    validate_overrides(store, manifest)
    return store


def validate_official_document(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.official_documents[{index}]"
    require(tidy(item.get("title")), f"{prefix}.title이 필요합니다.")
    require(tidy(item.get("document_type")), f"{prefix}.document_type이 필요합니다.")
    require(valid_http_url(item.get("url")), f"{prefix}.url은 유효한 HTTP(S) 주소여야 합니다.")
    require(
        item.get("status") in OFFICIAL_DOCUMENT_STATUSES,
        f"{prefix}.status가 허용값이 아닙니다.",
    )
    require(tidy(item.get("source_agency")), f"{prefix}.source_agency가 필요합니다.")
    if item.get("status") in {"조문 대조 완료", "사람 검토 완료"}:
        require(tidy(item.get("reviewed_at")), f"{prefix}.reviewed_at이 필요합니다.")
        require(tidy(item.get("reviewed_by")), f"{prefix}.reviewed_by가 필요합니다.")


def validate_evidence(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.external_evidence[{index}]"
    require(tidy(item.get("title")), f"{prefix}.title이 필요합니다.")
    require(tidy(item.get("publisher")), f"{prefix}.publisher가 필요합니다.")
    require(valid_http_url(item.get("url")), f"{prefix}.url은 유효한 HTTP(S) 주소여야 합니다.")
    require(item.get("status") in EVIDENCE_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    claim_ids = unique_strings(list(item.get("claim_ids") or []), 20)
    require(claim_ids, f"{prefix}.claim_ids가 하나 이상 필요합니다.")
    item["claim_ids"] = claim_ids
    require(tidy(item.get("summary")), f"{prefix}.summary가 필요합니다.")
    if item.get("status") in {"주장 입증", "주장 부분입증", "주장 반증", "판정 유보"}:
        require(tidy(item.get("reviewed_at")), f"{prefix}.reviewed_at이 필요합니다.")
        require(tidy(item.get("reviewed_by")), f"{prefix}.reviewed_by가 필요합니다.")


def validate_clause(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.clause_comparisons[{index}]"
    require(tidy(item.get("law_name")), f"{prefix}.law_name이 필요합니다.")
    require(tidy(item.get("clause_id")), f"{prefix}.clause_id가 필요합니다.")
    require(tidy(item.get("current_text")), f"{prefix}.current_text가 필요합니다.")
    require(tidy(item.get("proposed_text")), f"{prefix}.proposed_text가 필요합니다.")
    require(valid_http_url(item.get("source_url")), f"{prefix}.source_url이 필요합니다.")
    require(item.get("status") in CLAUSE_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    if item.get("status") in {"문장 대조 완료", "운영자 검토", "전문가 검토"}:
        require(tidy(item.get("analysis")), f"{prefix}.analysis가 필요합니다.")


def validate_fork(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.fork_drafts[{index}]"
    require(tidy(item.get("label")) in {"A", "B", "C"}, f"{prefix}.label은 A/B/C여야 합니다.")
    require(tidy(item.get("title")), f"{prefix}.title이 필요합니다.")
    require(item.get("status") in FORK_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    if item.get("status") != "설계초안":
        require(tidy(item.get("exact_clause_text")), f"{prefix}.exact_clause_text가 필요합니다.")
        require(tidy(item.get("target_clause")), f"{prefix}.target_clause가 필요합니다.")
        require(tidy(item.get("drafted_by")), f"{prefix}.drafted_by가 필요합니다.")
        require(tidy(item.get("drafted_at")), f"{prefix}.drafted_at이 필요합니다.")


def validate_review(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.reviews[{index}]"
    require(item.get("role") in REVIEW_ROLES, f"{prefix}.role이 허용값이 아닙니다.")
    require(item.get("status") in REVIEW_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    require(tidy(item.get("reviewer")), f"{prefix}.reviewer가 필요합니다.")
    require(tidy(item.get("reviewed_at")), f"{prefix}.reviewed_at이 필요합니다.")
    require(tidy(item.get("summary")), f"{prefix}.summary가 필요합니다.")
    if item.get("role") in {"expert", "legal", "domain"} and item.get("status") == "완료":
        require(tidy(item.get("affiliation")), f"{prefix}.affiliation이 필요합니다.")
        require(tidy(item.get("expertise")), f"{prefix}.expertise가 필요합니다.")


def validate_correction(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.corrections[{index}]"
    require(tidy(item.get("correction_id")), f"{prefix}.correction_id가 필요합니다.")
    require(item.get("status") in CORRECTION_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    require(tidy(item.get("issue")), f"{prefix}.issue가 필요합니다.")
    require(tidy(item.get("reported_at")), f"{prefix}.reported_at이 필요합니다.")
    if item.get("status") in {"반영", "기각"}:
        require(tidy(item.get("decision_reason")), f"{prefix}.decision_reason이 필요합니다.")



def validate_annotation(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.annotations[{index}]"
    require(tidy(item.get("annotation_id")), f"{prefix}.annotation_id가 필요합니다.")
    require(tidy(item.get("target")), f"{prefix}.target이 필요합니다.")
    require(tidy(item.get("content")), f"{prefix}.content가 필요합니다.")
    require(item.get("status") in CIVIC_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    require(tidy(item.get("created_at")), f"{prefix}.created_at이 필요합니다.")


def validate_citizen_fork(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.citizen_forks[{index}]"
    require(tidy(item.get("fork_id")), f"{prefix}.fork_id가 필요합니다.")
    require(tidy(item.get("title")), f"{prefix}.title이 필요합니다.")
    require(tidy(item.get("proposal")), f"{prefix}.proposal이 필요합니다.")
    require(item.get("status") in CIVIC_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    require(tidy(item.get("created_at")), f"{prefix}.created_at이 필요합니다.")


def validate_decision(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.decisions[{index}]"
    require(tidy(item.get("decision_id")), f"{prefix}.decision_id가 필요합니다.")
    require(item.get("status") in CIVIC_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    require(tidy(item.get("decision")), f"{prefix}.decision이 필요합니다.")
    require(tidy(item.get("reason")), f"{prefix}.reason이 필요합니다.")
    require(tidy(item.get("decided_at")), f"{prefix}.decided_at이 필요합니다.")
    require(tidy(item.get("decided_by")), f"{prefix}.decided_by가 필요합니다.")


def validate_implementation(item: dict[str, Any], bill_id: str, index: int) -> None:
    prefix = f"{bill_id}.implementation_tracking[{index}]"
    require(tidy(item.get("event_id")), f"{prefix}.event_id가 필요합니다.")
    require(tidy(item.get("date")), f"{prefix}.date가 필요합니다.")
    require(item.get("status") in IMPLEMENTATION_STATUSES, f"{prefix}.status가 허용값이 아닙니다.")
    progress = item.get("progress_percent", 0)
    require(isinstance(progress, (int, float)) and 0 <= progress <= 100, f"{prefix}.progress_percent는 0~100이어야 합니다.")
    if item.get("source_url"):
        require(valid_http_url(item.get("source_url")), f"{prefix}.source_url이 올바르지 않습니다.")


def validate_overrides(store: dict[str, Any], manifest: dict[str, Any]) -> None:
    cases = store["cases"]
    for bill_id in manifest["locked_ids"]:
        case = cases[bill_id]
        for index, item in enumerate(case["official_documents"]):
            require(isinstance(item, dict), f"{bill_id}.official_documents[{index}]는 객체여야 합니다.")
            validate_official_document(item, bill_id, index)
        for index, item in enumerate(case["external_evidence"]):
            require(isinstance(item, dict), f"{bill_id}.external_evidence[{index}]는 객체여야 합니다.")
            validate_evidence(item, bill_id, index)
        for claim_id, verdict in case["claim_verdicts"].items():
            require(tidy(claim_id), f"{bill_id}.claim_verdicts에 빈 claim ID가 있습니다.")
            require(verdict in CLAIM_VERDICTS, f"{bill_id}.{claim_id} 판정값이 허용되지 않습니다.")
        for index, item in enumerate(case["clause_comparisons"]):
            require(isinstance(item, dict), f"{bill_id}.clause_comparisons[{index}]는 객체여야 합니다.")
            validate_clause(item, bill_id, index)
        for index, item in enumerate(case["fork_drafts"]):
            require(isinstance(item, dict), f"{bill_id}.fork_drafts[{index}]는 객체여야 합니다.")
            validate_fork(item, bill_id, index)
        for index, item in enumerate(case["reviews"]):
            require(isinstance(item, dict), f"{bill_id}.reviews[{index}]는 객체여야 합니다.")
            validate_review(item, bill_id, index)
        for index, item in enumerate(case["corrections"]):
            require(isinstance(item, dict), f"{bill_id}.corrections[{index}]는 객체여야 합니다.")
            validate_correction(item, bill_id, index)
        for index, item in enumerate(case["annotations"]):
            require(isinstance(item, dict), f"{bill_id}.annotations[{index}]는 객체여야 합니다.")
            validate_annotation(item, bill_id, index)
        for index, item in enumerate(case["citizen_forks"]):
            require(isinstance(item, dict), f"{bill_id}.citizen_forks[{index}]는 객체여야 합니다.")
            validate_citizen_fork(item, bill_id, index)
        for index, item in enumerate(case["decisions"]):
            require(isinstance(item, dict), f"{bill_id}.decisions[{index}]는 객체여야 합니다.")
            validate_decision(item, bill_id, index)
        for index, item in enumerate(case["implementation_tracking"]):
            require(isinstance(item, dict), f"{bill_id}.implementation_tracking[{index}]는 객체여야 합니다.")
            validate_implementation(item, bill_id, index)


def evidence_verdict_from_status(status: str) -> str:
    return {
        "주장 입증": "입증",
        "주장 부분입증": "부분입증",
        "주장 반증": "반증",
        "판정 유보": "판정 유보",
    }.get(status, "")


def completed_review(case_store: dict[str, Any], roles: set[str]) -> dict[str, Any] | None:
    completed = [
        item
        for item in case_store.get("reviews") or []
        if item.get("role") in roles and item.get("status") == "완료"
    ]
    if not completed:
        return None
    completed.sort(key=lambda item: tidy(item.get("reviewed_at")), reverse=True)
    return completed[0]


def recompute_maturity(case: dict[str, Any], case_store: dict[str, Any]) -> dict[str, Any]:
    maturity = dict(case.get("maturity") or {})
    claims = case.get("claim_audit") or []
    verified_claims = [
        claim
        for claim in claims
        if claim.get("verdict") in CLAIM_VERDICTS
        and claim.get("independent_evidence")
    ]

    official_docs = case_store.get("official_documents") or []
    doc_types = {
        tidy(item.get("document_type"))
        for item in official_docs
        if item.get("status") in OFFICIAL_DOCUMENT_STATUSES
    }
    official_score = 10 if (case.get("page_audit") or {}).get("status") == "상세페이지 연결 완료" else 0
    official_score += 10 if (case.get("clause_audit") or {}).get("references_from_summary") else 0
    official_score += 25 if any("조문" in item or "법률안" in item for item in doc_types) else 0
    official_score += 20 if any("신·구" in item or "대비표" in item for item in doc_types) else 0
    official_score += 15 if any("비용" in item for item in doc_types) else 0
    official_score += 15 if any("검토" in item or "심사" in item for item in doc_types) else 0
    official_score += 5 if any("회의록" in item or "토론" in item for item in doc_types) else 0

    evidence_score = round(100 * len(verified_claims) / len(claims)) if claims else 0
    operator = completed_review(case_store, {"operator"})
    expert = completed_review(case_store, {"expert", "legal", "domain"})

    maturity.update(
        {
            "official_source_coverage": min(100, official_score),
            "independent_evidence_validation": min(100, evidence_score),
            "human_review": 100 if operator else 0,
            "expert_review": 100 if expert else 0,
        }
    )

    if maturity["expert_review"] == 100:
        maturity["stage"], maturity["stage_label"] = "S5", "전문가 검증 완료"
    elif maturity["human_review"] == 100:
        maturity["stage"], maturity["stage_label"] = "S4", "운영자 검토 완료"
    elif maturity["independent_evidence_validation"] >= 40:
        maturity["stage"], maturity["stage_label"] = "S3", "독립근거 검증 중"
    elif maturity["official_source_coverage"] >= 40:
        maturity["stage"], maturity["stage_label"] = "S2", "공식문서 대조 중"
    else:
        maturity["stage"], maturity["stage_label"] = "S1", "구조화·자료확보 준비"

    return maturity


def merge_case(case: dict[str, Any], case_store: dict[str, Any], generated_at: str) -> None:
    case["verified_review_store"] = {
        "schema_version": SCHEMA_VERSION,
        "methodology_version": METHODOLOGY_VERSION,
        "merged_at": generated_at,
        "official_documents": deepcopy(case_store.get("official_documents") or []),
        "external_evidence": deepcopy(case_store.get("external_evidence") or []),
        "clause_comparisons": deepcopy(case_store.get("clause_comparisons") or []),
        "fork_drafts": deepcopy(case_store.get("fork_drafts") or []),
        "reviews": deepcopy(case_store.get("reviews") or []),
        "corrections": deepcopy(case_store.get("corrections") or []),
        "annotations": deepcopy(case_store.get("annotations") or []),
        "citizen_forks": deepcopy(case_store.get("citizen_forks") or []),
        "decisions": deepcopy(case_store.get("decisions") or []),
        "implementation_tracking": deepcopy(case_store.get("implementation_tracking") or []),
        "operator_notes": tidy(case_store.get("operator_notes")),
    }

    evidence_by_claim: dict[str, list[dict[str, Any]]] = {}
    verdicts: dict[str, list[str]] = {}
    for evidence in case_store.get("external_evidence") or []:
        for claim_id in evidence.get("claim_ids") or []:
            evidence_by_claim.setdefault(claim_id, []).append(deepcopy(evidence))
            verdict = evidence_verdict_from_status(tidy(evidence.get("status")))
            if verdict:
                verdicts.setdefault(claim_id, []).append(verdict)

    explicit_verdicts = case_store.get("claim_verdicts") or {}
    for claim in case.get("claim_audit") or []:
        claim_id = tidy(claim.get("id"))
        claim["independent_evidence"] = evidence_by_claim.get(claim_id, [])
        if claim_id in explicit_verdicts:
            claim["verdict"] = explicit_verdicts[claim_id]
        elif verdicts.get(claim_id):
            ordered = ["반증", "부분입증", "입증", "판정 유보"]
            claim["verdict"] = next(
                (value for value in ordered if value in verdicts[claim_id]),
                "판정 유보",
            )
        else:
            claim["verdict"] = "독립 검증 전"

    comparisons = case_store.get("clause_comparisons") or []
    if comparisons:
        case["clause_audit"]["comparisons"] = deepcopy(comparisons)
        complete = sum(
            1
            for item in comparisons
            if item.get("status") in {"문장 대조 완료", "운영자 검토", "전문가 검토"}
        )
        case["clause_audit"]["comparison_status"] = (
            f"수동 대조 {complete}/{len(comparisons)}건 완료"
        )

    fork_by_label = {
        tidy(item.get("label")): item
        for item in case_store.get("fork_drafts") or []
        if tidy(item.get("label"))
    }
    for fork in case.get("fork_designs") or []:
        manual = fork_by_label.get(tidy(fork.get("label")))
        if manual:
            fork.update(
                {
                    "title": manual.get("title") or fork.get("title"),
                    "exact_clause_text": manual.get("exact_clause_text", ""),
                    "target_clause": manual.get("target_clause", ""),
                    "draft_status": manual.get("status", ""),
                    "drafted_by": manual.get("drafted_by", ""),
                    "drafted_at": manual.get("drafted_at", ""),
                    "rationale": manual.get("rationale", ""),
                    "source_links": manual.get("source_links") or [],
                }
            )

    operator_review = completed_review(case_store, {"operator"})
    expert_review = completed_review(case_store, {"expert", "legal", "domain"})
    case["human_review_status"] = (
        "운영자 검토 완료" if operator_review else "운영자 검토 전"
    )
    case["expert_review_status"] = (
        "전문가 검토 완료" if expert_review else "전문가 검토 전"
    )
    case["maturity"] = recompute_maturity(case, case_store)
    case["provenance"] = {
        "automatic_generated_at": generated_at,
        "manual_store_methodology": METHODOLOGY_VERSION,
        "manual_updated_at": "",
        "authorship": "창안 및 원안 설계: 박찬성",
    }


def merge_verified_reviews(
    pilot_program: dict[str, Any],
    bills: list[dict[str, Any]],
    manifest: dict[str, Any],
    overrides: dict[str, Any],
    generated_at: str,
) -> dict[str, Any]:
    cases = pilot_program.get("cases") or []
    case_by_id = {
        tidy(case.get("bill_id")): case
        for case in cases
        if tidy(case.get("bill_id"))
    }
    require(
        list(case_by_id) == manifest["locked_ids"],
        "생성된 시범검토 순서가 pilot_manifest.json과 다릅니다.",
    )

    for bill_id in manifest["locked_ids"]:
        case = case_by_id[bill_id]
        case_store = overrides["cases"][bill_id]
        merge_case(case, case_store, generated_at)

    bill_by_id = {
        tidy(bill.get("id")): bill
        for bill in bills
        if tidy(bill.get("id"))
    }
    for bill_id, case in case_by_id.items():
        if bill_id in bill_by_id:
            bill_by_id[bill_id]["pilot_case"] = case

    summary = pilot_program.setdefault("summary", {})
    summary["verified_claims"] = sum(
        1
        for case in cases
        for claim in case.get("claim_audit") or []
        if claim.get("verdict") in CLAIM_VERDICTS
        and claim.get("independent_evidence")
    )
    summary["clause_comparisons"] = sum(
        len(
            ((case.get("verified_review_store") or {}).get("clause_comparisons") or [])
        )
        for case in cases
    )
    summary["manual_official_documents"] = sum(
        len(
            ((case.get("verified_review_store") or {}).get("official_documents") or [])
        )
        for case in cases
    )
    summary["human_reviewed"] = sum(
        1 for case in cases if case.get("human_review_status") == "운영자 검토 완료"
    )
    summary["expert_reviewed"] = sum(
        1 for case in cases if case.get("expert_review_status") == "전문가 검토 완료"
    )
    summary["corrections"] = sum(
        len(((case.get("verified_review_store") or {}).get("corrections") or []))
        for case in cases
    )
    summary["annotations"] = sum(
        len(((case.get("verified_review_store") or {}).get("annotations") or []))
        for case in cases
    )
    summary["citizen_forks"] = sum(
        len(((case.get("verified_review_store") or {}).get("citizen_forks") or []))
        for case in cases
    )
    summary["implementation_events"] = sum(
        len(((case.get("verified_review_store") or {}).get("implementation_tracking") or []))
        for case in cases
    )

    pilot_program["manual_review_store"] = {
        "schema_version": overrides.get("schema_version"),
        "methodology_version": overrides.get("methodology_version"),
        "updated_at": overrides.get("updated_at", ""),
        "updated_by": overrides.get("updated_by", ""),
        "authorship": overrides.get("authorship", ""),
    }
    pilot_program["manifest"]["source"] = "pilot_manifest.json"
    pilot_program["manifest"]["locked_by"] = manifest.get("locked_by", "")
    pilot_program["manifest"]["locked_at"] = manifest.get("locked_at", "")
    return pilot_program
