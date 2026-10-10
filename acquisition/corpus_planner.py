"""Annual independent queries, using the existing jobs/cursor mechanism."""
import hashlib,json,time
from collections import Counter
from corpus_store import load_config,dump,now
def job_id(d,t,s,y,cursor=None,language=None):
    # Exactly the original job key convention: annual rows cannot collide with range rows.
    values=[d,t,s,y,y,'relevance',cursor]
    if language:values.append('language:'+language)
    return hashlib.sha256(json.dumps(values,ensure_ascii=False).encode()).hexdigest()[:24]
def seed_annual_jobs(db,cfg=None):
    cfg=cfg or load_config();inserted=0
    if cfg.get('continuous',{}).get('enabled'):
        from corpus_continuous import seed_frontier
        return seed_frontier(db,cfg)
    for di,d in enumerate(cfg['domains']):
        for ti,topic in enumerate(d['topics']):
            for year in d['target_years']:
                for query in topic.get('queries') or [topic['name']]:
                    available=cfg['sampling']['providers'] if query==topic['name'] else cfg['sampling']['multilingual_providers']
                    providers=[s for s in d.get('search_sources',available) if s in available]
                    for si,source in enumerate(providers):
                        jid=job_id(d['domain_name'],query,source,year);exists=db.execute('SELECT 1 FROM jobs WHERE id=?',(jid,)).fetchone()
                        if not exists:
                            db.execute("INSERT INTO jobs(id,domain,topic,source,lo,hi,sort,cursor,page,priority,status,created,updated) VALUES(?,?,?,?,?,?,'relevance',NULL,0,?,'pending',?,?)",(jid,d['domain_name'],query,source,year,year,ti*100+si*10+di,now(),now()));inserted+=1
                        db.execute('INSERT OR IGNORE INTO corpus_v2_jobs VALUES(?,?,?,?,?,?,?)',(jid,d['domain_name'],topic['topic_id'],year,source,int(not exists),now()))
    db.commit();return inserted
def coverage_counts(db):
    domain_year=Counter();topic_year=Counter();global_year=Counter()
    for year,domains,topics in db.execute("SELECT json_extract(m.normalized,'$.canonical_publication_year'),s.domains,s.topics FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id WHERE s.accepted_for_corpus=1"):
        if year is None:continue
        global_year[int(year)]+=1
        for d in json.loads(domains):
            domain_year[(d,int(year))]+=1
            for t in json.loads(topics).get(d,[]):topic_year[(d,t,int(year))]+=1
    return domain_year,topic_year,global_year
def score_job(job,cfg,counts):
    ds,ts,gs=counts;d=next(d for d in cfg['domains'] if d['domain_name']==job['domain']);year=int(job['lo']);weight=float(cfg['sampling']['year_weights'].get(str(year),1))
    topic=next((t for t in d['topics'] if job['topic'] in [t['name'],*t.get('aliases',[]),*t.get('queries',[])]),None)
    target=cfg['sampling']['minimum_by_domain_year'].get(d['domain_name'],{}).get(str(year));globalmin=cfg['sampling']['minimum_by_year'].get(str(year))
    deficit=max(0,float(target)-ds[(d['domain_name'],year)]) if target is not None else 0
    globaldef=max(0,float(globalmin)-gs[year]) if globalmin is not None else 0
    scarcity=weight/(1+ds[(d['domain_name'],year)])
    theme_count=ts[(d['domain_name'],topic['topic_id'],year)] if topic else 0
    theme_global=sum(value for (name,t,y),value in ts.items() if name==d['domain_name'] and topic and t==topic['topic_id']);theme_target=float(topic.get('target_count') or 1) if topic else 1;theme_deficit=max(0,theme_target-theme_global);theme_priority=max(.01,float(topic.get('priority',1) if topic else 1))
    return (-globaldef,-deficit,-scarcity*max(.01,float(d['priority'])),-theme_deficit*theme_priority,theme_global/theme_target/theme_priority,theme_count/theme_priority,int(job['page']),float(job['priority']))
def annual_jobs(db,cfg=None):
    cfg=cfg or load_config();domains={d['domain_name']:d for d in cfg['domains']};cols=['id','domain','topic','source','lo','hi','sort','cursor','page','priority','attempts']
    if cfg.get('continuous',{}).get('enabled'):
        from corpus_continuous import jobs
        return jobs(db,cfg)
    batch_topics=cfg.get('batch',{}).get('topics_by_domain',{})
    allowed=cfg.get('batch',{}).get('domains_allowed',list(domains))
    jobs=[dict(zip(cols,r)) for r in db.execute("SELECT id,domain,topic,source,lo,hi,sort,cursor,page,priority,attempts FROM jobs WHERE status IN ('pending','retry') AND next_retry<=? AND lo=hi AND sort='relevance'",(time.time(),))]
    queries={d['domain_name']:{q:cfg['sampling']['providers'] if q==t['name'] else cfg['sampling']['multilingual_providers'] for t in d['topics'] if d['domain_name'] not in batch_topics or t['topic_id'] in batch_topics[d['domain_name']] for q in [t['name'],*t.get('queries',[]),*t.get('aliases',[])]} for d in cfg['domains']}
    return [j for j in jobs if j['domain'] in domains and j['domain'] in allowed and j['lo'] in domains[j['domain']]['target_years'] and j['source'] in domains[j['domain']].get('search_sources',cfg['sampling']['providers']) and j['source'] in cfg['sampling']['providers'] and j['source'] in queries[j['domain']].get(j['topic'],[])]
def preview(db,cfg=None,limit=100):
    cfg=cfg or load_config();counts=coverage_counts(db)
    if cfg.get('continuous',{}).get('enabled'):
        from corpus_continuous import jobs
        return jobs(db,cfg)[:limit]
    return [dict(j,planning_score=score_job(j,cfg,counts),observed_accepted_for_domain_year=counts[0][(j['domain'],j['lo'])],year_weight=cfg['sampling']['year_weights'].get(str(j['lo']),1)) for j in sorted(annual_jobs(db,cfg),key=lambda j:score_job(j,cfg,counts))[:limit]]
