"""Democracy 3.0 국가 시범검토 10건 선정·공식자료 조사 모듈."""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests


TARGET_PROFILES: tuple[tuple[str, str], ...] = (
    ("appointment_governance", "인사·권력구조"),
    ("data_security", "개인정보·정보보안"),
    ("subsidy_fiscal", "재정·지원배분"),
    ("punitive_regulation", "규제·제재"),
    ("labor_safety", "노동·산업안전"),
    ("health_care", "보건·의료"),
    ("housing_property", "주거·재산권"),
    ("education_system", "교육제도"),
    ("organization_power", "행정조직·권한배분"),
    ("general_reform", "일반 제도개편"),
)

PRIORITY_SIGNALS: tuple[tuple[str, int], ...] = (
    ("교육감", 14), ("선거", 10), ("임명", 9),
    ("개인정보", 12), ("인공지능", 10), ("정보보안", 10),
    ("의료", 9), ("환자", 8), ("산업재해", 10),
    ("근로자", 7), ("주택", 9), ("임대", 8),
    ("조세", 7), ("보조금", 7), ("과태료", 7),
    ("벌칙", 8), ("위원회", 6), ("안전", 7),
    ("장애", 6), ("아동", 6),
)

UNKNOWN_COMMITTEES = {
    "", "확인 필요", "소관위원회 확인 필요", "소관위원회 미정 또는 확인 필요",
    "미정", "없음", "null", "None",
}


def tidy(value: Any, fallback: str = "") -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text or fallback


def unique(items: list[str], limit: int = 20) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in items:
        cleaned = tidy(item)
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        result.append(cleaned)
        if len(result) >= limit:
            break
    return result


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.href = ""
        self.title = ""
        self.text_parts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        if tag.lower() != "a":
            return
        values = {key.lower(): value or "" for key, value in attrs}
        self.href = values.get("href", "")
        self.title = values.get("title", "")
        self.text_parts = []

    def handle_data(self, data: str) -> None:
        if self.href:
            self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self.href:
            return
        self.links.append({
            "href": self.href,
            "title": self.title,
            "text": tidy(" ".join(self.text_parts), self.title),
        })
        self.href = ""
        self.title = ""
        self.text_parts = []


def classify_document(label: str, url: str) -> str:
    context = f"{label} {url}".lower()
    rules = (
        ("신·구조문 대비표", ("신구조문", "신ㆍ구조문", "대비표")),
        ("비용추계서·미첨부사유", ("비용추계", "미첨부", "cost")),
        ("위원회 검토·심사보고서", ("검토보고", "심사보고", "전문위원")),
        ("회의록·토론자료", ("회의록", "minutes")),
        ("법률안 조문 전문", ("의안원문", "법률안원문", "법률안 원문", "의안 원문")),
        ("정부·기관 의견", ("정부의견", "부처의견", "기관의견", "검토의견")),
        ("첨부자료", (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".zip")),
    )
    for kind, words in rules:
        if any(word in context for word in words):
            return kind
    return ""


def normalize_url(base_url: str, href: str) -> str:
    href = html.unescape((href or "").strip())
    if not href or href.startswith(("#", "javascript:", "mailto:")):
        return ""
    resolved = urljoin(base_url, href)
    parsed = urlparse(resolved)
    return resolved if parsed.scheme in ("http", "https") else ""


def explore_official_page(detail_url: str) -> dict[str, Any]:
    if not detail_url:
        return {
            "status": "상세페이지 주소 없음",
            "http_status": 0,
            "documents": [],
            "error": "국회 상세페이지 주소가 제공되지 않았습니다.",
        }

    try:
        response = requests.get(
            detail_url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 Democracy3-Policy-Fork/0.12 "
                    "(public policy research prototype)"
                ),
                "Accept-Language": "ko-KR,ko;q=0.9,en;q=0.5",
            },
        )
        response.raise_for_status()
    except requests.RequestException as exc:
        return {
            "status": "상세페이지 연결 실패",
            "http_status": getattr(
                getattr(exc, "response", None), "status_code", 0
            ) or 0,
            "documents": [],
            "error": str(exc)[:300],
        }

    parser = LinkParser()
    try:
        parser.feed(response.text)
    except Exception:
        pass

    documents: list[dict[str, str]] = []
    seen: set[str] = set()

    for item in parser.links:
        url = normalize_url(detail_url, item["href"])
        if not url or url in seen:
            continue
        label = tidy(item.get("text") or item.get("title"), "첨부자료")
        kind = classify_document(label, url)
        if not kind:
            continue
        seen.add(url)
        documents.append({
            "type": kind,
            "title": label,
            "url": url,
            "status": "공식 상세페이지에서 위치 발견",
            "content_status": "내용 추출·조문 대조 전",
        })

    file_pattern = re.compile(
        r'(?P<url>(?:https?:)?//[^"\'<> ]+?\.(?:pdf|hwp|hwpx|docx?|zip)'
        r'(?:\?[^"\'<> ]*)?|/[^"\'<> ]+?\.(?:pdf|hwp|hwpx|docx?|zip)'
        r'(?:\?[^"\'<> ]*)?)',
        re.IGNORECASE,
    )
    for match in file_pattern.finditer(response.text):
        url = normalize_url(detail_url, match.group("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        documents.append({
            "type": classify_document("", url) or "첨부자료",
            "title": Path(urlparse(url).path).name or "첨부자료",
            "url": url,
            "status": "공식 상세페이지 HTML에서 위치 발견",
            "content_status": "내용 추출·조문 대조 전",
        })

    return {
        "status": "상세페이지 연결 완료",
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "documents": documents[:40],
        "error": "",
    }


def extract_clause_references(text: str) -> list[str]:
    patterns = (
        r"안\s*제\s*\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?",
        r"제\s*\d+조(?:의\d+)?(?:제\d+항)?(?:제\d+호)?",
        r"안\s*별표\s*\d*",
        r"안\s*부칙",
    )
    found: list[str] = []
    for pattern in patterns:
        found.extend(re.findall(pattern, text or ""))
    normalized = [
        re.sub(r"\s+", "", item).replace("안제", "안 제")
        for item in found
    ]
    return unique(normalized, 20)


def priority_score(bill: dict[str, Any]) -> int:
    deep = bill.get("deep_review") or {}
    profiles = deep.get("profiles") or []
    context = f"{bill.get('title', '')} {bill.get('official_summary', '')}"

    score = min(len(profiles), 3) * 8
    score += min(len(deep.get("deep_red_team") or []), 12)
    score += min(len(deep.get("claim_ledger") or []), 10)
    score += min(len(bill.get("fork_candidates") or []), 3) * 4
    score += 10 if bill.get("official_summary") else 0
    score += 5 if (bill.get("official") or {}).get("source_url") else 0
    score += 4 if bill.get("committee") not in UNKNOWN_COMMITTEES else 0

    for signal, points in PRIORITY_SIGNALS:
        if signal in context:
            score += points
    return score


def selection_reasons(bill: dict[str, Any]) -> list[str]:
    deep = bill.get("deep_review") or {}
    context = f"{bill.get('title', '')} {bill.get('official_summary', '')}"
    matched = [signal for signal, _ in PRIORITY_SIGNALS if signal in context]

    reasons = [
        f"정책 위험축 {len(deep.get('profiles') or [])}개",
        f"심층 공격경로 {len(deep.get('deep_red_team') or [])}개",
        f"발의자 주장 {len(deep.get('claim_ledger') or [])}개",
    ]
    if matched:
        reasons.append("고영향 신호: " + ", ".join(matched[:5]))
    reasons.append("권력·기본권·재정·집행 위험을 대표하는 국가 시범검토 대상")
    return reasons


def select_pilot_bills(
    bills: list[dict[str, Any]],
    limit: int = 10,
) -> list[dict[str, Any]]:
    ranked = sorted(
        bills,
        key=lambda b: (priority_score(b), str(b.get("proposed_date", ""))),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    covered_profiles: set[str] = set()
    covered_categories: set[str] = set()

    for bill in ranked:
        context = f"{bill.get('title', '')} {bill.get('official_summary', '')}"
        if "교육감" in context:
            selected.append(bill)
            selected_ids.add(str(bill.get("id", "")))
            covered_categories.add(str(bill.get("category", "")))
            covered_profiles.update(
                item.get("key", "")
                for item in (bill.get("deep_review") or {}).get("profiles") or []
            )
            break

    for profile_key, _ in TARGET_PROFILES:
        if len(selected) >= limit:
            break
        if profile_key in covered_profiles:
            continue
        candidate = next(
            (
                bill for bill in ranked
                if str(bill.get("id", "")) not in selected_ids
                and any(
                    item.get("key") == profile_key
                    for item in (
                        (bill.get("deep_review") or {}).get("profiles") or []
                    )
                )
            ),
            None,
        )
        if candidate:
            selected.append(candidate)
            selected_ids.add(str(candidate.get("id", "")))
            covered_categories.add(str(candidate.get("category", "")))
            covered_profiles.update(
                item.get("key", "")
                for item in (
                    (candidate.get("deep_review") or {}).get("profiles") or []
                )
            )

    while len(selected) < limit:
        candidates = [
            bill for bill in ranked
            if str(bill.get("id", "")) not in selected_ids
        ]
        if not candidates:
            break
        candidates.sort(
            key=lambda b: (
                str(b.get("category", "")) not in covered_categories,
                priority_score(b),
                str(b.get("proposed_date", "")),
            ),
            reverse=True,
        )
        candidate = candidates[0]
        selected.append(candidate)
        selected_ids.add(str(candidate.get("id", "")))
        covered_categories.add(str(candidate.get("category", "")))

    return selected[:limit]


def build_claim_audit(bill: dict[str, Any]) -> list[dict[str, Any]]:
    claims = (bill.get("deep_review") or {}).get("claim_ledger") or []
    return [{
        "id": claim.get("id", ""),
        "claim": claim.get("claim", ""),
        "claim_type": claim.get("claim_type", ""),
        "current_status": "발의자 제안이유에 기재된 주장",
        "official_basis": "국회 공식 제안이유·주요내용",
        "required_evidence": claim.get("required_evidence", ""),
        "rejection_condition": claim.get("rejection_condition", ""),
        "independent_evidence": [],
        "verdict": "독립 검증 전",
    } for claim in claims[:10]]


def build_case(
    bill: dict[str, Any],
    rank: int,
    page_audit: dict[str, Any],
) -> dict[str, Any]:
    deep = bill.get("deep_review") or {}
    summary = str(bill.get("official_summary", ""))
    clause_refs = extract_clause_references(summary)
    documents = page_audit.get("documents") or []
    found_types = {
        doc.get("type", "") for doc in documents if doc.get("type")
    }

    score = (
        (15 if summary else 0)
        + (10 if page_audit.get("status") == "상세페이지 연결 완료" else 0)
        + min(len(found_types), 5) * 5
        + (10 if clause_refs else 0)
        + (10 if deep.get("claim_ledger") else 0)
        + (10 if deep.get("deep_red_team") else 0)
        + (10 if bill.get("fork_candidates") else 0)
        + (5 if bill.get("committee") not in UNKNOWN_COMMITTEES else 0)
    )
    score = min(score, 85)

    def source_status(kind: str, fallback: str) -> str:
        return "위치 발견" if kind in found_types else fallback

    source_matrix = [
        {
            "source": "국회 제안이유 및 주요내용",
            "status": "연결 완료" if summary else "미연결",
            "use": "문제진단·목표·변경방향·조문 참조",
        },
        {
            "source": "국회 의안 상세페이지",
            "status": page_audit.get("status", "확인 필요"),
            "use": "진행상태와 공식 첨부자료 위치 확인",
        },
        {
            "source": "법률안 조문 전문",
            "status": source_status("법률안 조문 전문", "위치 확인 필요"),
            "use": "실제 권리·의무·예외·위임·제재 대조",
        },
        {
            "source": "신·구조문 대비표",
            "status": source_status("신·구조문 대비표", "위치 확인 필요"),
            "use": "현행법과 개정안 문장 단위 비교",
        },
        {
            "source": "비용추계서·미첨부사유",
            "status": source_status("비용추계서·미첨부사유", "위치 확인 필요"),
            "use": "재정·인력·민간 준수비용 검증",
        },
        {
            "source": "위원회 검토·심사보고서",
            "status": source_status("위원회 검토·심사보고서", "심사 진행 후 확인"),
            "use": "법체계·위헌성·집행가능성·쟁점 검토",
        },
        {
            "source": "독립 통계·학술·감사자료",
            "status": "미연결",
            "use": "발의자 주장 입증·반증",
        },
    ]

    fork_designs: list[dict[str, Any]] = []
    for fork in (bill.get("fork_candidates") or [])[:3]:
        linked = [
            chain for chain in deep.get("evidence_chains") or []
            if fork.get("label") in (chain.get("fork_links") or [])
        ]
        fork_designs.append({
            "label": fork.get("label", ""),
            "title": fork.get("title", ""),
            "design_change": fork.get("change") or fork.get("body", ""),
            "solves": fork.get("solves", ""),
            "benefit": fork.get("benefit", ""),
            "new_risk": fork.get("risk", ""),
            "clause_direction": fork.get("clause_hint", ""),
            "draft_status": "법률안 조문 전문 대조 전 설계초안",
            "exact_clause_text": "",
            "blocked_attacks": unique(
                [chain.get("attack", "") for chain in linked], 8
            ),
            "residual_risks": unique(
                [chain.get("residual_risk", "") for chain in linked], 6
            ),
        })

    return {
        "rank": rank,
        "bill_id": bill.get("id", ""),
        "bill_no": (bill.get("official") or {}).get("bill_no", ""),
        "title": bill.get("title", ""),
        "category": bill.get("category", ""),
        "committee": bill.get("committee", ""),
        "priority_score": priority_score(bill),
        "selection_reasons": selection_reasons(bill),
        "status": "국가 시범검토 10건 · 공식자료 조사 단계",
        "completion_score": score,
        "completion_ceiling": 85,
        "human_review_status": "운영자 검토 전",
        "expert_review_status": "전문가 검토 전",
        "profiles": deep.get("profiles") or [],
        "official_url": (bill.get("official") or {}).get("source_url", ""),
        "page_audit": page_audit,
        "source_matrix": source_matrix,
        "clause_audit": {
            "references_from_summary": clause_refs,
            "current_law_text": "현행법 원문 대조 전",
            "proposed_clause_text": "법률안 조문 전문 대조 전",
            "comparison_status": "신·구조문 문장 단위 대조 전",
            "affected_rights": bill.get("affected_groups") or [],
            "authority_shift": bill.get("authority_changes") or [],
            "cost_effect": bill.get("cost_bearers") or [],
        },
        "claim_audit": build_claim_audit(bill),
        "red_team_audit": [{
            "profile": attack.get("profile_label", ""),
            "attack": attack.get("title", ""),
            "actor": attack.get("attacker", ""),
            "path": attack.get("path", ""),
            "warning": attack.get("warning", ""),
            "guardrail": attack.get("defense", ""),
            "residual": attack.get("residual", ""),
            "evidence_status": "조문·외부자료 대조 전",
        } for attack in (deep.get("deep_red_team") or [])[:12]],
        "fork_designs": fork_designs,
        "review_tasks": [
            "법률안 조문 전문을 확보해 조문번호별 원문을 입력",
            "현행 법령 원문과 신·구조문을 문장 단위로 대조",
            "비용추계서 또는 미첨부 사유서를 검토",
            "발의자 주장별 독립 통계·감사·연구자료를 연결",
            "위원회 검토보고서와 회의록의 반론·정부답변을 반영",
            "포크 A/B/C를 실제 법률문장으로 작성",
            "각 포크의 방어력과 새 위험을 사람 검토로 확정",
            "박찬성 운영자 검토와 외부 전문가 검토를 별도 기록",
        ],
        "remaining_uncertainty": [
            "첨부자료 위치 발견은 문서 내용 검토 완료를 의미하지 않습니다.",
            "조문·비용·독립 통계 연결 전에는 찬반 결론을 확정하지 않습니다.",
            "자동 포크는 실제 법률문장 확정 전 설계 방향입니다.",
        ],
    }


def build_pilot_program(
    bills: list[dict[str, Any]],
    generated_at: str,
) -> dict[str, Any]:
    selected = select_pilot_bills(bills, 10)
    audits: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                explore_official_page,
                (bill.get("official") or {}).get("source_url", ""),
            ): str(bill.get("id", ""))
            for bill in selected
        }
        for future in as_completed(futures):
            bill_id = futures[future]
            try:
                audits[bill_id] = future.result()
            except Exception as exc:
                audits[bill_id] = {
                    "status": "상세페이지 조사 오류",
                    "http_status": 0,
                    "documents": [],
                    "error": str(exc)[:300],
                }

    cases: list[dict[str, Any]] = []
    for rank, bill in enumerate(selected, start=1):
        case = build_case(
            bill,
            rank,
            audits.get(
                str(bill.get("id", "")),
                {"status": "상세페이지 조사 미실행", "documents": [], "error": ""},
            ),
        )
        bill["pilot_case"] = case
        cases.append(case)

    return {
        "title": "Democracy 3.0 국가 시범검토 10건",
        "generated_at": generated_at,
        "status": "공식자료 조사 및 조문 대조 진행",
        "purpose": (
            "모든 법안을 자동 수집하는 기반 위에서, 권력·기본권·재정·규제·복지·"
            "교육·보건·노동·주거·디지털을 대표하는 10건을 증거 기반 완성보고서로 "
            "승격하기 위한 국가 시범운영 묶음입니다."
        ),
        "selection_method": (
            "복수 위험프로필, 국민영향, 기본권·권력·재정 신호, 레드팀 공격 수, "
            "공식자료 연결 가능성과 분야 다양성을 합산해 자동 선정"
        ),
        "cases": cases,
        "summary": {
            "selected": len(cases),
            "detail_pages_connected": sum(
                1 for case in cases
                if (case.get("page_audit") or {}).get("status")
                == "상세페이지 연결 완료"
            ),
            "documents_discovered": sum(
                len((case.get("page_audit") or {}).get("documents") or [])
                for case in cases
            ),
            "claims_to_verify": sum(
                len(case.get("claim_audit") or []) for case in cases
            ),
            "forks_to_draft": sum(
                len(case.get("fork_designs") or []) for case in cases
            ),
            "human_reviewed": 0,
            "expert_reviewed": 0,
        },
    }
