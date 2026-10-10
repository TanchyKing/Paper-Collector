import copy,json,os,sqlite3,tempfile,unittest,asyncio
from pathlib import Path
from unittest.mock import patch
from types import SimpleNamespace
import pymupdf,httpx
from corpus_languages import init,record,detect_pdf,detect_texts,title_overlap,balanced_frontier
from corpus_store import load_config,version
from corpus_planner import job_id
from test_continuous import fixture
from test_download_engine import engine,fixture_pdf
from collect import validate,Adapter
import corpus_continuous as c
from language_queue import seed
from publisher_oa import pdf_links

class Tests(unittest.TestCase):
    def test_chinese_candidate_is_not_hidden_by_large_english_backlog(self):
        pool=[('en'+str(i),['English heavy domain']) for i in range(1000)]+[('cn',['support'])]
        selected=balanced_frontier(pool,{'cn'},{'cn'},600);self.assertIn('cn',selected);self.assertEqual(len(selected),600)
    def test_chinese_reviews_keep_review_role(self):
        self.assertEqual(c.article_role({'title':'固态电池材料研究进展'}, {})[0],'review')
    def test_chinese_title_wraps_match_but_unrelated_title_does_not(self):
        self.assertEqual(title_overlap('基于深度学习的机器人运动规划方法','基于深度学习的\n机器人运动规划方法'),1)
        self.assertLess(title_overlap('锂离子电池电解液材料设计','机器人人机交互评价实验方法'),.35)
    def test_fulltext_body_not_chinese_abstract_decides_language(self):
        doc=pymupdf.open()
        for i in range(6):
            page=doc.new_page()
            if i==0:page.insert_text((30,30),'中文摘要与关键词'*20,fontname='china-s',fontsize=7)
            else:page.insert_text((30,30),'The method and the results of this study are compared with the baseline. '*15,fontsize=7)
        self.assertEqual(detect_pdf(doc)['language'],'en')
        self.assertEqual(detect_texts(['研究方法与实验结果表明锂离子电池材料具有良好性能。'*30])['language'],'zh')
        self.assertEqual(detect_texts(['無線システムの研究方法と結果を検討した。'*30])['language'],'other')
        self.assertEqual(detect_texts([''])['language'],'unknown')
    def test_chinese_pdf_identity_is_verified(self):
        doc=pymupdf.open();title='基于深度学习的机器人运动规划方法'
        for i in range(3):
            page=doc.new_page();page.insert_text((25,30),title,fontname='china-s',fontsize=11)
            for j in range(35):page.insert_text((25,55+j*18),'实验结果表明该机器人运动规划方法能够提高控制精度和计算效率。',fontname='china-s',fontsize=10)
        info=validate(doc.tobytes(),{'title':title});self.assertEqual(info['language'],'zh');self.assertEqual(info['title_overlap'],1)
        with self.assertRaises(ValueError):validate(doc.tobytes(),{'title':'锂离子电池电解液材料设计与性能研究'})
    def test_language_view_preserves_original_inode_and_original_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=sqlite3.connect(':memory:');init(db);root=Path(tmp);old=root/'old.pdf';payload=fixture_pdf();old.write_bytes(payload)
            cfg={'language_policy':{'enabled':True}};p={'title':'Test Research Paper','domains':['technical']}
            self.assertEqual(record(db,root,cfg,'id',p,old),'en');view=Path(db.execute('SELECT view_path FROM corpus_v6_languages').fetchone()[0]);self.assertTrue(os.path.samefile(old,view));self.assertEqual(old.read_bytes(),payload)
            record(db,root,cfg,'id',p,old);self.assertEqual(db.execute('SELECT count(*) FROM corpus_v6_language_history').fetchone()[0],1)
    def test_chinese_frontier_is_bounded_bilingual_and_all_domains_have_tasks(self):
        db=fixture();cfg=load_config();cfg['language_policy']={'enabled':True,'zh_search_fraction':.5,'zh_sources':['doaj','crossref','openalex']};before=version(cfg)
        count=seed(db,cfg);self.assertEqual(count,len(cfg['domains']));self.assertEqual(len({r[0] for r in db.execute('SELECT domain FROM jobs')}),len(cfg['domains']))
        for i in range(5):c.seed_frontier(db,cfg)
        active=db.execute("SELECT count(*) FROM jobs j JOIN corpus_v4_job_policy q ON j.id=q.job_id WHERE j.status IN ('pending','retry','running') AND NOT EXISTS(SELECT 1 FROM corpus_v5_frontier f WHERE f.job_id=j.id AND f.parked=1)").fetchone()[0];self.assertLessEqual(active,cfg['continuous']['frontier_limit']);self.assertEqual(version(cfg),before)
    def test_language_job_identity_and_child_checkpoint_are_distinct(self):
        a=job_id('d','q','openalex',2024);b=job_id('d','q','openalex',2024,language='zh');self.assertNotEqual(a,b)
        db=fixture();db.execute('INSERT INTO corpus_v6_job_language VALUES(?,?,?,?,?)',(b,'zh','中文','{}','now'));c.record_child(db,b,'child');self.assertEqual(db.execute("SELECT language FROM corpus_v6_job_language WHERE job_id='child'").fetchone()[0],'zh')
    def test_only_public_linked_pdfs_are_extracted(self):
        text='<meta name="citation_pdf_url" content="https://publisher.example/a.pdf"><a href="/b.pdf">pdf</a><a href="https://sci-hub.example/b.pdf">bad</a><a href="javascript:bad.pdf">bad</a>'
        self.assertEqual(pdf_links(text,'https://publisher.example/article'),['https://publisher.example/a.pdf','https://publisher.example/b.pdf'])

class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_publisher_resolver_keeps_prior_denial(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp);e.add_url('id','https://publisher.example/a.pdf','test');e.db.execute("UPDATE download_locations SET status='forbidden'");e.db.commit();e.api=lambda *a,**k:None
            self.assertEqual(await e.resolve_one(None,'id',{'url':'https://publisher.example/a.pdf','is_open_access':True},'publisher_oa'),0)
    async def test_publisher_oa_resolver_requires_actual_oa_evidence(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp);e.api=lambda *a,**k:None
            self.assertEqual(await e.resolve_one(None,'id',{'doi':'10.1/no','url':'https://publisher.example/a','is_open_access':False},'publisher_oa'),0)
    async def test_openalex_language_filter_reaches_request_and_cache_key(self):
        from paper_pilot.services.academic import AcademicSearchService
        adapter=Adapter.__new__(Adapter);adapter.job={'source':'openalex','language':'zh','hi':2024};adapter.lib=SimpleNamespace();adapter.raw_items={};captured={}
        async def fake(self,client,url,params,namespace,timeout=None):captured.update(params);return {'results':[]}
        with patch.object(AcademicSearchService,'_get_json',fake):await adapter._get_json(None,'https://api.openalex.org/works',{'filter':'publication_year:2024'},'openalex_search')
        self.assertEqual(captured['filter'],'publication_year:2024,language:zh')

if __name__=='__main__':unittest.main()
