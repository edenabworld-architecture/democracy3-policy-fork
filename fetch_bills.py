"""Democracy 3.0 정책포크: 열린국회정보 의안 자동수집기."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

API_URL = "https://open.assembly.go.kr/portal/openapi/ALLBILLV2"
OUTPUT = Path(__file__).resolve().parent / "bills.json"
KST = timezone(timedelta(hours=9))


def find_rows(value: Any) -> list[dict[str, Any]]:
    """열린국회정보 응답 안에서 row 목록을 안전하게 찾습니다."""
    if isinstance(value, dict):
        row = value.get("row")
        if isinstance(row, list) and all(isinstance(x, dict) for x in row):
            return row
        for child in value.values():
            found = find_rows(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_rows(child)
            if found:
                return found
    return []


def find_api_error(value: Any) -> tuple[str, str] | None:
    """응답 안의 RESULT 오류코드를 찾아 사람이 읽을 수 있게 반환합니다."""
    if isinstance(value, dict):
        code = value.get("CODE")
        message = value.get("MESSAGE")
        if isinstance(code, str) and code not in {"INFO-000", "INFO-100"}:
            if code.startswith("ERROR") or code == "INFO-200":
                return code, str(message or "")
        for child in value.values():
            found = find_api_error(child)
            if found:
                return found
    elif isinstance(value, list):
        for child in value:
            found = find_api_error(child)
            if found:
                return found
    return None


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
    for needles, category in rules:
        if any(word in name for word in needles):
            return category
    return "기타"


def clean(value: Any, fallback: str = "확인 필요") -> str:
    text = str(value or "").strip()
    return text or fallback


def main() -> None:
    key = os.environ.get("ASSEMBLY_API_KEY", "").strip()
    if not key:
        raise RuntimeError("ASSEMBLY_API_KEY가 설정되지 않았습니다.")

    age = os.environ.get("ASSEMBLY_AGE", "22").strip()
    params = {
        "KEY": key,
        "Type": "json",
        "pIndex": 1,
        "pSize": 100,
        "AGE": age,
    }

    response = requests.get(API_URL, params=params, timeout=60)
    response.raise_for_status()
    payload = response.json()

    api_error = find_api_error(payload)
    if api_error and api_error[0] != "INFO-200":
        raise RuntimeError(f"국회 API 오류 {api_error[0]}: {api_error[1]}")

    rows = find_rows(payload)
    rows.sort(key=lambda row: clean(row.get("PROPOSE_DT"), ""), reverse=True)

    now = datetime.now(KST)
    today = now.strftime("%Y-%m-%d")
    bills: list[dict[str, Any]] = []

    for row in rows:
        title = clean(row.get("BILL_NAME") or row.get("BILL_NM"), "제목 확인 필요")
        committee = clean(
            row.get("CURR_COMMITTEE")
            or row.get("COMMITTEE_NM")
            or row.get("COMMITTEE_NAME"),
            "소관위원회 확인 필요",
        )
        proposed = clean(row.get("PROPOSE_DT"), "제안일 확인 필요")
        proposer = clean(row.get("PROPOSER"), "제안자 확인 필요")
        result = clean(row.get("PROC_RESULT_CD"), "")
        stage = f"처리결과: {result}" if result else "심사 진행 중"

        bills.append(
            {
                "id": clean(row.get("BILL_ID") or row.get("BILL_NO"), title),
                "category": category_from_committee(committee),
                "title": title,
                "status": "공식자료 자동수집",
                "stage": stage,
                "committee": committee,
                "proposed_date": proposed,
                "updated_at": today,
                "review_count": 0,
                "summary": f"제{age}대 국회에 {proposed} 제안된 의안입니다. 제안자는 {proposer}이며, 현재 소관은 {committee}입니다.",
                "why_now": "제안이유와 주요내용은 의안 상세 API 연동 후 원문에 근거해 표시할 예정입니다.",
                "beneficiaries": ["상세 원문 분석 전"],
                "cost_bearers": ["재정·비용 분석 전"],
                "authority_changes": ["권한 변화 분석 전"],
                "strengths": ["Democracy 3.0 분석 전"],
                "risks": ["Democracy 3.0 분석 전"],
                "questions": [
                    "제안이유와 정책목표는 무엇인가?",
                    "재정과 집행기관은 구체적으로 설계되어 있는가?",
                    "적용대상과 예상 부작용은 충분히 검토되었는가?",
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
                    "proposer": proposer,
                    "result": result,
                    "source_url": clean(row.get("LINK_URL"), ""),
                },
            }
        )

    output = {
        "generated_at": now.isoformat(timespec="seconds"),
        "notice": (
            "열린국회정보 공식 API에서 자동수집한 제22대 국회 의안입니다. "
            "현재는 기본정보만 연결되어 있으며, 상세 원문 분석과 정책 포크는 다음 단계에서 추가됩니다."
        ),
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
    print(f"{len(bills)}건을 {OUTPUT.name}에 저장했습니다.")


if __name__ == "__main__":
    main()
