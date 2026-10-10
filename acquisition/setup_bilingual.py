"""Offline additive language rollout. Run only after collector.lock is released."""
import json,sqlite3,datetime,collections
from corpus_store import load_config,ROOT,STATE,version,csvwrite,atomic_json
from corpus_languages import init,record,export,has_han
from project_paths import resolve_project_path
import corpus_continuous as continuous
from language_queue import reserve_initial_room

assert not (STATE/'collector.lock').exists(),'Stop active collector first'
cfg=load_config();before_version=version(cfg)
cfg['language_policy']={'enabled':True,'languages':['zh','en'],'zh_search_fraction':.5,'zh_sources':['doaj','crossref','openalex'],'storage_root':'corpus_by_language','historical_storage':'hardlinks preserving original files and audit paths','language_basis':'sampled fulltext body; metadata kept separately; uncertain and other languages retained','publisher_oa_resolution':True,'request':'同时采集中文和英文论文，分语言保存，保留原文','approved_at':datetime.datetime.now(datetime.timezone.utc).isoformat()}
cfg['continuous']['api_requests_per_day']['publisher_oa']=600
for d in cfg['domains']:
    for t in d['topics']:
        queries=t.get('queries') or [t['name']]
        t['language_queries']={'zh':[q for q in queries if has_han(q)],'en':[q for q in queries if not has_han(q)]}
        assert t['language_queries']['zh'] and t['language_queries']['en'],t['topic_id']
assert version(cfg)==before_version,'Language policy must not change relevance rules'
atomic_json(STATE/'corpus_config.json',cfg)
db=sqlite3.connect(STATE/'state.sqlite3');init(db);continuous.init(db);reserve_initial_room(db,cfg);continuous.seed_frontier(db,cfg)
rows=db.execute("SELECT id,data,path,sha256 FROM papers WHERE status='downloaded' AND path IS NOT NULL").fetchall();counts=collections.Counter();errors=[]
for i,(pid,data,path,sha) in enumerate(rows):
    try:
        result=record(db,ROOT,cfg,pid,json.loads(data),resolve_project_path(path));counts[result]+=1
    except Exception as error:errors.append({'paper_id':pid,'error':type(error).__name__+': '+str(error)[:200]})
    if (i+1)%100==0:print(json.dumps({'event':'language_index','processed':i+1,'total':len(rows),'counts':dict(counts),'errors':len(errors)},ensure_ascii=False),flush=True)
for path in (STATE/'fulltext_xml').glob('*.xml'):
    row=db.execute('SELECT data FROM papers WHERE id=?',(path.stem,)).fetchone()
    if row:record(db,ROOT,cfg,path.stem,json.loads(row[0]),path,kind='xml')
export(db,ROOT)
plans=[dict(domain=d['domain_name'],topic_id=t['topic_id'],language=language,queries=queries,years=d['target_years'],sources=cfg['language_policy']['zh_sources'] if language=='zh' else d.get('search_sources'),enabled=True,dispatch='bounded frontier and persistent fair language turn') for d in cfg['domains'] for t in d['topics'] for language,queries in t['language_queries'].items()]
csvwrite(ROOT/'BILINGUAL_SEARCH_SCOPE.csv',plans)
proof={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'backup':(STATE/'BILINGUAL_BACKUP.txt').read_text().strip(),'existing_pdf_language_counts':dict(counts),'language_index_errors':errors,'topic_count':sum(len(d['topics']) for d in cfg['domains']),'bilingual_topic_config_count':len(plans),'classifier_version_preserved':before_version,'minimum_score':cfg['acceptance']['minimum_score'],'storage_stage':cfg['storage_stage'],'initial_chinese_managed_jobs':db.execute("SELECT count(*) FROM corpus_v6_job_language").fetchone()[0]}
atomic_json(STATE/'BILINGUAL_CHECK.json',proof);db.close();print(json.dumps(proof,ensure_ascii=False),flush=True)
