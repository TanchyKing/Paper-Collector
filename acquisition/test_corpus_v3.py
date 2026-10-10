import copy,json,pathlib,sqlite3,tempfile,unittest
from types import SimpleNamespace
from unittest.mock import patch
from corpus_store import load_config,migrate,version
from corpus_v3 import extend_metadata
from corpus_runner import CorpusRunner
from corpus_planner import annual_jobs,seed_annual_jobs
from project_paths import ROOT,resolve_project_path
from fulltext_xml import verify_jats
from literature_audit import explicit_pdf_genre,fact
from collect import paper_keys

class Tests(unittest.TestCase):
    def test_formal_work_keeps_arxiv_file(self):
        p={'downloaded_from':'https://arxiv.org/pdf/1605.01680','document_type':'preprint'}
        old={'doi':'','arxiv_id':'1605.01680','first_submission_date':'2016-05-05','downloaded_version':'preprint_or_author_manuscript','issn':[]}
        ev={'crossref':{'DOI':'10.1063/1.4948770','type':'journal-article','container-title':['Applied Physics Letters'],'published-online':{'date-parts':[[2016,5,5]]},'published-print':{'date-parts':[[2016,5,2]]},'volume':'108','article-number':'183509'},'source':'https://api.crossref.org/works/10.1063/1.4948770','title_similarity':.9,'author_overlap':1,'arxiv_relation':{'source':'https://arxiv.org/abs/1605.01680'}}
        n=extend_metadata(p,old,ev)
        self.assertEqual(n['publication_status'],'published');self.assertEqual(n['local_file_version'],'arXiv/preprint');self.assertEqual(n['formal_doi'],'10.1063/1.4948770');self.assertEqual(n['published_print_date'],'2016-05-02');self.assertEqual(n['published_online_date'],'2016-05-05');self.assertEqual(n['first_public_date'],'2016-05-05');self.assertEqual(old['doi'],'')

    def test_no_formal_metadata_is_invented(self):
        n=extend_metadata({'downloaded_from':'https://arxiv.org/pdf/1'},{'preprint_source':'arXiv','downloaded_version':'preprint_or_author_manuscript'})
        self.assertEqual(n['publication_status'],'preprint_only');self.assertIsNone(n['formal_doi']);self.assertNotIn('publisher',n)
    def test_acquisition_quota_does_not_invalidate_taxonomy(self):
        cfg=load_config();before=version(cfg);changed=copy.deepcopy(cfg);changed['domains'][0]['target_years']=[2023];changed['domains'][0]['target_count']=99;changed['batch']={'topics_by_domain':{changed['domains'][0]['domain_name']:['biosimilars']}}
        self.assertEqual(before,version(changed));self.assertEqual(len(changed['domains'][0]['topics']),len(cfg['domains'][0]['topics']))

    def test_year_only_current_year_does_not_confirm_publication(self):
        n=extend_metadata({}, {'formal_venue':'Journal','formal_publication_status':'published_year_precision_only'})
        self.assertEqual(n['publication_status'],'publication_unconfirmed')
    def test_crossref_year_does_not_erase_verified_epmc_day(self):
        old={'published_online_date':'2026-10-02','issn':[],'metadata_candidates':[]}
        ev={'crossref':{'DOI':'10.1/a','type':'journal-article','container-title':['Journal'],'published-online':{'date-parts':[[2026]]}},'source':'crossref','title_similarity':1,'author_overlap':1}
        n=extend_metadata({},old,ev)
        self.assertEqual(n['published_online_date'],'2026-10-02');self.assertEqual(n['publication_status'],'published')

    def test_old_and_relative_paths_resolve_without_db_rewrite(self):
        p=ROOT/'acquisition/corpus_config.json';self.assertEqual(resolve_project_path('acquisition/corpus_config.json'),p)
        self.assertEqual(resolve_project_path(r'X:\old\papers\acquisition\corpus_config.json'),p)

    def test_cap_counts_inflight_claims(self):
        r=object.__new__(CorpusRunner);r.args=SimpleNamespace(max_new_pdfs=60);r.completed=58;r.db=sqlite3.connect(':memory:');r.db.execute('CREATE TABLE papers(status TEXT)');r.db.executemany('INSERT INTO papers VALUES(?)',[('downloading',),('downloading',)])
        with patch('download_engine.OptimizedRunner.claim',side_effect=AssertionError('must not claim')):self.assertIsNone(r.claim())
    def test_strong_index_identifiers_join_work_before_file_dedup(self):
        self.assertIn('openalex:W12345',paper_keys({'source':'openalex','source_id':'https://openalex.org/W12345','title':'T'}))
        keys=paper_keys({'source':'europe_pmc','raw_source_evidence':{'id':'12345','source':'MED','pmcid':'PMC123'},'title':'T'})
        self.assertIn('pmid:12345',keys);self.assertIn('pmcid:PMC123',keys)
    def test_wrong_xml_identity_cannot_enter_corpus(self):
        xml='<article><front><article-meta><article-id pub-id-type="doi">10.1/other</article-id><title-group><article-title>Wrong unrelated title</article-title></title-group></article-meta></front></article>'
        with self.assertRaises(ValueError):verify_jats(xml,{'title':'Original antibody engineering research','doi':'10.1/right'})
    def test_arxiv_dissertation_is_not_a_research_article(self):
        header='Quantum Network Protocols\nA Dissertation\nSubmitted to the Graduate Faculty\nin partial fulfillment of the requirements for the degree'
        self.assertEqual(explicit_pdf_genre({'title':'Quantum Network Protocols'},{'header_excerpt':header}),'dissertation')
    def test_book_chapter_is_not_made_journal_by_openalex_source(self):
        raw={'display_name':'A technical research chapter','type':'book-chapter','primary_location':{'source':{'type':'journal','display_name':'Biochemistry'}}}
        self.assertEqual(fact('openalex',raw,{'title':raw['display_name']})['kind'],'non_article')
        n=extend_metadata({'document_type':'book-chapter'},{'formal_venue':'Biochemistry','formal_publication_type':'journal','formal_publication_status':'confirmed_online_or_published'})
        self.assertEqual(n['document_type'],'book_chapter');self.assertIsNone(n['formal_venue'])

    def test_narrow_config_does_not_execute_unselected_old_queries(self):
        db=sqlite3.connect(':memory:');db.executescript("CREATE TABLE jobs(id TEXT PRIMARY KEY,domain TEXT,topic TEXT,source TEXT,lo INTEGER,hi INTEGER,sort TEXT,cursor TEXT,page INTEGER,priority REAL,status TEXT DEFAULT 'pending',attempts INTEGER DEFAULT 0,next_retry REAL DEFAULT 0,error TEXT,total INTEGER,created TEXT,updated TEXT);");migrate(db)
        cfg=load_config();cfg['continuous']={'enabled':False};cfg['domains']=copy.deepcopy(cfg['domains'][:1]);cfg['domains'][0]['topics']=cfg['domains'][0]['topics'][:2];cfg['domains'][0]['target_years']=[2023];seed_annual_jobs(db,cfg)
        small=copy.deepcopy(cfg);small['domains'][0]['topics']=small['domains'][0]['topics'][:1];small['sampling']['providers']=['openalex'];jobs=annual_jobs(db,small)
        self.assertTrue(jobs);self.assertTrue(all(j['source']=='openalex' for j in jobs));self.assertTrue(all(j['topic'] in small['domains'][0]['topics'][0]['queries'] for j in jobs))

if __name__=='__main__':unittest.main()
