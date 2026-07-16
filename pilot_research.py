"""Democracy 3.0 국가 시범검토 10건 — 신뢰성 보정·고정선정 모듈.

핵심 원칙
1. 현재 확정된 10건은 후속 실행에서도 유지한다.
2. 법안별 첨부문서와 공통 내비게이션 링크를 분리한다.
3. 구조화 준비도와 실제 증거 완성도를 같은 점수로 표시하지 않는다.
4. 발의자의 주장을 유형별로 분해하고, 필요한 증거와 기각조건을 다시 지정한다.
5. 조문 참조를 중복 없이 정규화한다.
"""

from __future__ import annotations

import html
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urljoin, urlparse

import requests


SELECTION_VERSION = "pilot-selection-v2"
METHODOLOGY_VERSION = "evidence-maturity-v1"

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
    ("교육감", 14),
    ("선거", 10),
    ("임명", 9),
    ("개인정보", 12),
    ("인공지능", 10),
    ("정보보안", 10),
    ("의료", 9),
    ("환자", 8),
    ("산업재해", 10),
    ("근로자", 7),
    ("주택", 9),
    ("임대", 8),
    ("조세", 7),
    ("보조금", 7),
    ("과태료", 7),
    ("벌칙", 8),
    ("위원회", 6),
    ("안전", 7),
    ("장애", 6),
    ("아동", 6),
)

UNKNOWN_COMMITTEES = {
    "",
    "확인 필요",
    "소관위원회 확인 필요",
    "소관위원회 미정 또는 확인 필요",
    "미정",
    "없음",
    "null",
    "None",
}

FILE_EXTENSIONS = (".pdf", ".hwp", ".hwpx", ".doc", ".docx", ".zip", ".xls", ".xlsx")
ATTACHMENT_QUERY_KEYS = {
    "fileid",
    "file_id",
    "attachid",
    "attach_id",
    "atchfileid",
    "atch_file_id",
    "seq",
    "fileseq",
    "file_seq",
    "download",
    "down",
    "filename",
    "file_name",
}
ATTACHMENT_PATH_SIGNALS = (
    "download",
    "filedown",
    "filedownload",
    "attach",
    "attachment",
    "file.do",
    "down.do",
)

GENERIC_NAVIGATION_LABELS = {
    "회의록",
    "국회회의록",
    "의안정보",
    "열린국회정보",
    "국가법령정보센터",
    "법률정보",
    "국회",
    "홈",
    "메인",
}

CLAIM_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "법적·헌법적 주장",
        (
            "헌법",
            "법률",
            "법적",
            "위헌",
            "합헌",
            "법률상",
            "명시하고",
            "명시되어",
            "조문",
            "법체계",
        ),
    ),
    (
        "국제·제도 비교 주장",
        (
            "미국",
            "일본",
            "유럽",
            "해외",
            "외국",
            "선진국",
            "oecd",
            "국제적으로",
            "다른 나라",
        ),
    ),
    (
        "재정·비용 주장",
        (
            "예산",
            "재정",
            "비용",
            "세수",
            "세액",
            "국고",
            "지방비",
            "부담금",
            "추계",
        ),
    ),
    (
        "기본권·권리 영향 주장",
        (
            "기본권",
            "권리",
            "자유",
            "평등",
            "차별",
            "정치적 중립",
            "사생활",
            "재산권",
            "생명권",
        ),
    ),
    (
        "인과·원인 주장",
        (
            "때문",
            "원인",
            "결과",
            "초래",
            "야기",
            "로 인해",
            "따라서",
            "결국",
            "영향을 미",
        ),
    ),
    (
        "집행·행정역량 주장",
        (
            "집행",
            "행정",
            "기관",
            "절차",
            "담당",
            "인력",
            "관리",
            "감독",
            "권한",
            "의무",
        ),
    ),
    (
        "효과·편익 주장",
        (
            "효과",
            "개선",
            "증진",
            "해소",
            "완화",
            "줄일",
            "감소시",
            "확대할",
            "보호할",
            "도움",
        ),
    ),
    (
        "규범·당위 주장",
        (
            "하여야",
            "해야",
            "필요함",
            "필요가",
            "바람직",
            "타당",
            "옳다",
            "정당",
        ),
    ),
)

CLAIM_EVIDENCE_STANDARDS: dict[str, tuple[str, str]] = {
    "법적·헌법적 주장": (
        "헌법·현행법 조문, 헌법재판소·대법원 판례, 법제처 해석과 관련 법리",
        "인용 조문이나 판례가 주장의 법적 결론을 지지하지 않거나 반대 법리가 우세한 경우",
    ),
    "국제·제도 비교 주장": (
        "국가별 공식 법령·정부자료, 실제 선임절차, 국가별 예외와 제도 맥락",
        "일부 국가만 선택했거나 비교대상 국가의 실제 제도가 주장과 다르고 맥락 차이가 큰 경우",
    ),
    "재정·비용 주장": (
        "비용추계서, 예산·결산, 세수효과, 행정인력·민간 준수비용과 장기 재정전망",
        "비용이 누락되거나 편익보다 크고 지속 가능한 재원조달 방안이 없는 경우",
    ),
    "기본권·권리 영향 주장": (
        "권리 제한의 대상·범위·기간, 비례성, 대체수단, 차별영향과 권리구제 절차",
        "침해가 과도하거나 덜 제한적인 대안이 있고 구제절차가 불충분한 경우",
    ),
    "인과·원인 주장": (
        "시간순서, 비교집단, 대안원인 통제, 자연실험·준실험 또는 장기 추세자료",
        "다른 요인이 결과를 더 잘 설명하거나 원인과 결과가 함께 변하지 않는 경우",
    ),
    "집행·행정역량 주장": (
        "담당기관의 권한·인력·예산, 처리기간, 현장업무량, 정보시스템과 책임구조",
        "필요한 집행역량이 없거나 역할과 책임이 불명확하고 현장부담이 감당하기 어려운 경우",
    ),
    "효과·편익 주장": (
        "선행사업 성과, 비교집단, 효과크기, 수혜자 분포, 부작용과 비용 대비 편익",
        "효과가 작거나 특정 집단에 편중되고 부작용·비용이 편익보다 큰 경우",
    ),
    "규범·당위 주장": (
        "정책목표의 정당성, 가치충돌, 기본권·형평성 영향과 대안 간 비교",
        "상충가치를 과도하게 희생하거나 같은 목표를 덜 침해적으로 달성할 수 있는 경우",
    ),
    "수치·규모 주장": (
        "원자료, 분모, 산출방식, 조사기간, 표본, 추세와 비교집단",
        "수치가 재현되지 않거나 분모·기간·표본 선택에 따라 결론이 달라지는 경우",
    ),
    "문제 진단": (
        "최근 공식 통계, 현행제도 운영자료, 피해·사각지대 규모와 집단별 분포",
        "문제 규모가 작거나 일시적이고 현행제도의 제한적 보완으로 해결 가능한 경우",
    ),
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


def clamp_score(value: int | float) -> int:
    return max(0, min(100, int(round(value))))


class LinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[dict[str, str]] = []
        self.href = ""
        self.title = ""
        self.rel = ""
        self.class_name = ""
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
        self.rel = values.get("rel", "")
        self.class_name = values.get("class", "")
        self.text_parts = []

    def handle_data(self, data: str) -> None:
        if self.href:
            self.text_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() != "a" or not self.href:
            return
        self.links.append(
            {
                "href": self.href,
                "title": self.title,
                "rel": self.rel,
                "class": self.class_name,
                "text": tidy(" ".join(self.text_parts), self.title),
            }
        )
        self.href = ""
        self.title = ""
        self.rel = ""
        self.class_name = ""
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
        ("첨부자료", FILE_EXTENSIONS),
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


def has_file_extension(url: str) -> bool:
    path = urlparse(url).path.lower()
    return any(path.endswith(ext) for ext in FILE_EXTENSIONS)


def has_attachment_signature(url: str) -> bool:
    parsed = urlparse(url)
    query_keys = {key.lower() for key in parse_qs(parsed.query)}
    path = parsed.path.lower()
    return bool(query_keys & ATTACHMENT_QUERY_KEYS) or any(
        signal in path for signal in ATTACHMENT_PATH_SIGNALS
    )


def is_generic_navigation(
    label: str,
    url: str,
    bill_id: str,
    bill_no: str,
) -> bool:
    parsed = urlparse(url)
    path = parsed.path.rstrip("/")
    normalized_label = tidy(label).replace(" ", "")
    case_signal = bill_id in url or (bill_no and bill_no in url)

    if case_signal or has_file_extension(url) or has_attachment_signature(url):
        return False

    if normalized_label in {item.replace(" ", "") for item in GENERIC_NAVIGATION_LABELS}:
        return True

    if parsed.netloc == "record.assembly.go.kr" and path in ("", "/"):
        return True

    if path in ("", "/") and not parsed.query:
        return True

    return False


def is_case_specific_document(
    label: str,
    url: str,
    bill_id: str,
    bill_no: str,
) -> bool:
    context = f"{label} {url}"
    if bill_id and bill_id in context:
        return True
    if bill_no and bill_no in context:
        return True
    if has_file_extension(url):
        return True
    if has_attachment_signature(url):
        return True
    return False


def explore_official_page(
    detail_url: str,
    bill_id: str = "",
    bill_no: str = "",
) -> dict[str, Any]:
    if not detail_url:
        return {
            "status": "상세페이지 주소 없음",
            "http_status": 0,
            "documents": [],
            "related_system_links": [],
            "excluded_links": 0,
            "error": "국회 상세페이지 주소가 제공되지 않았습니다.",
        }

    try:
        response = requests.get(
            detail_url,
            timeout=15,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 Democracy3-Policy-Fork/0.12.1 "
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
            )
            or 0,
            "documents": [],
            "related_system_links": [],
            "excluded_links": 0,
            "error": str(exc)[:300],
        }

    parser = LinkParser()
    try:
        parser.feed(response.text)
    except Exception:
        pass

    documents: list[dict[str, str]] = []
    related_system_links: list[dict[str, str]] = []
    seen: set[str] = set()
    excluded = 0

    for item in parser.links:
        url = normalize_url(detail_url, item["href"])
        if not url or url in seen:
            continue
        seen.add(url)
        label = tidy(item.get("text") or item.get("title"), "링크")
        kind = classify_document(label, url)

        if is_generic_navigation(label, url, bill_id, bill_no):
            if kind or label.replace(" ", "") in {
                value.replace(" ", "") for value in GENERIC_NAVIGATION_LABELS
            }:
                related_system_links.append(
                    {
                        "type": "관련 시스템",
                        "title": label,
                        "url": url,
                        "status": "공통 내비게이션 링크",
                    }
                )
            else:
                excluded += 1
            continue

        if kind and is_case_specific_document(label, url, bill_id, bill_no):
            documents.append(
                {
                    "type": kind,
                    "title": label,
                    "url": url,
                    "status": "법안별 공식문서 위치 발견",
                    "content_status": "내용 추출·조문 대조 전",
                    "case_specific": True,
                }
            )
        elif kind:
            related_system_links.append(
                {
                    "type": "관련 시스템",
                    "title": label,
                    "url": url,
                    "status": "법안별 문서 여부 미확정",
                }
            )
        else:
            excluded += 1

    file_pattern = re.compile(
        r'(?P<url>(?:https?:)?//[^"\'<> ]+?\.(?:pdf|hwp|hwpx|docx?|xlsx?|zip)'
        r'(?:\?[^"\'<> ]*)?|/[^"\'<> ]+?\.(?:pdf|hwp|hwpx|docx?|xlsx?|zip)'
        r'(?:\?[^"\'<> ]*)?)',
        re.IGNORECASE,
    )
    for match in file_pattern.finditer(response.text):
        url = normalize_url(detail_url, match.group("url"))
        if not url or url in seen:
            continue
        seen.add(url)
        documents.append(
            {
                "type": classify_document("", url) or "첨부자료",
                "title": Path(urlparse(url).path).name or "첨부자료",
                "url": url,
                "status": "법안 상세페이지 HTML에서 파일 위치 발견",
                "content_status": "내용 추출·조문 대조 전",
                "case_specific": True,
            }
        )

    return {
        "status": "상세페이지 연결 완료",
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "documents": documents[:40],
        "related_system_links": related_system_links[:20],
        "excluded_links": excluded,
        "error": "",
    }


CLAUSE_PATTERN = re.compile(
    r"(?P<draft>안\s*)?"
    r"제\s*(?P<article>\d+)\s*조"
    r"(?:의\s*(?P<subarticle>\d+))?"
    r"(?:\s*제\s*(?P<paragraph>\d+)\s*항)?"
    r"(?:\s*제\s*(?P<item>\d+)\s*호)?"
)


def canonical_clause(match: re.Match[str]) -> tuple[str, str]:
    prefix = "안 " if match.group("draft") else ""
    article = f"제{match.group('article')}조"
    if match.group("subarticle"):
        article += f"의{match.group('subarticle')}"
    if match.group("paragraph"):
        article += f"제{match.group('paragraph')}항"
    if match.group("item"):
        article += f"제{match.group('item')}호"
    key = article
    return key, prefix + article


def extract_clause_references(text: str) -> list[str]:
    selected: dict[str, str] = {}
    order: list[str] = []

    for match in CLAUSE_PATTERN.finditer(text or ""):
        key, label = canonical_clause(match)
        if key not in selected:
            selected[key] = label
            order.append(key)
        elif label.startswith("안 "):
            selected[key] = label

    if re.search(r"안\s*별표", text or ""):
        order.append("안 별표")
        selected["안 별표"] = "안 별표"
    if re.search(r"안\s*부칙", text or ""):
        order.append("안 부칙")
        selected["안 부칙"] = "안 부칙"

    return [selected[key] for key in order][:20]


def has_quantitative_signal(text: str) -> bool:
    return bool(
        re.search(
            r"\d+(?:[.,]\d+)?\s*(?:%|퍼센트|명|건|원|억원|조원|배|개|년|개월)",
            text,
            re.IGNORECASE,
        )
    )


def classify_claim_types(text: str) -> tuple[str, list[str]]:
    normalized = tidy(text).lower()
    matched: list[str] = []

    if has_quantitative_signal(normalized):
        matched.append("수치·규모 주장")

    for claim_type, signals in CLAIM_TYPE_RULES:
        if any(signal.lower() in normalized for signal in signals):
            matched.append(claim_type)

    matched = unique(matched, 6)
    if not matched:
        matched = ["문제 진단"]

    priority = (
        "법적·헌법적 주장",
        "국제·제도 비교 주장",
        "재정·비용 주장",
        "기본권·권리 영향 주장",
        "수치·규모 주장",
        "인과·원인 주장",
        "집행·행정역량 주장",
        "효과·편익 주장",
        "규범·당위 주장",
        "문제 진단",
    )
    primary = next((item for item in priority if item in matched), matched[0])
    secondary = [item for item in matched if item != primary]
    return primary, secondary


def claim_evidence_standard(primary_type: str) -> tuple[str, str]:
    return CLAIM_EVIDENCE_STANDARDS.get(
        primary_type,
        CLAIM_EVIDENCE_STANDARDS["문제 진단"],
    )


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
    reasons.append("최신 100건 창에서 권력·기본권·재정·집행 위험을 대표하도록 선정")
    return reasons


def ranked_candidates(bills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        bills,
        key=lambda bill: (
            priority_score(bill),
            str(bill.get("proposed_date", "")),
        ),
        reverse=True,
    )


def select_pilot_bills(
    bills: list[dict[str, Any]],
    limit: int = 10,
    locked_ids: list[str] | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    ranked = ranked_candidates(bills)
    by_id = {
        str(bill.get("id", "")): bill
        for bill in bills
        if bill.get("id")
    }
    locked_ids = unique(list(locked_ids or []), limit)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    missing_locked_ids: list[str] = []

    for bill_id in locked_ids:
        bill = by_id.get(bill_id)
        if bill:
            selected.append(bill)
            selected_ids.add(bill_id)
        else:
            missing_locked_ids.append(bill_id)

    covered_profiles = {
        profile.get("key", "")
        for bill in selected
        for profile in (bill.get("deep_review") or {}).get("profiles") or []
    }
    covered_categories = {
        str(bill.get("category", ""))
        for bill in selected
    }

    if not selected:
        for bill in ranked:
            context = f"{bill.get('title', '')} {bill.get('official_summary', '')}"
            if "교육감" in context:
                selected.append(bill)
                selected_ids.add(str(bill.get("id", "")))
                covered_categories.add(str(bill.get("category", "")))
                covered_profiles.update(
                    profile.get("key", "")
                    for profile in (
                        (bill.get("deep_review") or {}).get("profiles") or []
                    )
                )
                break

    for profile_key, _ in TARGET_PROFILES:
        if len(selected) >= limit:
            break
        if profile_key in covered_profiles:
            continue
        candidate = next(
            (
                bill
                for bill in ranked
                if str(bill.get("id", "")) not in selected_ids
                and any(
                    profile.get("key") == profile_key
                    for profile in (
                        (bill.get("deep_review") or {}).get("profiles") or []
                    )
                )
            ),
            None,
        )
        if not candidate:
            continue
        selected.append(candidate)
        selected_ids.add(str(candidate.get("id", "")))
        covered_categories.add(str(candidate.get("category", "")))
        covered_profiles.update(
            profile.get("key", "")
            for profile in (
                (candidate.get("deep_review") or {}).get("profiles") or []
            )
        )

    while len(selected) < limit:
        candidates = [
            bill
            for bill in ranked
            if str(bill.get("id", "")) not in selected_ids
        ]
        if not candidates:
            break
        candidates.sort(
            key=lambda bill: (
                str(bill.get("category", "")) not in covered_categories,
                priority_score(bill),
                str(bill.get("proposed_date", "")),
            ),
            reverse=True,
        )
        candidate = candidates[0]
        selected.append(candidate)
        selected_ids.add(str(candidate.get("id", "")))
        covered_categories.add(str(candidate.get("category", "")))

    selection_mode = (
        "고정 유지"
        if locked_ids and not missing_locked_ids and len(selected) == limit
        else "고정대상 일부 대체"
        if locked_ids
        else "최초 자동선정"
    )

    return selected[:limit], {
        "selection_mode": selection_mode,
        "locked_input_count": len(locked_ids),
        "missing_locked_ids": missing_locked_ids,
        "replacement_count": len(missing_locked_ids),
        "selection_version": SELECTION_VERSION,
    }


def build_claim_audit(bill: dict[str, Any]) -> list[dict[str, Any]]:
    claims = (bill.get("deep_review") or {}).get("claim_ledger") or []
    result: list[dict[str, Any]] = []

    for claim in claims[:10]:
        claim_text = tidy(claim.get("claim"))
        primary_type, secondary_types = classify_claim_types(claim_text)
        required_evidence, rejection_condition = claim_evidence_standard(
            primary_type
        )
        result.append(
            {
                "id": claim.get("id", ""),
                "claim": claim_text,
                "original_claim_type": claim.get("claim_type", ""),
                "primary_claim_type": primary_type,
                "secondary_claim_types": secondary_types,
                "current_status": "발의자 제안이유에 기재된 주장",
                "official_basis": "국회 공식 제안이유·주요내용",
                "required_evidence": required_evidence,
                "rejection_condition": rejection_condition,
                "independent_evidence": [],
                "verdict": "독립 검증 전",
            }
        )

    return result


def calculate_maturity(
    bill: dict[str, Any],
    page_audit: dict[str, Any],
    claim_audit: list[dict[str, Any]],
) -> dict[str, Any]:
    deep = bill.get("deep_review") or {}
    documents = page_audit.get("documents") or []
    found_types = {
        document.get("type", "")
        for document in documents
        if document.get("case_specific")
    }

    structure = 0
    structure += 20 if bill.get("official_summary") else 0
    structure += 20 if deep.get("profiles") else 0
    structure += 20 if claim_audit else 0
    structure += 20 if deep.get("deep_red_team") else 0
    structure += 20 if bill.get("fork_candidates") else 0

    official_sources = 0
    official_sources += (
        10 if page_audit.get("status") == "상세페이지 연결 완료" else 0
    )
    official_sources += 10 if extract_clause_references(
        str(bill.get("official_summary", ""))
    ) else 0
    official_sources += 25 if "법률안 조문 전문" in found_types else 0
    official_sources += 20 if "신·구조문 대비표" in found_types else 0
    official_sources += 15 if "비용추계서·미첨부사유" in found_types else 0
    official_sources += 15 if "위원회 검토·심사보고서" in found_types else 0
    official_sources += 5 if "회의록·토론자료" in found_types else 0

    verified_claims = [
        claim
        for claim in claim_audit
        if claim.get("verdict") not in ("", "독립 검증 전")
        and claim.get("independent_evidence")
    ]
    evidence = (
        round(100 * len(verified_claims) / len(claim_audit))
        if claim_audit
        else 0
    )

    review_state = bill.get("analysis_review_state", "")
    human_review = 100 if "완료" in review_state else 0
    expert_review = 100 if "전문가 검토 완료" in review_state else 0

    scores = {
        "structure_readiness": clamp_score(structure),
        "official_source_coverage": clamp_score(official_sources),
        "independent_evidence_validation": clamp_score(evidence),
        "human_review": clamp_score(human_review),
        "expert_review": clamp_score(expert_review),
    }

    if scores["expert_review"] >= 100:
        stage, stage_label = "S5", "전문가 검증 완료"
    elif scores["human_review"] >= 100:
        stage, stage_label = "S4", "운영자 검토 완료"
    elif scores["independent_evidence_validation"] >= 40:
        stage, stage_label = "S3", "독립근거 검증 중"
    elif scores["official_source_coverage"] >= 40:
        stage, stage_label = "S2", "공식문서 대조 중"
    else:
        stage, stage_label = "S1", "구조화·자료확보 준비"

    return {
        **scores,
        "stage": stage,
        "stage_label": stage_label,
        "interpretation": (
            "각 점수는 서로 다른 성숙도를 나타냅니다. 구조화 점수가 높더라도 "
            "공식문서·독립근거·사람검토 점수가 낮으면 검증 완료가 아닙니다."
        ),
    }


def source_status(found_types: set[str], kind: str, fallback: str) -> str:
    return "법안별 문서 위치 발견" if kind in found_types else fallback


def build_case(
    bill: dict[str, Any],
    rank: int,
    page_audit: dict[str, Any],
    generated_at: str,
    selection_mode: str,
) -> dict[str, Any]:
    deep = bill.get("deep_review") or {}
    official_summary = str(bill.get("official_summary", ""))
    clause_refs = extract_clause_references(official_summary)
    documents = page_audit.get("documents") or []
    found_types = {
        document.get("type", "")
        for document in documents
        if document.get("case_specific")
    }
    claim_audit = build_claim_audit(bill)
    maturity = calculate_maturity(bill, page_audit, claim_audit)

    source_matrix = [
        {
            "source": "국회 제안이유 및 주요내용",
            "status": "연결 완료" if official_summary else "미연결",
            "use": "문제진단·목표·변경방향·조문 참조",
        },
        {
            "source": "국회 의안 상세페이지",
            "status": page_audit.get("status", "확인 필요"),
            "use": "진행상태와 법안별 공식 첨부자료 위치 확인",
        },
        {
            "source": "법률안 조문 전문",
            "status": source_status(
                found_types,
                "법률안 조문 전문",
                "위치 확인 필요",
            ),
            "use": "실제 권리·의무·예외·위임·제재 대조",
        },
        {
            "source": "신·구조문 대비표",
            "status": source_status(
                found_types,
                "신·구조문 대비표",
                "위치 확인 필요",
            ),
            "use": "현행법과 개정안 문장 단위 비교",
        },
        {
            "source": "비용추계서·미첨부사유",
            "status": source_status(
                found_types,
                "비용추계서·미첨부사유",
                "위치 확인 필요",
            ),
            "use": "재정·인력·민간 준수비용 검증",
        },
        {
            "source": "위원회 검토·심사보고서",
            "status": source_status(
                found_types,
                "위원회 검토·심사보고서",
                "심사 진행 후 확인",
            ),
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
            chain
            for chain in deep.get("evidence_chains") or []
            if fork.get("label") in (chain.get("fork_links") or [])
        ]
        fork_designs.append(
            {
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
                    [chain.get("attack", "") for chain in linked],
                    8,
                ),
                "residual_risks": unique(
                    [chain.get("residual_risk", "") for chain in linked],
                    6,
                ),
            }
        )

    return {
        "rank": rank,
        "bill_id": bill.get("id", ""),
        "bill_no": (bill.get("official") or {}).get("bill_no", ""),
        "title": bill.get("title", ""),
        "category": bill.get("category", ""),
        "committee": bill.get("committee", ""),
        "priority_score": priority_score(bill),
        "selection_reasons": selection_reasons(bill),
        "selection_status": selection_mode,
        "status": "국가 시범검토 10건 · 공식자료 조사 및 검증 대기",
        "maturity": maturity,
        "human_review_status": "운영자 검토 전",
        "expert_review_status": "전문가 검토 전",
        "profiles": deep.get("profiles") or [],
        "official_url": (bill.get("official") or {}).get("source_url", ""),
        "page_audit": page_audit,
        "document_audit": {
            "case_specific_documents": len(documents),
            "related_system_links": len(
                page_audit.get("related_system_links") or []
            ),
            "excluded_links": page_audit.get("excluded_links", 0),
            "counting_rule": (
                "파일·첨부식별자·법안번호·법안ID 등 법안별 연결 근거가 있는 "
                "자료만 공식문서 수에 포함"
            ),
        },
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
        "claim_audit": claim_audit,
        "red_team_audit": [
            {
                "profile": attack.get("profile_label", ""),
                "attack": attack.get("title", ""),
                "actor": attack.get("attacker", ""),
                "path": attack.get("path", ""),
                "warning": attack.get("warning", ""),
                "guardrail": attack.get("defense", ""),
                "residual": attack.get("residual", ""),
                "evidence_status": "조문·외부자료 대조 전",
            }
            for attack in (deep.get("deep_red_team") or [])[:12]
        ],
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
        "review_log": [
            {
                "at": generated_at,
                "actor": "자동 조사 모듈",
                "action": "공식자료 위치·주장유형·성숙도 재평가",
                "result": maturity["stage_label"],
                "methodology_version": METHODOLOGY_VERSION,
            }
        ],
        "remaining_uncertainty": [
            "법안별 첨부자료 위치 발견은 문서 내용 검토 완료를 의미하지 않습니다.",
            "조문·비용·독립 통계 연결 전에는 찬반 결론을 확정하지 않습니다.",
            "자동 포크는 실제 법률문장 확정 전 설계 방향입니다.",
        ],
    }


def validate_pilot_program(program: dict[str, Any]) -> None:
    cases = program.get("cases") or []
    if len(cases) != 10:
        raise ValueError(f"국가 시범검토 대상은 10건이어야 합니다: {len(cases)}")

    ids = [str(case.get("bill_id", "")) for case in cases]
    if len(set(ids)) != len(ids):
        raise ValueError("국가 시범검토 대상에 중복 법안 ID가 있습니다.")

    for case in cases:
        refs = (case.get("clause_audit") or {}).get(
            "references_from_summary"
        ) or []
        normalized_refs = [ref.replace("안 ", "") for ref in refs]
        if len(normalized_refs) != len(set(normalized_refs)):
            raise ValueError(
                f"조문 참조 중복: {case.get('bill_id', '')}"
            )

        maturity = case.get("maturity") or {}
        for field in (
            "structure_readiness",
            "official_source_coverage",
            "independent_evidence_validation",
            "human_review",
            "expert_review",
        ):
            score = maturity.get(field)
            if not isinstance(score, int) or not 0 <= score <= 100:
                raise ValueError(
                    f"성숙도 점수 오류 {field}: {case.get('bill_id', '')}"
                )

        for document in (case.get("page_audit") or {}).get("documents") or []:
            if not document.get("case_specific"):
                raise ValueError(
                    f"공식문서 수에 공통 링크 포함: {case.get('bill_id', '')}"
                )


def build_pilot_program(
    bills: list[dict[str, Any]],
    generated_at: str,
    locked_ids: list[str] | None = None,
) -> dict[str, Any]:
    selected, selection_meta = select_pilot_bills(
        bills,
        10,
        locked_ids=locked_ids,
    )
    audits: dict[str, dict[str, Any]] = {}

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = {
            executor.submit(
                explore_official_page,
                (bill.get("official") or {}).get("source_url", ""),
                str(bill.get("id", "")),
                str((bill.get("official") or {}).get("bill_no", "")),
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
                    "related_system_links": [],
                    "excluded_links": 0,
                    "error": str(exc)[:300],
                }

    cases: list[dict[str, Any]] = []
    for rank, bill in enumerate(selected, start=1):
        case = build_case(
            bill,
            rank,
            audits.get(
                str(bill.get("id", "")),
                {
                    "status": "상세페이지 조사 미실행",
                    "documents": [],
                    "related_system_links": [],
                    "excluded_links": 0,
                    "error": "",
                },
            ),
            generated_at,
            selection_meta["selection_mode"],
        )
        bill["pilot_case"] = case
        cases.append(case)

    candidate_pool = [
        {
            "bill_id": bill.get("id", ""),
            "title": bill.get("title", ""),
            "category": bill.get("category", ""),
            "priority_score": priority_score(bill),
        }
        for bill in ranked_candidates(bills)[:20]
    ]

    program = {
        "title": "Democracy 3.0 국가 시범검토 10건",
        "generated_at": generated_at,
        "status": "공식자료 조사 및 조문·근거 검증 진행",
        "purpose": (
            "현재 최신 법안 100건의 자동 검토 창에서 고영향 정책 10건을 고정해, "
            "공식문서·조문·비용·주장·공격경로·대체조문을 실제 증거 기반 "
            "완성보고서로 승격하기 위한 시범운영 묶음입니다."
        ),
        "selection_method": (
            "최초 선정 시 복수 위험프로필, 국민영향, 기본권·권력·재정 신호, "
            "레드팀 공격 수와 분야 다양성을 합산합니다. 확정 후에는 법안이 "
            "수집창에서 사라지지 않는 한 같은 10건을 유지합니다."
        ),
        "manifest": {
            "locked_ids": [
                str(case.get("bill_id", ""))
                for case in cases
            ],
            "locked_count": len(cases),
            "selection_mode": selection_meta["selection_mode"],
            "selection_version": SELECTION_VERSION,
            "replacement_policy": (
                "고정 법안이 최신 수집창에서 사라진 경우에만 자동 후보로 대체하고 "
                "missing_locked_ids와 replacement_count를 공개"
            ),
            "missing_locked_ids": selection_meta["missing_locked_ids"],
            "replacement_count": selection_meta["replacement_count"],
        },
        "candidate_pool": candidate_pool,
        "cases": cases,
        "summary": {
            "selected": len(cases),
            "locked": len(cases) - selection_meta["replacement_count"],
            "replacements": selection_meta["replacement_count"],
            "detail_pages_connected": sum(
                1
                for case in cases
                if (case.get("page_audit") or {}).get("status")
                == "상세페이지 연결 완료"
            ),
            "case_specific_documents": sum(
                len((case.get("page_audit") or {}).get("documents") or [])
                for case in cases
            ),
            "related_system_links": sum(
                len(
                    (case.get("page_audit") or {}).get(
                        "related_system_links"
                    )
                    or []
                )
                for case in cases
            ),
            "claims_to_verify": sum(
                len(case.get("claim_audit") or [])
                for case in cases
            ),
            "verified_claims": sum(
                1
                for case in cases
                for claim in case.get("claim_audit") or []
                if claim.get("verdict") not in ("", "독립 검증 전")
                and claim.get("independent_evidence")
            ),
            "forks_to_draft": sum(
                len(case.get("fork_designs") or [])
                for case in cases
            ),
            "human_reviewed": sum(
                1
                for case in cases
                if case.get("human_review_status") == "운영자 검토 완료"
            ),
            "expert_reviewed": sum(
                1
                for case in cases
                if case.get("expert_review_status") == "전문가 검토 완료"
            ),
        },
    }

    validate_pilot_program(program)
    return program
