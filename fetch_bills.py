"""열린국회정보 Open API 수집기 골격."""
import json, os
from pathlib import Path
import requests

KEY=os.environ["ASSEMBLY_API_KEY"]
SERVICE=os.environ["ASSEMBLY_API_SERVICE"]
URL=f"https://open.assembly.go.kr/portal/openapi/{SERVICE}"
params={"KEY":KEY,"Type":"json","pIndex":1,"pSize":100,"AGE":os.getenv("ASSEMBLY_AGE","22")}
payload=requests.get(URL,params=params,timeout=30).json()

rows=[]
for value in payload.values():
    if isinstance(value,list):
        for item in value:
            if isinstance(item,dict) and isinstance(item.get("row"),list):
                rows=item["row"]

bills=[]
for row in rows:
    bills.append({
        "id":str(row.get("BILL_ID") or row.get("BILL_NO") or ""),
        "title":str(row.get("BILL_NAME") or row.get("BILL_NM") or "제목 확인 필요"),
        "status":"자동수집",
        "committee":row.get("CURR_COMMITTEE") or row.get("COMMITTEE_NAME") or "확인 필요",
        "summary":"원문 연동 및 국민용 설명 생성 전입니다.",
        "beneficiaries":[],"cost_bearers":[],"strengths":[],"risks":[],"scores":{},"forks":[],
        "source":row
    })

out=Path(__file__).parents[1]/"site/data/bills.json"
out.write_text(json.dumps({"notice":"공식 API 자동수집 자료입니다.","bills":bills},ensure_ascii=False,indent=2),encoding="utf-8")
print(len(bills), "건 저장")
