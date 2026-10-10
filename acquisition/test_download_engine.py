import asyncio,json,sqlite3,tempfile,time,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import httpx,pymupdf as fitz
from download_engine import OptimizedRunner,HostGate,normalize_url,retry_after,pmcids
from collect import DOMAINS,validate

def fixture_pdf():
    doc=fitz.open()
    for i in range(2):
        page=doc.new_page(); page.insert_text((45,45),'Test Research Paper')
        page.insert_text((45,70),('\n'.join(['Methods and results demonstrate a reproducible scientific experiment with measurements.']*15)),fontsize=9)
    return doc.tobytes()

def engine(tmp):
    e=OptimizedRunner.__new__(OptimizedRunner); e.db=sqlite3.connect(':memory:')
    e.db.executescript('''create table controls(key text primary key,value real);
    create table papers(id text primary key,data text,status text,path text,sha256 text,bytes integer default 0,pages integer default 0,error text,attempts integer default 0,next_retry real default 0,updated text);
    create table jobs(id text,status text,next_retry real,priority real);
    create table files(path text primary key,sha256 text,bytes integer,pages integer,validation text);''')
    e.root=Path(tmp); e.state=Path(tmp); (e.state/'metadata').mkdir()
    e.args=SimpleNamespace(read_timeout=2,connect_timeout=1,request_deadline=3,backlog_first_minutes=10,search_backlog_low=250,search_backlog_high=750,retry_failed=False)
    e.settings=SimpleNamespace(ssl_verify=True); e.run_id='test'; e.started=time.monotonic(); e.domain_index=0;e.network_active=0;e.peak_network=0;e.completed=0
    e.validation_gate=asyncio.Semaphore(1)
    def success(pid,path,info):
        e.db.execute("update papers set status='downloaded',path=?,sha256=?,bytes=?,pages=?,error=null where id=?",(str(path),info['sha256'],info['bytes'],info['pages'],pid))
        e.db.execute('insert into files values(?,?,?,?,?)',(str(path),info['sha256'],info['bytes'],info['pages'],'validated')); e.db.commit()
    e.lib=SimpleNamespace(db=e.db,success=success,event=lambda *a,**k:None)
    e.init_schema();e.gate=HostGate(e.db)
    return e

class Tests(unittest.TestCase):
    def test_stop_during_xml_saves_pending_download_without_cancellation_error(self):
        async def run(tmp):
            e=engine(tmp);e.cfg={'fulltext':{'prefer_biomedical_xml':True}};e.stop=False;e.stopping=lambda:e.stop
            p={'title':'Test Research Paper','domains':[DOMAINS[0]],'topics':[]}
            e.db.execute('insert into papers(id,data,status,updated) values(?,?,?,?)',('p',json.dumps(p),'pending','0'));e.add_url('p','https://first.example/p.pdf','test');e.db.commit()
            async def cancel(*args,**kwargs):e.stop=True;raise asyncio.CancelledError()
            with patch('fulltext_xml.preserve_jats',side_effect=cancel):await e.download(0)
            self.assertEqual(e.db.execute('select status from papers where id=?',('p',)).fetchone()[0],'pending')
            self.assertEqual(e.db.execute('select status from download_locations where paper_id=?',('p',)).fetchone()[0],'pending')
            self.assertEqual(e.completed,0)
        with tempfile.TemporaryDirectory() as tmp:asyncio.run(run(tmp))
    def test_url_normalization_and_identifiers(self):
        self.assertEqual(normalize_url('http://arxiv.org/abs/2301.12345v2'),'https://arxiv.org/pdf/2301.12345')
        self.assertEqual(normalize_url('https://www.nature.com/articles/test_reference.pdf'),'https://www.nature.com/articles/test.pdf')
        self.assertEqual(pmcids({'pdf_urls':['https://europepmc.org/articles/PMC12345?pdf=render']}),['PMC12345'])
        self.assertEqual(retry_after({'retry-after':'300'}),300)
    def test_host_limits_and_persistent_cooldown(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp); g=e.gate
            self.assertTrue(g.reserve('arxiv.org')); self.assertFalse(g.reserve('arxiv.org'))
            g.release('arxiv.org'); self.assertFalse(g.ready('arxiv.org'))
            g.block('blocked.example',180)
            self.assertFalse(HostGate(e.db).ready('blocked.example'))
    def test_claim_does_not_duplicate_paper(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp); p={'title':'Test Research Paper','domains':[DOMAINS[0]],'topics':[]}
            e.db.execute('insert into papers(id,data,status,updated) values(?,?,?,?)',('p',json.dumps(p),'pending','0'))
            e.add_url('p','https://first.example/p.pdf','test');e.add_url('p','https://second.example/p.pdf','test');e.db.commit()
            self.assertIsNotNone(e.claim()); self.assertIsNone(e.claim())
    def test_existing_file_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp); d=e.root/DOMAINS[0]; d.mkdir(); prior=d/'p.pdf'; prior.write_bytes(b'prior contents')
            content=fixture_pdf(); p={'title':'Test Research Paper','domains':[DOMAINS[0]]}; info=validate(content,p)
            new=e.save_pdf('p',p,content,info)
            self.assertNotEqual(prior,new); self.assertEqual(prior.read_bytes(),b'prior contents'); self.assertEqual(new.read_bytes(),content)
    def test_repaired_oa_location_is_not_hidden_behind_large_backlog(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp);p={'title':'Test Research Paper','domains':[DOMAINS[0]],'topics':[]}
            for i in range(350):
                pid='old'+str(i);e.db.execute('insert into papers(id,data,status,updated) values(?,?,?,?)',(pid,json.dumps(p),'pending','0'));e.add_url(pid,'https://old.example/'+pid+'.pdf','existing_metadata')
            e.db.execute('insert into papers(id,data,status,updated) values(?,?,?,?)',('repair',json.dumps(p),'pending','999'))
            e.add_url('repair','https://pmc-oa-opendata.s3.amazonaws.com/p.pdf','pmc_cloud');e.db.commit()
            self.assertEqual(e.claim()[0],'repair')
    def test_403_switches_location_and_does_not_repeat(self):
        async def run(tmp):
            e=engine(tmp); p={'title':'Test Research Paper','domains':[DOMAINS[0]],'topics':[],'pdf_urls':[]}
            e.db.execute('insert into papers(id,data,status,updated) values(?,?,?,?)',('p',json.dumps(p),'pending','0'))
            e.add_url('p','https://first.example/p.pdf','test'); e.add_url('p','https://second.example/p.pdf','test');e.db.commit()
            requests=[]
            def respond(request):
                requests.append(str(request.url))
                return httpx.Response(403 if request.url.host=='first.example' else 200,content=b'Forbidden' if request.url.host=='first.example' else fixture_pdf())
            real_client=httpx.AsyncClient
            def client(**kwargs): return real_client(transport=httpx.MockTransport(respond),**kwargs)
            deadline=time.monotonic()+4; e.stopping=lambda:e.completed>=1 or time.monotonic()>deadline
            with patch('download_engine.httpx.AsyncClient',client),patch('download_engine.allowed_url',lambda u:True): await e.download(0)
            self.assertEqual(requests,['https://first.example/p.pdf','https://second.example/p.pdf'])
            self.assertEqual(e.db.execute('select status from papers where id="p"').fetchone()[0],'downloaded')
            self.assertEqual(e.db.execute('select status from download_locations where host="first.example"').fetchone()[0],'forbidden')
            self.assertEqual(e.db.execute('select count(*) from http_attempts').fetchone()[0],2)
        with tempfile.TemporaryDirectory() as tmp: asyncio.run(run(tmp))

if __name__=='__main__': unittest.main()
