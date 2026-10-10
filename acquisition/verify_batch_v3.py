"""Read-only preservation and batch gate. Run after the collector saves its checkpoint."""
import argparse,hashlib,json,pathlib,sqlite3,sys,collections
from collect import validate
from corpus_store import ROOT,STATE,atomic_json,now,load_config
from project_paths import resolve_project_path
from fulltext_xml import verify_jats

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--min-new',type=int,default=50);parser.add_argument('--min-per-domain',type=int,default=5);args=parser.parse_args()
    if any((STATE/p).exists() for p in ['collector.lock','repair_v2/repair.lock','repair_v3/repair.lock']):raise SystemExit('Wait for collector/repair shutdown before final batch verification.')
    baseline=json.loads((STATE/'migration_v3/pdf_preservation.json').read_text(encoding='utf8'));known={x['id']:x for x in baseline}
    db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True);db.execute('BEGIN');issues=[];new=[];preserved=0
    for pid,data,value,sha,size in db.execute("SELECT id,data,path,sha256,bytes FROM papers WHERE status='downloaded'"):
        p=resolve_project_path(value)
        if not p.is_file():issues.append({'paper_id':pid,'error':'missing_file'});continue
        with p.open('rb') as f:actual=hashlib.file_digest(f,'sha256').hexdigest()
        if actual!=sha or p.stat().st_size!=size:issues.append({'paper_id':pid,'error':'stored_hash_or_size_mismatch'})
        if pid in known:
            if sha!=known[pid]['expected_sha256'] or size!=known[pid]['expected_bytes']:issues.append({'paper_id':pid,'error':'original_pdf_changed'})
            else:preserved+=1
        else:
            try:info=validate(p.read_bytes(),json.loads(data));new.append({'paper_id':pid,'valid_pdf':True,**info})
            except Exception as e:issues.append({'paper_id':pid,'error':'new_pdf_invalid:'+str(e)})
    accepted_new=sum(db.execute('SELECT accepted_for_corpus FROM corpus_v2_status WHERE paper_id=?',(x['paper_id'],)).fetchone()[0] for x in new)
    domain_counts=collections.Counter()
    for item in new:
        row=db.execute('SELECT domains,accepted_for_corpus FROM corpus_v2_status WHERE paper_id=?',(item['paper_id'],)).fetchone()
        if row and row[1]:domain_counts.update(json.loads(row[0]))
    unique_accepted=db.execute('SELECT count(*) FROM (SELECT article_identity FROM corpus_v2_status WHERE accepted_for_corpus=1 GROUP BY article_identity HAVING count(*)>1)').fetchone()[0]==0
    idle=db.execute("SELECT count(*) FROM papers WHERE status='downloading'").fetchone()[0]==0 and db.execute("SELECT count(*) FROM jobs WHERE status='running'").fetchone()[0]==0
    result={'checked_at':now(),'integrity':db.execute('PRAGMA integrity_check').fetchone()[0],'original_pdfs_preserved':preserved,'expected_original_pdfs':len(known),'new_valid_pdfs':len(new),'new_accepted':accepted_new,'new_accepted_ratio':accepted_new/max(1,len(new)),'accepted_unique_per_work':unique_accepted,'checkpoint_idle':idle,'issues':issues,'new_pdf_details':new,'minimum_smoke_pdfs':args.min_new}
    result['new_accepted_by_domain']=dict(domain_counts)
    xml_checked=0
    for path in (STATE/'fulltext_xml').glob('*.xml'):
        try:
            record=json.loads(path.with_suffix('.json').read_text(encoding='utf8'));content=path.read_bytes()
            if hashlib.sha256(content).hexdigest()!=record['sha256']:raise ValueError('xml_hash_mismatch')
            row=db.execute('SELECT p.data,m.normalized FROM papers p JOIN corpus_v2_metadata m ON m.paper_id=p.id WHERE p.id=?',(path.stem,)).fetchone()
            p,n=map(json.loads,row);p['formal_doi']=n.get('formal_doi');verify_jats(content.decode('utf8'),p);xml_checked+=1
        except Exception as error:issues.append({'paper_id':path.stem,'error':type(error).__name__+':'+str(error)})
    result['jats_xml_verified']=xml_checked
    result['domain_coverage_passed']=all(domain_counts[d['domain_name']]>=args.min_per_domain for d in load_config()['domains'])
    result['preservation_passed']=result['integrity']=='ok' and preserved==len(known) and not issues and unique_accepted and idle
    result['smoke_passed']=result['preservation_passed'] and result['domain_coverage_passed'] and len(new)>=args.min_new and accepted_new/max(1,len(new))>=.85
    atomic_json(STATE/'migration_v3/BATCH_VERIFY.json',result);print(json.dumps({k:v for k,v in result.items() if k not in ['new_pdf_details','issues']},ensure_ascii=False))
    if not result['smoke_passed']:raise SystemExit(2)

if __name__=='__main__':main()
