"""Verify missing-DOI candidates from PDF title pages / declared publication venues."""
import pathlib,sqlite3,json,time,hashlib,datetime,re
from urllib.parse import quote
import httpx
from literature_audit import FOLDER,ROOT,similarity,doi_norm,crossref_title
db=sqlite3.connect(FOLDER/'snapshot.sqlite3')
papers={pid:json.loads(data) for pid,data in db.execute("select id,data from papers where status='downloaded'")}
checks={q['id']:q for q in map(json.loads,(FOLDER/'pdf_checks.jsonl').read_text(encoding='utf-8').splitlines())}
candidates=[]
for pid,p in papers.items():
 if p.get('doi'):continue
 q=checks[pid];found=q.get('pdf_dois') or []
 venue_evidence=re.search(r'accepted.{0,140}(conference|symposium|dac|iccad|neurips|iclr|icml)|(?:ieee|acm).{0,50}(conference|symposium)|journal.ref|Wiley Encyclopedia',q.get('front_text','')[:9000],re.I)
 if (found and len(found)<=3 and q.get('title_score',0)>=.5) or venue_evidence:candidates.append((pid,found,p['title']))
(FOLDER/'supplement_candidates.json').write_text(json.dumps(candidates,ensure_ascii=False),encoding='utf-8')
print('supplement candidates',len(candidates),flush=True)
while True:
 try:
  if json.loads((FOLDER/'metadata_progress.json').read_text()).get('done'):break
 except (OSError,ValueError):pass
 time.sleep(5)
result={}
path=FOLDER/'supplement_metadata.json'
if path.exists():result=json.loads(path.read_text(encoding='utf-8'))
with httpx.Client(timeout=httpx.Timeout(20,connect=6),headers={'User-Agent':'LocalLiteratureAudit/1.0; free missing-DOI metadata verification'},follow_redirects=True) as client:
 for i,(pid,found,title) in enumerate(candidates):
  if pid in result:continue
  record={'paper_id':pid,'verified':False,'attempts':[]}
  for doi in found:
   try:
    response=client.get('https://api.crossref.org/works/'+quote(doi,safe=''));record['attempts'].append({'url':str(response.url),'status':response.status_code})
    if response.status_code==429:time.sleep(60);continue
    if response.status_code==200:
     raw=response.json()['message'];score=similarity(title,crossref_title(raw))
     if score>=.9:record.update(verified=True,message=raw,title_similarity=score,evidence_url=str(response.url));break
   except Exception as error:record['attempts'].append({'error':type(error).__name__})
   time.sleep(.4)
  if not record['verified']:
   try:
    response=client.get('https://api.crossref.org/works',params={'query.title':title,'rows':3});record['attempts'].append({'url':str(response.url),'status':response.status_code})
    if response.status_code==200:
     items=response.json().get('message',{}).get('items',[])
     matches=[(similarity(title,crossref_title(raw)),raw) for raw in items];matches.sort(key=lambda item:-item[0])
     if matches and matches[0][0]>=.9 and (len(matches)<2 or matches[1][0]<.9 or matches[1][1].get('DOI')==matches[0][1].get('DOI')):
      record.update(verified=True,message=matches[0][1],title_similarity=matches[0][0],evidence_url=str(response.url))
     else:record['error']='ambiguous_or_low_title_match'
    elif response.status_code==429:record['error']='rate_limited_no_forcing';time.sleep(60)
   except Exception as error:record['error']=type(error).__name__
  record['verified_at']=datetime.datetime.now(datetime.timezone.utc).isoformat();result[pid]=record
  path.write_text(json.dumps(result,ensure_ascii=False),encoding='utf-8');time.sleep(1.1)
  if (i+1)%10==0:print('supplement completed',i+1,'/',len(candidates),flush=True)
print('supplement finished',len(result),sum(r['verified'] for r in result.values()),flush=True)
