import unittest,tempfile,pathlib,sqlite3,json
from unittest.mock import patch,AsyncMock
from types import SimpleNamespace
from corpus_store import load_config
from corpus_runner import CorpusRunner
import corpus_capacity as c
from test_continuous import fixture

class CapacityTests(unittest.TestCase):
    def test_total_existing_pdf_and_raw_xml_count_towards_target(self):
        db=fixture();db.execute("INSERT INTO files VALUES('sha','p.pdf',100)");cfg=load_config();cfg['storage_stage']={'enabled':True,'target_fulltext_bytes':113}
        with tempfile.TemporaryDirectory() as tmp:
            state=pathlib.Path(tmp);(state/'fulltext_xml').mkdir();(state/'fulltext_xml/a.xml').write_bytes(b'x'*13);(state/'fulltext_xml/a.sections.json').write_bytes(b'x'*500);(state/'backup.pdf').write_bytes(b'x'*900)
            v=c.stage(db,cfg,state);self.assertEqual(v['fulltext_bytes'],113);self.assertTrue(v['target_reached']);self.assertEqual(v['remaining_bytes'],0)
            cfg['storage_stage']['enabled']=False;self.assertFalse(c.stage(db,cfg,state)['target_reached'])
    def test_cap_stops_new_pdf_claim_and_discovery(self):
        db=fixture();db.execute("INSERT INTO files VALUES('sha','p.pdf',100)");r=object.__new__(CorpusRunner);r.db=db;r.cfg=load_config();r.cfg['storage_stage']={'enabled':True,'target_fulltext_bytes':100};r.continuous=False
        with tempfile.TemporaryDirectory() as tmp,patch('corpus_capacity.STATE',pathlib.Path(tmp)),patch('download_engine.OptimizedRunner.claim',side_effect=AssertionError('must not claim')):
            self.assertIsNone(r.claim());self.assertFalse(r.search_allowed())
    def test_inventory_deduplicates_versions_and_does_not_promote_review(self):
        db=fixture();db.execute('ALTER TABLE papers ADD COLUMN error TEXT');cfg=load_config();cfg['storage_stage']={'enabled':True,'target_fulltext_bytes':120000000000};d=cfg['domains'][0];topic=d['topics'][0]['topic_id']
        for pid,identity,review,excluded,valid in [('a','pending',0,0,0),('b','pending',0,0,0),('r','review',1,0,0),('x','excluded',0,1,0),('done','complete',0,0,1)]:
            db.execute('INSERT INTO papers(id,data,status) VALUES(?,?,?)',(pid,json.dumps({'title':pid,'source':'arxiv','pdf_urls':['https://arxiv.org/pdf/2401.12345']}),'downloaded' if valid else 'pending'))
            db.execute('INSERT INTO corpus_v2_metadata(paper_id,normalized) VALUES(?,?)',(pid,json.dumps({'document_type':'preprint','canonical_publication_year':2023})))
            db.execute('INSERT INTO corpus_v2_status(paper_id,article_identity,domains,topics,needs_review,excluded,relevant,valid_pdf,downloaded) VALUES(?,?,?,?,?,?,1,?,?)',(pid,identity,json.dumps([d['domain_name']]),json.dumps({d['domain_name']:[topic]}),review,excluded,valid,valid))
        with tempfile.TemporaryDirectory() as tmp:
            root=pathlib.Path(tmp);result=c.inventory(db,cfg,root,root);self.assertEqual(result['remaining_independent_articles'],3);self.assertEqual(result['action_counts'],{'resolve_legal_OA_and_download':1,'needs_review_before_download':1,'excluded_keep_evidence':1});self.assertTrue((root/'DOWNLOAD_PENDING.csv').exists());self.assertEqual(db.execute('SELECT count(*) FROM papers').fetchone()[0],5)

class CapacityAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_monitor_records_capacity_stop_without_rescanning_or_launching_network(self):
        db=fixture();db.execute("INSERT INTO files VALUES('sha','p.pdf',100)");r=object.__new__(CorpusRunner);r.db=db;r.cfg=load_config();r.cfg['storage_stage']={'enabled':True,'target_fulltext_bytes':100};r.args=SimpleNamespace(minutes=0,max_new_pdfs=None);r.run_id='test_capacity';r.stop=False;r.stopping=lambda:r.stop
        with tempfile.TemporaryDirectory() as tmp,patch('corpus_capacity.STATE',pathlib.Path(tmp)),patch('corpus_runner.STATE',pathlib.Path(tmp)),patch('corpus_runner.asyncio.sleep',new=AsyncMock()):
            self.assertEqual(await r.monitor(),'stopped_capacity_target');self.assertTrue(r.stop);self.assertEqual(json.loads((pathlib.Path(tmp)/'run_state.json').read_text())['status'],'stopped_capacity_target')

if __name__=='__main__':unittest.main()
