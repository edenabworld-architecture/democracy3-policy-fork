#!/usr/bin/env python3
"""국회 법안 상세페이지의 숨은 첨부파일·AJAX·onclick 단서를 진단합니다."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import requests


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "document-diagnostics.json"
KEYWORDS = (
    "의안원문", "법률안 원문", "신구조문", "대비표", "비용추계",
    "미첨부", "검토보고", "심사보고", "회의록", "첨부", "download",
    "fileId", "atchFileId", "fileDown", "fnFile",
)


class DiagnosticParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.anchors: list[dict[str, str]] = []
        self.forms: list[dict[str, str]] = []
        self.scripts: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        values = {key.lower(): value or "" for key, value in attrs}
        if tag.lower() == "a":
            self.anchors.append(
                {
                    "href": values.get("href", ""),
                    "onclick": values.get("onclick", ""),
                    "title": values.get("title", ""),
                    "class": values.get("class", ""),
                }
            )
        elif tag.lower() == "form":
            self.forms.append(
                {
                    "action": values.get("action", ""),
                    "method": values.get("method", ""),
                    "id": values.get("id", ""),
                    "name": values.get("name", ""),
                }
            )
        elif tag.lower() == "script" and values.get("src"):
            self.scripts.append(values["src"])


def contexts(text: str, keyword: str, radius: int = 180) -> list[str]:
    result: list[str] = []
    lower = text.lower()
    needle = keyword.lower()
    start = 0
    while len(result) < 12:
        index = lower.find(needle, start)
        if index < 0:
            break
        snippet = re.sub(
            r"\s+",
            " ",
            text[max(0, index - radius): index + len(keyword) + radius],
        )
        result.append(snippet)
        start = index + len(keyword)
    return result


def diagnose(url: str, bill_id: str, title: str) -> dict[str, Any]:
    response = requests.get(
        url,
        timeout=25,
        headers={
            "User-Agent": "Mozilla/5.0 Democracy3-Document-Diagnostic/1.0",
            "Accept-Language": "ko-KR,ko;q=0.9",
        },
    )
    response.raise_for_status()
    parser = DiagnosticParser()
    parser.feed(response.text)

    anchor_candidates: list[dict[str, str]] = []
    for item in parser.anchors:
        combined = " ".join(item.values())
        if any(keyword.lower() in combined.lower() for keyword in KEYWORDS):
            anchor_candidates.append(
                {
                    **item,
                    "resolved_url": (
                        urljoin(url, item["href"]) if item["href"] else ""
                    ),
                }
            )

    onclick_pattern = re.compile(
        r'''onclick\s*=\s*["']([^"']+)["']''',
        re.IGNORECASE,
    )
    function_calls = sorted(set(onclick_pattern.findall(response.text)))

    endpoint_pattern = re.compile(
        r'''["']([^"']*(?:download|file|attach|bill|report)[^"']*\.(?:do|json|ajax|jsp)[^"']*)["']''',
        re.IGNORECASE,
    )
    endpoints = sorted(set(endpoint_pattern.findall(response.text)))

    return {
        "bill_id": bill_id,
        "title": title,
        "url": url,
        "http_status": response.status_code,
        "content_type": response.headers.get("content-type", ""),
        "html_length": len(response.text),
        "anchor_candidates": anchor_candidates[:100],
        "forms": parser.forms[:30],
        "script_sources": [
            urljoin(url, item) for item in parser.scripts[:50]
        ],
        "function_calls": function_calls[:100],
        "candidate_endpoints": [
            urljoin(url, item) for item in endpoints[:100]
        ],
        "keyword_contexts": {
            keyword: contexts(response.text, keyword)
            for keyword in KEYWORDS
            if keyword.lower() in response.text.lower()
        },
    }


def main() -> None:
    data = json.loads((ROOT / "bills.json").read_text(encoding="utf-8"))
    cases = (data.get("pilot_program") or {}).get("cases") or []
    results = []
    for case in cases:
        url = str(case.get("official_url", "")).strip()
        if not url:
            results.append(
                {
                    "bill_id": case.get("bill_id", ""),
                    "title": case.get("title", ""),
                    "error": "공식 상세페이지 주소 없음",
                }
            )
            continue
        try:
            results.append(
                diagnose(
                    url,
                    str(case.get("bill_id", "")),
                    str(case.get("title", "")),
                )
            )
        except Exception as exc:
            results.append(
                {
                    "bill_id": case.get("bill_id", ""),
                    "title": case.get("title", ""),
                    "url": url,
                    "error": str(exc),
                }
            )

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "purpose": "법안별 공식 첨부문서 접근경로 진단",
        "cases": results,
    }
    OUTPUT.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"진단 {len(results)}건 완료: {OUTPUT}")


if __name__ == "__main__":
    main()
