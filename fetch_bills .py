"""Democracy 3.0 정책포크 v0.7

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


def category_from_context(title: str, committee: str) -> tuple[str, str]:
    official_category = category_from_committee(committee)
    if official_category != "기타":
        return official_category, "소관위원회 기준"

    rules = [
        (("교육", "학교", "대학", "교원", "유치원", "교육감"), "교육"),
        (("보건", "의료", "환자", "건강", "복지", "장애", "노인"), "복지·보건"),
        (("개인정보", "정보통신", "인공지능", "데이터", "전자정부", "디지털"), "과학·디지털"),
        (("국토", "주택", "건축", "교통", "철도", "도로", "자동차"), "국토·교통"),
        (("근로", "노동", "산업재해", "고용", "환경", "기후", "탄소"), "환경·노동"),
        (("농업", "농촌", "축산", "수산", "어업", "산림"), "농림·해양"),
        (("형법", "민법", "소송", "법원", "검찰", "변호사"), "법·사법"),
        (("재난", "소방", "경찰", "지방자치", "행정", "공무원"), "행정·안전"),
        (("국방", "군인", "병역", "외교", "통일", "북한"), "외교·안보"),
        (("문화", "체육", "관광", "예술", "청소년", "가족", "여성"), "문화·사회"),
        (("조세", "세금", "은행", "금융", "기업", "산업", "상법"), "경제·산업"),
    ]
    for words, category in rules:
        if any(word in title for word in words):
            return category, "법안명 기준 자동분류"
    return "기타", "자동분류"



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
                    "User-Agent": "Democracy3-Policy-Fork/0.7",
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



def unique_items(items: list[str], limit: int = 6) -> list[str]:
    result: list[str] = []
    for item in items:
        value = clean_optional(item)
        if value and value not in result:
            result.append(value)
        if len(result) >= limit:
            break
    return result


def build_decision_question(text: str) -> str:
    if contains_any(text, ("임명", "선임", "선출", "추천", "동의")):
        return (
            "대표성·독립성을 훼손하지 않으면서 전문성과 책임성을 높이려면 "
            "권한을 누구에게 주고 어떤 견제장치를 붙여야 하는가?"
        )
    if contains_any(text, ("보안", "정보시스템", "개인정보", "정보통신망")):
        return (
            "새 의무가 서류상 준수에 그치지 않고 실제 안전 향상으로 이어지도록 "
            "무엇을 측정하고 누가 검증해야 하는가?"
        )
    if contains_any(text, ("지원", "보조금", "급여", "감면", "혜택")):
        return (
            "지원이 꼭 필요한 대상에게 도달하면서 사각지대·중복지원·재정 누수를 "
            "어떻게 동시에 줄일 것인가?"
        )
    if contains_any(text, ("벌칙", "과태료", "처벌", "제재", "금지")):
        return (
            "규제 목적을 달성하면서 과잉제재·선택적 집행·영세주체의 불균형 부담을 "
            "어떻게 막을 것인가?"
        )
    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        return (
            "새 의무가 실제 문제를 해결하도록 최소 기준·검증 방식·불이행 책임을 "
            "어떻게 설계해야 하는가?"
        )
    return (
        "제안된 수단이 문제의 원인에 직접 맞고, 비용과 부작용보다 편익이 크다는 것을 "
        "어떤 근거로 확인할 것인가?"
    )


def build_weaknesses(text: str) -> list[str]:
    weaknesses: list[str] = []

    if not contains_any(text, FINANCE_WORDS):
        weaknesses.append("공식 요약에 재정·인력 소요와 비용 부담 주체가 드러나지 않음")
    if not contains_any(text, METRIC_WORDS):
        weaknesses.append("성과를 무엇으로 측정할지 공식 요약에 명확한 기준이 없음")
    if not contains_any(text, SAFEGUARD_WORDS):
        weaknesses.append("감독·이의제기·책임추적 같은 통제장치가 공식 요약에 충분히 드러나지 않음")
    if not contains_any(text, REVIEW_WORDS):
        weaknesses.append("시행 후 재검토·일몰·중단 조건이 공식 요약에 보이지 않음")
    if not re.search(r"\d[\d,.]*\s*(건|명|개|%|퍼센트|억원|조원|년|개월)", text):
        weaknesses.append("문제 규모와 기대효과를 판단할 정량 근거가 제한적임")
    if not contains_any(text, ("예외", "제외", "면제", "특례")):
        weaknesses.append("예외·사각지대·경계사례 처리 방식이 공식 요약에 드러나지 않음")

    return unique_items(weaknesses, 5) or [
        "공식 요약만으로는 조문 간 충돌, 비용, 집행 세부를 충분히 판단하기 어려움"
    ]


def build_counterarguments(text: str) -> list[str]:
    arguments: list[str] = [
        "문제의 존재와 별개로, 제안된 수단이 실제 원인을 해결하는 가장 덜 침해적인 방법인지 검증이 필요함"
    ]

    if contains_any(text, ("임명", "선임")):
        arguments.append(
            "직접선거의 문제를 줄일 수 있지만 임명권자에게 권력이 집중되어 정치적 종속이 오히려 커질 수 있음"
        )
    if contains_any(text, ("선거", "투표")):
        arguments.append(
            "효율성과 전문성을 높이더라도 유권자의 직접 통제와 민주적 정당성을 약화할 수 있음"
        )
    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        arguments.append(
            "새 의무가 기존 제도와 중복되고 현장에서는 서류 작성만 늘리는 형식적 규제가 될 수 있음"
        )
    if contains_any(text, ("보안", "정보시스템", "개인정보")):
        arguments.append(
            "보안 의무를 법에 추가해도 예산·전문인력·사고공개가 없으면 실제 보안수준은 개선되지 않을 수 있음"
        )
    if contains_any(text, ("지원", "보조금", "급여", "감면")):
        arguments.append(
            "지원 확대가 필요한 사람을 돕는 대신 대상선정 왜곡·중복수혜·재정의 경직성을 키울 수 있음"
        )
    if contains_any(text, ("벌칙", "과태료", "처벌", "제재")):
        arguments.append(
            "제재 강화가 억지력을 높이기보다 영세주체에 불균형한 부담을 주고 음성화를 촉진할 수 있음"
        )
    if contains_any(text, ("위원회", "기관", "센터", "기구")):
        arguments.append(
            "새 조직이나 절차가 기존 기관과 중복되어 책임소재를 흐리고 행정비용만 늘릴 수 있음"
        )

    return unique_items(arguments, 5)


def build_loopholes(text: str) -> list[str]:
    loopholes: list[str] = []

    if contains_any(text, ("대통령령", "총리령", "부령", "정하는 바", "필요한 사항")):
        loopholes.append(
            "핵심 기준을 하위법령에 넓게 위임하면 집행기관이 법의 실질 범위를 크게 바꿀 수 있음"
        )
    if contains_any(text, ("필요한 조치", "적절한", "상당한", "충분한", "합리적")):
        loopholes.append(
            "추상적 기준은 기관마다 다르게 해석되어 선택적 집행이나 책임회피에 이용될 수 있음"
        )
    if contains_any(text, ("의무", "수립", "시행")) and not contains_any(
        text, ("벌칙", "과태료", "제재", "공개", "감사", "평가")
    ):
        loopholes.append(
            "의무는 있으나 검증·공개·제재가 없으면 문서만 만들어 형식적으로 준수할 가능성이 있음"
        )
    if contains_any(text, ("지원", "보조금", "급여")) and not contains_any(
        text, ("환수", "중복", "소득", "재산", "심사")
    ):
        loopholes.append(
            "대상요건과 환수기준이 약하면 명의분산·중복신청·형식적 자격맞추기로 제도를 우회할 수 있음"
        )
    if contains_any(text, ("등록", "신고", "허가")) and not contains_any(
        text, ("실질", "관계인", "특수관계", "우회")
    ):
        loopholes.append(
            "법적 명의와 실제 운영주체를 분리해 등록·허가 기준을 우회할 가능성을 확인해야 함"
        )
    if contains_any(text, ("정보", "개인정보", "자료")) and not contains_any(
        text, ("최소", "파기", "보유기간", "접근기록", "암호화")
    ):
        loopholes.append(
            "정보수집 목적과 보유기간이 좁게 정해지지 않으면 목적 외 이용과 과잉보유가 가능함"
        )

    return unique_items(loopholes, 5) or [
        "공식 요약만으로는 구체적인 우회경로를 확정할 수 없어 조문 정의·예외·위임규정을 확인해야 함"
    ]


def build_red_team_cases(text: str) -> list[dict[str, str]]:
    cases: list[dict[str, str]] = []

    def add(title: str, attack: str, consequence: str, guardrail: str, severity: str) -> None:
        cases.append(
            {
                "title": title,
                "attack": attack,
                "consequence": consequence,
                "guardrail": guardrail,
                "severity": severity,
            }
        )

    if contains_any(text, ("임명", "선임", "추천", "동의")):
        add(
            "인사권 포획",
            "임명권자와 다수의회가 자기 진영 인사를 반복 선임하거나 검증 절차를 형식화함",
            "독립성·전문성·정치적 중립성이 약화되고 책임이 임명권자에게 종속될 수 있음",
            "독립추천위원회, 공개평가기준, 이해충돌 회피, 임기분산, 청문자료 공개",
            "높음",
        )

    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        add(
            "서류 준수 공격",
            "기관이 실제 개선 없이 계획서·보고서만 작성해 법적 의무를 충족한 것처럼 보이게 함",
            "현장 문제는 남고 행정비용과 허위안심만 증가할 수 있음",
            "결과지표, 표본감사, 현장검증, 공개보고, 반복 미달 시 시정명령",
            "높음",
        )

    if contains_any(text, ("보안", "개인정보", "정보시스템", "정보통신망")):
        add(
            "보안 명분의 과잉권한",
            "보안 강화를 이유로 필요 이상의 정보접근·감시·자료보관 권한을 확대함",
            "사생활 침해와 내부자 오남용 위험이 커지고 사고 시 피해 규모가 확대될 수 있음",
            "최소수집, 접근권한 분리, 접속기록, 독립감사, 사고통지, 보유기간 제한",
            "높음",
        )

    if contains_any(text, ("지원", "보조금", "급여", "감면", "혜택")):
        add(
            "지원제도 포획",
            "정보와 행정역량이 좋은 집단이 기준을 선점하고 실제 취약계층은 신청에서 탈락함",
            "정책 혜택이 필요도가 아닌 신청능력과 조직력에 따라 배분될 수 있음",
            "자동선정·찾아가는 신청, 중복검증, 이의신청, 수혜분포 공개, 주기적 기준조정",
            "중간",
        )

    if contains_any(text, ("벌칙", "과태료", "처벌", "제재", "금지")):
        add(
            "선택적 집행",
            "모호한 규정을 이용해 특정 집단에는 엄격히 적용하고 다른 집단에는 느슨하게 적용함",
            "법 집행의 예측가능성과 평등성이 훼손되고 정치적·행정적 보복수단이 될 수 있음",
            "명확한 요건, 단계별 제재, 처분사유 공개, 불복절차, 집행통계 공개",
            "높음",
        )

    if contains_any(text, ("위원회", "기관", "센터", "기구")):
        add(
            "책임 분산",
            "여러 기관이 권한을 나눠 갖되 실패 책임은 서로에게 미룸",
            "결정은 늦어지고 예산은 늘지만 누구도 결과에 책임지지 않는 구조가 될 수 있음",
            "단일 책임기관, 역할표, 처리기한, 공동성과지표, 실패 시 책임귀속 명시",
            "중간",
        )

    if not cases:
        add(
            "목표-수단 불일치",
            "정책이 제시한 문제와 직접 관련이 약한 수단을 확대하면서 성과를 홍보함",
            "비용과 규제는 발생하지만 핵심 문제는 그대로 남을 수 있음",
            "문제원인 검증, 대안비교, 시범사업, 사전·사후 성과측정, 중단조건",
            "중간",
        )

    return cases[:6]


def build_failure_modes(text: str) -> list[dict[str, str]]:
    modes: list[dict[str, str]] = []

    def add(condition: str, failure: str, warning: str, response: str) -> None:
        modes.append(
            {
                "condition": condition,
                "failure": failure,
                "warning": warning,
                "response": response,
            }
        )

    add(
        "문제 규모와 원인에 대한 독립 검증 없이 시행",
        "정책 수단이 실제 원인과 어긋나 효과가 작거나 역효과가 발생함",
        "초기 성과지표가 개선되지 않는데 적용범위와 예산만 확대됨",
        "시행 전 기준선 공개, 대안 비교, 제한된 시범사업, 중단 기준 설정",
    )

    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        add(
            "집행기관의 인력·예산·전문성이 부족함",
            "현장점검 없이 서류심사만 남고 기관별 편차가 커짐",
            "처리 지연, 형식적 보고서 반복, 민원과 예외요청 급증",
            "업무량 추계, 전담인력, 표준지침, 현장감사, 단계적 시행",
        )

    if contains_any(text, ("임명", "선임", "추천", "동의", "위원회")):
        add(
            "인사·위원구성이 특정 세력에 편중됨",
            "견제장치가 내부 합의기구로 변질되고 반대정보가 배제됨",
            "후보군 축소, 반복되는 동일 경력·이해관계, 회의록 비공개",
            "구성 다양성, 공개추천, 소수의견 기록, 이해충돌 공개, 임기 교차",
        )

    if contains_any(text, FINANCE_WORDS) or contains_any(text, ("지원", "운영", "설치")):
        add(
            "지속가능한 재원 없이 사업을 확대함",
            "초기에는 시행되지만 예산 부족으로 대상 축소·서비스 질 하락이 발생함",
            "미지급·대기기간·지방별 격차·추가경정 반복",
            "비용추계 공개, 재원조달 규칙, 우선순위, 자동조정, 재정상한",
        )

    if contains_any(text, ("정보", "개인정보", "보안")):
        add(
            "정보 접근권한과 사고대응 책임이 불명확함",
            "침해사고 발생 후 기관 간 책임공방으로 피해통지와 복구가 늦어짐",
            "과도한 권한계정, 로그 미점검, 사고보고 지연, 반복 취약점",
            "책임자 지정, 접근기록, 사고통지 시한, 독립점검, 피해구제 절차",
        )

    add(
        "시행 후 평가와 수정 절차가 없음",
        "효과가 낮거나 부작용이 커도 제도가 관성적으로 존속함",
        "성과자료 없이 예산·조직·규제만 계속 유지 또는 확대됨",
        "일몰조항, 정기 재승인, 공개평가, 시민·전문가 이의제기, 자동축소 조건",
    )

    return modes[:6]


def build_fork_levers(text: str) -> list[dict[str, str]]:
    levers: list[dict[str, str]] = [
        {
            "dimension": "적용대상",
            "question": "누구에게 적용하고 누구를 예외로 둘 것인가?",
            "options": "전면 적용 / 고위험 대상 우선 / 규모별 단계 적용 / 취약계층·영세주체 예외",
        },
        {
            "dimension": "시행속도",
            "question": "한 번에 시행할지 시험 후 확대할지 결정해야 함",
            "options": "즉시 시행 / 시범지역 / 단계적 확대 / 성과조건부 확대",
        },
        {
            "dimension": "검증과 종료",
            "question": "실패를 누가 어떻게 확인하고 멈출 수 있는가?",
            "options": "일몰 / 정기 재승인 / 독립평가 / 자동중단 기준",
        },
    ]

    if contains_any(text, ("임명", "선임", "추천", "동의", "위원회")):
        levers.append(
            {
                "dimension": "권한 배분",
                "question": "결정권·추천권·동의권·해임권을 한 주체에 모을지 나눌지 선택해야 함",
                "options": "단독권한 / 복수기관 교차견제 / 독립추천 / 시민·전문가 참여",
            }
        )
    if contains_any(text, ("의무", "규제", "금지", "벌칙", "과태료")):
        levers.append(
            {
                "dimension": "강제수단",
                "question": "권고·공개·시정명령·제재 중 어느 강도를 적용할 것인가?",
                "options": "자율준수 / 공개평가 / 단계별 시정 / 비례적 제재",
            }
        )
    if contains_any(text, FINANCE_WORDS) or contains_any(text, ("지원", "설치", "운영")):
        levers.append(
            {
                "dimension": "비용 배분",
                "question": "국가·지자체·기관·사업자·수혜자 중 누가 얼마를 부담할 것인가?",
                "options": "전액 국비 / 매칭 / 규모별 분담 / 성과연동 / 비용상한",
            }
        )
    if contains_any(text, ("정보", "개인정보", "보안")):
        levers.append(
            {
                "dimension": "데이터 권한",
                "question": "어떤 정보를 누가 얼마 동안 접근·보관할 수 있는가?",
                "options": "최소수집 / 분리보관 / 접근기록 / 자동파기 / 외부감사",
            }
        )

    return levers[:7]


def build_fork_candidates(text: str) -> list[dict[str, str]]:
    forks: list[dict[str, str]] = []

    def add(kind: str, title: str, body: str, benefit: str, risk: str) -> None:
        forks.append(
            {
                "type": kind,
                "title": title,
                "body": body,
                "benefit": benefit,
                "risk": risk,
                "status": "자동 제안·사람 검토 전",
            }
        )

    if contains_any(text, ("임명", "선임", "추천", "동의")):
        add(
            "권한분산형",
            "독립추천·공개청문·임기분산 포크",
            "임명권자가 단독으로 후보를 정하지 못하도록 독립추천기구가 복수후보를 공개하고, 지방의회 또는 국회의 공개청문과 동의를 거치며, 위원·기관장 임기를 선거주기와 엇갈리게 설계합니다.",
            "정치적 포획과 단일권력 집중을 줄이고 후보 검증자료를 시민에게 공개할 수 있음",
            "절차가 길어지고 추천기구 자체가 또 다른 이해관계 집단에 포획될 수 있음",
        )

    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        add(
            "성과검증형",
            "문서 의무를 결과 의무로 바꾸는 포크",
            "계획서 제출만으로 준수한 것으로 보지 않고 최소 성과지표, 현장검증, 결과공개, 반복 미달 시 개선명령을 법률 또는 하위기준에 명시합니다.",
            "형식적 준수를 줄이고 실제 정책효과를 확인할 수 있음",
            "성과지표가 잘못 설계되면 숫자 맞추기와 현장 왜곡이 발생할 수 있음",
        )

    if contains_any(text, ("정보", "개인정보", "보안", "정보시스템")):
        add(
            "권리보호형",
            "최소수집·접근기록·사고통지 포크",
            "정보의 수집목적과 보유기간을 제한하고, 접근권한 분리와 접속기록 보존, 독립점검, 침해사고 통지시한과 피해구제 절차를 함께 둡니다.",
            "보안 강화가 과잉감시나 책임회피로 변질되는 위험을 줄임",
            "기관의 구축비용과 운영부담이 증가하고 세부기술기준이 빠르게 낡을 수 있음",
        )

    if contains_any(text, ("지원", "보조금", "급여", "감면", "혜택")):
        add(
            "정밀지원형",
            "자동발굴·중복검증·이의신청 포크",
            "신청주의만 두지 않고 행정자료로 잠재 대상자를 찾아 안내하며, 중복수혜 검증과 탈락사유 공개, 간단한 이의신청 절차를 결합합니다.",
            "정보취약계층의 탈락과 중복지원을 동시에 줄일 수 있음",
            "행정자료 결합이 개인정보 침해를 낳거나 잘못된 자동판정이 발생할 수 있음",
        )

    if contains_any(text, ("벌칙", "과태료", "처벌", "제재", "금지")):
        add(
            "비례규제형",
            "경고·시정·제재의 단계형 포크",
            "고의성·피해규모·반복성·자진시정 여부에 따라 경고, 시정명령, 과태료, 강한 제재를 단계화하고 처분사유와 불복절차를 공개합니다.",
            "과잉제재와 선택적 집행을 줄이면서 반복 위반에는 억지력을 유지할 수 있음",
            "절차가 복잡해지고 긴급한 위험에 대한 신속대응이 느려질 수 있음",
        )

    if not contains_any(text, REVIEW_WORDS):
        add(
            "가역성형",
            "시범시행·일몰·재승인 포크",
            "전면 영구시행 전에 제한된 대상이나 지역에서 시험하고, 공개된 성과·부작용 기준을 충족해야 확대하며, 일정 기간 후 국회가 재승인하지 않으면 종료되도록 합니다.",
            "잘못된 정책을 작은 범위에서 발견하고 되돌릴 수 있음",
            "불확실성이 커져 장기투자와 현장 준비가 지연될 수 있음",
        )

    if not contains_any(text, METRIC_WORDS):
        add(
            "책임추적형",
            "성과지표·공개대시보드 포크",
            "정책 목표, 기준선, 연도별 목표, 부작용 지표, 책임기관을 공개하고 정기적으로 원자료와 평가결과를 게시합니다.",
            "정책 성공과 실패를 시민이 추적하고 다음 개정의 근거로 사용할 수 있음",
            "측정하기 쉬운 지표에만 집중하거나 지표 조작이 발생할 수 있음",
        )

    return forks[:6]


def build_fork_readiness(
    text: str,
    problem_sentence: str,
    change_sentence: str,
    actors: list[str],
    committee: str,
) -> dict[str, Any]:
    points = 0
    reasons: list[str] = []
    missing: list[str] = []

    if problem_sentence:
        points += 1
        reasons.append("문제 문장 분리 가능")
    else:
        missing.append("명확한 문제정의")

    if change_sentence:
        points += 1
        reasons.append("변경방향 분리 가능")
    else:
        missing.append("구체적 변경방향")

    if actors:
        points += 1
        reasons.append("영향주체 후보 식별")
    else:
        missing.append("적용대상·영향주체")

    if re.search(r"\(안\s*제?\d+조", text):
        points += 1
        reasons.append("관련 조문 단서 존재")
    else:
        missing.append("개정 조문 단서")

    if committee and committee != "소관위원회 미정 또는 확인 필요":
        points += 1
        reasons.append("소관위원회 확인")
    else:
        missing.append("소관위원회·심사자료")

    level = "높음" if points >= 5 else "중간" if points >= 3 else "초기"
    missing.extend(["법안 조문 전문", "비용추계서", "위원회 검토보고서·회의록"])

    return {
        "level": level,
        "points": points,
        "max_points": 5,
        "reasons": unique_items(reasons, 5),
        "missing": unique_items(missing, 6),
        "meaning": "정책의 우수성 점수가 아니라 수정안을 설계하기 위해 확보된 정보의 준비 정도",
    }



def remove_clause_reference(sentence: str) -> str:
    return re.sub(r"\s*\(안\s*[^)]*\)\s*\.?$", "", clean_optional(sentence)).strip()


def extract_clause_reference(text: str) -> str:
    match = re.search(r"\(안\s*([^)]+)\)", text)
    if match:
        return "안 " + match.group(1).strip()
    return "관련 개정조문 — 조문 전문 확인 필요"


def easy_korean(sentence: str) -> str:
    value = remove_clause_reference(sentence)
    value = re.sub(r"^(그러나|그런데|이에|이에,|한편|한편,|특히|특히,)\s*", "", value)
    value = value.strip(" .")

    replacements = (
        ("하도록 하고자 함", "하도록 바꾸려는 내용입니다"),
        ("할 수 있도록 하고자 함", "할 수 있게 바꾸려는 내용입니다"),
        ("하고자 함", "하려는 내용입니다"),
        ("하려는 것임", "하려는 내용입니다"),
        ("규정하고 있음", "규정하고 있습니다"),
        ("규정되어 있음", "규정되어 있습니다"),
        ("제외되어 있음", "대상에서 빠져 있습니다"),
        ("발생하고 있음", "발생하고 있습니다"),
        ("드러나고 있음", "드러나고 있습니다"),
        ("필요한 실정임", "필요한 상황입니다"),
        ("어려운 실정임", "어려운 상황입니다"),
        ("실정임", "상황입니다"),
        ("필요함", "필요하다는 내용입니다"),
        ("어려움", "어렵습니다"),
        ("없음", "없습니다"),
        ("있음", "있습니다"),
        ("규정함", "규정합니다"),
    )
    for old, new in replacements:
        if value.endswith(old):
            value = value[: -len(old)].rstrip() + new
            break

    if not re.search(r"(입니다|습니다|합니다|됩니다|내용입니다|상황입니다)$", value):
        value += "."

    value = re.sub(r"\.{2,}", ".", value)
    return value.strip()


def build_easy_card_summary(
    title: str,
    text: str,
    problem_sentence: str,
    change_sentence: str,
) -> str:
    if "교육감" in text and contains_any(text, ("임명", "선임")):
        return (
            "교육감을 주민이 직접 뽑는 대신 지방자치단체장이 임명하고 "
            "지방의회가 청문·동의하도록 바꾸려는 법안입니다."
        )
    if "공공기관" in text and contains_any(text, ("보안", "정보시스템")):
        return (
            "공공기관도 정보시스템 보안대책을 의무적으로 세우고 실행하도록 "
            "책임 범위를 넓히려는 법안입니다."
        )
    if change_sentence:
        easy_change = easy_korean(change_sentence)
        if len(easy_change) > 170:
            easy_change = easy_change[:167].rstrip() + "…"
        return "쉽게 말해, " + easy_change
    if problem_sentence:
        easy_problem = easy_korean(problem_sentence)
        return "이 법안은 " + easy_problem
    return f"{title}의 핵심 변경 내용을 공식 요약에서 쉽게 분리하지 못했습니다."


def build_claimed_benefit(text: str) -> str:
    if "교육감" in text and contains_any(text, ("임명", "선임")):
        return "후보자의 전문성과 책임성을 더 엄격히 검증하고 선거 혼란을 줄일 수 있다는 주장입니다."
    if contains_any(text, ("보안", "정보시스템", "개인정보")):
        return "공공 정보시스템의 안전성과 국민 개인정보 보호를 강화할 수 있다는 주장입니다."
    if contains_any(text, ("지원", "보조금", "급여", "감면", "혜택")):
        return "지원 사각지대를 줄이고 필요한 대상의 보호나 혜택을 넓힐 수 있다는 주장입니다."
    if contains_any(text, ("벌칙", "과태료", "처벌", "제재")):
        return "위반을 줄이고 제도의 실효성과 준수율을 높일 수 있다는 주장입니다."
    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        return "담당 기관이나 사업자의 책임을 분명히 하고 실제 집행력을 높일 수 있다는 주장입니다."
    return "제안된 제도 변경으로 현재의 문제를 줄일 수 있다는 것이 법안의 핵심 주장입니다."


def build_evidence_review(
    text: str,
    problem_sentence: str,
    strengths: list[str],
) -> dict[str, list[str]]:
    clues = unique_items(strengths, 4)
    verified = [
        "현재 연결된 자료만으로 독립적으로 검증이 끝난 외부 근거는 없습니다."
    ]
    unverified: list[str] = []

    if re.search(r"\d[\d,.]*\s*(건|명|개|%|퍼센트|억원|조원|년|개월)", text):
        unverified.append("공식 요약에 나온 수치·사례는 원자료와 산출방식을 아직 대조하지 않았습니다.")
    if contains_any(text, ("미국", "일본", "유럽", "해외", "선진국", "외국")):
        unverified.append("해외 제도 비교는 국가별 제도 차이와 원출처를 아직 확인하지 않았습니다.")
    if problem_sentence:
        unverified.append("법안이 제시한 문제 진단은 발의자의 주장으로서 독립 검증 전입니다.")
    unverified.append("법안이 예상하는 효과는 시행자료가 없는 사전 주장으로서 검증 전입니다.")

    return {
        "analysis_clues": clues or ["공식 제안이유와 관련 조문 단서가 공개되어 있습니다."],
        "verified_evidence": verified,
        "unverified_claims": unique_items(unverified, 4),
    }


def build_fork_candidates_v07(
    title: str,
    text: str,
    change_sentence: str,
) -> list[dict[str, str]]:
    clause_hint = extract_clause_reference(text)
    forks: list[dict[str, str]] = []

    def add(
        label: str,
        kind: str,
        fork_title: str,
        change: str,
        solves: str,
        benefit: str,
        risk: str,
        cost: str,
        evidence_needed: str,
    ) -> None:
        forks.append(
            {
                "label": label,
                "type": kind,
                "title": fork_title,
                "change": change,
                "solves": solves,
                "benefit": benefit,
                "risk": risk,
                "cost": cost,
                "clause_hint": clause_hint,
                "evidence_needed": evidence_needed,
                "status": "자동 수정안 아이디어 · 사람 검토 전",
            }
        )

    if "교육감" in text and contains_any(text, ("임명", "선임")):
        add(
            "A",
            "권력분산 임명형",
            "독립추천·공개청문·가중동의",
            "독립추천위원회가 복수 후보를 공개 추천하고, 단체장은 그중 1명을 지명하며, 지방의회는 단순 과반보다 높은 동의요건으로 승인합니다.",
            "단체장 한 사람이나 의회 다수파가 교육감 인사를 독점하는 위험",
            "전문성 검증과 정치적 견제를 함께 둘 수 있습니다.",
            "추천위원회 자체가 이해관계자에게 포획되고 선임이 장기 지연될 수 있습니다.",
            "행정비용 중간 — 추천·청문 절차 운영비 검토 필요",
            "추천위원 구성, 동의정족수, 임기·해임 조건 비교자료",
        )
        add(
            "B",
            "직선제 개선형",
            "직선제 유지·결선투표·정보공개",
            "교육감 직선제는 유지하되 과반 득표자가 없으면 결선투표를 하고, 후보자의 경력·정책·재산·이해충돌·선거비용 정보를 표준화해 공개합니다.",
            "유권자의 직접 선택권을 없애지 않으면서 후보 난립과 정보 부족을 줄이는 문제",
            "민주적 정당성을 유지하면서 대표성과 검증가능성을 높일 수 있습니다.",
            "선거비용과 정치적 경쟁은 계속되고 정보공개만으로 전문성이 보장되지는 않습니다.",
            "선거비용 높음 — 결선투표 추가비용 추계 필요",
            "무효표·득표분산 자료, 결선투표 비용, 후보정보 이용효과",
        )
        add(
            "C",
            "혼합 선택형",
            "주민 선택과 전문가 검증 결합",
            "독립기구가 자격과 전문성을 심사해 복수 후보를 만들고, 주민이 최종 선택하거나 주민투표 결과와 전문가 평가를 일정 비율로 결합합니다.",
            "전문성 검증과 주민의 직접 통제를 동시에 확보하는 문제",
            "직선제와 임명제의 장점을 일부 결합할 수 있습니다.",
            "평가점수와 주민투표 중 무엇을 우선할지 논란이 생기고 제도가 복잡해질 수 있습니다.",
            "행정·선거비용 높음 — 이중 절차 비용 검토 필요",
            "전문가 평가 신뢰도, 주민 수용성, 해외 혼합모델 성과",
        )
        return forks

    if contains_any(text, ("보안", "정보시스템", "개인정보")):
        add(
            "A",
            "최소기준형",
            "위험등급별 최소 보안기준",
            "모든 기관에 같은 의무를 주는 대신 보유정보의 민감도와 시스템 중요도에 따라 필수 보안조치를 단계화합니다.",
            "기관 규모와 위험이 다른데 동일 규제를 적용하는 문제",
            "고위험 시스템에 자원을 집중하고 영세기관의 과도한 부담을 줄일 수 있습니다.",
            "위험등급을 낮게 신고하거나 분류기준을 우회할 수 있습니다.",
            "비용 중간 — 기관별 위험평가 필요",
            "침해사고 통계, 시스템별 위험도, 기관별 구축비용",
        )
        add(
            "B",
            "성과책임형",
            "계획서보다 실제 보안성과 검증",
            "보안계획 제출만으로 의무를 다한 것으로 보지 않고 독립점검, 취약점 개선기한, 사고통지, 결과공개를 결합합니다.",
            "서류만 갖추고 실제 보안은 개선하지 않는 형식적 준수",
            "실제 취약점과 사고대응 능력을 확인할 수 있습니다.",
            "측정지표 맞추기와 감사 대응용 형식주의가 새로 생길 수 있습니다.",
            "비용 높음 — 독립점검과 개선비용 필요",
            "점검주기, 공개범위, 사고통지 기준, 전문인력 수요",
        )
        add(
            "C",
            "단계시행형",
            "고위험 기관 우선·시범 후 확대",
            "민감정보와 필수 공공서비스를 다루는 기관부터 시행하고 성과와 비용을 공개한 뒤 적용범위를 넓힙니다.",
            "전면 시행으로 인한 준비부족과 예산낭비",
            "초기 실패를 작은 범위에서 발견하고 기준을 보완할 수 있습니다.",
            "낮은 우선순위로 분류된 기관의 위험이 방치될 수 있습니다.",
            "초기비용 중간 — 확대 시 총비용 재산정 필요",
            "우선순위 기준, 시범기관 성과, 확대·중단 조건",
        )
        return forks

    if contains_any(text, ("지원", "보조금", "급여", "감면", "혜택")):
        add(
            "A", "보편·간편형", "넓은 대상과 간단한 신청",
            "대상 범위를 넓히고 신청서류와 심사를 최소화합니다.",
            "복잡한 요건 때문에 필요한 사람이 탈락하는 문제",
            "접근성과 수혜율을 높일 수 있습니다.",
            "재정지출과 불필요한 지원이 늘 수 있습니다.",
            "비용 높음 가능 — 대상 규모 추계 필요",
            "잠재 대상자 수, 1인당 비용, 중복지원 규모",
        )
        add(
            "B", "정밀지원형", "필요도 기반 선별·자동안내",
            "소득·재산·위기정보로 우선대상을 정하고 행정자료를 활용해 받을 가능성이 있는 사람에게 먼저 안내합니다.",
            "재정 누수와 정보취약계층의 미신청 문제",
            "필요도가 높은 사람에게 재원을 집중할 수 있습니다.",
            "자동판정 오류와 개인정보 결합 위험이 있습니다.",
            "비용 중간 — 자료연계·이의신청 체계 필요",
            "탈락오류율, 개인정보 영향평가, 이의신청 처리량",
        )
        add(
            "C", "성과연동형", "지원·평가·재조정 결합",
            "일정 기간 지원한 뒤 생활안정·고용·건강 등 목적에 맞는 결과를 평가해 지원방식을 조정합니다.",
            "지원이 계속되지만 실제 효과를 확인하지 않는 문제",
            "정책효과와 재정 지속가능성을 함께 관리할 수 있습니다.",
            "개인의 상황을 단순한 성과지표로 판단해 부당한 중단이 생길 수 있습니다.",
            "비용 중간 — 평가·사후관리 비용 필요",
            "성과지표 타당성, 장기효과, 중단 시 피해",
        )
        return forks

    if contains_any(text, ("벌칙", "과태료", "처벌", "제재", "금지")):
        add(
            "A", "단계제재형", "경고·시정·제재 단계화",
            "고의성·피해·반복성에 따라 경고, 시정명령, 과태료, 강한 제재를 순차 적용합니다.",
            "경미한 위반까지 같은 수준으로 처벌하는 문제",
            "비례성을 높이고 반복 위반에는 억지력을 유지할 수 있습니다.",
            "긴급 위험에 대한 대응이 늦어질 수 있습니다.",
            "행정비용 중간 — 위반등급 심사 필요",
            "위반유형·재범률·피해규모 통계",
        )
        add(
            "B", "위험집중형", "고위험 행위에 집행력 집중",
            "위험과 피해가 큰 행위는 강하게 제재하고 경미한 위반은 교육·개선 중심으로 처리합니다.",
            "단속자원을 낮은 위험에 낭비하는 문제",
            "한정된 집행자원을 중대한 피해 예방에 집중할 수 있습니다.",
            "위험분류가 자의적으로 운영될 수 있습니다.",
            "비용 중간 — 위험평가체계 필요",
            "위험등급 기준, 집행편차, 피해감소 효과",
        )
        add(
            "C", "투명성형", "집행통계·처분사유·불복 공개",
            "제재수준뿐 아니라 처분사유, 기관별 집행통계, 이의신청 결과를 공개합니다.",
            "선택적 집행과 기관별 들쭉날쭉한 처분",
            "집행의 평등성과 예측가능성을 높일 수 있습니다.",
            "개별 사건의 개인정보와 영업비밀 침해 우려가 있습니다.",
            "비용 낮음~중간 — 공개시스템 구축 필요",
            "기관별 처분격차, 불복 인용률, 공개범위",
        )
        return forks

    add(
        "A", "최소개정형", "적용범위를 좁힌 최소 수정",
        "법안이 겨냥한 핵심 문제에 직접 관련된 대상과 조문만 우선 바꿉니다.",
        "불필요하게 넓은 적용과 예측하지 못한 부작용",
        "제도 변화의 충격과 비용을 줄일 수 있습니다.",
        "범위가 너무 좁아 문제 해결효과가 작을 수 있습니다.",
        "비용 낮음~중간 — 대상 규모 확인 필요",
        "핵심 원인, 직접 적용대상, 현행제도 대안",
    )
    add(
        "B", "단계시행형", "시범사업 후 조건부 확대",
        "일부 지역·기관·대상에서 먼저 시행하고 공개된 성과와 부작용 기준을 충족해야 확대합니다.",
        "전면 시행 뒤 실패를 되돌리기 어려운 문제",
        "작은 범위에서 오류를 찾고 수정할 수 있습니다.",
        "지역·대상별 형평성 논란과 시행 지연이 생길 수 있습니다.",
        "초기비용 중간 — 시범평가 비용 필요",
        "기준선, 성과·부작용 지표, 확대·중단 조건",
    )
    add(
        "C", "책임추적형", "성과공개·일몰·재승인",
        "목표, 책임기관, 비용, 부작용 지표를 공개하고 일정 기간 뒤 국회 재승인을 받도록 합니다.",
        "성과가 없어도 제도가 관성적으로 유지되는 문제",
        "실패를 확인하고 수정하거나 종료할 수 있습니다.",
        "단기 성과에 치우치고 장기정책의 안정성이 낮아질 수 있습니다.",
        "비용 중간 — 평가와 공개체계 필요",
        "장기성과, 평가주체 독립성, 일몰기간",
    )
    return forks


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

    empty_scores = {
        key: 0
        for key in (
            "문제정의", "근거", "재정", "집행",
            "기본권", "통제장치", "성과측정", "재검토",
        )
    }

    if not text:
        return {
            "analysis_status": "미분석",
            "analysis_method": "공식 원문 없음",
            "analysis_confidence": "생성 불가",
            "analysis_review_state": "사람 검토 필요",
            "analysis_generated_at": generated_at,
            "card_summary": "공식 제안이유·주요내용이 없어 쉬운 설명을 만들지 못했습니다.",
            "plain_language": "공식 원문을 먼저 확인해야 합니다.",
            "problem_definition": "공식 원문 없음",
            "proposed_change": "공식 원문 없음",
            "affected_groups": [],
            "beneficiaries": ["공식 원문 없음"],
            "cost_bearers": ["공식 원문 없음"],
            "authority_changes": ["공식 원문 없음"],
            "claimed_benefit": "확인 불가",
            "strongest_objection": "확인 불가",
            "largest_risk": "확인 불가",
            "analysis_clues": ["공식 원문 없음"],
            "verified_evidence": ["독립 검증 자료 없음"],
            "unverified_claims": ["공식 원문 없음"],
            "strengths": ["공식 원문 없음"],
            "weaknesses": ["공식 원문 없음"],
            "risks": ["공식 원문 없음"],
            "counterarguments": ["공식 원문 없음"],
            "loopholes": ["공식 원문 없음"],
            "red_team_cases": [],
            "failure_modes": [],
            "fork_levers": [],
            "fork_candidates": [],
            "fork_readiness": {
                "level": "자료 없음",
                "points": 0,
                "max_points": 5,
                "reasons": [],
                "missing": ["국회 공식 제안이유·주요내용"],
                "meaning": "수정안 작성에 필요한 자료가 얼마나 확보됐는지를 뜻합니다.",
            },
            "one_minute_brief": {
                "problem": "공식 원문 없음",
                "change": "공식 원문 없음",
                "who": "확인 불가",
                "decision": "국회 원문과 부속자료를 먼저 확보해야 합니다.",
            },
            "questions": ["국회 원문과 부속자료를 직접 확인해야 합니다."],
            "scores": empty_scores,
            "analysis_basis": (
                "공식 제안이유·주요내용을 받지 못해 자동 검토를 생성하지 않았습니다."
            ),
        }

    strengths = build_strengths(text, change_sentence)
    weaknesses = build_weaknesses(text)
    counterarguments = build_counterarguments(text)
    risks = build_risks(text)
    loopholes = build_loopholes(text)
    red_team_cases = build_red_team_cases(text)
    failure_modes = build_failure_modes(text)
    affected = actors or ["공식 요약에서 직접 영향 주체를 찾지 못했습니다."]
    evidence = build_evidence_review(text, problem_sentence, strengths)

    easy_problem = easy_korean(
        problem_sentence or (sentences[0] if sentences else "문제정의 확인 필요")
    )
    easy_change = easy_korean(
        change_sentence or (sentences[-1] if sentences else "변경방향 확인 필요")
    )
    claimed_benefit = build_claimed_benefit(text)
    strongest_objection = (
        counterarguments[1] if len(counterarguments) > 1 else counterarguments[0]
    )
    largest_risk = (
        red_team_cases[0]["consequence"]
        if red_team_cases
        else (risks[0] if risks else "구체적인 위험을 추가 확인해야 합니다.")
    )

    return {
        "analysis_status": "자동 검토 초안",
        "analysis_method": "공식 원문 규칙 기반 국민용 설명·위험검토·포크 v0.3",
        "analysis_confidence": confidence,
        "analysis_review_state": "사람 검토 전",
        "analysis_generated_at": generated_at,
        "card_summary": build_easy_card_summary(
            title, text, problem_sentence, change_sentence
        ),
        "plain_language": (
            f"이 법안은 {easy_problem} "
            f"그래서 {easy_change}"
        ),
        "problem_definition": easy_problem,
        "proposed_change": easy_change,
        "affected_groups": affected,
        "beneficiaries": build_beneficiaries(text, actors),
        "cost_bearers": build_cost_bearers(text, actors),
        "authority_changes": build_authority_changes(text),
        "claimed_benefit": claimed_benefit,
        "strongest_objection": strongest_objection,
        "largest_risk": largest_risk,
        "analysis_clues": evidence["analysis_clues"],
        "verified_evidence": evidence["verified_evidence"],
        "unverified_claims": evidence["unverified_claims"],
        "strengths": strengths,
        "weaknesses": weaknesses,
        "risks": risks,
        "counterarguments": counterarguments,
        "loopholes": loopholes,
        "red_team_cases": red_team_cases,
        "failure_modes": failure_modes,
        "fork_levers": build_fork_levers(text),
        "fork_candidates": build_fork_candidates_v07(
            title, text, change_sentence
        ),
        "fork_readiness": build_fork_readiness(
            text,
            problem_sentence,
            change_sentence,
            actors,
            committee,
        ),
        "one_minute_brief": {
            "problem": easy_problem,
            "change": easy_change,
            "who": ", ".join(affected[:4]),
            "decision": build_decision_question(text),
        },
        "questions": build_questions(text, committee),
        "scores": build_source_signals(text, problem_sentence),
        "analysis_basis": (
            "국회가 공개한 ‘제안이유 및 주요내용’만 사용한 자동 초안입니다. "
            "찬성 논리, 반대 논리, 위험과 수정안은 검토를 시작하기 위한 가설입니다. "
            "법안 조문 전문, 비용추계서, 위원회 검토보고서, 회의록, 외부 통계와 "
            "이해관계자 의견을 확인하기 전에는 결론으로 사용해서는 안 됩니다."
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
                "category": category_from_context(title, committee)[0],
                "category_basis": category_from_context(title, committee)[1],
                "title": title,
                "status": analysis["analysis_status"],
                "stage": stage,
                "committee": committee,
                "proposed_date": proposed_date,
                "updated_at": today,
                "review_count": 0,
                "summary": analysis["card_summary"],
                "official_excerpt": excerpt(official_summary),
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
                "card_summary": analysis["card_summary"],
                "why_now": analysis["plain_language"],
                "claimed_benefit": analysis["claimed_benefit"],
                "strongest_objection": analysis["strongest_objection"],
                "largest_risk": analysis["largest_risk"],
                "analysis_clues": analysis["analysis_clues"],
                "verified_evidence": analysis["verified_evidence"],
                "unverified_claims": analysis["unverified_claims"],
                "problem_definition": analysis["problem_definition"],
                "proposed_change": analysis["proposed_change"],
                "affected_groups": analysis["affected_groups"],
                "beneficiaries": analysis["beneficiaries"],
                "cost_bearers": analysis["cost_bearers"],
                "authority_changes": analysis["authority_changes"],
                "strengths": analysis["strengths"],
                "weaknesses": analysis["weaknesses"],
                "risks": analysis["risks"],
                "counterarguments": analysis["counterarguments"],
                "loopholes": analysis["loopholes"],
                "red_team_cases": analysis["red_team_cases"],
                "failure_modes": analysis["failure_modes"],
                "fork_levers": analysis["fork_levers"],
                "fork_candidates": analysis["fork_candidates"],
                "fork_readiness": analysis["fork_readiness"],
                "one_minute_brief": analysis["one_minute_brief"],
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
        1 for bill in bills if bill.get("analysis_status") == "자동 검토 초안"
    )
    red_team_case_count = sum(
        len(bill.get("red_team_cases") or []) for bill in bills
    )
    fork_candidate_count = sum(
        len(bill.get("fork_candidates") or []) for bill in bills
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
            f"상세 진행정보 {detail_count}건, 쉬운 설명과 자동 검토 {structured_count}건, "
            f"핵심 위험 시나리오 {red_team_case_count}개, 자동 수정안 아이디어 {fork_candidate_count}개를 표시합니다. "
            "자동 설명과 수정안은 사람이 검토하기 전의 초안입니다."
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
            "red_team_case_count": red_team_case_count,
            "fork_candidate_count": fork_candidate_count,
            "summary_error_count": len(summary_errors),
            "detail_error_count": len(detail_errors),
        },
        "stats": {
            "tracked": len(bills),
            "official_summaries": linked_count,
            "structured_drafts": structured_count,
            "red_team_cases": red_team_case_count,
            "fork_candidates": fork_candidate_count,
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
        f"상세 진행정보 {detail_count}건 / 쉬운 설명 {structured_count}건 / "
        f"위험 검토 {red_team_case_count}개 / 수정안 아이디어 {fork_candidate_count}개."
    )
    if all_warnings:
        print(f"개별 API 경고 {len(all_warnings)}건은 다음 실행에서 다시 확인합니다.")


if __name__ == "__main__":
    main()
