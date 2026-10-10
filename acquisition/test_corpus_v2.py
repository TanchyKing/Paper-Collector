import copy,hashlib,json,pathlib,sqlite3,unittest,uuid
from unittest.mock import patch
import httpx
from corpus_store import load_config,version,migrate,rollback,STATE
from corpus_rules import Classifier,LocalSemantic,apply_semantic
from corpus_metadata import Normalizer
from corpus_planner import seed_annual_jobs,preview,score_job
from corpus_repair import build_identities,acceptance
from corpus_venues import normalize_venues
class Tests(unittest.TestCase):
 def setUp(self):
  self.cfg=load_config();self.classifier=Classifier(self.cfg);self.folder=STATE/'repair_v2'/'test_artifacts'/uuid.uuid4().hex;self.folder.mkdir(parents=True)
 def test_ambiguous_generic_terms_are_not_positive(self):
  for title in ['Language model analysis of primary school value-added scores','High-Throughput Analysis of Arabidopsis Stem Vibrations','Fluorescent Receptor Binding Assay for Detecting Ciguatoxins in Fish']:
   p={'title':title,'abstract':'Screening models and statistical analysis are performed.','domains':[]};results=self.classifier.all(p)
   self.assertFalse(any(self.classifier.accepts(r) for r in results.values()),results)
 def test_relevant_antibody_method(self):
  r=self.classifier.classify({'title':'Antibody engineering for targeted drug therapy','abstract':'Designing bispecific antibodies for cancer drug discovery.'},'生物制药');self.assertTrue(self.classifier.accepts(r))
 def test_new_domain_requires_only_config(self):
  cfg=copy.deepcopy(self.cfg);cfg['domains'].append({'domain_name':'QuantumMethods','description':'Quantum computing methods','aliases':[],'positive_terms':['quantum computer'],'weak_terms':[],'negative_terms':[],'context_terms':['quantum'],'topics':[{'topic_id':'quantum algorithm','positive_terms':['quantum algorithm']}],'priority':1,'target_count':20,'target_years':[2023]})
  self.assertEqual(Classifier(cfg).classify({'title':'Quantum algorithm for a quantum computer'},'QuantumMethods')['relevance_label'],'relevant')
 def test_epmc_date_and_pubyear_remain_separate(self):
  p={'source':'europe_pmc','title':'AAV gene therapy method','doi':'10.9/x','year':2024,'publication_date':'2023-12-01','raw_source_evidence':{'title':'AAV gene therapy method','doi':'10.9/x','pubYear':'2024','firstPublicationDate':'2023-12-01','journalInfo':{'journal':{'title':'Journal Example','issn':'1234-5678'}}}};before=copy.deepcopy(p)
  n=Normalizer(audit_folder=self.folder).normalize('p',p,persist=False);self.assertEqual(n['canonical_publication_year'],2023);self.assertEqual(n['europe_pmc_pub_year'],'2024');self.assertEqual(n['source_reported_year'],2024);self.assertEqual(p,before)
 def test_future_issue_not_online_date(self):
  folder=self.folder/'crossref';folder.mkdir();raw={'DOI':'10.9/x','title':['AAV gene therapy method'],'type':'journal-article','container-title':['Example Journal'],'published-print':{'date-parts':[[2027,1,1]]},'published-online':{'date-parts':[[2025,12,1]]}}
  (folder/'response.json').write_text(json.dumps({'status':200,'doi':'10.9/x','message':raw}),encoding='utf-8')
  n=Normalizer(audit_folder=self.folder).normalize('p',{'title':'AAV gene therapy method','source':'doaj','doi':'10.9/x','year':2027},persist=False);self.assertEqual(n['canonical_publication_year'],2025);self.assertTrue(n['future_issue_or_date']);self.assertEqual(n['formal_publication_status'],'confirmed_online_or_published')
 def test_annual_queue_and_cursor_idempotence(self):
  db=sqlite3.connect(':memory:');db.executescript("CREATE TABLE jobs(id TEXT PRIMARY KEY,domain TEXT,topic TEXT,source TEXT,lo INTEGER,hi INTEGER,sort TEXT,cursor TEXT,page INTEGER,priority REAL,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_retry REAL DEFAULT 0,error TEXT,total INTEGER,created TEXT,updated TEXT);")
  migrate(db);cfg=copy.deepcopy(self.cfg);cfg['continuous']={'enabled':False};cfg['domains']=cfg['domains'][:1];cfg['domains'][0]['topics']=cfg['domains'][0]['topics'][:1];cfg['domains'][0]['target_years']=[2023,2024,2026];cfg['sampling']['providers']=['doaj'];cfg['sampling']['multilingual_providers']=[]
  cfg['domains'][0]['search_sources']=['doaj']
  self.assertEqual(seed_annual_jobs(db,cfg),3);db.execute("UPDATE jobs SET cursor='saved-page',page=3,status='done' WHERE lo=2023");db.commit();self.assertEqual(seed_annual_jobs(db,cfg),0);self.assertEqual(db.execute('SELECT cursor,page,status FROM jobs WHERE lo=2023').fetchone(),('saved-page',3,'done'));self.assertEqual(db.execute('SELECT count(*) FROM jobs WHERE lo!=hi OR sort!=\'relevance\'').fetchone()[0],0)
 def test_migration_rollback_preserves_original(self):
  db=sqlite3.connect(':memory:');db.execute('CREATE TABLE papers(id TEXT,data TEXT)');db.execute('INSERT INTO papers VALUES(?,?)',('original','untouched evidence'));db.commit();migrate(db);db.execute("INSERT INTO corpus_v2_metadata VALUES('original','{}','run','now')");db.commit();rollback(db,str(self.folder/'archive.sqlite3'));self.assertEqual(db.execute('SELECT * FROM papers').fetchall(),[('original','untouched evidence')]);self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='corpus_v2_metadata'").fetchone());migrate(db)
 def test_publication_and_server_separate(self):
  folder=self.folder/'crossref';folder.mkdir();raw={'DOI':'10.9/x','title':['Paper on advanced solar perovskite technology'],'type':'journal-article','container-title':['Nature Communications'],'publisher':'Publisher Example','published-online':{'date-parts':[[2024,2,3]]}}
  (folder/'r.json').write_text(json.dumps({'status':200,'doi':'10.9/x','message':raw}),encoding='utf-8');p={'title':raw['title'][0],'doi':'10.9/x','source':'arxiv','source_id':'https://arxiv.org/abs/2301.12345','document_type':'preprint','year':2023,'publication_date':'2023-01-03','downloaded_from':'https://arxiv.org/pdf/2301.12345'}
  n=Normalizer(audit_folder=self.folder).normalize('p',p,persist=False);self.assertEqual(n['formal_venue'],'Nature Communications');self.assertEqual(n['preprint_source'],'arXiv');self.assertEqual(n['downloaded_version'],'preprint_or_author_manuscript');self.assertEqual(n['canonical_publication_year'],2024);self.assertEqual(n['first_submission_date'],'2023-01-03')
 def test_negative_rule_not_overruled_by_model(self):
  rule=self.classifier.classify({'title':'Value-added scores in primary school','abstract':'Language model analysis'},'软件与通信');answer={'relevance_label':'relevant','relevance_score':.94,'method':'local_qwen_boundary','reason':'test'};self.assertEqual(apply_semantic(rule,answer)['relevance_label'],'uncertain')
 def test_local_model_invalid_quote_not_promoted(self):
  def response(*a,**kw):return httpx.Response(200,request=httpx.Request('POST','http://localhost/'),json={'message':{'content':json.dumps({'results':[{'id':'0','label':'relevant','reason':'test','core_evidence':'invented statement'}]})}})
  with patch('corpus_rules.httpx.get',side_effect=RuntimeError('offline')),patch('corpus_rules.httpx.post',side_effect=response):
   model=LocalSemantic(self.cfg,self.folder/'semantic');r=model.classify_batch([{'paper_id':'p','domain':'生物制药','p':{'title':'Gene therapy','abstract':'An AAV gene therapy method.'}}]);self.assertEqual(r[('p','生物制药')]['relevance_label'],'uncertain')
 def test_valid_pdf_without_relevance_not_accepted(self):
  results={d:self.classifier.classify({'title':'School value-added score','abstract':'Education study'},d) for d in self.classifier.domains};n={'document_type':'journal_paper','canonical_publication_year':2024};e=acceptance({},n,{'valid_pdf':True,'identity_confirmed':True},results,self.cfg);self.assertFalse(e['eligible'])
 def test_issn_merges_journal_alias_not_conference_series(self):
  db=sqlite3.connect(':memory:');migrate(db)
  metadata={p:{'formal_venue':v,'formal_publication_type':kind,'issn':['1234-5678'],'publisher':'P','field_provenance':{}} for p,v,kind in [('a','Journal A','journal'),('b','Journal Alternate','journal'),('c','Conference Main','conference'),('d','Conference Workshop','conference')]}
  self.assertEqual(normalize_venues(db,metadata,'run'),3);self.assertEqual(metadata['a']['formal_venue_id'],metadata['b']['formal_venue_id']);self.assertNotEqual(metadata['c']['formal_venue_id'],metadata['d']['formal_venue_id'])
 def test_identity_merges_versions_not_similar_titles(self):
  db=sqlite3.connect(':memory:');migrate(db);names={'a':'Shared article on antibody engineering','b':'Shared article on antibody engineering','c':'Another article on antibody engineering'}
  rows={pid:{'data':json.dumps({'title':t}),'sha256':None} for pid,t in names.items()};metadata={pid:{'doi':'10.9/'+pid,'authors':['Jane Smith'],'canonical_publication_year':2024,'metadata_candidates':[]} for pid in names};groups,membership,edges=build_identities(db,rows,metadata,{})
  self.assertEqual(len(groups),2);self.assertEqual(membership['a'],membership['b']);self.assertNotEqual(membership['a'],membership['c'])
 def test_future_current_year_needs_publication_review(self):
  p={'title':'Antibody engineering for targeted drug therapy','domains':['生物制药']};r=self.classifier.all(p);n={'document_type':'journal_paper','canonical_publication_year':2026,'formal_publication_status':'published_year_precision_only'}
  e=acceptance(p,n,{'valid_pdf':True,'identity_confirmed':True},r,self.cfg);self.assertFalse(e['eligible']);self.assertIn('current_year_online_publication_not_confirmed',e['issues'])
 def test_fill_preserves_original_and_records_conflict(self):
  folder=self.folder/'crossref';folder.mkdir();title='Antibody engineering for targeted drug therapy';raw={'DOI':'10.9/x','title':[title],'author':[{'given':'Different','family':'Author'}],'abstract':'New abstract','type':'journal-article','container-title':['Example Journal'],'published-online':{'date-parts':[[2024,2,3]]}}
  (folder/'r.json').write_text(json.dumps({'status':200,'doi':'10.9/x','message':raw}),encoding='utf-8');p={'title':title,'doi':'10.9/x','source':'openalex','authors':['Original Author'],'abstract':'Original abstract','year':2024}
  n=Normalizer(audit_folder=self.folder).normalize('p',p,persist=False);self.assertEqual(n['authors'],['Original Author']);self.assertEqual(n['abstract'],'Original abstract');self.assertIn('authors',n['metadata_conflicts']);self.assertIn('abstract',n['metadata_conflicts'])
 def test_annual_planning_prefers_underrepresented_topics(self):
  from collections import Counter
  d=self.cfg['domains'][0];popular,missing=d['topics'][:2];counts=(Counter(),Counter({(d['domain_name'],popular['topic_id'],2022):300}),Counter());base={'domain':d['domain_name'],'lo':2024,'page':0,'priority':0}
  self.assertLess(score_job(dict(base,topic=missing['name']),self.cfg,counts),score_job(dict(base,topic=popular['name']),self.cfg,counts))
 def test_existing_valid_identity_cannot_be_downloaded_again(self):
  from corpus_runner import CorpusRunner
  db=sqlite3.connect(':memory:');migrate(db);db.execute('CREATE TABLE papers(id TEXT)');db.executemany('INSERT INTO papers VALUES(?)',[('existing',),('candidate',)])
  db.executemany('INSERT INTO corpus_v2_status(paper_id,article_identity,downloaded,valid_pdf,relevant,excluded,needs_review) VALUES(?,?,?,?,?,?,?)',[('existing','same',1,1,0,1,0),('candidate','same',0,0,1,0,0)])
  runner=object.__new__(CorpusRunner);self.assertEqual(db.execute('SELECT p.id FROM papers p WHERE 1=1'+runner.corpus_download_filter()).fetchall(),[])
if __name__=='__main__':unittest.main()
