from project_paths import resolve_project_path, pointer_path
"""Read-only preservation checks against the pre-repair SQLite snapshot."""
import hashlib,json,pathlib,sqlite3,collections
from corpus_store import ROOT,STATE,atomic_json,now
def fingerprint(rows):
    h=hashlib.sha256();count=0
    for row in rows:
        h.update(json.dumps(list(row),ensure_ascii=False,separators=(',',':')).encode());h.update(b'\n');count+=1
    return {'rows':count,'sha256':h.hexdigest()}
def main():
    backup=pointer_path(STATE/'repair_backup.txt');original=sqlite3.connect('file:'+str(backup/'state.sqlite3').replace('\\','/')+'?mode=ro',uri=True);current=sqlite3.connect('file:'+str(STATE/'state.sqlite3').replace('\\','/')+'?mode=ro',uri=True);current.execute('BEGIN')
    result={'checked_at':now(),'backup':str(backup),'integrity':current.execute('PRAGMA integrity_check').fetchone()[0],'original_tables':{},'pdfs':{},'invariants':{}}
    tables=original.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
    for name,sql in tables:
        schema=current.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0];a=fingerprint(original.execute('SELECT * FROM '+name+' ORDER BY rowid'))
        if name=='jobs':
            oldids={r[0] for r in original.execute('SELECT id FROM jobs')};b=fingerprint(row for row in current.execute('SELECT * FROM jobs ORDER BY rowid') if row[0] in oldids)
        else:b=fingerprint(current.execute('SELECT * FROM '+name+' ORDER BY rowid'))
        result['original_tables'][name]={'original':a,'current_original_rows':b,'unchanged':a==b,'schema_unchanged':schema==sql}
    pdfs=current.execute("SELECT id,path,sha256,bytes FROM papers WHERE status='downloaded'").fetchall();bad=[];total=0
    for pid,path,sha,size in pdfs:
        f=resolve_project_path(path)
        if not f.is_file():bad.append({'paper_id':pid,'issue':'missing_file'});continue
        with f.open('rb') as reader:actual=hashlib.file_digest(reader,'sha256').hexdigest()
        if actual!=sha or f.stat().st_size!=size:bad.append({'paper_id':pid,'issue':'bytes_or_hash_changed'})
        total+=f.stat().st_size
    result['pdfs']={'checked':len(pdfs),'bytes':total,'unchanged':not bad,'issues':bad}
    result['invariants']={'accepted_has_valid_identity':current.execute('SELECT count(*) FROM corpus_v2_status WHERE accepted_for_corpus=1 AND (valid_pdf!=1 OR downloaded!=1 OR relevant!=1 OR needs_review!=0 OR excluded!=0 OR duplicate_version!=0)').fetchone()[0]==0,'one_accepted_per_identity':current.execute('SELECT count(*) FROM (SELECT article_identity FROM corpus_v2_status WHERE accepted_for_corpus=1 GROUP BY article_identity HAVING count(*)>1)').fetchone()[0]==0,'annual_queues':current.execute("SELECT count(*) FROM corpus_v2_jobs c JOIN jobs j ON j.id=c.job_id WHERE j.lo!=j.hi OR j.sort!='relevance' OR j.cursor IS NOT NULL OR j.status!='pending'").fetchone()[0]==0,'validation_per_domain':dict(current.execute('SELECT domain,count(*) FROM corpus_v2_validation GROUP BY domain')),'old_http_attempts_unchanged':result['original_tables']['http_attempts']['unchanged']}
    result['passed']=all(x['unchanged'] and x['schema_unchanged'] for x in result['original_tables'].values()) and not bad and all(result['invariants'][k] for k in ['accepted_has_valid_identity','one_accepted_per_identity','annual_queues','old_http_attempts_unchanged']) and all(n>=100 for n in result['invariants']['validation_per_domain'].values()) and len(result['invariants']['validation_per_domain'])>=4
    atomic_json(STATE/'repair_v2/PRESERVATION_CHECK.json',result);print(json.dumps(result,ensure_ascii=False));current.close();original.close()
    if not result['passed']:raise SystemExit(1)
if __name__=='__main__':main()
