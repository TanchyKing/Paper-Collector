import unittest,sqlite3,tempfile,asyncio,json,time
from pathlib import Path
from unittest.mock import patch
import httpx
from network_recovery import NetworkRecovery,transient_network_error,transient_request_error,retry_delay
from test_download_engine import engine,fixture_pdf
from collect import DOMAINS

class NetworkTests(unittest.TestCase):
    def test_server_failures_are_temporary_but_access_denials_are_not(self):
        for code,temporary in [(503,True),(403,False),(404,False)]:
            response=httpx.Response(code,request=httpx.Request('GET','https://example.org/'))
            error=httpx.HTTPStatusError('HTTP error',request=response.request,response=response)
            self.assertEqual(transient_request_error(error),temporary)
    def test_transport_errors_retry_but_access_denials_are_not_network_errors(self):
        self.assertTrue(transient_network_error(httpx.ConnectError('offline')));self.assertTrue(transient_network_error(httpx.ReadTimeout('drop')));self.assertTrue(transient_network_error(TimeoutError()))
        response=httpx.Response(403,request=httpx.Request('GET','https://example.org/'))
        self.assertFalse(transient_network_error(httpx.HTTPStatusError('denied',request=response.request,response=response)));self.assertFalse(transient_network_error(ValueError('invalid PDF')))
        self.assertEqual(retry_delay(100,{'base_retry_seconds':10,'max_retry_seconds':300}),300)
    def test_distinct_host_failures_pause_and_state_survives_recreation(self):
        db=sqlite3.connect(':memory:');r=NetworkRecovery(db,{'no_response_seconds':0});r.failure('a.example',httpx.ConnectError('offline'));r.failure('b.example',httpx.ConnectError('offline'));self.assertTrue(r.ready());r.failure('c.example',httpx.ConnectError('offline'));self.assertFalse(r.ready());other=NetworkRecovery(db,{'no_response_seconds':0});self.assertFalse(other.ready());self.assertEqual(other.state['status'],'waiting_for_network')
    def test_a_successful_host_prevents_false_global_disconnect(self):
        db=sqlite3.connect(':memory:');r=NetworkRecovery(db);r.response('healthy.example')
        for host in ['blocked1.example','blocked2.example','blocked3.example']:r.failure(host,httpx.ConnectTimeout('bad route'))
        self.assertTrue(r.ready())
    def test_response_resumes_only_transport_retries_and_preserves_forbidden_urls(self):
        db=sqlite3.connect(':memory:');db.execute('CREATE TABLE download_locations(status TEXT,next_retry REAL,last_error TEXT)');db.executemany('INSERT INTO download_locations VALUES(?,?,?)',[('transient',time.time()+900,'network_retry:ConnectError'),('forbidden',0,'HTTP_403'),('invalid',0,'not_pdf'),('cooldown',time.time()+900,'HTTP_429')]);r=NetworkRecovery(db,{'no_response_seconds':0,'failing_hosts_threshold':1});r.failure('a',httpx.ConnectError('offline'));r.response('a');self.assertTrue(r.ready());rows=db.execute('SELECT * FROM download_locations').fetchall();self.assertLess(rows[0][1],time.time()+5);self.assertEqual(rows[1],('forbidden',0,'HTTP_403'));self.assertEqual(rows[2],('invalid',0,'not_pdf'));self.assertGreater(rows[3][1],time.time()+800)

class NetworkAsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnected_probe_then_recovered_probe_wakes_without_restarting(self):
        db=sqlite3.connect(':memory:');r=NetworkRecovery(db,{'no_response_seconds':0,'failing_hosts_threshold':1,'probe_interval_seconds':0,'probe_urls':['https://probe.example/']});r.failure('a',httpx.ConnectError('offline'));online=False;real=httpx.AsyncClient
        def respond(request):
            if not online:raise httpx.ConnectError('offline')
            return httpx.Response(200,content=b'ok')
        def client(**kwargs):return real(transport=httpx.MockTransport(respond),**kwargs)
        with patch('network_recovery.httpx.AsyncClient',client):
            await r.probe(lambda url:True);self.assertFalse(r.ready());online=True;await r.probe(lambda url:True);self.assertTrue(r.ready());self.assertEqual(r.state['recoveries'],1)
    async def test_pdf_download_succeeds_after_more_than_three_connection_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            e=engine(tmp);p={'title':'Test Research Paper','domains':[DOMAINS[0]],'topics':[]};e.db.execute('INSERT INTO papers(id,data,status,updated) VALUES(?,?,?,?)',('p',json.dumps(p),'pending','0'));e.add_url('p','https://retry.example/p.pdf','test');e.db.commit();calls=0;real=httpx.AsyncClient;real_sleep=asyncio.sleep;deadline=time.monotonic()+5;e.stopping=lambda:e.completed>=1 or time.monotonic()>deadline
            def respond(request):
                nonlocal calls
                calls+=1
                if calls<=4:raise httpx.ConnectError('simulated offline')
                return httpx.Response(200,content=fixture_pdf())
            def client(**kwargs):return real(transport=httpx.MockTransport(respond),**kwargs)
            async def elapsed(seconds):e.db.execute("UPDATE download_locations SET next_retry=0 WHERE status='transient'");e.db.commit();e.gate.next_start.clear();await real_sleep(0)
            with patch('download_engine.httpx.AsyncClient',client),patch('download_engine.allowed_url',lambda u:True),patch('download_engine.asyncio.sleep',elapsed):await e.download(0)
            self.assertEqual(calls,5);self.assertEqual(e.completed,1);self.assertEqual(e.db.execute('SELECT status FROM papers').fetchone()[0],'downloaded');self.assertEqual(e.db.execute("SELECT count(*) FROM http_attempts WHERE outcome='transient'").fetchone()[0],4)

if __name__=='__main__':unittest.main()
