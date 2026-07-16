#!/usr/bin/env python3
"""Democracy 3.0 정책포크 분할 데이터 무결성 검사."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


class ValidationFailure(RuntimeError):
    pass


def read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValidationFailure(f"필수 파일 없음: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValidationFailure(
            f"JSON 형식 오류: {path} line={exc.lineno} column={exc.colno}"
        ) from exc


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValidationFailure(message)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate(root: Path) -> dict[str, Any]:
    index_path = root / "bills-index.json"
    reports_dir = root / "reports"

    payload = read_json(index_path)
    require(isinstance(payload, dict), "bills-index.json 최상위는 객체여야 합니다.")

    bills = payload.get("bills")
    require(isinstance(bills, list), "bills 항목은 목록이어야 합니다.")
    require(len(bills) > 0, "법안 목록이 비어 있습니다.")
    require(reports_dir.is_dir(), "reports 폴더가 없습니다.")

    ids: set[str] = set()
    urls: set[str] = set()
    referenced_reports: set[Path] = set()
    warnings: list[str] = []

    required_index_fields = {
        "id",
        "title",
        "category",
        "stage",
        "proposed_date",
        "card_summary",
        "largest_risk",
        "report_url",
    }

    deep_report_count = 0
    official_summary_count = 0
    claim_count = 0
    attack_count = 0
    evidence_chain_count = 0

    for position, summary in enumerate(bills, start=1):
        require(isinstance(summary, dict), f"{position}번째 목록 항목이 객체가 아닙니다.")

        missing = [
            field for field in required_index_fields
            if not str(summary.get(field, "")).strip()
        ]
        require(
            not missing,
            f"{position}번째 법안 필수 필드 누락: {', '.join(missing)}",
        )

        bill_id = str(summary["id"])
        require(bill_id not in ids, f"중복 법안 ID: {bill_id}")
        ids.add(bill_id)

        report_url = str(summary["report_url"])
        require(
            report_url.startswith("reports/") and report_url.endswith(".json"),
            f"잘못된 보고서 경로: {report_url}",
        )
        require(report_url not in urls, f"중복 보고서 경로: {report_url}")
        urls.add(report_url)

        report_path = root / report_url
        referenced_reports.add(report_path.resolve())
        require(report_path.exists(), f"개별 보고서 없음: {report_url}")

        report_payload = read_json(report_path)
        bill = report_payload.get("bill") if isinstance(report_payload, dict) else None
        require(isinstance(bill, dict), f"보고서 bill 객체 없음: {report_url}")
        require(str(bill.get("id", "")) == bill_id, f"목록·보고서 ID 불일치: {bill_id}")
        require(
            str(bill.get("title", "")) == str(summary["title"]),
            f"목록·보고서 제목 불일치: {bill_id}",
        )

        official = bill.get("official") or {}
        require(
            str(official.get("source_url", "")).startswith("http"),
            f"국회 원문 링크 없음: {bill_id}",
        )

        if str(bill.get("official_summary", "")).strip():
            official_summary_count += 1

        deep = bill.get("deep_review") or {}
        if deep.get("status"):
            deep_report_count += 1

        claims = deep.get("claim_ledger") or []
        attacks = deep.get("deep_red_team") or []
        chains = deep.get("evidence_chains") or []

        require(isinstance(claims, list), f"주장 원장 형식 오류: {bill_id}")
        require(isinstance(attacks, list), f"레드팀 형식 오류: {bill_id}")
        require(isinstance(chains, list), f"증거사슬 형식 오류: {bill_id}")

        claim_count += len(claims)
        attack_count += len(attacks)
        evidence_chain_count += len(chains)

        fork_count = int(summary.get("fork_count", 0) or 0)
        if fork_count > 0:
            require(
                len(bill.get("fork_candidates") or []) == fork_count,
                f"수정안 개수 불일치: {bill_id}",
            )

    actual_reports = {
        path.resolve() for path in reports_dir.glob("*.json")
    }
    orphan_reports = actual_reports - referenced_reports
    if orphan_reports:
        warnings.append(f"참조되지 않은 보고서 {len(orphan_reports)}건")

    require(
        deep_report_count >= max(1, int(len(bills) * 0.90)),
        "심층보고서 생성률이 90% 미만입니다.",
    )
    require(
        official_summary_count >= max(1, int(len(bills) * 0.90)),
        "공식 요약 연동률이 90% 미만입니다.",
    )

    return {
        "status": "healthy",
        "schema_version": payload.get("schema_version", ""),
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "generated_at": payload.get("generated_at", ""),
        "counts": {
            "bills": len(bills),
            "reports": len(actual_reports),
            "deep_reports": deep_report_count,
            "official_summaries": official_summary_count,
            "claims": claim_count,
            "attacks": attack_count,
            "evidence_chains": evidence_chain_count,
        },
        "checks": {
            "index_json": "pass",
            "unique_ids": "pass",
            "report_references": "pass",
            "report_json": "pass",
            "official_links": "pass",
            "deep_review_coverage": "pass",
            "official_summary_coverage": "pass",
        },
        "warnings": warnings,
        "artifacts": {
            "bills_index_sha256": sha256(index_path),
        },
    }


def write_health(root: Path, payload: dict[str, Any]) -> None:
    (root / "health.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args()
    root = args.root.resolve()

    try:
        result = validate(root)
    except Exception as exc:
        write_health(
            root,
            {
                "status": "degraded",
                "checked_at": datetime.now(timezone.utc).isoformat(),
                "error": str(exc),
                "counts": {},
                "checks": {},
                "warnings": [],
            },
        )
        print(f"[FAIL] {exc}")
        return 1

    write_health(root, result)
    print(
        "[PASS] "
        f"bills={result['counts']['bills']} "
        f"reports={result['counts']['reports']} "
        f"claims={result['counts']['claims']} "
        f"attacks={result['counts']['attacks']} "
        f"chains={result['counts']['evidence_chains']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
