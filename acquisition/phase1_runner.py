"""Finite, resumable phase one using the existing collector, queues and gates."""
from __future__ import annotations
import asyncio,json,time
from collections import Counter
from datetime import datetime,timezone
from collect import STATE,DOMAINS,TOPICS,dump,stamp,norm
from download_engine import OptimizedRunner,API_HOSTS
from phase1_audit import PhaseRegistry,atomic,topic_labels
from phase1_audit import DOMAINS

SEARCH_HOSTS=dict(API_HOSTS,crossref='api.crossref.org',doaj='doaj.org')

class PhaseRunner(OptimizedRunner):
    def __init__(self,lib,args):
        super().__init__(lib,args)
        self.phase=PhaseRegistry(lib);self.phase_reason=None;self.phase_report_due=False;self.phase_last_gain=time.monotonic();self.last_search=0;self.phase_last_progress=0
        self.db.execute("update download_locations set status='pending',attempts=0,next_retry=0 where status in ('transient','exhausted') and last_error like '%PoolTimeout%'");self.db.commit()
        def verify_progress(done,total):
            atomic(STATE/'run_state.json',dump(dict(pid=__import__('os').getpid(),status='verifying_existing_pdfs',updated=stamp(),phase='phase1',checked=done,to_check=total,target=3000)))
        self.phase.refresh(callback=verify_progress)
        self.phase_baseline=self.phase.summary()['valid_unique_papers']
        self.phase_last_progress=self.phase_baseline
        self.phase.progress('running');self.check_phase_goal()
    def check_phase_goal(self):
        if self.phase.summary()['goal_met']:
            self.phase_reason='phase1_target_reached';self.stop=True;return True
        return False
    def download_completed(self,pid):
        before=self.phase.summary()['valid_unique_papers'];self.phase.add(pid);s=self.phase.summary()
        if s['valid_unique_papers']>before:self.phase_last_gain=time.monotonic()
        if s['valid_unique_papers']-self.phase_last_progress>=self.phase.config['checkpoint_every_unique_pdfs']:
            s=self.phase.progress('running')
            with (STATE/'phase1_progress.jsonl').open('a',encoding='utf-8') as f:f.write(dump(s)+'\n')
            self.phase_last_progress=s['valid_unique_papers'];self.phase_report_due=True
        self.check_phase_goal()
    def needs(self,d):
        s=self.phase.summary();return s['domains'].get(d,0)<self.phase.config['minimum_per_domain'] or s['topics_with_minimum'][d]<self.phase.config['minimum_topics_per_domain']
    def domain_rank(self,d):
        s=self.phase.summary()
        return (0 if self.needs(d) else 1,s['domains'].get(d,0)/self.phase.config['minimum_per_domain']+.35*s['topics_with_minimum'][d]/self.phase.config['minimum_topics_per_domain'])
    def search_allowed(self):
        if self.check_phase_goal():return False
        # Existing PDFs take priority; only open focused searches for field/topic shortfalls.
        if time.monotonic()-self.started<120 or time.monotonic()-self.last_search<6:self.search_paused=True;return False
        needed=[d for d in DOMAINS if self.needs(d)]
        if not needed:needed=DOMAINS if self.phase.summary()['valid_unique_papers']<self.phase.config['target_unique_papers'] else []
        s=self.phase.summary();self.focus_domain=min(needed,key=self.domain_rank) if needed else DOMAINS[0]
        breadth=s['topics_with_minimum'][self.focus_domain]
        ready=self.db.execute("select count(distinct p.id) from papers p join download_locations l on l.paper_id=p.id where p.status in ('pending','failed','no_oa_url','suspected_incomplete') and l.status in ('pending','transient','cooldown') and l.next_retry<=? and exists(select 1 from json_each(p.data,'$.domains') where value=?) and l.host not in(select substr(key,6) from controls where key like 'host:%' and value>?)",(time.time(),self.focus_domain,time.time())).fetchone()[0]
        allow=breadth<self.phase.config['minimum_topics_per_domain'] or ready<250
        self.search_paused=not allow;return allow
    def select_search_job(self,ignored_rows):
        now=time.time();cols=['id','domain','topic','source','lo','hi','sort','cursor','page','priority','attempts']
        rows=self.db.execute("select id,domain,topic,source,lo,hi,sort,cursor,page,priority,attempts from jobs where status in ('pending','retry') and next_retry<=?",(now,)).fetchall()
        rows=[dict(zip(cols,r)) for r in rows if self.source_ready.get(r[3],0)<=now and self.gate.ready(SEARCH_HOSTS[r[3]],api=True)]
        if not rows:return None
        summary=self.phase.summary();providers={'openalex':0,'arxiv':1,'doaj':2,'europe_pmc':3,'crossref':4,'semantic_scholar':5}
        def score(j):
            d=j['domain'];canonical=next((en for en,zh in TOPICS[d] if norm(j['topic']) in [norm(en),norm(zh)]),j['topic'])
            n=summary['topics'][d].get(canonical,0)
            years=sum(v for y,v in summary['years'].items() if y.isdigit() and j['lo']<=int(y)<=j['hi'])
            return (self.domain_rank(d),0 if n<self.phase.config['minimum_papers_per_topic'] else 1,n,j['page'],0 if j['lo']>=2015 else 1,years,providers[j['source']],j['priority'])
        return min(rows,key=score)
    def reserve_search(self,source):
        ok=self.gate.reserve(SEARCH_HOSTS[source],api=True)
        if ok:self.last_search=time.monotonic()
        return ok
    def release_search(self,source):self.gate.release(SEARCH_HOSTS[source])
    def claim(self):
        if self.check_phase_goal():return None
        d=min(DOMAINS,key=self.domain_rank);self.domain_index=DOMAINS.index(d)
        # The existing dispatcher handles readiness and all host restrictions.
        return super().claim()
    def preferred_download_domain(self):return min(DOMAINS,key=self.domain_rank)
    def candidate_priority(self,p):
        s=self.phase.summary();d=self.preferred_download_domain();themes=topic_labels(p,d)
        n=min((s['topics'][d].get(t,0) for t in themes),default=0)
        venue_n=s['venues'][d].get(p.get('venue'),0)
        return (0 if n<self.phase.config['minimum_papers_per_topic'] else 1,1 if p.get('venue') and venue_n>self.phase.config['topic_concentration_soft_limit']*max(1,s['domains'].get(d,0)) else 0,n)
    async def monitor(self):
        reason='stopped_checkpoint_saved';last_report=0
        while not self.stopping():
            await asyncio.sleep(5)
            if time.monotonic()-self.reconcile_at>60:
                self.reconcile();self.phase.refresh();self.check_phase_goal()
            if self.phase_report_due or time.monotonic()-last_report>=self.phase.config['progress_export_seconds']:
                self.phase.progress('running');self.phase.report('running');last_report=time.monotonic();self.phase_report_due=False
            m=self.metrics();s=self.phase.summary()
            free=__import__('shutil').disk_usage(self.root).free
            state=dict(pid=__import__('os').getpid(),status='running',phase='phase1',updated=stamp(),run_id=self.run_id,download_workers=self.args.download_workers,resolver_workers=self.args.resolver_workers,search_paused=self.search_paused,valid_unique_papers=s['valid_unique_papers'],target=3000,domains=s['domains'],topics_with_minimum=s['topics_with_minimum'],new_pdf_files=m['new_pdf_files'],pdf_per_minute=m['pdf_per_minute'],free_bytes=free)
            if free<self.args.reserve_gb*1024**3:self.stop=True;reason='stopped_resource_limit';self.phase_reason=reason
            if time.monotonic()-self.phase_last_gain>self.phase.config['no_progress_hours']*3600:self.stop=True;reason='stopped_no_progress';self.phase_reason=reason
            atomic(STATE/'run_state.json',dump(state))
        return self.phase_reason or reason
    async def run(self):
        self.phase.report('running')
        try:await super().run()
        except Exception as e:
            self.phase_reason='stopped_error';self.phase_error=type(e).__name__+': '+str(e)
            self.db.execute('insert into phase1_state values(?,?) on conflict(key) do update set value=excluded.value',('last_error',self.phase_error));self.db.commit()
        atomic(STATE/'run_state.json',dump(dict(pid=__import__('os').getpid(),status='phase1_acceptance_running',phase='phase1',updated=stamp(),reason=self.phase_reason)))
        # All network tasks have safely finished before full file verification.
        result=self.phase.report(self.phase_reason or 'interrupted',full=True)
        if self.phase_reason=='phase1_target_reached' and not result['goal_met']:
            # A changed identifier/file found in final acceptance must not create a false finish.
            self.stop=False;self.phase_reason=None;self.phase_last_gain=time.monotonic();self.run_id=__import__('uuid').uuid4().hex[:16];self.started=time.monotonic();self.baseline_files=self.db.execute('select count(distinct sha256) from files').fetchone()[0];self.completed=0
            self.db.execute('insert into engine_runs values(?,?,?,?,?,?,?)',(self.run_id,stamp(),None,self.baseline_files,'running',dump(vars(self.args)),None));self.db.commit()
            return await self.run()
        completed=result['goal_met'] and self.phase_reason=='phase1_target_reached'
        status='completed' if completed else self.phase_reason or 'interrupted'
        if completed:
            # Retain the full-file and directory-size measurements in the completed report.
            final=self.phase.report('completed')
            final['total_directory_bytes']=result.get('total_directory_bytes')
            atomic(STATE/'phase1_acceptance.json',dump(final));result=final
        self.phase.progress(status)
        self.db.execute('insert into phase1_state values(?,?) on conflict(key) do update set value=excluded.value',('completion',dump(dict(status=status,goal_met=completed,at=stamp(),valid_unique_papers=result['valid_unique_papers']))));self.db.commit()
        atomic(STATE/'run_state.json',dump(dict(pid=__import__('os').getpid(),status='phase1_completed' if completed else status,phase='phase1',updated=stamp(),goal_met=completed,valid_unique_papers=result['valid_unique_papers'],domains=result['domains'],topics_with_minimum=result['topics_with_minimum'],error=getattr(self,'phase_error',None),report=str(self.root/'PHASE1_REPORT.md'))))
