"""Provenance-aware normalization; original papers.data is never rewritten here."""
from __future__ import annotations
import datetime,hashlib,html,json,pathlib,re
from urllib.parse import urlparse
from corpus_store import STATE,load_config,add_candidate,put_metadata,dump
from project_paths import resolve_project_path, pointer_path
def clean(value):return re.sub(r'\s+',' ',re.sub('<[^>]+>',' ',html.unescape(value or ''))).strip()
def dni(value):return re.sub(r'^(?:https?://(?:dx\.)?doi\.org/|doi:\s*)','',(value or '').strip().lower())
def date(value):
    if isinstance(value,int):return str(value) if 1800<=value<=2100 else None
    if not value:return None
    value=str(value).strip();match=re.match(r'^(\d{4})(?:-(\d{1,2})(?:-(\d{1,2}))?)?',value)
    if not match:return None
    year,month,day=match.groups()
    try:
        if month and day:datetime.date(int(year),int(month),int(day))
        elif month and not 1<=int(month)<=12:return None
        elif not 1800<=int(year)<=2100:return None
    except ValueError:return None
    return year+('-'+month.zfill(2) if month else '')+('-'+day.zfill(2) if day else '')
def asof():return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).date().isoformat()
def basic_dates(p):
    raw=p.get('raw_source_evidence') or {};source=p.get('source');result={'source_reported_year':p.get('year'),'source_reported_date':p.get('publication_date')}
    if source=='arxiv':result['first_submission_date']=date(p.get('publication_date'))
    if source=='europe_pmc':result.update(first_publication_date=date(raw.get('firstPublicationDate')),europe_pmc_pub_year=raw.get('pubYear'),europe_pmc_electronic_date=date(raw.get('electronicPublicationDate')))
    return result
class Normalizer:
    def __init__(self,db=None,run_id='',audit_folder=None):
        self.db=db;self.run_id=run_id;self.audit=pathlib.Path(audit_folder) if audit_folder else pointer_path(STATE/'audit/LATEST.txt');self.previous=json.loads((self.audit/'enriched_metadata.json').read_text(encoding='utf-8')) if (self.audit/'enriched_metadata.json').exists() else {}
        self.crossref={}
        for path in (self.audit/'crossref').glob('*.json'):
            r=json.loads(path.read_text(encoding='utf-8'))
            if r.get('status')==200:self.crossref[dni(r['doi'])]=r
        self.supplement=json.loads((self.audit/'supplement_metadata.json').read_text(encoding='utf-8')) if (self.audit/'supplement_metadata.json').exists() else {}
        self.genre=None
    def normalize(self,pid,p,pdf=None,extra_facts=None,persist=True):
        from literature_audit import fact,venue_norm,crossref_title,similarity,explicit_pdf_genre
        original=dni(p.get('doi'));previous=self.previous.get(pid,{})
        facts=list(previous.get('facts') or [])+list(extra_facts or [])
        if p.get('raw_source_evidence'):facts.append(fact(p.get('source'),p['raw_source_evidence'],p))
        cr=self.crossref.get(original);sup=self.supplement.get(pid,{})
        if not cr and sup.get('verified'):cr={'message':sup['message'],'retrieved_at':sup.get('verified_at')}
        if cr:
            f=fact('crossref',cr['message'],p);f.update(evidence_mode='cached_free_api',verified_at=cr.get('retrieved_at'));facts.insert(0,f)
        facts=list({dump(f):f for f in facts}.values());reliable=[f for f in facts if f.get('identity_confirmed')]
        reliable.sort(key=lambda f:(0 if f['provider']=='crossref' and f.get('evidence_mode') in ['cached_free_api','live_free_api'] else 1,{'crossref':0,'openalex':1,'doaj':2,'europe_pmc':3,'semantic_scholar':4}.get(f['provider'],9)))
        candidates=[]
        def add(field,value,source,evidence,confidence=.9):
            if value in [None,'',[],{}]:return
            candidates.append(dict(field=field,value=value,source=source,evidence=evidence,confidence=confidence))
            if self.db and persist:add_candidate(self.db,pid,field,value,source,evidence,confidence)
        provenance={};n={'paper_id':pid,'source_reported_year':p.get('year'),'source_reported_date':p.get('publication_date'),'original_document_type':p.get('document_type'),'original_venue':p.get('venue'),'normalized_title':re.sub(r'[^\w]+',' ',clean(p.get('title')).casefold()).strip()}
        for field in ['doi','authors','abstract','keywords','year','venue','document_type']:
            add('original_'+field,p.get(field),'original:'+str(p.get('source')),{'paper_id':pid,'field':field},.9)
            if field in ['doi','authors','abstract','keywords']:add(field,p.get(field),'original:'+str(p.get('source')),{'paper_id':pid,'role':'trusted_original_value'},.9)
        for f in facts:
            if not f.get('identity_confirmed'):continue
            ev={'url':f.get('evidence_url'),'title_similarity':f.get('title_similarity'),'mode':f.get('evidence_mode','stored_source_evidence')}
            for key,out in [('doi','doi'),('venue','formal_venue' if f.get('kind') in ['journal','conference'] else 'repository'),('issn','issn'),('publisher','publisher'),('kind','document_type')]:add(out,f.get(key),f['provider'],ev,.96 if f['provider']=='crossref' else .9)
        # Trusted originals remain the canonical non-date values; supplementary candidates are explicit.
        n['doi']=original or next((f['doi'] for f in reliable if f.get('doi')),'')
        provenance['doi']={'source':'original:'+str(p.get('source')) if original else next((f['provider'] for f in reliable if f.get('doi')),'unknown'),'confidence':.92 if n['doi'] else 0}
        for field in ['authors','abstract','keywords']:
            n[field]=p.get(field) or ([] if field!='abstract' else '')
            provenance[field]={'source':'original:'+str(p.get('source')),'confidence':.9} if p.get(field) else {'source':'unknown','confidence':0}
        raw=p.get('raw_source_evidence') or {};source=p.get('source')
        if cr and similarity(p.get('title'),crossref_title(cr['message']))>=.8:
            work=cr['message'];vals={'authors':[' '.join(v for v in [a.get('given'),a.get('family')] if v) or a.get('name','') for a in work.get('author',[])],'abstract':clean(work.get('abstract') or ''),'keywords':work.get('subject') or []}
            for field,val in vals.items():
                add(field,val,'crossref',{'doi':dni(work.get('DOI')),'cache':str(self.audit)},.96)
                if not n[field] and val:n[field]=val;provenance[field]={'source':'crossref_cached','confidence':.96}
        rawkeywords=[];academic=[]
        for f in reliable:
            for t in f.get('topics') or []:
                academic.append(t.get('display_name',''));academic.extend((t.get(k) or {}).get('display_name','') for k in ['subfield','field','domain'])
        if source=='openalex':rawkeywords=[v.get('display_name') for v in raw.get('keywords',[]) if v.get('display_name')]+[v.get('display_name') for v in raw.get('concepts',[]) if v.get('display_name')]
        if not n['keywords'] and rawkeywords:n['keywords']=rawkeywords;provenance['keywords']={'source':'openalex_cached_keywords/concepts','confidence':.88}
        add('keywords',rawkeywords,'openalex_source',{'paper_id':pid},.88);n['academic_classification']=sorted(set(t for t in academic if t))
        # Dates are modeled separately. FIRST_PDATE and pubYear never share a field.
        choices=[]
        def dadd(key,value,provider,weight):
            value=date(value)
            if not value:return
            add(key,value,provider,{'paper_id':pid,'date_role':key},weight);choices.append((key,value,provider,weight))
        if source=='arxiv':dadd('first_submission_date',p.get('publication_date'),'arxiv.first_submission',.99)
        if source=='europe_pmc':
            dadd('first_publication_date',raw.get('firstPublicationDate'),'europe_pmc.firstPublicationDate',.95);dadd('published_online_date',raw.get('electronicPublicationDate'),'europe_pmc.electronicPublicationDate',.94)
            n['europe_pmc_pub_year']=raw.get('pubYear');add('source_reported_year',raw.get('pubYear'),'europe_pmc.pubYear',{'role':'volume_or_source_year'},.9)
        for f in reliable:
            ds=f.get('dates') or {};provider=f['provider']
            mapping={'published-online':('published_online_date',.99),'published-print':('published_print_date',.96),'published':('first_publication_date',.92),'posted':('first_submission_date',.97),'firstPublicationDate':('first_publication_date',.95),'electronicPublicationDate':('published_online_date',.94),'publication_date':('first_publication_date',.87),'publicationDate':('first_publication_date',.86),'bibjson_year_month':('source_reported_date',.8)}
            for k,(out,weight) in mapping.items():
                if k in ds:dadd(out,ds[k],provider+'.'+k,weight)
            add('source_reported_year',f.get('year'),provider+'.reported_year',{'url':f.get('evidence_url')},.9)
        for key in ['first_submission_date','first_publication_date','published_online_date','published_print_date']:
            pool=[v for v in choices if v[0]==key];selected=max(pool,key=lambda c:(c[3],len(c[1]))) if pool else None
            n[key]=selected[1] if selected else None
            if selected:provenance[key]={'source':selected[2],'confidence':selected[3]}
        formal=[f for f in reliable if f.get('kind') in ['journal','conference'] and f.get('venue')]
        publication=formal[0] if formal else None
        n['formal_venue']=html.unescape(publication['venue']) if publication else None;n['formal_publication_type']=publication['kind'] if publication else None
        n['issn']=sorted({s for f in reliable if publication and venue_norm(f.get('venue') or '')==venue_norm(publication['venue']) for s in f.get('issn') or []})
        n['publisher']=next((f['publisher'] for f in reliable if f.get('publisher') and publication and venue_norm(f.get('venue') or '')==venue_norm(publication['venue'])),None)
        n['repository_operator']=next((f['publisher'] for f in reliable if f.get('publisher') and f.get('kind') in ['repository','preprint']),None)
        n['repository']=previous.get('publication_name') if previous.get('publication_kind')=='preprint' else None
        if not n['repository']:n['repository']=next((f['venue'] for f in reliable if f.get('kind') in ['repository','preprint'] and f.get('venue')),None)
        aid=next((m.group(1) for s in [p.get('source_id'),p.get('url'),*p.get('pdf_urls',[])] if s and (m:=re.search(r'(\d{4}\.\d{4,5}|[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?',str(s))) and ('arxiv' in str(s).lower() or p.get('source')=='arxiv')),'')
        n['arxiv_id']=aid;n['pdf_host']=previous.get('download_host') or urlparse(p.get('downloaded_from') or '').hostname or None
        n['preprint_source']=('arXiv' if aid else n['repository']) if aid or any(f.get('kind')=='preprint' for f in reliable) else None
        if source=='arxiv':n['preprint_source']='arXiv';n['repository']='arXiv'
        preprint=bool(aid or str(p.get('document_type')).lower() in ['preprint','posted-content'] or previous.get('downloaded_version')=='preprint')
        if any(f.get('kind')=='preprint' for f in reliable) and not publication:preprint=True
        formal_pdf_header=bool(pdf and publication and venue_norm(publication['venue']) in venue_norm(pdf.get('header_excerpt') or '') and n.get('doi') and n['doi'] in (pdf.get('front_text') or '').lower())
        verified_formal_file=formal_pdf_header and n['pdf_host'] in ['www.nature.com','www.frontiersin.org','link.springer.com','www.aclweb.org','aclanthology.org']
        n['downloaded_version']='formal_publication_copy' if verified_formal_file else 'preprint_or_author_manuscript' if preprint else 'formal_publication_copy' if formal_pdf_header else 'repository_or_publisher_copy_unknown_version' if n['pdf_host'] else 'unknown'
        n['downloaded_version_evidence']={'preprint_source_or_id':n.get('preprint_source'),'formal_venue_and_doi_in_pdf_header':formal_pdf_header,'host_is_not_version_evidence':True}
        n['conference_track']='workshop' if n['formal_venue'] and 'workshop' in n['formal_venue'].lower() else next((x for x in ['demo','companion','short paper','software engineering in practice','software engineering in society','findings','tutorial'] if n['formal_venue'] and x in n['formal_venue'].lower()),'main_or_unspecified' if n['formal_publication_type']=='conference' else None)
        typ=str(p.get('document_type') or '').lower();n['document_type']='preprint' if not publication and (preprint or any(f.get('kind')=='preprint' for f in reliable)) else 'conference_paper' if publication and publication['kind']=='conference' else 'review' if 'review' in typ else 'journal_paper' if publication and publication['kind']=='journal' else 'unknown'
        verified_conference=any(f['provider']=='crossref' and f.get('kind')=='conference' and f.get('identity_confirmed') for f in reliable)
        if any(f.get('kind')=='non_article' and f.get('identity_confirmed') and not (verified_conference and f['provider']=='openalex' and f.get('raw_type')=='book-chapter') for f in reliable):n['document_type']='non_article_requires_review'
        if pdf and re.search(r'\bencyclopedia\b',pdf.get('header_excerpt','')[:1500],re.I):n['document_type']='book_chapter'
        genre=explicit_pdf_genre(p,pdf or {}) if pdf else '';n['pdf_explicit_genre']=genre
        # Prefer confirmed already-published online dates. A future issue is not online evidence.
        today=asof();formal_pool=[c for c in choices if c[0] in ['published_online_date','first_publication_date','published_print_date'] and c[1]<=today]
        order={'published_online_date':0,'first_publication_date':1,'published_print_date':2}
        canonical=min(formal_pool,key=lambda c:(order[c[0]],-c[3],-len(c[1]))) if publication and formal_pool else None
        if not canonical and n['first_submission_date'] and n['first_submission_date']<=today:canonical=('first_submission_date',n['first_submission_date'],provenance['first_submission_date']['source'],.9)
        if not canonical and not publication:
            pool=[c for c in choices if c[0] in ['first_publication_date','published_online_date','source_reported_date'] and c[1]<=today]
            canonical=max(pool,key=lambda c:c[3]) if pool else None
        if not canonical and p.get('year') and int(p['year'])<=int(today[:4]) and not any(c[1]>today for c in choices):canonical=('source_reported_year',str(p['year']),'original:'+str(source),.65)
        n['canonical_publication_year']=int(canonical[1][:4]) if canonical else None;n['date_source']=canonical[2] if canonical else 'unknown_or_future_schedule';n['canonical_date_role']=canonical[0] if canonical else None
        online_proof=any(c[0] in ['published_online_date','first_publication_date'] and (len(c[1])==10 or c[1][:7]<today[:7] if len(c[1])>=7 else c[1][:4]<today[:4]) for c in formal_pool)
        n['formal_publication_status']='confirmed_online_or_published' if publication and online_proof else 'published_year_precision_only' if publication and formal_pool else 'future_scheduled_or_unconfirmed' if publication else 'preprint_only' if preprint or n['preprint_source'] else 'unknown'
        n['future_issue_or_date']=any(c[1]>today for c in choices if c[0] in ['published_online_date','published_print_date','first_publication_date'])
        n['date_conflicts']=[{'field':key,'candidates':[dict(date=c[1],source=c[2],confidence=c[3]) for c in choices if c[0]==key]} for key in ['first_submission_date','first_publication_date','published_online_date','published_print_date'] if len({c[1] for c in choices if c[0]==key})>1]
        if n['canonical_publication_year'] and p.get('year') and n['canonical_publication_year']!=p['year']:n['date_conflicts'].append({'field':'canonical_vs_original_year','original':p['year'],'canonical':n['canonical_publication_year'],'source':n['date_source']})
        n['field_provenance']=provenance;n['metadata_candidates']=candidates;n['metadata_conflicts']={field:[c for c in candidates if c['field']==field] for field in ['doi','authors','abstract','formal_venue','publisher','document_type'] if len({dump(c['value']) for c in candidates if c['field']==field})>1}
        n['identity_metadata_conflict']=bool(cr and similarity(p.get('title'),crossref_title(cr['message']))<.5)
        n['version_relations']=[{'relation_doi':related,'source':f.get('evidence_url'),'confidence':.98,'relation_type':'verified_related_version'} for f in reliable for related in f.get('related_dois') or []]
        if cr:
            for kind,rels in (cr['message'].get('relation') or {}).items():
                if kind not in ['is-preprint-of','has-preprint','is-version-of','has-version','is-identical-to']:continue
                for rel in rels:
                    if rel.get('id-type')=='doi':n['version_relations'].append({'relation_doi':dni(rel['id']),'source':'https://api.crossref.org/works/'+dni(cr['message'].get('DOI')),'confidence':.99,'relation_type':kind})
        for field in ['formal_venue','issn','publisher','document_type']:
            selected=next((c for c in candidates if c['field']==field and c['value']),None)
            provenance[field]={'source':selected['source'],'confidence':selected['confidence'],'evidence':selected['evidence']} if selected else {'source':'unknown','confidence':0}
        n['external_formal_doi']=next((f['doi'] for f in formal if f.get('doi')),None)
        # Retain verified V3 repair evidence when the existing pipeline refreshes this work.
        evidence_path=STATE/'repair_v3/metadata_evidence.json'
        if not hasattr(self,'v3_evidence'):
            self.v3_evidence=json.loads(evidence_path.read_text(encoding='utf-8')) if evidence_path.exists() else {}
        from corpus_v3 import extend_metadata
        n=extend_metadata(p,n,self.v3_evidence.get(pid))
        continuous_evidence=STATE/'continuous_metadata'/(pid+'.json')
        if continuous_evidence.exists():n=extend_metadata(p,n,json.loads(continuous_evidence.read_text(encoding='utf-8')))
        if self.db and persist:put_metadata(self.db,pid,n,self.run_id)
        return n
