"""Add evidenced approved labels; archive derived status, retain raw records."""
import sqlite3,json,collections
from corpus_store import STATE,ROOT,load_config,put_relevance,dump,now,csvwrite,atomic_json,version
from corpus_rules import Classifier,hits
from corpus_repair import load_pdf_cache,acceptance
import corpus_continuous as c

def main():
    if (STATE/'collector.lock').exists():raise RuntimeError('Writer active')
    db=sqlite3.connect(STATE/'state.sqlite3');cfg=load_config();classifier=Classifier(cfg);pdfs=load_pdf_cache();backup=__import__('pathlib').Path((STATE/'ALL_DOMAINS_BACKUP.txt').read_text(encoding='utf8'))
    original=json.loads((backup/'corpus_config.json').read_text(encoding='utf8'));oldnames={d['domain_name'] for d in original['domains']};new=[d for d in cfg['domains'] if d['domain_name'] not in oldnames]
    db.execute('CREATE TABLE IF NOT EXISTS corpus_v5_status_history(paper_id TEXT,version TEXT,status_record TEXT,recorded TEXT,PRIMARY KEY(paper_id,version))')
    run='approved-expansion-'+version(cfg);counts=collections.Counter();examples=collections.defaultdict(list)
    columns=[r[1] for r in db.execute('pragma table_info(corpus_v2_status)')]
    for pid,p,n,s in c.records(db):
        results={};text=(p.get('title') or '')+' '+(n.get('abstract') or '')+' '+' '.join(n.get('keywords') or [])
        for d in new:
            if not hits(d['positive_terms'],text):continue
            r=classifier.classify(p,d['domain_name'],n);put_relevance(db,pid,d['domain_name'],r,cfg,run)
            if classifier.accepts(r):results[d['domain_name']]=r
        if not results:continue
        q=pdfs.get(pid) if s['downloaded'] else None
        e=acceptance(p,n,q,results,cfg)
        if not e['domains']:continue
        row=db.execute('select * from corpus_v2_status where paper_id=?',(pid,)).fetchone();old=dict(zip(columns,row));db.execute('insert or ignore into corpus_v5_status_history values(?,?,?,?)',(pid,version(cfg),dump(old),now()))
        domains=list(dict.fromkeys(s['domains']+e['domains']));topics=dict(s['topics'])
        for d in e['domains']:
            topics[d]=list(dict.fromkeys(topics.get(d,[])+e['topics'][d]));counts[d]+=1
            if len(examples[d])<8:examples[d].append(dict(paper_id=pid,title=p.get('title'),topics=e['topics'][d],source=p.get('source'),downloaded=s['downloaded'],rule_evidence=results[d]['relevance_evidence']))
        eligible=bool(e['eligible'] and not old['needs_review']);other=db.execute('select 1 from corpus_v2_status where article_identity=? and accepted_for_corpus=1 and paper_id!=?',(old['article_identity'],pid)).fetchone()
        accepted=bool(old['accepted_for_corpus'] or eligible and not other);review=bool(old['needs_review'] or e['needs_review']);excluded=bool(e['excluded'] and not s['domains']);primary='accepted_for_corpus' if accepted else 'metadata_only' if not old['downloaded'] else 'duplicate_version' if eligible and other else 'needs_review' if review else 'excluded'
        db.execute('update corpus_v2_status set domains=?,topics=?,relevant=1,accepted_for_corpus=?,needs_review=?,excluded=?,primary_state=?,run_id=?,updated=? where paper_id=?',(dump(domains),dump(topics),int(accepted),int(review),int(excluded),primary,run,now(),pid))
    db.commit();atomic_json(STATE/'APPROVED_HISTORICAL_LABEL_EVIDENCE.json',dict(classifier_version=version(cfg),counts=dict(counts),examples=dict(examples),no_raw_metadata_changes=True,no_identity_changes=True))
    print('supplement',dump(dict(counts)),flush=True);stats=c.live(db,cfg);c.audit(db,cfg,stats);print('accepted',stats['accepted_for_corpus'],flush=True)

if __name__=='__main__':main()
