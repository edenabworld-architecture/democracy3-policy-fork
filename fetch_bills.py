"""Democracy 3.0 정책포크 v0.10.1

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
                    "User-Agent": "Democracy3-Policy-Fork/0.10.1",
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

    # 분야별 고유 약점을 먼저 제시합니다.
    if contains_any(text, ("임명", "선임", "추천", "동의")):
        if not contains_any(text, ("임기", "해임", "독립", "이해충돌")):
            weaknesses.append(
                "임명 이후의 임기 보장, 해임 사유, 이해충돌 방지장치가 공식 요약에 드러나지 않습니다."
            )
        if not contains_any(text, ("추천위원", "복수후보", "정족수", "소수의견")):
            weaknesses.append(
                "후보 추천 방식과 의회 동의요건이 구체적이지 않아 권력집중 위험을 판단하기 어렵습니다."
            )

    if contains_any(text, ("보안", "정보시스템", "개인정보")):
        if not contains_any(text, ("보유기간", "파기", "접근기록", "사고통지")):
            weaknesses.append(
                "정보 접근기록, 보유기간, 사고통지와 피해구제 절차가 공식 요약에 드러나지 않습니다."
            )
        if not contains_any(text, ("인력", "전문인력", "예산", "비용")):
            weaknesses.append(
                "보안 의무를 실제로 이행할 전문인력과 예산 대책이 공식 요약에 보이지 않습니다."
            )

    if contains_any(text, ("지원", "보조금", "급여", "감면", "혜택")):
        if not contains_any(text, ("대상", "소득", "재산", "중복", "환수")):
            weaknesses.append(
                "지원대상 선정, 중복수혜 방지와 환수기준이 공식 요약에 충분히 드러나지 않습니다."
            )

    if contains_any(text, ("벌칙", "과태료", "처벌", "제재", "금지")):
        if not contains_any(text, ("고의", "반복", "피해규모", "불복", "이의")):
            weaknesses.append(
                "위반의 고의성·피해규모에 따른 제재 차등과 불복절차가 공식 요약에 보이지 않습니다."
            )

    # 공통 누락은 뒤에 배치하며 최대 3개만 보여줍니다.
    if not contains_any(text, FINANCE_WORDS):
        weaknesses.append(
            "재정·인력 소요와 비용 부담 주체가 공식 요약에 드러나지 않습니다."
        )
    if not contains_any(text, METRIC_WORDS):
        weaknesses.append(
            "정책이 성공했는지 판단할 성과기준이 공식 요약에 명확하지 않습니다."
        )
    if not contains_any(text, REVIEW_WORDS):
        weaknesses.append(
            "시행 후 재검토·일몰·중단 조건이 공식 요약에 보이지 않습니다."
        )
    if not contains_any(text, ("예외", "제외", "면제", "특례")):
        weaknesses.append(
            "예외·사각지대·경계사례를 어떻게 처리할지 공식 요약만으로는 알기 어렵습니다."
        )

    return dedupe_similar(weaknesses, 3) or [
        "공식 요약만으로는 비용, 집행 세부와 조문 간 충돌을 충분히 판단하기 어렵습니다."
    ]


def build_counterarguments(text: str) -> list[str]:
    arguments: list[str] = []

    if contains_any(text, ("임명", "선임")):
        arguments.append(
            "직접선거의 문제를 줄이더라도 임명권자에게 권력이 집중되어 정치적 종속이 더 커질 수 있습니다."
        )
    if contains_any(text, ("선거", "투표")):
        arguments.append(
            "전문성과 효율성을 높이더라도 유권자의 직접 통제와 민주적 정당성이 약해질 수 있습니다."
        )
    if contains_any(text, ("보안", "정보시스템", "개인정보")):
        arguments.append(
            "법률에 보안 의무만 추가하고 예산·전문인력·사고공개가 빠지면 실제 보안은 개선되지 않을 수 있습니다."
        )
    if contains_any(text, ("의무", "수립", "시행", "제출", "보고")):
        arguments.append(
            "새 의무가 기존 제도와 겹치고 현장에서는 실제 개선 없이 서류만 늘리는 규제가 될 수 있습니다."
        )
    if contains_any(text, ("지원", "보조금", "급여", "감면")):
        arguments.append(
            "지원 확대가 실제 취약계층보다 신청능력이 높은 집단에 유리하고 재정의 경직성을 키울 수 있습니다."
        )
    if contains_any(text, ("벌칙", "과태료", "처벌", "제재")):
        arguments.append(
            "제재 강화가 억지력보다 영세주체의 불균형 부담과 선택적 집행을 키울 수 있습니다."
        )
    if contains_any(text, ("위원회", "기관", "센터", "기구")):
        arguments.append(
            "새 조직이나 절차가 기존 기관과 겹쳐 책임소재를 흐리고 행정비용만 늘릴 수 있습니다."
        )

    arguments.append(
        "법안이 제시한 문제가 실제로 존재하더라도, 제안된 수단이 가장 효과적이고 덜 침해적인 대안인지는 별도로 검증해야 합니다."
    )
    return dedupe_similar(arguments, 3)



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


def normalize_korean_sentence(value: str) -> str:
    value = clean_optional(value)
    value = re.sub(r"\s+", " ", value)
    value = re.sub(r"\s+([,.;:!?])", r"\1", value)
    value = re.sub(r"([,.;:!?])(?=[가-힣A-Za-z0-9])", r"\1 ", value)
    value = re.sub(r"^\s*[,.;:]+\s*", "", value)

    # 자동 변환에서 자주 생기는 한국어 띄어쓰기 오류를 보정합니다.
    replacements = (
        ("하기어려", "하기 어려"),
        ("하기쉽", "하기 쉽"),
        ("할수있", "할 수 있"),
        ("할수없", "할 수 없"),
        ("될수있", "될 수 있"),
        ("될수없", "될 수 없"),
        ("수립ㆍ시행", "수립·시행"),
        ("청문ㆍ동의", "청문·동의"),
        ("검토ㆍ평가", "검토·평가"),
    )
    for old, new in replacements:
        value = value.replace(old, new)

    value = re.sub(r"\s{2,}", " ", value)
    return value.strip()


def easy_korean(sentence: str) -> str:
    value = remove_clause_reference(sentence)
    value = re.sub(
        r"^\s*(그러나|그런데|이에|한편|특히)\s*,?\s*",
        "",
        value,
    )
    value = normalize_korean_sentence(value).strip(" .")

    replacements = (
        ("할 수 있도록 하고자 함", "할 수 있게 바꾸려는 내용입니다"),
        ("하도록 하고자 함", "하도록 바꾸려는 내용입니다"),
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

    value = normalize_korean_sentence(value)
    if not re.search(
        r"(입니다|습니다|합니다|됩니다|있습니다|없습니다|어렵습니다|내용입니다|상황입니다)[.!?]?$",
        value,
    ):
        value += "."

    if not value.endswith((".", "!", "?")):
        value += "."

    value = re.sub(r"\.{2,}", ".", value)
    return normalize_korean_sentence(value)


def sentence_without_period(value: str) -> str:
    return normalize_korean_sentence(value).rstrip(" .!?").strip()


def text_tokens(value: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[가-힣A-Za-z0-9]{2,}", value)
        if token not in {
            "공식", "요약", "확인", "필요", "검토", "법안",
            "가능성", "문제", "내용", "제도",
        }
    }


def dedupe_similar(items: list[str], limit: int = 4) -> list[str]:
    result: list[str] = []
    token_sets: list[set[str]] = []

    for item in items:
        value = normalize_korean_sentence(item)
        if not value:
            continue
        tokens = text_tokens(value)
        duplicated = False

        for old_tokens in token_sets:
            if not tokens or not old_tokens:
                continue
            similarity = len(tokens & old_tokens) / max(1, len(tokens | old_tokens))
            if similarity >= 0.42:
                duplicated = True
                break

        if not duplicated and value not in result:
            result.append(value)
            token_sets.append(tokens)

        if len(result) >= limit:
            break

    return result



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



POLICY_PROFILE_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "appointment_governance",
        "인사·권력구조",
        (
            "임명", "선임", "추천", "동의", "청문", "해임",
            "기관장", "교육감", "위원장", "위원",
        ),
    ),
    (
        "data_security",
        "개인정보·정보보안",
        (
            "개인정보", "정보시스템", "정보통신망", "보안",
            "데이터", "전산", "해킹", "정보보호",
        ),
    ),
    (
        "subsidy_fiscal",
        "재정·지원배분",
        (
            "지원", "보조금", "급여", "수당", "감면", "기금",
            "재정", "예산", "융자", "세액공제",
        ),
    ),
    (
        "punitive_regulation",
        "규제·제재",
        (
            "벌칙", "과태료", "처벌", "제재", "금지", "허가",
            "등록", "신고", "취소", "영업정지",
        ),
    ),
    (
        "organization_power",
        "행정조직·권한배분",
        (
            "위원회", "기관", "센터", "기구", "설치", "지정",
            "장관", "지방자치단체", "공공기관",
        ),
    ),
    (
        "labor_safety",
        "노동·산업안전",
        (
            "근로자", "노동자", "사용자", "고용", "산업재해",
            "안전", "휴게", "임금", "사업주",
        ),
    ),
    (
        "housing_property",
        "주거·재산권",
        (
            "주택", "임대", "임차", "토지", "건축", "재산권",
            "분양", "전세", "상가",
        ),
    ),
    (
        "health_care",
        "보건·의료",
        (
            "환자", "의료", "병원", "의사", "간호", "건강",
            "질병", "약품", "보건",
        ),
    ),
    (
        "education_system",
        "교육제도",
        (
            "교육", "학교", "학생", "교원", "대학", "유치원",
            "학습", "입학",
        ),
    ),
)


def score_policy_profiles(title: str, text: str) -> list[dict[str, Any]]:
    context = f"{title} {text}"
    profiles: list[dict[str, Any]] = []

    for key, label, keywords in POLICY_PROFILE_RULES:
        matched = [word for word in keywords if word in context]
        score = len(matched)

        # 법안의 정책수단을 나타내는 단어에는 더 큰 가중치를 줍니다.
        weighted_signals: dict[str, tuple[tuple[str, ...], int]] = {
            "appointment_governance": (
                ("임명", "선임", "추천", "동의", "해임"),
                4,
            ),
            "data_security": (
                ("개인정보", "보안", "정보시스템", "해킹"),
                3,
            ),
            "subsidy_fiscal": (
                ("보조금", "급여", "지원", "감면", "기금"),
                3,
            ),
            "punitive_regulation": (
                ("벌칙", "과태료", "처벌", "영업정지", "허가취소"),
                3,
            ),
            "labor_safety": (
                ("산업재해", "근로자", "임금", "휴게", "사업주"),
                3,
            ),
            "housing_property": (
                ("주택", "임대", "임차", "전세", "재산권"),
                3,
            ),
            "health_care": (
                ("의료", "환자", "병원", "의사", "약품"),
                3,
            ),
            "education_system": (
                ("교육", "학교", "학생", "교원", "입학"),
                2,
            ),
            "organization_power": (
                ("위원회", "기관", "센터", "설치", "지정"),
                2,
            ),
        }

        signals, weight = weighted_signals.get(key, ((), 0))
        if signals and contains_any(context, signals):
            score += weight

        profiles.append(
            {
                "key": key,
                "label": label,
                "score": score,
                "matched_signals": matched[:10],
                "confidence": (
                    "높음" if score >= 6
                    else "보통" if score >= 3
                    else "낮음"
                ),
            }
        )

    profiles.sort(key=lambda item: item["score"], reverse=True)
    return profiles


def detect_policy_profiles(
    title: str,
    text: str,
    max_profiles: int = 3,
) -> list[dict[str, Any]]:
    scored = score_policy_profiles(title, text)
    selected: list[dict[str, Any]] = []

    for profile in scored:
        if profile["score"] <= 0:
            continue

        # 주유형은 항상 선택합니다. 부유형은 최소한 독립적인 신호가 있어야 합니다.
        if not selected:
            selected.append(profile)
        elif profile["score"] >= 3:
            selected.append(profile)

        if len(selected) >= max_profiles:
            break

    if not selected:
        selected = [
            {
                "key": "general_reform",
                "label": "일반 제도개편",
                "score": 0,
                "matched_signals": [],
                "confidence": "낮음",
            }
        ]

    for index, profile in enumerate(selected):
        profile["role"] = "주유형" if index == 0 else f"부유형 {index}"

    return selected


def detect_policy_profile(title: str, text: str) -> dict[str, Any]:
    """기존 필드와의 호환을 위해 주유형 하나를 반환합니다."""
    return detect_policy_profiles(title, text, max_profiles=1)[0]


def merge_dict_items(
    groups: list[list[dict[str, Any]]],
    identity_keys: tuple[str, ...],
    limit: int | None = None,
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[tuple[str, ...]] = set()

    for group in groups:
        for item in group:
            identity = tuple(
                normalize_korean_sentence(str(item.get(key, "")))
                for key in identity_keys
            )
            if identity in seen:
                continue
            seen.add(identity)
            result.append(item)

            if limit and len(result) >= limit:
                return result

    return result


def attach_profile(
    items: list[dict[str, Any]],
    profile: dict[str, Any],
) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for item in items:
        copied = dict(item)
        copied["profile_key"] = profile["key"]
        copied["profile_label"] = profile["label"]
        copied["profile_role"] = profile.get("role", "")
        enriched.append(copied)
    return enriched



def deep_case(
    title: str,
    attacker: str,
    incentive: str,
    path: str,
    harmed: str,
    warning: str,
    defense: str,
    residual: str,
    severity: str = "높음",
) -> dict[str, str]:
    return {
        "title": title,
        "attacker": attacker,
        "incentive": incentive,
        "path": path,
        "harmed": harmed,
        "warning": warning,
        "defense": defense,
        "residual": residual,
        "severity": severity,
    }


def profile_assumptions(profile: str) -> list[dict[str, str]]:
    common = [
        {
            "assumption": "법안이 지목한 문제가 실제로 제안된 원인 때문에 발생한다.",
            "why_critical": "원인 진단이 틀리면 제도는 바뀌지만 문제는 남습니다.",
            "disproof": "독립 통계나 비교집단에서 다른 원인이 더 크게 설명될 경우",
        },
        {
            "assumption": "새 권한이나 의무를 맡을 기관에 인력·예산·전문성이 있다.",
            "why_critical": "집행역량이 없으면 법률은 서류상 의무로만 남습니다.",
            "disproof": "담당 인력·예산·처리기간 추계가 없거나 기존 업무도 적체된 경우",
        },
    ]

    specific: dict[str, list[dict[str, str]]] = {
        "appointment_governance": [
            {
                "assumption": "임명·동의 절차가 직접선거보다 전문성과 중립성을 더 잘 검증한다.",
                "why_critical": "절차 변경의 핵심 정당화 근거입니다.",
                "disproof": "후보 추천과 동의가 정파적 거래로 운영되거나 검증자료가 비공개인 경우",
            },
            {
                "assumption": "임명 이후에도 임명권자로부터 독립적으로 직무를 수행할 수 있다.",
                "why_critical": "전문성보다 정치적 종속이 커지는 역효과를 막아야 합니다.",
                "disproof": "해임권·예산권·재임명권이 한 권력에 집중된 경우",
            },
            {
                "assumption": "주민의 직접 통제 상실을 다른 책임성과 공개성 장치가 보완한다.",
                "why_critical": "민주적 정당성의 손실을 상쇄할 장치가 필요합니다.",
                "disproof": "청문·평가·해임사유·회의록 공개가 약하거나 없는 경우",
            },
        ],
        "data_security": [
            {
                "assumption": "법정 의무가 실제 보안투자와 취약점 개선으로 이어진다.",
                "why_critical": "계획서 작성만 늘어나는 형식적 준수를 막아야 합니다.",
                "disproof": "취약점·사고율은 그대로인데 계획서 제출률만 오르는 경우",
            },
            {
                "assumption": "추가 정보 접근권한이 보안 목적을 넘어 사용되지 않는다.",
                "why_critical": "보안 강화가 과잉감시로 변질될 수 있습니다.",
                "disproof": "목적 외 조회, 장기보관, 광범위한 내부 접근이 허용되는 경우",
            },
            {
                "assumption": "기관과 외주업체 사이의 책임 경계가 명확하다.",
                "why_critical": "사고가 나면 책임 공백과 복구 지연이 발생할 수 있습니다.",
                "disproof": "계약·법률상 사고통지와 손해책임이 서로 전가되는 경우",
            },
        ],
        "subsidy_fiscal": [
            {
                "assumption": "지원대상 기준이 실제 필요도를 정확히 구분한다.",
                "why_critical": "누락과 부정수급을 동시에 줄여야 합니다.",
                "disproof": "탈락오류·중복수혜·소득역전 현상이 크게 나타나는 경우",
            },
            {
                "assumption": "지원이 목표행동이나 생활개선을 실제로 유발한다.",
                "why_critical": "현금·보조금 지급 자체가 성과는 아닙니다.",
                "disproof": "비교집단 대비 핵심 성과가 개선되지 않는 경우",
            },
            {
                "assumption": "재원이 장기간 지속 가능하다.",
                "why_critical": "중단되면 수혜자의 의존과 행정 혼란이 커질 수 있습니다.",
                "disproof": "수요 증가율이 재원 증가율을 지속적으로 초과하는 경우",
            },
        ],
        "punitive_regulation": [
            {
                "assumption": "위반행위를 명확하고 일관되게 식별할 수 있다.",
                "why_critical": "모호한 기준은 선택적 집행과 과잉제재를 만듭니다.",
                "disproof": "기관별 처분 편차와 불복 인용률이 높은 경우",
            },
            {
                "assumption": "제재가 회피·음성화보다 준수를 더 많이 유도한다.",
                "why_critical": "강한 처벌이 항상 높은 준수율로 이어지지는 않습니다.",
                "disproof": "명의분산·업종전환·비공식시장 확대가 나타나는 경우",
            },
            {
                "assumption": "영세주체와 대규모 주체에 대한 부담이 비례적이다.",
                "why_critical": "동일 제재가 실질적으로 불균형한 피해를 줄 수 있습니다.",
                "disproof": "작은 사업자만 시장에서 퇴출되고 대형주체는 비용으로 흡수하는 경우",
            },
        ],
        "organization_power": [
            {
                "assumption": "새 조직이 기존 기관의 기능중복을 줄이고 책임을 명확히 한다.",
                "why_critical": "조직 신설이 문제해결보다 예산과 절차만 늘릴 수 있습니다.",
                "disproof": "동일 업무를 여러 기관이 수행하고 실패 책임이 분산되는 경우",
            },
            {
                "assumption": "새 권한의 범위가 법률에서 충분히 제한된다.",
                "why_critical": "하위법령과 내부지침으로 권한이 계속 확장될 수 있습니다.",
                "disproof": "핵심 기준이 ‘필요한 사항’ 형태로 광범위하게 위임되는 경우",
            },
        ],
    }

    return common + specific.get(
        profile,
        [
            {
                "assumption": "제안된 수단이 다른 대안보다 비용 대비 효과가 높다.",
                "why_critical": "법률 개정이 가장 적합한 수단인지 확인해야 합니다.",
                "disproof": "기존 제도 보완이나 비규제 대안이 더 낮은 비용으로 같은 효과를 내는 경우",
            },
            {
                "assumption": "예상하지 못한 부작용을 발견하고 수정할 수 있다.",
                "why_critical": "정책 실패가 고착되는 것을 막아야 합니다.",
                "disproof": "평가·일몰·재승인·중단 조건이 없는 경우",
            },
        ],
    )


def profile_actor_incentives(
    profile: str,
    actors: list[str],
) -> list[dict[str, str]]:
    templates: dict[str, list[dict[str, str]]] = {
        "appointment_governance": [
            {
                "actor": "임명권자",
                "formal_role": "후보 지명 또는 임명",
                "likely_incentive": "정책 방향이 맞는 인사와 책임공유가 쉬운 인사를 선호",
                "gaming_risk": "전문성 기준을 이용해 사실상 정치적 충성도를 선별",
            },
            {
                "actor": "동의·청문기관",
                "formal_role": "후보 검증과 견제",
                "likely_incentive": "정파적 협상, 인사교환 또는 책임 회피",
                "gaming_risk": "청문을 공개 검증이 아닌 정치적 거래로 형식화",
            },
            {
                "actor": "후보자",
                "formal_role": "전문성과 독립성 입증",
                "likely_incentive": "임명권자와 동의권자 모두에게 수용 가능한 태도",
                "gaming_risk": "선임 전에는 중립을 약속하고 선임 후 특정 세력에 종속",
            },
            {
                "actor": "주민·이용자",
                "formal_role": "정책의 최종 영향을 받음",
                "likely_incentive": "독립성·서비스 품질·책임성 확보",
                "gaming_risk": "직접 선택권은 줄지만 실질적 이의제기 수단도 약할 수 있음",
            },
        ],
        "data_security": [
            {
                "actor": "공공기관 경영진",
                "formal_role": "보안책임과 예산배분",
                "likely_incentive": "사고 방지와 동시에 비용·평판손실 최소화",
                "gaming_risk": "실제 취약점보다 서류상 준수와 사고 은폐에 집중",
            },
            {
                "actor": "보안 담당자",
                "formal_role": "취약점 관리와 사고 대응",
                "likely_incentive": "권한·인력·예산 확보",
                "gaming_risk": "과도한 접근권한이나 광범위한 통제를 보안 명분으로 요구",
            },
            {
                "actor": "외주·보안업체",
                "formal_role": "시스템 구축·점검",
                "likely_incentive": "계약 확대와 책임 제한",
                "gaming_risk": "복잡한 기준과 독점적 기술의존을 만들어 장기계약 유도",
            },
            {
                "actor": "국민·정보주체",
                "formal_role": "정보 제공과 피해 부담",
                "likely_incentive": "최소수집·안전·신속한 피해구제",
                "gaming_risk": "사고 사실과 위험을 제때 알지 못할 수 있음",
            },
        ],
        "subsidy_fiscal": [
            {
                "actor": "수혜 신청자",
                "formal_role": "자격 입증과 지원 사용",
                "likely_incentive": "지원 극대화와 자격 유지",
                "gaming_risk": "소득·재산·사업구조를 형식적으로 조정",
            },
            {
                "actor": "집행기관",
                "formal_role": "대상선정·지급·환수",
                "likely_incentive": "신속 집행과 감사위험 최소화",
                "gaming_risk": "정교한 판단보다 서류가 완벽한 신청자를 선호",
            },
            {
                "actor": "중개기관·공급자",
                "formal_role": "지원서비스 제공",
                "likely_incentive": "지원단가와 이용자 수 확대",
                "gaming_risk": "필요하지 않은 서비스 공급과 가격 부풀리기",
            },
            {
                "actor": "납세자·비수혜자",
                "formal_role": "재원 부담",
                "likely_incentive": "형평성과 재정 지속성",
                "gaming_risk": "혜택과 부담의 분리가 커지면 제도 신뢰가 약화",
            },
        ],
        "punitive_regulation": [
            {
                "actor": "규제기관",
                "formal_role": "위반 조사와 제재",
                "likely_incentive": "집행성과와 사고책임 회피",
                "gaming_risk": "측정하기 쉬운 위반만 집중하거나 특정 집단을 선택적으로 단속",
            },
            {
                "actor": "규제대상",
                "formal_role": "의무 준수",
                "likely_incentive": "준수비용과 제재위험 최소화",
                "gaming_risk": "명의·업종·계약형태를 바꾸어 규제범위 우회",
            },
            {
                "actor": "신고자·피해자",
                "formal_role": "위반정보 제공과 구제 청구",
                "likely_incentive": "신속한 보호와 보상",
                "gaming_risk": "보복 우려나 복잡한 입증책임 때문에 신고 포기",
            },
            {
                "actor": "경쟁사업자",
                "formal_role": "시장 참여",
                "likely_incentive": "경쟁자의 규제비용 증가",
                "gaming_risk": "규제·신고절차를 경쟁자 압박수단으로 활용",
            },
        ],
    }

    base = templates.get(profile, [])
    if base:
        return base

    result = [
        {
            "actor": actor,
            "formal_role": "법안의 직접 또는 간접 영향주체",
            "likely_incentive": "자신의 권리·비용·권한에 유리한 방향으로 제도에 적응",
            "gaming_risk": "법률상 형식과 실제 행동 사이의 차이를 이용할 가능성",
        }
        for actor in actors[:4]
    ]
    return result or [
        {
            "actor": "집행기관",
            "formal_role": "법률 집행",
            "likely_incentive": "업무량과 책임위험 최소화",
            "gaming_risk": "실질 성과보다 서류상 준수에 집중",
        },
        {
            "actor": "정책 대상",
            "formal_role": "새 권리·의무의 적용을 받음",
            "likely_incentive": "혜택 극대화 또는 부담 최소화",
            "gaming_risk": "정의·예외·경계조건을 이용해 적용을 우회",
        },
    ]


def profile_deep_red_team(profile: str) -> list[dict[str, str]]:
    cases: dict[str, list[dict[str, str]]] = {
        "appointment_governance": [
            deep_case(
                "추천단계 선점",
                "정당·이해관계단체·임명권자 측근",
                "최종 임명 전에 후보군 자체를 유리하게 좁히려는 유인",
                "추천위원 구성과 자격기준을 장악해 경쟁 가능한 후보를 사전에 배제",
                "독립적 후보, 소수 견해, 주민의 선택 가능성",
                "후보군의 경력·성향이 반복적으로 동일하고 탈락사유가 비공개",
                "복수기관 추천, 공개모집, 평가표·탈락사유 공개, 이해충돌 회피",
                "공개 절차가 있어도 비공식 사전조율과 네트워크 포획은 남을 수 있음",
            ),
            deep_case(
                "청문·동의의 거래화",
                "임명권자와 의회 다수파",
                "다른 예산·인사·정책과 묶어 인사를 교환하려는 유인",
                "후보 능력보다 정파 간 협상결과로 동의 여부를 결정",
                "독립성, 정책 전문성, 야당·소수파의 검증권",
                "청문질문이 정책검증보다 정쟁에 치우치고 핵심자료가 늦게 제출됨",
                "자료제출 시한, 독립 검증보고서, 가중정족수, 소수의견 공개",
                "가중정족수가 오히려 장기 공석과 거래비용을 높일 수 있음",
            ),
            deep_case(
                "해임·예산을 통한 사후통제",
                "임명권자·예산편성기관",
                "임명 후에도 정책노선을 통제하려는 유인",
                "해임 가능성, 재임명, 예산삭감과 조직통제를 이용해 독립성을 약화",
                "기관장 독립성, 장기정책, 반대 의견을 내는 조직 구성원",
                "임명권자 교체 뒤 정책·인사·예산이 급격히 변경됨",
                "법정 임기, 한정된 해임사유, 독립예산, 재임명 제한, 사법적 불복",
                "법정 독립성이 있어도 인사·정보·조직문화의 비공식 압력은 남음",
            ),
            deep_case(
                "주민책임성 공백",
                "모든 제도 참여자",
                "실패 책임을 다른 기관에 넘기려는 유인",
                "단체장은 의회 동의를, 의회는 단체장 지명을 이유로 책임을 분산",
                "주민, 학생·학부모, 서비스 이용자",
                "정책 실패 후 어느 기관도 해임·평가·사과 책임을 지지 않음",
                "책임기관 단일표시, 공개성과계약, 주민청원·소환·평가절차",
                "책임명시는 가능하지만 실제 선거에서 책임이 분산될 가능성은 남음",
            ),
        ],
        "data_security": [
            deep_case(
                "서류상 보안",
                "기관 경영진·감사 대응 조직",
                "실제 개선보다 법적 책임을 피하려는 유인",
                "계획서·점검표·교육실적을 채우되 취약한 시스템과 인력문제는 방치",
                "국민정보, 현장 담당자, 필수 공공서비스",
                "점검점수는 높지만 반복 취약점과 사고가 줄지 않음",
                "결과지표, 침투시험, 미조치 취약점 공개, 반복 미달 시 책임",
                "공격기법이 변하므로 결과지표만으로 모든 위험을 포착할 수 없음",
            ),
            deep_case(
                "사고 은폐·축소",
                "기관 경영진·외주업체",
                "평판·제재·계약손실을 줄이려는 유인",
                "사고 범위를 축소 분류하고 신고시점을 늦추며 피해자 통지를 최소화",
                "정보주체와 연계기관",
                "사고 최초 발견과 공식 신고 사이의 시간이 길고 사고등급이 반복 하향",
                "법정 통지시한, 독립 신고채널, 내부고발 보호, 사고등급 사후감사",
                "초기에는 사고범위를 알기 어려워 과소·과대신고 논란이 남음",
            ),
            deep_case(
                "보안 명분의 과잉감시",
                "보안부서·수사·관리기관",
                "더 넓은 데이터와 접근권한을 확보하려는 유인",
                "최소수집 원칙 없이 행동로그·개인정보를 장기 보관하고 목적 외 이용",
                "직원·국민의 사생활과 표현의 자유",
                "접근계정과 보유데이터는 계속 늘지만 삭제·목적제한 기록은 없음",
                "최소수집, 목적제한, 자동파기, 접근기록, 독립 개인정보 영향평가",
                "비식별·보안목적이라는 명칭으로 실질적 재식별 위험이 남음",
            ),
            deep_case(
                "외주업체 책임회피",
                "시스템 구축·클라우드·보안업체",
                "비용과 손해배상 책임을 기관에 전가하려는 유인",
                "계약상 책임범위를 좁히고 하도급 구조로 실제 통제주체를 불명확하게 함",
                "공공기관과 피해 국민",
                "사고 후 계약당사자·하도급사 간 책임공방이 장기화",
                "공급망 목록, 공동책임, 하도급 공개, 보안의무 승계, 보험·배상기준",
                "대형 공급자의 시장지배력 때문에 계약조건 개선이 제한될 수 있음",
            ),
        ],
        "subsidy_fiscal": [
            deep_case(
                "자격기준 맞추기",
                "신청자·대행업체",
                "실질 필요보다 형식적 자격을 충족하려는 유인",
                "소득·재산·사업체·가구구성을 일시적으로 조정하거나 명의를 분산",
                "정직한 신청자, 더 취약하지만 기준 밖에 있는 사람, 납세자",
                "특정 기준선 바로 아래에 신청자가 비정상적으로 집중",
                "기간평균, 실질지배 확인, 중복검증, 사후환수와 선의의 오류 구분",
                "강한 검증은 개인정보 침해와 정당한 수혜자의 위축을 낳을 수 있음",
            ),
            deep_case(
                "공급자 포획",
                "서비스 제공기관·중개업체",
                "지원예산을 가격과 이용량 확대로 흡수하려는 유인",
                "지원금만큼 가격을 올리거나 불필요한 서비스를 묶어 판매",
                "수혜자와 납세자, 신규 경쟁자",
                "지원 확대 뒤 실질 이용량보다 단가와 업체매출만 빠르게 증가",
                "가격공개, 경쟁입찰, 이용자 선택, 성과연동 지급, 부당청구 환수",
                "가격통제가 품질저하와 공급자 이탈을 만들 수 있음",
            ),
            deep_case(
                "사각지대 고착",
                "집행기관",
                "감사위험을 줄이기 위해 증빙이 쉬운 신청만 승인하려는 유인",
                "서류가 부족한 취약계층을 반복 탈락시키고 미신청자를 방치",
                "고령자·장애인·이주민·위기가구 등 정보취약계층",
                "예산 집행률은 높지만 취약집단 수혜율이 낮음",
                "자동발굴, 찾아가는 신청, 대리신청, 간이증빙, 이의신청 지원",
                "자동발굴 과정에서 잘못된 낙인과 개인정보 결합위험이 남음",
            ),
            deep_case(
                "재정의 영구고착",
                "정치권·수혜집단·공급기관",
                "한번 생긴 혜택과 조직을 유지·확대하려는 유인",
                "효과가 불분명해도 수혜축소의 정치비용 때문에 사업을 계속 확대",
                "미래 납세자와 다른 정책분야",
                "성과자료 없이 대상·단가·조직이 매년 확대",
                "일몰, 재승인, 대안비교, 자동조정, 재정상한과 종료지원",
                "일몰 직전 정치적 연장과 단기성과 조작이 가능",
            ),
        ],
        "punitive_regulation": [
            deep_case(
                "선택적 집행",
                "규제기관·정치권",
                "눈에 띄는 대상이나 불리한 집단을 집중 단속하려는 유인",
                "모호한 기준과 재량을 이용해 유사 위반을 다르게 처분",
                "소수사업자, 비판집단, 규제 예측가능성",
                "기관·지역·대상별 처분률과 제재수준 편차가 큼",
                "명확한 요건, 처분사유 공개, 무작위 사건배당, 불복통계, 외부감사",
                "사건 특성 차이 때문에 통계만으로 차별집행을 확정하기 어려움",
            ),
            deep_case(
                "규제범위 우회",
                "규제대상 사업자",
                "의무와 제재비용을 피하려는 유인",
                "명의·법인·고용형태·상품명·계약구조를 바꾸어 법적 정의 밖으로 이동",
                "준수사업자, 근로자·소비자, 세수",
                "법 시행 뒤 유사업종·특수고용·위탁계약이 급증",
                "실질우선 기준, 특수관계인 합산, 우회행위 조항, 정기 정의 갱신",
                "실질기준이 넓어지면 법적 예측가능성이 낮아질 수 있음",
            ),
            deep_case(
                "영세주체 퇴출",
                "규제 설계자·대형사업자",
                "고정 준수비용을 경쟁장벽으로 활용하려는 유인",
                "대형사업자는 비용을 흡수하지만 영세주체는 시장에서 퇴출",
                "영세사업자, 지역소비자, 신규진입자",
                "위반 감소보다 사업자 수 감소와 시장집중도가 빠르게 증가",
                "규모별 단계, 기술지원, 유예, 위험비례 의무, 간소 준수경로",
                "차등규제가 영세사업자의 낮은 안전·품질을 고착시킬 수 있음",
            ),
            deep_case(
                "신고·제재의 경쟁무기화",
                "경쟁사업자·분쟁당사자",
                "상대의 영업과 평판을 저비용으로 훼손하려는 유인",
                "반복 신고와 임시조치를 이용해 최종 판단 전 시장에서 배제",
                "정상 사업자와 행정자원",
                "동일 신고인의 반복 사건과 기각률이 높음",
                "악의적 신고 제재, 신속 사전심사, 반론권, 임시조치 비례성",
                "악의성 판단이 정당한 내부고발과 신고를 위축시킬 수 있음",
            ),
        ],
    }

    return cases.get(
        profile,
        [
            deep_case(
                "목표와 수단의 불일치",
                "정책 추진기관",
                "가시적인 제도 변경으로 성과를 보여주려는 유인",
                "문제의 근본원인보다 법률로 바꾸기 쉬운 절차·조직·의무만 확대",
                "정책 대상과 납세자",
                "제도 변경은 완료됐지만 핵심 결과지표가 개선되지 않음",
                "원인모형 공개, 대안비교, 시범사업, 기준선·중단조건",
                "사회문제의 다원적 원인 때문에 단일 성과지표는 한계가 있음",
            ),
            deep_case(
                "책임의 분산",
                "관련 기관들",
                "실패 책임을 다른 주체에 전가하려는 유인",
                "권한은 공유하지만 최종 책임기관과 처리기한은 불명확하게 설계",
                "민원인과 정책 대상",
                "기관 간 이송·협의·검토가 반복되고 처리기간이 증가",
                "단일 책임기관, 역할표, 법정 처리기한, 실패책임 공개",
                "복합정책에서는 완전한 단일책임 구조가 어려울 수 있음",
            ),
            deep_case(
                "성과지표 조작",
                "집행기관·수탁기관",
                "예산과 조직을 유지하기 위해 성과를 높게 보이려는 유인",
                "쉬운 대상만 선택하거나 측정지표를 실제 목적과 분리",
                "어려운 대상과 장기적 정책효과",
                "보고지표는 개선되지만 민원·피해·현장평가는 나빠짐",
                "복수지표, 부작용지표, 원자료 공개, 독립평가, 표본감사",
                "평가지표가 많아지면 행정부담과 책임분산이 증가",
            ),
        ],
    )


def profile_second_order_effects(profile: str) -> list[dict[str, str]]:
    effects: dict[str, list[dict[str, str]]] = {
        "appointment_governance": [
            {
                "effect": "정책의 장기성 또는 급격한 정권종속",
                "direction": "양면",
                "mechanism": "임기와 임명주기 설계에 따라 장기 전문성이 생기거나 선거주기마다 정책이 급변",
                "monitor": "임명권자 교체 전후 정책·인사·예산 변경 폭",
            },
            {
                "effect": "후보자 시장의 축소",
                "direction": "위험",
                "mechanism": "공개 선거보다 제한된 추천 네트워크가 후보 진입을 좌우",
                "monitor": "후보군의 직업·지역·성별·경력 다양성",
            },
            {
                "effect": "정당책임의 명확화 가능성",
                "direction": "기회",
                "mechanism": "임명 주체가 명확하면 실패에 대한 정치적 책임을 묻기 쉬울 수 있음",
                "monitor": "성과공개와 선거에서의 책임귀속 여부",
            },
            {
                "effect": "교육·행정의 중앙 또는 지방권력 종속",
                "direction": "위험",
                "mechanism": "독립기관이 단체장·의회의 정책연합에 포함",
                "monitor": "독립적 반대의견, 감사결과, 인사교체 빈도",
            },
        ],
        "data_security": [
            {
                "effect": "보안시장과 외주 의존 확대",
                "direction": "양면",
                "mechanism": "의무 강화로 전문서비스 수요가 늘지만 특정 업체 종속도 커짐",
                "monitor": "공급자 집중도·계약기간·교체비용",
            },
            {
                "effect": "서비스 편의성 저하",
                "direction": "위험",
                "mechanism": "보안절차가 과도하면 시민과 직원의 접근성이 낮아짐",
                "monitor": "처리시간·접근실패·민원 증가",
            },
            {
                "effect": "사고공개의 역설",
                "direction": "양면",
                "mechanism": "투명한 기관이 사고가 많아 보이고 은폐기관이 안전해 보일 수 있음",
                "monitor": "신고율과 실제 취약점 점검결과의 차이",
            },
            {
                "effect": "공공데이터 활용 위축",
                "direction": "위험",
                "mechanism": "책임회피를 위해 정당한 데이터 공유까지 과도하게 제한",
                "monitor": "연구·행정 데이터 제공 거절률",
            },
        ],
        "subsidy_fiscal": [
            {
                "effect": "가격 인상과 지원금 자본화",
                "direction": "위험",
                "mechanism": "공급이 제한된 시장에서 지원금이 가격상승으로 흡수",
                "monitor": "지원 전후 가격·임대료·서비스단가",
            },
            {
                "effect": "근로·소득 증가의 역유인",
                "direction": "위험",
                "mechanism": "자격 기준선에서 추가소득보다 혜택 상실이 더 큼",
                "monitor": "기준선 주변 소득분포와 수급탈락 후 생활변화",
            },
            {
                "effect": "행정자료 통합의 확대",
                "direction": "양면",
                "mechanism": "정밀선별을 위해 개인정보 결합이 증가",
                "monitor": "연계데이터 항목·오류정정·접근기록",
            },
            {
                "effect": "지역·계층별 공급격차",
                "direction": "위험",
                "mechanism": "지원은 같아도 서비스 공급이 부족한 지역은 혜택을 사용하지 못함",
                "monitor": "지역별 이용률·대기시간·공급자 수",
            },
        ],
        "punitive_regulation": [
            {
                "effect": "시장집중",
                "direction": "위험",
                "mechanism": "고정 준수비용이 영세주체에 더 크게 작용",
                "monitor": "사업자 수·진입률·상위기업 점유율",
            },
            {
                "effect": "음성화·비공식화",
                "direction": "위험",
                "mechanism": "제재가 높을수록 신고·등록 밖으로 이동할 유인 증가",
                "monitor": "무등록 거래·현금거래·특수계약 증가",
            },
            {
                "effect": "규제 신뢰 상승 가능성",
                "direction": "기회",
                "mechanism": "명확하고 일관된 집행은 피해예방과 시장신뢰를 높임",
                "monitor": "피해율·재범률·불복률·소비자 신뢰",
            },
            {
                "effect": "과잉준수와 혁신 위축",
                "direction": "위험",
                "mechanism": "모호한 제재를 피하기 위해 합법적 신제품·표현·서비스까지 중단",
                "monitor": "신규사업·허가문의·사전질의 증가",
            },
        ],
    }

    return effects.get(
        profile,
        [
            {
                "effect": "행정부담 증가",
                "direction": "위험",
                "mechanism": "새 의무·보고·협의절차가 기존 업무와 중첩",
                "monitor": "처리시간·인력·보고서 수·민원 적체",
            },
            {
                "effect": "책임성과 투명성 개선 가능성",
                "direction": "기회",
                "mechanism": "권한과 의무가 명확히 규정되면 추적이 쉬워짐",
                "monitor": "책임기관 표시·성과공개·정정 속도",
            },
            {
                "effect": "지역·기관별 격차",
                "direction": "위험",
                "mechanism": "같은 법률도 자원과 역량 차이로 다르게 집행",
                "monitor": "지역별 처리기간·성과·민원·예산",
            },
            {
                "effect": "법률과 현실의 괴리",
                "direction": "위험",
                "mechanism": "현장 유인과 맞지 않는 의무는 형식적 준수로 전환",
                "monitor": "서류상 준수율과 실제 결과의 차이",
            },
        ],
    )


def profile_stress_tests(profile: str) -> list[dict[str, str]]:
    common = [
        {
            "scenario": "예산과 인력이 계획의 절반만 확보되는 경우",
            "question": "핵심 기능을 유지하려면 무엇을 우선하고 무엇을 중단할 것인가?",
            "pass_condition": "우선순위·최소서비스·단계시행 기준이 법률 또는 계획에 명시",
        },
        {
            "scenario": "집행기관이 법의 취지에 소극적이거나 반대하는 경우",
            "question": "의도적 지연·최소이행·책임회피를 어떻게 발견하고 교정하는가?",
            "pass_condition": "처리기한·공개지표·독립감사·이의제기·시정명령이 존재",
        },
        {
            "scenario": "가장 영리한 이해관계자가 법의 경계조건을 이용하는 경우",
            "question": "명의·계약·조직·데이터 형식을 바꿔도 실질적으로 적용되는가?",
            "pass_condition": "실질우선·우회행위·특수관계·정기 정의갱신 규정이 존재",
        },
        {
            "scenario": "정책이 예상과 반대로 피해를 키우는 경우",
            "question": "피해를 얼마나 빨리 발견하고 중단·축소·보상할 수 있는가?",
            "pass_condition": "부작용지표·중단조건·일몰·피해구제와 책임주체가 명시",
        },
    ]

    extra: dict[str, dict[str, str]] = {
        "appointment_governance": {
            "scenario": "임명권자와 의회 다수파가 같은 정치세력인 경우",
            "question": "실질적 견제가 사라져도 독립성과 후보 다양성이 유지되는가?",
            "pass_condition": "외부추천·소수의견·가중동의·임기분산·독립해임심사가 존재",
        },
        "data_security": {
            "scenario": "대규모 침해사고가 야간·휴일에 외주업체에서 발생한 경우",
            "question": "누가 즉시 차단·신고·통지·복구하고 비용을 부담하는가?",
            "pass_condition": "시간별 대응책임·통지시한·공동책임·피해지원 절차가 존재",
        },
        "subsidy_fiscal": {
            "scenario": "신청자가 예상보다 두 배 늘고 재원은 그대로인 경우",
            "question": "대상 축소·단가 조정·대기·추가재원 중 어떤 규칙을 적용하는가?",
            "pass_condition": "자동조정·우선순위·재정상한·의회 재승인 규칙이 존재",
        },
        "punitive_regulation": {
            "scenario": "동일 위반을 대기업과 영세사업자가 저지른 경우",
            "question": "억지력과 생존영향을 모두 고려한 비례적 제재가 가능한가?",
            "pass_condition": "피해·매출·고의·반복성에 따른 단계적 제재기준이 존재",
        },
    }

    if profile in extra:
        return common + [extra[profile]]
    return common


def profile_evidence_tests(profile: str) -> list[dict[str, str]]:
    common = [
        {
            "claim": "문제의 규모가 법 개정을 정당화할 만큼 크다.",
            "required_evidence": "최근 3~5년 공식 통계, 분모가 있는 발생률, 지역·집단별 분포",
            "rejection_condition": "절대건수만 제시되거나 추세·비교집단에서 문제가 확인되지 않음",
        },
        {
            "claim": "제안된 수단이 문제의 원인에 직접 작용한다.",
            "required_evidence": "인과경로, 기존 제도 실패자료, 국내외 비교와 대안비교",
            "rejection_condition": "수단이 원인보다 결과나 행정절차만 바꾸고 대안비교가 없음",
        },
        {
            "claim": "편익이 행정·재정·권리비용보다 크다.",
            "required_evidence": "비용추계, 인력·시스템 구축비, 권리영향과 기회비용",
            "rejection_condition": "직접예산만 계산하고 민간·지방·권리비용을 누락",
        },
    ]

    specific: dict[str, list[dict[str, str]]] = {
        "appointment_governance": [
            {
                "claim": "임명제가 직선제보다 전문성과 중립성을 높인다.",
                "required_evidence": "선출·임명 방식별 성과, 부패·정치편향, 후보 다양성, 책임성 비교",
                "rejection_condition": "국가·지역 맥락을 통제하지 않은 단순 해외사례 나열",
            },
            {
                "claim": "청문과 의회 동의가 실질적 견제가 된다.",
                "required_evidence": "후보 탈락률, 자료공개, 정파별 표결, 임명 후 독립성 사례",
                "rejection_condition": "동의율이 거의 100%이고 검증자료·소수의견이 비공개",
            },
        ],
        "data_security": [
            {
                "claim": "법정 보안의무가 실제 사고와 취약점을 줄인다.",
                "required_evidence": "의무 도입 전후 취약점 조치시간, 사고율, 피해규모, 독립점검 결과",
                "rejection_condition": "교육·계획서 제출률만 개선되고 사고·취약점 결과는 개선되지 않음",
            },
            {
                "claim": "추가 데이터 처리와 접근권한이 필요한 최소범위다.",
                "required_evidence": "개인정보 영향평가, 대체수단, 보유기간·접근권한 최소화 분석",
                "rejection_condition": "목적범위가 포괄적이고 삭제·이의제기·감사절차가 없음",
            },
        ],
        "subsidy_fiscal": [
            {
                "claim": "지원대상이 실제 필요집단과 일치한다.",
                "required_evidence": "수혜·탈락자의 소득·재산·위험분포, 누락률·부정수급률",
                "rejection_condition": "집행률만 제시하고 사각지대·오선정 자료가 없음",
            },
            {
                "claim": "지원이 가격상승이나 공급자 이익이 아닌 수혜자 편익으로 귀착된다.",
                "required_evidence": "지원 전후 가격·수량·품질·공급자 마진과 비교시장",
                "rejection_condition": "지원액만큼 가격이 오르거나 공급량·품질이 개선되지 않음",
            },
        ],
        "punitive_regulation": [
            {
                "claim": "제재 강화가 위반과 피해를 줄인다.",
                "required_evidence": "제재 전후 위반률·재범률·피해율과 음성화·우회지표",
                "rejection_condition": "단속·처분건수만 늘고 실제 피해와 재범은 줄지 않음",
            },
            {
                "claim": "집행기준이 명확하고 차별 없이 적용된다.",
                "required_evidence": "기관·지역·기업규모별 처분분포, 불복 인용률, 사건배당·감사자료",
                "rejection_condition": "유사사건의 처분편차가 크고 사유공개·불복구제가 약함",
            },
        ],
    }

    return common + specific.get(profile, [])


def profile_design_requirements(profile: str) -> list[str]:
    base = [
        "정책 목표와 기준선을 수치 또는 검증 가능한 문장으로 명시",
        "집행 책임기관, 처리기한, 필요한 인력·예산과 실패 책임을 명시",
        "권리침해·비용전가·지역격차를 측정할 부작용 지표를 별도 공개",
        "시행 후 독립평가, 일몰 또는 재승인, 중단·축소 기준을 마련",
        "이의제기·정정·피해구제와 수정이력 공개절차를 마련",
    ]

    extras: dict[str, list[str]] = {
        "appointment_governance": [
            "추천권·지명권·동의권·해임권을 서로 다른 주체에 분산",
            "후보 평가표, 이해충돌, 탈락사유, 청문자료와 소수의견을 공개",
            "임기와 선거주기를 분리하고 제한된 해임사유와 독립심사를 마련",
        ],
        "data_security": [
            "최소수집·목적제한·보유기간·자동파기·접근기록을 법률 또는 기준에 명시",
            "침해사고 통지시한, 공동책임, 피해지원과 외주 공급망 의무를 명시",
            "서류 제출이 아니라 취약점·사고·복구시간을 측정하는 결과지표를 사용",
        ],
        "subsidy_fiscal": [
            "대상선정의 누락오류·중복수혜·환수와 선의의 오류를 구분",
            "가격·공급자 마진·지역별 이용률을 추적해 지원금 자본화를 감시",
            "재정상한·자동조정·종료지원과 기준선 주변의 역유인을 완화",
        ],
        "punitive_regulation": [
            "고의·피해·반복성·규모에 따른 단계적·비례적 제재",
            "처분사유·기관별 집행통계·불복결과를 개인정보 보호 범위에서 공개",
            "실질우선과 우회행위 조항을 두되 법적 명확성과 사전질의 절차를 보장",
        ],
    }

    return base + extras.get(profile, [])



def additional_profile_assumptions(profile: str) -> list[dict[str, str]]:
    data: dict[str, list[dict[str, str]]] = {
        "labor_safety": [
            {
                "assumption": "법정 의무가 현장의 실제 작업방식과 안전투자로 이어진다.",
                "why_critical": "서류상 교육과 규정만 늘고 위험한 생산압박은 그대로일 수 있습니다.",
                "disproof": "교육 이수율은 높지만 사고·아차사고·작업중지 사용률이 개선되지 않는 경우",
            },
            {
                "assumption": "근로자가 불이익 없이 신고·작업중지·권리구제를 사용할 수 있다.",
                "why_critical": "권리가 있어도 해고·배제 위험이 크면 실제로 작동하지 않습니다.",
                "disproof": "신고자 인사불이익, 하청교체, 계약종료가 반복되는 경우",
            },
            {
                "assumption": "원청·하청·플랫폼 구조에서도 최종 책임주체를 식별할 수 있다.",
                "why_critical": "다단계 계약은 위험과 책임을 가장 약한 주체에게 떠넘길 수 있습니다.",
                "disproof": "사고 뒤 사용자성·지휘감독·비용부담에 대한 책임공방이 장기화되는 경우",
            },
        ],
        "health_care": [
            {
                "assumption": "규제나 지원이 환자의 실제 의료접근성과 건강결과를 개선한다.",
                "why_critical": "기관 수·지급액이 늘어도 대기시간과 치료결과가 나빠질 수 있습니다.",
                "disproof": "환자 접근성·치료결과는 개선되지 않고 청구량이나 행정부담만 증가하는 경우",
            },
            {
                "assumption": "의료인의 행동유인이 과잉·과소진료로 왜곡되지 않는다.",
                "why_critical": "수가·책임·규제는 임상결정을 강하게 바꿉니다.",
                "disproof": "불필요한 검사·회피진료·고위험환자 전원이 증가하는 경우",
            },
            {
                "assumption": "지역·기관 규모에 따른 공급격차를 감당할 수 있다.",
                "why_critical": "같은 의무라도 인력 부족 지역은 서비스를 중단할 수 있습니다.",
                "disproof": "지역별 대기시간·휴폐업·전원율 격차가 확대되는 경우",
            },
        ],
        "housing_property": [
            {
                "assumption": "규제나 지원의 편익이 가격·임대료 상승으로 흡수되지 않는다.",
                "why_critical": "공급이 제한된 시장에서는 혜택이 소유자 가격으로 이전될 수 있습니다.",
                "disproof": "지원 또는 규제 시행 뒤 실질 주거비와 매매·임대가격이 더 빠르게 오르는 경우",
            },
            {
                "assumption": "임대인·임차인·금융기관이 새로운 계약형태로 규제를 우회하지 않는다.",
                "why_critical": "보증금·관리비·특약·법인명의로 부담이 이동할 수 있습니다.",
                "disproof": "관리비·단기계약·보증상품·명의분산이 비정상적으로 증가하는 경우",
            },
            {
                "assumption": "재산권 제한과 주거안정 편익 사이의 비례성이 유지된다.",
                "why_critical": "과도한 제한은 공급축소와 분쟁을 낳고 약한 제한은 보호효과가 없습니다.",
                "disproof": "퇴거·분쟁·공급감소가 보호받은 가구의 편익보다 크게 증가하는 경우",
            },
        ],
        "education_system": [
            {
                "assumption": "제도 변경이 학생의 학습·안전·기회에 실제로 도움이 된다.",
                "why_critical": "조직과 선발방식 변화가 학생 성과와 무관할 수 있습니다.",
                "disproof": "행정지표만 개선되고 학습격차·중도탈락·학교만족도는 악화되는 경우",
            },
            {
                "assumption": "지역·가정배경에 따른 교육격차를 확대하지 않는다.",
                "why_critical": "선택권이나 평가제도는 정보와 자원이 많은 가정에 더 유리할 수 있습니다.",
                "disproof": "소득·지역·장애·이주배경별 접근성과 성과격차가 커지는 경우",
            },
            {
                "assumption": "교원과 학교가 지표 맞추기보다 교육목적에 맞게 행동한다.",
                "why_critical": "평가와 책임 강화는 시험교육·학생선별·기록조작을 유발할 수 있습니다.",
                "disproof": "쉬운 학생 선별, 평가대상 제외, 시험집중과 비교육 업무가 증가하는 경우",
            },
        ],
        "organization_power": [
            {
                "assumption": "새 조직이 기존 기관과 다른 고유 기능을 수행한다.",
                "why_critical": "조직 신설은 쉽게 되지만 중복조직의 폐지는 어렵습니다.",
                "disproof": "기존 부처·위원회와 업무·예산·보고선이 중복되는 경우",
            },
            {
                "assumption": "권한 확대와 함께 독립적 통제와 책임이 강화된다.",
                "why_critical": "권한만 집중되고 감사·불복·정보공개가 약하면 행정권력이 비대해집니다.",
                "disproof": "포괄위임, 비공개 의사결정, 내부 자체평가만 존재하는 경우",
            },
        ],
    }
    return data.get(profile, [])


def additional_profile_attacks(profile: str) -> list[dict[str, str]]:
    data: dict[str, list[dict[str, str]]] = {
        "labor_safety": [
            deep_case(
                "생산압박에 의한 안전규정 무력화",
                "경영진·현장관리자·원청",
                "납기와 비용을 맞추기 위해 안전절차를 축소하려는 유인",
                "위험성평가와 작업중지는 서류에만 두고 실제 작업에서는 속도와 생산량을 우선",
                "현장 근로자·하청근로자·인근 시민",
                "아차사고와 초과근로는 늘지만 공식 위험보고는 감소",
                "작업중지권, 생산지표와 안전지표 분리, 원청책임, 신고자 보호, 불시점검",
                "비공식 압박과 계약갱신을 통한 보복은 계속될 수 있음",
            ),
            deep_case(
                "하청·플랫폼 책임전가",
                "원청·플랫폼·다단계 도급업체",
                "보험·보상·안전투자 비용을 계약상 약자에게 넘기려는 유인",
                "직접 지휘는 유지하면서 사용자성·고용관계·시설책임을 부인",
                "하청·특수고용·이주근로자",
                "사고 뒤 계약관계와 지휘감독 여부를 두고 장기간 책임공방",
                "실질지배 기준, 공동책임, 계약정보 공개, 산재보험 사각지대 제거",
                "실질기준이 넓어질수록 정상적인 전문도급과의 경계분쟁이 남음",
            ),
            deep_case(
                "신고자 제거와 통계 정화",
                "사업주·관리조직",
                "사고율과 감독위험을 낮게 보이게 할 유인",
                "경미사고 미보고, 사적 합의, 신고자 배치전환·계약종료로 공식통계를 낮춤",
                "신고 근로자와 미래 작업자",
                "공식 사고율은 낮지만 응급실 이용·결근·퇴사와 익명제보는 증가",
                "의료·보험자료 교차검증, 익명신고, 불이익 추정규정, 신고 후 고용추적",
                "사적 합의와 비공식 차별을 완전히 포착하기 어려움",
            ),
            deep_case(
                "일률적 의무의 영세사업장 붕괴",
                "규제설계자·대형기업",
                "고정 준수비용을 시장진입 장벽으로 이용할 유인",
                "대기업 기준의 장비·인력·보고의무를 영세사업장에 동일 적용",
                "영세사업자와 그 근로자",
                "안전투자보다 폐업·외주화·무등록 고용이 증가",
                "위험비례 의무, 공동 안전서비스, 기술·재정지원, 단계시행",
                "차등기준이 낮은 안전수준을 고착시킬 위험이 남음",
            ),
        ],
        "health_care": [
            deep_case(
                "방어진료와 고위험환자 회피",
                "의료기관·의료인",
                "법적 책임과 손실위험을 낮추려는 유인",
                "검사·전원·입원을 과도하게 늘리거나 고위험환자의 진료를 거부",
                "중증·희귀질환·응급환자와 건강보험 재정",
                "검사·전원율은 오르고 고위험환자 수용률은 하락",
                "무과실 보상, 표준진료지침의 합리적 면책, 환자위험 조정평가",
                "표준지침이 개별 환자의 예외적 필요를 억압할 수 있음",
            ),
            deep_case(
                "수가·지원의 공급자 포획",
                "의료기관·제약·장비·중개기관",
                "지급단가와 사용량을 확대하려는 유인",
                "필요보다 청구가 쉬운 검사·처치·제품을 늘리고 가격을 지원수준에 맞춤",
                "환자·보험재정·필수의료",
                "건강결과보다 청구량·고가항목·공급자 매출이 빠르게 증가",
                "성과·환자결과 연동, 가격·이해충돌 공개, 이상청구 탐지, 독립평가",
                "성과연동이 위험환자 회피와 지표조작을 만들 수 있음",
            ),
            deep_case(
                "지역의료 공백 심화",
                "대형병원·의료인력 시장",
                "수익성과 근무여건이 좋은 지역·기관으로 집중하려는 유인",
                "새 의무를 감당하지 못하는 지역기관이 서비스 축소 또는 폐쇄",
                "농어촌·저소득·장애·응급환자",
                "법 시행 후 지역별 대기·전원·휴폐업 격차가 확대",
                "지역가산, 공동인력, 원격지원, 단계기준, 필수서비스 유지보상",
                "보조금만으로 장기 인력정착과 진료품질을 보장하기 어려움",
            ),
            deep_case(
                "환자정보의 목적 외 활용",
                "의료기관·보험자·플랫폼·연구기관",
                "위험선별·마케팅·비용통제를 위해 더 많은 정보를 이용할 유인",
                "치료목적으로 수집한 정보를 보험·고용·광고·가격차별에 재사용",
                "환자와 가족의 사생활·보험접근",
                "동의서 범위와 실제 데이터 공유기관 수가 계속 증가",
                "목적제한, 세분화 동의, 접근기록, 차별금지, 삭제·이의제기",
                "비식별 자료의 재식별과 집단차별 위험은 남음",
            ),
        ],
        "housing_property": [
            deep_case(
                "지원금의 임대료 흡수",
                "임대인·분양사업자·중개업체",
                "공급이 제한된 상황에서 공적 지원을 가격으로 흡수할 유인",
                "지원 대상과 시기에 맞춰 임대료·관리비·분양가를 인상",
                "임차인·납세자·비수혜 세입자",
                "지원액 증가와 동시에 유사주택 임대료·관리비가 상승",
                "가격·관리비 공개, 공급확대, 지역별 상한·환수, 비수혜 시장 비교",
                "가격통제가 공급감소와 음성계약을 낳을 수 있음",
            ),
            deep_case(
                "계약형태 변형을 통한 보호 회피",
                "임대인·중개업자·법인",
                "임차인 보호와 세금·등록의무를 피하려는 유인",
                "단기계약·관리위탁·사용대차·법인숙소 등 다른 명칭으로 실질 임대를 구성",
                "임차인과 준법 임대인",
                "법 시행 뒤 특정 비정형 계약과 관리비 비중이 급증",
                "실질 임대차 기준, 특약 무효, 표준계약, 중개책임, 익명신고",
                "실질판단이 사후 분쟁에 의존하고 거래예측성을 낮출 수 있음",
            ),
            deep_case(
                "보호대상 선별과 퇴거 전가",
                "임대인·금융기관",
                "규제비용이 낮은 임차인만 선택하려는 유인",
                "고위험·저소득·다자녀·외국인 신청자를 계약 전 단계에서 배제",
                "주거취약계층",
                "공식 퇴거는 줄지만 계약거절·보증요구·비공식 심사가 증가",
                "차별금지, 거절사유 기록, 공공보증, 표본감사, 피해구제",
                "입증이 어려운 비공식 선별은 계속될 수 있음",
            ),
            deep_case(
                "공급감소와 시장 양극화",
                "주택 소유자·개발사업자",
                "규제된 임대시장보다 매각·고가시장·비주거 전환을 선호할 유인",
                "임대물량을 회수하거나 규제 밖 고급·단기시장으로 이동",
                "중저가 임차인과 지역사회",
                "규제대상 물량 감소, 공실·단기임대·매각 증가",
                "공급탄력성 분석, 단계시행, 공공공급, 합리적 비용인정",
                "공급지원이 개발이익 사유화와 지역갈등을 만들 수 있음",
            ),
        ],
        "education_system": [
            deep_case(
                "평가지표 맞추기와 학생 선별",
                "학교·교육행정·교원",
                "성과평가와 예산·평판을 높이려는 유인",
                "시험 대비에 집중하고 저성과·특수지원 학생을 제외·전학·결석 처리",
                "취약학생·특수교육 대상·장기결석 학생",
                "평균점수는 오르지만 제외학생·전학·중도탈락이 증가",
                "학생구성 위험조정, 포용지표, 원자료 감사, 장기성과와 학생경험 평가",
                "지표가 복잡해질수록 교육현장의 행정부담과 조작기회가 증가",
            ),
            deep_case(
                "정보격차에 의한 선택권 포획",
                "정보와 이동수단이 많은 가정·사교육 시장",
                "좋은 학교·과정·지원에 먼저 접근하려는 유인",
                "복잡한 신청과 정보비대칭을 이용해 선택권과 특례를 선점",
                "저소득·농어촌·이주·장애학생",
                "선택권 확대 뒤 계층·지역별 참여율과 성과격차가 확대",
                "자동안내, 단순신청, 교통·돌봄 지원, 무작위·균형배정, 격차공개",
                "균형배정이 가족의 선택권과 학교 특성화를 제한할 수 있음",
            ),
            deep_case(
                "정치·이념적 교육통제",
                "임명권자·교육행정·다수 정치세력",
                "교육과정·인사·예산을 정치적 정체성 강화에 이용할 유인",
                "인사·교재·평가·지원사업을 통해 비판적 견해와 소수교육을 축소",
                "학생·교원·학부모의 표현과 교육자율",
                "정권교체마다 교육과정·인사·지원기준이 급격히 변경",
                "독립 심의, 다원적 구성, 공개근거, 학문·교육자유, 사법적 불복",
                "독립기구 역시 특정 전문가집단에 포획될 가능성이 남음",
            ),
            deep_case(
                "학교 현장의 행정업무 폭증",
                "중앙·지방 교육행정",
                "새 정책의 이행을 보고서와 시스템 입력으로 확인하려는 유인",
                "교원과 학교에 계획·실적·증빙업무를 전가",
                "학생의 수업시간과 교원의 교육역량",
                "정책사업 수와 보고시간은 늘지만 수업·상담시간은 감소",
                "업무량 영향평가, 자동연계, 보고 통폐합, 현장 표본검증",
                "자동화가 새로운 데이터 입력·감시 부담으로 변할 수 있음",
            ),
        ],
        "organization_power": [
            deep_case(
                "조직 신설을 통한 예산·정원 고착",
                "부처·위원회·이해관계집단",
                "새 조직과 직위·예산을 장기 유지하려는 유인",
                "한시적 문제를 이유로 상설기구를 만들고 성과와 무관하게 기능 확대",
                "납세자·기존기관·정책대상",
                "성과자료 없이 정원·예산·산하기관이 계속 증가",
                "설치기한, 일몰, 기능중복 심사, 정원·예산 상한, 독립 성과평가",
                "일몰 직전 형식적 성과와 정치적 연장 가능성이 남음",
            ),
            deep_case(
                "포괄위임을 통한 권한 팽창",
                "행정부·신설기관",
                "법률보다 유연하게 권한범위를 확대하려는 유인",
                "핵심 기준을 시행령·고시·내부지침의 ‘필요한 사항’으로 위임",
                "국민·기업·지방정부·국회의 통제권",
                "하위규정 개정으로 적용대상·자료요구·제재범위가 계속 확대",
                "법률유보, 위임범위 명시, 국회보고, 규제영향평가, 사법심사",
                "기술·현장변화에 대한 신속 대응은 느려질 수 있음",
            ),
            deep_case(
                "기관 간 책임 떠넘기기",
                "신설기관·기존부처·지방정부",
                "실패와 민원책임을 다른 기관에 넘기려는 유인",
                "공동협의·지원·조정이라는 표현으로 최종결정권과 책임을 불명확하게 함",
                "민원인·정책대상·현장공무원",
                "기관 간 이송과 협의가 반복되고 처리기한이 길어짐",
                "단일 책임기관, 권한표, 법정 처리기한, 공동사건 추적, 책임공개",
                "복합정책의 협업 필요와 단일책임 사이 긴장은 남음",
            ),
            deep_case(
                "전문가·위원회 포획",
                "업계·전문직단체·정치권",
                "위원 구성과 전문기준을 통해 정책결정을 선점할 유인",
                "동일 네트워크가 추천·심사·평가를 반복 점유하고 이해충돌을 숨김",
                "소수의견·신규진입자·일반 시민",
                "위원 경력과 소속이 반복되고 회의록·이해충돌 자료가 비공개",
                "공개모집, 임기제한, 구성 다양성, 이해충돌 공개, 시민위원·소수의견",
                "형식적 다양성을 갖춰도 실질 정보와 의제설정 권력은 집중될 수 있음",
            ),
        ],
    }
    return data.get(profile, [])


def additional_second_order_effects(profile: str) -> list[dict[str, str]]:
    data: dict[str, list[dict[str, str]]] = {
        "labor_safety": [
            {
                "effect": "외주화·무등록 고용 증가",
                "direction": "위험",
                "mechanism": "고정 준수비용과 사용자책임을 회피하기 위해 계약구조를 분리",
                "monitor": "도급단계·특수고용·무등록 사업장·산재 미가입 증가",
            },
            {
                "effect": "안전투자와 생산성 개선 가능성",
                "direction": "기회",
                "mechanism": "사고·이직·중단비용 감소가 장기 생산성으로 이어질 수 있음",
                "monitor": "사고율·이직률·설비가동중단·생산성",
            },
        ],
        "health_care": [
            {
                "effect": "의료비와 행정비용 증가",
                "direction": "위험",
                "mechanism": "책임회피 검사·보고·인증과 새로운 청구항목이 확대",
                "monitor": "환자당 검사·청구·행정시간·보험지출",
            },
            {
                "effect": "고위험환자 접근성 변화",
                "direction": "양면",
                "mechanism": "안전기준 강화가 보호를 높이거나 수용회피를 만들 수 있음",
                "monitor": "전원율·수용거절·치료지연·중증도 조정 결과",
            },
        ],
        "housing_property": [
            {
                "effect": "임대료·관리비 구성의 변화",
                "direction": "위험",
                "mechanism": "규제된 가격 대신 비규제 비용항목으로 부담 이동",
                "monitor": "총주거비 중 관리비·보증·수수료 비중",
            },
            {
                "effect": "공급구조 변화",
                "direction": "양면",
                "mechanism": "보호가 안정성을 높이거나 민간공급을 축소할 수 있음",
                "monitor": "임대물량·신규공급·공실·단기임대 전환",
            },
        ],
        "education_system": [
            {
                "effect": "사교육·정보시장 확대",
                "direction": "위험",
                "mechanism": "복잡한 선발·평가·선택제도는 정보구매 능력의 가치를 높임",
                "monitor": "사교육비·컨설팅 이용·계층별 참여격차",
            },
            {
                "effect": "교원 전문성 또는 행정화",
                "direction": "양면",
                "mechanism": "책임성과 지원이 전문성을 높이거나 보고업무를 늘릴 수 있음",
                "monitor": "수업·상담시간, 행정시간, 교원 이직·만족도",
            },
        ],
        "organization_power": [
            {
                "effect": "행정조정 개선 가능성",
                "direction": "기회",
                "mechanism": "분산된 업무와 책임을 한 조직이 통합할 수 있음",
                "monitor": "처리기간·중복예산·기관이송·민원해결률",
            },
            {
                "effect": "관료제와 규제범위 확대",
                "direction": "위험",
                "mechanism": "조직 생존을 위해 새로운 업무·자료요구·규제를 계속 발굴",
                "monitor": "정원·예산·규정·자료요구 건수",
            },
        ],
    }
    return data.get(profile, [])


def additional_evidence_tests(profile: str) -> list[dict[str, str]]:
    data: dict[str, list[dict[str, str]]] = {
        "labor_safety": [
            {
                "claim": "새 안전의무가 실제 사고와 중대위험을 줄인다.",
                "required_evidence": "업종·규모·고용형태별 사고율, 아차사고, 작업중지, 원청·하청 비교",
                "rejection_condition": "교육·서류 실적만 늘고 위험노출·사고·은폐지표는 개선되지 않음",
            },
            {
                "claim": "준수비용이 영세사업장과 고용에 과도한 충격을 주지 않는다.",
                "required_evidence": "사업장 규모별 비용, 폐업·외주화·고용변화와 지원효과",
                "rejection_condition": "사고감소보다 무등록·외주화·폐업이 크게 증가",
            },
        ],
        "health_care": [
            {
                "claim": "제도 변경이 환자결과와 접근성을 개선한다.",
                "required_evidence": "위험조정 사망·합병증·대기·전원·미충족의료와 지역별 자료",
                "rejection_condition": "청구·기관·보고량만 늘고 환자결과·접근성은 개선되지 않음",
            },
            {
                "claim": "수가·책임 구조가 과잉·과소진료를 유발하지 않는다.",
                "required_evidence": "검사·처치·고위험환자 수용·회피진료와 비교집단 분석",
                "rejection_condition": "저위험 청구량은 늘고 고위험환자 수용은 줄어듦",
            },
        ],
        "housing_property": [
            {
                "claim": "보호·지원의 편익이 실제 임차인에게 귀착된다.",
                "required_evidence": "총주거비, 임대료·관리비·보증비용, 계약거절과 공급량",
                "rejection_condition": "지원액이 가격으로 흡수되거나 보호대상 계약거절이 증가",
            },
            {
                "claim": "재산권 제한이 공급감소보다 큰 주거안정 편익을 만든다.",
                "required_evidence": "퇴거·이동·주거지속성과 임대물량·신규공급·전환 자료",
                "rejection_condition": "보호효과보다 임대공급 축소와 비정형 계약 증가가 큼",
            },
        ],
        "education_system": [
            {
                "claim": "제도 변경이 학생의 학습·기회·안전을 개선한다.",
                "required_evidence": "학생구성 위험조정 성과, 격차, 중도탈락, 만족도와 장기추적",
                "rejection_condition": "행정성과만 개선되고 취약학생 격차·배제·사교육비가 증가",
            },
            {
                "claim": "학교와 교원의 행동이 교육목적에 맞게 변화한다.",
                "required_evidence": "수업·상담·행정시간, 학생선별, 평가대상 제외와 질적 조사",
                "rejection_condition": "시험교육·학생선별·보고업무가 늘고 수업시간이 감소",
            },
        ],
        "organization_power": [
            {
                "claim": "새 조직이 중복을 줄이고 책임성과 처리속도를 높인다.",
                "required_evidence": "기능맵, 기존기관 대비 업무·예산·인력, 처리기간과 민원이송",
                "rejection_condition": "기존 조직은 유지된 채 정원·보고·협의절차만 증가",
            },
            {
                "claim": "추가 권한에 상응하는 민주적·사법적 통제가 존재한다.",
                "required_evidence": "위임범위, 공개·감사·불복·국회보고와 실제 통제 사례",
                "rejection_condition": "포괄위임과 비공개 의사결정이 늘고 외부통제는 약함",
            },
        ],
    }
    return data.get(profile, [])


def additional_design_requirements(profile: str) -> list[str]:
    data: dict[str, list[str]] = {
        "labor_safety": [
            "원청·플랫폼·다단계 도급의 실질 지배와 공동책임 기준을 명시",
            "신고·작업중지 이후 해고·배치·계약갱신 불이익을 추적하고 구제",
            "사고율뿐 아니라 아차사고·위험노출·은폐·외주화 지표를 공개",
        ],
        "health_care": [
            "환자결과·접근성·고위험환자 수용을 위험조정해 평가",
            "수가·책임 변화에 따른 과잉·과소진료와 방어진료를 함께 추적",
            "지역·기관 규모별 단계시행과 필수의료 유지비용을 보완",
        ],
        "housing_property": [
            "임대료뿐 아니라 관리비·보증·수수료를 포함한 총주거비를 추적",
            "명의·특약·단기·위탁 등 실질 임대차 우회를 방지",
            "임차인 안정성과 공급량·계약거절·비정형시장 변화를 함께 평가",
        ],
        "education_system": [
            "평균성과 외에 취약학생 격차·배제·중도탈락·학생경험을 평가",
            "교원 수업·상담시간과 행정부담 영향을 사전·사후 측정",
            "정치·이념적 통제를 막는 독립성·다원성·공개근거와 불복절차를 마련",
        ],
        "organization_power": [
            "신설 전 기존기관 기능·예산·정원과의 중복분석 및 폐지·통합계획을 공개",
            "핵심 권한·자료요구·제재범위는 법률에서 제한하고 포괄위임을 금지",
            "설치기한·정원·예산상한·독립평가와 자동폐지 또는 재승인을 결합",
        ],
    }
    return data.get(profile, [])



CLAIM_TYPE_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("수치·규모 주장", ("%", "퍼센트", "건", "명", "억원", "조원", "증가율", "감소율")),
    ("국제·타제도 비교", ("미국", "일본", "유럽", "해외", "선진국", "외국", "다른 나라")),
    ("원인 주장", ("때문", "원인", "초래", "야기", "따라", "결국", "인하여")),
    ("문제 진단", PROBLEM_WORDS),
    ("효과·편익 주장", ("강화", "개선", "제고", "보호", "확대", "줄일", "높일", "기대")),
    ("권한·의무 변경", ("임명", "선임", "동의", "의무", "금지", "허가", "지원", "제재", "설치")),
)


def classify_claim(sentence: str) -> str:
    for label, words in CLAIM_TYPE_RULES:
        if contains_any(sentence, words):
            return label
    return "기타 정책 주장"


def claim_evidence_requirement(claim_type: str, sentence: str) -> tuple[str, str]:
    if claim_type == "수치·규모 주장":
        return (
            "원자료, 분모, 산출방식, 기간별 추세와 비교집단",
            "수치의 분모·기간·출처가 불명확하거나 재현되지 않는 경우",
        )
    if claim_type == "국제·타제도 비교":
        return (
            "국가별 법령 원문, 제도 차이, 시행성과와 한국 적용가능성",
            "선택한 국가가 대표성이 없거나 제도맥락과 성과가 서로 다른 경우",
        )
    if claim_type == "원인 주장":
        return (
            "원인과 결과의 시간순서, 대안원인 통제, 비교집단 또는 자연실험",
            "다른 요인이 문제를 더 크게 설명하거나 제안된 원인과 결과가 함께 변하지 않는 경우",
        )
    if claim_type == "문제 진단":
        return (
            "최근 공식 통계, 현행제도 운영자료, 피해·사각지대와 집단별 분포",
            "문제 규모가 작거나 일시적이며 현행제도 보완으로 해결 가능한 경우",
        )
    if claim_type == "효과·편익 주장":
        return (
            "유사제도 성과, 시범사업, 비용편익, 부작용과 장기추적 자료",
            "결과지표가 개선되지 않거나 부작용과 비용이 편익보다 큰 경우",
        )
    if claim_type == "권한·의무 변경":
        return (
            "현행법·개정안 조문, 권한 흐름, 집행기관·비용·불복절차",
            "권한은 확대되지만 통제·책임·자원이 불충분하거나 기존제도와 중복되는 경우",
        )
    return (
        "주장을 직접 뒷받침하는 공식자료와 반대자료",
        "독립자료로 확인되지 않거나 반대증거가 더 강한 경우",
    )


def build_claim_ledger(
    text: str,
    sentences: list[str],
) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []

    for index, sentence in enumerate(sentences, start=1):
        cleaned = normalize_korean_sentence(remove_clause_reference(sentence))
        if len(cleaned) < 18:
            continue

        claim_type = classify_claim(cleaned)
        required, rejection = claim_evidence_requirement(claim_type, cleaned)

        ledger.append(
            {
                "id": f"C{index:02d}",
                "claim": cleaned,
                "claim_type": claim_type,
                "source_type": "발의자 제안이유·주요내용",
                "source_status": "국회 공식 공개문서",
                "verification_status": "독립 검증 전",
                "required_evidence": required,
                "rejection_condition": rejection,
                "confidence": "낮음" if claim_type == "기타 정책 주장" else "검증 필요",
            }
        )

    return ledger[:12]


def build_official_document_register(
    detail_link: str,
    summary_connected: bool,
    committee: str,
    stage: str,
) -> list[dict[str, str]]:
    committee_known = committee not in UNKNOWN_COMMITTEE_VALUES
    review_possible = committee_known and stage not in ("의안 접수", "위원회 회부·심사 대기")

    return [
        {
            "document": "제안이유 및 주요내용",
            "status": "연동 완료" if summary_connected else "자료 없음",
            "importance": "법안이 주장하는 문제·목적·변경방향",
            "source_url": detail_link,
        },
        {
            "document": "법률안 조문 전문",
            "status": "상세페이지·첨부파일 확인 필요",
            "importance": "실제 권리·의무·예외·위임·제재 확인",
            "source_url": detail_link,
        },
        {
            "document": "신·구조문 대비표",
            "status": "존재 여부 확인 필요",
            "importance": "현행법과 개정안의 문장 단위 차이",
            "source_url": detail_link,
        },
        {
            "document": "비용추계서·미첨부 사유서",
            "status": "존재 여부 확인 필요",
            "importance": "국가·지자체·민간의 재정·인력 부담",
            "source_url": detail_link,
        },
        {
            "document": "소관위원회 검토보고서",
            "status": (
                "심사자료 확인 필요" if review_possible
                else "위원회 심사 진행 후 확인"
            ),
            "importance": "전문위원의 법체계·집행·비용·쟁점 검토",
            "source_url": detail_link,
        },
        {
            "document": "위원회 회의록·찬반토론",
            "status": (
                "회의자료 확인 필요" if review_possible
                else "회의 개최 후 확인"
            ),
            "importance": "질의·반론·정부답변·수정논의",
            "source_url": detail_link,
        },
    ]


def map_attack_to_forks(
    profile_key: str,
    attack_title: str,
) -> list[str]:
    mapping: dict[str, dict[str, list[str]]] = {
        "appointment_governance": {
            "추천단계 선점": ["A", "C"],
            "청문·동의의 거래화": ["A"],
            "해임·예산을 통한 사후통제": ["A"],
            "주민책임성 공백": ["B", "C"],
        },
        "data_security": {
            "서류상 보안": ["B", "C"],
            "사고 은폐·축소": ["B"],
            "보안 명분의 과잉감시": ["A", "B"],
            "외주업체 책임회피": ["B"],
        },
        "subsidy_fiscal": {
            "자격기준 맞추기": ["B"],
            "공급자 포획": ["C"],
            "사각지대 고착": ["A", "B"],
            "재정의 영구고착": ["C"],
        },
        "punitive_regulation": {
            "선택적 집행": ["A", "C"],
            "규제범위 우회": ["B"],
            "영세주체 퇴출": ["A", "B"],
            "신고·제재의 경쟁무기화": ["C"],
        },
    }
    return mapping.get(profile_key, {}).get(attack_title, ["A", "B", "C"])


def build_evidence_chains(
    claims: list[dict[str, Any]],
    attacks: list[dict[str, Any]],
    forks: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not attacks:
        return []

    claim_ids = [claim["id"] for claim in claims]
    available_forks = {fork.get("label", "") for fork in forks}
    chains: list[dict[str, Any]] = []

    for attack in attacks:
        linked_forks = [
            label
            for label in map_attack_to_forks(
                attack.get("profile_key", ""),
                attack.get("title", ""),
            )
            if label in available_forks
        ]

        chains.append(
            {
                "attack": attack.get("title", ""),
                "profile": attack.get("profile_label", ""),
                "claim_ids": claim_ids[:3],
                "logic": (
                    "발의자의 문제·효과 주장이 참이더라도, 이해관계자의 실제 유인이 "
                    "법의 형식과 다른 행동을 만들 수 있는지 검토합니다."
                ),
                "failure_path": attack.get("path", ""),
                "guardrail": attack.get("defense", ""),
                "residual_risk": attack.get("residual", ""),
                "fork_links": linked_forks,
                "verification_status": "조문·외부근거 대조 전",
            }
        )

    return chains[:10]


def build_deep_review(
    title: str,
    text: str,
    easy_problem: str,
    easy_change: str,
    actors: list[str],
    generated_at: str,
    sentences: list[str] | None = None,
    fork_candidates: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    profiles = detect_policy_profiles(title, text, max_profiles=3)
    primary = profiles[0]
    sentences = sentences or split_sentences(text)
    fork_candidates = fork_candidates or []

    mechanism_chain = [
        {
            "step": "문제",
            "content": easy_problem,
            "failure_question": "문제의 규모·원인·대상이 독립자료로 확인되는가?",
        },
        {
            "step": "법적 수단",
            "content": easy_change,
            "failure_question": "실제 원인에 작용하는가, 보이는 절차와 조직만 바꾸는가?",
        },
        {
            "step": "권한·비용 이동",
            "content": (
                "새 권한·의무·예산·정보접근이 어느 기관과 집단으로 이동하는지 확인해야 합니다."
            ),
            "failure_question": "편익을 얻는 주체와 비용·위험을 지는 주체가 분리되는가?",
        },
        {
            "step": "집행과 유인",
            "content": (
                "집행기관과 정책대상은 법의 취지뿐 아니라 자신에게 유리한 방식으로 적응합니다."
            ),
            "failure_question": "준수보다 우회·은폐·책임전가·지표조작이 더 유리하지 않은가?",
        },
        {
            "step": "직접 결과",
            "content": (
                "정책이 내세운 목표가 실제 결과지표에서 개선되는지 확인해야 합니다."
            ),
            "failure_question": "활동량·보고량이 아니라 국민의 실제 결과가 개선되는가?",
        },
        {
            "step": "2차 효과와 되돌림",
            "content": (
                "가격·시장구조·권력·지역격차·기본권에 나타나는 간접효과와 실패 시 되돌림을 검토합니다."
            ),
            "failure_question": "부작용을 조기에 발견하고 축소·중단·보상할 수 있는가?",
        },
    ]

    assumption_groups: list[list[dict[str, Any]]] = []
    actor_groups: list[list[dict[str, Any]]] = []
    attack_groups: list[list[dict[str, Any]]] = []
    effect_groups: list[list[dict[str, Any]]] = []
    stress_groups: list[list[dict[str, Any]]] = []
    evidence_groups: list[list[dict[str, Any]]] = []
    requirements: list[str] = []

    for profile in profiles:
        key = profile["key"]

        assumptions = (
            profile_assumptions(key)
            + additional_profile_assumptions(key)
        )
        assumption_groups.append(attach_profile(assumptions, profile))

        actor_groups.append(
            attach_profile(
                profile_actor_incentives(key, actors),
                profile,
            )
        )

        attacks = (
            profile_deep_red_team(key)
            + additional_profile_attacks(key)
        )
        attack_groups.append(attach_profile(attacks, profile))

        effects = (
            profile_second_order_effects(key)
            + additional_second_order_effects(key)
        )
        effect_groups.append(attach_profile(effects, profile))

        stress_groups.append(
            attach_profile(profile_stress_tests(key), profile)
        )

        tests = (
            profile_evidence_tests(key)
            + additional_evidence_tests(key)
        )
        evidence_groups.append(attach_profile(tests, profile))

        requirements.extend(profile_design_requirements(key))
        requirements.extend(additional_design_requirements(key))

    critical_assumptions = merge_dict_items(
        assumption_groups,
        ("assumption",),
        limit=14,
    )
    actor_incentives = merge_dict_items(
        actor_groups,
        ("actor", "gaming_risk"),
        limit=12,
    )
    deep_red_team = merge_dict_items(
        attack_groups,
        ("title", "path"),
        limit=12,
    )
    second_order_effects = merge_dict_items(
        effect_groups,
        ("effect", "mechanism"),
        limit=12,
    )
    stress_tests = merge_dict_items(
        stress_groups,
        ("scenario", "question"),
        limit=10,
    )
    evidence_tests = merge_dict_items(
        evidence_groups,
        ("claim",),
        limit=12,
    )
    design_requirements = unique_items(requirements, 16)

    claim_ledger = build_claim_ledger(text, sentences)
    evidence_chains = build_evidence_chains(
        claim_ledger,
        deep_red_team,
        fork_candidates,
    )

    return {
        "status": "복수 위험프로필 증거사슬형 심층 검토 초안",
        "scope": (
            "국회 ‘제안이유 및 주요내용’을 바탕으로 복수 위험축을 동시에 공격한 초안입니다. "
            "조문 전문·비용추계·위원회 자료·외부 통계 대조 전에는 확정평가가 아닙니다."
        ),
        "generated_at": generated_at,
        "profile": primary,
        "profiles": profiles,
        "central_conflict": build_decision_question(text),
        "mechanism_chain": mechanism_chain,
        "claim_ledger": claim_ledger,
        "critical_assumptions": critical_assumptions,
        "actor_incentives": actor_incentives,
        "deep_red_team": deep_red_team,
        "second_order_effects": second_order_effects,
        "stress_tests": stress_tests,
        "evidence_tests": evidence_tests,
        "evidence_chains": evidence_chains,
        "design_requirements": design_requirements,
        "depth_summary": {
            "profiles": len(profiles),
            "claims": len(claim_ledger),
            "assumptions": len(critical_assumptions),
            "actors": len(actor_incentives),
            "attacks": len(deep_red_team),
            "second_order_effects": len(second_order_effects),
            "stress_tests": len(stress_tests),
            "evidence_tests": len(evidence_tests),
            "evidence_chains": len(evidence_chains),
        },
        "residual_uncertainty": [
            "법률안 조문 전문과 현행법 대비표가 아직 자동 대조되지 않았습니다.",
            "비용추계서·미첨부 사유서와 실제 행정인력 자료가 아직 반영되지 않았습니다.",
            "위원회 검토보고서·회의록과 정부·이해관계자 답변이 아직 반영되지 않았습니다.",
            "외부 통계와 국내외 유사제도의 시행성과를 독립적으로 대조하지 않았습니다.",
            "주장 원장의 모든 항목은 현재 독립 검증 전 상태입니다.",
        ],
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
    quality_flags: list[str] = []

    if len(text) >= 250:
        confidence_points += 1
    else:
        quality_flags.append("공식 요약이 짧음")

    if problem_sentence:
        confidence_points += 1
    else:
        quality_flags.append("문제정의 분리 실패")

    if change_sentence:
        confidence_points += 1
    else:
        quality_flags.append("변경내용 분리 실패")

    if re.search(r"\(안\s*제?\d+조", text):
        confidence_points += 1
    else:
        quality_flags.append("관련 조문 단서 없음")

    confidence = (
        "보통" if confidence_points >= 3
        else "낮음" if confidence_points >= 1
        else "생성 불가"
    )
    analysis_visibility = "전체 표시" if confidence_points >= 3 else "제한 표시"

    empty_scores = {
        key: 0
        for key in (
            "문제정의", "근거", "재정", "집행",
            "기본권", "통제장치", "성과측정", "재검토",
        )
    }

    review_status = {
        "official_source": "연동 완료" if text else "자료 없음",
        "plain_language": "자동 초안" if text else "미작성",
        "external_evidence": "미연결",
        "clause_comparison": "미완료",
        "human_review": "미완료",
    }

    source_labels = [
        {
            "label": "국회 공식 원문",
            "status": review_status["official_source"],
            "kind": "official",
        },
        {
            "label": "쉬운 설명·위험",
            "status": review_status["plain_language"],
            "kind": "automatic",
        },
        {
            "label": "외부 근거",
            "status": review_status["external_evidence"],
            "kind": "missing",
        },
        {
            "label": "사람 검토",
            "status": review_status["human_review"],
            "kind": "missing",
        },
    ]

    if not text:
        return {
            "analysis_status": "미분석",
            "analysis_method": "공식 원문 없음",
            "analysis_confidence": "생성 불가",
            "analysis_visibility": "숨김",
            "analysis_review_state": "사람 검토 필요",
            "analysis_generated_at": generated_at,
            "quality_flags": ["공식 원문 없음"],
            "review_status": review_status,
            "source_labels": source_labels,
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
            "fork_hidden_reason": "공식 원문이 없어 자동 수정안을 만들지 않았습니다.",
            "deep_review": {
                "status": "생성 불가",
                "scope": "공식 원문 없음",
                "profile": {
                    "key": "unknown",
                    "label": "분류 불가",
                    "matched_signals": [],
                    "confidence": "없음",
                },
                "profiles": [],
                "central_conflict": "확인 불가",
                "mechanism_chain": [],
                "claim_ledger": [],
                "critical_assumptions": [],
                "actor_incentives": [],
                "deep_red_team": [],
                "second_order_effects": [],
                "stress_tests": [],
                "evidence_tests": [],
                "evidence_chains": [],
                "design_requirements": [],
                "depth_summary": {},
                "residual_uncertainty": ["공식 제안이유·주요내용 필요"],
            },
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

    strengths = dedupe_similar(build_strengths(text, change_sentence), 3)
    weaknesses = build_weaknesses(text)
    counterarguments = build_counterarguments(text)
    risks = dedupe_similar(build_risks(text), 3)
    loopholes = dedupe_similar(build_loopholes(text), 3)
    red_team_cases = build_red_team_cases(text)
    failure_modes = build_failure_modes(text)
    affected = actors or ["공식 요약에서 직접 영향 주체를 찾지 못했습니다."]
    evidence = build_evidence_review(text, problem_sentence, strengths)

    easy_problem = easy_korean(
        problem_sentence or (sentences[0] if sentences else "문제정의를 확인해야 합니다.")
    )
    easy_change = easy_korean(
        change_sentence or (sentences[-1] if sentences else "변경내용을 확인해야 합니다.")
    )

    claimed_benefit = normalize_korean_sentence(build_claimed_benefit(text))
    strongest_objection = normalize_korean_sentence(
        counterarguments[0]
        if counterarguments
        else "가장 강한 반대논거를 추가 확인해야 합니다."
    )
    largest_risk = normalize_korean_sentence(
        red_team_cases[0]["consequence"]
        if red_team_cases
        else (risks[0] if risks else "구체적인 위험을 추가 확인해야 합니다.")
    )

    all_forks = build_fork_candidates_v07(title, text, change_sentence)
    if confidence_points >= 3:
        fork_candidates = all_forks[:3]
        fork_hidden_reason = ""
    else:
        fork_candidates = []
        fork_hidden_reason = (
            "공식 요약에서 문제·변경내용·관련 조문이 충분히 분리되지 않아 "
            "자동 수정안을 숨겼습니다."
        )

    status = "자동 검토 초안" if confidence_points >= 3 else "제한적 자동 검토"

    deep_review = build_deep_review(
        title=title,
        text=text,
        easy_problem=easy_problem,
        easy_change=easy_change,
        actors=actors,
        generated_at=generated_at,
        sentences=sentences,
        fork_candidates=fork_candidates,
    )

    return {
        "analysis_status": status,
        "analysis_method": "복수 위험프로필·주장 원장·증거사슬 검토 v0.6",
        "analysis_confidence": confidence,
        "analysis_visibility": analysis_visibility,
        "analysis_review_state": "사람 검토 전",
        "analysis_generated_at": generated_at,
        "quality_flags": quality_flags,
        "review_status": review_status,
        "source_labels": source_labels,
        "card_summary": build_easy_card_summary(
            title, text, problem_sentence, change_sentence
        ),
        "plain_language": (
            f"법안이 제시한 문제: {easy_problem} "
            f"제안된 변경: {easy_change}"
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
        "analysis_clues": dedupe_similar(evidence["analysis_clues"], 3),
        "verified_evidence": dedupe_similar(evidence["verified_evidence"], 3),
        "unverified_claims": dedupe_similar(evidence["unverified_claims"], 3),
        "strengths": strengths,
        "weaknesses": weaknesses,
        "risks": risks,
        "counterarguments": counterarguments,
        "loopholes": loopholes,
        "red_team_cases": red_team_cases[:2],
        "failure_modes": failure_modes[:2],
        "fork_levers": build_fork_levers(text),
        "fork_candidates": fork_candidates,
        "fork_hidden_reason": fork_hidden_reason,
        "deep_review": deep_review,
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
        "questions": dedupe_similar(build_questions(text, committee), 4),
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
                "analysis_visibility": analysis["analysis_visibility"],
                "analysis_review_state": analysis["analysis_review_state"],
                "quality_flags": analysis["quality_flags"],
                "review_status": analysis["review_status"],
                "source_labels": analysis["source_labels"],
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
                "fork_hidden_reason": analysis["fork_hidden_reason"],
                "deep_review": analysis["deep_review"],
                "official_documents": build_official_document_register(
                    detail_link=detail_link,
                    summary_connected=source_connected,
                    committee=committee,
                    stage=stage,
                ),
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
    limited_count = sum(
        1 for bill in bills if bill.get("analysis_status") == "제한적 자동 검토"
    )
    red_team_case_count = sum(
        len(bill.get("red_team_cases") or []) for bill in bills
    )
    fork_candidate_count = sum(
        len(bill.get("fork_candidates") or []) for bill in bills
    )
    clause_compared_count = 0
    human_reviewed_count = 0
    deep_review_count = sum(
        1
        for bill in bills
        if (bill.get("deep_review") or {}).get("status")
        == "복수 위험프로필 증거사슬형 심층 검토 초안"
    )
    deep_attack_count = sum(
        len((bill.get("deep_review") or {}).get("deep_red_team") or [])
        for bill in bills
    )
    claim_ledger_count = sum(
        len((bill.get("deep_review") or {}).get("claim_ledger") or [])
        for bill in bills
    )
    evidence_chain_count = sum(
        len((bill.get("deep_review") or {}).get("evidence_chains") or [])
        for bill in bills
    )
    multi_profile_count = sum(
        1
        for bill in bills
        if len((bill.get("deep_review") or {}).get("profiles") or []) >= 2
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
            f"공식 원문 {linked_count}건, 심층 검토 {deep_review_count}건, "
            f"복수 위험프로필 법안 {multi_profile_count}건, 주장 원장 {claim_ledger_count}개, "
            f"증거사슬 {evidence_chain_count}개를 표시합니다. "
            "현재는 공식 요약 기반 초안이며 조문·비용·위원회 자료 대조 전 단계입니다."
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
            "limited_draft_count": limited_count,
            "red_team_case_count": red_team_case_count,
            "fork_candidate_count": fork_candidate_count,
            "clause_compared_count": clause_compared_count,
            "human_reviewed_count": human_reviewed_count,
            "deep_review_count": deep_review_count,
            "deep_attack_count": deep_attack_count,
            "claim_ledger_count": claim_ledger_count,
            "evidence_chain_count": evidence_chain_count,
            "multi_profile_count": multi_profile_count,
            "summary_error_count": len(summary_errors),
            "detail_error_count": len(detail_errors),
        },
        "stats": {
            "tracked": len(bills),
            "official_summaries": linked_count,
            "structured_drafts": structured_count,
            "limited_drafts": limited_count,
            "clause_compared": clause_compared_count,
            "human_reviewed": human_reviewed_count,
            "deep_reviews": deep_review_count,
            "deep_attacks": deep_attack_count,
            "claim_ledger": claim_ledger_count,
            "evidence_chains": evidence_chain_count,
            "multi_profile_bills": multi_profile_count,
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
