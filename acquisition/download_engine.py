"""Download-first upgrade for the existing collector and SQLite library.

Persistent per-location outcomes, polite host dispatch, cross-provider OA lookup,
and measurable HTTP attempts. Uses only public OA links and free official APIs.
"""
from __future__ import annotations
from project_paths import resolve_project_path, pointer_path
import asyncio, hashlib, json, re, time, uuid, shutil
import xml.etree.ElementTree as ET
from collections import Counter,defaultdict
from datetime import datetime,timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse,quote,urlencode
import httpx
from resilient_http import RecoveringClient
from network_recovery import transient_network_error,transient_request_error,retry_delay
from collect import Runner,ROOT,STATE,DOMAINS,dump,stamp,doi_norm,arxiv_id,paper_keys,publisher_urls,validate,allowed_url
from corpus_store import load_config
ENGINE_CONFIG=load_config()

PMC_BUCKET='https://pmc-oa-opendata.s3.amazonaws.com'
RESOLVERS=['pmc_cloud','openalex','unpaywall','europe_pmc','semantic_scholar','arxiv']
API_HOSTS={'pmc_cloud':'pmc-oa-opendata.s3.amazonaws.com','openalex':'api.openalex.org','unpaywall':'api.unpaywall.org','europe_pmc':'www.ebi.ac.uk','semantic_scholar':'api.semanticscholar.org','arxiv':'export.arxiv.org'}

def provider(host):
    h=(host or '').lower()
    if 'arxiv.org' in h: return 'arXiv'
    if 'pmc-oa-opendata' in h: return 'PMC Open Data'
    if 'ncbi.nlm.nih.gov' in h: return 'NCBI PMC Portal'
    if 'europepmc' in h or 'ebi.ac.uk' in h: return 'Europe PMC'
    if 'nature.com' in h: return 'Nature'
    if 'springer' in h: return 'Springer'
    if 'frontiersin.org' in h: return 'Frontiers'
    if 'wiley' in h: return 'Wiley'
    if 'acs.org' in h: return 'ACS'
    if 'mdpi' in h: return 'MDPI'
    if 'plos.org' in h: return 'PLOS'
    if 'aclanthology' in h or 'aclweb' in h: return 'ACL Anthology'
    return h or 'unknown'

def normalize_url(url):
    if not isinstance(url,str): return ''
    url=url.strip()
    if 'arxiv.org/' in url:
        aid=arxiv_id(url)
        if aid: return 'https://arxiv.org/pdf/'+aid
    return url.replace('_reference.pdf','.pdf')

def pmcids(p):
    values=[p.get('source_id') or '',p.get('url') or '',*p.get('pdf_urls',[])]
    values.append(dump(p.get('raw_source_evidence') or {})); values.append(dump(p.get('raw') or {}))
    ids=set(re.findall(r'PMC\d+', ' '.join(values),re.I))
    ids.update('PMC'+n for n in re.findall(r'pubmedcentral.nih.gov:(\d+)', ' '.join(values)))
    ids.update(p.get('pmcids') or [])
    return sorted(x.upper() for x in ids)

def retry_after(headers,default=120):
    value=headers.get('retry-after','')
    try: return max(default,float(value))
    except ValueError:
        try: return max(default,parsedate_to_datetime(value).timestamp()-time.time())
        except (TypeError,ValueError,OverflowError): return default

class HostDeferred(Exception):
    def __init__(self,host,until):
        super().__init__('host_cooldown: '+host); self.until=until

class HostGate:
    def __init__(self,db):
        self.db=db; self.active=Counter(); self.next_start={}; self.cooldown={k[5:]:v for k,v in db.execute("select key,value from controls where key like 'host:%'")}; self.recent_403=defaultdict(list)
    def policy(self,host,api=False):
        if api: return 1,6 if host=='export.arxiv.org' else 4 if 'semanticscholar' in host else 1.5
        if host and 'arxiv.org' in host: return 1,4
        if host and ('europepmc' in host or 'ncbi.nlm.nih.gov' in host): return 1,3
        return 2,2
    def ready(self,host,api=False):
        limit,_=self.policy(host,api)
        return self.active[host]<limit and max(self.cooldown.get(host,0),self.next_start.get(host,0))<=time.time()
    def reserve(self,host,api=False):
        if not self.ready(host,api): return False
        _,spacing=self.policy(host,api); self.active[host]+=1; self.next_start[host]=time.time()+spacing; return True
    def release(self,host): self.active[host]=max(0,self.active[host]-1)
    def block(self,host,seconds):
        end=max(self.cooldown.get(host,0),time.time()+seconds); self.cooldown[host]=end
        self.db.execute('insert into controls values(?,?) on conflict(key) do update set value=excluded.value',('host:'+host,end)); self.db.commit()
    def forbidden(self,host):
        now=time.time(); self.recent_403[host]=[t for t in self.recent_403[host] if now-t<600]+[now]
        if len(self.recent_403[host])>=3: self.block(host,3600)

class OptimizedRunner(Runner):
    def network_ready(self):return not getattr(self,'network_recovery',None) or self.network_recovery.ready()
    def network_source_host(self,source):return API_HOSTS.get(source,source)
    async def network_http_response(self,response):self.network_response(response.url.host)
    def network_failure(self,host,error):
        if getattr(self,'network_recovery',None):self.network_recovery.failure(host,error)
    def network_response(self,host):
        if getattr(self,'network_recovery',None):self.network_recovery.response(host)
    def __init__(self,lib,args):
        super().__init__(lib,args)
        self.root=ROOT; self.state=STATE; self.db=lib.db; self.started=time.monotonic(); self.run_id=uuid.uuid4().hex[:16]
        self.completed=0; self.exported_at=0; self.exported_completed=0; self.search_paused=True; self.reconcile_at=0; self.network_active=0; self.peak_network=0
        self.validation_gate=asyncio.Semaphore(1)
        self.init_schema(); self.gate=HostGate(self.db); self.reconcile(initial=True)
        self.baseline_files=self.db.execute('select count(distinct sha256) from files').fetchone()[0]
        self.db.execute('insert into engine_runs values(?,?,?,?,?,?,?)',(self.run_id,stamp(),None,self.baseline_files,'running',dump(vars(args)),None)); self.db.commit()
        lib.event('optimization_run_started',run_id=self.run_id,baseline_pdf_count=self.baseline_files,workers=args.download_workers,resolver_workers=args.resolver_workers)
    def init_schema(self):
        self.db.executescript('''
        create table if not exists download_locations(paper_id text,url text,host text,provider text,provenance text,status text default 'pending',attempts integer default 0,next_retry real default 0,last_error text,updated text,primary key(paper_id,url));
        create index if not exists location_ready on download_locations(status,next_retry,host);
        create index if not exists paper_queue on papers(status,next_retry,updated);
        create index if not exists job_queue on jobs(status,next_retry,priority);
        create index if not exists file_digest on files(sha256);
        create table if not exists resolution_queue(paper_id text,provider text,status text default 'pending',attempts integer default 0,next_retry real default 0,error text,updated text,primary key(paper_id,provider));
        create index if not exists resolution_ready on resolution_queue(status,next_retry,provider);
        create table if not exists http_attempts(id integer primary key,run_id text,paper_id text,stage text,provider text,host text,url text,outcome text,http_status integer,seconds real,bytes integer,error text,created text);
        create index if not exists attempt_run on http_attempts(run_id,stage,provider,outcome);
        create table if not exists engine_runs(run_id text primary key,started text,finished text,baseline_files integer,status text,parameters text,summary text);
        ''')
        self.db.execute("update download_locations set status='pending' where status='inflight'")
        self.db.execute("update resolution_queue set status='pending' where status='inflight'")
        self.db.execute("update download_locations set provider='PMC Open Data' where host='pmc-oa-opendata.s3.amazonaws.com'")
        self.db.execute("update download_locations set provider='NCBI PMC Portal' where host like '%ncbi.nlm.nih.gov'")
        if self.args.retry_failed:
            self.db.execute("update download_locations set status='pending',attempts=0,next_retry=0 where status in ('transient','exhausted')")
            self.db.execute("update resolution_queue set status='pending',attempts=0,next_retry=0 where status in ('retry','unavailable') and coalesce(error,'') not like '%403%' and coalesce(error,'') not like '%401%'")
        self.db.commit()
    def add_url(self,pid,url,origin):
        url=normalize_url(url)
        if not url or urlparse(url).scheme not in ['http','https']: return False
        host=urlparse(url).hostname or ''
        if any(x in host.lower() for x in ['sci-hub','scihub','libgen','annas-archive','z-lib','localhost']): return False
        status='landing' if host in ['doi.org','dx.doi.org'] else 'pending'
        cursor=self.db.execute('insert or ignore into download_locations(paper_id,url,host,provider,provenance,status,updated) values(?,?,?,?,?,?,?)',(pid,url,host,provider(host),origin,status,stamp()))
        return bool(cursor.rowcount)
    def add_resolution(self,pid,name):
        self.db.execute('insert or ignore into resolution_queue(paper_id,provider,updated) values(?,?,?)',(pid,name,stamp()))
    def seed_resolution(self,pid,p,status):
        if getattr(self,'cfg',{}).get('language_policy',{}).get('publisher_oa_resolution') and p.get('is_open_access') and p.get('url') and status in ['no_oa_url','failed','suspected_incomplete']:
            self.add_resolution(pid,'publisher_oa')
        if pmcids(p): self.add_resolution(pid,'pmc_cloud')
        if p.get('doi') and status in ['no_oa_url','failed','suspected_incomplete']:
            self.add_resolution(pid,'openalex')
            if getattr(self.settings,'unpaywall_email',None) or getattr(self.settings,'openalex_email',None):self.add_resolution(pid,'unpaywall')
            resolvers=set(r for d in ENGINE_CONFIG['domains'] if d['domain_name'] in p.get('domains',[]) for r in d.get('oa_resolvers',['semantic_scholar','arxiv']))
            for name in resolvers:
                if name in RESOLVERS:self.add_resolution(pid,name)
    def reconcile(self,initial=False):
        for pid,data,status,error in self.db.execute("select id,data,status,error from papers where status not in ('downloaded','excluded_relevance')").fetchall():
            p=json.loads(data)
            for url in list(dict.fromkeys(p.get('pdf_urls',[])+publisher_urls(p))): self.add_url(pid,url,'existing_metadata')
            for key in paper_keys(p):
                if key.startswith('arxiv:'): self.add_url(pid,'https://arxiv.org/pdf/'+key[6:],'arxiv_id')
            self.seed_resolution(pid,p,status)
            if initial and error:
                # Migrate only URLs explicitly present in real historical error messages.
                bad_urls=re.findall(r"(?:url |URL: )['\"]?(https?://[^\s'\"<>|]+)",error)
                if '403' in error:
                    for url in bad_urls:
                        self.db.execute("update download_locations set status='forbidden',last_error=?,updated=? where paper_id=? and url=?",(error,stamp(),pid,normalize_url(url)))
        self.db.commit(); self.reconcile_at=time.monotonic()
    def search_allowed(self):
        if time.monotonic()-self.started<self.args.backlog_first_minutes*60: self.search_paused=True; return False
        # Pause based on runnable locations, not closed/blocked papers that need OA lookup.
        count=self.db.execute("select count(distinct l.paper_id) from download_locations l join papers p on p.id=l.paper_id where p.status in ('pending','failed','no_oa_url','suspected_incomplete') and l.status in ('pending','transient','cooldown') and l.next_retry<=? and l.host not in (select substr(key,6) from controls where key like 'host:%' and value>?)",(time.time(),time.time())).fetchone()[0]
        if self.search_paused and count<=self.args.search_backlog_low: self.search_paused=False
        elif not self.search_paused and count>=self.args.search_backlog_high: self.search_paused=True
        return not self.search_paused
    def claim(self):
        if not self.network_ready():return None
        now=time.time(); unavailable={h for h in set(self.gate.active)|set(self.gate.next_start)|set(self.gate.cooldown) if not self.gate.ready(h)}
        params=[now]; exclusion=''
        if unavailable:
            exclusion=' and l.host not in ('+','.join('?' for _ in unavailable)+')'; params.extend(sorted(unavailable))
        states="('pending','failed','no_oa_url','suspected_incomplete','excluded_relevance')" if hasattr(self,'corpus_download_filter') else "('pending','failed','no_oa_url','suspected_incomplete')"
        query="select p.id,p.data,l.url,l.host,l.provider,l.attempts from download_locations l join papers p on p.id=l.paper_id where p.status in "+states+" and l.status in ('pending','transient','cooldown') and l.next_retry<=?"+exclusion
        if hasattr(self,'corpus_download_filter'):query+=self.corpus_download_filter()
        ordering=" order by case when l.provenance not in ('existing_metadata','arxiv_id') then 0 else 1 end,l.attempts,p.updated limit 300"
        focus=self.preferred_download_domain() if hasattr(self,'preferred_download_domain') else None
        focus_filter=self.corpus_focus_filter() if hasattr(self,'corpus_focus_filter') else "exists(select 1 from json_each(p.data,'$.domains') where value=?)"
        rows=self.db.execute(query+" and "+focus_filter+ordering,params+[focus]).fetchall() if focus else []
        if getattr(self,'cfg',{}).get('language_policy',{}).get('enabled'):
            # Metadata only chooses queue opportunities; saved language uses PDF body.
            from corpus_continuous import setting,checkpoint,historical_slot
            turn=setting(self.db,'language_download_turn',0)
            if historical_slot(turn,.5):
                chinese=self.db.execute(query+" AND json_extract(p.data,'$.title') GLOB '*[一-龥]*'"+ordering,params).fetchall()
                if chinese:rows=chinese
        if not rows:rows=self.db.execute(query+ordering,params).fetchall()
        if not rows: return None
        if getattr(self,'cfg',{}).get('language_policy',{}).get('enabled'):checkpoint(self.db,'language_download_turn',turn+1)
        domain=DOMAINS[self.domain_index%len(DOMAINS)]; self.domain_index+=1
        scores={'PMC Open Data':0,'arXiv':1,'Frontiers':2,'Nature':3,'Springer':4,'ACL Anthology':5,'Europe PMC':30,'NCBI PMC Portal':30,'Wiley':25,'ACS':25,'MDPI':20}
        chosen=min(rows,key=lambda r:(0 if domain in json.loads(r[1]).get('domains',[]) else 1,self.candidate_priority(json.loads(r[1])) if hasattr(self,'candidate_priority') else 0,r[5],scores.get(r[4],10)))
        pid,data,url,host,source,attempts=chosen
        if hasattr(self,'normalize_download_candidate'):data=dump(self.normalize_download_candidate(pid,json.loads(data)))
        if not self.gate.reserve(host): return None
        self.db.execute("update papers set status='downloading',attempts=attempts+1,updated=? where id=?",(stamp(),pid))
        self.db.execute("update download_locations set status='inflight',attempts=attempts+1,updated=? where paper_id=? and url=?",(stamp(),pid,url)); self.db.commit()
        return pid,json.loads(data),url,host,source,attempts+1
    def attempt(self,pid,stage,source,host,url,outcome,code,elapsed,size=0,error=None):
        self.db.execute('insert into http_attempts(run_id,paper_id,stage,provider,host,url,outcome,http_status,seconds,bytes,error,created) values(?,?,?,?,?,?,?,?,?,?,?,?)',(self.run_id,pid,stage,source,host,url,outcome,code,elapsed,size,error,stamp())); self.db.commit()
    async def fetch(self,client,url,pid,source,stage='download',params=None):
        host=urlparse(url).hostname or ''; start=time.monotonic(); code=None; size=0; response_headers={}
        self.network_active+=int(stage=='download'); self.peak_network=max(self.peak_network,self.network_active); redirects=set()
        try:
            async with asyncio.timeout(self.args.request_deadline):
                current=str(httpx.URL(url,params=params)) if params else url
                for _ in range(8):
                    current_host=urlparse(current).hostname or ''
                    if source=='publisher_oa' and self.db.execute("SELECT 1 FROM download_locations WHERE paper_id=? AND url=? AND status IN ('forbidden','not_found')",(pid,normalize_url(current))).fetchone():raise ValueError('protected OA PDF location; keep prior denial')
                    if current_host!=host and current_host not in redirects:
                        if self.gate.cooldown.get(current_host,0)>time.time(): raise HostDeferred(current_host,self.gate.cooldown[current_host])
                        while not self.gate.reserve(current_host,api=stage=='resolve'):
                            await asyncio.sleep(.25)
                        redirects.add(current_host)
                    if not await asyncio.to_thread(allowed_url,current): raise ValueError('disallowed_url')
                    async with client.stream('GET',current,follow_redirects=False) as response:
                        self.network_response(current_host)
                        code=response.status_code; response_headers=dict(response.headers)
                        if response.has_redirect_location:
                            current=str(response.url.join(response.headers['location'])); continue
                        response.raise_for_status()
                        chunks=[]; limit=75*1024**2 if stage=='download' else 4*1024**2
                        async for chunk in response.aiter_bytes():
                            size+=len(chunk)
                            if size>limit: raise ValueError('response_too_large')
                            chunks.append(chunk)
                        return b''.join(chunks),code,time.monotonic()-start
                raise ValueError('too_many_redirects')
        except Exception as e:
            e.download_code=code; e.download_elapsed=time.monotonic()-start; e.download_bytes=size
            self.network_failure(host,e)
            if code==429: self.gate.block(host,retry_after(response_headers))
            if isinstance(e,httpx.PoolTimeout) and hasattr(client,'reset'):
                await client.reset();self.lib.event('http_client_recycled',run_id=self.run_id,paper_id=pid,reason='local_pool_timeout',source=source)
            raise
        finally:
            self.network_active-=int(stage=='download')
            for redirected_host in redirects: self.gate.release(redirected_host)
    def save_pdf(self,pid,p,content,info,existing_path=None):
        if existing_path and resolve_project_path(existing_path).is_file(): return resolve_project_path(existing_path)
        if getattr(self,'cfg',{}).get('language_policy',{}).get('enabled'):
            from corpus_languages import directory
            directory=directory(self.root,self.cfg,info.get('language','unknown'),p['domains'][0])
        else:
            directory=self.root/p['domains'][0]; directory.mkdir(exist_ok=True)
        path=directory/f'{pid}.pdf'
        if path.exists():
            if hashlib.sha256(path.read_bytes()).hexdigest()==info['sha256']: return path
            path=directory/f'{pid}_{info["sha256"][:8]}_{uuid.uuid4().hex[:6]}.pdf'
        part=path.with_name(path.name+'.'+uuid.uuid4().hex[:8]+'.part')
        with part.open('xb') as f:
            f.write(content); f.flush()
            import os
            os.fsync(f.fileno())
        part.rename(path); return path
    def metadata(self,pid):
        row=self.db.execute('select data,status,path,sha256,bytes,pages,error,attempts,next_retry from papers where id=?',(pid,)).fetchone()
        if not row: return
        data,status,path,sha,size,pages,error,attempts,nr=row
        p=json.loads(data); p.update(download_status=status,pdf_path=path or '',sha256=sha or '',file_bytes=size,page_count=pages,failure_reason=error or '',download_attempts=attempts,next_retry=nr)
        p['oa_lookup_status']=[dict(provider=name,status=state,attempts=n,next_retry=due,error=err) for name,state,n,due,err in self.db.execute('select provider,status,attempts,next_retry,error from resolution_queue where paper_id=?',(pid,))]
        path=self.state/'metadata'/f'{pid}.json'; temp=path.with_name(path.name+'.tmp'); temp.write_text(dump(p),encoding='utf-8'); temp.replace(path)
    def requeue(self,pid,error=None):
        row=self.db.execute('select data,status from papers where id=?',(pid,)).fetchone()
        if not row or row[1]=='downloaded': return
        ready=self.db.execute("select min(next_retry) from download_locations where paper_id=? and status in ('pending','transient','cooldown')",(pid,)).fetchone()[0]
        status='pending' if ready is not None else 'failed' if self.db.execute('select 1 from download_locations where paper_id=?',(pid,)).fetchone() else 'no_oa_url'
        if status=='no_oa_url' and error is None: error='no_oa_pdf: no downloadable OA address found so far; provider results preserved in oa_lookup_status'
        self.db.execute('update papers set status=?,next_retry=?,error=coalesce(?,error),updated=? where id=?',(status,ready or 0,error,stamp(),pid)); self.db.commit()
        self.seed_resolution(pid,json.loads(row[0]),status); self.db.commit(); self.metadata(pid)
    async def download(self,worker):
        timeout=httpx.Timeout(self.args.read_timeout,connect=self.args.connect_timeout,pool=5)
        async with RecoveringClient(timeout=timeout,limits=httpx.Limits(max_connections=12,max_keepalive_connections=6,keepalive_expiry=15),headers={'User-Agent':'paper-pilot-library/2.0','Accept':'application/pdf,*/*'},trust_env=True,verify=self.settings.ssl_verify) as client:
            while not self.stopping():
                claim=self.claim()
                if not claim: await asyncio.sleep(.35); continue
                pid,p,url,host,source,attempts=claim; start=time.monotonic(); code=None; content=b''
                try:
                    if getattr(self,'cfg',{}).get('fulltext',{}).get('prefer_biomedical_xml',False):
                        from fulltext_xml import preserve_jats
                        await preserve_jats(self,client,pid,p,reserved_host=host)
                    content,code,elapsed=await self.fetch(client,url,pid,source)
                    async with self.validation_gate:
                        info=await asyncio.to_thread(validate,content,p)
                    existing=self.db.execute('select path from files where sha256=?',(info['sha256'],)).fetchone()
                    path=await asyncio.to_thread(self.save_pdf,pid,p,content,info,existing[0] if existing else None)
                    self.lib.success(pid,path,info)
                    fresh=json.loads(self.db.execute('select data from papers where id=?',(pid,)).fetchone()[0])
                    fresh.update(downloaded_from=url,validation=info,pdf_path=str(path),is_open_access=True,download_origin=self.db.execute('select provenance from download_locations where paper_id=? and url=?',(pid,url)).fetchone()[0])
                    self.db.execute('update papers set data=? where id=?',(dump(fresh),pid))
                    self.db.execute("update download_locations set status='success',last_error=null,updated=? where paper_id=? and url=?",(stamp(),pid,url)); self.db.commit()
                    self.attempt(pid,'download',source,host,url,'success',code,elapsed,info['bytes'])
                    self.lib.event('download_ok',id=pid,path=str(path),run_id=self.run_id,url=url,provider=source,seconds=elapsed,**info)
                    self.metadata(pid); self.completed+=1
                    if hasattr(self,'download_completed'):self.download_completed(pid)
                    print(dump(dict(event='download',run_id=self.run_id,id=pid,provider=source,seconds=round(elapsed,2),pages=info['pages'])),flush=True)
                except asyncio.CancelledError:
                    self.db.execute("update download_locations set status='pending',updated=? where paper_id=? and url=? and status='inflight'",(stamp(),pid,url))
                    self.db.execute("update papers set status='pending',updated=? where id=? and status='downloading'",(stamp(),pid));self.db.commit()
                    if self.stopping():break
                    raise
                except Exception as e:
                    code=getattr(e,'download_code',code); elapsed=getattr(e,'download_elapsed',time.monotonic()-start)
                    error=(f'HTTP_{code}: ' if code and code>=400 else '')+(str(e) or type(e).__name__)[:240]
                    if isinstance(e,HostDeferred): status='cooldown'; due=e.until
                    elif isinstance(e,httpx.PoolTimeout):
                        status='transient';due=time.time()+30
                        self.db.execute('UPDATE download_locations SET attempts=max(0,attempts-1) WHERE paper_id=? AND url=?',(pid,url));self.db.execute('UPDATE papers SET attempts=max(0,attempts-1) WHERE id=?',(pid,))
                    elif code in [401,403]: status='forbidden'; due=0; self.gate.forbidden(host)
                    elif code in [404,410]: status='not_found'; due=0
                    elif code==429: status='cooldown'; due=self.gate.cooldown.get(host,time.time()+120)
                    elif transient_network_error(e):
                        status='transient';due=time.time()+retry_delay(attempts,getattr(self,'cfg',{}).get('network_recovery'));error='network_retry:'+type(e).__name__+': '+error
                        if hasattr(client,'reset'):await client.reset()
                    elif code and 500<=code<600:status='transient';due=time.time()+retry_delay(attempts,getattr(self,'cfg',{}).get('network_recovery'));error='service_retry:'+error
                    elif isinstance(e,(httpx.TransportError,TimeoutError)) or code and code>=500:
                        status='transient' if attempts<3 else 'exhausted'; due=time.time()+min(1800,30*2**attempts)
                    else: status='invalid'; due=0
                    self.db.execute('update download_locations set status=?,next_retry=?,last_error=?,updated=? where paper_id=? and url=?',(status,due,error,stamp(),pid,url)); self.db.commit()
                    self.attempt(pid,'download',source,host,url,status,code,elapsed,len(content),error)
                    self.requeue(pid,error); self.lib.event('download_error',id=pid,error=error,run_id=self.run_id,url=url,provider=source,seconds=elapsed)
                finally: self.gate.release(host)
    async def api(self,client,pid,name,url,params=None,text=False):
        host=urlparse(url).hostname or ''
        while not self.stopping() and not self.gate.reserve(host,api=True): await asyncio.sleep(.25)
        if self.stopping(): raise asyncio.CancelledError()
        try:
            payload,code,elapsed=await self.fetch(client,url,pid,name,'resolve',params)
            result=payload.decode('utf-8',errors='replace' if text=='html' else 'strict') if text else json.loads(payload)
            self.attempt(pid,'resolve',name,host,str(httpx.URL(url,params=params)) if params else url,'http_success',code,elapsed,len(payload)); return result
        except Exception as e:
            code=getattr(e,'download_code',None)
            self.attempt(pid,'resolve',name,host,url,'http_error',code,getattr(e,'download_elapsed',0),0,str(e)[:240])
            if code in [401,403]: self.gate.forbidden(host)
            raise
        finally: self.gate.release(host)
    def update_resolution(self,pid,name,urls,extra=None):
        row=self.db.execute('select data,status from papers where id=?',(pid,)).fetchone()
        if not row: return 0
        p=json.loads(row[0]); before=self.db.total_changes
        for url in urls: self.add_url(pid,url,name)
        added=self.db.total_changes-before
        p['pdf_urls']=list(dict.fromkeys(p.get('pdf_urls',[])+[normalize_url(u) for u in urls if normalize_url(u)]))
        if extra:
            p['pmcids']=sorted(set(p.get('pmcids',[])+extra.get('pmcids',[])))
            p.setdefault('oa_cross_checks',{})[name]=extra
        if p.get('pmcids'): self.add_resolution(pid,'pmc_cloud')
        self.db.execute('update papers set data=? where id=?',(dump(p),pid)); self.db.commit()
        if row[1]!='downloading': self.requeue(pid)
        if hasattr(self,'resolution_completed'):self.resolution_completed(pid,name,extra or {})
        return added
    async def resolve_one(self,client,pid,p,name):
        doi=doi_norm(p.get('doi')); urls=[]; extra={}; aid=next((k[6:] for k in paper_keys(p) if k.startswith('arxiv:')),None)
        if name=='publisher_oa':
            if not p.get('is_open_access') or not p.get('url'):return 0
            if self.db.execute("SELECT 1 FROM download_locations WHERE paper_id=? AND url=? AND status IN ('forbidden','not_found')",(pid,normalize_url(p['url']))).fetchone():return 0
            from publisher_oa import pdf_links
            content=await self.api(client,pid,name,p['url'],text='html')
            urls=pdf_links(content,p['url']);extra={'is_oa':True,'landing_page':p['url'],'permission_basis':'existing DOAJ/OpenAlex OA or publisher CC license evidence; public linked PDF only'}
        elif name=='openalex':
            if not doi: return 0
            work=await self.api(client,pid,name,'https://api.openalex.org/works/'+quote('https://doi.org/'+doi,safe=':/'))
            for loc in [work.get('best_oa_location') or {},*work.get('locations',[])]:
                if not loc.get('is_oa'): continue
                if loc.get('pdf_url'): urls.append(loc['pdf_url'])
                a=arxiv_id(loc.get('landing_page_url') or '')
                if a: urls.append('https://arxiv.org/pdf/'+a)
                extra.setdefault('pmcids',[]).extend(re.findall(r'PMC\d+',loc.get('landing_page_url') or '',re.I))
            extra.update(is_oa=work.get('open_access',{}).get('is_oa'),source_id=work.get('id'))
            if hasattr(self,'resolution_completed'):extra['source_record']=work
        elif name=='unpaywall':
            if not doi: return 0
            work=await self.api(client,pid,name,'https://api.unpaywall.org/v2/'+quote(doi,safe=''),{'email':self.settings.effective_unpaywall_email})
            for loc in [work.get('best_oa_location') or {},*work.get('oa_locations',[])]:
                if loc.get('url_for_pdf'): urls.append(loc['url_for_pdf'])
                a=arxiv_id(loc.get('url_for_landing_page') or '')
                if a: urls.append('https://arxiv.org/pdf/'+a)
                ids=re.findall(r'PMC\d+',loc.get('url_for_landing_page') or '',re.I)
                ids.extend('PMC'+n for n in re.findall(r'pubmedcentral.nih.gov:(\d+)',loc.get('pmh_id') or ''))
                extra.setdefault('pmcids',[]).extend(ids)
            extra.update(is_oa=work.get('is_oa'),oa_status=work.get('oa_status'))
        elif name=='europe_pmc':
            query='DOI:"'+doi+'"' if doi else p.get('source_id') or ''
            work=await self.api(client,pid,name,'https://www.ebi.ac.uk/europepmc/webservices/rest/search',{'query':query,'format':'json','resultType':'core','pageSize':5})
            for item in work.get('resultList',{}).get('result',[]):
                if doi and doi_norm(item.get('doi'))!=doi: continue
                if item.get('pmcid'): extra.setdefault('pmcids',[]).append(item['pmcid'])
                for loc in item.get('fullTextUrlList',{}).get('fullTextUrl',[]):
                    if loc.get('availabilityCode')=='OA' and loc.get('documentStyle')=='pdf' and loc.get('url'): urls.append(loc['url'])
                if hasattr(self,'resolution_completed'):extra['source_record']=item
        elif name=='pmc_cloud':
            evidence=[]
            for pmcid in pmcids(p):
                listing=await self.api(client,pid,name,PMC_BUCKET+'/',{'list-type':'2','prefix':'metadata/'+pmcid+'.','max-keys':30},text=True)
                keys=[node.text for node in ET.fromstring(listing).iter() if node.tag.endswith('}Key') and node.text and node.text.endswith('.json')]
                for key in keys:
                    meta=await self.api(client,pid,name,PMC_BUCKET+'/'+quote(key,safe='/'))
                    if str(meta.get('is_retracted','')).lower() in ['yes','true','1']: continue
                    if doi and meta.get('doi') and doi_norm(meta['doi'])!=doi: continue
                    if str(meta.get('is_pmc_openaccess','')).lower() not in ['yes','true','1'] and str(meta.get('is_manuscript','')).lower() not in ['yes','true','1']: continue
                    if meta.get('pdf_url'):
                        u=meta['pdf_url'].replace('s3://pmc-oa-opendata/',PMC_BUCKET+'/')
                        urls.append(u); evidence.append({k:meta.get(k) for k in ['pmcid','version','doi','license_code','is_manuscript','pdf_url']})
            extra['nlm_metadata']=evidence; extra['source']='NIH NLM NCBI PMC Article Datasets on AWS'; extra['accessed']=stamp()
        elif name=='semantic_scholar':
            if not doi: return 0
            work=await self.api(client,pid,name,'https://api.semanticscholar.org/graph/v1/paper/DOI:'+quote(doi,safe=''),{'fields':'title,externalIds,openAccessPdf,isOpenAccess,authors,abstract,venue,year,publicationTypes,publicationDate,fieldsOfStudy'})
            if hasattr(self,'resolution_completed'):extra['source_record']=work
            if work.get('openAccessPdf',{}).get('url'): urls.append(work['openAccessPdf']['url'])
            a=(work.get('externalIds') or {}).get('ArXiv')
            if a: urls.append('https://arxiv.org/pdf/'+a)
        elif name=='arxiv':
            if aid: urls.append('https://arxiv.org/pdf/'+aid)
            else:
                work=await self.api(client,pid,name,'https://export.arxiv.org/api/query',{'search_query':'ti:"'+p.get('title','').replace('"','')+'"','start':0,'max_results':3},text=True)
                atom='{http://www.w3.org/2005/Atom}'
                from collect import norm
                from difflib import SequenceMatcher
                for entry in ET.fromstring(work).findall(atom+'entry'):
                    title=entry.findtext(atom+'title') or ''
                    if SequenceMatcher(None,norm(title),norm(p.get('title'))).ratio()<.96: continue
                    a=arxiv_id(entry.findtext(atom+'id') or '')
                    if a: urls.append('https://arxiv.org/pdf/'+a)
        return self.update_resolution(pid,name,urls,extra)
    def resolver_host(self,name,p):
        return (urlparse(p.get('url') or '').hostname or 'invalid-oa-url') if name=='publisher_oa' else API_HOSTS[name]
    def claim_resolution(self):
        if not self.network_ready():return None
        now=time.time();extra=self.corpus_resolve_filter() if hasattr(self,'corpus_resolve_filter') else self.corpus_download_filter() if hasattr(self,'corpus_download_filter') else " and p.status!='excluded_relevance'"
        rows=self.db.execute("select q.paper_id,p.data,q.provider,q.attempts,p.status from resolution_queue q join papers p on p.id=q.paper_id where q.status in ('pending','retry') and q.next_retry<=? and p.status!='downloaded'"+extra+" order by case when p.status='failed' then 0 when q.provider='pmc_cloud' then 1 else 2 end,q.attempts,q.updated limit 500",(now,)).fetchall()
        for pid,data,name,attempts,status in rows:
            if hasattr(self,'api_budget_ready') and not self.api_budget_ready(name):continue
            if name=='unpaywall' and not (getattr(self.settings,'unpaywall_email',None) or getattr(self.settings,'openalex_email',None)):continue
            # HostGate already excludes the source actually in use; planned searches
            # must not starve every OA resolver during a long discovery request.
            if not self.gate.ready(self.resolver_host(name,json.loads(data)),api=True): continue
            self.db.execute("update resolution_queue set status='inflight',attempts=attempts+1,updated=? where paper_id=? and provider=?",(stamp(),pid,name)); self.db.commit()
            if hasattr(self,'normalize_download_candidate'):data=dump(self.normalize_download_candidate(pid,json.loads(data)))
            return pid,json.loads(data),name,attempts+1
        return None
    async def resolve_oa(self,worker):
        async with RecoveringClient(timeout=httpx.Timeout(15,connect=5),limits=httpx.Limits(max_connections=12,max_keepalive_connections=6,keepalive_expiry=15),headers={'User-Agent':'paper-pilot-library/2.0','Accept':'application/json,application/xml,*/*'},trust_env=True,verify=self.settings.ssl_verify) as client:
            while not self.stopping():
                chosen=self.claim_resolution()
                if not chosen: await asyncio.sleep(.5); continue
                pid,p,name,attempts=chosen
                resolver_host=self.resolver_host(name,p)
                try:
                    added=await self.resolve_one(client,pid,p,name)
                    self.db.execute("update resolution_queue set status='done',error=null,updated=? where paper_id=? and provider=?",(stamp(),pid,name)); self.db.commit()
                    self.lib.event('oa_crosscheck',run_id=self.run_id,id=pid,provider=name,new_locations=added)
                except asyncio.CancelledError:
                    self.db.execute("update resolution_queue set status='pending' where paper_id=? and provider=?",(pid,name)); self.db.commit(); break
                except Exception as e:
                    code=getattr(e,'download_code',None); permanent=code in [400,401,403,404,410] or attempts>=3 and code!=429
                    network=transient_request_error(e)
                    if network:permanent=False
                    if isinstance(e,httpx.PoolTimeout):permanent=False;self.db.execute('UPDATE resolution_queue SET attempts=max(0,attempts-1) WHERE paper_id=? AND provider=?',(pid,name))
                    status='unavailable' if permanent else 'retry'; due=max(time.time()+min(3600,30*2**min(attempts,7)),self.gate.cooldown.get(resolver_host,0))
                    detail=str(e)[:300] or type(e).__name__
                    if network:
                        detail='network_retry:'+type(e).__name__+': '+detail;due=max(time.time()+retry_delay(attempts,getattr(self,'cfg',{}).get('network_recovery')),self.gate.cooldown.get(resolver_host,0))
                        if hasattr(client,'reset'):await client.reset()
                    if hasattr(e,'budget_reset'):
                        status='retry';due=max(due,e.budget_reset);self.db.execute('UPDATE resolution_queue SET attempts=max(0,attempts-1) WHERE paper_id=? AND provider=?',(pid,name))
                    self.db.execute('update resolution_queue set status=?,next_retry=?,error=?,updated=? where paper_id=? and provider=?',(status,due,detail,stamp(),pid,name)); self.db.commit()
                self.metadata(pid)
    def metrics(self,status='running'):
        elapsed=(time.monotonic()-self.started)/60
        files=self.db.execute('select count(distinct sha256) from files').fetchone()[0]
        groups=defaultdict(Counter)
        for source,outcome,n,seconds in self.db.execute("select provider,outcome,count(*),sum(seconds) from http_attempts where run_id=? and stage='download' group by provider,outcome",(self.run_id,)):
            groups[source]['requests']+=n; groups[source]['seconds']+=seconds or 0
            if outcome=='success': groups[source]['success']+=n
            else: groups[source][outcome]+=n
        for value in groups.values(): value['success_rate']=value['success']/max(1,value['requests']); value['mean_seconds']=value['seconds']/max(1,value['requests'])
        extra=dict(self.db.execute('select status,count(*) from resolution_queue group by status'))
        m=dict(run_id=self.run_id,status=status,updated=stamp(),minutes=elapsed,baseline_files=self.baseline_files,current_files=files,new_pdf_files=files-self.baseline_files,new_downloaded_papers=self.completed,pdf_per_minute=(files-self.baseline_files)/max(.001,elapsed),download_workers=self.args.download_workers,resolver_workers=self.args.resolver_workers,active_download_requests=self.network_active,peak_download_requests=self.peak_network,active_hosts=dict(self.gate.active),source_rates=groups,resolution_queue=extra,search_paused=self.search_paused,export_seconds=getattr(self,'last_export_seconds',None))
        (self.state/'download_metrics.json').write_text(dump(m),encoding='utf-8'); return m
    async def monitor(self):
        reason='stopped_checkpoint_saved'
        while not self.stopping():
            await asyncio.sleep(5)
            if time.monotonic()-self.reconcile_at>60: self.reconcile()
            if time.monotonic()-self.exported_at>=self.args.export_seconds or self.completed-self.exported_completed>=100:
                t=time.monotonic(); self.lib.export(metadata=False); self.last_export_seconds=time.monotonic()-t; self.exported_at=time.monotonic(); self.exported_completed=self.completed
                # Keep RESULTS.md current in a separate read-only process; one exporter at a time.
                if hasattr(self,'report_process'): await self.report_process.wait()
                import sys
                self.report_process=await asyncio.create_subprocess_exec(sys.executable,str(self.state/'report.py'),stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
            m=self.metrics()
            free=shutil.disk_usage(self.root).free
            state=dict(pid=__import__('os').getpid(),status='running',updated=stamp(),free_bytes=free,minutes_budget=self.args.minutes,run_id=self.run_id,download_workers=self.args.download_workers,resolver_workers=self.args.resolver_workers,search_paused=self.search_paused,new_pdf_files=m['new_pdf_files'],pdf_per_minute=m['pdf_per_minute'])
            if free<self.args.reserve_gb*1024**3: self.stop=True; reason='stopped_resource_limit'; state['status']=reason
            if self.discovery_done:
                remaining=self.db.execute("select 1 from download_locations l join papers p on p.id=l.paper_id where p.status not in ('downloaded','excluded_relevance') and l.status in ('pending','transient','cooldown','inflight') limit 1").fetchone()
                lookup=self.db.execute("select 1 from resolution_queue q join papers p on p.id=q.paper_id where p.status not in ('downloaded','excluded_relevance') and q.status in ('pending','retry','inflight') limit 1").fetchone()
                if not remaining and not lookup: self.stop=True;reason='completed_frontier';state['status']=reason
            (self.state/'run_state.json').write_text(dump(state),encoding='utf-8')
        self.stop=True
        return reason
    async def run(self):
        tasks=[asyncio.create_task(self.discover())]
        tasks.extend(asyncio.create_task(self.download(i)) for i in range(self.args.download_workers))
        tasks.extend(asyncio.create_task(self.resolve_oa(i)) for i in range(self.args.resolver_workers))
        self.engine_tasks=tasks
        try:
            reason=await self.monitor(); await asyncio.gather(*tasks)
            self.db.execute("update resolution_queue set status='pending' where status='inflight'"); self.db.execute("update download_locations set status='pending' where status='inflight'"); self.db.commit()
            self.lib.export(metadata=False); summary=self.metrics(reason)
            runfile=self.state/'run_state.json'
            runstate=json.loads(runfile.read_text(encoding='utf-8')) if runfile.exists() else {}
            runstate.update(status=reason,updated=stamp(),run_id=self.run_id,new_pdf_files=summary['new_pdf_files'],current_pdf_files=summary['current_files'],pdf_per_minute=summary['pdf_per_minute'],active_download_requests=0,peak_download_requests=self.peak_network)
            runfile.write_text(dump(runstate),encoding='utf-8')
            self.db.execute('update engine_runs set finished=?,status=?,summary=? where run_id=?',(stamp(),reason,dump(summary),self.run_id)); self.db.commit()
            (self.state/f'run-metrics-{self.run_id}.json').write_text(dump(summary),encoding='utf-8')
        finally:
            self.stop=True
            for task in tasks:
                if not task.done(): task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            if hasattr(self,'report_process'): await self.report_process.wait()
