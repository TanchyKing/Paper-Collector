"""Read-only migration inventory: never instantiate the acquisition Library."""
import collections, datetime, hashlib, json, os, pathlib, sqlite3, sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
OUT = ROOT / 'acquisition' / 'migration_v3'

def resolve_old(value):
    p = pathlib.Path(value)
    if p.exists():
        return p
    parts = pathlib.PureWindowsPath(value).parts
    for i, part in enumerate(parts):
        if part.casefold() == 'papers':
            candidate = ROOT.joinpath(*parts[i+1:])
            if candidate.exists():
                return candidate
    return ROOT / value

def main():
    OUT.mkdir(exist_ok=True)
    inventory = []
    for folder, dirs, files in os.walk(ROOT):
        for name in files:
            p = pathlib.Path(folder) / name
            inventory.append({'path':p.relative_to(ROOT).as_posix(), 'bytes':p.stat().st_size})
    (OUT/'file_inventory.json').write_text(json.dumps(inventory,ensure_ascii=False),encoding='utf-8')
    db = sqlite3.connect((ROOT/'acquisition/state.sqlite3').as_uri()+'?mode=ro',uri=True)
    tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    result = {'root':str(ROOT),'checked_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
              'python':sys.version,'inventory_files':len(inventory),
              'integrity_check':[r[0] for r in db.execute('PRAGMA integrity_check')],
              'tables':{t:db.execute('SELECT count(*) FROM "'+t+'"').fetchone()[0] for t in tables},
              'paper_status':dict(db.execute('SELECT status,count(*) FROM papers GROUP BY status')),
              'corpus_status':dict(db.execute('SELECT primary_state,count(*) FROM corpus_v2_status GROUP BY primary_state')),
              'job_status':dict(db.execute('SELECT status,count(*) FROM jobs GROUP BY status')),
              'annual_job_status':dict(db.execute('SELECT j.status,count(*) FROM jobs j JOIN corpus_v2_jobs v ON v.job_id=j.id GROUP BY j.status'))}
    checks=[]
    for pid,value,sha,size,pages in db.execute("SELECT id,path,sha256,bytes,pages FROM papers WHERE status='downloaded'"):
        p=resolve_old(value or '')
        check={'id':pid,'stored_path':value,'resolved_path':str(p),'exists':p.is_file(),'expected_bytes':size,'expected_sha256':sha}
        if p.is_file():
            check['bytes']=p.stat().st_size
            with p.open('rb') as f:
                check['sha256']=hashlib.file_digest(f,'sha256').hexdigest()
                f.seek(0); check['header_valid']=f.read(1024).find(b'%PDF-')>=0
                f.seek(max(0,check['bytes']-2048));check['eof_valid']=b'%%EOF' in f.read()
            check['matches']=check['sha256']==sha and check['bytes']==size
        checks.append(check)
    result['pdf']={'downloaded':len(checks),'existing':sum(c['exists'] for c in checks),
                   'matching_hash_and_size':sum(c.get('matches',False) for c in checks),
                   'bytes':sum(c.get('bytes',0) for c in checks),
                   'header_and_eof':sum(c.get('header_valid',False) and c.get('eof_valid',False) for c in checks),
                   'stored_paths_need_relocation':sum(c['stored_path']!=c['resolved_path'] for c in checks)}
    # Include each original row and schema in the preservation baseline, with deterministic order.
    result['original_fingerprints']={}
    for t in tables:
        if t.startswith('corpus_v2_'):continue
        h=hashlib.sha256()
        for row in db.execute('SELECT * FROM "'+t+'" ORDER BY rowid'):
            h.update(json.dumps(row,ensure_ascii=False,default=str).encode());h.update(b'\n')
        result['original_fingerprints'][t]={'sha256':h.hexdigest(),'rows':result['tables'][t],
          'schema':db.execute('SELECT sql FROM sqlite_master WHERE name=?',(t,)).fetchone()[0]}
    db.close()
    (OUT/'pdf_preservation.json').write_text(json.dumps(checks,ensure_ascii=False),encoding='utf-8')
    (OUT/'migration_baseline.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({k:v for k,v in result.items() if k!='original_fingerprints'},ensure_ascii=False,indent=2))

if __name__=='__main__':main()
