import unittest,asyncio,tempfile,json,copy,pathlib,time
from unittest.mock import patch
from types import SimpleNamespace
import httpx
import corpus_continuous as c
from corpus_store import load_config,version
from test_continuous import fixture
from test_download_engine import engine,fixture_pdf
from download_engine import OptimizedRunner
from collect import DOMAINS

class RecoveryTests(unittest.TestCase):
    def test_ready_older_pdfs_are_not_hidden_by_600_new_blocked_candidates(self):
        pool=[('new'+str(i),['popular']) for i in range(800)]+[('older_ready',['support'])]
        selected=c.download_frontier(pool,{'older_ready'},600);self.assertEqual(len(selected),600);self.assertIn('older_ready',selected)
    def test_ready_share_and_domain_round_robin_are_bounded(self):
        ready=[('a'+str(i),['a']) for i in range(800)]+[('b_ready',['b'])];other=[('n'+str(i),['new']) for i in range(800)];ids={p for p,d in ready};result=c.download_frontier(ready+other,ids,600,.7)
        self.assertEqual(len(result),600);self.assertEqual(len(result&ids),420);self.assertIn('b_ready',result);self.assertTrue(result-{p for p,d in ready})
    def test_historical_allocation_uses_configured_fraction(self):
        self.assertEqual(sum(c.historical_slot(i,.2) for i in range(100)),20);self.assertEqual(sum(c.historical_slot(i,.1) for i in range(100)),10)
    def test_2000_range_creates_actual_bounded_historical_jobs_without_changing_classifier(self):
        cfg=load_config();before=version(cfg);cfg=copy.deepcopy(cfg);cfg['continuous']['historical_fraction']=.2
        for d in cfg['domains']:d['target_years']=list(range(2000,2027));d['year_policy']['historical_years']=list(range(2000,2019))
        db=fixture()
        for i in range(4):c.seed_frontier(db,cfg)
        jobs=c.jobs(db,cfg);old=[j for j in jobs if j['purpose']=='historical'];self.assertTrue(old);self.assertTrue(all(2000<=j['lo']<=2018 and j['lo']==j['hi'] for j in old));self.assertLessEqual(len(jobs),cfg['continuous']['frontier_limit']);self.assertEqual(before,version(cfg))
    def test_oa_resolver_is_not_starved_by_a_merely_planned_search(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp);p={'title':'Test Research Paper','domains':[DOMAINS[0]],'topics':[]};e.db.execute('INSERT INTO papers(id,data,status) VALUES(?,?,?)',('p',json.dumps(p),'pending'));e.add_resolution('p','openalex');e.db.commit();e.pending_search_sources={'openalex'};e.search_allowed=lambda:True
            self.assertIsNotNone(e.claim_resolution())
    def test_pool_timeout_recycles_client_without_permanently_exhausting_url(self):
        async def run(tmp):
            e=engine(tmp);p={'title':'Test Research Paper','domains':[DOMAINS[0]],'topics':[]};e.db.execute('INSERT INTO papers(id,data,status,updated) VALUES(?,?,?,?)',('p',json.dumps(p),'pending','0'));e.add_url('p','https://test.example/p.pdf','test');e.db.execute('UPDATE download_locations SET attempts=3');e.db.commit();e.stop=False;e.stopping=lambda:e.stop
            requests=[];real=httpx.AsyncClient
            def respond(request):requests.append(str(request.url));e.stop=True;raise httpx.PoolTimeout('local pool unavailable')
            def client(**kwargs):return real(transport=httpx.MockTransport(respond),**kwargs)
            with patch('download_engine.httpx.AsyncClient',client),patch('download_engine.allowed_url',lambda u:True):await e.download(0)
            self.assertEqual(e.db.execute('SELECT status,attempts FROM download_locations').fetchone(),('transient',3));self.assertEqual(e.db.execute('SELECT outcome FROM http_attempts').fetchone()[0],'transient');self.assertEqual(len(requests),1)
        with tempfile.TemporaryDirectory() as tmp:asyncio.run(run(tmp))

if __name__=='__main__':unittest.main()
