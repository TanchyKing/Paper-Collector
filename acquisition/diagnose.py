import json,sqlite3,time
from collections import Counter,defaultdict
from datetime import datetime,timezone
from urllib.parse import urlparse
from pathlib import Path
root=Path(__file__).resolve().parent.parent
db=sqlite3.connect(root/'acquisition'/'state.sqlite3'); db.execute('begin')
rows=db.execute('select id,data,status,path,sha256,bytes,attempts,error from papers').fetchall()
statuses=Counter(); hosts=defaultdict(Counter); attempts=Counter(); resolutions=Counter(); pending_hosts=Counter()
preserved=[]
for pid,data,status,path,sha,size,n,error in rows:
    p=json.loads(data); statuses[status]+=1; attempts[status]+=n
    resolutions[(p.get('oa_resolution') or {}).get('source') or (p.get('oa_resolution') or {}).get('error') or 'not_checked']+=1
    if status=='downloaded':
        host=urlparse(p.get('downloaded_from') or p.get('pdf_url') or '').hostname or 'pilot_or_unknown'
        hosts[host]['downloaded_unique_papers']+=1
        preserved.append(dict(id=pid,path=path,sha256=sha,bytes=size))
    if status=='pending':
        urls=p.get('pdf_urls') or []
        pending_hosts[urlparse(urls[0]).hostname if urls else 'none']+=1
events=db.execute("select time,kind,payload from events where kind in ('download_ok','download_error') order by id").fetchall()
recent=[(t,k,json.loads(p)) for t,k,p in events if t>='2026-10-06T05:25:39']
success=[x for x in recent if x[1]=='download_ok']; fail=[x for x in recent if x[1]=='download_error']
repeat=Counter(x[2]['id'] for x in fail)
latest=datetime.now(timezone.utc)
minutes=(latest-datetime.fromisoformat('2026-10-06T05:25:39+00:00')).total_seconds()/60
result=dict(at=latest.isoformat(),statuses=statuses,preserved_pdfs=preserved,unique_pdf_count=db.execute('select count(distinct sha256) from files').fetchone()[0],historical_download_hosts=hosts,pending_first_url_hosts=pending_hosts,attempts_by_status=attempts,oa_resolutions=resolutions,baseline_window=dict(start='2026-10-06T05:25:39+00:00',minutes=minutes,download_success_events=len(success),download_failure_events=len(fail),success_per_minute=len(success)/minutes,repeated_failed_papers=sum(n>1 for n in repeat.values()),max_failures_one_paper=max(repeat.values(),default=0)),jobs=dict(db.execute('select status,count(*) from jobs group by status')))
db.rollback();db.close()
out=root/'acquisition'/'optimization-baseline.json';out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
result.pop('preserved_pdfs');print(json.dumps(result,ensure_ascii=False))
