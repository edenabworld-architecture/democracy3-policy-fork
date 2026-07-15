"""Democracy 3.0 정책포크: 열린국회정보 제22대 국회 의안 자동수집기."""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

API_KEY_ENV = "ASSEMBLY_API_KEY"
API_ROOT_KEY = "nzmimeepazxkubdpn"
API_URL = f"https://open.assembly.go.kr/portal/openapi/{API_ROOT_KEY}"
OUTPUT = Path(__file__).resolve().parent / "bills.json"
KST = timezone(timedelta(hours=9))


def clean(value: Any, fallback: str = "확인 필요") -> str:
    text = str(value or "").strip()
    return text or fallback


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


def parse_response(payload: Any) -> tuple[int, list[dict[str, Any]]]:
    data = payload.get(API_ROOT_KEY) if isinstance(payload, dict) else None

    if not isinstance(data, list) or len(data) < 2:
        if isinstance(payload, dict) and isinstance(payload.get("RESULT"), dict):
            result = payload["RESULT"]
            raise RuntimeError(
                f"국회 API 오류 {result.get('CODE', '')}: "
                f"{result.get('MESSAGE', '')}"
            )
        raise RuntimeError(f"예상하지 못한 국회 API 응답입니다: {str(payload)[:500]}")

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

    if result and result.get("CODE") != "INFO-000":
        raise RuntimeError(
            f"국회 API 오류 {result.get('CODE', '')}: "
            f"{result.get('MESSAGE', '')}"
        )

    rows = data[1].get("row") or []
    if isinstance(rows, dict):
        rows = [rows]
    if not isinstance(rows, list):
        rows = []

    return total_count, [row for row in rows if isinstance(row, dict)]


def main() -> None:
    api_key = os.environ.get(API_KEY_ENV, "").strip()
    if not api_key:
        raise RuntimeError(f"{API_KEY_ENV}가 설정되지 않았습니다.")

    age = os.environ.get("ASSEMBLY_AGE", "22").strip()

    response = requests.get(
        API_URL,
        params={
            "KEY": api_key,
            "Type": "json",
            "pIndex": 1,
            "pSize": 100,
            "AGE": age,
        },
        headers={
            "Accept": "application/json",
            "User-Agent": "Democracy3-Policy-Fork/0.3",
        },
        timeout=60,
    )
    response.raise_for_status()

    try:
        payload = response.json()
    except requests.JSONDecodeError as exc:
        raise RuntimeError(
            "국회 API가 JSON이 아닌 응답을 반환했습니다. "
            f"응답 앞부분: {response.text[:300]}"
        ) from exc

    total_count, rows = parse_response(payload)
    rows.sort(
        key=lambda row: clean(row.get("PROPOSE_DT"), ""),
        reverse=True,
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
        stage = (
            f"처리결과: {process_result}"
            if process_result
            else "심사 진행 중"
        )
        detail_link = normalize_url(row.get("DETAIL_LINK"))

        bills.append(
            {
                "id": clean(row.get("BILL_ID") or row.get("BILL_NO"), title),
                "category": category_from_committee(committee),
                "title": title,
                "status": "공식자료 자동수집",
                "stage": stage,
                "committee": committee,
                "proposed_date": proposed_date,
                "updated_at": today,
                "review_count": 0,
                "summary": (
                    f"제{age}대 국회에 {proposed_date} 제안된 의안입니다. "
                    f"대표발의자는 {proposer}이며, 소관위원회는 {committee}입니다."
                ),
                "why_now": (
                    "제안이유와 주요내용은 의안 상세정보를 추가 연동한 뒤 "
                    "공식 원문에 근거해 표시할 예정입니다."
                ),
                "beneficiaries": ["상세 원문 분석 전"],
                "cost_bearers": ["재정·비용 분석 전"],
                "authority_changes": ["권한 변화 분석 전"],
                "strengths": ["Democracy 3.0 분석 전"],
                "risks": ["Democracy 3.0 분석 전"],
                "questions": [
                    "법안이 해결하려는 문제가 명확한가?",
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
                    "bill_no": clean(row.get("BILL_NO"), ""),
                    "proposer": clean(row.get("PROPOSER"), proposer),
                    "lead_proposer": proposer,
                    "co_proposers": clean(row.get("PUBL_PROPOSER"), ""),
                    "result": process_result,
                    "source_url": detail_link,
                },
            }
        )

    output = {
        "generated_at": now.isoformat(timespec="seconds"),
        "notice": (
            "대한민국 국회 열린국회정보 Open API에서 자동수집한 "
            f"제{age}대 국회 의안입니다. 현재 기본정보 {len(bills)}건을 표시하며, "
            "상세 원문 분석과 정책 포크는 다음 단계에서 추가됩니다."
        ),
        "source": {
            "provider": "대한민국 국회 열린국회정보",
            "api": "국회의원 발의법률안",
            "official_total_count": total_count,
            "retrieved_count": len(bills),
        },
        "stats": {
            "tracked": len(bills),
            "ai_drafts": 0,
            "citizen_review": 0,
            "forks": 0,
        },
        "bills": bills,
    }

    OUTPUT.write_text(
        json.dumps(output, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"공식 전체 {total_count}건 중 최신 {len(bills)}건을 "
        f"{OUTPUT.name}에 저장했습니다."
    )


if __name__ == "__main__":
    main()
