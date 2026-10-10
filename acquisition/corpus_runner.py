"""Reviewed annual collection mode, reusing the existing network engine and SQLite queues."""
import asyncio,hashlib,json,time
from collections import Counter
from collect import ROOT,STATE,DOMAINS,paper_keys,publisher_urls
from download_engine import OptimizedRunner,API_HOSTS
from corpus_store import load_config,version,dump,now,put_relevance,put_metadata,atomic_json,migrate
from corpus_metadata import Normalizer
from corpus_rules import Classifier,LocalSemantic,apply_semantic
from corpus_planner import annual_jobs,coverage_counts,score_job
import corpus_continuous as continuous
SEARCH_HOSTS=dict(API_HOSTS,crossref='api.crossref.org',doaj='doaj.org')
class CorpusRunner(OptimizedRunner):
    def __init__(self,lib,args):
        self.cfg=load_config();self.classifier=Classifier(self.cfg);self.normalizer=Normalizer(lib.db,'balanced_runtime');self.last_gain=time.monotonic();self.last_search=0
        self.local_semantic=LocalSemantic(self.cfg) if self.cfg['semantic']['enabled'] else None
        self.semantic_available=False if not self.cfg['semantic']['enabled'] else None
        from corpus_repair import load_pdf_cache
        self.file_checks=load_pdf_cache()
        migrate(lib.db)
        from network_recovery import NetworkRecovery
        self.network_recovery=NetworkRecovery(lib.db,self.cfg.get('network_recovery',{}))
        self.continuous=self.cfg.get('continuous',{}).get('enabled',False)
        if self.continuous:
            continuous.init(lib.db);self.pdf_paused=False;self.disk_info=None;self.last_live=0;self.last_plan=0;self.dispatch=0;self.metadata_ready=0
            self.frontier_limit=self.cfg['continuous']['download_frontier_limit'];self.profile=None
            lib.api_budget=self.api_budget
        if not lib.db.execute('SELECT 1 FROM corpus_v2_status LIMIT 1').fetchone():raise RuntimeError('Run run_reclassify.ps1 before enabling collection.')
        super().__init__(lib,args)
        if self.continuous:self.exported_at=time.monotonic()
        self.normalizer.run_id=self.run_id
    def corpus_download_filter(self):
        query=" AND EXISTS(SELECT 1 FROM corpus_v2_status cs WHERE cs.paper_id=p.id AND cs.relevant=1 AND cs.excluded=0 AND cs.needs_review=0 AND cs.valid_pdf=0) AND NOT EXISTS(SELECT 1 FROM corpus_v2_status old JOIN corpus_v2_status me ON me.article_identity=old.article_identity WHERE me.paper_id=p.id AND old.downloaded=1 AND old.valid_pdf=1)"
        if hasattr(self,'scope_ids'):
            query+=' AND p.id IN ('+','.join("'"+pid.replace("'","''")+"'" for pid in sorted(self.scope_ids))+')' if self.scope_ids else ' AND 0'
            query+=" AND NOT EXISTS(SELECT 1 FROM corpus_v2_status other JOIN corpus_v2_status me ON me.article_identity=other.article_identity JOIN papers inflight ON inflight.id=other.paper_id WHERE me.paper_id=p.id AND inflight.status='downloading')"
        elif hasattr(self,'cfg'):
            query+=" AND NOT EXISTS(SELECT 1 FROM corpus_v2_status other JOIN corpus_v2_status me ON me.article_identity=other.article_identity JOIN papers inflight ON inflight.id=other.paper_id WHERE me.paper_id=p.id AND inflight.status='downloading')"
            targets={d['domain_name']:{'years':d['target_years'],'topics':[t['topic_id'] for t in d['topics']]} for d in self.cfg['domains']}
            encoded=dump(targets).replace("'","''")
            query+=" AND EXISTS(SELECT 1 FROM corpus_v2_status cs JOIN corpus_v2_metadata cm ON cm.paper_id=cs.paper_id,json_each('"+encoded+"') target WHERE cs.paper_id=p.id AND EXISTS(SELECT 1 FROM json_each(cs.domains) WHERE value=target.key) AND json_extract(cm.normalized,'$.canonical_publication_year') IN (SELECT value FROM json_each(target.value,'$.years')) AND EXISTS(SELECT 1 FROM json_each(cs.topics) domain_topics,json_each(domain_topics.value) topic WHERE domain_topics.key=target.key AND topic.value IN (SELECT value FROM json_each(target.value,'$.topics'))))"
        return query
    def claim(self):
        if getattr(self,'continuous',False):
            free=__import__('shutil').disk_usage(ROOT).free
            self.pdf_paused=free<max(self.args.reserve_gb,self.cfg['acquisition']['reserve_gb'])*1024**3
            if self.pdf_paused:return None
        if self.capacity_status().get('target_reached'):return None
        cap=getattr(self.args,'max_new_pdfs',None)
        if cap is not None:
            inflight=self.db.execute("SELECT count(*) FROM papers WHERE status='downloading'").fetchone()[0]
            if self.completed+inflight>=cap:return None
        return super().claim()
    def capacity_status(self,refresh=False):
        policy=getattr(self,'cfg',{}).get('storage_stage',{})
        if not policy.get('enabled') or not policy.get('target_fulltext_bytes'):return {'enabled':False,'target_reached':False}
        from corpus_capacity import stage
        if refresh or not hasattr(self,'_capacity_cache') or time.monotonic()-self._capacity_cache_at>5:
            self._capacity_cache=stage(self.db,self.cfg);self._capacity_cache_at=time.monotonic()
        value=dict(self._capacity_cache)
        value['pdf_bytes']=self.db.execute('SELECT coalesce(sum(bytes),0) FROM files').fetchone()[0]
        value['fulltext_bytes']=value['pdf_bytes']+value['xml_bytes'];value['remaining_bytes']=max(0,value['target_fulltext_bytes']-value['fulltext_bytes']);value['target_reached']=value['remaining_bytes']==0
        return value
    def corpus_resolve_filter(self):
        return self.corpus_download_filter()
    def corpus_focus_filter(self):return "EXISTS(SELECT 1 FROM corpus_v2_status cs,json_each(cs.domains) WHERE cs.paper_id=p.id AND value=?)"
    def normalize_download_candidate(self,pid,p):
        row=self.db.execute('SELECT normalized FROM corpus_v2_metadata WHERE paper_id=?',(pid,)).fetchone();s=self.db.execute('SELECT domains FROM corpus_v2_status WHERE paper_id=?',(pid,)).fetchone()
        if row:
            n=json.loads(row[0]);p=dict(p,doi=p.get('doi') or n.get('doi'),formal_doi=n.get('formal_doi') if not n.get('identity_metadata_conflict') else None,abstract=p.get('abstract') or n.get('abstract'),authors=p.get('authors') or n.get('authors'),keywords=p.get('keywords') or n.get('keywords'))
        if s:p['domains']=json.loads(s[0]) or p.get('domains',[])
        return p
    def reconcile(self,initial=False):
        # Cache configuration scope between checkpoints; workers must not rescan 22k JSON records per poll.
        self.scope_ids=set()
        eligible=[];chinese_ids=set()
        for pid,n,domains,topics in self.db.execute("SELECT s.paper_id,m.normalized,s.domains,s.topics FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id JOIN papers p ON p.id=s.paper_id WHERE p.status!='downloaded' AND s.relevant=1 AND s.excluded=0 AND s.needs_review=0 AND s.valid_pdf=0 AND NOT EXISTS(SELECT 1 FROM corpus_v2_status old WHERE old.article_identity=s.article_identity AND old.downloaded=1 AND old.valid_pdf=1) AND (NOT EXISTS(SELECT 1 FROM resolution_queue q WHERE q.paper_id=p.id) OR EXISTS(SELECT 1 FROM resolution_queue q WHERE q.paper_id=p.id AND q.status IN ('pending','retry','inflight')) OR EXISTS(SELECT 1 FROM download_locations l WHERE l.paper_id=p.id AND l.status IN ('pending','transient','cooldown','inflight'))) ORDER BY p.updated DESC"):
            matched=json.loads(domains)
            normalized=json.loads(n)
            if self.in_scope(normalized,matched,json.loads(topics)):
                eligible.append((pid,matched))
                from corpus_languages import has_han
                if has_han(normalized.get('normalized_title')):chinese_ids.add(pid)
        if self.continuous:
            ready={pid for pid,host,due in self.db.execute("SELECT paper_id,host,next_retry FROM download_locations WHERE status IN ('pending','transient','cooldown') AND next_retry<=?",(time.time(),)) if self.gate.cooldown.get(host,0)<=time.time()}
            if self.cfg.get('language_policy',{}).get('enabled'):
                from corpus_languages import balanced_frontier
                selected=balanced_frontier(eligible,chinese_ids,ready,self.frontier_limit,self.cfg['language_policy'].get('zh_candidate_fraction',.5),self.cfg['continuous'].get('ready_pdf_fraction',.7))
            else:selected=continuous.download_frontier(eligible,ready,self.frontier_limit,self.cfg['continuous'].get('ready_pdf_fraction',.7))
            self.scope_ids.update(selected)
        else:self.scope_ids.update(pid for pid,domains in eligible)
        # Raw legacy exclusions remain unchanged; only derived high-confidence candidates are considered.
        for pid,data,status in self.db.execute("SELECT p.id,p.data,p.status FROM papers p JOIN corpus_v2_status s ON s.paper_id=p.id WHERE p.status!='downloaded' AND s.relevant=1 AND s.excluded=0 AND s.valid_pdf=0").fetchall():
            if pid not in self.scope_ids:continue
            p=self.normalize_download_candidate(pid,json.loads(data))
            for u in list(dict.fromkeys(p.get('pdf_urls',[])+publisher_urls(p))):self.add_url(pid,u,'existing_metadata')
            for key in paper_keys(p):
                if key.startswith('arxiv:'):self.add_url(pid,'https://arxiv.org/pdf/'+key[6:],'arxiv_id')
            self.seed_resolution(pid,p,'failed' if status=='excluded_relevance' or status=='pending' else status)
        self.db.commit();self.reconcile_at=time.monotonic()
    def in_scope(self,n,domains,topics):
        selected=self.cfg.get('batch',{}).get('topics_by_domain',{})
        allowed=self.cfg.get('batch',{}).get('domains_allowed',[d['domain_name'] for d in self.cfg['domains']])
        year=n.get('canonical_publication_year')
        return any(d.get('enabled',True) and d['domain_name'] in allowed and d['domain_name'] in domains and (year in d['target_years'] or self.cfg.get('sampling',{}).get('include_classics') and year is not None and 1800<int(year)<min(d['target_years'])) and set(topics.get(d['domain_name'],[]))&set(selected.get(d['domain_name']) or [t['topic_id'] for t in d['topics']]) for d in self.cfg['domains'])
    def candidate_seen(self,pid,extra_facts=None,pdf=None):
        row=self.db.execute('SELECT data,status,path,sha256,bytes FROM papers WHERE id=?',(pid,)).fetchone()
        if not row:return
        p=json.loads(row[0]);old=self.db.execute('SELECT normalized FROM corpus_v2_metadata WHERE paper_id=?',(pid,)).fetchone()
        if pdf:self.file_checks[pid]=pdf
        pdf=pdf or self.file_checks.get(pid)
        n=self.normalizer.normalize(pid,p,pdf,extra_facts)
        results=self.classifier.all(p,n)
        # Prior negative semantic evidence remains a review guard after configuration expands.
        # It must not silently disappear because the global taxonomy hash changed.
        for d,r in list(results.items()):
            if r['relevance_label'] in ['relevant','probably_relevant','uncertain']:
                prior=self.db.execute('SELECT relevance_evidence FROM corpus_v2_relevance WHERE paper_id=? AND domain=? ORDER BY updated DESC LIMIT 1',(pid,d)).fetchone()
                prior_answer=json.loads(prior[0]).get('semantic') if prior else None
                if prior_answer and prior_answer.get('relevance_label') in ['uncertain','probably_irrelevant','irrelevant']:results[d]=apply_semantic(r,prior_answer)
                elif prior_answer and r['relevance_label']=='uncertain' and prior_answer.get('quote_verified') and prior_answer.get('core_evidence','').casefold() in ((p.get('title') or '')+' '+(n.get('abstract') or '')).casefold():results[d]=apply_semantic(r,prior_answer)
        for d,result in results.items():
            if result['relevance_label']=='uncertain' and self.local_semantic:
                cached=self.local_semantic.cached(dict(p,abstract=n.get('abstract') or p.get('abstract')),d)
                if cached:results[d]=apply_semantic(result,cached)
        for d,result in results.items():put_relevance(self.db,pid,d,result,self.cfg,self.run_id)
        from corpus_repair import acceptance
        validations={d:dict(json.loads(checks),verdict=verdict) for d,verdict,checks in self.db.execute('SELECT domain,verdict,checks FROM corpus_v2_validation WHERE paper_id=?',(pid,))}
        evaluation=acceptance(p,n,pdf if row[1]=='downloaded' else None,results,self.cfg,validations)
        domains=evaluation['domains'];topics=evaluation['topics'];review=evaluation['needs_review']
        identity=self.db.execute('SELECT article_identity FROM corpus_v2_status WHERE paper_id=?',(pid,)).fetchone()
        if identity:identity=identity[0]
        else:
            found=self.db.execute("SELECT s.article_identity FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id WHERE s.paper_id!=? AND ((json_extract(m.normalized,'$.doi')=? AND ?!='') OR (json_extract(m.normalized,'$.arxiv_id')=? AND ?!='')) LIMIT 1",(pid,n.get('doi') or '',n.get('doi') or '',n.get('arxiv_id') or '',n.get('arxiv_id') or '')).fetchone();identity=found[0] if found else 'article:'+pid
            if not found and len(n.get('normalized_title') or '')>=25:
                from corpus_repair import author_keys
                aa=author_keys(n.get('authors'))
                for other,othermeta in self.db.execute("SELECT s.article_identity,m.normalized FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id WHERE s.paper_id!=? AND json_extract(m.normalized,'$.normalized_title')=?",(pid,n['normalized_title'])):
                    oo=json.loads(othermeta);bb=author_keys(oo.get('authors'));years=(n.get('canonical_publication_year'),oo.get('canonical_publication_year'))
                    if aa and bb and len(aa&bb)/min(len(aa),len(bb))>=.5 and (None in years or abs(years[0]-years[1])<=10):identity=other;break
            aliases=[value for value in [n.get('doi'),n.get('external_formal_doi'),*[r.get('relation_doi') for r in n.get('version_relations',[])]] if value]
            if aliases and not n.get('identity_metadata_conflict'):
                placeholders=','.join('?' for value in aliases)
                found=self.db.execute("SELECT s.article_identity FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id WHERE s.paper_id!=? AND (json_extract(m.normalized,'$.doi') IN ("+placeholders+") OR json_extract(m.normalized,'$.external_formal_doi') IN ("+placeholders+")) LIMIT 1",(pid,*aliases,*aliases)).fetchone()
                if found:identity=found[0]
        registry=self.db.execute('SELECT members,doi_aliases,arxiv_aliases,evidence FROM corpus_v2_identity WHERE article_identity=?',(identity,)).fetchone();members=set(json.loads(registry[0])) if registry else set();members.add(pid);dois=set(json.loads(registry[1])) if registry else set();dois.update(value for value in [n.get('doi'),n.get('external_formal_doi')] if value);aids=set(json.loads(registry[2])) if registry else set()
        if n.get('arxiv_id'):aids.add(n['arxiv_id'])
        proof=json.loads(registry[3]) if registry else [];self.db.execute('INSERT INTO corpus_v2_identity VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(article_identity) DO UPDATE SET members=excluded.members,doi_aliases=excluded.doi_aliases,arxiv_aliases=excluded.arxiv_aliases,updated=excluded.updated',(identity,p.get('title'),dump(sorted(members)),dump(sorted(dois)),dump(sorted(aids)),dump(proof),.93 if len(members)>1 else 1,now()))
        for rel in n.get('version_relations',[]):
            reverse=rel.get('relation_type')=='has-preprint';edge={'paper_id':pid,'article_identity':identity,'relation':rel};key=hashlib.sha256(dump(edge).encode()).hexdigest();other=self.db.execute("SELECT paper_id FROM corpus_v2_metadata WHERE json_extract(normalized,'$.doi')=? AND paper_id!=? LIMIT 1",(rel['relation_doi'],pid)).fetchone()
            self.db.execute('INSERT OR IGNORE INTO corpus_v2_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,pid,other[0] if other else None,identity,rel['relation_doi'] if reverse else n.get('doi') if n.get('preprint_source') else None,n.get('doi') if reverse else rel['relation_doi'],n.get('downloaded_version'),'formal_publication' if reverse or rel.get('relation_type')=='is-preprint-of' else 'publisher_declared_related_version',rel['confidence'],dump(rel),now()))
        downloaded=row[1]=='downloaded';q=pdf or {};existing=self.db.execute('SELECT paper_id FROM corpus_v2_status WHERE article_identity=? AND accepted_for_corpus=1 AND paper_id!=? LIMIT 1',(identity,pid)).fetchone();rep=existing[0] if existing else pid
        accepted=evaluation['eligible'] and rep==pid;state='accepted_for_corpus' if accepted else 'metadata_only' if not downloaded else 'duplicate_version' if existing and evaluation['eligible'] else 'needs_review' if review else 'excluded'
        self.db.execute('INSERT INTO corpus_v2_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET article_identity=excluded.article_identity,representative_id=excluded.representative_id,downloaded=excluded.downloaded,valid_pdf=excluded.valid_pdf,relevant=excluded.relevant,accepted_for_corpus=excluded.accepted_for_corpus,duplicate_version=excluded.duplicate_version,metadata_only=excluded.metadata_only,needs_review=excluded.needs_review,excluded=excluded.excluded,exclusion_reason=excluded.exclusion_reason,domains=excluded.domains,topics=excluded.topics,primary_state=excluded.primary_state,run_id=excluded.run_id,updated=excluded.updated',(pid,identity,rep,int(downloaded),int(q.get('valid_pdf',False)),int(bool(domains)),int(accepted),int(rep!=pid),int(not downloaded),int(review),int(evaluation['excluded']),';'.join(evaluation['issues']),dump(domains),dump(topics),state,self.run_id,now()));self.db.commit()
        if hasattr(self,'scope_ids'):
            self.scope_ids.discard(pid)
            if not downloaded and not review and not evaluation['excluded'] and self.in_scope(n,domains,topics) and (not self.continuous or len(self.scope_ids)<self.frontier_limit):self.scope_ids.add(pid)
        if self.continuous:
            continuous.save_tier(self.db,pid,p,n,domains,topics,self.cfg);self.db.commit()
    def resolution_completed(self,pid,name,extra):
        facts=[]
        if extra.get('source_record'):
            from literature_audit import fact
            p=json.loads(self.db.execute('SELECT data FROM papers WHERE id=?',(pid,)).fetchone()[0]);facts.append(fact(name,extra['source_record'],p))
        self.candidate_seen(pid,facts)
    def download_completed(self,pid):
        from corpus_repair import check_pdf
        self.db.row_factory=__import__('sqlite3').Row;r=dict(self.db.execute('SELECT * FROM papers WHERE id=?',(pid,)).fetchone());self.db.row_factory=None;q=check_pdf(pid,r);self.candidate_seen(pid,pdf=q);self.last_gain=time.monotonic()
        folder=STATE/'repair_v3/pdf_checks';folder.mkdir(exist_ok=True);atomic_json(folder/(pid+'.json'),q)
        if self.cfg.get('language_policy',{}).get('enabled'):
            from corpus_languages import record
            from project_paths import resolve_project_path
            record(self.db,ROOT,self.cfg,pid,json.loads(r['data']),resolve_project_path(r['path']))
    def preferred_download_domain(self):
        counts=coverage_counts(self.db)[0]
        return min(continuous.enabled(self.cfg),key=lambda d:sum(counts[(d['domain_name'],y)] for y in d['target_years'])/max(1,d.get('target_depth',d.get('target_count')) or 1))['domain_name']
    def search_allowed(self):
        if not self.network_ready():return False
        if self.capacity_status().get('target_reached'):return False
        if self.continuous:
            frontier_full=len(self.scope_ids)>=self.frontier_limit
            self.search_paused=False
            if self.pdf_paused:
                before=continuous.setting(self.db,'disk_pause_candidates')
                current=self.db.execute('SELECT count(*) FROM papers').fetchone()[0]
                if before is None:continuous.checkpoint(self.db,'disk_pause_candidates',current);before=current
                if current-before>=self.cfg['continuous']['disk_metadata_candidate_budget']:self.search_paused=True;return False
            else:
                if continuous.setting(self.db,'disk_pause_candidates') is not None:continuous.checkpoint(self.db,'disk_pause_candidates',None)
            # Initial breadth gets a bounded discovery opportunity even with a full PDF queue.
            dispatched={r[0] for r in self.db.execute('SELECT DISTINCT domain FROM corpus_v5_dispatch')}
            breadth_due=self.cfg['continuous'].get('fair_dispatch') and any(d['domain_name'] not in dispatched for d in continuous.enabled(self.cfg))
            return time.monotonic()-self.started>=self.args.backlog_first_minutes*60 and time.monotonic()-self.last_search>=(45 if frontier_full and not breadth_due else 6)
        return time.monotonic()-self.started>=self.args.backlog_first_minutes*60 and time.monotonic()-self.last_search>=6
    def select_search_job(self,unused):
        planned=annual_jobs(self.db,self.cfg);self.pending_search_sources={j['source'] for j in planned}
        jobs=[j for j in planned if self.source_ready.get(j['source'],0)<=time.time() and self.gate.ready(SEARCH_HOSTS[j['source']],api=True) and (not self.continuous or continuous.budget_ready(self.db,self.cfg,j['source']))]
        if not jobs:return None
        if self.continuous:
            self.dispatch+=1;queue='exploration' if self.dispatch%20==0 else 'medium' if self.dispatch%20 in [5,10,15] else 'high'
            options=[j for j in jobs if j['queue']==queue] or jobs
            historical=[j for j in jobs if j['purpose']=='historical']
            if continuous.historical_slot(self.dispatch,self.cfg['continuous'].get('historical_fraction',0)) and historical:options=historical
            if self.cfg.get('language_policy',{}).get('enabled'):
                turn=continuous.setting(self.db,'language_dispatch_turn',0)
                target='zh' if continuous.historical_slot(turn,self.cfg['language_policy'].get('zh_search_fraction',.5)) else None
                language_options=[j for j in jobs if (j.get('language')=='zh')==(target=='zh')]
                if language_options:
                    filtered=[j for j in options if j in language_options];options=filtered or language_options
            if self.profile is None:self.profile=continuous.coverage_profile(self.db,self.cfg)
            health=continuous.source_health(self.db);definitions={d['domain_name']:d for d in continuous.enabled(self.cfg)}
            def score(j):
                d=definitions[j['domain']];t=next(t for t in d['topics'] if t['topic_id']==j['topic_id']);return (-continuous.priority(d,t,j['lo'],j['source'],self.profile,health,j['purpose'])[0]/(1+j['page']*.25),j['page'])
            return continuous.fair_job(self.db,options,score,{name:d.get('parent_domain') or name for name,d in definitions.items()}) if self.cfg['continuous'].get('fair_dispatch') else min(options,key=score)
        counts=coverage_counts(self.db);return min(jobs,key=lambda j:score_job(j,self.cfg,counts))
    def reserve_search(self,source):
        ready=self.gate.reserve(SEARCH_HOSTS[source],api=True)
        if ready:self.last_search=time.monotonic()
        return ready
    def network_source_host(self,source):return SEARCH_HOSTS[source]
    def release_search(self,source):self.gate.release(SEARCH_HOSTS[source])
    def search_child_created(self,parent,child):
        if self.continuous:continuous.record_child(self.db,parent,child,self.cfg)
    def search_dispatched(self,job):
        if self.continuous:continuous.dispatched(self.db,job)
    def api_budget(self,source):
        if not continuous.budget_ready(self.db,self.cfg,source):raise continuous.ApiBudgetDeferred(source)
        continuous.spend(self.db,source)
    def api_budget_ready(self,source):return not self.continuous or continuous.budget_ready(self.db,self.cfg,source)
    async def api(self,client,pid,name,url,params=None,text=False):
        if self.continuous:self.api_budget(name)
        return await super().api(client,pid,name,url,params,text)
    async def metadata_worker(self):
        """Small independent formal-metadata and citation-reference lane."""
        import httpx
        from resilient_http import RecoveringClient
        from network_recovery import transient_request_error,retry_delay
        from collect import Adapter
        from literature_audit import similarity,crossref_title
        from corpus_repair import author_keys
        from urllib.parse import quote
        API_HOSTS['crossref']='api.crossref.org'
        folder=STATE/'continuous_metadata';folder.mkdir(exist_ok=True)
        async with RecoveringClient(timeout=25,headers={'User-Agent':'paper-pilot-corpus/4.0'},follow_redirects=True) as client:
            while not self.stopping():
                if not self.network_ready():await asyncio.sleep(5);continue
                if self.pdf_paused and not self.search_allowed():await asyncio.sleep(10);continue
                # Prefer incomplete downloaded works; queue has bounded size and persistent retries.
                if time.monotonic()-self.metadata_ready>600:
                    self.metadata_ready=time.monotonic()
                    self.db.execute("INSERT OR IGNORE INTO corpus_v4_metadata_queue(paper_id,updated) SELECT s.paper_id,? FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id WHERE s.downloaded=1 AND (json_extract(m.normalized,'$.formal_venue') IS NULL OR json_extract(m.normalized,'$.publisher') IS NULL) AND coalesce(json_extract(m.normalized,'$.formal_doi'),json_extract(m.normalized,'$.doi'),'')!='' LIMIT 500",(now(),));self.db.commit()
                row=self.db.execute("SELECT work_id,domain,evidence,attempts FROM corpus_v4_references WHERE status IN ('pending','retry') AND next_retry<=? LIMIT 1",(time.time(),)).fetchone() if self.dispatch%5==0 else None
                if row and continuous.budget_ready(self.db,self.cfg,'openalex'):
                    work,domain,proof,attempts=row
                    try:
                        raw=await self.api(client,None,'openalex','https://api.openalex.org/works/'+work)
                        adapter=Adapter(self.settings,self.lib,{'source':'openalex','purpose':'classic','lo':0,'hi':0});record=adapter._paper_from_openalex(raw);adapter.raw_items[str(record.source_id)]=raw
                        pid=self.lib.ingest(adapter.enrich(record),domain,'citation_graph_reference','reference:'+work);self.candidate_seen(pid)
                        self.db.execute("UPDATE corpus_v4_references SET status='done',evidence=?,updated=? WHERE work_id=?",(dump({'citing_evidence':json.loads(proof),'resolved_paper_id':pid,'source':'https://api.openalex.org/works/'+work}),now(),work))
                    except Exception as error:
                        network=transient_request_error(error);due=time.time()+(retry_delay(attempts+1,self.cfg.get('network_recovery')) if network else min(3600,60*2**min(attempts,6)))
                        self.db.execute("UPDATE corpus_v4_references SET status=?,attempts=attempts+1,next_retry=?,updated=? WHERE work_id=?",('retry' if network else 'unavailable' if attempts>=2 else 'retry',due,now(),work));self.lib.event('reference_metadata_error',work=work,error=type(error).__name__+': '+str(error)[:200])
                        if network:await client.reset()
                    self.db.commit();await asyncio.sleep(10);continue
                row=self.db.execute("SELECT q.paper_id,p.data,m.normalized,q.attempts FROM corpus_v4_metadata_queue q JOIN papers p ON p.id=q.paper_id JOIN corpus_v2_metadata m ON m.paper_id=q.paper_id WHERE q.status IN ('pending','retry') AND q.next_retry<=? LIMIT 1",(time.time(),)).fetchone()
                if not row or not continuous.budget_ready(self.db,self.cfg,'crossref'):await asyncio.sleep(20);continue
                pid,data,normalized,attempts=row;p=json.loads(data);n=json.loads(normalized);doi=n.get('formal_doi') or n.get('doi')
                try:
                    raw=await self.api(client,pid,'crossref','https://api.crossref.org/works/'+quote(doi,safe=''))
                    raw=raw['message'];score=similarity(p.get('title'),crossref_title(raw));aa=author_keys(p.get('authors'));bb=author_keys([' '.join([a.get('given',''),a.get('family','')]) for a in raw.get('author',[])]);overlap=len(aa&bb)/min(len(aa),len(bb)) if aa and bb else 0
                    proof={'crossref':raw,'title_similarity':score,'author_overlap':overlap,'source':'https://api.crossref.org/works/'+doi,'retrieved_at':now()}
                    if score<.8 or aa and bb and overlap<.5:raise ValueError('metadata identity mismatch; retain for review')
                    atomic_json(folder/(pid+'.json'),proof);self.candidate_seen(pid)
                    self.db.execute("UPDATE corpus_v4_metadata_queue SET status='done',evidence=?,updated=? WHERE paper_id=?",(dump({'source':proof['source'],'title_similarity':score,'author_overlap':overlap,'cache':str((folder/(pid+'.json')).relative_to(ROOT))}),now(),pid))
                    self.lib.event('formal_metadata_verified',id=pid,doi=doi,title_similarity=score)
                except Exception as error:
                    network=transient_request_error(error);due=time.time()+(retry_delay(attempts+1,self.cfg.get('network_recovery')) if network else min(3600,60*2**min(attempts,6)))
                    self.db.execute("UPDATE corpus_v4_metadata_queue SET status=?,attempts=attempts+1,next_retry=?,evidence=?,updated=? WHERE paper_id=?",('retry' if network else 'unavailable' if attempts>=2 else 'retry',due,dump({'error':type(error).__name__+': '+str(error)[:200],'temporary_network_error':network}),now(),pid))
                    if network:await client.reset()
                self.db.commit();await asyncio.sleep(20)
    async def discovery_worker(self):
        # CPU-heavy clustering reads a separate snapshot off the networking event loop.
        # Only this collector's event loop persists resulting proposals and graph tasks.
        def analyze():
            import sqlite3
            with sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
                db.execute('BEGIN');return continuous.discovery(db,self.cfg,persist=False)
        try:
            payload=await asyncio.to_thread(analyze);continuous.persist_discovery(self.db,payload)
        except Exception as error:self.lib.event('domain_discovery_error',error=str(error)[:240])
    async def semantic_worker(self):
        local=LocalSemantic(self.cfg)
        while not self.stopping():
            rows=self.db.execute("SELECT q.paper_id,q.domain,p.data FROM corpus_v2_semantic_queue q JOIN papers p ON p.id=q.paper_id WHERE q.status='pending' AND q.classifier_version=? AND p.status!='downloaded' ORDER BY q.priority DESC LIMIT ?",(version(self.cfg),self.cfg['semantic']['batch_size'])).fetchall()
            if not rows:await asyncio.sleep(2);continue
            items=[{'paper_id':pid,'domain':d,'p':json.loads(data)} for pid,d,data in rows];answers=await asyncio.to_thread(local.classify_batch,items)
            self.semantic_available=all(a.get('available',True) for a in answers.values())
            for item in items:
                if not answers[(item['paper_id'],item['domain'])].get('available',True):continue
                pid=item['paper_id'];d=item['domain'];answer=answers[(pid,d)];n=json.loads(self.db.execute('SELECT normalized FROM corpus_v2_metadata WHERE paper_id=?',(pid,)).fetchone()[0]);rule=self.classifier.classify(item['p'],d,n);result=apply_semantic(rule,answer);put_relevance(self.db,pid,d,result,self.cfg,self.run_id);self.db.execute("UPDATE corpus_v2_semantic_queue SET status='done',result=?,updated=? WHERE paper_id=? AND domain=? AND classifier_version=?",(dump(answer),now(),pid,d,version(self.cfg)))
                # Reuse the just-cached decision and the common type/date/identity guards.
                self.candidate_seen(pid)
            self.db.commit()
            if any(not a.get('available',True) for a in answers.values()):await asyncio.sleep(60)
    async def monitor(self):
        initial_capacity=self.capacity_status(refresh=True)
        if initial_capacity.get('target_reached'):
            self.stop=True;atomic_json(STATE/'run_state.json',{'pid':__import__('os').getpid(),'status':'stopped_capacity_target','mode':'corpus_balanced_annual','updated':now(),'run_id':self.run_id,'minutes_budget':self.args.minutes,'max_new_pdfs':getattr(self.args,'max_new_pdfs',None),'storage_stage':initial_capacity});return 'stopped_capacity_target'
        atomic_json(STATE/'run_state.json',{'pid':__import__('os').getpid(),'status':'running','mode':'corpus_balanced_annual','updated':now(),'run_id':self.run_id,'minutes_budget':self.args.minutes,'max_new_pdfs':getattr(self.args,'max_new_pdfs',None),'phase':'workers_started','semantic_model_available':False if not self.cfg['semantic']['enabled'] else None})
        while not self.stopping():
            await asyncio.sleep(5)
            if getattr(self,'network_recovery',None):
                from collect import allowed_url
                await self.network_recovery.probe(allowed_url)
            failed=[task.exception() for task in getattr(self,'engine_tasks',[]) if task.done() and not task.cancelled() and task.exception()]
            if failed:
                atomic_json(STATE/'run_state.json',{'pid':__import__('os').getpid(),'status':'failed_worker','run_id':self.run_id,'updated':now(),'error':str(failed[0])});raise RuntimeError('Collector worker failed: '+str(failed[0]))
            capacity=self.capacity_status(refresh=True)
            if capacity.get('target_reached'):
                self.stop=True;atomic_json(STATE/'run_state.json',{'pid':__import__('os').getpid(),'status':'stopped_capacity_target','mode':'corpus_balanced_annual','updated':now(),'run_id':self.run_id,'minutes_budget':self.args.minutes,'max_new_pdfs':getattr(self.args,'max_new_pdfs',None),'storage_stage':capacity});return 'stopped_capacity_target'
            if time.monotonic()-self.reconcile_at>60:self.reconcile()
            if time.monotonic()-self.exported_at>self.args.export_seconds:
                self.lib.export(metadata=False);self.exported_at=time.monotonic()
            free=__import__('shutil').disk_usage(ROOT).free;accepted=self.db.execute('SELECT sum(accepted_for_corpus) FROM corpus_v2_status').fetchone()[0]
            metrics=self.metrics()
            state={'pid':__import__('os').getpid(),'status':'running','mode':'corpus_balanced_annual','updated':now(),'run_id':self.run_id,'accepted_for_corpus':accepted,'download_workers':self.args.download_workers,'free_bytes':free,'minutes_budget':self.args.minutes,'max_new_pdfs':getattr(self.args,'max_new_pdfs',None),'pdf_paused_low_disk':getattr(self,'pdf_paused',False),'download_frontier':len(self.scope_ids),'new_pdf_files':metrics['new_pdf_files'],'pdf_per_minute':metrics['pdf_per_minute'],'semantic_model_available':getattr(self,'semantic_available',None)}
            state['storage_stage']=capacity
            if getattr(self,'network_recovery',None):state['network_recovery']=dict(self.network_recovery.state)
            if self.continuous:
                self.pdf_paused=free<max(self.args.reserve_gb,self.cfg['acquisition']['reserve_gb'])*1024**3;state['pdf_paused_low_disk']=self.pdf_paused
                if free<1024**3:self.stop=True;state['status']='stopped_critical_disk';atomic_json(STATE/'run_state.json',state);return state['status']
                if time.monotonic()-self.last_plan>self.cfg['continuous']['refresh_seconds']:
                    continuous.seed_frontier(self.db,self.cfg);self.profile=continuous.coverage_profile(self.db,self.cfg);self.last_plan=time.monotonic();self.discovery_done=False
                if time.monotonic()-self.last_live>120:
                    self.disk_info=continuous.disk_usage(self.db);stats=continuous.live(self.db,self.cfg,metrics,self.disk_info);continuous.plan(self.db,self.cfg);self.last_live=time.monotonic()
                    last=continuous.setting(self.db,'last_audit',{'time':0,'accepted':0})
                    if time.time()-last['time']>=self.cfg['continuous']['audit_seconds'] or stats['accepted_for_corpus']-last['accepted']>=self.cfg['continuous']['audit_accepted_delta']:continuous.audit(self.db,self.cfg,stats)
                    if time.time()-continuous.setting(self.db,'last_discovery',0)>=self.cfg['continuous']['discovery_seconds'] and (not hasattr(self,'discovery_task') or self.discovery_task.done()):self.discovery_task=asyncio.create_task(self.discovery_worker())
                    self.db.execute('PRAGMA wal_checkpoint(PASSIVE)')
                    if self.cfg.get('language_policy',{}).get('enabled'):
                        from corpus_languages import export
                        export(self.db,ROOT)
            elif free<self.args.reserve_gb*1024**3:self.stop=True;return 'stopped_resource_limit'
            atomic_json(STATE/'run_state.json',state)
            if getattr(self.args,'max_new_pdfs',None) is not None and self.completed>=self.args.max_new_pdfs:self.stop=True;return 'completed_bounded_batch'
            idle=self.cfg['acquisition'].get('no_progress_hours')
            if idle and time.monotonic()-self.last_gain>idle*3600:self.stop=True;return 'stopped_no_progress'
        return 'stopped_checkpoint_saved'
    async def run(self):
        task=asyncio.create_task(self.semantic_worker()) if self.cfg['semantic']['enabled'] else None
        metadata_task=asyncio.create_task(self.metadata_worker()) if self.continuous else None
        try:await super().run()
        finally:
            self.stop=True
            if hasattr(self,'discovery_task'):await asyncio.gather(self.discovery_task,return_exceptions=True)
            if task is not None:
                if not task.done():task.cancel()
                await asyncio.gather(task,return_exceptions=True)
            if metadata_task:
                if not metadata_task.done():metadata_task.cancel()
                await asyncio.gather(metadata_task,return_exceptions=True)
            # Refresh the V3 audit after the existing engine has committed/exported its checkpoint.
            from corpus_v3 import audit
            audit(self.db)
            if self.continuous:
                final_state=json.loads((STATE/'run_state.json').read_text(encoding='utf8'))
                stats=continuous.live(self.db,self.cfg,self.metrics(final_state.get('status','stopped_checkpoint_saved')));continuous.audit(self.db,self.cfg,stats)
                if self.cfg.get('storage_stage',{}).get('enabled'):
                    from corpus_capacity import inventory
                    inventory(self.db,self.cfg)
