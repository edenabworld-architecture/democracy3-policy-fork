"""Democracy 3.0 정책포크: 국회 의안 기본정보와 공식 제안이유·주요내용 자동수집기."""

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
OUTPUT = Path(__file__).resolve().parent / "bills.json"
KST = timezone(timedelta(hours=9))
SUMMARY_WORKERS = 5


def clean(value: Any, fallback: str = "확인 필요") -> str:
    text = str(value or "").strip()
    return text or fallback


def clean_official_text(value: Any) -> str:
    """공식 SUMMARY의 HTML 조각과 과도한 공백을 정리하되 문단은 보존합니다."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p\s*>", "\n\n", text)
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.split("\n")]
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def excerpt(value: str, limit: int = 360) -> str:
    one_line = re.sub(r"\s+", " ", value).strip()
    if not one_line:
        return "공식 제안이유와 주요내용을 확인 중입니다."
    return one_line if len(one_line) <= limit else one_line[:limit].rstrip() + "…"


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
        raise RuntimeError(
            f"국회 API 오류 {code}: {result.get('MESSAGE', '')}"
        )

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
        raise RuntimeError(
            f"국회 API 오류 {code}: {result.get('MESSAGE', '')}"
        )

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
                    "User-Agent": "Democracy3-Policy-Fork/0.4",
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
    """이전 수집에서 이미 받은 공식 원문은 다시 호출하지 않습니다."""
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
        bill_no = str(official.get("bill_no") or "").strip()
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
        attempts=3,
        timeout=60,
    )
    _, rows = parse_rows(payload, SUMMARY_API_KEY)
    if not rows:
        return ""
    return clean_official_text(rows[0].get("SUMMARY"))


def collect_summaries(
    api_key: str,
    bill_numbers: list[str],
    cache: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    summaries = dict(cache)
    missing = [number for number in bill_numbers if number and not summaries.get(number)]
    errors: list[str] = []

    if not missing:
        return summaries, errors

    with ThreadPoolExecutor(max_workers=SUMMARY_WORKERS) as executor:
        jobs = {
            executor.submit(fetch_official_summary, api_key, bill_no): bill_no
            for bill_no in missing
        }
        for future in as_completed(jobs):
            bill_no = jobs[future]
            try:
                summaries[bill_no] = future.result()
            except Exception as exc:  # 한 건 실패가 전체 갱신을 막지 않게 합니다.
                errors.append(f"{bill_no}: {exc}")

    return summaries, errors


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
        attempts=3,
        timeout=60,
    )
    total_count, rows = parse_rows(list_payload, LIST_API_KEY)
    rows.sort(key=lambda row: clean(row.get("PROPOSE_DT"), ""), reverse=True)

    bill_numbers = [
        str(row.get("BILL_NO") or "").strip()
        for row in rows
        if str(row.get("BILL_NO") or "").strip()
    ]
    summaries, summary_errors = collect_summaries(
        api_key,
        bill_numbers,
        load_summary_cache(),
    )

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    bills: list[dict[str, Any]] = []

    for row in rows:
        title = clean(row.get("BILL_NAME"), "제목 확인 필요")
        committee = clean(row.get("COMMITTEE"), "소관위원회 확인 필요")
        proposed_date = clean(row.get("PROPOSE_DT"), "제안일 확인 필요")
        proposer = clean(
            row.get("RST_PROPOSER") or row.get("PROPOSER"),
            "제안자 확인 필요",
        )
        process_result = clean(row.get("PROC_RESULT"), "")
        stage = f"처리결과: {process_result}" if process_result else "심사 진행 중"
        detail_link = normalize_url(row.get("DETAIL_LINK"))
        bill_no = str(row.get("BILL_NO") or "").strip()
        official_summary = summaries.get(bill_no, "")
        source_connected = bool(official_summary)

        bills.append(
            {
                "id": clean(row.get("BILL_ID") or bill_no, title),
                "category": category_from_committee(committee),
                "title": title,
                "status": "공식 원문 연동" if source_connected else "공식 기본정보",
                "stage": stage,
                "committee": committee,
                "proposed_date": proposed_date,
                "updated_at": today,
                "review_count": 0,
                "summary": excerpt(official_summary),
                "official_summary": official_summary,
                "official_summary_status": "연동 완료" if source_connected else "자료 없음 또는 연동 대기",
                "analysis_status": "미분석",
                "why_now": (
                    "이 영역은 공식 원문과 별도로 작성될 Democracy 3.0 국민용 해설 영역입니다. "
                    "아직 분석되지 않았습니다."
                ),
                "beneficiaries": ["Democracy 3.0 분석 전"],
                "cost_bearers": ["Democracy 3.0 분석 전"],
                "authority_changes": ["Democracy 3.0 분석 전"],
                "strengths": ["Democracy 3.0 분석 전"],
                "risks": ["Democracy 3.0 분석 전"],
                "questions": [
                    "법안이 해결하려는 문제가 공식 원문에서 명확한가?",
                    "재정과 집행기관이 구체적으로 설계되어 있는가?",
                    "적용대상과 예상 부작용이 충분히 검토되었는가?",
                ],
                "scores": {
                    "문제정의": 0,
                    "근거": 0,
                    "재정": 0,
                    "집행": 0,
                    "기본권": 0,
                    "악용방지": 0,
                    "성과지표": 0,
                    "재검토": 0,
                },
                "forks": [],
                "official": {
                    "age": clean(row.get("AGE"), age),
                    "bill_no": bill_no,
                    "proposer": clean(row.get("PROPOSER"), proposer),
                    "lead_proposer": proposer,
                    "co_proposers": clean(row.get("PUBL_PROPOSER"), ""),
                    "result": process_result,
                    "source_url": detail_link,
                    "summary_api": "BPMBILLSUMMARY",
                },
            }
        )

    linked_count = sum(1 for bill in bills if bill.get("official_summary"))

    if bills and linked_count == 0:
        raise RuntimeError(
            "법안 목록은 수집했지만 공식 제안이유·주요내용이 한 건도 연결되지 않았습니다. "
            "BPMBILLSUMMARY 호출 로그를 확인해야 합니다."
        )

    output = {
        "generated_at": now.isoformat(timespec="seconds"),
        "notice": (
            "대한민국 국회 열린국회정보에서 자동수집한 제22대 국회 최신 의안입니다. "
            f"기본정보 {len(bills)}건 중 공식 제안이유·주요내용 {linked_count}건을 연결했습니다. "
            "공식 원문과 Democracy 3.0 분석은 서로 구분하여 표시합니다."
        ),
        "source": {
            "provider": "대한민국 국회 열린국회정보",
            "list_api": "국회의원 발의법률안",
            "summary_api": "법률안 제안이유 및 주요내용",
            "official_total_count": total_count,
            "retrieved_count": len(bills),
            "official_summary_count": linked_count,
            "summary_error_count": len(summary_errors),
        },
        "stats": {
            "tracked": len(bills),
            "official_summaries": linked_count,
            "ai_drafts": 0,
            "citizen_review": 0,
            "forks": 0,
        },
        "collection_warnings": summary_errors[:20],
        "bills": bills,
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"공식 전체 {total_count}건 중 최신 {len(bills)}건 수집, "
        f"제안이유·주요내용 {linked_count}건 연동 완료."
    )
    if summary_errors:
        print(f"개별 상세 연동 실패 {len(summary_errors)}건은 다음 실행에서 재시도합니다.")


if __name__ == "__main__":
    main()
