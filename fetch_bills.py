"""Democracy 3.0 정책포크 v0.5

국회 최신 의안, 공식 제안이유·주요내용, 상세 진행정보를 수집하고
공식 원문에서 확인되는 단서만으로 규칙 기반 구조화 초안을 생성합니다.

주의:
- 구조화 초안은 AI 판정이나 법률 자문이 아닙니다.
- 법안 조문, 비용추계서, 검토보고서, 회의록을 아직 모두 읽은 분석이 아닙니다.
- 공식자료와 자동 추론을 데이터 구조에서 분리합니다.
"""

from __future__ import annotations

import html
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

API_KEY_ENV = "ASSEMBLY_API_KEY"

LIST_API_KEY = "nzmimeepazxkubdpn"
LIST_API_URL = f"https://open.assembly.go.kr/portal/openapi/{LIST_API_KEY}"

SUMMARY_API_KEY = "BPMBILLSUMMARY"
SUMMARY_API_URL = f"https://open.assembly.go.kr/portal/openapi/{SUMMARY_API_KEY}"

DETAIL_API_KEY = "BILLINFODETAIL"
DETAIL_API_URL = f"https://open.assembly.go.kr/portal/openapi/{DETAIL_API_KEY}"

OUTPUT = Path(__file__).resolve().parent / "bills.json"
KST = timezone(timedelta(hours=9))
API_WORKERS = 5

UNKNOWN_COMMITTEE_VALUES = {
    "",
    "확인 필요",
    "소관위원회 확인 필요",
    "미정",
    "없음",
    "null",
    "None",
}

ACTOR_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("교육감",), "교육감과 지방교육행정"),
    (("학생", "학습자"), "학생·학습자"),
    (("교원", "교사"), "교원·교사"),
    (("학교", "유치원"), "학교·유치원"),
    (("지방자치단체", "지자체"), "지방자치단체"),
    (("지방의회",), "지방의회"),
    (("공공기관",), "공공기관"),
    (("행정기관", "행정부"), "행정기관"),
    (("사업자", "기업", "회사"), "사업자·기업"),
    (("근로자", "노동자"), "근로자·노동자"),
    (("사용자", "고용주"), "사용자·고용주"),
    (("소비자",), "소비자"),
    (("환자",), "환자"),
    (("의료기관", "병원"), "의료기관"),
    (("장애인",), "장애인"),
    (("노인", "고령자"), "노인·고령자"),
    (("청년",), "청년"),
    (("아동", "어린이"), "아동"),
    (("여성", "임산부"), "여성·임산부"),
    (("농업인", "농민"), "농업인"),
    (("어업인", "어민"), "어업인"),
    (("임차인", "세입자"), "임차인"),
    (("임대인",), "임대인"),
    (("납세자",), "납세자"),
    (("개인정보", "정보주체"), "개인정보 주체"),
    (("국민", "주민", "시민"), "국민·주민"),
)

PROBLEM_WORDS = (
    "그러나", "그런데", "문제", "한계", "미비", "부족", "어렵",
    "우려", "발생", "제외", "취약", "불합리", "혼란", "침해",
    "증가", "지적", "실정", "필요",
)

CHANGE_WORDS = (
    "이에", "개정", "신설", "삭제", "확대", "강화", "완화",
    "의무", "허용", "금지", "임명", "선임", "변경", "개선",
    "보장", "지원", "하도록", "하고자 함", "하고자",
)

RIGHTS_WORDS = (
    "기본권", "인권", "개인정보", "사생활", "표현의 자유", "평등",
    "차별", "선거", "투표", "정치적 중립", "재산권", "노동권",
)

FINANCE_WORDS = (
    "예산", "재정", "비용", "국고", "보조금", "지원금", "기금",
    "세금", "과세", "부담금", "수수료",
)

IMPLEMENTATION_WORDS = (
    "장관", "위원회", "지방자치단체", "공공기관", "행정기관",
    "지정", "등록", "허가", "신고", "계획", "시행", "수립",
)

SAFEGUARD_WORDS = (
    "감사", "감독", "청문", "이의", "불복", "공개", "보고",
    "벌칙", "과태료", "처벌", "제재", "심사", "동의",
)

METRIC_WORDS = (
    "성과", "평가", "지표", "목표", "실적", "효과 측정", "통계",
)

REVIEW_WORDS = (
    "재검토", "일몰", "유효기간", "한시", "시행 후", "경과조치",
)


def clean(value: Any, fallback: str = "확인 필요") -> str:
    text = str(value or "").strip()
    return text or fallback


def clean_optional(value: Any) -> str:
    return str(value or "").strip()


def clean_official_text(value: Any) -> str:
    """공식 텍스트의 HTML 조각과 과도한 공백을 정리하되 문단을 보존합니다."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def strip_summary_heading(text: str) -> str:
    return re.sub(
        r"^\s*(제안이유\s*및\s*주요내용|제안이유|주요내용)\s*",
        "",
        text,
        count=1,
    ).strip()


def excerpt(value: str, limit: int = 360) -> str:
    one_line = re.sub(r"\s+", " ", value).strip()
    if not one_line:
        return "공식 제안이유와 주요내용을 확인 중입니다."
    return one_line if len(one_line) <= limit else one_line[:limit].rstrip() + "…"


def split_sentences(text: str) -> list[str]:
    """법률안 요약문의 줄바꿈과 문장부호를 이용해 보수적으로 분리합니다."""
    body = strip_summary_heading(clean_official_text(text))
    if not body:
        return []

    chunks: list[str] = []
    for paragraph in re.split(r"\n+", body):
        paragraph = paragraph.strip()
        if not paragraph:
            continue
        parts = re.split(r"(?<=[.!?])\s+|(?<=함)\s+(?=[가-힣A-Z])", paragraph)
        chunks.extend(part.strip() for part in parts if part.strip())

    return chunks


def first_matching(sentences: list[str], words: tuple[str, ...]) -> str:
    for sentence in sentences:
        if any(word in sentence for word in words):
            return sentence
    return ""


def last_matching(sentences: list[str], words: tuple[str, ...]) -> str:
    for sentence in reversed(sentences):
        if any(word in sentence for word in words):
            return sentence
    return ""


def contains_any(text: str, words: tuple[str, ...]) -> bool:
    return any(word in text for word in words)


def category_from_committee(name: str) -> str:
    rules = [
        (("기획재정", "정무", "산업통상", "중소벤처"), "경제·산업"),
        (("보건복지",), "복지·보건"),
        (("교육",), "교육"),
        (("국토교통",), "국토·교통"),
        (("환경노동",), "환경·노동"),
        (("과학기술", "방송통신"), "과학·디지털"),
        (("농림축산", "해양수산"), "농림·해양"),
        (("법제사법",), "법·사법"),
        (("행정안전",), "행정·안전"),
        (("외교통일", "국방"), "외교·안보"),
        (("문화체육", "여성가족"), "문화·사회"),
    ]
    for words, category in rules:
        if any(word in name for word in words):
            return category
    return "기타"


def normalize_url(value: Any) -> str:
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith("http://") or url.startswith("https://"):
        return url
    if url.startswith("/"):
        return f"https://open.assembly.go.kr{url}"
    return url


def parse_rows(payload: Any, root_key: str) -> tuple[int, list[dict[str, Any]]]:
    """열린국회정보의 공통 head/row 구조를 해석합니다."""
    if isinstance(payload, dict) and isinstance(payload.get("RESULT"), dict):
        result = payload["RESULT"]
        code = str(result.get("CODE") or "")
        if code in {"INFO-200", "DATA-000"}:
            return 0, []
        raise RuntimeError(f"국회 API 오류 {code}: {result.get('MESSAGE', '')}")

    data = payload.get(root_key) if isinstance(payload, dict) else None
    if not isinstance(data, list) or len(data) < 2:
        raise RuntimeError(
            f"{root_key}의 예상하지 못한 응답입니다: {str(payload)[:500]}"
        )

    head = data[0].get("head") or []
    total_count = 0
    result: dict[str, Any] = {}

    for item in head:
        if not isinstance(item, dict):
            continue
        if "list_total_count" in item:
            total_count = int(item.get("list_total_count") or 0)
        if isinstance(item.get("RESULT"), dict):
            result = item["RESULT"]

    code = str(result.get("CODE") or "")
    if code and code != "INFO-000":
        if code in {"INFO-200", "DATA-000"}:
            return total_count, []
        raise RuntimeError(f"국회 API 오류 {code}: {result.get('MESSAGE', '')}")

    rows = data[1].get("row") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        rows = []

    return total_count, [row for row in rows if isinstance(row, dict)]


def request_json(
    url: str,
    params: dict[str, Any],
    *,
    attempts: int = 3,
    timeout: int = 60,
) -> Any:
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            response = requests.get(
                url,
                params=params,
                headers={
                    "Accept": "application/json",
                    "User-Agent": "Democracy3-Policy-Fork/0.5",
                },
                timeout=timeout,
            )
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, requests.JSONDecodeError) as exc:
            last_error = exc
            if attempt < attempts:
                time.sleep(attempt * 1.5)

    raise RuntimeError(f"국회 API 연결 실패: {last_error}") from last_error


def load_summary_cache() -> dict[str, str]:
    """이미 수집한 공식 제안이유·주요내용은 다시 호출하지 않습니다."""
    if not OUTPUT.exists():
        return {}

    try:
        old = json.loads(OUTPUT.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}

    cache: dict[str, str] = {}
    for bill in old.get("bills") or []:
        if not isinstance(bill, dict):
            continue
        official = bill.get("official") or {}
        bill_no = clean_optional(official.get("bill_no"))
        summary = clean_official_text(bill.get("official_summary"))
        if bill_no and summary:
            cache[bill_no] = summary
    return cache


def fetch_official_summary(api_key: str, bill_no: str) -> str:
    payload = request_json(
        SUMMARY_API_URL,
        {
            "KEY": api_key,
            "Type": "json",
            "pIndex": 1,
            "pSize": 1,
            "BILL_NO": bill_no,
        },
    )
    _, rows = parse_rows(payload, SUMMARY_API_KEY)
    if not rows:
        return ""
    return clean_official_text(rows[0].get("SUMMARY"))


def fetch_official_detail(api_key: str, bill_id: str) -> dict[str, Any]:
    payload = request_json(
        DETAIL_API_URL,
        {
            "KEY": api_key,
            "Type": "json",
            "pIndex": 1,
            "pSize": 1,
            "BILL_ID": bill_id,
        },
    )
    _, rows = parse_rows(payload, DETAIL_API_KEY)
    return rows[0] if rows else {}


def collect_by_key(
    keys: list[str],
    worker,
) -> tuple[dict[str, Any], list[str]]:
    results: dict[str, Any] = {}
    errors: list[str] = []

    if not keys:
        return results, errors

    with ThreadPoolExecutor(max_workers=API_WORKERS) as executor:
        jobs = {executor.submit(worker, key): key for key in keys}
        for future in as_completed(jobs):
            key = jobs[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                errors.append(f"{key}: {exc}")

    return results, errors


def collect_summaries(
    api_key: str,
    bill_numbers: list[str],
    cache: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    summaries = dict(cache)
    missing = [number for number in bill_numbers if number and not summaries.get(number)]

    fetched, errors = collect_by_key(
        missing,
        lambda bill_no: fetch_official_summary(api_key, bill_no),
    )
    summaries.update({key: str(value or "") for key, value in fetched.items()})
    return summaries, errors


def collect_details(
    api_key: str,
    bill_ids: list[str],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    fetched, errors = collect_by_key(
        bill_ids,
        lambda bill_id: fetch_official_detail(api_key, bill_id),
    )
    details = {
        key: value
        for key, value in fetched.items()
        if isinstance(value, dict)
    }
    return details, errors


def valid_committee(*values: Any) -> str:
    for value in values:
        text = clean_optional(value)
        if text not in UNKNOWN_COMMITTEE_VALUES:
            return text
    return "소관위원회 미정 또는 확인 필요"


def derive_stage(detail: dict[str, Any], row: dict[str, Any]) -> str:
    result = clean_optional(
        detail.get("PROC_RESULT_CD")
        or detail.get("PROC_RESULT")
        or row.get("PROC_RESULT")
    )
    if result:
        return f"처리결과: {result}"

    if clean_optional(detail.get("RGS_PROC_DT") or detail.get("PROC_DT")):
        return "본회의 처리 단계"
    if clean_optional(detail.get("LAW_PROC_DT")):
        return "법제사법위원회 처리"
    if clean_optional(detail.get("LAW_PRESENT_DT") or detail.get("LAW_SUBMIT_DT")):
        return "법제사법위원회 심사"
    if clean_optional(detail.get("CMT_PROC_DT")):
        return "소관위원회 처리"
    if clean_optional(detail.get("CMT_PRESENT_DT")):
        return "소관위원회 심사"
    if valid_committee(
        detail.get("CURR_COMMITTEE"),
        detail.get("COMMITTEE"),
        row.get("COMMITTEE"),
    ) != "소관위원회 미정 또는 확인 필요":
        return "소관위원회 회부·심사 대기"
    return "의안 접수"


def build_progress_timeline(
    proposed_date: str,
    detail: dict[str, Any],
    row: dict[str, Any],
) -> list[dict[str, str]]:
    candidates = [
        ("제안", proposed_date),
        ("소관위원회 상정", detail.get("CMT_PRESENT_DT") or row.get("CMT_PRESENT_DT")),
        ("소관위원회 처리", detail.get("CMT_PROC_DT") or row.get("CMT_PROC_DT")),
        ("법사위 회부", detail.get("LAW_SUBMIT_DT") or row.get("LAW_SUBMIT_DT")),
        ("법사위 상정", detail.get("LAW_PRESENT_DT") or row.get("LAW_PRESENT_DT")),
        ("법사위 처리", detail.get("LAW_PROC_DT") or row.get("LAW_PROC_DT")),
        ("본회의 처리", detail.get("RGS_PROC_DT") or detail.get("PROC_DT") or row.get("PROC_DT")),
    ]

    timeline = [
        {"label": label, "date": clean_optional(date)}
        for label, date in candidates
        if clean_optional(date)
    ]

    result = clean_optional(
        detail.get("PROC_RESULT_CD")
        or detail.get("PROC_RESULT")
        or row.get("PROC_RESULT")
    )
    if result:
        timeline.append({"label": "최종 처리결과", "date": result})

    return timeline


def extract_actors(text: str) -> list[str]:
    found: list[str] = []
    for keywords, label in ACTOR_RULES:
        if any(keyword in text for keyword in keywords) and label not in found:
            found.append(label)
    return found[:6]


def build_plain_language(
    title: str,
    sentences: list[str],
    problem_sentence: str,
    change_sentence: str,
) -> str:
    if problem_sentence and change_sentence:
        return (
            f"이 법안은 공식 제안이유에서 다음 문제를 제기합니다: "
            f"{problem_sentence} "
            f"이를 해결하기 위해 다음과 같은 제도 변경을 제안합니다: "
            f"{change_sentence}"
        )
    if change_sentence:
        return (
            f"이 법안은 공식 요약상 다음 제도 변경을 추진합니다: "
            f"{change_sentence}"
        )
    if sentences:
        return f"{title}의 공식 요약 핵심은 다음과 같습니다: {sentences[0]}"
    return "공식 제안이유·주요내용이 없어 국민용 구조화 초안을 만들지 못했습니다."


def build_beneficiaries(text: str, actors: list[str]) -> list[str]:
    benefit_terms = ("지원", "보호", "보장", "혜택", "급여", "구제", "완화")
    if contains_any(text, benefit_terms) and actors:
        return [
            "잠재적 수혜 후보(자동 추출): " + ", ".join(actors),
            "실제 수혜 범위는 법안 조문과 적용요건을 확인해야 확정할 수 있음",
        ]
    return [
        "공식 제안이유만으로 직접 수혜자를 확정하기 어려움",
        "법안 조문에서 적용대상·예외·자격요건을 추가 확인해야 함",
    ]


def build_cost_bearers(text: str, actors: list[str]) -> list[str]:
    burden_terms = (
        "의무", "수립", "시행", "설치", "제출", "보고", "부담", "비용",
        "등록", "신고", "감독", "관리", "지원",
    )
    if contains_any(text, burden_terms) and actors:
        return [
            "행정·재정 부담 가능 주체(자동 추정): " + ", ".join(actors),
            "비용 규모와 부담 귀속은 비용추계서·하위법령 확인 전에는 확정할 수 없음",
        ]
    return [
        "비용 부담 주체가 공식 요약에 명확히 드러나지 않음",
        "국가·지자체·공공기관·민간사업자 중 누가 집행비용을 부담하는지 확인 필요",
    ]


def build_authority_changes(text: str) -> list[str]:
    changes: list[str] = []

    if contains_any(text, ("임명", "선임", "선출", "추천", "동의")):
        changes.append("선출·임명·동의 권한의 주체와 견제 절차 변경 가능성")
    if contains_any(text, ("의무", "하도록", "수립", "시행", "제출", "보고")):
        changes.append("기관·사업자·개인에게 새로운 의무를 부과하거나 기존 의무를 확대할 가능성")
    if contains_any(text, ("허가", "등록", "신고", "지정", "승인")):
        changes.append("행정기관의 허가·등록·지정·승인 권한 범위 변경 가능성")
    if contains_any(text, ("벌칙", "과태료", "처벌", "제재")):
        changes.append("제재 권한과 법적 책임 범위 변경 가능성")
    if contains_any(text, ("개인정보", "정보시스템", "보안", "정보통신망")):
        changes.append("정보 관리·보안·감독 책임의 범위 변경 가능성")

    return changes[:4] or [
        "공식 요약만으로 구체적인 권한 이동을 확정하기 어려움",
        "개정 조문에서 누가 새 권한을 얻고 누가 통제를 받는지 확인 필요",
    ]


def build_strengths(text: str, change_sentence: str) -> list[str]:
    strengths: list[str] = []

    if re.search(r"\d[\d,.]*\s*(건|명|개|%|퍼센트|억원|조원|년|개월)", text):
        strengths.append("공식 제안이유에 수치·사례가 제시되어 문제 인식의 근거를 확인할 단서가 있음")
    if change_sentence:
        strengths.append("변경하려는 제도 방향이 공식 요약에 제시되어 있음")
    if re.search(r"\(안\s*제?\d+조", text):
        strengths.append("관련 개정 조문이 공식 요약에 표시되어 원문 대조가 가능함")

    return strengths[:3] or [
        "공식 제안이유와 주요내용이 공개되어 기본적인 검토 출발점은 확보됨"
    ]


def build_risks(text: str) -> list[str]:
    risks: list[str] = []

    if contains_any(text, ("임명", "선임", "선출", "동의")):
        risks.append("선출·임명 방식 변경이 대표성·독립성·권력집중에 미치는 영향 검토 필요")
    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        risks.append("새 의무의 집행비용, 담당기관 역량, 위반 시 책임기준 검토 필요")
    if contains_any(text, ("개인정보", "정보", "보안", "감시")):
        risks.append("개인정보 보호, 정보 접근권한, 사고 발생 시 책임 배분 검토 필요")
    if contains_any(text, ("벌칙", "과태료", "처벌", "제재")):
        risks.append("규제 목적과 제재수준의 비례성 및 과잉제재 가능성 검토 필요")
    if contains_any(text, ("지원", "보조금", "급여", "재정", "예산")):
        risks.append("지원대상 선정기준, 재정 지속가능성, 사각지대와 도덕적 해이 검토 필요")
    if contains_any(text, ("위원회", "기관", "센터", "기구")):
        risks.append("기존 조직과의 기능 중복, 책임소재 분산, 추가 행정비용 검토 필요")

    return risks[:4] or [
        "공식 요약에는 비용·집행·부작용 정보가 제한적이므로 조문과 검토보고서 확인 필요"
    ]


def build_questions(text: str, committee: str) -> list[str]:
    questions = [
        "법안이 제시한 문제의 규모와 원인이 독립된 통계·연구로 확인되는가?",
        "적용대상, 예외, 권리구제 절차가 조문에 구체적으로 규정되어 있는가?",
    ]

    if contains_any(text, FINANCE_WORDS) or contains_any(text, ("지원", "설치", "운영")):
        questions.append("재정 소요와 비용 부담 주체가 비용추계서에 제시되어 있는가?")
    else:
        questions.append("새 제도를 운영하는 데 드는 재정·인력 비용은 누구에게 귀속되는가?")

    if contains_any(text, RIGHTS_WORDS):
        questions.append("기본권 제한과 공익 사이의 비례성, 독립적 통제장치가 충분한가?")
    elif contains_any(text, ("의무", "규제", "금지", "제재")):
        questions.append("의무·규제의 범위가 과도하지 않고 예외와 이의제기 절차가 있는가?")
    else:
        questions.append("예상하지 못한 부작용을 발견하고 수정할 재검토 장치가 있는가?")

    if committee and committee != "소관위원회 미정 또는 확인 필요":
        questions.append(f"{committee} 심사에서 이해관계자와 반대 근거가 충분히 청취되는가?")

    return questions[:5]


def signal_level(text: str, words: tuple[str, ...], *, numeric_boost: bool = False) -> int:
    count = sum(1 for word in words if word in text)
    score = min(3, count)
    if numeric_boost and re.search(r"\d[\d,.]*", text):
        score += 1
    return min(4, score)


def build_source_signals(text: str, problem_sentence: str) -> dict[str, int]:
    return {
        "문제정의": min(4, (3 if problem_sentence else 1) + (1 if re.search(r"\d", text) else 0)),
        "근거": signal_level(
            text,
            ("통계", "조사", "연구", "사례", "결과", "자료", "보고서"),
            numeric_boost=True,
        ),
        "재정": signal_level(text, FINANCE_WORDS),
        "집행": signal_level(text, IMPLEMENTATION_WORDS),
        "기본권": signal_level(text, RIGHTS_WORDS),
        "통제장치": signal_level(text, SAFEGUARD_WORDS),
        "성과측정": signal_level(text, METRIC_WORDS),
        "재검토": signal_level(text, REVIEW_WORDS),
    }


def build_structured_analysis(
    title: str,
    official_summary: str,
    committee: str,
    generated_at: str,
) -> dict[str, Any]:
    text = strip_summary_heading(clean_official_text(official_summary))
    sentences = split_sentences(text)
    problem_sentence = first_matching(sentences, PROBLEM_WORDS)
    change_sentence = last_matching(sentences, CHANGE_WORDS)
    actors = extract_actors(text)

    confidence_points = 0
    if len(text) >= 250:
        confidence_points += 1
    if problem_sentence:
        confidence_points += 1
    if change_sentence:
        confidence_points += 1
    if re.search(r"\(안\s*제?\d+조", text):
        confidence_points += 1

    confidence = (
        "보통" if confidence_points >= 3
        else "낮음" if confidence_points >= 1
        else "생성 불가"
    )

    if not text:
        return {
            "analysis_status": "미분석",
            "analysis_method": "공식 원문 없음",
            "analysis_confidence": "생성 불가",
            "analysis_review_state": "사람 검토 필요",
            "analysis_generated_at": generated_at,
            "plain_language": "공식 제안이유·주요내용이 없어 구조화 초안을 만들지 못했습니다.",
            "problem_definition": "공식 원문 없음",
            "proposed_change": "공식 원문 없음",
            "affected_groups": [],
            "beneficiaries": ["공식 원문 없음"],
            "cost_bearers": ["공식 원문 없음"],
            "authority_changes": ["공식 원문 없음"],
            "strengths": ["공식 원문 없음"],
            "risks": ["공식 원문 없음"],
            "questions": ["국회 원문과 부속자료를 직접 확인해야 함"],
            "scores": {key: 0 for key in (
                "문제정의", "근거", "재정", "집행",
                "기본권", "통제장치", "성과측정", "재검토",
            )},
            "analysis_basis": (
                "공식 제안이유·주요내용을 받지 못했습니다. "
                "어떠한 정책 판단도 생성하지 않았습니다."
            ),
        }

    return {
        "analysis_status": "구조화 초안",
        "analysis_method": "공식 원문 규칙 기반 구조화 v0.1",
        "analysis_confidence": confidence,
        "analysis_review_state": "사람 검토 전",
        "analysis_generated_at": generated_at,
        "plain_language": build_plain_language(
            title,
            sentences,
            problem_sentence,
            change_sentence,
        ),
        "problem_definition": problem_sentence or (
            sentences[0] if sentences else "공식 요약에서 문제정의를 분리하지 못함"
        ),
        "proposed_change": change_sentence or (
            sentences[-1] if sentences else "공식 요약에서 변경방향을 분리하지 못함"
        ),
        "affected_groups": actors or ["공식 요약에서 직접 적용 주체를 자동 추출하지 못함"],
        "beneficiaries": build_beneficiaries(text, actors),
        "cost_bearers": build_cost_bearers(text, actors),
        "authority_changes": build_authority_changes(text),
        "strengths": build_strengths(text, change_sentence),
        "risks": build_risks(text),
        "questions": build_questions(text, committee),
        "scores": build_source_signals(text, problem_sentence),
        "analysis_basis": (
            "대한민국 국회가 공개한 ‘제안이유 및 주요내용’만 사용한 자동 구조화입니다. "
            "법안 조문 전문, 비용추계서, 위원회 검토보고서, 회의록, 외부 통계는 아직 반영하지 않았습니다. "
            "따라서 수혜자·부담자·위험 항목은 확정판이 아니라 검토 후보입니다."
        ),
    }


def main() -> None:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV}가 설정되지 않았습니다.")

    age = os.environ.get("ASSEMBLY_AGE", "22").strip()

    list_payload = request_json(
        LIST_API_URL,
        {
            "KEY": api_key,
            "Type": "json",
            "pIndex": 1,
            "pSize": 100,
            "AGE": age,
        },
    )
    total_count, rows = parse_rows(list_payload, LIST_API_KEY)
    rows.sort(key=lambda row: clean_optional(row.get("PROPOSE_DT")), reverse=True)

    bill_numbers = [
        clean_optional(row.get("BILL_NO"))
        for row in rows
        if clean_optional(row.get("BILL_NO"))
    ]
    bill_ids = [
        clean_optional(row.get("BILL_ID"))
        for row in rows
        if clean_optional(row.get("BILL_ID"))
    ]

    summaries, summary_errors = collect_summaries(
        api_key,
        bill_numbers,
        load_summary_cache(),
    )
    details, detail_errors = collect_details(api_key, bill_ids)

    now = datetime.now(KST)
    generated_at = now.isoformat(timespec="seconds")
    today = now.strftime("%Y-%m-%d")
    bills: list[dict[str, Any]] = []

    for row in rows:
        bill_id = clean_optional(row.get("BILL_ID"))
        bill_no = clean_optional(row.get("BILL_NO"))
        detail = details.get(bill_id, {})

        title = clean(
            detail.get("BILL_NAME") or row.get("BILL_NAME"),
            "제목 확인 필요",
        )
        committee = valid_committee(
            detail.get("CURR_COMMITTEE"),
            detail.get("COMMITTEE"),
            row.get("COMMITTEE"),
        )
        proposed_date = clean(
            detail.get("PROPOSE_DT") or row.get("PROPOSE_DT"),
            "제안일 확인 필요",
        )
        proposer = clean(
            detail.get("RST_PROPOSER")
            or row.get("RST_PROPOSER")
            or detail.get("PROPOSER")
            or row.get("PROPOSER"),
            "제안자 확인 필요",
        )
        process_result = clean_optional(
            detail.get("PROC_RESULT_CD")
            or detail.get("PROC_RESULT")
            or row.get("PROC_RESULT")
        )
        stage = derive_stage(detail, row)
        detail_link = normalize_url(
            detail.get("DETAIL_LINK")
            or detail.get("LINK_URL")
            or row.get("DETAIL_LINK")
        )
        official_summary = summaries.get(bill_no, "")
        analysis = build_structured_analysis(
            title,
            official_summary,
            committee,
            generated_at,
        )
        source_connected = bool(official_summary)

        bills.append(
            {
                "id": clean(bill_id or bill_no, title),
                "category": category_from_committee(committee),
                "title": title,
                "status": analysis["analysis_status"],
                "stage": stage,
                "committee": committee,
                "proposed_date": proposed_date,
                "updated_at": today,
                "review_count": 0,
                "summary": excerpt(official_summary),
                "official_summary": official_summary,
                "official_summary_status": (
                    "연동 완료" if source_connected else "자료 없음 또는 연동 대기"
                ),
                "analysis_status": analysis["analysis_status"],
                "analysis_method": analysis["analysis_method"],
                "analysis_confidence": analysis["analysis_confidence"],
                "analysis_review_state": analysis["analysis_review_state"],
                "analysis_generated_at": analysis["analysis_generated_at"],
                "analysis_basis": analysis["analysis_basis"],
                "why_now": analysis["plain_language"],
                "problem_definition": analysis["problem_definition"],
                "proposed_change": analysis["proposed_change"],
                "affected_groups": analysis["affected_groups"],
                "beneficiaries": analysis["beneficiaries"],
                "cost_bearers": analysis["cost_bearers"],
                "authority_changes": analysis["authority_changes"],
                "strengths": analysis["strengths"],
                "risks": analysis["risks"],
                "questions": analysis["questions"],
                "scores": analysis["scores"],
                "progress_timeline": build_progress_timeline(
                    proposed_date,
                    detail,
                    row,
                ),
                "forks": [],
                "official": {
                    "age": clean(detail.get("AGE") or row.get("AGE"), age),
                    "bill_no": bill_no,
                    "proposer": clean(
                        detail.get("PROPOSER") or row.get("PROPOSER"),
                        proposer,
                    ),
                    "lead_proposer": proposer,
                    "co_proposers": clean_optional(
                        detail.get("PUBL_PROPOSER")
                        or row.get("PUBL_PROPOSER")
                    ),
                    "result": process_result,
                    "source_url": detail_link,
                    "summary_api": SUMMARY_API_KEY,
                    "detail_api": DETAIL_API_KEY,
                    "committee_presented_at": clean_optional(
                        detail.get("CMT_PRESENT_DT")
                        or row.get("CMT_PRESENT_DT")
                    ),
                    "committee_processed_at": clean_optional(
                        detail.get("CMT_PROC_DT")
                        or row.get("CMT_PROC_DT")
                    ),
                    "committee_result": clean_optional(
                        detail.get("CMT_PROC_RESULT_CD")
                        or row.get("CMT_PROC_RESULT_CD")
                    ),
                    "law_submitted_at": clean_optional(
                        detail.get("LAW_SUBMIT_DT")
                        or row.get("LAW_SUBMIT_DT")
                    ),
                    "law_processed_at": clean_optional(
                        detail.get("LAW_PROC_DT")
                        or row.get("LAW_PROC_DT")
                    ),
                    "plenary_processed_at": clean_optional(
                        detail.get("RGS_PROC_DT")
                        or detail.get("PROC_DT")
                        or row.get("PROC_DT")
                    ),
                },
            }
        )

    linked_count = sum(1 for bill in bills if bill.get("official_summary"))
    detail_count = sum(
        1
        for bill in bills
        if bill.get("committee") != "소관위원회 미정 또는 확인 필요"
        or len(bill.get("progress_timeline") or []) > 1
    )
    structured_count = sum(
        1 for bill in bills if bill.get("analysis_status") == "구조화 초안"
    )

    if bills and linked_count == 0:
        raise RuntimeError(
            "법안 목록은 수집했지만 공식 제안이유·주요내용이 한 건도 연결되지 않았습니다."
        )

    all_warnings = (
        [f"SUMMARY {item}" for item in summary_errors]
        + [f"DETAIL {item}" for item in detail_errors]
    )

    output = {
        "generated_at": generated_at,
        "notice": (
            "대한민국 국회 열린국회정보의 제22대 국회 최신 의안입니다. "
            f"기본정보 {len(bills)}건, 공식 제안이유·주요내용 {linked_count}건, "
            f"상세 진행정보 {detail_count}건, 규칙 기반 구조화 초안 {structured_count}건을 표시합니다. "
            "구조화 초안은 사람 검토 전 자료이며 정책의 찬반·선악 판정이 아닙니다."
        ),
        "source": {
            "provider": "대한민국 국회 열린국회정보",
            "list_api": "국회의원 발의법률안",
            "summary_api": "법률안 제안이유 및 주요내용",
            "detail_api": "의안 상세정보",
            "official_total_count": total_count,
            "retrieved_count": len(bills),
            "official_summary_count": linked_count,
            "official_detail_count": detail_count,
            "structured_draft_count": structured_count,
            "summary_error_count": len(summary_errors),
            "detail_error_count": len(detail_errors),
        },
        "stats": {
            "tracked": len(bills),
            "official_summaries": linked_count,
            "structured_drafts": structured_count,
            "ai_drafts": 0,
            "citizen_review": 0,
            "forks": 0,
        },
        "collection_warnings": all_warnings[:30],
        "bills": bills,
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(
        f"최신 {len(bills)}건 수집 / 공식 원문 {linked_count}건 / "
        f"상세 진행정보 {detail_count}건 / 구조화 초안 {structured_count}건."
    )
    if all_warnings:
        print(f"개별 API 경고 {len(all_warnings)}건은 다음 실행에서 다시 확인합니다.")


if __name__ == "__main__":
    main()
