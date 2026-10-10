from project_paths import resolve_project_path, pointer_path
"""Read-only Crossref enrichment against an immutable SQLite snapshot."""
import datetime,json,pathlib,sqlite3,time,hashlib,sys
import httpx
ROOT=pathlib.Path(__file__).resolve().parent.parent
FOLDER=pointer_path(ROOT/'acquisition/audit/LATEST.txt')
DB=sqlite3.connect('file:'+str(FOLDER/'snapshot.sqlite3').replace('\\','/')+'?mode=ro',uri=True)
CACHE=FOLDER/'crossref';CACHE.mkdir(exist_ok=True)
def normalized(doi):return (doi or '').lower().removeprefix('https://doi.org/').strip()
papers=[json.loads(r[0]) for r in DB.execute("select data from papers where status='downloaded'")]
dois=sorted({normalized(p.get('doi')) for p in papers if p.get('doi') and not normalized(p['doi']).startswith('10.48550/')},key=lambda d:(not any(normalized(p.get('doi'))==d and (p.get('year')==2026 or not p.get('venue')) for p in papers),d))
progress=FOLDER/'metadata_progress.json';log=FOLDER/'api_attempts.jsonl'
started=time.monotonic();outcomes={};finished=0
with httpx.Client(timeout=httpx.Timeout(18,connect=6),headers={'User-Agent':'LocalLiteratureAudit/1.0; read-only Crossref metadata verification'},follow_redirects=True) as client:
 for doi in dois:
  destination=CACHE/(hashlib.sha256(doi.encode()).hexdigest()+'.json')
  if destination.exists():finished+=1;continue
  # Share the current machine politely; avoid overlapping collector Crossref searches.
  for attempt in range(3):
   try:
    live=sqlite3.connect('file:'+str(ROOT/'acquisition/state.sqlite3').replace('\\','/')+'?mode=ro',uri=True,timeout=5)
    busy=live.execute("select count(*) from jobs where source='crossref' and status='running'").fetchone()[0];live.close()
    if busy:time.sleep(2)
    began=time.monotonic();response=client.get('https://api.crossref.org/works/'+__import__('urllib.parse',fromlist=['quote']).quote(doi,safe=''))
    code=response.status_code;record={'doi':doi,'status':code,'retrieved_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'url':str(response.url),'seconds':round(time.monotonic()-began,3)}
    if code==200:record['message']=response.json().get('message',{})
    else:record['error']=response.text[:600]
    with log.open('a',encoding='utf-8') as f:f.write(json.dumps({k:v for k,v in record.items() if k!='message'})+'\n')
    if code==429:
     delay=max(60,float(response.headers.get('retry-after','60') or 60));time.sleep(min(delay,120));continue
    if code>=500 and attempt<2:time.sleep(5*(attempt+1));continue
    destination.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8');outcomes[str(code)]=outcomes.get(str(code),0)+1;break
   except Exception as error:
    record={'doi':doi,'status':'network_error','error':type(error).__name__+': '+str(error),'retrieved_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    if attempt<2:time.sleep(2*(attempt+1));continue
    destination.write_text(json.dumps(record,ensure_ascii=False),encoding='utf-8');outcomes['network_error']=outcomes.get('network_error',0)+1
  finished+=1
  state={'completed':finished,'total':len(dois),'outcomes':outcomes,'elapsed_minutes':round((time.monotonic()-started)/60,2),'done':finished==len(dois)}
  temporary=progress.with_suffix('.json.tmp');temporary.write_text(json.dumps(state,ensure_ascii=False),encoding='utf-8');temporary.replace(progress)
  if finished%50==0:print(json.dumps(state),flush=True)
  time.sleep(.3)
print(progress.read_text(encoding='utf-8'),flush=True)
