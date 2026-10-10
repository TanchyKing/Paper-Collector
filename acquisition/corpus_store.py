"""Additive, rollbackable corpus layer over the existing Paper Pilot SQLite state."""
from __future__ import annotations
import csv,datetime,hashlib,json,pathlib,sqlite3,os,re
ROOT=pathlib.Path(__file__).resolve().parent.parent;STATE=ROOT/'acquisition';CONFIG=STATE/'corpus_config.json'
def now():return datetime.datetime.now(datetime.timezone.utc).isoformat()
def dump(v):return json.dumps(v,ensure_ascii=False,sort_keys=True)
def load_config(path=None):
    path=path or os.environ.get('CORPUS_CONFIG') or CONFIG
    cfg=json.loads(pathlib.Path(path).read_text(encoding='utf-8'))
    # New definitions can use the documented cn/en names; legacy names remain aliases.
    for d in cfg['domains']:
        d.setdefault('domain_name',d.get('domain_name_cn') or d.get('domain_name_en'))
        if not d['domain_name']:raise ValueError('A domain needs domain_name_cn/domain_name_en or legacy domain_name.')
        d.setdefault('domain_id','domain_'+hashlib.sha256(d['domain_name'].encode()).hexdigest()[:12]);d.setdefault('domain_name_cn',d['domain_name']);d.setdefault('domain_name_en',d['domain_name']);d.setdefault('enabled',True)
        d.setdefault('parent_domain',None);d.setdefault('aliases',[]);d.setdefault('description',d['domain_name_en']);d.setdefault('positive_terms',[]);d.setdefault('negative_terms',[]);d.setdefault('context_terms',list(d['positive_terms']));d.setdefault('priority',1)
        d.setdefault('year_policy',{});d['year_policy'].setdefault('year_weights',dict(cfg['sampling'].get('year_weights',{})));d.setdefault('target_years',d['year_policy'].get('years') or list(range(datetime.datetime.now().year-7,datetime.datetime.now().year+1)))
        d.setdefault('target_depth',d.get('target_count',300));d.setdefault('target_count',d['target_depth']);d.setdefault('preferred_sources',d.get('search_sources') or cfg['sampling']['providers']);d.setdefault('search_sources',d['preferred_sources']);d.setdefault('preferred_venues',[])
        topics=[]
        for item in d.get('topics',[]):
            t={'name':item} if isinstance(item,str) else item
            t.setdefault('name',t.get('topic_id'));t.setdefault('topic_id',t['name']);t.setdefault('aliases',[]);t.setdefault('positive_terms',[re.escape(t['name'])]);t.setdefault('negative_terms',[]);t.setdefault('queries',[t['name']]);t.setdefault('priority',1);topics.append(t)
        d['topics']=topics;d.setdefault('query_terms_cn',[a for t in topics for a in t['aliases']]);d.setdefault('query_terms_en',[t['name'] for t in topics])
    names=[d['domain_name'] for d in cfg['domains']]
    if len(names)!=len(set(names)) or not names:raise ValueError('Domain names must be nonempty and unique.')
    ids=[d['domain_id'] for d in cfg['domains']]
    if len(ids)!=len(set(ids)):raise ValueError('Domain IDs must be unique.')
    if cfg['sampling']['sort']!='relevance':raise ValueError('Annual queues use relevance, not combined-year newest sorting.')
    return cfg
def version(cfg):
    definitions=[{k:v for k,v in d.items() if k not in ['oa_resolvers']} for d in cfg['domains']]
    if cfg['classifier_version'].startswith('corpus-v3'):
        keys=['domain_name','description','positive_terms','weak_terms','negative_terms','negative_exceptions','context_terms','boundary_terms']
        definitions=[dict({k:d.get(k) for k in keys},topics=[{k:t.get(k) for k in ['topic_id','name','aliases','positive_terms','negative_terms']} for t in d['topics']]) for d in cfg['domains']]
    return cfg['classifier_version']+':'+hashlib.sha256(dump(definitions).encode()).hexdigest()[:12]
UP='''
CREATE TABLE IF NOT EXISTS corpus_v2_migrations(version INTEGER PRIMARY KEY,applied TEXT,backup_path TEXT,schema_hash TEXT);
CREATE TABLE IF NOT EXISTS corpus_v2_runs(run_id TEXT PRIMARY KEY,started TEXT,finished TEXT,status TEXT,config_hash TEXT,summary TEXT);
CREATE TABLE IF NOT EXISTS corpus_v2_metadata(paper_id TEXT PRIMARY KEY,normalized TEXT NOT NULL,run_id TEXT,updated TEXT);
CREATE INDEX IF NOT EXISTS corpus_v2_meta_doi ON corpus_v2_metadata(json_extract(normalized,'$.doi'));
CREATE INDEX IF NOT EXISTS corpus_v2_meta_external_formal_doi ON corpus_v2_metadata(json_extract(normalized,'$.external_formal_doi'));
CREATE INDEX IF NOT EXISTS corpus_v2_meta_arxiv ON corpus_v2_metadata(json_extract(normalized,'$.arxiv_id'));
CREATE INDEX IF NOT EXISTS corpus_v2_meta_title ON corpus_v2_metadata(json_extract(normalized,'$.normalized_title'));
CREATE INDEX IF NOT EXISTS corpus_v2_meta_openalex ON corpus_v2_metadata(json_extract(normalized,'$.openalex_work_id'));
CREATE INDEX IF NOT EXISTS corpus_v2_meta_pmid ON corpus_v2_metadata(json_extract(normalized,'$.pmid'));
CREATE INDEX IF NOT EXISTS corpus_v2_meta_pmcid ON corpus_v2_metadata(json_extract(normalized,'$.pmcid'));
CREATE TABLE IF NOT EXISTS corpus_v2_candidates(candidate_id TEXT PRIMARY KEY,paper_id TEXT,field TEXT,value TEXT,source TEXT,evidence TEXT,confidence REAL,verified INTEGER,created TEXT);
CREATE INDEX IF NOT EXISTS corpus_v2_candidate_field ON corpus_v2_candidates(paper_id,field);
CREATE TABLE IF NOT EXISTS corpus_v2_relevance(paper_id TEXT,domain TEXT,classifier_version TEXT,relevance_score REAL,relevance_label TEXT,relevance_evidence TEXT,matched_topics TEXT,excluded_reason TEXT,method TEXT,run_id TEXT,updated TEXT,PRIMARY KEY(paper_id,domain,classifier_version));
CREATE INDEX IF NOT EXISTS corpus_v2_label ON corpus_v2_relevance(domain,relevance_label);
CREATE TABLE IF NOT EXISTS corpus_v2_status(paper_id TEXT PRIMARY KEY,article_identity TEXT,representative_id TEXT,downloaded INTEGER,valid_pdf INTEGER,relevant INTEGER,accepted_for_corpus INTEGER,duplicate_version INTEGER,metadata_only INTEGER,needs_review INTEGER,excluded INTEGER,exclusion_reason TEXT,domains TEXT,topics TEXT,primary_state TEXT,run_id TEXT,updated TEXT);
CREATE INDEX IF NOT EXISTS corpus_v2_accept ON corpus_v2_status(accepted_for_corpus,article_identity);
CREATE INDEX IF NOT EXISTS corpus_v2_identity_files ON corpus_v2_status(article_identity,downloaded,valid_pdf);
CREATE INDEX IF NOT EXISTS corpus_v2_meta_year ON corpus_v2_metadata(json_extract(normalized,'$.canonical_publication_year'));
CREATE TABLE IF NOT EXISTS corpus_v2_identity(article_identity TEXT PRIMARY KEY,title TEXT,members TEXT,doi_aliases TEXT,arxiv_aliases TEXT,evidence TEXT,confidence REAL,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v2_versions(edge_id TEXT PRIMARY KEY,paper_id TEXT,related_paper_id TEXT,article_identity TEXT,preprint_doi TEXT,formal_doi TEXT,from_version TEXT,to_version TEXT,confidence REAL,evidence TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v2_venues(venue_id TEXT PRIMARY KEY,canonical_name TEXT,venue_type TEXT,issns TEXT,aliases TEXT,publishers TEXT,evidence TEXT,updated TEXT);
CREATE TABLE IF NOT EXISTS corpus_v2_jobs(job_id TEXT PRIMARY KEY,domain TEXT,topic TEXT,year INTEGER,source TEXT,created_by_v2 INTEGER,created TEXT);
CREATE TABLE IF NOT EXISTS corpus_v2_semantic_queue(paper_id TEXT,domain TEXT,classifier_version TEXT,priority REAL,status TEXT,result TEXT,updated TEXT,PRIMARY KEY(paper_id,domain,classifier_version));
CREATE TABLE IF NOT EXISTS corpus_v2_validation(run_id TEXT,paper_id TEXT,domain TEXT,strata TEXT,pages TEXT,checks TEXT,verdict TEXT,updated TEXT,PRIMARY KEY(run_id,paper_id,domain));
'''
TABLES=['corpus_v2_migrations','corpus_v2_runs','corpus_v2_metadata','corpus_v2_candidates','corpus_v2_relevance','corpus_v2_status','corpus_v2_identity','corpus_v2_versions','corpus_v2_venues','corpus_v2_jobs','corpus_v2_semantic_queue','corpus_v2_validation']
def migrate(db,backup_path=''):
    db.executescript(UP)
    checksum=hashlib.sha256(UP.encode()).hexdigest()
    if not db.execute('SELECT 1 FROM corpus_v2_migrations WHERE schema_hash=?',(checksum,)).fetchone():
        latest=db.execute('SELECT max(version) FROM corpus_v2_migrations').fetchone()[0];db.execute('INSERT INTO corpus_v2_migrations VALUES(?,?,?,?)',(max(2,(latest or 1)+1),now(),backup_path,checksum))
    db.commit()
def rollback(db,archive_path):
    """Archive all derived data first; never delete original rows, PDFs or progressed jobs."""
    destination=sqlite3.connect(archive_path);db.backup(destination);destination.close()
    db.execute('BEGIN IMMEDIATE')
    for name in reversed(TABLES):db.execute('DROP TABLE IF EXISTS '+name)
    db.commit()
def add_candidate(db,pid,field,value,source,evidence,confidence,verified=True):
    if value in [None,'',[],{}]:return
    key=hashlib.sha256(dump([pid,field,value,source,evidence]).encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO corpus_v2_candidates VALUES(?,?,?,?,?,?,?,?,?)',(key,pid,field,dump(value),source,dump(evidence),confidence,int(verified),now()))
def put_metadata(db,pid,data,run_id):
    db.execute('INSERT INTO corpus_v2_metadata VALUES(?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET normalized=excluded.normalized,run_id=excluded.run_id,updated=excluded.updated',(pid,dump(data),run_id,now()))
def put_relevance(db,pid,d,result,cfg,run_id):
    v=result.get('classifier_version') or version(cfg)
    db.execute('INSERT INTO corpus_v2_relevance VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id,domain,classifier_version) DO UPDATE SET relevance_score=excluded.relevance_score,relevance_label=excluded.relevance_label,relevance_evidence=excluded.relevance_evidence,matched_topics=excluded.matched_topics,excluded_reason=excluded.excluded_reason,method=excluded.method,run_id=excluded.run_id,updated=excluded.updated',(pid,d,v,result['relevance_score'],result['relevance_label'],dump(result['relevance_evidence']),dump(result['matched_topics']),result.get('excluded_reason',''),result['method'],run_id,now()))
    if result['relevance_label']=='uncertain':db.execute("INSERT OR IGNORE INTO corpus_v2_semantic_queue VALUES(?,?,?,?,?,?,?)",(pid,d,v,result.get('priority',1),'pending',None,now()))
def csvwrite(path,rows,fields=None):
    path=pathlib.Path(path);path.parent.mkdir(parents=True,exist_ok=True)
    if fields is None:fields=list(dict.fromkeys(k for row in rows for k in row))
    temporary=path.with_name(path.name+'.tmp')
    with temporary.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader()
        for row in rows:w.writerow({k:dump(v) if isinstance(v,(dict,list)) else v for k,v in row.items()})
    temporary.replace(path)
def atomic_json(path,value):
    path=pathlib.Path(path);tmp=path.with_name(path.name+'.tmp');tmp.write_text(dump(value),encoding='utf-8');tmp.replace(path)
