#!/usr/bin/env python3
"""정책의 입안·심사·통과·시행·성과를 버전과 책임 로그로 누적합니다."""
from __future__ import annotations
import hashlib, json, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT=Path(__file__).resolve().parent
BILLS=ROOT/'bills.json'; OUTPUT=ROOT/'policy-lifecycle.json'; OVERRIDES=ROOT/'review_overrides.json'; CIVIC=ROOT/'civic-log.json'
STAGES=[
 ('시행',100,("시행","시행중")),('공포',85,("공포",)),('정부이송',75,("정부이송",)),
 ('본회의 의결',65,("본회의","가결","의결")),('법사위',50,("법제사법","체계자구")),
 ('위원회 심사',35,("위원회","소위","심사")),('입법예고',20,("입법예고",)),('의안 접수',10,("접수","발의"))]

def read(path:Path,default:Any)->Any:
    try:return json.loads(path.read_text(encoding='utf-8'))
    except Exception:return default

def digest(obj:Any)->str:return hashlib.sha256(json.dumps(obj,ensure_ascii=False,sort_keys=True).encode()).hexdigest()
def stage_info(b:dict[str,Any])->tuple[str,int]:
    text=' '.join([str(b.get('stage','')),str((b.get('official') or {}).get('result',''))]+[str(x.get('label','')) for x in b.get('progress_timeline') or []])
    for label,pct,signals in STAGES:
        if any(s in text for s in signals):return label,pct
    return str(b.get('stage') or '상태 확인'),5

def main()->None:
    data=read(BILLS,{}); old=read(OUTPUT,{'policies':{}}); overrides=read(OVERRIDES,{'cases':{}}); civic=read(CIVIC,{'entries':[]})
    now=datetime.now(timezone.utc).isoformat(); policies={}; stage_counts={}
    civic_by={}
    for entry in civic.get('entries') or []:
        if entry.get('policy_id'):civic_by.setdefault(entry['policy_id'],[]).append(entry)
    for b in data.get('bills') or []:
        pid=str(b.get('id','')); previous=(old.get('policies') or {}).get(pid,{})
        stage,pct=stage_info(b); stage_counts[stage]=stage_counts.get(stage,0)+1
        manual=((overrides.get('cases') or {}).get(pid) or {}).get('implementation_tracking') or []
        manual_pct=max([float(x.get('progress_percent',0)) for x in manual]+[0])
        if manual_pct>pct:pct=int(manual_pct)
        snapshot={'title':b.get('title',''),'stage':stage,'official_result':(b.get('official') or {}).get('result',''),'committee':b.get('committee',''),'updated_at':b.get('updated_at','')}
        sh=digest(snapshot); versions=list(previous.get('versions') or []); events=list(previous.get('events') or [])
        if not versions or versions[-1].get('hash')!=sh:
            changed=[]
            if versions:
                prior=versions[-1].get('snapshot') or {}
                changed=[k for k in snapshot if prior.get(k)!=snapshot.get(k)]
            version_id=f"V{len(versions)+1:04d}"
            versions.append({'version_id':version_id,'at':now,'source':'자동 공식상태 추적','hash':sh,'changed_fields':changed,'snapshot':snapshot})
            events.append({'event_id':f"{pid}-{version_id}",'at':now,'type':'policy_state_change','source':'공식자료 자동추적','summary':f"정책 상태 기록: {stage}",'version_id':version_id})
        for item in manual:
            eid=str(item.get('event_id',''))
            if eid and not any(x.get('event_id')==eid for x in events):
                events.append({'event_id':eid,'at':item.get('date',''),'type':'implementation','source':item.get('source_url','수동 검증 원장'),'summary':item.get('summary') or item.get('issue') or item.get('status',''),'details':item})
        metrics=[x for x in manual if x.get('metric')]
        issues=[x.get('issue') for x in manual if x.get('issue')]
        benefits=[x.get('benefit') for x in manual if x.get('benefit')]
        budget_planned=sum(float(x.get('budget_planned',0) or 0) for x in manual)
        budget_executed=sum(float(x.get('budget_executed',0) or 0) for x in manual)
        policies[pid]={
          'policy_id':pid,'title':b.get('title',''),'policy_type':b.get('policy_type','국회 법률안'),'jurisdiction':b.get('jurisdiction') or {'level':'국가','name':'대한민국'},
          'current_stage':stage,'progress_percent':pct,'responsible_body':b.get('committee',''),'official_url':(b.get('official') or {}).get('source_url',''),
          'dates':{'proposed':b.get('proposed_date',''),'updated':b.get('updated_at','')},
          'budget':{'planned':budget_planned,'executed':budget_executed,'execution_rate':round(budget_executed/budget_planned*100,1) if budget_planned else None},
          'outcomes':{'metrics':metrics,'issues':issues,'benefits':benefits},
          'civic_activity':civic_by.get(pid,[]),'events':events[-200:],'versions':versions[-100:],
          'next_accountability_question':'예산·실행률·성과·부작용을 어떤 공식자료로 확인할 것인가?'
        }
    payload={'schema_version':'1.0','generated_at':now,'summary':{'policies':len(policies),'stage_counts':stage_counts,'manual_implementation_events':sum(len(((overrides.get('cases') or {}).get(pid) or {}).get('implementation_tracking') or []) for pid in policies),'civic_entries':sum(len(x.get('civic_activity') or []) for x in policies.values())},'policies':policies}
    OUTPUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
