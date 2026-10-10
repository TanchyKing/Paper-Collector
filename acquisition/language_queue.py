"""Bounded Chinese annual searches sharing the original queues and API budgets."""
import datetime,hashlib,json,time
from corpus_languages import has_han

def seed(db,cfg):
    import corpus_continuous as c
    from corpus_planner import job_id
    policy=cfg.get('language_policy',{})
    if not policy.get('enabled'):return 0
    active=db.execute("SELECT count(*) FROM jobs j JOIN corpus_v4_job_policy p ON p.job_id=j.id WHERE j.status IN ('pending','retry','running') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1)").fetchone()[0]
    room=min(max(1,int(cfg['continuous']['new_jobs_per_refresh']*policy.get('zh_search_fraction',.5))),max(0,cfg['continuous']['frontier_limit']-active))
    if not room:return 0
    history=dict(db.execute("SELECT j.domain,count(*) FROM jobs j JOIN corpus_v6_job_language l ON l.job_id=j.id WHERE l.language='zh' GROUP BY j.domain"));cycle=c.setting(db,'language_planner_cycle',0);added=0;profile=c.coverage_profile(db,cfg);health=c.source_health(db)
    for d in sorted(c.enabled(cfg),key=lambda d:history.get(d['domain_name'],0)):
        oldtopics=dict(db.execute("SELECT p.topic_id,count(*) FROM jobs j JOIN corpus_v4_job_policy p ON p.job_id=j.id JOIN corpus_v6_job_language l ON l.job_id=j.id WHERE j.domain=? GROUP BY p.topic_id",(d['domain_name'],)))
        for t in sorted(d['topics'],key=lambda t:oldtopics.get(t['topic_id'],0)):
            busy=db.execute("SELECT 1 FROM jobs j JOIN corpus_v4_job_policy p ON p.job_id=j.id JOIN corpus_v6_job_language l ON l.job_id=j.id WHERE j.domain=? AND p.topic_id=? AND j.status IN ('pending','retry','running') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1) LIMIT 1",(d['domain_name'],t['topic_id'])).fetchone()
            if busy:continue
            queries=t.get('language_queries',{}).get('zh') or [q for q in t.get('queries',[]) if has_han(q)]
            if not queries:continue
            providers=policy.get('zh_sources',['doaj','crossref','openalex']);source=providers[(cycle+oldtopics.get(t['topic_id'],0))%len(providers)];query=queries[cycle%len(queries)];purpose='historical' if c.historical_slot(cycle+oldtopics.get(t['topic_id'],0),cfg['continuous'].get('historical_fraction',.2)) else 'original'
            years=d.get('year_policy',{}).get('historical_years',[]) if purpose=='historical' else d['target_years']
            if not years:continue
            ordered=sorted(years,key=lambda y:-c.priority(d,t,y,source,profile,health,purpose)[0]);chosen=None
            for year in ordered:
                jid=job_id(d['domain_name'],query,source,year,language='zh');old=db.execute('SELECT status,updated FROM jobs WHERE id=?',(jid,)).fetchone()
                if old:continue
                chosen=jid,year;break
            if not chosen:continue
            jid,year=chosen;score,evidence=c.priority(d,t,year,source,profile,health,purpose);evidence.update(language_target='zh',fulltext_language_verified_after_download=True,query=query)
            db.execute("INSERT INTO jobs(id,domain,topic,source,lo,hi,sort,cursor,page,priority,status,created,updated) VALUES(?,?,?,?,?,?,'relevance',NULL,0,?,'pending',?,?)",(jid,d['domain_name'],query,source,year,year,-score,c.now(),c.now()))
            db.execute('INSERT INTO corpus_v4_job_policy VALUES(?,?,?,?,?,?,?)',(jid,t['topic_id'],'high',purpose,score,c.dump(evidence),c.now()));db.execute('INSERT INTO corpus_v2_jobs VALUES(?,?,?,?,?,?,?)',(jid,d['domain_name'],t['topic_id'],year,source,1,c.now()));db.execute('INSERT INTO corpus_v6_job_language VALUES(?,?,?,?,?)',(jid,'zh',query,c.dump(evidence),c.now()));added+=1
            break
        if added>=room:break
    c.checkpoint(db,'language_planner_cycle',cycle+1);db.commit();return added

def reserve_initial_room(db,cfg):
    """Park surplus old searches, retaining every cursor and its prior policy."""
    if not cfg.get('language_policy',{}).get('enabled'):return
    reserve=min(len(cfg['domains']),cfg['continuous']['frontier_limit']//2)
    rows=db.execute("SELECT j.id FROM jobs j JOIN corpus_v4_job_policy p ON p.job_id=j.id WHERE j.status IN ('pending','retry') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1) AND NOT EXISTS(SELECT 1 FROM corpus_v6_job_language l WHERE l.job_id=j.id) ORDER BY p.score DESC,j.page").fetchall()
    for jid, in rows[max(0,cfg['continuous']['frontier_limit']-reserve):]:db.execute('INSERT OR REPLACE INTO corpus_v5_frontier VALUES(?,1,?,?)',(jid,'reserve bounded Chinese discovery; old cursor retained',datetime.datetime.now(datetime.timezone.utc).isoformat()))
    db.commit()
