"""Resumable OA collection. Reuses the installed Paper Pilot provider adapters.
No shadow libraries, paid APIs, full-text analysis, reports or PDF rendering.
"""
from __future__ import annotations
import argparse, asyncio, csv, hashlib, html, io, json, logging, os, re, shutil, sqlite3, sys, time, unicodedata, uuid, threading
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
import httpx
import pymupdf as fitz
try:
    from paper_pilot.config import load_settings
    from paper_pilot.services.academic import AcademicSearchService
    from paper_pilot.services.net import download_capped, is_public_http_url
    PAPER_PILOT_IMPORT_ERROR = None
except ModuleNotFoundError as error:
    PAPER_PILOT_IMPORT_ERROR = error
    AcademicSearchService = object

def require_paper_pilot():
    if PAPER_PILOT_IMPORT_ERROR is not None:
        raise RuntimeError('Original Paper Pilot environment unavailable. Restore its installation or set CORPUS_PYTHON; acquisition has not started.') from PAPER_PILOT_IMPORT_ERROR

from project_paths import resolve_project_path
from corpus_store import load_config,migrate
from corpus_rules import Classifier

ROOT = Path(__file__).resolve().parent.parent
STATE = ROOT / 'acquisition'
CORPUS_CONFIG=load_config()
DOMAINS = [d['domain_name'] for d in CORPUS_CONFIG['domains'] if d.get('enabled',True)]
TOPICS = {d['domain_name']:[(t['name'],(t.get('aliases') or [t['name']])[0]) for t in d['topics']] for d in CORPUS_CONFIG['domains']}
CLASSIFIER=Classifier(CORPUS_CONFIG)
PROVIDERS = ['openalex','doaj','semantic_scholar','crossref','europe_pmc','arxiv']
SEEDS = CORPUS_CONFIG.get('legacy_seed_domains',{})
PERIODS = [(y,y,'relevance') for y in sorted({y for d in CORPUS_CONFIG['domains'] for y in d['target_years']})]
logging.getLogger('httpx').setLevel(logging.WARNING)
PDF_VALIDATION_LOCK=threading.RLock()

def stamp(): return datetime.now(timezone.utc).isoformat()
def dump(x): return json.dumps(x,ensure_ascii=False)
def norm(s): return re.sub(r'[^\w]+',' ',unicodedata.normalize('NFKC',html.unescape(s or '')).lower()).strip()
def doi_norm(s): return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)','',(s or '').strip().lower())
def arxiv_id(s):
    if not s or not ('arxiv' in s.lower() or re.fullmatch(r'\d{4}\.\d{4,5}(v\d+)?',s)): return ''
    m=re.search(r'(\d{4}\.\d{4,5}|[a-z][a-z\-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?',s)
    return m.group(1) if m else ''
def paper_keys(p):
    keys=[]
    if p.get('doi'): keys.append('doi:'+doi_norm(p['doi']))
    raw=p.get('raw_source_evidence') or {}
    for value in [p.get('source_id'),raw.get('id')]:
        if value and (match:=re.search(r'(?:openalex\.org/)?(W\d+)$',str(value))) and (p.get('source')=='openalex' or 'openalex.org' in str(value)):keys.append('openalex:'+match.group(1))
    pmcid=raw.get('pmcid')
    if pmcid and re.fullmatch(r'PMC\d+',str(pmcid),re.I):keys.append('pmcid:'+str(pmcid).upper())
    if p.get('source')=='europe_pmc' and raw.get('source')=='MED' and str(raw.get('id','')).isdigit():keys.append('pmid:'+str(raw['id']))
    for s in [p.get('source_id'),p.get('url'),p.get('pdf_url'),*p.get('pdf_urls',[])]:
        a=arxiv_id(s)
        if a and 'arxiv:'+a not in keys: keys.append('arxiv:'+a)
    if p.get('title'): keys.append('title:'+norm(p['title'])+':'+str(p.get('year') or ''))
    return keys
def relevant(p,query):
    query=query.removeprefix('pilot:');domains=[d for d,ts in TOPICS.items() if any(norm(query) in [norm(en),norm(zh)] for en,zh in ts)]
    return any(CLASSIFIER.accepts(CLASSIFIER.classify(p,d)) for d in domains)
def allowed_url(url):
    require_paper_pilot()
    host=urlparse(url or '').hostname or ''
    return is_public_http_url(url or '') and not any(x in host.lower() for x in ['sci-hub','scihub','libgen','annas-archive','z-lib'])

def publisher_urls(p):
    if not p.get('is_open_access'): return []
    doi=doi_norm(p.get('doi')); urls=[]
    if doi.startswith('10.1038/'): urls.append('https://www.nature.com/articles/'+doi.split('/',1)[1]+'.pdf')
    if doi.startswith('10.3389/'): urls.append('https://www.frontiersin.org/articles/'+doi+'/pdf')
    if doi.startswith('10.7554/elife.'): urls.append('https://elifesciences.org/articles/'+doi.rsplit('.',1)[1]+'.pdf')
    if doi.startswith('10.1007/'): urls.append('https://link.springer.com/content/pdf/'+doi+'.pdf')
    if doi.startswith('10.1002/'): urls.append('https://onlinelibrary.wiley.com/doi/pdfdirect/'+doi)
    plos={'pone':'plosone','pgen':'plosgenetics','pbio':'plosbiology','pcbi':'ploscompbiol','ppat':'plospathogens','pmed':'plosmedicine','pntd':'plosntds'}
    if doi.startswith('10.1371/journal.'):
        key=doi.split('.')[2]
        if key in plos: urls.append('https://journals.plos.org/'+plos[key]+'/article/file?id='+doi+'&type=printable')
    return urls

class Library:
    def __init__(self):
        self.db=sqlite3.connect(STATE/'state.sqlite3')
        self.db.execute('pragma journal_mode=WAL')
        self.db.executescript('''
        create table if not exists papers(id text primary key,data text not null,status text not null,path text,sha256 text,bytes integer default 0,pages integer default 0,error text,attempts integer default 0,next_retry real default 0,updated text);
        create table if not exists keys(key text primary key,paper_id text);
        create table if not exists observations(id integer primary key,paper_id text,domain text,topic text,source text,source_id text,duplicate integer,payload text,job_id text,created text,unique(job_id,source,source_id));
        create table if not exists jobs(id text primary key,domain text,topic text,source text,lo integer,hi integer,sort text,cursor text,page integer,priority real,status text default 'pending',attempts integer default 0,next_retry real default 0,error text,total integer,created text,updated text);
        create table if not exists events(id integer primary key,time text,kind text,payload text);
        create table if not exists files(path text primary key,sha256 text,bytes integer,pages integer,validation text);
        create table if not exists controls(key text primary key,value real);
        ''')
        self.db.execute("update jobs set status='pending' where status='running'")
        self.db.execute("update papers set status='pending' where status='downloading'")
        self.db.commit(); self.since=0; self.host_time={}; self.cooldown={k[5:]:v for k,v in self.db.execute("select key,value from controls where key like 'host:%'")}
        migrate(self.db)
        for d in DOMAINS: (ROOT/d).mkdir(exist_ok=True)
        (STATE/'metadata').mkdir(exist_ok=True)
        self.seed_jobs()
    def event(self,kind,**payload):
        self.db.execute('insert into events(time,kind,payload) values(?,?,?)',(stamp(),kind,dump(payload)))
        self.db.commit()
    def seed_jobs(self):
        from corpus_planner import seed_annual_jobs
        seed_annual_jobs(self.db,CORPUS_CONFIG)
    def job(self,d,t,s,lo,hi,sort,cursor,page,priority):
        jid=hashlib.sha256(dump([d,t,s,lo,hi,sort,cursor]).encode()).hexdigest()[:24]
        self.db.execute('insert or ignore into jobs(id,domain,topic,source,lo,hi,sort,cursor,page,priority,created,updated) values(?,?,?,?,?,?,?,?,?,?,?,?)',(jid,d,t,s,lo,hi,sort,cursor,page,priority,stamp(),stamp()))
        return jid
    def ingest(self,p,d,t,jid):
        p=dict(p); p['doi']=doi_norm(p.get('doi')); p['title']=html.unescape(p.get('title') or 'Untitled')
        if not p.get('year') and p.get('publication_date'): p['year']=int(p['publication_date'][:4])
        p['pdf_urls']=list(dict.fromkeys(p.get('pdf_urls',[])+publisher_urls(p)))
        keys=paper_keys(p)
        matches=[r[0] for k in keys if (r:=self.db.execute('select paper_id from keys where key=?',(k,)).fetchone())]
        if not matches:
            fields={'doi':'doi','openalex':'openalex_work_id','pmid':'pmid','pmcid':'pmcid'}
            for key in keys:
                kind,_,value=key.partition(':')
                if kind not in fields:continue
                row=self.db.execute("SELECT paper_id FROM corpus_v2_metadata WHERE json_extract(normalized,'$."+fields[kind]+"')=? LIMIT 1",(value if kind!='openalex' else 'https://openalex.org/'+value,)).fetchone()
                if row:matches.append(row[0]);break
        pid=matches[0] if matches else hashlib.sha256((keys[0] if keys else dump(p)).encode()).hexdigest()[:24]
        old=self.db.execute('select data,status from papers where id=?',(pid,)).fetchone()
        passed=relevant(p,t)
        typ=str(p.get('document_type') or 'unknown').lower()
        excluded_type=any(x in typ for x in ['book-chapter','dataset','editorial','correction','erratum','letter','abstract','dissertation','monograph','paratext','supplementary','journal-issue','peer-review','news']) or typ=='book'
        excluded_title=bool(re.search(r'^(correction|erratum|retraction|corrigendum)\b',p['title'],re.I))
        eligible=passed and not excluded_type and not excluded_title
        if old:
            m=json.loads(old[0]); m['domains']=sorted(set(m['domains']+[d])); m['topics']=sorted(set(m['topics']+[t])); m['sources']=sorted(set(m['sources']+[p['source']])); m['retrieval_count']=m.get('retrieval_count',1)+1
            for k in ['abstract','authors','keywords','doi','year','publication_date','venue']:
                if not m.get(k) and p.get(k): m[k]=p[k]
            m['pdf_urls']=list(dict.fromkeys(m.get('pdf_urls',[])+p.get('pdf_urls',[])+([p['pdf_url']] if p.get('pdf_url') and p.get('is_open_access') else [])))
            m['eligible']=m.get('eligible',False) or eligible
            if m.get('document_type') in [None,'unknown'] and typ!='unknown': m['document_type']=p['document_type']; m['type_evidence']=p.get('type_evidence')
            status=old[1]
            if status=='excluded_relevance' and m['eligible']: status='pending' if m['pdf_urls'] else 'no_oa_url'
            elif status=='no_oa_url' and m['eligible'] and m['pdf_urls']: status='pending'
        else:
            m=p; m.update(domains=[d],topics=[t],sources=[p['source']],eligible=eligible,retrieval_count=1)
            m['pdf_urls']=list(dict.fromkeys(p.get('pdf_urls',[])+([p['pdf_url']] if p.get('pdf_url') and p.get('is_open_access') else [])))
            m['metadata_gaps']=[k for k in ['authors','abstract','year','doi','keywords','document_type'] if not m.get(k) or m.get(k)=='unknown']
            status='pending' if eligible and m['pdf_urls'] else 'no_oa_url' if eligible else 'excluded_relevance'
        m['id']=pid
        self.db.execute('insert into papers(id,data,status,updated) values(?,?,?,?) on conflict(id) do update set data=excluded.data,status=excluded.status,updated=excluded.updated',(pid,dump(m),status,stamp()))
        for k in keys: self.db.execute('insert or ignore into keys values(?,?)',(k,pid))
        self.db.execute('insert or ignore into observations(paper_id,domain,topic,source,source_id,duplicate,payload,job_id,created) values(?,?,?,?,?,?,?,?,?)',(pid,d,t,p['source'],p.get('source_id') or keys[0],int(bool(old)),dump(p),jid,stamp()))
        self.since+=1
        self.write_metadata(pid)
        if hasattr(self,'corpus_refresh'):self.corpus_refresh(pid)
        return pid
    def write_metadata(self,pid):
        row=self.db.execute('select data,status,path,sha256,bytes,pages,error,attempts,next_retry from papers where id=?',(pid,)).fetchone()
        if not row: return
        data,status,path,sha,size,pages,error,attempts,nr=row
        document=dict(json.loads(data),download_status=status,pdf_path=path or '',sha256=sha or '',file_bytes=size,page_count=pages,failure_reason=error or '',download_attempts=attempts,next_retry=nr)
        destination=STATE/'metadata'/f'{pid}.json'; temp=destination.with_name(destination.name+'.tmp'); temp.write_text(dump(document),encoding='utf-8'); temp.replace(destination)
    def import_seeds(self):
        for key,d in SEEDS.items():
            f=STATE/'seeds'/f'{key}.json'
            if not f.exists(): continue
            data=json.loads(f.read_text(encoding='utf-8')); jid='mcp_pilot_'+key
            if self.db.execute('select 1 from observations where job_id=?',(jid,)).fetchone(): continue
            for p in data['results']: self.ingest(p,d,data['topic'],jid)
            df=STATE/'seeds'/f'{key}_download.json'
            if df.exists():
                result=json.loads(df.read_text(encoding='utf-8')); dl=json.loads(next(x['text'] for x in result['content'] if x['type']=='text'))
                doi=dl.get('paper',{}).get('doi'); selected=next((p for p in data['results'] if p.get('doi')==doi),None)
                if selected:
                    selected=dict(selected); selected['document_type']='review' if key=='bio' else 'journal_article'; selected['type_evidence']='pilot PDF header' if key=='bio' else 'pilot publication venue; subtype unspecified'
                    pid=self.ingest(selected,d,'pilot:'+data['topic'],jid+'_download')
                    path=resolve_project_path(dl['pdf_path'])
                    if path.exists():
                        info=validate(path.read_bytes(),selected)
                        dest=ROOT/d/f'{pid}.pdf'
                        # These four files were created in this task; move without overwriting.
                        if not dest.exists(): path.rename(dest)
                        else: dest=path
                        self.success(pid,dest,info)
            self.event('mcp_pilot',domain=d,warnings=data.get('warnings'),source_status=data.get('source_status'))
        self.db.commit()
    def success(self,pid,path,info):
        self.db.execute("update papers set status='downloaded',path=?,sha256=?,bytes=?,pages=?,error=null,updated=? where id=?",(str(path),info['sha256'],info['bytes'],info['pages'],stamp(),pid))
        self.db.execute('insert or ignore into files values(?,?,?,?,?)',(str(path),info['sha256'],info['bytes'],info['pages'],dump(info)))
        self.db.commit()
    def export(self,metadata=True):
        rows=self.db.execute('select id,data,status,path,sha256,bytes,pages,error,attempts,next_retry from papers').fetchall()
        fields=['id','title','authors','year','publication_date','doi','arxiv_id','abstract','keywords','document_type','type_evidence','sources','domains','topics','eligible','retrieval_count','duplicates_removed','download_status','pdf_path','sha256','file_bytes','page_count','failure_reason','attempts','next_retry','metadata_gaps']
        corpus_meta={pid:json.loads(n) for pid,n in self.db.execute('SELECT paper_id,normalized FROM corpus_v2_metadata')};corpus_states={pid:dict(zip(['article_identity','downloaded','valid_pdf','relevant','accepted_for_corpus','duplicate_version','needs_review','excluded','exclusion_reason','reclassified_domains','reclassified_topics'],row)) for pid,*row in self.db.execute('SELECT paper_id,article_identity,downloaded,valid_pdf,relevant,accepted_for_corpus,duplicate_version,needs_review,excluded,exclusion_reason,domains,topics FROM corpus_v2_status')}
        corpus_fields=['first_submission_date','first_publication_date','published_online_date','published_print_date','source_reported_year','canonical_publication_year','date_source','formal_venue','canonical_formal_venue','formal_venue_id','publisher','repository','pdf_host','downloaded_version','preprint_source','conference_track','issn','field_provenance','normalized_doi','normalized_authors','normalized_abstract','normalized_keywords','canonical_document_type','article_identity','downloaded','valid_pdf','relevant','accepted_for_corpus','duplicate_version','needs_review','excluded','exclusion_reason','reclassified_domains','reclassified_topics'];fields+=corpus_fields
        corpus_fields+=['formal_doi','publication_status','local_file_version','formal_publication_date','first_public_date','volume','issue','pages','article_number','openalex_work_id','crossref_id','pmid','pmcid'];fields+=corpus_fields[-13:]
        tiers={pid:dict(literature_tier=tier,article_role=role,tier_evidence=json.loads(evidence),tier_version=v) for pid,tier,role,evidence,v in self.db.execute('SELECT paper_id,literature_tier,article_role,evidence,version FROM corpus_v4_tiers')} if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='corpus_v4_tiers'").fetchone() else {}
        fields+=['literature_tier','article_role','tier_evidence','tier_version']
        output=[]; ds={d:Counter() for d in DOMAINS}; coverage=defaultdict(Counter); errors=Counter(); statuses=Counter()
        for pid,data,status,path,sha,size,pages,error,attempts,nr in rows:
            p=json.loads(data); r={k:p.get(k,'') for k in fields}; r.update(arxiv_id=next((k[6:] for k in paper_keys(p) if k.startswith('arxiv:')),''),duplicates_removed=p.get('retrieval_count',1)-1,download_status=status,pdf_path=str(resolve_project_path(path)) if path else '',sha256=sha or '',file_bytes=size,page_count=pages,failure_reason=error or '',attempts=attempts,next_retry=nr)
            r.update({k:corpus_meta.get(pid,{}).get(k,'') for k in corpus_fields});r.update(corpus_states.get(pid,{}))
            n=corpus_meta.get(pid,{});r.update(tiers.get(pid,{}));r.update({alias:n.get(field,'') for alias,field in [('normalized_doi','doi'),('normalized_authors','authors'),('normalized_abstract','abstract'),('normalized_keywords','keywords'),('canonical_document_type','document_type')]})
            for k,v in list(r.items()):
                if isinstance(v,(list,dict)): r[k]=dump(v)
            output.append(r); statuses[status]+=1
            if metadata:
                document=dict(p,download_status=status,pdf_path=path or '',sha256=sha or '',file_bytes=size,page_count=pages,failure_reason=error or '',download_attempts=attempts,next_retry=nr)
                mp=STATE/'metadata'/f'{pid}.json'; serialized=dump(document)
                if not mp.exists() or mp.read_text(encoding='utf-8')!=serialized: mp.write_text(serialized,encoding='utf-8')
            if error and status in ['failed','suspected_incomplete']: errors[error.split(':')[0]]+=1
            for d in p['domains']:
                ds.setdefault(d,Counter())
                ds[d]['unique_candidates']+=1
                if p.get('eligible'): ds[d]['eligible']+=1
                ds[d][status]+=1
                if status=='downloaded': ds[d]['pdf_bytes']+=size
                for t in p['topics']:
                    if t.startswith('pilot:'): continue
                    coverage[(d,t)]['candidates']+=1
                    if p.get('eligible'): coverage[(d,t)]['eligible']+=1
                    if status=='downloaded': coverage[(d,t)]['downloaded']+=1
        def csvwrite(path,columns,items):
            temp=path.with_name(path.name+'.tmp')
            with temp.open('w',encoding='utf-8-sig',newline='') as f:
                w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore'); w.writeheader(); w.writerows(items)
            os.replace(temp,path)
        csvwrite(ROOT/'manifest.csv',fields,output)
        csvwrite(STATE/'failures.csv',fields,[r for r in output if r['download_status'] in ['failed','suspected_incomplete','no_oa_url']])
        cv=[dict(domain=d,topic=t,**v) for (d,t),v in coverage.items()]
        csvwrite(STATE/'coverage.csv',['domain','topic','candidates','eligible','downloaded'],cv)
        obs=self.db.execute('select paper_id,domain,topic,source,source_id,duplicate,job_id,created from observations').fetchall()
        of=['paper_id','domain','topic','source','source_id','duplicate','job_id','created']
        csvwrite(STATE/'retrievals.csv',of,[dict(zip(of,x)) for x in obs])
        for d in DOMAINS:
            csvwrite(ROOT/d/'index.csv',fields,[r for r in output if d in json.loads(r['domains'])])
            ds[d]['raw_retrievals']=sum(1 for x in obs if x[1]==d)
        files=self.db.execute('select count(distinct sha256),sum(bytes) from files').fetchone()
        jobs=dict(self.db.execute('select status,count(*) from jobs group by status').fetchall())
        stats=dict(updated=stamp(),domains=ds,unique_candidates=len(rows),retrievals=len(obs),duplicates_removed=len(obs)-len(rows),statuses=statuses,failure_reasons=errors,distinct_pdf_files=files[0],pdf_bytes=files[1] or 0,jobs=jobs,validation='PDF header, EOF, parser repair flag, every page structure, first/last page sampled text and title/DOI match; heuristic completeness, no full-text analysis')
        stats['corpus_v2']={'accepted_for_corpus':sum(bool(s.get('accepted_for_corpus')) for s in corpus_states.values()),'valid_pdf':sum(bool(s.get('valid_pdf')) for s in corpus_states.values()),'domain_accepted':{d:sum(bool(s.get('accepted_for_corpus')) and d in json.loads(s['reclassified_domains']) for s in corpus_states.values()) for d in DOMAINS},'legacy_domains_above_are_original_query_labels':True}
        if CORPUS_CONFIG.get('continuous',{}).get('enabled') and (STATE/'stats.json').exists():
            previous=json.loads((STATE/'stats.json').read_text(encoding='utf-8'));stats=dict(previous,**stats)
        from corpus_store import atomic_json
        atomic_json(STATE/'stats.json',stats); self.db.commit(); self.since=0
        return stats

def validate(content,p):
    with PDF_VALIDATION_LOCK:return _validate_locked(content,p)

def _validate_locked(content,p):
    if not content.startswith(b'%PDF-'): raise ValueError('not_pdf: server returned HTML or other content')
    if b'%%EOF' not in content[-4096:]: raise ValueError('suspected_incomplete: missing PDF EOF marker')
    with fitz.open(stream=content,filetype='pdf') as doc:
        if doc.is_repaired: raise ValueError('suspected_incomplete: parser repaired damaged PDF')
        if doc.is_encrypted: raise ValueError('encrypted_pdf: cannot validate')
        n=len(doc)
        if n<2: raise ValueError('suspected_incomplete: one-page file may be abstract or cover')
        for i in range(n): doc.load_page(i).get_contents()
        first=doc[0].get_text(); last=doc[-1].get_text(); front=' '.join(doc[i].get_text() for i in range(min(3,n))); sample=norm(front+' '+last)
        if len(sample)<600: raise ValueError('suspected_incomplete: very little sampled text; scan needs review')
        if re.search(r'^(supplementary|supporting information|references only|cover page)\b',first.strip(),re.I): raise ValueError('suspected_incomplete: supplement or cover')
        title_tokens=[x for x in norm(p.get('title')).split() if len(x)>3 and x not in {'with','from','using','based','study','review','applications'}]
        overlap=sum(x in norm(front) for x in title_tokens)/max(1,len(title_tokens))
        from corpus_languages import title_overlap,detect_pdf
        chinese_overlap=title_overlap(p.get('title'),front)
        if chinese_overlap is not None:overlap=chinese_overlap
        doi=doi_norm(p.get('doi'))
        if title_tokens and overlap<0.35 and not (doi and doi in front.lower()): raise ValueError('title_mismatch: PDF front pages do not match candidate title or DOI')
        detected=detect_pdf(doc)
    return dict(sha256=hashlib.sha256(content).hexdigest(),bytes=len(content),pages=n,validation='structural_and_sampled_content_pass',title_overlap=overlap,**detected)

class Adapter(AcademicSearchService):
    def __init__(self,settings,lib,job):
        require_paper_pilot()
        super().__init__(settings); self.lib=lib; self.job=job; self.raw_items={}
    async def _enrich_with_unpaywall(self,client,records,*,force_lookup=False):
        if not (self.settings.unpaywall_email or self.settings.openalex_email):
            return records,['Unpaywall fallback skipped: UNPAYWALL_EMAIL/OPENALEX_EMAIL not configured.']
        return await super()._enrich_with_unpaywall(client,records,force_lookup=force_lookup)
    def _parse_arxiv_feed(self,xml_text):
        import xml.etree.ElementTree as ET
        records=super()._parse_arxiv_feed(xml_text)
        ns={'a':'http://www.w3.org/2005/Atom','x':'http://arxiv.org/schemas/atom'}
        entries=ET.fromstring(xml_text).findall('a:entry',ns)
        for record,entry in zip(records,entries):
            self.raw_items[str(record.source_id)]={'arxiv_id':arxiv_id(record.source_id),'title':record.title,'authors':record.authors,'related_formal_doi':entry.findtext('x:doi','',ns),'journal_reference':entry.findtext('x:journal_ref','',ns),'first_submission_date':record.publication_date,'source':'arxiv'}
        return records
    async def _get_json(self,client,url,params,namespace,timeout=None):
        if hasattr(self.lib,'api_budget'):self.lib.api_budget(self.job['source'])
        if self.job.get('purpose')=='classic' and self.job['source']=='openalex':params=dict(params or {},sort='cited_by_count:desc')
        params=dict(params)
        if 'openalex.org' in url and self.job.get('language'):
            params['filter']=','.join(x for x in [params.get('filter'), 'language:'+self.job['language']] if x)
        if 'semanticscholar.org' in url:
            params['fields']=str(params.get('fields',''))+',publicationTypes'
        if 'openalex.org' in url and self.job['hi']==2014: params['sort']='cited_by_count:desc'
        # Save date/type/license evidence before the adapter normalizes records.
        result=await super()._get_json(client,url,params,namespace,timeout)
        items=result.get('results',result.get('data',result.get('message',{}).get('items',result.get('resultList',{}).get('result',[]))))
        for raw in items:
            source=self.job['source']; record=getattr(self,'_paper_from_'+source)(raw)
            self.raw_items[str(record.source_id)]=raw
        return result
    async def _get_text(self,client,url,params,namespace,timeout=None):
        if hasattr(self.lib,'api_budget'):self.lib.api_budget(self.job['source'])
        return await super()._get_text(client,url,params,namespace)
    def enrich(self,record):
        p=record.to_dict(); raw=self.raw_items.get(str(record.source_id),{}); s=record.source
        p['pdf_urls']=[]; typ='unknown'; evidence='unreported'
        if s=='openalex':
            typ=raw.get('type','unknown'); evidence='openalex.type'
            if typ=='article': typ='journal_article_unspecified'
            p['keywords']=list(dict.fromkeys([x.get('display_name') for x in raw.get('keywords',[]) if x.get('display_name')]+p.get('keywords',[])))
            for loc in [raw.get('best_oa_location') or {},*raw.get('locations',[])]:
                if loc.get('is_oa') and loc.get('pdf_url'): p['pdf_urls'].append(loc['pdf_url'])
            if raw.get('open_access',{}).get('is_oa'):
                for loc in raw.get('locations',[]):
                    aid=arxiv_id(loc.get('landing_page_url') or '')
                    if aid: p['pdf_urls'].append('https://arxiv.org/pdf/'+aid)
        elif s=='crossref':
            typ=raw.get('type','unknown'); evidence='crossref.type'
            # A Crossref link alone is not evidence of permission to retrieve full text.
            licenses=[x.get('URL','').lower() for x in raw.get('license',[])]
            p['is_open_access']=any('creativecommons.org' in u for u in licenses)
        elif s=='semantic_scholar': typ=';'.join(raw.get('publicationTypes') or []) or 'unknown'; evidence='semantic_scholar.publicationTypes'
        elif s=='europe_pmc':
            typ=';'.join(raw.get('pubTypeList',{}).get('pubType',[])) or 'unknown'; evidence='europe_pmc.pubTypeList'
            if p.get('is_open_access') and raw.get('pmcid'): p['pdf_urls'].append('https://europepmc.org/articles/'+raw['pmcid']+'?pdf=render')
        elif s=='arxiv': typ='preprint'; evidence='arxiv repository'
        elif s=='doaj': typ='journal_article_unspecified'; evidence='DOAJ journal index; subtype unspecified'
        p['document_type']=typ; p['type_evidence']=evidence
        from corpus_languages import metadata_language
        p['metadata_language']=metadata_language(dict(p,raw_source_evidence=raw))
        if p.get('pdf_url') and p.get('is_open_access'): p['pdf_urls'].append(p['pdf_url'])
        p['pdf_urls']=list(dict.fromkeys(p['pdf_urls'])); p['raw_source_evidence']=raw
        from corpus_metadata import basic_dates
        p.update(basic_dates(p))
        return p

class Runner:
    def __init__(self,lib,args):
        require_paper_pilot()
        self.lib=lib; self.args=args; self.until=time.monotonic()+args.minutes*60 if args.minutes else float('inf'); self.stop=False; self.source_ready={k[7:]:v for k,v in lib.db.execute("select key,value from controls where key like 'source:%'")}; self.last_domain=''; self.domain_index=0; self.discovery_done=False; self.settings=load_settings()
    def stopping(self): return self.stop or time.monotonic()>=self.until or (STATE/'STOP').exists()
    async def discover(self):
        while not self.stopping():
            if hasattr(self,'search_allowed') and not self.search_allowed():
                await asyncio.sleep(5); continue
            now=time.time(); rows=self.lib.db.execute("select id,domain,topic,source,lo,hi,sort,cursor,page,priority,attempts from jobs where status in ('pending','retry') and next_retry<=? order by priority limit 200",(now,)).fetchall()
            columns=['id','domain','topic','source','lo','hi','sort','cursor','page','priority','attempts']
            job=self.select_search_job(rows) if hasattr(self,'select_search_job') else next((dict(zip(columns,r)) for r in rows if self.source_ready.get(r[3],0)<=now),None)
            if not job:
                if not getattr(self,'continuous',False) and not rows and not self.lib.db.execute("select 1 from jobs where status in ('pending','retry')").fetchone(): break
                await asyncio.sleep(2); continue
            self.lib.db.execute("update jobs set status='running',updated=? where id=?",(stamp(),job['id'])); self.lib.db.commit()
            service=Adapter(self.settings,self.lib,job)
            s=job['source']; kwargs=dict(sort_by=job['sort'],cursor=job['cursor'])
            if hasattr(self,'reserve_search') and not self.reserve_search(s):
                self.lib.db.execute("update jobs set status='pending' where id=?",(job['id'],));self.lib.db.commit();await asyncio.sleep(1);continue
            if s in ['openalex','semantic_scholar','europe_pmc']: kwargs['open_access_only']=False
            if hasattr(self,'search_dispatched'):self.search_dispatched(job)
            try:
                async with httpx.AsyncClient(timeout=30,follow_redirects=True,headers=service._headers(),trust_env=True,verify=self.settings.ssl_verify,event_hooks={'response':[self.network_http_response]} if hasattr(self,'network_http_response') else None) as client:
                    async with asyncio.timeout(self.args.request_deadline):
                        page=await getattr(service,'_search_'+s)(client,job['topic'],self.args.page_size,job['lo'],job['hi'],**kwargs)
                for record in page.results:
                    pid=self.lib.ingest(service.enrich(record),job['domain'],job['topic'],job['id'])
                    if hasattr(self,'candidate_seen'):self.candidate_seen(pid)
                    # Metadata normalization must yield between records so PDF streams and
                    # host cooldown timers are serviced during a full search page.
                    await asyncio.sleep(0)
                if page.next_cursor and page.next_cursor!=job['cursor']:
                    # Second page is visited within each topic; later pages rotate behind breadth.
                    priority=job['priority']+45 if job['page']==0 else max(20000,job['priority'])+100
                    child=self.lib.job(job['domain'],job['topic'],s,job['lo'],job['hi'],job['sort'],page.next_cursor,job['page']+1,priority)
                    if hasattr(self,'search_child_created'):self.search_child_created(job['id'],child)
                self.lib.db.execute("update jobs set status='done',total=?,error=null,updated=? where id=?",(page.total,stamp(),job['id']))
                self.lib.event('search_ok',**job,returned=len(page.results),next_cursor=page.next_cursor,total=page.total,notes=page.notes)
                self.source_ready[s]=time.time()+({'arxiv':6,'semantic_scholar':4}.get(s,1.5))
                print(dump(dict(event='search',domain=job['domain'],topic=job['topic'],source=s,page=job['page'],returned=len(page.results))),flush=True)
            except Exception as e:
                code=getattr(getattr(e,'response',None),'status_code',None); attempt=job['attempts']+1
                backoff=min(3600,(120 if code==429 else 30)*2**min(attempt-1,5))
                retry_after=getattr(getattr(e,'response',None),'headers',{}).get('retry-after')
                if retry_after and retry_after.isdigit(): backoff=max(backoff,int(retry_after))
                err=f'HTTP_{code}' if code else type(e).__name__; detail=err+': '+str(e)[:400]
                from network_recovery import transient_network_error,retry_delay
                if transient_network_error(e):
                    detail='network_retry:'+detail;backoff=retry_delay(attempt,getattr(self,'cfg',{}).get('network_recovery'))
                    if hasattr(self,'network_failure'):self.network_failure(self.network_source_host(s),e)
                self.lib.db.execute("update jobs set status='retry',attempts=?,next_retry=?,error=?,updated=? where id=?",(attempt,time.time()+backoff,detail,stamp(),job['id']))
                self.source_ready[s]=time.time()+backoff
                self.lib.db.execute('insert into controls values(?,?) on conflict(key) do update set value=excluded.value',('source:'+s,self.source_ready[s])); self.lib.db.commit()
                self.lib.event('search_error',domain=job['domain'],topic=job['topic'],source=s,error=detail,backoff=backoff)
                print(dump(dict(event='search_error',source=s,error=err,backoff=backoff)),flush=True)
            finally:
                if hasattr(self,'release_search'):self.release_search(s)
            self.lib.db.commit()
            if self.lib.since>=100:
                self.lib.export(metadata=False); self.lib.since=0
            await asyncio.sleep(0.5)
        self.discovery_done=True

def main():
    parser=argparse.ArgumentParser(); parser.add_argument('--minutes',type=float,default=20,help='Session wall-time budget; 0 runs until stopped. No article count cap.'); parser.add_argument('--page-size',type=int,default=50); parser.add_argument('--reserve-gb',type=float,default=2); parser.add_argument('--export-only',action='store_true'); parser.add_argument('--retry-failed',action='store_true')
    parser.add_argument('--download-workers',type=int,default=6); parser.add_argument('--resolver-workers',type=int,default=2)
    parser.add_argument('--backlog-first-minutes',type=float,default=10); parser.add_argument('--search-backlog-low',type=int,default=250); parser.add_argument('--search-backlog-high',type=int,default=750)
    parser.add_argument('--connect-timeout',type=float,default=6); parser.add_argument('--read-timeout',type=float,default=20); parser.add_argument('--request-deadline',type=float,default=45); parser.add_argument('--export-seconds',type=float,default=120)
    parser.add_argument('--phase1',action='store_true')
    parser.add_argument('--corpus-mode',action='store_true');parser.add_argument('--enable-acquisition',action='store_true');parser.add_argument('--plan-only',action='store_true')
    parser.add_argument('--max-new-pdfs',type=int,default=None,help='Optional bounded incremental batch; in-flight claims count against the cap.')
    args=parser.parse_args()
    if not args.phase1 and not args.export_only:args.corpus_mode=True
    if not 1<=args.page_size<=100 or args.minutes<0 or args.reserve_gb<0: parser.error('Invalid runtime budget, page size or disk reserve.')
    if args.max_new_pdfs is not None and args.max_new_pdfs<1:parser.error('--max-new-pdfs must be positive.')
    if not 1<=args.download_workers<=12 or not 1<=args.resolver_workers<=4 or args.search_backlog_low>=args.search_backlog_high: parser.error('Invalid concurrency or backlog thresholds.')
    os.environ['PAPER_PILOT_DATA_DIR']=str(ROOT); os.environ['SCIHUB_ENABLED']='false'
    if (STATE/'repair_v2/repair.lock').exists() or (STATE/'repair_v3/repair.lock').exists():raise SystemExit('Corpus repair is active. Finish it before starting or planning acquisition.')
    if args.corpus_mode and (args.plan_only or not args.enable_acquisition):
        if (STATE/'collector.lock').exists():raise SystemExit('Collector active; planning may append annual queues and cannot run concurrently.')
        from corpus_planner import seed_annual_jobs,preview
        conn=sqlite3.connect(STATE/'state.sqlite3');migrate(conn);seed_annual_jobs(conn,CORPUS_CONFIG)
        print(dump(dict(mode='plan_only',network_started=False,preview=preview(conn,CORPUS_CONFIG,12))),flush=True);conn.close();return
    if not args.export_only: require_paper_pilot()
    if args.enable_acquisition and not args.minutes and args.max_new_pdfs is None and CORPUS_CONFIG.get('continuous',{}).get('enabled'):
        gate=STATE/'CONTINUOUS_SMOKE_CHECK.json'
        if not gate.exists() or not json.loads(gate.read_text(encoding='utf-8')).get('passed'):raise SystemExit('Unlimited mode requires a passed real smoke/resume check; no collector started.')
    lock=STATE/'collector.lock'
    try: fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError: raise SystemExit('Collector lock exists. Check the PID in collector.lock before resuming; do not run two collectors.')
    os.write(fd,str(os.getpid()).encode()); os.close(fd)
    lib=None
    try:
        lib=Library(); lib.import_seeds()
        # The exclusive process lock permits recovery of interrupted work without rewinding cursors.
        lib.db.execute("update jobs set status='pending' where status='running'")
        lib.db.execute("update papers set status='pending' where status='downloading'");lib.db.commit()
        # Reclassification lives in corpus_v2_*; never rescreen by rewriting original evidence.
        if args.retry_failed:
            lib.db.execute("update papers set status='pending',attempts=0,next_retry=0 where status in ('failed','suspected_incomplete')"); lib.db.commit()
            for pid,data in lib.db.execute('select id,data from papers').fetchall():
                p=json.loads(data)
                # Historical resolution evidence is retained; per-location queues implement retries.
            lib.db.commit()
        if args.export_only or not (args.corpus_mode and CORPUS_CONFIG.get('continuous',{}).get('enabled')):lib.export(metadata=args.export_only)
        if not args.export_only:
            from download_engine import OptimizedRunner
            phase_policy=STATE/'phase1_policy.json'
            if args.corpus_mode:
                from corpus_runner import CorpusRunner
                asyncio.run(CorpusRunner(lib,args).run())
            elif args.phase1 or phase_policy.exists() and json.loads(phase_policy.read_text(encoding='utf-8')).get('enabled'):
                completed=lib.db.execute("select value from phase1_state where key='completion'").fetchone()
                if completed and json.loads(completed[0]).get('goal_met'):
                    print('Historical phase one is complete. Use run_balanced.ps1 for a new reviewed stage.',flush=True);return
                from phase1_runner import PhaseRunner
                asyncio.run(PhaseRunner(lib,args).run())
            else:asyncio.run(OptimizedRunner(lib,args).run())
    except KeyboardInterrupt: print('Interrupted; saving checkpoint.',flush=True)
    finally:
        if lib:
            lib.db.execute("update jobs set status='pending' where status='running'"); lib.db.execute("update papers set status='pending' where status='downloading'"); lib.db.commit()
            print(dump(lib.export(metadata=args.export_only)),flush=True); lib.db.close()
        runfile=STATE/'run_state.json'
        if runfile.exists() and not args.export_only:
            runstate=json.loads(runfile.read_text(encoding='utf-8'))
            if runstate.get('status')=='running': runstate.update(status='stopped_checkpoint_saved',updated=stamp())
            runfile.write_text(dump(runstate),encoding='utf-8')
        lock.unlink(missing_ok=True)

if __name__=='__main__': main()
