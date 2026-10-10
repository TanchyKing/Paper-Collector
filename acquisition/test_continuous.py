import copy,json,sqlite3,unittest
from unittest.mock import patch,AsyncMock,Mock
from types import SimpleNamespace
from collections import Counter
import corpus_continuous as c
from corpus_store import load_config,migrate
from corpus_rules import Classifier
from corpus_runner import CorpusRunner
from corpus_store import STATE
import tempfile,pathlib

def fixture():
    db=sqlite3.connect(':memory:');migrate(db)
    db.executescript("CREATE TABLE papers(id TEXT PRIMARY KEY,data TEXT,status TEXT,updated TEXT); CREATE TABLE jobs(id TEXT PRIMARY KEY,domain TEXT,topic TEXT,source TEXT,lo INTEGER,hi INTEGER,sort TEXT,cursor TEXT,page INTEGER,priority REAL,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_retry REAL DEFAULT 0,error TEXT,total INTEGER,created TEXT,updated TEXT);")
    db.execute('CREATE TABLE files(sha256 TEXT PRIMARY KEY,path TEXT,bytes INTEGER)');c.init(db);return db

class Tests(unittest.TestCase):
    def test_documented_cn_en_domain_fields_work_without_core_changes(self):
        cfg=load_config();cfg['domains'].append({'domain_id':'robotics_test','domain_name_cn':'机器人测试','domain_name_en':'Robotics Test','topics':['robot navigation'],'positive_terms':['robot navigation'],'preferred_sources':['openalex'],'year_policy':{'years':[2023]}})
        base=STATE/'repair_v2/test_artifacts';base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as task_dir:
            path=pathlib.Path(task_dir)/'config.json';path.write_text(json.dumps(cfg,ensure_ascii=False),encoding='utf8');loaded=load_config(path)
            self.assertEqual(loaded['domains'][-1]['target_years'],[2023]);self.assertTrue(Classifier(loaded).accepts(Classifier(loaded).classify({'title':'Robot navigation method'},'机器人测试')))
    def test_frontier_is_bounded_and_old_cursor_is_preserved(self):
        cfg=load_config();cfg['continuous']['frontier_limit']=12;cfg['continuous']['new_jobs_per_refresh']=12
        db=fixture();db.execute("INSERT INTO jobs(id,domain,topic,source,lo,hi,sort,cursor,page,status) VALUES('old','old domain','old topic','openalex',2014,2026,'newest','saved',7,'done')")
        count=c.seed_frontier(db,cfg);self.assertGreater(count,0);self.assertLessEqual(count,12);self.assertEqual(c.seed_frontier(db,cfg),0)
        self.assertEqual(db.execute("SELECT cursor,page,status FROM jobs WHERE id='old'").fetchone(),('saved',7,'done'))
        self.assertTrue(any(j['queue']=='exploration' for j in c.jobs(db,cfg)))
        self.assertTrue(any(j['purpose']=='classic' for j in c.jobs(db,cfg)))
        self.assertTrue(all(j['lo']==j['hi'] for j in c.jobs(db,cfg)))
    def test_candidate_domains_and_disabled_domains_cannot_classify(self):
        cfg=load_config();cfg['candidate_domains']=[{'domain_name':'UNREVIEWED'}];cfg['domains'].append(dict(cfg['domains'][0],domain_name='DISABLED',enabled=False))
        results=Classifier(cfg).all({'title':'Antibody engineering for drug therapy'});self.assertNotIn('DISABLED',results);self.assertNotIn('UNREVIEWED',results)
    def test_curated_venue_alone_cannot_make_core_tier(self):
        cfg=load_config();d=cfg['domains'][-1];p={'title':'A study','citation_count':0};n={'formal_venue':d['preferred_venues'][0],'publication_status':'published','formal_publication_type':'conference','canonical_publication_year':2024}
        value=c.tier(p,n,[d['domain_name']],{d['domain_name']:[]},cfg);self.assertEqual(value['literature_tier'],'B Standard')
        p.update(title='A benchmark framework',citation_count=500);value=c.tier(p,n,[d['domain_name']],{d['domain_name']:[d['topics'][0]['topic_id']]},cfg);self.assertEqual(value['literature_tier'],'A Core');self.assertTrue(value['evidence']['signals']['influence_threshold_met'])
    def test_formal_metadata_does_not_assert_local_file_version(self):
        cfg=load_config();p={'title':'A preprint method'};n={'publication_status':'publication_unconfirmed','local_file_version':'preprint_or_author_manuscript'}
        self.assertEqual(c.tier(p,n,[],{},cfg)['literature_tier'],'C Supporting');self.assertEqual(n['local_file_version'],'preprint_or_author_manuscript')
    def test_saturated_topic_is_deprioritized_without_ban(self):
        cfg=load_config();d=cfg['domains'][0];t=d['topics'][0];empty=(__import__('collections').defaultdict(Counter),__import__('collections').defaultdict(Counter),Counter());s1=c.priority(d,t,2023,'openalex',empty,{})[0]
        full=copy.deepcopy(empty);full[0][(d['domain_name'],t['topic_id'],2023)]['accepted']=10000;s2=c.priority(d,t,2023,'openalex',full,{})[0];self.assertGreater(s2,0);self.assertLess(s2,s1)
    def test_persistent_api_budget(self):
        db=fixture();cfg=load_config();cfg['continuous']['api_requests_per_day']['openalex']=2
        self.assertTrue(c.budget_ready(db,cfg,'openalex'));c.spend(db,'openalex');c.spend(db,'openalex');self.assertFalse(c.budget_ready(db,cfg,'openalex'))
    def test_budget_exhaustion_does_not_consume_resolution_attempt(self):
        from download_engine import OptimizedRunner
        db=fixture();db.executescript("CREATE TABLE resolution_queue(paper_id TEXT,provider TEXT,status TEXT,attempts INTEGER,next_retry REAL,updated TEXT);")
        db.execute("INSERT INTO papers(id,data,status) VALUES('p','{}','pending')");db.execute("INSERT INTO resolution_queue VALUES('p','openalex','pending',0,0,'now')")
        r=object.__new__(OptimizedRunner);r.db=db;r.corpus_resolve_filter=lambda:'';r.api_budget_ready=lambda source:False
        self.assertIsNone(r.claim_resolution());self.assertEqual(db.execute('SELECT attempts,status FROM resolution_queue').fetchone(),(0,'pending'))
    def test_tier_evidence_history_survives_new_citation_signal(self):
        db=fixture();cfg=load_config();p={'title':'A method','citation_count':0};n={'publication_status':'published','formal_publication_type':'journal','canonical_publication_year':2020}
        c.save_tier(db,'p',p,n,[],{},cfg);p['citation_count']=500;c.save_tier(db,'p',p,n,[],{},cfg);c.save_tier(db,'p',p,n,[],{},cfg)
        counts=[r[0] for r in db.execute("SELECT json_extract(evidence,'$.citations') FROM corpus_v4_tier_history ORDER BY recorded")];self.assertEqual(counts,[0,500])
    def test_low_disk_pauses_pdf_claim_without_stopping_runner(self):
        r=object.__new__(CorpusRunner);r.continuous=True;r.cfg=load_config();r.args=SimpleNamespace(reserve_gb=10,max_new_pdfs=None);r.stop=False
        with patch('shutil.disk_usage',return_value=SimpleNamespace(free=5*1024**3)),patch('download_engine.OptimizedRunner.claim',side_effect=AssertionError('must not fetch')):self.assertIsNone(r.claim())
        self.assertTrue(r.pdf_paused);self.assertFalse(r.stop)
    def test_low_disk_metadata_budget_is_bounded_and_persistent(self):
        r=object.__new__(CorpusRunner);r.continuous=True;r.cfg=load_config();r.cfg['continuous']['disk_metadata_candidate_budget']=2;r.db=fixture();r.scope_ids=set();r.frontier_limit=600;r.pdf_paused=True;r.started=0;r.last_search=0;r.args=SimpleNamespace(backlog_first_minutes=0)
        self.assertTrue(r.search_allowed());r.db.executemany('INSERT INTO papers(id,data) VALUES(?,?)',[('a','{}'),('b','{}')]);r.db.commit();self.assertFalse(r.search_allowed());self.assertEqual(c.setting(r.db,'disk_pause_candidates'),0)
    def test_review_role_keeps_evidence(self):
        role,proof=c.article_role({'title':'Retrieval augmented generation: a survey'},{});self.assertEqual(role,'survey');self.assertEqual(proof['basis'],'title_heuristic')
    def test_new_families_require_specific_concepts(self):
        classifier=Classifier(load_config());p={'title':'Cell model screening and material network analysis','abstract':'Analysis of a model.'}
        self.assertFalse(any(classifier.accepts(r) for r in classifier.all(p).values()))
        self.assertTrue(classifier.accepts(classifier.classify({'title':'Technology readiness level for research commercialization'},'成果转化与创新共性研究')))
        self.assertTrue(classifier.accepts(classifier.classify({'title':'Retrieval augmented generation for large language models'},'AI与知识工程共性技术')))
class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_formal_metadata_lane_consumes_api_payload_and_checkpoints(self):
        db=fixture();p={'title':'Technology transfer framework','authors':['Jane Smith'],'doi':'10.9/test'};n={'doi':'10.9/test','formal_venue':None}
        db.execute('INSERT INTO papers(id,data,status) VALUES(?,?,?)',('test',json.dumps(p),'downloaded'));db.execute('INSERT INTO corpus_v2_metadata(paper_id,normalized) VALUES(?,?)',('test',json.dumps(n)));db.execute('INSERT INTO corpus_v2_status(paper_id,downloaded) VALUES(?,1)',('test',));db.commit()
        r=object.__new__(CorpusRunner);r.db=db;r.cfg=load_config();r.pdf_paused=False;r.metadata_ready=0;r.dispatch=1;r.stop=False;r.stopping=lambda:r.stop;r.lib=SimpleNamespace(event=Mock());r.settings=SimpleNamespace();r.api=AsyncMock(return_value={'message':{'DOI':'10.9/test','title':[p['title']],'type':'journal-article','container-title':['Journal Example'],'author':[{'given':'Jane','family':'Smith'}]}});r.candidate_seen=lambda pid:setattr(r,'stop',True)
        base=STATE/'repair_v2/test_artifacts';base.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(dir=base) as task_dir,patch('corpus_runner.STATE',pathlib.Path(task_dir)),patch('corpus_runner.asyncio.sleep',new=AsyncMock()):
            await r.metadata_worker();proof=json.loads((pathlib.Path(task_dir)/'continuous_metadata/test.json').read_text(encoding='utf8'));self.assertEqual(proof['crossref']['DOI'],'10.9/test');self.assertEqual(db.execute('SELECT status FROM corpus_v4_metadata_queue WHERE paper_id=?',('test',)).fetchone()[0],'done');self.assertTrue(r.lib.event.called)
if __name__=='__main__':unittest.main()
