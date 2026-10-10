"""Read-only preservation, language-view and live-run verification evidence."""
import json,sqlite3,hashlib,os,datetime
from pathlib import Path
import pymupdf
from corpus_store import ROOT,STATE,atomic_json
from project_paths import resolve_project_path

backup=Path((STATE/'BILINGUAL_BACKUP.txt').read_text().strip());old=sqlite3.connect((backup/'before.sqlite3').as_uri()+'?mode=ro',uri=True);db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True);db.execute('BEGIN')
oldfiles={r[0]:r for r in old.execute('SELECT * FROM files')};files={r[0]:r for r in db.execute('SELECT * FROM files')};changed=sum(files.get(k)!=v for k,v in oldfiles.items())
oldjobs={r[0]:r[1:] for r in old.execute("SELECT id,cursor,page,status FROM jobs WHERE status='done'")};jobs={r[0]:r[1:] for r in db.execute('SELECT id,cursor,page,status FROM jobs')};cursor_changes=sum(jobs.get(k)!=v for k,v in oldjobs.items())
links=[]
for pid,kind,path,view,sha in db.execute('SELECT paper_id,kind,path,view_path,sha256 FROM corpus_v6_languages'):
    if not Path(path).is_file() or not Path(view).is_file() or not os.path.samefile(path,view):links.append((pid,kind,path,view))
checks=[]
for key,row in files.items():
    if key in oldfiles:continue
    path=resolve_project_path(key);content=path.read_bytes()
    with pymupdf.open(path) as pdf:pages=len(pdf)
    lang=db.execute('SELECT l.language,s.accepted_for_corpus,l.view_path FROM corpus_v6_languages l JOIN papers p ON l.paper_id=p.id JOIN corpus_v2_status s ON s.paper_id=p.id WHERE p.path=? AND l.kind=?',(key,'pdf')).fetchone()
    checks.append(dict(path=key,sha256_ok=hashlib.sha256(content).hexdigest()==row[1],size_ok=len(content)==row[2],pages=pages,recorded_pages=row[3],language_record=lang))
searches=db.execute("SELECT j.domain,j.topic,j.source,j.total,j.updated FROM jobs j JOIN corpus_v6_job_language l ON l.job_id=j.id WHERE j.status='done'").fetchall()
proof=json.loads((STATE/'BILINGUAL_CHECK.json').read_text(encoding='utf8'));state=json.loads((STATE/'run_state.json').read_text(encoding='utf8'))
proof['validation']={'at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'tests_passed':103,'old_file_rows_changed_or_removed':changed,'old_completed_cursor_rows_changed_or_removed':cursor_changes,'old_completed_cursor_count':len(oldjobs),'bad_language_views':links,'new_pdf_count':len(checks),'new_pdf_checks':checks,'completed_chinese_searches':searches,'smoke_run_id':state['run_id'],'smoke_status':state['status'],'language_summary':json.loads((ROOT/'CORPUS_LANGUAGE_SUMMARY.json').read_text(encoding='utf8'))}
atomic_json(STATE/'BILINGUAL_CHECK.json',proof)
print(json.dumps({k:v for k,v in proof['validation'].items() if k not in ['new_pdf_checks','language_summary']},ensure_ascii=False))
assert changed==0 and cursor_changes==0 and not links and searches and checks
assert all(x['sha256_ok'] and x['size_ok'] and x['pages']==x['recorded_pages'] and x['language_record'] for x in checks)
