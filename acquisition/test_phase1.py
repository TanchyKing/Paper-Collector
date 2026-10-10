import unittest,tempfile,hashlib
from pathlib import Path
import pymupdf as fitz
from phase1_audit import PhaseRegistry,DOMAINS,TOPICS,signature
from phase1_runner import PhaseRunner

def registry():
    r=PhaseRegistry.__new__(PhaseRegistry);r.rows={};r.checks={};r.cached_summary=None
    r.config={'target_unique_papers':4,'minimum_per_domain':1,'minimum_topics_per_domain':1,'minimum_papers_per_topic':1}
    for i,d in enumerate(DOMAINS):
        pid=str(i);r.rows[pid]={'p':{'title':'A unique scholarly title for domain '+str(i),'doi':'10.9999/'+str(i),'domains':[d],'topics':[TOPICS[d][0][0]],'year':2020,'document_type':'journal_article','authors':['Author']},'sha':'hash'+str(i),'path':''};r.checks[pid]={'valid':True,'relevant_domains':[d]}
    return r

class Tests(unittest.TestCase):
    def test_total_and_each_domain_must_both_pass(self):
        r=registry();self.assertTrue(r.summary()['goal_met'])
        r.rows['3']['p']['domains']=[DOMAINS[0]];r.checks['3']['relevant_domains']=[DOMAINS[0]];r.cached_summary=None
        self.assertEqual(r.summary()['valid_unique_papers'],4);self.assertFalse(r.summary()['goal_met'])
    def test_doi_duplicates_not_counted_twice(self):
        r=registry();r.rows['3']['p']['doi']=r.rows['0']['p']['doi']
        self.assertEqual(r.summary()['valid_unique_papers'],3);self.assertFalse(r.summary()['goal_met'])
    def test_hash_duplicates_not_counted_twice(self):
        r=registry();r.rows['3']['sha']=r.rows['0']['sha'];self.assertEqual(r.summary()['valid_unique_papers'],3)
    def test_topic_breadth_prevents_hot_topic_quota_only(self):
        r=registry();r.config['minimum_topics_per_domain']=2;self.assertFalse(r.summary()['goal_met'])
    def test_invalid_pdf_not_counted(self):
        r=registry();r.checks['3']['valid']=False;self.assertEqual(r.summary()['valid_unique_papers'],3)
    def test_target_stops_new_task_claiming(self):
        runner=PhaseRunner.__new__(PhaseRunner);runner.phase=registry();runner.stop=False;runner.phase_reason=None
        self.assertTrue(runner.check_phase_goal());self.assertTrue(runner.stop);self.assertEqual(runner.phase_reason,'phase1_target_reached');self.assertIsNone(runner.claim())
    def identity_pdf(self,front_has_title):
        title='Engineered antibody therapeutics for selective cancer treatment'
        p=dict(title=title,doi='10.9999/identity',document_type='journal_article',eligible=True,domains=[DOMAINS[0]],authors=['Author'],abstract=title,keywords=[])
        body='Scientific experimental measurements and methods describe quantitative results. '*12
        pdf=fitz.open()
        for i in range(5):
            text='Repository permission and license information. '+body
            if i==1 and front_has_title or i==4:text=title+'\nAuthor\n'+body
            page=pdf.new_page();page.insert_textbox(fitz.Rect(40,40,550,750),text,fontsize=11)
        content=pdf.tobytes();pdf.close()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'sample.pdf';path.write_bytes(content)
            return PhaseRegistry.__new__(PhaseRegistry).inspect(p,str(path),hashlib.sha256(content).hexdigest())
    def test_repository_cover_followed_by_matching_title_passes(self):
        result=self.identity_pdf(True);self.assertTrue(result['valid'],result);self.assertEqual(result['identity_page'],2)
    def test_title_only_in_last_page_references_fails(self):
        result=self.identity_pdf(False);self.assertFalse(result['valid']);self.assertTrue(result['error'].startswith('title_mismatch:') or result['error']=='identity_not_confirmed_on_front_pages',result)
    def test_subject_metadata_change_invalidates_cached_check(self):
        p={'title':'Test','domains':[DOMAINS[0]]};before=signature(p);p['domains']=[DOMAINS[1]];self.assertNotEqual(before,signature(p))

if __name__=='__main__':unittest.main()
