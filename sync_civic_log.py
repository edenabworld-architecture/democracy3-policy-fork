#!/usr/bin/env python3
"""GitHub 공개 의견을 Democracy 3.0 시민참여 로그로 정규화합니다."""
from __future__ import annotations
import json, os, re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import requests

ROOT=Path(__file__).resolve().parent
OUTPUT=ROOT/'civic-log.json'
REPO=os.getenv('GITHUB_REPOSITORY','edenabworld-architecture/democracy3-policy-fork')
TOKEN=os.getenv('GITHUB_TOKEN','')
TYPE_PREFIX={
 '[주석]':'annotation','[정정]':'correction','[근거]':'evidence','[레드팀]':'red_team',
 '[포크]':'fork','[정책 포크 제안]':'fork','[설명 정정·개선]':'correction'
}
STATUS_LABELS={'accepted':'반영','partial':'부분반영','rejected':'기각','reviewing':'검토 중','evidence-checked':'근거 확인','hold':'보류'}

def read_old()->dict[str,Any]:
    try:return json.loads(OUTPUT.read_text(encoding='utf-8'))
    except Exception:return {'schema_version':'1.0','entries':[]}

def classify(issue:dict[str,Any])->str:
    labels=[str(x.get('name','')).lower() for x in issue.get('labels') or []]
    for candidate in ('annotation','correction','evidence','red_team','policy-fork','fork'):
        if candidate in labels:return 'fork' if candidate in {'policy-fork','fork'} else candidate
    title=str(issue.get('title',''))
    for prefix,kind in TYPE_PREFIX.items():
        if title.startswith(prefix):return kind
    return 'public_comment'

def status(issue:dict[str,Any])->str:
    labels=[str(x.get('name','')).lower() for x in issue.get('labels') or []]
    for label,value in STATUS_LABELS.items():
        if label in labels:return value
    return '접수' if issue.get('state')=='open' else '보류'

def policy_ref(title:str, body:str)->dict[str,str]:
    merged=title+'\n'+body
    id_match=re.search(r'PRC_[A-Z0-9]+',merged)
    bill_match=re.search(r'(?:법안명|정책명)\s*:\s*(.+)',body)
    return {'policy_id':id_match.group(0) if id_match else '', 'policy_title':bill_match.group(1).strip() if bill_match else ''}

def fetch_issues()->list[dict[str,Any]]:
    if not TOKEN:return []
    headers={'Authorization':f'Bearer {TOKEN}','Accept':'application/vnd.github+json','X-GitHub-Api-Version':'2022-11-28'}
    results=[]
    for page in range(1,11):
        r=requests.get(f'https://api.github.com/repos/{REPO}/issues',params={'state':'all','per_page':100,'page':page},headers=headers,timeout=25)
        r.raise_for_status(); rows=r.json()
        if not rows:break
        results.extend(x for x in rows if 'pull_request' not in x)
        if len(rows)<100:break
    return results

def main()->None:
    old=read_old(); issues=fetch_issues()
    if not issues and not TOKEN:
        old['generated_at']=datetime.now(timezone.utc).isoformat(); old['sync_status']='token_missing_preserved'
        OUTPUT.write_text(json.dumps(old,ensure_ascii=False,indent=2),encoding='utf-8'); return
    entries=[]
    for issue in issues:
        body=str(issue.get('body') or '')
        ref=policy_ref(str(issue.get('title','')),body)
        entries.append({
          'entry_id':f"GH-{issue.get('number')}",'type':classify(issue),**ref,
          'title':issue.get('title',''),'content':body,'status':status(issue),
          'author':((issue.get('user') or {}).get('login','')),
          'created_at':issue.get('created_at',''),'updated_at':issue.get('updated_at',''),
          'public_url':issue.get('html_url',''),'source':'GitHub 공개 의견','version':issue.get('updated_at','')
        })
    payload={'schema_version':'1.0','generated_at':datetime.now(timezone.utc).isoformat(),'sync_status':'ok','count':len(entries),'entries':entries}
    OUTPUT.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
if __name__=='__main__':main()
