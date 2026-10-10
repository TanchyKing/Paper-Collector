import unittest
from literature_audit import fact,selected_publication,similarity,venue_norm,duplicate_groups,explicit_pdf_genre
class AuditTests(unittest.TestCase):
 def test_flagship_name_is_strict(self):
  self.assertEqual(selected_publication('Nature','journal')[0],'综合性旗舰期刊')
  self.assertNotEqual(selected_publication('Nature Communications','journal')[0],'综合性旗舰期刊')
  self.assertEqual(selected_publication('Nature','preprint')[0],'未列入重点名单')
 def test_workshop_not_main_conference(self):
  self.assertEqual(selected_publication('Proceedings of the Annual Meeting of the Association for Computational Linguistics Workshop','conference')[0],'未列入重点名单')
  self.assertEqual(selected_publication('2022 IEEE International Solid-State Circuits Conference (ISSCC)','conference')[0],'重点会议')
 def test_published_year_not_indexed_year(self):
  r=fact('crossref',{'DOI':'10.1/x','title':['A title'],'type':'journal-article','published-online':{'date-parts':[[2023,12,5]]},'published-print':{'date-parts':[[2024]]},'indexed':{'date-parts':[[2026]]}},{'title':'A title','doi':'10.1/x'})
  self.assertEqual(r['year'],2023);self.assertEqual(r['dates']['published-print'],'2024')
 def test_mismatched_doi_title_not_confirmed(self):
  r=fact('crossref',{'DOI':'10.1/x','title':['Clinical treatment of diabetes']},{'title':'Semiconductor transistor fabrication','doi':'10.1/x'});self.assertFalse(r['identity_confirmed'])
 def test_non_article_conference_chapter_requires_event_evidence(self):
  raw={'DOI':'10.1/x','title':['A title'],'type':'book-chapter','event':{'name':'International Conference on Computer Aided Design'},'container-title':['Lecture Notes']}
  r=fact('crossref',raw,{'title':'A title'});self.assertEqual(r['kind'],'conference');self.assertEqual(r['venue'],raw['event']['name'])
 def test_name_variants_normalize(self):
  self.assertEqual(venue_norm('Scientific Reports'),venue_norm('Scientific reports'))
  self.assertEqual(venue_norm('Light: Science &amp; Applications'),venue_norm('Light: Science & Applications'))
 def test_year_differences_do_not_make_new_work(self):
  rows={str(i):{'p':{'title':'A sufficiently long identical scholarly title','year':y},'sha':'hash'+str(i),'path':''} for i,y in enumerate([2022,2024])}
  self.assertEqual(len(duplicate_groups(rows)),1)
 def test_crossref_subtitle_is_part_of_identity(self):
  r=fact('crossref',{'title':['ALICE'],'subtitle':['An Automatic Design Flow for eFPGA Redaction'],'DOI':'10.1/x'},{'title':'ALICE: An Automatic Design Flow for eFPGA Redaction','doi':'10.1/x'});self.assertTrue(r['identity_confirmed'])
 def test_title_correction_term_not_article_genre(self):
  self.assertEqual(explicit_pdf_genre({'title':'Spectrometer with dispersion correction'},{'header_excerpt':'ARTICLE\nSpectrometer with dispersion\ncorrection\nAuthors'}),'')
  self.assertEqual(explicit_pdf_genre({'title':'Make solar cells stable'},{'header_excerpt':'OBITUARY Another scientist remembered p.162\nMake solar cells stable'}),'')
 def test_explicit_interview_editorial_and_forum(self):
  self.assertEqual(explicit_pdf_genre({'title':'Light People Interview'},{'header_excerpt':'Light People Interview\nEditorial\nSilicon photonics...'}),'editorial')
  self.assertEqual(explicit_pdf_genre({'title':'F5: New architectures'},{'header_excerpt':'This forum discusses advanced architectures'}),'forum_session_overview')
if __name__=='__main__':unittest.main()
