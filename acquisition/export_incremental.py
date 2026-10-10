"""Create a consistent, append-only corpus transfer package; never connect to a server."""
import argparse,hashlib,json,pathlib,shutil,sqlite3,datetime
from project_paths import ROOT,STATE,resolve_project_path

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--full',action='store_true');parser.add_argument('--since-manifest',type=pathlib.Path);args=parser.parse_args()
    if (STATE/'collector.lock').exists() or (STATE/'repair_v3/repair.lock').exists() or (STATE/'repair_v2/repair.lock').exists():raise SystemExit('Stop acquisition/repair before taking a reproducible transfer snapshot.')
    baseline=args.since_manifest or STATE/'migration_v3/pdf_preservation.json'
    known=set()
    if not args.full and baseline.exists():
        raw=json.loads(baseline.read_text(encoding='utf8'));items=raw if isinstance(raw,list) else raw.get('files',[])
        known={x['sha256'] for x in items if x.get('sha256')}
    batch=datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out=ROOT/'sync_exports'/batch;out.mkdir(parents=True)
    db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True);snapshot=out/'acquisition/state.sqlite3';snapshot.parent.mkdir()
    target=sqlite3.connect(snapshot);db.backup(target);target.close()
    transfers=[];all_files=[]
    def add(path,relative=None):
        relative=relative or path.relative_to(ROOT);destination=out/relative;destination.parent.mkdir(parents=True,exist_ok=True)
        if not destination.exists():shutil.copy2(path,destination)
        with destination.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
        transfers.append({'path':relative.as_posix(),'bytes':destination.stat().st_size,'sha256':sha})
    for pid,value,sha,size in db.execute("SELECT id,path,sha256,bytes FROM papers WHERE status='downloaded'"):
        path=resolve_project_path(value);all_files.append({'paper_id':pid,'sha256':sha,'path':path.relative_to(ROOT).as_posix(),'bytes':size})
        if sha in known:continue
        add(path)
        metadata=STATE/'metadata'/(pid+'.json')
        if metadata.exists():add(metadata)
        row=db.execute('SELECT normalized FROM corpus_v2_metadata WHERE paper_id=?',(pid,)).fetchone()
        if row:
            destination=out/'acquisition/normalized_metadata'/(pid+'.json');destination.parent.mkdir(exist_ok=True);destination.write_text(row[0],encoding='utf8');add(destination,destination.relative_to(out))
    for folder in ['fulltext_xml','repair_v3']:
        for path in (STATE/folder).rglob('*'):
            if path.is_file() and path.suffix in ['.json','.xml','.csv','.jsonl']:add(path)
    for name in ['manifest.csv','MIGRATION_VERIFY_REPORT.md','LITERATURE_AUDIT_V3.md','NEXT_ACQUISITION_PLAN_V3.csv','NEXT_RUN_GUIDE.md','CORPUS_RECONCILIATION_V3.csv']:
        if (ROOT/name).exists():add(ROOT/name)
    for path in STATE.glob('*.py'):add(path)
    for path in STATE.glob('*.ps1'):add(path)
    for name in ['run_state.json','phase1_policy.json','audit/LATEST.txt','repair_backup.txt','migration_v3/pdf_preservation.json','migration_v3/migration_baseline.json','migration_v3/SMOKE_VERIFY.json','migration_v3/BATCH_VERIFY.json','migration_v3/repair_effects.json']:
        if (STATE/name).exists():add(STATE/name)
    # These are the existing normalizer's required evidence caches, not the historical SQLite snapshots.
    for path in (STATE/'audit').rglob('*'):
        if path.is_file() and path.suffix in ['.json','.jsonl','.txt']:add(path)
    for name in ['corpus_config.json','requirements-migration.lock','smoke_v3.json']:
        if (STATE/name).exists():add(STATE/name)
    db.close()
    with snapshot.open('rb') as f:sha=hashlib.file_digest(f,'sha256').hexdigest()
    transfers.append({'path':'acquisition/state.sqlite3','bytes':snapshot.stat().st_size,'sha256':sha,'method':'SQLite backup API'})
    (out/'TRANSFER_MANIFEST.json').write_text(json.dumps({'batch':batch,'scope':'full' if args.full else 'incremental','baseline':str(baseline.relative_to(ROOT)) if baseline.is_relative_to(ROOT) else str(baseline),'files':transfers},ensure_ascii=False,indent=2),encoding='utf8')
    (out/'CORPUS_FILE_MANIFEST.json').write_text(json.dumps({'files':all_files},ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps({'package':str(out),'new_pdf_files':sum(x['path'].endswith('.pdf') for x in transfers),'files':len(transfers),'remote_upload_started':False},ensure_ascii=False))

if __name__=='__main__':main()
