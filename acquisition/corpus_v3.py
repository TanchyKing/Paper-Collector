"""Incremental repair and audit over V2; does not download papers or rewrite raw tables."""
from __future__ import annotations
import argparse, collections, copy, hashlib, html, json, os, pathlib, re, sqlite3, time
import xml.etree.ElementTree as ET
import httpx
from corpus_store import ROOT, STATE, dump, now, load_config, put_metadata, add_candidate, put_relevance, version, csvwrite, atomic_json
from corpus_rules import Classifier
from corpus_repair import acceptance, author_keys
from project_paths import pointer_path
from literature_audit import fact, similarity, crossref_title, dates_crossref

WORK=STATE/'repair_v3'
WORK.mkdir(exist_ok=True)
RUN='corpus-v3-incremental'

def rows(db):
    return [(pid,json.loads(p),json.loads(n),dict(zip(['article_identity','primary_state','exclusion_reason','domains','topics'],s)))
            for pid,p,n,*s in db.execute('SELECT p.id,p.data,m.normalized,s.article_identity,s.primary_state,s.exclusion_reason,s.domains,s.topics FROM papers p JOIN corpus_v2_metadata m ON m.paper_id=p.id JOIN corpus_v2_status s ON s.paper_id=p.id')]

def known_crossref():
    result={}
    for folder in [pointer_path(STATE/'audit/LATEST.txt')/'crossref',WORK/'crossref']:
        for path in folder.glob('*.json'):
            obj=json.loads(path.read_text(encoding='utf8'));raw=obj.get('message')
            if obj.get('status')==200 and isinstance(raw,dict):result[(raw.get('DOI') or obj.get('doi') or '').lower()]=raw
    supplement=pointer_path(STATE/'audit/LATEST.txt')/'supplement_metadata.json'
    if supplement.exists():
        for obj in json.loads(supplement.read_text(encoding='utf8')).values():
            if obj.get('verified') and obj.get('message',{}).get('DOI'):result[obj['message']['DOI'].lower()]=obj['message']
    return result

def enrich(db,limit):
    """arXiv ID metadata batches; retain raw Atom and public API Crossref evidence."""
    items=rows(db);ids=sorted({n['arxiv_id'] for pid,p,n,s in items if n.get('arxiv_id') and not n.get('formal_venue')
                            and db.execute('SELECT status FROM papers WHERE id=?',(pid,)).fetchone()[0]=='downloaded'})
    folder=WORK/'arxiv';folder.mkdir(exist_ok=True);(WORK/'crossref').mkdir(exist_ok=True)
    doi_map={};attempts=[]
    with httpx.Client(timeout=30,follow_redirects=True,headers={'User-Agent':'corpus-repair/3.0 (public scholarly metadata)'}) as client:
        for offset in range(0,len(ids),100):
            if (WORK/'STOP').exists():break
            batch=ids[offset:offset+100];key=hashlib.sha256(dump(batch).encode()).hexdigest();path=folder/(key+'.xml')
            if not path.exists():
                try:
                    response=client.get('https://export.arxiv.org/api/query',params={'id_list':','.join(batch),'max_results':100})
                    attempts.append({'source':'arxiv','status':response.status_code,'ids':batch,'time':now()});response.raise_for_status()
                    ET.fromstring(response.text);path.write_text(response.text,encoding='utf8')
                except Exception as error:
                    atomic_json(WORK/'enrichment_failure.json',{'source':'arxiv','error':type(error).__name__,'at':now()})
                    print(dump({'event':'metadata_source_unavailable','source':'arxiv','error':type(error).__name__}),flush=True);break
                time.sleep(6)
            ns={'a':'http://www.w3.org/2005/Atom','x':'http://arxiv.org/schemas/atom'}
            for entry in ET.fromstring(path.read_text(encoding='utf8')).findall('a:entry',ns):
                aid=(entry.findtext('a:id','',ns)).split('/abs/')[-1];aid=re.sub(r'v\d+$','',aid)
                doi=(entry.findtext('x:doi','',ns) or '').lower().strip()
                if doi:doi_map[aid]={'doi':doi,'journal_ref':entry.findtext('x:journal_ref','',ns),'source':'https://arxiv.org/abs/'+aid,'raw_cache':str(path.relative_to(ROOT))}
            print(dump({'event':'arxiv_metadata_scan','processed':min(offset+100,len(ids)),'total':len(ids),'doi_relations':len(doi_map)}),flush=True)
        # The independently fetched official arXiv page also supplies a verified relation.
        case=STATE/'migration_v3/case_arxiv.html'
        if case.exists() and '10.1063/1.4948770' in case.read_text(encoding='utf8'):
            doi_map['1605.01680']={'doi':'10.1063/1.4948770','journal_ref':'Appl. Phys. Lett. 108, 183509 (2016)','source':'https://arxiv.org/abs/1605.01680','raw_cache':'acquisition/migration_v3/case_arxiv.html'}
        available=known_crossref();requests=0
        casecr=STATE/'migration_v3/case_crossref.json'
        if casecr.exists():
            msg=json.loads(casecr.read_text(encoding='utf8')).get('message',{});
            if msg.get('DOI'):available[msg['DOI'].lower()]=msg
        candidates=[]
        from corpus_repair import load_pdf_cache
        pdfs=load_pdf_cache()
        for pid,p,n,s in items:
            if pid not in pdfs:continue
            rel=doi_map.get(n.get('arxiv_id'))
            dois=[rel['doi']] if rel else []
            # A PDF DOI is only a lookup candidate, never publication proof by itself.
            if not n.get('formal_venue'):
                for doi in re.findall(r'10\.\d{4,9}/[^\s<>\"\[\]]+',pdfs[pid].get('front_text','')[:4000]):
                    doi=doi.rstrip('.,;:)').lower()
                    if not any(x in doi for x in ['xxxx','nnnn','10.48550/']):dois.append(doi)
            if n.get('doi'):dois.append(n['doi'])
            candidates.append((pid,p,n,rel,list(dict.fromkeys(dois))))
        evidence={}
        baseline=STATE/'migration_v3/pdf_preservation.json'
        original_ids={x['id'] for x in json.loads(baseline.read_text(encoding='utf8'))} if baseline.exists() else set()
        candidates.sort(key=lambda item:item[0] in original_ids)
        for pid,p,n,rel,dois in candidates:
            if (WORK/'STOP').exists():break
            for doi in dois:
                key=hashlib.sha256(doi.encode()).hexdigest();path=WORK/'crossref'/(key+'.json')
                if doi not in available and not path.exists() and requests<limit:
                    try:
                        response=client.get('https://api.crossref.org/works/'+doi);requests+=1
                        obj={'doi':doi,'status':response.status_code,'retrieved_at':now()}
                        if response.status_code==200:obj['message']=response.json()['message'];available[doi]=obj['message']
                        atomic_json(path,obj);attempts.append({'source':'crossref','doi':doi,'status':response.status_code,'time':now()})
                        if response.status_code==429:break
                    except Exception as error:
                        attempts.append({'source':'crossref','doi':doi,'error':type(error).__name__,'time':now()})
                    time.sleep(1.6)
                raw=available.get(doi)
                if not raw:continue
                score=similarity(p.get('title'),crossref_title(raw));aa=author_keys(p.get('authors'));bb=author_keys([' '.join([a.get('given',''),a.get('family','')]) for a in raw.get('author',[])])
                overlap=len(aa&bb)/min(len(aa),len(bb)) if aa and bb else 0
                if score<.8 or (rel and overlap<.5):continue
                original_doi=(p.get('doi') or '').lower().removeprefix('https://doi.org/')
                if doi!=original_doi and overlap<.5:continue
                evidence[pid]={'crossref':raw,'title_similarity':score,'author_overlap':overlap,'arxiv_relation':rel,'retrieved_at':now(),'source':'https://api.crossref.org/works/'+doi}
                break
        old=json.loads((WORK/'metadata_evidence.json').read_text(encoding='utf8')) if (WORK/'metadata_evidence.json').exists() else {}
        old.update(evidence);atomic_json(WORK/'metadata_evidence.json',old)
    with (WORK/'api_attempts.jsonl').open('a',encoding='utf8') as f:
        for obj in attempts:f.write(dump(obj)+'\n')
    print(dump({'event':'metadata_enrichment','evidence_records':len(old),'new_requests':requests,'arxiv_relations':len(doi_map)}),flush=True)

def extend_metadata(p,n,evidence=None):
    n=copy.deepcopy(n);ev=evidence or {};raw=ev.get('crossref') or {}
    if raw:
        doi=raw.get('DOI','').lower();dates=dates_crossref(raw)
        source={'source':ev['source'],'confidence':.98,'title_similarity':ev['title_similarity'],'author_overlap':ev['author_overlap'],'arxiv_relation':ev.get('arxiv_relation')}
        conference_chapter=raw.get('type')=='book-chapter' and bool((raw.get('event') or {}).get('name'))
        formal=raw.get('type') in ['journal-article','proceedings-article'] or conference_chapter
        if raw.get('type') in ['book','book-chapter','dataset','dissertation','component'] and not (raw.get('event') or {}).get('name'):
            n['document_type']={'book-chapter':'book_chapter','component':'non_article_requires_review'}.get(raw['type'],raw['type']);n.setdefault('field_provenance',{})['document_type']=source
        if formal:
            n.update(formal_venue=raw['event']['name'] if conference_chapter else (raw.get('container-title') or [None])[0],external_formal_doi=doi,formal_doi=doi,
                     formal_publication_type='conference' if raw['type']=='proceedings-article' or conference_chapter else 'journal')
            if not n.get('doi'):n['doi']=doi
            n['issn']=list(dict.fromkeys([*n.get('issn',[]),*raw.get('ISSN',[])]));n['publisher']=raw.get('publisher') or n.get('publisher')
            for field in ['formal_venue','formal_doi','issn','publisher']:n.setdefault('field_provenance',{})[field]=source
            if n.get('doi')==doi:n.setdefault('field_provenance',{})['doi']=source
            for role,key in [('published_online_date','published-online'),('published_print_date','published-print')]:
                if dates.get(key) and len(dates[key])>=len(n.get(role) or ''):n[role]=dates[key];n.setdefault('field_provenance',{})[role]=source
                # A lower-precision Crossref date cannot erase previously verified day-level evidence.
                preserved=[c for c in n.get('metadata_candidates',[]) if c['field']==role and isinstance(c['value'],str)]
                if preserved:
                    best=max(preserved,key=lambda c:(len(c['value']),c['confidence']))
                    if len(best['value'])>len(n.get(role) or ''):n[role]=best['value'];n.setdefault('field_provenance',{})[role]={'source':best['source'],'confidence':best['confidence'],'evidence':best['evidence']}
            # Preserve V2 online-first policy and date-role differences, including preprint first public date.
            confirmed=[(key,dates[key]) for key in ['published-online','published','published-print'] if dates.get(key) and dates[key]<=now()[:10]]
            if n.get('published_online_date') and n['published_online_date']<=now()[:10]:confirmed.insert(0,('published-online',n['published_online_date']))
            elif n.get('first_publication_date') and n['first_publication_date']<=now()[:10] and len(n['first_publication_date'])>=7:confirmed.insert(0,('published',n['first_publication_date']))
            if confirmed:
                chosen=confirmed[0];n['formal_publication_date']=chosen[1];n['canonical_publication_year']=int(chosen[1][:4]);n['date_source']='crossref.'+chosen[0];n['canonical_date_role']={'published-online':'published_online_date','published':'first_publication_date','published-print':'published_print_date'}[chosen[0]]
                n['formal_publication_status']='confirmed_online_or_published' if len(chosen[1])>=7 or chosen[1][:4]<now()[:4] else 'published_year_precision_only'
            n['document_type']='conference_paper' if raw['type']=='proceedings-article' or conference_chapter else 'review' if p.get('document_type')=='review' else 'journal_paper'
        for out,key in [('volume','volume'),('issue','issue'),('pages','page'),('article_number','article-number')]:
            if raw.get(key):n[out]=raw[key];n.setdefault('field_provenance',{})[out]=source
        if not n.get('keywords') and raw.get('subject'):n['keywords']=raw['subject'];n.setdefault('field_provenance',{})['keywords']=source
        n['crossref_id']=doi;n['crossref_resource']=ev['source']
        n['licenses']=raw.get('license') or n.get('licenses') or []
        if ev.get('arxiv_relation'):
            relation={'relation_doi':doi,'relation_type':'is-preprint-of','source':ev['arxiv_relation']['source'],'confidence':.99,'identity_evidence':source}
            n['version_relations']=list({dump(x):x for x in [*n.get('version_relations',[]),relation]}.values())
    raw_type=(p.get('raw_source_evidence') or {}).get('type') or p.get('document_type')
    if raw_type in ['book-chapter','book','dataset','dissertation'] and n.get('formal_publication_type')!='conference':
        n['document_type']={'book-chapter':'book_chapter'}.get(raw_type,raw_type)
        n['book_or_nonarticle_source']=n.get('book_or_nonarticle_source') or n.get('formal_venue');n['formal_venue']=None;n['canonical_formal_venue']=None;n['formal_publication_type']=None
    n['formal_doi']=n.get('formal_doi') or n.get('external_formal_doi') if n.get('formal_venue') else None
    n['publication_status']='published' if n.get('formal_venue') and n.get('formal_publication_status')=='confirmed_online_or_published' else 'publication_unconfirmed' if n.get('formal_venue') else 'preprint_only' if n.get('preprint_source') else 'unknown'
    n['local_file_version']=n.get('downloaded_version') or 'unknown'
    if 'arxiv.org' in (p.get('downloaded_from') or ''):
        n['repository']='arXiv'
        if n.get('downloaded_version')!='formal_publication_copy':n['local_file_version']='arXiv/preprint';n['downloaded_version']='preprint_or_author_manuscript'
    public_dates=[n[k] for k in ['first_submission_date','published_online_date'] if n.get(k) and n[k]<=now()[:10]]
    first=n.get('first_publication_date');first_source=n.get('field_provenance',{}).get('first_publication_date',{}).get('source','')
    if first and first<=now()[:10] and not (first==n.get('published_print_date') and first_source.startswith('crossref.') and public_dates):public_dates.append(first)
    n['first_public_date']=min(public_dates,default=None)
    n['first_public_date_policy']='earliest submission/online/explicit first publication; issue date excluded as availability evidence'
    n['normalized_publication_year_policy']='V2 online-first formal publication year when confirmed; otherwise first submission/source year; source/issue dates retained'
    rawsource=p.get('raw_source_evidence') or {}
    if rawsource.get('related_formal_doi'):
        n['formal_doi_candidate']=rawsource['related_formal_doi'];n['arxiv_journal_reference']=rawsource.get('journal_reference')
        if not n.get('formal_venue'):n['publication_status']='formal_relation_pending_verification'
    if p.get('source')=='openalex' and rawsource.get('id'):n['openalex_work_id']=rawsource['id']
    n['pmid']=rawsource.get('id') if rawsource.get('source')=='MED' else n.get('pmid');n['pmcid']=rawsource.get('pmcid') or n.get('pmcid')
    pid=p.get('id') or n.get('paper_id')
    artifact=WORK.parent/'fulltext_xml'/(str(pid)+'.json')
    if pid and artifact.exists():
        record=json.loads(artifact.read_text(encoding='utf8'));n['fulltext_artifacts']=[record]
        xml_path=STATE/'fulltext_xml'/(str(pid)+'.xml')
        if xml_path.exists():
            root=ET.fromstring(xml_path.read_text(encoding='utf8'));kw=[' '.join(v.itertext()).strip() for v in root.findall('.//kwd-group/kwd')]
            if kw and not n.get('keywords'):n['keywords']=kw;n.setdefault('field_provenance',{})['keywords']={'source':record['source'],'confidence':.99,'evidence':'JATS kwd-group/kwd from identity-verified OA article'}
            for field,path in [('volume','.//article-meta/volume'),('issue','.//article-meta/issue'),('article_number','.//article-meta/elocation-id')]:
                value=root.findtext(path)
                if value and not n.get(field):n[field]=value;n.setdefault('field_provenance',{})[field]={'source':record['source'],'confidence':.99,'evidence':path}
    return n

def repair(db):
    cfg=load_config();classifier=Classifier(cfg);evidence=json.loads((WORK/'metadata_evidence.json').read_text(encoding='utf8')) if (WORK/'metadata_evidence.json').exists() else {}
    from corpus_repair import load_pdf_cache
    cr=known_crossref();pdfs=load_pdf_cache()
    changes=[];reviewlog=[];validations=collections.defaultdict(dict)
    for pid,d,verdict,checks in db.execute('SELECT paper_id,domain,verdict,checks FROM corpus_v2_validation'):validations[pid][d]=dict(json.loads(checks),verdict=verdict)
    current_version=version(cfg)
    for index,(pid,p,old,s) in enumerate(rows(db)):
        if (WORK/'STOP').exists():break
        ev=evidence.get(pid)
        if not ev:
            raw=cr.get(old.get('formal_doi') or old.get('external_formal_doi') or old.get('doi'))
            if raw and similarity(p.get('title'),crossref_title(raw))>=.8:
                ev={'crossref':raw,'source':'https://api.crossref.org/works/'+raw['DOI'].lower(),'title_similarity':similarity(p.get('title'),crossref_title(raw)),'author_overlap':None}
        n=extend_metadata(p,old,ev)
        if pid in pdfs:
            from literature_audit import explicit_pdf_genre
            n['pdf_explicit_genre']=explicit_pdf_genre(p,pdfs[pid])
        fields=[key for key in n if n[key]!=old.get(key)]
        if fields:
            put_metadata(db,pid,n,RUN)
            for key in fields:
                if key not in ['field_provenance','metadata_candidates','version_relations']:add_candidate(db,pid,key,n[key],ev['source'] if ev else 'v3_explicit_roles',{'policy':'separate file version from work publication','fields':fields},.98 if ev else .9)
            changes.append({'paper_id':pid,'fields':fields,'old_formal_venue':old.get('formal_venue'),'formal_venue':n.get('formal_venue'),'formal_doi':n.get('formal_doi'),'local_file_version':n['local_file_version']})
        impactful={'doi','formal_venue','document_type','canonical_publication_year','formal_publication_status','abstract','keywords','pdf_explicit_genre','identity_metadata_conflict'}
        if impactful.intersection(fields) or not db.execute('SELECT 1 FROM corpus_v2_relevance WHERE paper_id=? AND classifier_version=? LIMIT 1',(pid,current_version)).fetchone():
            results={}
            for d in classifier.domains:
                rule=classifier.classify(p,d,n)
                # Preserve previous independent semantic evidence unless new title-level synonym rules provide clear positive evidence.
                saved=db.execute('SELECT relevance_score,relevance_label,relevance_evidence,matched_topics,excluded_reason,method FROM corpus_v2_relevance WHERE paper_id=? AND domain=? ORDER BY updated DESC LIMIT 1',(pid,d)).fetchone()
                if saved:
                    score,label,proof,topics,reason,method=saved;result={'relevance_score':score,'relevance_label':label,'relevance_evidence':json.loads(proof),'matched_topics':json.loads(topics),'excluded_reason':reason,'method':method}
                    result['matched_topics']=sorted(set(result['matched_topics'])|set(rule['matched_topics']))
                    # No model/validation-negative decisions are silently replaced.
                    if rule['relevance_label']=='relevant' and method=='precision_rules':result=rule
                else:result=rule
                results[d]=result;put_relevance(db,pid,d,result,cfg,RUN)
            e=acceptance(p,n,pdfs.get(pid) if pid in pdfs else None,results,cfg,validations[pid])
            orig=s['primary_state'];new=orig;reason=';'.join(e['issues'])
            genre=n.get('pdf_explicit_genre')
            explicit_nonresearch=genre in ['editorial','correction','erratum','obituary','book review','news and views','forum_session_overview','dissertation','supplement_or_cover','retraction','corrigendum'] or n.get('document_type') in ['book_chapter','book','dissertation','dataset','editorial']
            other=db.execute('SELECT paper_id FROM corpus_v2_status WHERE article_identity=? AND accepted_for_corpus=1 AND paper_id!=?',(s['article_identity'],pid)).fetchone()
            if explicit_nonresearch and pid in pdfs:new='excluded'
            elif e['eligible']:new='duplicate_version' if other else 'accepted_for_corpus'
            elif orig=='accepted_for_corpus' and e['needs_review']:new='needs_review'
            # Do not convert a merely model-negative boundary review into an exclusion.
            review_flag=e['needs_review'] if pid not in pdfs else new=='needs_review'
            excluded_flag=e['excluded'] if pid not in pdfs else new=='excluded'
            db.execute('UPDATE corpus_v2_status SET primary_state=?,accepted_for_corpus=?,needs_review=?,excluded=?,duplicate_version=?,relevant=?,domains=?,topics=?,exclusion_reason=?,run_id=?,updated=? WHERE paper_id=?',
                       (new,int(new=='accepted_for_corpus'),int(review_flag),int(excluded_flag),int(new=='duplicate_version'),int(e['relevant']),dump(e['domains']),dump(e['topics']),reason,RUN,now(),pid))
            if orig=='needs_review':reviewlog.append({'paper_id':pid,'title':p['title'],'before':orig,'after':new,'category':'non_research' if explicit_nonresearch else 'relevant' if new=='accepted_for_corpus' else 'preprint_formal_duplicate' if new=='duplicate_version' else 'unconfirmed','reason':reason,'evidence':{d:r['relevance_evidence'] for d,r in results.items() if r['relevance_label']!='irrelevant'}})
        if ev and n.get('arxiv_id') and n.get('formal_doi'):
            edge={'paper_id':pid,'arxiv_id':n['arxiv_id'],'formal_doi':n['formal_doi'],'evidence':ev['source']};key=hashlib.sha256(dump(edge).encode()).hexdigest()
            related=db.execute("SELECT paper_id FROM corpus_v2_metadata WHERE paper_id!=? AND json_extract(normalized,'$.doi')=? LIMIT 1",(pid,n['formal_doi'])).fetchone()
            db.execute('INSERT OR IGNORE INTO corpus_v2_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,pid,related[0] if related else None,s['article_identity'],p.get('doi') or None,n['formal_doi'],n['local_file_version'],'formal_publication',.98,dump(ev),now()))
        if index%100==0:db.commit();atomic_json(WORK/'progress.json',{'stage':'repair','processed':index,'run_id':RUN})
    db.commit()
    # Reuse V2 work-level identity resolution after adding verified DOI relations.
    from corpus_repair import build_identities, rows_dict
    metadata={pid:json.loads(data) for pid,data in db.execute('SELECT paper_id,normalized FROM corpus_v2_metadata')}
    from corpus_venues import normalize_venues
    normalize_venues(db,metadata,RUN)
    identities,membership,edges=build_identities(db,rows_dict(db),metadata,pdfs)
    for identity,members in identities.items():
        accepted=[pid for pid in members if db.execute('SELECT accepted_for_corpus FROM corpus_v2_status WHERE paper_id=?',(pid,)).fetchone()[0]]
        eligible=[pid for pid in members if db.execute("SELECT valid_pdf=1 AND relevant=1 AND needs_review=0 AND excluded=0 AND primary_state IN ('accepted_for_corpus','duplicate_version') FROM corpus_v2_status WHERE paper_id=?",(pid,)).fetchone()[0]]
        representative=max(eligible,key=lambda pid:(metadata[pid].get('downloaded_version')=='formal_publication_copy',bool(metadata[pid].get('formal_venue')),pid)) if eligible else min(members)
        for pid in members:
            db.execute('UPDATE corpus_v2_status SET article_identity=?,representative_id=? WHERE paper_id=?',(identity,representative,pid))
            if pid==representative and eligible:
                db.execute("UPDATE corpus_v2_status SET accepted_for_corpus=1,duplicate_version=0,primary_state='accepted_for_corpus' WHERE paper_id=?",(pid,))
            if pid in accepted and pid!=representative:
                db.execute("UPDATE corpus_v2_status SET accepted_for_corpus=0,duplicate_version=1,primary_state='duplicate_version',exclusion_reason='verified_same_work_version',updated=? WHERE paper_id=?",(now(),pid))
    db.commit()
    history=WORK/'history';history.mkdir(exist_ok=True)
    import shutil
    suffix=now().replace(':','').replace('+','_')
    for name in ['METADATA_CHANGES.csv','REVIEW_DECISIONS.csv','repair_summary.json']:
        if (WORK/name).exists():shutil.copy2(WORK/name,history/(suffix+'-'+name))
    csvwrite(WORK/'METADATA_CHANGES.csv',changes);csvwrite(WORK/'REVIEW_DECISIONS.csv',reviewlog)
    atomic_json(WORK/'repair_summary.json',{'metadata_records_changed':len(changes),'original_needs_review_examined':len(reviewlog),'review_resolved':sum(x['after']!='needs_review' for x in reviewlog),'review_after':dict(collections.Counter(x['after'] for x in reviewlog)),'at':now()})
    print((WORK/'repair_summary.json').read_text(encoding='utf8'),flush=True)

def audit(db):
    cfg=load_config();items=rows(db);downloaded=set(r[0] for r in db.execute("SELECT id FROM papers WHERE status='downloaded'"));accepted=[];year=collections.Counter();topics=collections.Counter();venues=collections.Counter();publishers=collections.Counter();sources=collections.Counter();status=collections.Counter();versions=collections.Counter();unmapped=collections.Counter();domains=collections.Counter();reconciliation=[]
    for pid,p,n,s in items:
        ds=json.loads(s['domains']);ts=json.loads(s['topics']);isaccepted=s['primary_state']=='accepted_for_corpus'
        reconciliation.append({'paper_id':pid,'title':p['title'],'primary_state':s['primary_state'],'article_identity':s['article_identity'],'downloaded':pid in downloaded,'formal_doi':n.get('formal_doi'),'doi':n.get('doi'),'formal_venue':n.get('formal_venue'),'publication_status':n.get('publication_status'),'local_file_version':n.get('local_file_version'),'canonical_publication_year':n.get('canonical_publication_year'),'first_public_date':n.get('first_public_date'),'published_online_date':n.get('published_online_date'),'published_print_date':n.get('published_print_date'),'domains':ds,'topics':ts,'reason':s['exclusion_reason']})
        if pid in downloaded:status[n.get('publication_status','unknown')]+=1;versions[n.get('local_file_version','unknown')]+=1
        if not isaccepted:continue
        accepted.append(pid);venues[n.get('canonical_formal_venue') or n.get('formal_venue') or 'unknown/preprint']+=1;publishers[n.get('publisher') or 'unknown']+=1;sources[p.get('download_origin') or 'unknown']+=1
        for d in ds:
            domains[d]+=1;year[(d,n.get('canonical_publication_year') or 'unknown')]+=1
            if not ts.get(d):unmapped[d]+=1
            for t in ts.get(d,[]):topics[(d,t)]+=1
    fields=['doi','formal_venue','issn','publisher','keywords','formal_doi','volume','issue','pages','article_number','openalex_work_id','publication_status']
    complete={f:{'present':sum(bool(n.get(f)) for pid,p,n,s in items if pid in downloaded),'total':len(downloaded)} for f in fields}
    summary={'at':now(),'candidates':len(items),'downloaded':len(downloaded),'primary_states':dict(db.execute('SELECT primary_state,count(*) FROM corpus_v2_status GROUP BY primary_state')),'accepted':len(accepted),'publication_status_downloaded':dict(status),'local_file_version_downloaded':dict(versions),'completeness_downloaded':complete,'accepted_domain_counts':dict(domains),'unmapped_accepted':dict(unmapped),'formal_venues_accepted':dict(venues),'publishers_accepted':dict(publishers),'fulltext_sources_accepted':dict(sources),'new_pdf_downloads':len(downloaded)-3163}
    summary['preprints_linked_to_published_downloaded']=len({s['article_identity'] for pid,p,n,s in items if pid in downloaded and n.get('preprint_source') and n.get('formal_doi') and n.get('publication_status')=='published'})
    summary['venue_types_accepted']=dict(collections.Counter(n.get('formal_publication_type') or 'unknown/preprint' for pid,p,n,s in items if s['primary_state']=='accepted_for_corpus'))
    summary['download_failures_by_reason']=dict(db.execute("SELECT coalesce(last_error,'unknown'),count(*) FROM download_locations WHERE status IN ('forbidden','not_found','invalid','exhausted','transient','cooldown') GROUP BY coalesce(last_error,'unknown')"))
    summary['fulltext_xml_records']=sum(1 for path in (STATE/'fulltext_xml').glob('*.xml'))
    xml_ids={path.stem for path in (STATE/'fulltext_xml').glob('*.xml') if path.with_suffix('.json').exists()}
    accepted_works={s['article_identity'] for pid,p,n,s in items if s['primary_state']=='accepted_for_corpus'}
    xml_eligible=set()
    for pid,p,n,s in items:
        if pid in xml_ids:
            statusrow=db.execute('SELECT relevant,needs_review,excluded FROM corpus_v2_status WHERE paper_id=?',(pid,)).fetchone()
            if statusrow==(1,0,0) and n.get('document_type') in cfg['acceptance']['allowed_types'] and not n.get('pdf_explicit_genre'):xml_eligible.add(s['article_identity'])
    summary['accepted_fulltext_distinct_works']=len(accepted_works|xml_eligible)
    summary['accepted_jats_only_distinct_works']=len(xml_eligible-accepted_works)
    summary['available_fulltext_distinct_works']=len({s['article_identity'] for pid,p,n,s in items if pid in downloaded or pid in xml_ids})
    atomic_json(WORK/'audit_summary.json',summary);csvwrite(ROOT/'CORPUS_RECONCILIATION_V3.csv',reconciliation)
    csvwrite(WORK/'DOMAIN_YEAR_COVERAGE.csv',[{'domain':d,'year':y,'accepted':c} for (d,y),c in sorted(year.items(),key=lambda x:(x[0][0],str(x[0][1])))])
    csvwrite(WORK/'SUBDOMAIN_COVERAGE.csv',[{'domain':d['domain_name'],'subdomain':t['topic_id'],'accepted':topics[(d['domain_name'],t['topic_id'])],'target':t.get('target_count'),'deficit':max(0,(t.get('target_count') or 0)-topics[(d['domain_name'],t['topic_id'])])} for d in cfg['domains'] for t in d['topics']])
    from corpus_planner import preview
    planning_path=STATE/'incremental_v3.json'
    planning_cfg=load_config(planning_path) if planning_path.exists() else cfg
    pool=preview(db,planning_cfg,20000);plan=[]
    verified_path=STATE/'migration_v3/SMOKE_VERIFY.json'
    smoke_verified=bool(verified_path.exists() and json.loads(verified_path.read_text(encoding='utf8')).get('smoke_passed'))
    for y in [2023,2024,2019,2020,2021,2022,2025]:
        cells={}
        for d in cfg['domains']:
            names={t['name']:t['topic_id'] for t in d['topics']}
            cells[d['domain_name']]=[j for j in pool if j['domain']==d['domain_name'] and j['lo']==y and j['source']=='openalex' and j['topic'] in names][:4]
        for rank in range(4):
            for d in cfg['domains']:
                choices=cells[d['domain_name']]
                if rank>=len(choices):continue
                j=choices[rank];topic=next(t for t in d['topics'] if t['name']==j['topic'])
                plan.append(dict(j,subdomain=topic['topic_id'],query=j['topic'],year=j['lo'],batch_target=5,phase='incremental' if smoke_verified else 'smoke',requires_gate='smoke_verified' if smoke_verified else 'migration+tests+repair+MCP health+smoke success',venue_preference=d.get('venue_preference','confirmed formal venues; repository file version retained'),source_preference=d.get('search_sources',planning_cfg['sampling']['providers']),enabled=False))
    csvwrite(ROOT/'NEXT_ACQUISITION_PLAN_V3.csv',plan)
    text=['# 文献库 V3 增量审计','',f'时间（UTC）：{summary["at"]}。基于当前 SQLite 实际状态；V1/V2 报告与原始数据保留。','',f'候选 {len(items)}；已下载 PDF {len(downloaded)}；accepted {len(accepted)}；本次新下载 {summary["new_pdf_downloads"]}。', '', '互斥主状态：'+dump(summary['primary_states']),'','## 版本与日期','', 'work publication_status、formal_doi、formal_venue 与 local_file_version 分开；arXiv 本地文件不会因正式发表被改成 publisher PDF。','规范年份延续 V2 online-first 口径；first_public_date、在线日期、卷期日期、原来源日期分别保存。未知与未来排期不自动确认正式发表。','', '已下载 work publication_status：'+dump(dict(status)), '已下载本地文件版本：'+dump(dict(versions)),'','## 元数据完整率（分母：全部已下载记录）','','|字段|已确认非空|比例|','|---|---:|---:|']
    for f,v in complete.items():text.append(f'|{f}|{v["present"]}/{v["total"]}|{v["present"]/max(1,v["total"]):.1%}|')
    text+=['','publication_status 非空含 unknown，非空不等于确认。Crossref subject 保留来源，不能视作作者关键词。','', '## 年度与方向覆盖','','|领域|2023 accepted|2024 accepted|全部 accepted 标签|unmapped|','|---|---:|---:|---:|---:|']
    text[3:3]=[f'V2互斥状态和上述accepted保留PDF审计口径；PDF/JATS合并后，accepted全文独立work为 {summary["accepted_fulltext_distinct_works"]}，其中仅JATS为 {summary["accepted_jats_only_distinct_works"]}。可用全文独立work（尚含待复核/排除）为 {summary["available_fulltext_distinct_works"]}。XML不会计为PDF下载成功。','']
    for d in cfg['domains']:
        name=d['domain_name'];text.append(f'|{name}|{year[(name,2023)]}|{year[(name,2024)]}|{domains[name]}|{unmapped[name]}|')
    text+=['','领域/子领域为多标签，不能相加当作独立论文总数。完整表在 acquisition/repair_v3/DOMAIN_YEAR_COVERAGE.csv 和 SUBDOMAIN_COVERAGE.csv。','','## 正式 venue、publisher 与全文来源','', 'accepted venue 前20：'+dump(venues.most_common(20)), '', 'accepted publisher 前20：'+dump(publishers.most_common(20)), '', 'accepted 全文来源：'+dump(dict(sources)), '', 'accepted 正式出版类型：'+dump(summary['venue_types_accepted']), '', f'已下载预印本关联确认正式发表：{summary["preprints_linked_to_published_downloaded"]} 个独立 work；当前保存 JATS/XML：{summary["fulltext_xml_records"]} 份。历史 version edges 不能重复相加。', '', '下载失败来源、HTTP状态、单URL永久拒绝/冷却与次数均在 SQLite http_attempts/download_locations；汇总详情在 audit_summary.json 的 download_failures_by_reason，保留逐URL记录。','', 'OA 可得性和出版物重点名单都不是单篇研究质量证明。尚不能证明顶级期刊覆盖合理，仍需按正式 venue 缺口补齐。','','## 修复证据与剩余事项','', '逐条 metadata 改动与 needs_review 处理见 METADATA_CHANGES.csv / REVIEW_DECISIONS.csv 及 history；全程最终变化以 migration_v3/status_transitions.csv / repair_effects.json 为准。未解决的边界/模型冲突保留 needs_review。原始 papers/observations/jobs/download_locations 不由修复脚本更新。','未来采集复用 collect.py、CorpusRunner、download_engine；默认 plan-only。每轮应重跑本审计。']
    (ROOT/'LITERATURE_AUDIT_V3.md').write_text('\n'.join(text)+'\n',encoding='utf8')
    import shutil
    history=WORK/'audits'/now().replace(':','').replace('+','_');history.mkdir(parents=True,exist_ok=True)
    for path in [ROOT/'LITERATURE_AUDIT_V3.md',ROOT/'NEXT_ACQUISITION_PLAN_V3.csv',ROOT/'CORPUS_RECONCILIATION_V3.csv',WORK/'audit_summary.json',WORK/'DOMAIN_YEAR_COVERAGE.csv',WORK/'SUBDOMAIN_COVERAGE.csv']:shutil.copy2(path,history/path.name)
    print(dump({k:summary[k] for k in ['candidates','downloaded','accepted','primary_states','new_pdf_downloads']}),flush=True)

def main():
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['enrich','repair','audit']);parser.add_argument('--request-limit',type=int,default=80);args=parser.parse_args()
    if (STATE/'collector.lock').exists() or (STATE/'repair_v2/repair.lock').exists():raise SystemExit('Stop existing collector/repair before V3 work.')
    lock=WORK/'repair.lock'
    if args.stage=='audit':
        db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
        try:audit(db)
        finally:db.close()
        return
    fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY);os.write(fd,str(os.getpid()).encode());os.close(fd)
    db=sqlite3.connect(STATE/'state.sqlite3')
    try:
        if args.stage=='enrich':enrich(db,args.request_limit)
        else:repair(db)
    finally:db.close();lock.unlink(missing_ok=True)

if __name__=='__main__':main()
