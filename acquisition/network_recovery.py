"""Persistent network circuit state; remote access denials are never reset."""
import asyncio,datetime,json,time
from urllib.parse import urlparse
import httpx

def transient_network_error(error):
    return isinstance(error,(httpx.NetworkError,httpx.TimeoutException,httpx.RemoteProtocolError,httpx.ProxyError,TimeoutError))

def transient_request_error(error):
    code=getattr(error,'download_code',None) or getattr(getattr(error,'response',None),'status_code',None)
    return transient_network_error(error) or bool(code and 500<=code<600)

def retry_delay(attempts,policy=None):
    policy=policy or {};return min(float(policy.get('max_retry_seconds',300)),float(policy.get('base_retry_seconds',10))*2**min(max(0,attempts-1),6))

class NetworkRecovery:
    def __init__(self,db,policy=None):
        self.db=db;self.policy=policy or {};self.enabled=self.policy.get('enabled',True)
        db.executescript('CREATE TABLE IF NOT EXISTS network_recovery_state(key TEXT PRIMARY KEY,value TEXT,updated TEXT); CREATE TABLE IF NOT EXISTS network_recovery_history(id INTEGER PRIMARY KEY,recorded TEXT,event TEXT,details TEXT);')
        row=db.execute("SELECT value FROM network_recovery_state WHERE key='current'").fetchone()
        self.state=json.loads(row[0]) if row else dict(status='unknown',paused=False,last_response=0,failures={},next_probe=0,recoveries=0)
        # A restart must not defer the first connectivity check indefinitely.
        if self.state['paused']:self.state['next_probe']=min(self.state['next_probe'],time.time()+self.policy.get('probe_interval_seconds',30))
        db.commit()
    def save(self,event=None):
        at=datetime.datetime.now(datetime.timezone.utc).isoformat();text=json.dumps(self.state,sort_keys=True)
        self.db.execute("INSERT INTO network_recovery_state VALUES('current',?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated=excluded.updated",(text,at))
        if event:self.db.execute('INSERT INTO network_recovery_history(recorded,event,details) VALUES(?,?,?)',(at,event,text))
        self.db.commit()
    def ready(self):return not self.enabled or not self.state['paused']
    def failure(self,host,error):
        if not self.enabled or not transient_network_error(error) or isinstance(error,httpx.PoolTimeout):return
        now=time.time();window=self.policy.get('failure_window_seconds',120);self.state['failures']={h:t for h,t in self.state['failures'].items() if now-t<=window};self.state['failures'][host]=now
        was=self.state['paused'];threshold=self.policy.get('failing_hosts_threshold',3)
        if len(self.state['failures'])>=threshold and now-self.state['last_response']>=self.policy.get('no_response_seconds',15):
            self.state.update(paused=True,status='waiting_for_network',next_probe=now+self.policy.get('probe_interval_seconds',30))
        elif not was:self.state['status']='retrying_network'
        self.state['last_error']=type(error).__name__;self.save('network_wait_started' if not was and self.state['paused'] else None)
    def response(self,host):
        if not self.enabled:return
        was=self.state['paused'];self.state.update(status='online',paused=False,last_response=time.time(),failures={},next_probe=0,last_error=None)
        if was:
            self.state['recoveries']+=1;self.resume_transport_retries();self.save('network_recovered')
        else:
            # Persist the heartbeat at most every 30 seconds.
            if time.time()-getattr(self,'last_saved_response',0)>30:self.save();self.last_saved_response=time.time()
    def resume_transport_retries(self):
        now=time.time()+2
        for table,column,status in [('download_locations','last_error','transient'),('resolution_queue','error','retry'),('jobs','error','retry')]:
            if self.db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():
                self.db.execute('UPDATE '+table+' SET next_retry=min(next_retry,?) WHERE status=? AND '+column+" LIKE 'network_retry:%'",(now,status))
        for table in ['corpus_v4_metadata_queue','corpus_v4_references']:
            if self.db.execute('SELECT 1 FROM sqlite_master WHERE name=?',(table,)).fetchone():self.db.execute('UPDATE '+table+" SET next_retry=min(next_retry,?) WHERE status='retry'",(now,))
        self.db.commit()
    async def probe(self,allowed_url):
        if not self.enabled or not self.state['paused'] or time.time()<self.state['next_probe']:return
        self.state['next_probe']=time.time()+self.policy.get('probe_interval_seconds',30);self.save()
        # Static public pages: no paid service, API quota or login dependency.
        async with httpx.AsyncClient(timeout=self.policy.get('probe_timeout_seconds',8),follow_redirects=False,trust_env=True,verify=True) as client:
            for url in self.policy.get('probe_urls',['https://arxiv.org/','https://www.ebi.ac.uk/']):
                try:
                    async with asyncio.timeout(self.policy.get('probe_timeout_seconds',8)):
                        if not await asyncio.to_thread(allowed_url,url):continue
                        async with client.stream('GET',url) as response:
                            # HTTP reachability is separate from document access permission.
                            self.response(urlparse(url).hostname);return
                except Exception as error:self.state['last_probe_error']=type(error).__name__
        self.save('network_probe_failed')
