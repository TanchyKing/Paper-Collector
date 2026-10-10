"""Preservation plus live smoke/resume evidence from the current expansion backup."""
import sqlite3,pathlib,json,hashlib,collections
from corpus_store import STATE,ROOT,load_config,atomic_json,version,dump,now
from project_paths import resolve_project_path
from collect import validate
from fulltext_xml import verify_jats

def verify(final=False):
    if (STATE/'collector.lock').exists():raise RuntimeError('Stop collector before verification')
    folder=pathlib.Path((STATE/'ALL_DOMAINS_BACKUP.txt').read_text(encoding='utf8'));db=sqlite3.connect(STATE/'state.sqlite3');before=sqlite3.connect(folder/'before.sqlite3')
    baseline=json.loads((folder/'PDF_BASELINE.json').read_text(encoding='utf8'));bad=[]
    for path,sha,size in baseline:
        file=resolve_project_path(path)
        with file.open('rb') as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
        if actual!=sha or file.stat().st_size!=size:bad.append(str(file))
    cfg=load_config();oldcfg=json.loads((folder/'corpus_config.json').read_text(encoding='utf8'));schemas=dict(db.execute("select name,sql from sqlite_master where type='table'"));oldschemas=dict(before.execute("select name,sql from sqlite_master where type='table'"))
    original_jobs={r[0]:r[1:] for r in before.execute('select id,cursor,page from jobs')};current_jobs={r[0]:r[1:] for r in db.execute('select id,cursor,page from jobs')}
    preserved_jobs=all(current_jobs.get(k)==v for k,v in original_jobs.items())
    oldpdf={r[1] for r in baseline};new=[]
    for pid,data,path,sha,size in db.execute("select id,data,path,sha256,bytes from papers where status='downloaded'"):
        if sha in oldpdf:continue
        file=resolve_project_path(path);payload=file.read_bytes();info=validate(payload,json.loads(data));new.append(dict(paper_id=pid,sha256_matches=hashlib.sha256(payload).hexdigest()==sha,size_matches=len(payload)==size,validation=info,status=db.execute('select primary_state from corpus_v2_status where paper_id=?',(pid,)).fetchone()[0]))
    xml=[]
    for file in (STATE/'fulltext_xml').glob('*.xml'):
        side=json.loads(file.with_suffix('.json').read_text(encoding='utf8'));row=db.execute('select p.data,m.normalized from papers p join corpus_v2_metadata m on p.id=m.paper_id where p.id=?',(file.stem,)).fetchone();paper=json.loads(row[0]);meta=json.loads(row[1]);paper['formal_doi']=meta.get('formal_doi');verify_jats(file.read_text(encoding='utf8'),paper);xml.append(hashlib.sha256(file.read_bytes()).hexdigest()==side['sha256'])
    oldrelevance=before.execute('select count(*) from corpus_v2_relevance').fetchone()[0];db.execute('attach database ? as original',(str(folder/'before.sqlite3'),))
    deleted_evidence=db.execute('select count(*) from (select * from original.corpus_v2_relevance except select * from corpus_v2_relevance)').fetchone()[0]
    removed_versions=db.execute('select count(*) from (select * from original.corpus_v2_versions except select * from corpus_v2_versions)').fetchone()[0]
    removed_files=db.execute('select count(*) from (select * from original.files except select * from files)').fetchone()[0]
    raw_unchanged=db.execute('select count(*) from original.papers o join papers p on p.id=o.id where p.data!=o.data').fetchone()[0]
    duplicate=db.execute('select article_identity,count(*) from corpus_v2_status where accepted_for_corpus=1 group by article_identity having count(*)>1').fetchall()
    inflight={table:db.execute('select count(*) from '+table+' where status=?',(status,)).fetchone()[0] for table,status in [('jobs','running'),('papers','downloading'),('download_locations','inflight'),('resolution_queue','inflight')]}
    runs=[dict(run_id=i,status=s,parameters=json.loads(p),summary=json.loads(m) if m else None) for i,s,p,m in db.execute('select run_id,status,parameters,summary from engine_runs where started>? order by started',(cfg['approval_expansion_at'],))]
    events=dict(db.execute('select kind,count(*) from events where time>? group by kind',(cfg['approval_expansion_at'],)))
    result=dict(at=now(),config_hash=version(cfg),old_pdfs=len(baseline),preserved_pdf_hashes=not bad,bad_pdfs=bad,historical_schema_preserved=all(schemas.get(k)==v for k,v in oldschemas.items()),original_domain_definitions_preserved=cfg['domains'][:len(oldcfg['domains'])]==oldcfg['domains'],old_job_cursors_preserved=preserved_jobs,old_jobs=len(original_jobs),old_relevance_rows=oldrelevance,deleted_or_changed_old_relevance=deleted_evidence,removed_version_rows=removed_versions,removed_file_rows=removed_files,raw_metadata_changed_since_backup=raw_unchanged,new_pdf_count=len(new),new_pdf_accepted=sum(x['status']=='accepted_for_corpus' for x in new),new_pdf_validation=new,xml_count=len(xml),all_xml_verified=all(xml),integrity=[r[0] for r in db.execute('pragma integrity_check')],duplicate_accepted_identities=duplicate,inflight=inflight,runs=runs,events=events)
    checks=[not bad,result['historical_schema_preserved'],result['original_domain_definitions_preserved'],preserved_jobs,not deleted_evidence,not removed_versions,not removed_files,all(xml),result['integrity']==['ok'],not duplicate,not any(inflight.values()),all(x['sha256_matches'] and x['size_matches'] for x in new)]
    if final:
        first=json.loads((folder/'FIRST_STOP_CHECKPOINT.json').read_text(encoding='utf8'));firstjobs=first['jobs'];result['first_stop_completed_jobs_preserved']=all(db.execute('select cursor,page,status from jobs where id=?',(j[0],)).fetchone()==tuple(j[1:]) for j in firstjobs if j[3]=='done')
        checks += [len(runs)>=2,len(new)>0,events.get('search_ok',0)>0,result['first_stop_completed_jobs_preserved'],all(r['status']=='stopped_checkpoint_saved' for r in runs)]
    result['passed']=all(checks);atomic_json(folder/('SMOKE_RESUME_CHECK.json' if final else 'PRESERVATION_CHECK.json'),result)
    if final:atomic_json(STATE/'CONTINUOUS_SMOKE_CHECK.json',{k:v for k,v in result.items() if k!='new_pdf_validation'})
    print(dump({k:v for k,v in result.items() if k not in ['runs','new_pdf_validation']}),flush=True);assert result['passed']
    return result

if __name__=='__main__':verify('--final' in __import__('sys').argv)
