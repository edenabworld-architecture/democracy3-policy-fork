#!/usr/bin/env python3
"""Democracy 3.0 공개 배포 전 통합 무결성·신뢰성 감사."""

from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from review_store import (
    CLAIM_VERDICTS,
    load_manifest,
    load_overrides,
)
from policy_history import main as rebuild_policy_lifecycle


ROOT = Path(__file__).resolve().parent
BILLS_PATH = ROOT / "bills.json"
INDEX_PATH = ROOT / "bills-index.json"
REPORTS_DIR = ROOT / "reports"
MANIFEST_PATH = ROOT / "pilot_manifest.json"
OVERRIDES_PATH = ROOT / "review_overrides.json"
STATUS_PATH = ROOT / "release-status.json"
LIFECYCLE_PATH = ROOT / "policy-lifecycle.json"
CIVIC_PATH = ROOT / "civic-log.json"
SOURCE_REGISTRY_PATH = ROOT / "source_registry.json"
PARTICIPATION_CONFIG_PATH = ROOT / "participation-config.json"


class ReleaseValidationError(RuntimeError):
    pass


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ReleaseValidationError(message)


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ReleaseValidationError(f"필수 파일 없음: {path.name}") from exc
    except json.JSONDecodeError as exc:
        raise ReleaseValidationError(
            f"JSON 형식 오류: {path.name} line={exc.lineno} column={exc.colno}"
        ) from exc


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def is_http_url(value: Any) -> bool:
    parsed = urlparse(str(value or "").strip())
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def normalized_clause(value: str) -> str:
    return value.replace("안 ", "").replace(" ", "")


def validate_case(
    case: dict[str, Any],
    bill: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, int]:
    bill_id = str(case.get("bill_id", ""))
    require(bill_id == str(bill.get("id", "")), f"{bill_id}: 법안·시범보고서 ID 불일치")
    require(case.get("title") == bill.get("title"), f"{bill_id}: 법안·시범보고서 제목 불일치")

    maturity = case.get("maturity") or {}
    required_scores = (
        "structure_readiness",
        "official_source_coverage",
        "independent_evidence_validation",
        "human_review",
        "expert_review",
    )
    for field in required_scores:
        value = maturity.get(field)
        require(
            isinstance(value, int) and 0 <= value <= 100,
            f"{bill_id}: 성숙도 {field} 값 오류",
        )

    references = (case.get("clause_audit") or {}).get("references_from_summary") or []
    normalized = [normalized_clause(str(item)) for item in references]
    require(len(normalized) == len(set(normalized)), f"{bill_id}: 조문 참조 중복")

    page_documents = (case.get("page_audit") or {}).get("documents") or []
    for document in page_documents:
        require(document.get("case_specific") is True, f"{bill_id}: 공통 링크가 공식문서 수에 포함")
        require(is_http_url(document.get("url")), f"{bill_id}: 공식문서 URL 오류")

    store = case.get("verified_review_store") or {}
    require(
        store.get("methodology_version") == "verified-review-store-v2",
        f"{bill_id}: 수동검토 원장 병합 누락",
    )

    claim_ids = {
        str(claim.get("id", ""))
        for claim in case.get("claim_audit") or []
        if claim.get("id")
    }
    verified_claims = 0
    for claim in case.get("claim_audit") or []:
        evidence = claim.get("independent_evidence") or []
        verdict = claim.get("verdict", "")
        if verdict in CLAIM_VERDICTS:
            require(evidence, f"{bill_id}.{claim.get('id')}: 근거 없는 확정 판정")
            verified_claims += 1
        elif evidence:
            require(
                verdict in {"독립 검증 전", "판정 유보"},
                f"{bill_id}.{claim.get('id')}: 증거와 판정 상태 불일치",
            )

    for evidence in override.get("external_evidence") or []:
        for claim_id in evidence.get("claim_ids") or []:
            require(claim_id in claim_ids, f"{bill_id}: 존재하지 않는 주장 연결 {claim_id}")

    comparisons = override.get("clause_comparisons") or []
    for comparison in comparisons:
        require(is_http_url(comparison.get("source_url")), f"{bill_id}: 조문 출처 URL 누락")
        require(comparison.get("current_text"), f"{bill_id}: 현행 조문 누락")
        require(comparison.get("proposed_text"), f"{bill_id}: 개정 조문 누락")

    fork_drafts = override.get("fork_drafts") or []
    for fork in fork_drafts:
        if fork.get("status") != "설계초안":
            require(fork.get("exact_clause_text"), f"{bill_id}: 조문 단계 포크에 실제 조문 없음")
            require(fork.get("target_clause"), f"{bill_id}: 조문 단계 포크에 대상조문 없음")

    operator_reviews = [
        item
        for item in override.get("reviews") or []
        if item.get("role") == "operator" and item.get("status") == "완료"
    ]
    expert_reviews = [
        item
        for item in override.get("reviews") or []
        if item.get("role") in {"expert", "legal", "domain"}
        and item.get("status") == "완료"
    ]
    require(
        maturity["human_review"] == (100 if operator_reviews else 0),
        f"{bill_id}: 운영자 검토 점수와 기록 불일치",
    )
    require(
        maturity["expert_review"] == (100 if expert_reviews else 0),
        f"{bill_id}: 전문가 검토 점수와 기록 불일치",
    )

    return {
        "verified_claims": verified_claims,
        "clause_comparisons": len(comparisons),
        "fork_drafts": len(fork_drafts),
        "operator_reviewed": 1 if operator_reviews else 0,
        "expert_reviewed": 1 if expert_reviews else 0,
        "corrections": len(override.get("corrections") or []),
    }



def ensure_lifecycle_coverage(
    bill_by_id: dict[str, dict[str, Any]],
) -> tuple[dict[str, Any], bool]:
    """생애주기 파일이 없거나 현재 법안보다 뒤처졌으면 즉시 재생성합니다."""
    rebuilt = False
    try:
        lifecycle = read_json(LIFECYCLE_PATH)
    except ReleaseValidationError:
        rebuild_policy_lifecycle()
        lifecycle = read_json(LIFECYCLE_PATH)
        rebuilt = True

    lifecycle_ids = set((lifecycle.get("policies") or {}).keys())
    missing_ids = sorted(set(bill_by_id) - lifecycle_ids)

    if lifecycle.get("schema_version") != "1.0" or missing_ids:
        rebuild_policy_lifecycle()
        lifecycle = read_json(LIFECYCLE_PATH)
        rebuilt = True
        lifecycle_ids = set((lifecycle.get("policies") or {}).keys())
        missing_ids = sorted(set(bill_by_id) - lifecycle_ids)

    require(
        lifecycle.get("schema_version") == "1.0",
        "policy-lifecycle.json 스키마 오류",
    )
    require(
        not missing_ids,
        "시행·버전 추적에서 정책 누락: "
        + ", ".join(missing_ids[:10])
        + (f" 외 {len(missing_ids) - 10}건" if len(missing_ids) > 10 else ""),
    )
    return lifecycle, rebuilt

def validate() -> dict[str, Any]:
    manifest = load_manifest(MANIFEST_PATH)
    overrides = load_overrides(OVERRIDES_PATH, manifest)
    data = read_json(BILLS_PATH)

    require(isinstance(data, dict), "bills.json 최상위는 객체여야 합니다.")
    require(data.get("collection_warnings") == [], "수집 경고가 있어 배포를 중단합니다.")
    bills = data.get("bills")
    require(isinstance(bills, list) and bills, "법안 목록이 비어 있습니다.")

    bill_by_id = {
        str(bill.get("id", "")): bill
        for bill in bills
        if isinstance(bill, dict) and bill.get("id")
    }
    program = data.get("pilot_program") or {}
    cases = program.get("cases") or []
    require(len(cases) == 10, "국가 시범검토 보고서는 정확히 10건이어야 합니다.")
    case_ids = [str(case.get("bill_id", "")) for case in cases]
    require(case_ids == manifest["locked_ids"], "시범검토 순서가 manifest와 다릅니다.")
    require(len(set(case_ids)) == 10, "시범검토 ID 중복")
    require(
        (program.get("manifest") or {}).get("selection_mode") == "고정 유지",
        "시범검토 선정상태가 고정 유지가 아닙니다.",
    )

    totals = {
        "verified_claims": 0,
        "clause_comparisons": 0,
        "fork_drafts": 0,
        "operator_reviewed": 0,
        "expert_reviewed": 0,
        "corrections": 0,
    }
    stages: dict[str, int] = {}

    for case in cases:
        bill_id = str(case.get("bill_id", ""))
        require(bill_id in bill_by_id, f"고정 시범법안이 bills 목록에 없음: {bill_id}")
        result = validate_case(
            case,
            bill_by_id[bill_id],
            overrides["cases"][bill_id],
        )
        for key, value in result.items():
            totals[key] += value
        stage = str((case.get("maturity") or {}).get("stage", ""))
        stages[stage] = stages.get(stage, 0) + 1

    if INDEX_PATH.exists():
        index = read_json(INDEX_PATH)
        index_ids = {
            str(bill.get("id", ""))
            for bill in index.get("bills") or []
            if isinstance(bill, dict)
        }
        require(set(case_ids).issubset(index_ids), "경량 index에서 시범법안 누락")

    if REPORTS_DIR.exists():
        for bill_id in case_ids:
            report_path = REPORTS_DIR / f"{bill_id}.json"
            require(report_path.exists(), f"개별 보고서 누락: {report_path.name}")
            report = read_json(report_path)
            require(
                str((report.get("bill") or {}).get("id", "")) == bill_id,
                f"개별 보고서 ID 불일치: {report_path.name}",
            )

    lifecycle, lifecycle_rebuilt = ensure_lifecycle_coverage(bill_by_id)
    civic = read_json(CIVIC_PATH)
    registry = read_json(SOURCE_REGISTRY_PATH)
    participation = read_json(PARTICIPATION_CONFIG_PATH)
    lifecycle_ids = set((lifecycle.get("policies") or {}).keys())
    require(isinstance(civic.get("entries"), list), "civic-log.json entries 오류")
    require(any(source.get("key") == "assembly_bills" and source.get("status") == "active" for source in registry.get("sources") or []), "국회 공식수집원 등록 누락")
    require(participation.get("fallback"), "시민참여 fallback 설정 누락")

    generated_at = str(data.get("generated_at", ""))
    require(generated_at, "generated_at 누락")
    status = {
        "status": "healthy",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "generated_at": generated_at,
        "schema_version": data.get("schema_version", "legacy-root"),
        "checks": {
            "collection": "pass",
            "manifest": "pass",
            "locked_cases": "pass",
            "manual_store": "pass",
            "claim_evidence": "pass",
            "clause_integrity": "pass",
            "review_integrity": "pass",
            "split_outputs": "pass" if INDEX_PATH.exists() and REPORTS_DIR.exists() else "legacy-compatible",
            "policy_lifecycle": (
                "rebuilt-and-pass" if lifecycle_rebuilt else "pass"
            ),
            "civic_log": "pass",
            "source_registry": "pass",
            "participation_contract": "pass",
        },
        "counts": {
            "bills": len(bills),
            "pilot_cases": len(cases),
            **totals,
            "stages": stages,
            "lifecycle_policies": len(lifecycle_ids),
            "civic_entries": len(civic.get("entries") or []),
            "registered_sources": len(registry.get("sources") or []),
        },
        "hashes": {
            "bills_json": sha256(BILLS_PATH),
            "pilot_manifest": sha256(MANIFEST_PATH),
            "review_overrides": sha256(OVERRIDES_PATH),
        },
        "authorship": "창안 및 원안 설계: 박찬성",
    }
    return status


def main() -> int:
    try:
        status = validate()
    except Exception as exc:
        failure = {
            "status": "degraded",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "error": str(exc),
        }
        STATUS_PATH.write_text(
            json.dumps(failure, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"[FAIL] {exc}", file=sys.stderr)
        return 1

    STATUS_PATH.write_text(
        json.dumps(status, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        "[PASS] "
        f"bills={status['counts']['bills']} "
        f"pilot={status['counts']['pilot_cases']} "
        f"verified_claims={status['counts']['verified_claims']} "
        f"clauses={status['counts']['clause_comparisons']} "
        f"operator={status['counts']['operator_reviewed']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
