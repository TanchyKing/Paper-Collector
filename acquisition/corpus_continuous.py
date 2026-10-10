"""Incremental governance for the existing corpus. No changes to historical labels.

Evidence, tiers, bounded planner frontier and discovery proposals are additive.
All network operations remain in the original rate-limited engine.
"""
from __future__ import annotations
import collections,datetime,hashlib,json,math,os,re,shutil,sqlite3,time,html
from corpus_store import ROOT,STATE,load_config,now,dump,atomic_json,csvwrite

SCHEMA='''
CREATE TABLE IF NOT EXISTS corpus_v4_tiers(paper_id TEXT PRIMARY KEY,literature_tier TEXT,article_role TEXT,evidence TEXT,version TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v4_tier_history(evidence_id TEXT PRIMARY KEY,paper_id TEXT,literature_tier TEXT,article_role TEXT,evidence TEXT,version TEXT,recorded TEXT);
CREATE TABLE IF NOT EXISTS corpus_v4_job_policy(job_id TEXT PRIMARY KEY,topic_id TEXT,queue TEXT,purpose TEXT,score REAL,evidence TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS corpus_v4_proposals(proposal_id TEXT PRIMARY KEY,kind TEXT,definition TEXT,review_status TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v4_checkpoints(key TEXT PRIMARY KEY,value TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v4_budget(day TEXT,provider TEXT,requests INTEGER,PRIMARY KEY(day,provider));
CREATE TABLE IF NOT EXISTS corpus_v4_references(work_id TEXT PRIMARY KEY,domain TEXT,evidence TEXT,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_retry REAL DEFAULT 0,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v4_metadata_queue(paper_id TEXT PRIMARY KEY,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_retry REAL DEFAULT 0,evidence TEXT,updated TEXT);
CREATE INDEX IF NOT EXISTS corpus_v4_queue ON corpus_v4_job_policy(queue,purpose);
CREATE TABLE IF NOT EXISTS corpus_v5_frontier(job_id TEXT PRIMARY KEY,parked INTEGER DEFAULT 0,reason TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v5_dispatch(domain TEXT,topic_id TEXT,dispatches INTEGER DEFAULT 0,last_dispatch REAL DEFAULT 0,PRIMARY KEY(domain,topic_id));
'''
def init(db):
    from corpus_languages import SCHEMA as LANGUAGE_SCHEMA
    db.executescript(SCHEMA+LANGUAGE_SCHEMA);db.commit()
def setting(db,key,default=None):
    row=db.execute('SELECT value FROM corpus_v4_checkpoints WHERE key=?',(key,)).fetchone()
    return json.loads(row[0]) if row else default
def checkpoint(db,key,value):
    db.execute('INSERT INTO corpus_v4_checkpoints VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated',(key,dump(value),now()));db.commit()
def enabled(cfg):return [d for d in cfg['domains'] if d.get('enabled',True)]
def historical_slot(index,fraction):
    fraction=max(0,min(1,float(fraction)))
    return math.floor((index+1)*fraction)>math.floor(index*fraction)
def download_frontier(candidates,ready_ids,limit,ready_fraction=.7):
    """Reserve dispatchable PDFs while retaining space for new OA resolution."""
    def fair(pool):
        groups=collections.defaultdict(collections.deque)
        for pid,domains in pool:
            for domain in domains or ['unmapped']:groups[domain].append(pid)
        seen=set()
        while any(groups.values()):
            for group in groups.values():
                while group and group[0] in seen:group.popleft()
                if group:
                    pid=group.popleft();seen.add(pid);yield pid
    ready=list(fair([r for r in candidates if r[0] in ready_ids]));other=list(fair([r for r in candidates if r[0] not in ready_ids]));quota=min(len(ready),int(limit*ready_fraction))
    chosen=ready[:quota]+other[:max(0,limit-quota)];seen=set(chosen)
    chosen.extend(pid for pid in ready+other if pid not in seen)
    return set(chosen[:limit])
def article_role(p,n):
    source=(p.get('raw_source_evidence') or {})
    types=source.get('pubTypeList',{}).get('pubType',[]) if isinstance(source.get('pubTypeList'),dict) else []
    if n.get('document_type')=='review' or any('review' in t.lower() for t in types):return 'review',{'basis':'source_document_type','value':types or n.get('document_type')}
    match=re.search(r'\b(systematic review|scoping review|review|survey|perspective|roadmap|tutorial)\b',p.get('title',''),re.I)
    if re.search(r'综述|研究进展|进展与展望|系统性评价|文献回顾',p.get('title','')):return 'review',{'basis':'Chinese review-title heuristic','confidence':.75}
    if match:return match.group(1).lower().replace(' ','_'),{'basis':'title_heuristic','quote':match.group(0),'confidence':.75}
    return 'original_research',{'basis':'no_review_evidence','confidence':.6}
def signal(p,n):
    raw=p.get('raw_source_evidence') or {};raw=raw if isinstance(raw,dict) else {}
    citations=max([int(v) for v in [p.get('citation_count'),p.get('cited_by_count'),raw.get('cited_by_count'),raw.get('citationCount'),n.get('citation_count')] if str(v or '').isdigit()] or [0])
    references=raw.get('referenced_works') or raw.get('references') or raw.get('reference') or []
    academic=list(n.get('academic_classification') or [])
    for key in ['topics','concepts','fieldsOfStudy','s2FieldsOfStudy','subject']:
        for t in raw.get(key,[]) or []:
            academic.append(t.get('display_name') or t.get('category') or t.get('name') or '' if isinstance(t,dict) else str(t))
    return {'citations':citations,'citation_evidence':{'source':p.get('source'),'value':citations,'missing_is_not_zero_quality':True},'references':references[:200],'subjects':list(dict.fromkeys(academic))}
def tier(p,n,domains,topics,cfg):
    role,roleproof=article_role(p,n);s=signal(p,n)
    formal=n.get('publication_status')=='published' or n.get('formal_publication_status') in ['confirmed_online_or_published','published']
    kind=n.get('formal_publication_type');peer_review_proxy=formal and kind in ['journal','conference']
    venue=html.unescape(n.get('canonical_formal_venue') or n.get('formal_venue') or '')
    preferred=[v for d in enabled(cfg) if d['domain_name'] in domains for v in d.get('preferred_venues',[])]
    prestigious=any(v.casefold()==venue.casefold() or (len(v)>5 and v.casefold() in venue.casefold()) for v in preferred)
    policy=cfg.get('continuous',{}).get('tier',{});year=n.get('canonical_publication_year');age=max(1,datetime.datetime.now(datetime.timezone.utc).year-int(year or datetime.datetime.now().year)+1)
    influential=s['citations']>=policy.get('core_citations',100) and s['citations']/age>=policy.get('citations_per_year',15)
    method=bool(re.search(r'\b(method|framework|algorithm|benchmark|architecture|foundation)\b',p.get('title',''),re.I)) and any(topics.values())
    importance=max([float(t.get('importance',1)) for d in enabled(cfg) if d['domain_name'] in domains for t in d['topics'] if t['topic_id'] in topics.get(d['domain_name'],[])] or [1])
    features={'formal_publication_confirmed':formal,'journal_conference_proxy':peer_review_proxy,'preferred_venue':prestigious,'influence_threshold_met':influential,'method_title_and_topic_proxy':method,'topic_importance':importance,'review_role':role!='original_research'}
    # At least two independent signals, including influence or curated venue; no IF.
    support=sum([prestigious,influential,method,role!='original_research'])
    level='A Core' if peer_review_proxy and support>=2 and (influential or prestigious) else 'B Standard' if peer_review_proxy else 'C Supporting'
    return {'literature_tier':level,'article_role':role,'evidence':{'signals':features,**s,'role_evidence':roleproof,'formal_venue':venue,'publication_status':n.get('publication_status'),'thresholds':policy,'reason':'multi-signal assessment; venue and method are proxies, not proof of peer review or research quality'},'version':'tier-v4.1'}
def save_tier(db,pid,p,n,domains,topics,cfg):
    t=tier(p,n,domains,topics,cfg)
    proof=dump(t['evidence']);old=db.execute('SELECT literature_tier,article_role,evidence,version FROM corpus_v4_tiers WHERE paper_id=?',(pid,)).fetchone();value=(t['literature_tier'],t['article_role'],proof,t['version'])
    for item in [old,value] if old and old!=value else [value]:
        key=hashlib.sha256(dump([pid,*item]).encode()).hexdigest();db.execute('INSERT OR IGNORE INTO corpus_v4_tier_history VALUES(?,?,?,?,?,?,?)',(key,pid,*item,now()))
    if old==value:return t
    db.execute('INSERT INTO corpus_v4_tiers VALUES(?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET literature_tier=excluded.literature_tier,article_role=excluded.article_role,evidence=excluded.evidence,version=excluded.version,updated=excluded.updated',(pid,t['literature_tier'],t['article_role'],dump(t['evidence']),t['version'],now()))
    return t
def records(db,fulltext_only=False):
    where=''
    if fulltext_only:
        xml=xml_ids();where=' WHERE s.downloaded=1'+(' OR s.paper_id IN ('+','.join("'"+p.replace("'","''")+"'" for p in xml)+')' if xml else '')
    for pid,data,normalized,identity,accepted,downloaded,valid,relevant,review,excluded,domains,topics in db.execute('SELECT p.id,p.data,m.normalized,s.article_identity,s.accepted_for_corpus,s.downloaded,s.valid_pdf,s.relevant,s.needs_review,s.excluded,s.domains,s.topics FROM papers p JOIN corpus_v2_metadata m ON m.paper_id=p.id JOIN corpus_v2_status s ON s.paper_id=p.id'+where):
        yield pid,json.loads(data),json.loads(normalized),dict(identity=identity,accepted=accepted,downloaded=downloaded,valid=valid,relevant=relevant,review=review,excluded=excluded,domains=json.loads(domains),topics=json.loads(topics))
def xml_ids():return {p.stem for p in (STATE/'fulltext_xml').glob('*.xml') if p.with_suffix('.json').exists()}
def accepted_record(pid,n,s,xml,cfg):return bool(s['accepted'] or pid in xml and s['relevant'] and not s['review'] and not s['excluded'] and n.get('document_type') in cfg['acceptance']['allowed_types'] and not n.get('pdf_explicit_genre'))
def coverage_profile(db,cfg):
    cells=collections.defaultdict(collections.Counter);domain=collections.defaultdict(collections.Counter);years=collections.Counter();seen=set();xml=xml_ids()
    for pid,p,n,s in records(db,fulltext_only=True):
        if not accepted_record(pid,n,s,xml,cfg) or s['identity'] in seen:continue
        seen.add(s['identity']);year=n.get('canonical_publication_year') or 0;years[year]+=1;role,_=article_role(p,n);formal=bool(n.get('formal_venue') and n.get('publication_status')=='published');citations=signal(p,n)['citations'];venue=n.get('formal_venue') or ''
        for d in enabled(cfg):
            name=d['domain_name']
            if name not in s['domains']:continue
            domain[name]['accepted']+=1;domain[name]['unmapped']+=int(not s['topics'].get(name))
            for topic in s['topics'].get(name,[]):
                cell=cells[(name,topic,year)];cell['accepted']+=1;cell['formal']+=formal;cell['reviews']+=role!='original_research';cell['citations']+=citations;cell['preferred_venue']+=any(v.lower()==venue.lower() for v in d.get('preferred_venues',[]))
    return cells,domain,years
def source_health(db):
    # Recent outcomes affect dispatch, not relevance.
    health={}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='http_attempts'").fetchone():
        for name,total,success in db.execute("SELECT provider,count(*),sum(outcome IN ('success','http_success')) FROM http_attempts WHERE id>(SELECT coalesce(max(id),0)-10000 FROM http_attempts) GROUP BY provider"):
            health[name]=(success+2)/(total+4)
    for name,total,ok in db.execute("SELECT source,count(*),sum(status='done') FROM jobs WHERE updated>=? GROUP BY source",((datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=7)).isoformat(),)):
        health[name]=(health.get(name,.5)+(ok+2)/(total+4))/2
    return health
def priority(d,t,year,source,profile,health,purpose='original'):
    cells,domains,years=profile;cell=cells[(d['domain_name'],t['topic_id'],year)]
    total=sum(c['accepted'] for (dn,tn,y),c in cells.items() if dn==d['domain_name'] and tn==t['topic_id']);count=cell['accepted'];depth=max(1,float(t.get('target_depth',t.get('target_count',30))));gap=1/(1+total/depth);annual=1/(1+count/5)
    yearly=float(d.get('year_policy',{}).get('year_weights',{}).get(str(year),1))/(1+years[year]/200)
    ratio=lambda k:cell[k]/max(1,count)
    deficiency=(1-ratio('formal'))*.3+(1-ratio('preferred_venue'))*.15+max(0,.2-ratio('reviews'))*.5
    unmapped=domains[d['domain_name']]['unmapped']/max(1,domains[d['domain_name']]['accepted'])
    importance=float(d.get('importance',1))*float(d.get('priority',1))*float(t.get('priority',1))*float(t.get('importance',1));transfer=float(d.get('transfer_relevance',1))*float(t.get('transfer_relevance',1));emergence=float(t.get('emergence',d.get('emergence',1)))
    influence_gap=1/(1+cell['citations']/max(1,count)/30)
    score=(gap*4+annual*3+yearly+deficiency+unmapped*.4+influence_gap*.2)*importance*transfer*max(.35,health.get(source,.5))*(1+.1*emergence)
    if purpose=='review':score*=1.2 if ratio('reviews')<.2 else .5
    if purpose=='classic':score*=.65
    return score,{'accepted_topic_total':total,'accepted_topic_year':count,'topic_coverage_gap':gap,'year_coverage_gap':yearly,'unmapped_ratio':unmapped,'formal_ratio':ratio('formal'),'review_ratio':ratio('reviews'),'preferred_venue_ratio':ratio('preferred_venue'),'source_success_availability':health.get(source,.5),'influence_gap':influence_gap,'importance':importance,'emergence':emergence,'transfer_relevance':transfer,'depth_is_priority_weight_not_cap':True}
def seed_frontier(db,cfg):
    """One selected year/source/purpose per topic per refresh, bounded persisted frontier.

    Original eager queues are preserved but only managed rows are dispatched.
    Completed independent annual queries refresh weekly for newly indexed outputs.
    """
    init(db)
    from language_queue import seed
    language_added=seed(db,cfg)
    policy=cfg['continuous'];pending=db.execute("SELECT count(*) FROM jobs j JOIN corpus_v4_job_policy q ON q.job_id=j.id WHERE j.status IN ('pending','retry','running') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1)").fetchone()[0]
    room=min(policy['new_jobs_per_refresh'],max(0,policy['frontier_limit']-pending))
    if not room:return language_added
    resumed=0
    if policy.get('fair_dispatch'):
        names={d['domain_name'] for d in enabled(cfg)}
        parked=db.execute("SELECT j.id,j.domain,q.topic_id FROM jobs j JOIN corpus_v4_job_policy q ON q.job_id=j.id JOIN corpus_v5_frontier f ON f.job_id=j.id WHERE f.parked=1 AND j.status IN ('pending','retry') AND j.next_retry<=? ORDER BY j.page,q.score DESC",(time.time(),)).fetchall()
        for jid,dn,tn in parked:
            if dn not in names:continue
            busy=db.execute("SELECT 1 FROM jobs j JOIN corpus_v4_job_policy q ON q.job_id=j.id WHERE j.domain=? AND q.topic_id=? AND j.status IN ('pending','retry','running') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1) LIMIT 1",(dn,tn)).fetchone()
            if busy:continue
            db.execute('UPDATE corpus_v5_frontier SET parked=0,reason=?,updated=? WHERE job_id=?',('bounded historical cursor resume',now(),jid));resumed+=1
            if resumed>=max(1,int(room*.15)):break
        db.commit();room-=resumed
        if not room:return resumed+language_added
    profile=coverage_profile(db,cfg);health=source_health(db);cycle=setting(db,'planner_cycle',0);choices=[]
    for d in enabled(cfg):
        sources=d.get('preferred_sources') or d.get('search_sources') or cfg['sampling']['providers']
        sources=[s for s in sources if s in cfg['sampling']['providers']]
        if not sources:continue
        for i,t in enumerate(d['topics']):
            purpose='review' if (cycle+i)%10 in [3,7] else 'classic' if (cycle+i)%10==9 else 'original'
            if historical_slot(cycle+i+3,policy.get('historical_fraction',0)):purpose='historical'
            busy_sql="SELECT 1 FROM jobs j JOIN corpus_v4_job_policy q ON q.job_id=j.id WHERE j.domain=? AND q.topic_id=? AND j.status IN ('pending','retry','running') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1) AND (j.status!='retry' OR j.next_retry<=?)"
            if purpose=='historical':busy_sql+=" AND q.purpose='historical'"
            if policy.get('fair_dispatch') and db.execute(busy_sql+' LIMIT 1',(d['domain_name'],t['topic_id'],time.time())).fetchone():continue
            source=sources[(cycle+i)%len(sources)]
            if purpose=='classic' and 'openalex' in sources:source='openalex'
            possible=d['target_years'] if purpose!='classic' else policy.get('classic_years',list(range(1980,2019)))
            if purpose=='historical':possible=d.get('year_policy',{}).get('historical_years') or [y for y in d['target_years'] if y<2019]
            if not possible:continue
            # Evaluate annual gaps only; do not generate topic x year x source rows.
            queries=t.get('queries') or [t['name']]
            query=queries[(cycle//max(1,len(sources)))%len(queries)] if source in cfg['sampling'].get('multilingual_providers',[]) else queries[0]
            if purpose=='review':query+=' '+['review','survey','perspective','roadmap','tutorial'][(cycle+i)//10%5]
            from corpus_planner import job_id
            ordered_years=sorted(possible,key=lambda y:-priority(d,t,y,source,profile,health,purpose)[0]) if purpose!='classic' else possible[(cycle+i)%len(possible):]+possible[:(cycle+i)%len(possible)]
            if purpose=='historical':ordered_years=possible[(cycle+i)%len(possible):]+possible[:(cycle+i)%len(possible)]
            year=None
            for candidate_year in ordered_years:
                root_id=job_id(d['domain_name'],query,source,candidate_year)
                old=db.execute('SELECT status,updated FROM jobs WHERE id=?',(root_id,)).fetchone()
                managed=db.execute('SELECT 1 FROM corpus_v4_job_policy WHERE job_id=? AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=? AND f.parked=1)',(root_id,root_id)).fetchone()
                if managed and old and old[0] in ['pending','retry','running']:continue
                if old and old[0]=='done' and old[1] and old[1]>(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=7)).isoformat():continue
                year=candidate_year;break
            if year is None:continue
            score,evidence=priority(d,t,year,source,profile,health,purpose)
            queue='exploration' if purpose=='classic' or year>=2026 or (cycle+i)%20==18 else 'high' if evidence['accepted_topic_year']<5 else 'medium'
            choices.append((d,t,year,source,query,score,evidence,queue,purpose))
    # Round robin within queues ensures a new family cannot monopolize all resources.
    ordered=[];queue_pools={}
    for queue in ['high','medium','exploration']:
        groups={d['domain_name']:sorted([c for c in choices if c[0]['domain_name']==d['domain_name'] and c[7]==queue],key=lambda c:(int(c[8]!='classic') if queue=='exploration' and cycle%2==0 else 0,-c[5])) for d in enabled(cfg)}
        while any(groups.values()):
            for group in groups.values():
                if group:ordered.append(group.pop(0))
        queue_pools[queue]=[c for c in ordered if c[7]==queue]
    ordered=[]
    # Reserve each queue's share at generation as well as dispatch.
    for queue in ['high','medium','exploration']:
        quota=max(1,round(room*policy['queue_weights'][queue]));ordered.extend(queue_pools[queue][:quota])
    selected={(c[0]['domain_name'],c[1]['topic_id'],c[7]) for c in ordered}
    ordered.extend(c for pool in queue_pools.values() for c in pool if (c[0]['domain_name'],c[1]['topic_id'],c[7]) not in selected)
    added=0
    from corpus_planner import job_id
    for d,t,y,s,query,score,evidence,queue,purpose in ordered:
        jid=job_id(d['domain_name'],query,s,y);old=db.execute('SELECT status,updated FROM jobs WHERE id=?',(jid,)).fetchone()
        if old and old[0]=='done':
            # Refresh completed search root without deleting cursor history.
            if old[1] and old[1]>(datetime.datetime.now(datetime.timezone.utc)-datetime.timedelta(days=7)).isoformat():continue
            jid=hashlib.sha256(dump([jid,'weekly_refresh',datetime.datetime.now(datetime.timezone.utc).strftime('%G-%V')]).encode()).hexdigest()[:24]
            if db.execute('SELECT 1 FROM jobs WHERE id=?',(jid,)).fetchone():continue
        if db.execute('SELECT 1 FROM corpus_v4_job_policy WHERE job_id=?',(jid,)).fetchone():
            parked=db.execute('SELECT parked FROM corpus_v5_frontier WHERE job_id=?',(jid,)).fetchone()
            if parked and parked[0]:
                db.execute('UPDATE corpus_v5_frontier SET parked=0,reason=?,updated=? WHERE job_id=?',('resumed by bounded planner',now(),jid));added+=1
                if added>=room:break
            continue
        db.execute("INSERT OR IGNORE INTO jobs(id,domain,topic,source,lo,hi,sort,cursor,page,priority,status,created,updated) VALUES(?,?,?,?,?,?,'relevance',NULL,0,?,'pending',?,?)",(jid,d['domain_name'],query,s,y,y,-score,now(),now()))
        db.execute('INSERT OR IGNORE INTO corpus_v2_jobs VALUES(?,?,?,?,?,?,?)',(jid,d['domain_name'],t['topic_id'],y,s,1,now()))
        db.execute('INSERT INTO corpus_v4_job_policy VALUES(?,?,?,?,?,?,?)',(jid,t['topic_id'],queue,purpose,score,dump(evidence),now()));added+=1
        if added>=room:break
    checkpoint(db,'planner_cycle',cycle+1);db.commit();return added+resumed+language_added
def jobs(db,cfg):
    names={d['domain_name'] for d in enabled(cfg)}
    cols=['id','domain','topic','source','lo','hi','sort','cursor','page','priority','attempts','topic_id','queue','purpose','coverage_score','priority_evidence']
    langs=dict(db.execute('SELECT job_id,language FROM corpus_v6_job_language'))
    return [dict(dict(zip(cols,row)),language=langs.get(row[0])) for row in db.execute("SELECT j.id,j.domain,j.topic,j.source,j.lo,j.hi,j.sort,j.cursor,j.page,j.priority,j.attempts,q.topic_id,q.queue,q.purpose,q.score,q.evidence FROM jobs j JOIN corpus_v4_job_policy q ON q.job_id=j.id WHERE j.status IN ('pending','retry') AND j.next_retry<=? AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1) ORDER BY q.score DESC,j.page LIMIT 1000",(time.time(),)) if row[1] in names]

def fair_job(db,options,score,families=None):
    """Persist breadth across restarts; score breaks ties within a topic."""
    history={(d,t):(count,last) for d,t,count,last in db.execute('SELECT domain,topic_id,dispatches,last_dispatch FROM corpus_v5_dispatch')}
    domain_last={}
    for (d,t),(count,last) in history.items():domain_last[d]=max(domain_last.get(d,0),last)
    family_last={}
    if families:
        for d,last in domain_last.items():
            f=families.get(d,d);family_last[f]=max(family_last.get(f,0),last)
    return min(options,key=lambda j:(int(domain_last.get(j['domain'],0)>0),(family_last.get(families.get(j['domain'],j['domain']),0) if families else 0),domain_last.get(j['domain'],0),history.get((j['domain'],j['topic_id']),(0,0))[1],j['page'],score(j)))

def dispatched(db,job):
    if job.get('language')=='zh':checkpoint(db,'zh_search_dispatches',setting(db,'zh_search_dispatches',0)+1)
    checkpoint(db,'language_dispatch_turn',setting(db,'language_dispatch_turn',0)+1)
    db.execute('INSERT INTO corpus_v5_dispatch VALUES(?,?,1,?) ON CONFLICT(domain,topic_id) DO UPDATE SET dispatches=dispatches+1,last_dispatch=excluded.last_dispatch',(job['domain'],job['topic_id'],time.time()));db.commit()
def record_child(db,parent,child,cfg=None):
    language=db.execute('SELECT language,query,evidence FROM corpus_v6_job_language WHERE job_id=?',(parent,)).fetchone()
    if language:db.execute('INSERT OR IGNORE INTO corpus_v6_job_language VALUES(?,?,?,?,?)',(child,*language,now()))
    meta=db.execute('SELECT topic_id,queue,purpose,score,evidence FROM corpus_v4_job_policy WHERE job_id=?',(parent,)).fetchone()
    if meta:
        db.execute('INSERT OR IGNORE INTO corpus_v4_job_policy VALUES(?,?,?,?,?,?,?)',(child,meta[0],meta[1],meta[2],meta[3]*.85,meta[4],now()))
        if cfg:
            pending=db.execute("SELECT count(*) FROM jobs j JOIN corpus_v4_job_policy q ON q.job_id=j.id WHERE j.status IN ('pending','retry','running') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1)").fetchone()[0]
            if pending>cfg['continuous']['frontier_limit']:db.execute('INSERT OR REPLACE INTO corpus_v5_frontier VALUES(?,1,?,?)',(child,'bounded pagination overflow; cursor retained',now()))
        db.commit()
def budget_ready(db,cfg,source):
    day=datetime.datetime.now(datetime.timezone.utc).date().isoformat();row=db.execute('SELECT requests FROM corpus_v4_budget WHERE day=? AND provider=?',(day,source)).fetchone()
    return not row or row[0]<cfg['continuous']['api_requests_per_day'].get(source,1000)
class ApiBudgetDeferred(RuntimeError):
    def __init__(self,source):
        super().__init__('Daily public API budget reached: '+source)
        self.budget_reset=(datetime.datetime.now(datetime.timezone.utc).replace(hour=0,minute=0,second=0,microsecond=0)+datetime.timedelta(days=1)).timestamp()
def spend(db,source):
    db.execute('INSERT INTO corpus_v4_budget VALUES(?,?,1) ON CONFLICT(day,provider) DO UPDATE SET requests=requests+1',(datetime.datetime.now(datetime.timezone.utc).date().isoformat(),source));db.commit()

def discovery(db,cfg,persist=True):
    """Conservative source-enriched clustering and independently evidenced proposals."""
    from corpus_repair import text_features,STOPWORDS
    raw=collections.defaultdict(list);domain_matches=collections.defaultdict(list);subjects=collections.defaultdict(list);references=collections.Counter();ref_evidence=collections.defaultdict(list)
    seeds=cfg.get('domain_discovery_seeds',[]);compiled={d['domain_id']:re.compile('|'.join(d['positive_terms']),re.I) for d in seeds}
    for pid,p,n,s in records(db):
        sig=signal(p,n);text=' '.join([p.get('title') or '',n.get('abstract') or '',*(n.get('keywords') or []),*sig['subjects']]);references.update(str(r.get('DOI') or r.get('paperId') or '') if isinstance(r,dict) else str(r) for r in sig['references'])
        for ref in sig['references']:
            if isinstance(ref,str) and re.fullmatch(r'(?:https://openalex.org/)?W\d+',ref) and (s['domains'] or p.get('domains')):
                ref_evidence[ref.rsplit('/',1)[-1]].append({'citing_paper':pid,'domain':(s['domains'] or p['domains'])[0],'role':article_role(p,n)[0]})
        for seed in seeds:
            match=compiled[seed['domain_id']].search(text)
            if match:domain_matches[seed['domain_id']].append({'paper_id':pid,'title':p.get('title'),'matched_evidence':match.group(0),'source':p.get('source'),'subjects':sig['subjects'][:8],'current_domains':s['domains']})
        for subject in sig['subjects']:
            if subject and len(subject)<120:subjects[subject].append({'paper_id':pid,'title':p.get('title'),'source':p.get('source'),'current_domains':s['domains']})
        if not s['downloaded'] or not s['relevant'] or s['excluded']:continue
        for domain in s['domains']:
            if s['topics'].get(domain):continue
            n=dict(n);n['abstract']=' '.join([n.get('abstract') or '',*(n.get('keywords') or []),*sig['subjects'],'venue: '+(n.get('formal_venue') or '')]);features=text_features(p,n);raw[domain].append((pid,p,features,sig))
            # These signals remain represented even when the abstract exceeds the text cap.
            features.update({'keyword:'+str(k).casefold():2 for k in n.get('keywords') or []})
            features.update({'subject:'+str(k).casefold():2 for k in sig['subjects']})
            if n.get('formal_venue'):features['venue:'+n['formal_venue'].casefold()]+=1
    proposals=[];candidates=[]
    for domain,pool in raw.items():
        df=collections.Counter(k for pid,p,f,sig in pool for k in f);clusters=[]
        for pid,p,f,sig in sorted(pool,key=lambda r:r[0]):
            vec={t:(1+math.log(c))*math.log((1+len(pool))/(1+df[t])) for t,c in f.items() if df[t]>=2 and t not in ['model','analysis','network','material','cell','language','chip','screening']};norm=math.sqrt(sum(v*v for v in vec.values())) or 1;vec={t:v/norm for t,v in vec.items()}
            best=max(clusters,key=lambda c:sum(v*c['vector'].get(t,0) for t,v in vec.items()),default=None)
            similarity=sum(v*best['vector'].get(t,0) for t,v in vec.items()) if best else 0
            if not best or similarity<cfg['topic_discovery'].get('cosine_threshold',.18):clusters.append({'vector':vec,'members':[(pid,p,f,sig)],'similarities':[]})
            else:
                best['members'].append((pid,p,f,sig));best['similarities'].append(similarity)
                acc=collections.Counter(best['vector']);acc.update(vec);norm=math.sqrt(sum(v*v for v in acc.values())) or 1;best['vector']={t:v/norm for t,v in acc.items()}
        definition=next((d for d in cfg['domains'] if d['domain_name']==domain),None)
        for cluster in sorted(clusters,key=lambda c:-len(c['members']))[:25]:
            members=cluster['members'];count=collections.Counter();[count.update(f) for pid,p,f,sig in members];terms=[t for t,v in count.most_common(30) if df[t]>=2 and t not in STOPWORDS][:8];label=' / '.join(terms[:4]) or 'insufficient shared evidence'
            overlap=[t['topic_id'] for t in (definition or {}).get('topics',[]) if any(re.search(pattern,' '.join(terms),re.I) for pattern in t['positive_terms'])]
            confidence=min(.84,.4+math.log2(1+len(members))*.07+(sum(cluster['similarities'])/max(1,len(cluster['similarities'])))*.2)
            # Lexical clusters alone cannot be "high confidence" ontology definitions.
            proposal=dict(proposed_domain=domain,proposed_topic=label,aliases=terms,representative_papers=[{'id':pid,'title':p.get('title')} for pid,p,f,sig in members[:8]],paper_count=len(members),confidence=round(confidence,3),rationale={'method':'title-weighted TF-IDF/cosine; abstract+keywords+source subjects+venue','source_subjects':list(dict.fromkeys(t for pid,p,f,sig in members for t in sig['subjects']))[:12],'semantic_model_used':False,'limitation':'lexical clusters require domain/semantic review'},overlap_with_existing_topic=overlap,recommendation='review_synonym_or_boundary' if overlap else 'review_new_topic',auto_added_to_formal_taxonomy=False)
            proposals.append(proposal)
    domain_proposals=[];candidate_domains=[]
    for seed in seeds:
        matches=domain_matches[seed['domain_id']];sources={m['source'] for m in matches};overlap=collections.Counter(d for m in matches for d in m['current_domains']);confidence=min(.95,.45+math.log10(1+len(matches))*.18+min(2,len(sources))*.07)
        row=dict(proposed_domain=seed['domain_name_cn'],parent_domain=seed['parent_domain'],evidence={'query_patterns':seed['positive_terms'],'observed_sources':sorted(sources),'matched_examples':matches[:5],'confidence':round(confidence,3),'basis':'existing candidate corpus; seed list is hypothesis','citation_graph_repeated_reference_count':sum(v>=3 for k,v in references.items() if k)},representative_topics=list(dict.fromkeys(t for m in matches for t in m['subjects']))[:12],representative_papers=matches[:8],estimated_literature_volume=len(matches),overlap=dict(overlap),importance_for_technology_transfer=seed['importance_for_technology_transfer'],recommendation='candidate_configuration_review' if confidence>=.85 and len(matches)>=10 else 'needs_evidence_or_review',volume_scope='observed local candidates; not global estimate')
        domain_proposals.append(row)
        if row['recommendation']=='candidate_configuration_review':candidate_domains.append(dict(seed,confidence=confidence,evidence=row['evidence'],enabled=False,review_status='unreviewed'))
    known={d['domain_name_en'].casefold() for d in cfg['domains']}|{d['domain_name_cn'].casefold() for d in cfg['domains']}
    # Source labels beyond the seed list remain low confidence until ontology review.
    for subject,matches in sorted(subjects.items(),key=lambda x:-len(x[1]))[:50]:
        if len(matches)<10 or subject.casefold() in known:continue
        domain_proposals.append(dict(proposed_domain=subject,parent_domain='unassigned_source_subject',evidence={'basis':'provider subject/topic frequency','sources':sorted({m['source'] for m in matches}),'confidence':.55},representative_topics=[subject],representative_papers=matches[:6],estimated_literature_volume=len(matches),overlap=dict(collections.Counter(d for m in matches for d in m['current_domains'])),importance_for_technology_transfer='requires review',recommendation='review_topic_vs_domain',volume_scope='observed local source labels'))
    csvwrite(ROOT/'TAXONOMY_EXPANSION_PROPOSAL.csv',proposals,['proposed_domain','proposed_topic','aliases','representative_papers','paper_count','confidence','rationale','overlap_with_existing_topic','recommendation','auto_added_to_formal_taxonomy'])
    csvwrite(ROOT/'DOMAIN_EXPANSION_PROPOSAL.csv',domain_proposals,['proposed_domain','parent_domain','evidence','representative_topics','representative_papers','estimated_literature_volume','overlap','importance_for_technology_transfer','recommendation','volume_scope'])
    atomic_json(STATE/'candidate_domains.json',{'candidate_domains':candidate_domains,'candidate_topics':candidates,'proposals_only':True,'at':now(),'semantic_model_used':False})
    history=STATE/'discovery_history'/datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');history.mkdir(parents=True,exist_ok=True)
    for path in [ROOT/'TAXONOMY_EXPANSION_PROPOSAL.csv',ROOT/'DOMAIN_EXPANSION_PROPOSAL.csv',STATE/'candidate_domains.json']:shutil.copy2(path,history/path.name)
    reference_rows=[]
    for ref,proof in sorted(ref_evidence.items(),key=lambda x:-len(x[1]))[:200]:
        if len(proof)<3:continue
        reference_rows.append((ref,proof[0]['domain'],dump({'repeated_reference_count':len(proof),'citing_examples':proof[:10],'not_yet_verified_relevance':True}),now()))
    payload={'topic_proposals':len(proposals),'domain_proposals':len(domain_proposals),'candidate_domains':len(candidate_domains),'proposal_rows':[(kind,row) for kind,items in [('topic',proposals),('domain',domain_proposals)] for row in items],'reference_rows':reference_rows}
    if persist:persist_discovery(db,payload)
    return payload
def persist_discovery(db,payload):
    db.executemany('INSERT OR IGNORE INTO corpus_v4_references(work_id,domain,evidence,updated) VALUES(?,?,?,?)',payload['reference_rows'])
    for kind,row in payload['proposal_rows']:
        key=hashlib.sha256(dump([kind,row.get('proposed_domain'),row.get('proposed_topic')]).encode()).hexdigest()
        db.execute('INSERT INTO corpus_v4_proposals VALUES(?,?,?,?,?) ON CONFLICT(proposal_id) DO UPDATE SET definition=excluded.definition,updated=excluded.updated',(key,kind,dump(row),'unreviewed',now()))
    checkpoint(db,'last_discovery',time.time());db.commit()

def disk_usage(db):
    pdf_count,pdf_bytes=db.execute('SELECT count(distinct sha256),coalesce(sum(bytes),0) FROM files').fetchone();usage=shutil.disk_usage(ROOT)
    # Scan once per checkpoint, excluding optional environment and backup/export copies from corpus bytes.
    corpus_bytes=0;seen_files=set()
    for folder,dirs,files in os.walk(ROOT):
        dirs[:]=[d for d in dirs if d not in ['.venv','.git','vendor','backups','sync_exports','continuous_v4']]
        for name in files:
            try:
                info=(__import__('pathlib').Path(folder)/name).stat();identity=(info.st_dev,info.st_ino)
                if identity not in seen_files:corpus_bytes+=info.st_size;seen_files.add(identity)
            except FileNotFoundError:pass
    return dict(pdf_count=pdf_count,pdf_bytes=pdf_bytes,average_pdf_bytes=pdf_bytes/max(1,pdf_count),project_corpus_bytes=corpus_bytes,volume_used_bytes=usage.used,free_bytes=usage.free,volume_total_bytes=usage.total,excluded_from_project_bytes=['.venv','backups','sync_exports','vendor','continuous_v4'])
def live(db,cfg,metrics=None,disk=None):
    xml=xml_ids();works=set();available=set();coverage=collections.Counter();domains=collections.Counter();domain_year=collections.Counter();unmapped=collections.Counter();primary=collections.defaultdict(collections.Counter);years=collections.Counter();venues=collections.Counter();types=collections.Counter();roles=collections.Counter();tiers=collections.Counter();complete=collections.Counter();date_conflicts=0;pdf_valid=0;seen=set()
    fields=['doi','abstract','formal_venue','issn','publisher','first_publication_date']
    for pid,p,n,s in records(db,fulltext_only=True):
        if s['downloaded'] or pid in xml:available.add(s['identity'])
        if s['downloaded']:
            pdf_valid+=bool(s['valid']);date_conflicts+=bool(n.get('date_conflicts'))
            for f in fields:complete[f]+=bool(n.get(f))
        if not accepted_record(pid,n,s,xml,cfg):continue
        works.add(s['identity'])
        if s['identity'] in seen:continue
        seen.add(s['identity']);role,_=article_role(p,n);t=tier(p,n,s['domains'],s['topics'],cfg);save_tier(db,pid,p,n,s['domains'],s['topics'],cfg);tiers[t['literature_tier']]+=1;roles[role]+=1;year=n.get('canonical_publication_year') or 'unknown';years[str(year)]+=1;kind=n.get('document_type') or 'unknown';types[kind]+=1;venues[n.get('formal_venue') or 'unknown/preprint']+=1
        for d in s['domains']:
            domains[d]+=1;domain_year[(d,str(year))]+=1;matched=s['topics'].get(d) or [];unmapped[d]+=not matched
            if matched:primary[d][matched[0]]+=1
            for topic in matched or ['unmapped']:coverage[(d,topic,year,kind,role)]+=1
    db.commit();states=dict(db.execute('SELECT primary_state,count(*) FROM corpus_v2_status GROUP BY primary_state'));pdf_count=db.execute('SELECT count(distinct sha256) FROM files').fetchone()[0]
    try:old=json.loads((STATE/'stats.json').read_text(encoding='utf8'))
    except (OSError,ValueError):old={}
    disk=disk or disk_usage(db);disk.update(corpus_count=len(works),metadata_only=states.get('metadata_only',0));metrics=metrics or {}
    topicmetrics={}
    for d in enabled(cfg):
        name=d['domain_name'];classified=sum(primary[name].values());hhi=sum((v/max(1,classified))**2 for v in primary[name].values());topicmetrics[name]={'accepted':domains[name],'unmapped':unmapped[name],'unmapped_ratio':unmapped[name]/max(1,domains[name]),'HHI':hhi,'effective_topic_count':1/hhi if hhi else 0,'basis':'first matched topic per distinct accepted work; classified denominator','year_counts':{y:domain_year[(name,y)] for y in ['2023','2024']}}
    stats=dict(old,updated=now(),total_candidates=db.execute('SELECT count(*) FROM papers').fetchone()[0],total_downloaded_pdf=pdf_count,total_research_outputs=db.execute('SELECT count(distinct article_identity) FROM corpus_v2_status').fetchone()[0],available_fulltext_research_outputs=len(available),accepted_for_corpus=len(works),accepted_pdf=db.execute('SELECT sum(accepted_for_corpus) FROM corpus_v2_status').fetchone()[0],metadata_only=states.get('metadata_only',0),needs_review=states.get('needs_review',0),excluded=states.get('excluded',0),domain_count=len(enabled(cfg)),topic_count=sum(len(d['topics']) for d in enabled(cfg)),unmapped_ratio=sum(unmapped.values())/max(1,sum(domains.values())),unmapped_ratio_basis='domain assignments of distinct accepted fulltext outputs; multilabel',disk_usage=disk,download_rate=metrics.get('pdf_per_minute',0),download_rate_unit='PDF/minute this run',domain_metrics=topicmetrics,primary_states=states,accepted_years=dict(years),accepted_publication_types=dict(types),accepted_article_roles=dict(roles),literature_tiers=dict(tiers),top_venues=venues.most_common(25),metadata_completeness_downloaded={f:complete[f]/max(1,pdf_count) for f in fields},date_conflicts_downloaded=date_conflicts,pdf_validation={'valid_records':pdf_valid,'downloaded_pdf':pdf_count},version_relations=db.execute('SELECT count(*) FROM corpus_v2_versions').fetchone()[0],continuous_mode=True,corpus_total_cap=None)
    stats['metadata_needs_review']=db.execute('SELECT count(*) FROM corpus_v2_status WHERE metadata_only=1 AND needs_review=1').fetchone()[0];stats['metadata_excluded']=db.execute('SELECT count(*) FROM corpus_v2_status WHERE metadata_only=1 AND excluded=1').fetchone()[0]
    if cfg.get('language_policy',{}).get('enabled'):
        from corpus_languages import summary
        stats['languages']=summary(db)
    if cfg.get('storage_stage',{}).get('enabled'):
        from corpus_capacity import stage,inventory
        stats['storage_stage']=stage(db,cfg);inventory(db,cfg)
    day=datetime.datetime.now(datetime.timezone.utc).date().isoformat();stats['api_requests_today_utc']=dict(db.execute('SELECT provider,requests FROM corpus_v4_budget WHERE day=?',(day,)))
    atomic_json(STATE/'stats.json',stats);atomic_json(ROOT/'stats.json',stats)
    csvwrite(ROOT/'LIVE_COVERAGE.csv',[dict(domain=d,topic=t,year=y,publication_type=k,article_role=r,count=c) for (d,t,y,k,r),c in sorted(coverage.items(),key=lambda x:tuple(map(str,x[0])))],['domain','topic','year','publication_type','article_role','count'])
    if cfg.get('scope_mapping_file'):scope_coverage(db,cfg)
    return stats

def scope_coverage(db,cfg,write=True):
    import csv
    with (ROOT/cfg['scope_mapping_file']).open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
    full=collections.defaultdict(set);metadata=collections.defaultdict(set);review=collections.defaultdict(set);xml=xml_ids()
    for pid,p,n,s in records(db):
        for d,topics in s['topics'].items():
            for t in topics:
                if accepted_record(pid,n,s,xml,cfg):full[(d,t)].add(s['identity'])
                elif not s['downloaded'] and not s['excluded']:
                    metadata[(d,t)].add(s['identity'])
                    if s['review']:review[(d,t)].add(s['identity'])
    dispatch={(d,t):n for d,t,n in db.execute('SELECT domain,topic_id,dispatches FROM corpus_v5_dispatch')}
    active=collections.Counter((j['domain'],j['topic_id']) for j in jobs(db,cfg));success=collections.Counter()
    for payload, in db.execute("SELECT payload FROM events WHERE kind='search_ok' AND time>=?",(cfg.get('approval_expansion_at',''),)):
        j=json.loads(payload)
        if j.get('topic_id'):success[(j['domain'],j['topic_id'])]+=1
    output=[]
    for r in rows:
        keys=[(r['domain_name'],t) for t in json.loads(r['mapped_topic'])];a=set().union(*(full[k] for k in keys));m=set().union(*(metadata[k] for k in keys))-a;rev=set().union(*(review[k] for k in keys))-a
        output.append(dict(original_name=r['original_name'],canonical_domain_id=r['canonical_domain_id'],parent_family=r['parent_family'],enabled=True,configured_topics=len(keys),active_search_jobs=sum(active[k] for k in keys),search_dispatches=sum(dispatch.get(k,0) for k in keys),successful_searches=sum(success[k] for k in keys),valid_fulltext_unique=len(a),metadata_only_unique=len(m),metadata_needs_review_unique=len(rev),coverage_state='valid_fulltext' if a else 'metadata_only' if m else 'search_dispatched' if any(dispatch.get(k,0) for k in keys) else 'configured_and_planned'))
    if write:csvwrite(ROOT/'APPROVED_SCOPE_COVERAGE.csv',output)
    return output
def plan(db,cfg):
    profile=coverage_profile(db,cfg);health=source_health(db);out=[];lookup={d['domain_name']:d for d in enabled(cfg)}
    for j in jobs(db,cfg):
        d=lookup[j['domain']];t=next(t for t in d['topics'] if t['topic_id']==j['topic_id']);score,evidence=priority(d,t,j['lo'],j['source'],profile,health,j['purpose']);out.append(dict(j,coverage_score=score,priority_evidence=evidence,query=j['topic'],year=j['lo'],unlimited=not cfg.get('storage_stage',{}).get('enabled'),storage_stage_target_bytes=cfg.get('storage_stage',{}).get('target_fulltext_bytes')))
    out.sort(key=lambda j:-j['coverage_score']);csvwrite(ROOT/'NEXT_ACQUISITION_PLAN.csv',out,['id','domain','topic_id','query','year','source','queue','purpose','coverage_score','priority_evidence','page','cursor','unlimited','storage_stage_target_bytes']);return out
def audit(db,cfg,stats=None):
    stats=stats or live(db,cfg);planned=plan(db,cfg);stamp=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ');path=ROOT/('CORPUS_AUDIT_'+stamp+'.md')
    text=['# 长期文献库自动审计','',f'UTC: {now()}；只新增治理证据，不重写原始来源或历史分类。','',f"候选 {stats['total_candidates']}；PDF {stats['total_downloaded_pdf']}；独立成果（含 metadata）{stats['total_research_outputs']}；accepted 全文 {stats['accepted_for_corpus']}（PDF {stats['accepted_pdf']}）；metadata-only {stats['metadata_only']}；needs_review {stats['needs_review']}；excluded {stats['excluded']}。",'',f"启用领域 {stats['domain_count']}；topic {stats['topic_count']}；版本关系 {stats['version_relations']}；互斥状态 {dump(stats['primary_states'])}",'','## 覆盖与集中度','', '|领域|accepted|unmapped比例|HHI|有效topic数|','|---|---:|---:|---:|---:|']
    for d,v in stats['domain_metrics'].items():text.append(f"|{d}|{v['accepted']}|{v['unmapped_ratio']:.1%}|{v['HHI']:.4f}|{v['effective_topic_count']:.2f}|")
    text+=['','HHI以首个已命中topic计算，分母为已分类独立work；多标签覆盖另见 LIVE_COVERAGE.csv。未知领域/主题保持待审核，不通过降阈值补齐。','', '## 年份与来源','', 'accepted 年份：'+dump(stats['accepted_years']),'异常年份：'+dump({y:c for y,c in stats['accepted_years'].items() if y=='unknown' or y.isdigit() and int(y)>datetime.datetime.now().year}),'出版类型：'+dump(stats['accepted_publication_types']),'综述/原始研究角色：'+dump(stats['accepted_article_roles']),'主要正式venue：'+dump(stats['top_venues']),'','## 质量与磁盘','','元数据完整率（下载记录）：'+dump(stats['metadata_completeness_downloaded']),'日期冲突：'+str(stats['date_conflicts_downloaded']),'PDF验证：'+dump(stats['pdf_validation']),'文献Tier：'+dump(stats['literature_tiers']),'磁盘：'+dump(stats['disk_usage']),'','## 下一步','','优先补2023/2024、缺失topic、正式venue/综述缺口；保留经典及新兴探索资源。NEXT_ACQUISITION_PLAN.csv 保存逐任务评分证据；深度是优先级尺度，无总数硬上限。','最高优先任务：'+dump([{k:j[k] for k in ['domain','topic_id','year','queue','purpose','coverage_score']} for j in planned[:12]]),'','自动发现提案：DOMAIN_EXPANSION_PROPOSAL.csv / TAXONOMY_EXPANSION_PROPOSAL.csv；语义模型未就绪时不虚构判断。']
    path.write_text('\n'.join(text)+'\n',encoding='utf8');atomic_json(STATE/'LATEST_AUDIT.json',{'path':str(path.relative_to(ROOT)),'accepted':stats['accepted_for_corpus'],'at':now()});checkpoint(db,'last_audit',{'time':time.time(),'accepted':stats['accepted_for_corpus']});return path
def prepare(db,cfg):
    init(db);seed_frontier(db,cfg);payload=discovery(db,cfg);stats=live(db,cfg);path=audit(db,cfg,stats);return dict(proposals={k:payload[k] for k in ['topic_proposals','domain_proposals','candidate_domains']},stats={k:stats[k] for k in ['total_candidates','total_downloaded_pdf','accepted_for_corpus','domain_count','topic_count']},audit=str(path))
if __name__=='__main__':
    if any((STATE/p).exists() for p in ['collector.lock','repair_v3/repair.lock','repair_v2/repair.lock']):raise SystemExit('Writer active; prepare must run exclusively.')
    with sqlite3.connect(STATE/'state.sqlite3') as db:print(dump(prepare(db,load_config())))
