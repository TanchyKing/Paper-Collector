import unittest,json,csv,copy,time
from test_continuous import fixture
from corpus_store import ROOT,STATE,load_config
from corpus_rules import Classifier
import corpus_continuous as c

class ApprovedTests(unittest.TestCase):
    def test_identity_alias_lookup_uses_indexes(self):
        db=fixture();plan=db.execute("EXPLAIN QUERY PLAN SELECT paper_id FROM corpus_v2_metadata WHERE json_extract(normalized,'$.doi')=? OR json_extract(normalized,'$.external_formal_doi')=?",('doi','doi')).fetchall()
        self.assertTrue(any('corpus_v2_meta_external_formal_doi' in r[3] for r in plan));self.assertFalse(any('SCAN corpus_v2_metadata' in r[3] for r in plan))
    def test_union_maps_to_existing_executable_topics(self):
        cfg=load_config();registry=json.loads((STATE/'approved_scope_registry.json').read_text(encoding='utf8'));approval=registry['approval']
        names={d['domain_name'] for d in approval['existing_domains']}|{d['proposed_domain'] for d in approval['approved_proposal_rows']}|set(approval['approved_industrial_scope'])|set(registry['execution_proposal_names'])
        with (ROOT/'APPROVED_DOMAIN_SCOPE_MAP.csv').open(encoding='utf-8-sig') as stream:rows=list(csv.DictReader(stream))
        self.assertEqual(names,{r['original_name'] for r in rows})
        definitions={d['domain_id']:d for d in cfg['domains']}
        for r in rows:
            d=definitions[r['canonical_domain_id']];self.assertTrue(d['enabled']);self.assertTrue(set(json.loads(r['mapped_topic']))<={t['topic_id'] for t in d['topics']});self.assertTrue(json.loads(r['search_strategy'])['queries'])
    def test_generic_subjects_cannot_accept(self):
        cfg=load_config();results=Classifier(cfg).all({'title':'Physical Sciences Engineering Gene model cell network material analysis','abstract':'A study of subjects and data.'})
        self.assertFalse(any(Classifier(cfg).accepts(r) for r in results.values()))
    def test_fair_dispatch_survives_resume_and_covers_topics(self):
        db=fixture();options=[dict(id=d+t,domain=d,topic_id=t,page=0) for d in ['popular','new','support'] for t in ['a','b']]
        chosen=[]
        for i in range(6):
            j=c.fair_job(db,options,lambda j:0 if j['domain']=='popular' else 100);c.dispatched(db,j);chosen.append(j['id'])
        self.assertEqual(len(set(chosen)),6)
        self.assertEqual(db.execute('select sum(dispatches) from corpus_v5_dispatch').fetchone()[0],6)
    def test_family_fairness_prevents_large_industrial_family_from_starving_support(self):
        db=fixture();options=[dict(id=d,domain=d,topic_id='t',page=0) for d in ['industry_a','industry_b','support','common']];families={'industry_a':'industry','industry_b':'industry','support':'science','common':'common'}
        chosen=[]
        for i in range(3):
            j=c.fair_job(db,options,lambda j:0 if j['domain'].startswith('industry') else 100,families);c.dispatched(db,j);chosen.append(families[j['domain']])
        self.assertEqual(len(set(chosen)),3)
    def test_initial_breadth_visits_every_domain_before_repeating_small_families(self):
        db=fixture();names=['industry_'+str(i) for i in range(8)]+['support','common'];options=[dict(domain=d,topic_id='t',page=0) for d in names];families={d:('industry' if d.startswith('industry') else d) for d in names};chosen=[]
        for i in names:
            j=c.fair_job(db,options,lambda j:0,families);c.dispatched(db,j);chosen.append(j['domain'])
        self.assertEqual(set(chosen),set(names))
    def test_fair_frontier_covers_every_configured_topic_without_cartesian_product(self):
        cfg=load_config();db=fixture()
        for i in range(4):c.seed_frontier(db,cfg)
        jobs=c.jobs(db,cfg);self.assertLessEqual(len(jobs),cfg['continuous']['frontier_limit'])
        self.assertEqual({(d['domain_name'],t['topic_id']) for d in cfg['domains'] for t in d['topics']},{(j['domain'],j['topic_id']) for j in jobs})
    def test_parked_cursors_are_preserved(self):
        db=fixture();db.execute("insert into jobs(id,domain,topic,source,lo,hi,cursor,page,status) values('old','old','t','openalex',2023,2023,'saved',9,'pending')");db.execute("insert into corpus_v4_job_policy values('old','t','high','original',100,'{}','now')");db.execute("insert into corpus_v5_frontier values('old',1,'bounded','now')")
        self.assertFalse(c.jobs(db,load_config()));self.assertEqual(db.execute("select cursor,page from jobs where id='old'").fetchone(),('saved',9))

if __name__=='__main__':unittest.main()
