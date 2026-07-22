#!/usr/bin/env python3
"""Democracy 3.0 공개 데이터 한국어·인코딩·위험중복 품질검사.

기본 실행:
    python scripts/validate_public_korean.py

엄격 실행:
    python scripts/validate_public_korean.py --strict

치명 오류:
- 깨진 인코딩 문자
- 사용자 노출 문자열에 원시 JSON
- 내부 필드명이 문장처럼 노출

경고:
- 한국어가 거의 없는 영문 자동분석
- 동일 위험문구 3회 이상 반복
- 너무 짧고 범용적인 위험문구
"""
from __future__ import annotations
import argparse, json, re, sys
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

ROOT = Path(__file__).resolve().parents[1]
BILLS_FILES = [ROOT / "bills-index.json", ROOT / "bills.json"]
REPORTS_DIR = ROOT / "reports"

MOJIBAKE = re.compile(r"�|Ã.|Â.|â€|â€™|ì[\x80-\xBF]|ë[\x80-\xBF]|ðŸ")
INTERNAL_KEY = re.compile(r"\b(red_team|largest_risk|failure_path|evidence_state|trigger_conditions|residual_risk|policy_mechanism)\b", re.I)
RAW_JSON = re.compile(r'^\s*[\[{].*[\]}]\s*$', re.S)
VISIBLE_KEYS = {
    "title","card_summary","summary","largest_risk","description","body","claim","attack","risk",
    "change","finding","reason","public_brief","brief","citizen_brief","red_team_attacks",
    "red_team","risks","failure_modes","forks","auto_forks","claim_ledger","claims"
}
GENERIC_PATTERNS = [
    re.compile(r"예산.*낭비"),
    re.compile(r"책임.*불명확"),
    re.compile(r"악용.*가능"),
    re.compile(r"부작용.*가능"),
    re.compile(r"집행.*어려"),
]

def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)

def walk(value: Any, path: str = "") -> Iterable[tuple[str, Any]]:
    if isinstance(value, dict):
        for k, v in value.items():
            child = f"{path}.{k}" if path else k
            yield child, v
            yield from walk(v, child)
    elif isinstance(value, list):
        for i, v in enumerate(value):
            child = f"{path}[{i}]"
            yield child, v
            yield from walk(v, child)

def is_visible_path(path: str) -> bool:
    parts = set(re.findall(r"[A-Za-z_]+", path))
    return bool(parts & VISIBLE_KEYS)

def normalize_risk(text: str) -> str:
    text = re.sub(r"https?://\S+", "", text.lower())
    return re.sub(r"[^\w가-힣]+", " ", text).strip()

def english_heavy(text: str) -> bool:
    cleaned = re.sub(r"https?://\S+", "", text)
    ko = len(re.findall(r"[가-힣]", cleaned))
    en = len(re.findall(r"[A-Za-z]", cleaned))
    return en > 45 and ko < 8 and en > ko * 4

def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--strict", action="store_true", help="경고도 실패로 처리")
    parser.add_argument("--json-output", default="public-quality-report.json")
    args = parser.parse_args()

    bills_path = next((p for p in BILLS_FILES if p.exists()), None)
    if not bills_path:
        print("[FAIL] bills-index.json 또는 bills.json이 없습니다.")
        return 2

    payload = load_json(bills_path)
    bills = payload.get("bills", payload if isinstance(payload, list) else [])
    files = [(bills_path, payload)]
    if REPORTS_DIR.exists():
        for p in sorted(REPORTS_DIR.glob("*.json")):
            try:
                files.append((p, load_json(p)))
            except Exception as e:
                files.append((p, {"__load_error__": str(e)}))

    fatal, warnings = [], []
    risk_counter: Counter[str] = Counter()
    risk_examples: dict[str, list[str]] = {}

    for path, data in files:
        if isinstance(data, dict) and "__load_error__" in data:
            fatal.append({"file": str(path.relative_to(ROOT)), "path": "", "issue": "JSON 읽기 실패", "text": data["__load_error__"]})
            continue
        for node_path, value in walk(data):
            if not isinstance(value, str) or not value.strip() or not is_visible_path(node_path):
                continue
            text = value.strip()
            rel = str(path.relative_to(ROOT))
            if MOJIBAKE.search(text):
                fatal.append({"file": rel, "path": node_path, "issue": "문자 인코딩 깨짐", "text": text[:180]})
            if RAW_JSON.match(text) and (":" in text or '"' in text):
                fatal.append({"file": rel, "path": node_path, "issue": "원시 JSON 문자열 노출", "text": text[:180]})
            if INTERNAL_KEY.search(text):
                fatal.append({"file": rel, "path": node_path, "issue": "내부 영문 필드명 노출", "text": text[:180]})
            if english_heavy(text):
                warnings.append({"file": rel, "path": node_path, "issue": "영문 비중이 높은 자동분석", "text": text[:180]})
            if node_path.endswith("largest_risk") or ".largest_risk" in node_path:
                key = normalize_risk(text)
                if key:
                    risk_counter[key] += 1
                    risk_examples.setdefault(key, []).append(text)
                if len(key) < 18 or any(p.search(text) for p in GENERIC_PATTERNS):
                    warnings.append({"file": rel, "path": node_path, "issue": "범용적이거나 짧은 위험문구", "text": text[:180]})

    for key, count in risk_counter.items():
        if count >= 3:
            warnings.append({
                "file": str(bills_path.relative_to(ROOT)),
                "path": "bills[*].largest_risk",
                "issue": f"동일 위험문구 {count}회 반복",
                "text": risk_examples[key][0][:180],
            })

    report = {
        "ok": not fatal and (not warnings or not args.strict),
        "strict": args.strict,
        "counts": {"bills": len(bills), "fatal": len(fatal), "warnings": len(warnings)},
        "fatal": fatal,
        "warnings": warnings,
    }
    output = ROOT / args.json_output
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"[INFO] 정책 {len(bills)}건 검사")
    print(f"[INFO] 치명 오류 {len(fatal)}건 / 경고 {len(warnings)}건")
    for item in fatal[:20]:
        print(f"[FAIL] {item['file']} {item['path']} · {item['issue']} · {item['text']}")
    for item in warnings[:20]:
        print(f"[WARN] {item['file']} {item['path']} · {item['issue']} · {item['text']}")
    print(f"[INFO] 상세 보고서: {output.relative_to(ROOT)}")

    if fatal:
        return 1
    if args.strict and warnings:
        return 1
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
