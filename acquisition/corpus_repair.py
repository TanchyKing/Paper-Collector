"""Offline repair of the existing library; external paper downloads are never invoked."""
from __future__ import annotations
from project_paths import resolve_project_path, pointer_path
import argparse,collections,datetime,hashlib,json,math,pathlib,re,sqlite3,time,uuid
import pymupdf as fitz
from corpus_store import ROOT,STATE,load_config,version,migrate,dump,now,put_metadata,put_relevance,atomic_json,csvwrite
from corpus_metadata import Normalizer,clean,dni
from corpus_rules import Classifier,LocalSemantic,apply_semantic
from corpus_planner import seed_annual_jobs,preview
WORK=STATE/'repair_v2';WORK.mkdir(exist_ok=True)
def load_pdf_cache():
    folder=pointer_path(STATE/'audit/LATEST.txt');checks={}
    if (folder/'pdf_checks.jsonl').exists():
        for line in (folder/'pdf_checks.jsonl').read_text(encoding='utf-8').splitlines():
            q=json.loads(line);checks[q['id']]=q
    if (WORK/'pdf_checks.json').exists():checks.update(json.loads((WORK/'pdf_checks.json').read_text(encoding='utf-8')))
    for path in (STATE/'repair_v3/pdf_checks').glob('*.json'):
        q=json.loads(path.read_text(encoding='utf-8'));checks[q['id']]=q
    return checks
def check_pdf(pid,row,cached=None,sample=False):
    p=json.loads(row['data']);q=dict(cached or {});q['id']=pid
    path=resolve_project_path(row['path']) if row['path'] else None
    if not path or not path.is_file():return dict(q,valid_pdf=False,identity_confirmed=False,error='missing_file')
    with path.open('rb') as fh:digest=hashlib.file_digest(fh,'sha256').hexdigest()
    q['file_unchanged']=digest==row['sha256'];q['sha256']=digest;q['file_bytes']=path.stat().st_size
    if not cached or not cached.get('front_text'):
        from phase1_audit import identity_page
        content=path.read_bytes()
        with fitz.open(stream=content,filetype='pdf') as pdf:
            for i in range(len(pdf)):pdf.load_page(i).get_contents()
            index,header,score,doi_found=identity_page(pdf,p)
            q.update(pages=len(pdf),repaired=pdf.is_repaired,encrypted=pdf.is_encrypted,header_excerpt=header[:5000],front_text='\n'.join(pdf[i].get_text() for i in range(min(3,len(pdf))))[:15000],title_score=score,identity_page=index+1,identity_confirmed=score>=.35 or doi_found,pdf_dois=[])
            q['structural_checks_pass']=content.startswith(b'%PDF-') and b'%%EOF' in content[-4096:] and not pdf.is_repaired and not pdf.is_encrypted and len(pdf)>=2
    if sample and not q.get('body_excerpt'):
        with fitz.open(path) as pdf:
            indices=sorted(set([0,min(2,len(pdf)-1),len(pdf)//3,2*len(pdf)//3,len(pdf)-1]));q['sample_pages']=[i+1 for i in indices];q['body_excerpt']='\n'.join(pdf[i].get_text()[:4500] for i in indices[2:])[:12000]
    q['valid_pdf']=bool(q.get('file_unchanged') and q.get('pages',0)>=2 and not q.get('repaired') and not q.get('encrypted') and (q.get('structural_checks_pass',True)))
    return q
def rows_dict(db):
    db.row_factory=sqlite3.Row;rows={r['id']:dict(r) for r in db.execute('SELECT * FROM papers')};db.row_factory=None;return rows
def previous_decisions():
    folder=pointer_path(STATE/'audit/LATEST.txt');dec=json.loads((folder/'manual_sample_review.json').read_text(encoding='utf-8')) if (folder/'manual_sample_review.json').exists() else {};domains={}
    import csv
    if (folder/'sample_review.csv').exists():
        for r in csv.DictReader((folder/'sample_review.csv').open(encoding='utf-8-sig')):domains[r['paper_id']]=r['domain']
    return {(pid,domains[pid]):d for pid,d in dec.items() if pid in domains}
def prepare(db,cfg,run_id):
    rows=rows_dict(db);pdfs=load_pdf_cache();normalizer=Normalizer(db,run_id);classifier=Classifier(cfg);reviewed=previous_decisions();counts=collections.Counter();start=time.monotonic();downloaded=[pid for pid,r in rows.items() if r['status']=='downloaded'];local_cache=LocalSemantic(cfg) if cfg['semantic']['enabled'] else None
    for j,pid in enumerate(downloaded):
        pdfs[pid]=check_pdf(pid,rows[pid],pdfs.get(pid))
        if (j+1)%100==0:print(dump({'event':'verify_existing_pdf','checked':j+1,'total':len(downloaded)}),flush=True)
    atomic_json(WORK/'pdf_checks.json',pdfs)
    # Preserve additional source/date evidence from original observations for downloaded works.
    extra=collections.defaultdict(list)
    from literature_audit import fact
    for pid,payload in db.execute("SELECT paper_id,payload FROM observations WHERE paper_id IN (SELECT id FROM papers WHERE status='downloaded') AND instr(job_id,'_download')=0"):
        p=json.loads(payload)
        if p.get('raw_source_evidence'):extra[pid].append(fact(p.get('source'),p['raw_source_evidence'],json.loads(rows[pid]['data'])))
    for i,(pid,r) in enumerate(rows.items()):
        p=json.loads(r['data']);n=normalizer.normalize(pid,p,pdfs.get(pid),extra.get(pid))
        if n.get('doi') and pdfs.get(pid) and n['doi'] in pdfs[pid].get('front_text','').lower():pdfs[pid]['identity_confirmed']=True
        for d,result in classifier.all(p,n).items():
            manual=reviewed.get((pid,d))
            if manual:
                label={'confirmed_within_rule':'relevant','confirmed_out_of_scope':'irrelevant','uncertain_context_requires_review':'uncertain'}.get(manual['verdict'],'uncertain')
                result.update(relevance_label=label,relevance_score={'relevant':.97,'irrelevant':.01,'uncertain':.5}[label],method='prior_audit_semantic_review',excluded_reason=manual['reason'] if label!='relevant' else '')
                result['relevance_evidence']['prior_review']=manual
            if result['relevance_label']=='uncertain' and local_cache:
                semantic_input=dict(p,abstract=n.get('abstract') or p.get('abstract'));cached=local_cache.cached(semantic_input,d)
                if cached:result=apply_semantic(result,cached)
                else:db.execute("UPDATE corpus_v2_semantic_queue SET status='pending' WHERE paper_id=? AND domain=? AND classifier_version=?",(pid,d,version(cfg)))
            put_relevance(db,pid,d,result,cfg,run_id);counts[(r['status']=='downloaded',result['relevance_label'])]+=1
        if (i+1)%100==0:
            db.commit();atomic_json(WORK/'progress.json',{'stage':'normalize_and_classify','processed':i+1,'total':len(rows),'run_id':run_id})
            if (i+1)%1000==0:print(dump({'event':'metadata_and_rules','processed':i+1,'total':len(rows)}),flush=True)
    db.commit();atomic_json(WORK/'pdf_checks.json',pdfs);summary={'records':len(rows),'downloaded':len(downloaded),'rule_label_counts':{str(k):v for k,v in counts.items()},'minutes':(time.monotonic()-start)/60};atomic_json(WORK/'prepare_summary.json',summary);return summary
def semantic(db,cfg,run_id):
    classifier=Classifier(cfg);local=LocalSemantic(cfg);v=version(cfg);rows=rows_dict(db)
    jobs=db.execute("SELECT q.paper_id,q.domain FROM corpus_v2_semantic_queue q JOIN papers p ON p.id=q.paper_id WHERE q.classifier_version=? AND q.status='pending' AND p.status='downloaded' ORDER BY q.priority DESC,q.paper_id",(v,)).fetchall()
    size=cfg['semantic']['batch_size'];batches=0;labels=collections.Counter()
    for pos in range(0,len(jobs),size):
        if (WORK/'STOP').exists():break
        if cfg['semantic'].get('max_batches') is not None and batches>=cfg['semantic']['max_batches']:break
        items=[]
        for pid,d in jobs[pos:pos+size]:
            p=json.loads(rows[pid]['data']);n=json.loads(db.execute('SELECT normalized FROM corpus_v2_metadata WHERE paper_id=?',(pid,)).fetchone()[0]);p['abstract']=n.get('abstract') or p.get('abstract');items.append({'paper_id':pid,'domain':d,'p':p})
        answers=local.classify_batch(items)
        if any(not answer.get('available',True) for answer in answers.values()):break
        for item in items:
            pid=item['paper_id'];d=item['domain'];answer=answers[(pid,d)];p=item['p'];n=json.loads(db.execute('SELECT normalized FROM corpus_v2_metadata WHERE paper_id=?',(pid,)).fetchone()[0]);rule=classifier.classify(p,d,n);result=apply_semantic(rule,answer)
            put_relevance(db,pid,d,result,cfg,run_id);db.execute("UPDATE corpus_v2_semantic_queue SET status='done',result=?,updated=? WHERE paper_id=? AND domain=? AND classifier_version=?",(dump(answer),now(),pid,d,v));labels[answer['relevance_label']]+=1
        db.commit();batches+=1;atomic_json(WORK/'progress.json',{'stage':'local_semantic','processed':min(pos+size,len(jobs)),'total':len(jobs),'model':cfg['semantic']['model']})
        if batches%5==0:print(dump({'event':'local_semantic','processed':min(pos+size,len(jobs)),'total':len(jobs),'labels':dict(labels)}),flush=True)
    done=db.execute("SELECT count(*) FROM corpus_v2_semantic_queue q JOIN papers p ON p.id=q.paper_id WHERE q.classifier_version=? AND q.status='done' AND p.status='downloaded'",(v,)).fetchone()[0]
    remaining=db.execute("SELECT count(*) FROM corpus_v2_semantic_queue q JOIN papers p ON p.id=q.paper_id WHERE q.classifier_version=? AND q.status='pending' AND p.status='downloaded'",(v,)).fetchone()[0]
    summary={'boundary_pairs_this_resume':len(jobs),'processed_pairs_current_version':done,'pending_pairs':remaining,'batches':batches,'local_calls_this_resume':local.calls,'output_tokens_this_resume':local.tokens,'labels_this_resume':dict(labels)};atomic_json(WORK/'semantic_summary.json',summary);return summary
def author_keys(authors):
    out=set()
    for a in authors or []:
        words=re.findall(r'[\w]+',str(a).casefold())
        if words:out.add(words[0] if len(words[-1])<=3 and len(words)>1 else words[-1])
    return out
def build_identities(db,rows,metadata,pdfs):
    parent={pid:pid for pid in rows};keys={};proof=[]
    def find(x):
        while parent[x]!=x:parent[x]=parent[parent[x]];x=parent[x]
        return x
    def merge(a,b,why,confidence):
        if find(a)!=find(b):parent[find(a)]=find(b);proof.append({'a':a,'b':b,'basis':why,'confidence':confidence})
    titles=collections.defaultdict(list);doiowners={}
    for pid,r in rows.items():
        p=json.loads(r['data']);n=metadata[pid];kk=[]
        if n.get('doi') and not n.get('identity_metadata_conflict'):kk.append('doi:'+dni(n['doi']));doiowners[dni(n['doi'])]=pid
        if n.get('external_formal_doi') and not n.get('identity_metadata_conflict'):kk.append('doi:'+dni(n['external_formal_doi']))
        if n.get('arxiv_id'):kk.append('arxiv:'+n['arxiv_id'])
        if r.get('sha256') and pdfs.get(pid,{}).get('valid_pdf'):kk.append('sha256:'+r['sha256'])
        for key in kk:
            if key in keys:merge(pid,keys[key],key,.99)
            else:keys[key]=pid
        title=clean(p.get('title')).casefold();title=re.sub(r'[^\w]+',' ',title).strip()
        if len(title)>=25:
            for other in titles[title]:
                aa=author_keys(n.get('authors'));bb=author_keys(metadata[other].get('authors'));overlap=len(aa&bb)/max(1,min(len(aa),len(bb)));ya=n.get('canonical_publication_year');yb=metadata[other].get('canonical_publication_year')
                if aa and bb and overlap>=.5 and (not ya or not yb or abs(ya-yb)<=10):merge(pid,other,'exact_title + author_overlap + compatible_dates',.93)
            titles[title].append(pid)
    for pid,n in metadata.items():
        for relation in n.get('version_relations',[]):
            other=doiowners.get(dni(relation['relation_doi']))
            if other:merge(pid,other,'verified publisher version relation: '+relation['source'],relation['confidence'])
    groups=collections.defaultdict(list)
    for pid in rows:groups[find(pid)].append(pid)
    identities={};membership={}
    for ids in groups.values():
        old=db.execute('SELECT article_identity FROM corpus_v2_status WHERE paper_id IN ('+','.join('?' for _ in ids)+') ORDER BY rowid LIMIT 1',ids).fetchone()
        stable=old[0] if old else 'article:'+min(ids);aliases=sorted({metadata[x]['doi'] for x in ids if metadata[x].get('doi')});arxiv=sorted({metadata[x]['arxiv_id'] for x in ids if metadata[x].get('arxiv_id')});evidence=[e for e in proof if e['a'] in ids or e['b'] in ids]
        db.execute('INSERT INTO corpus_v2_identity VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(article_identity) DO UPDATE SET title=excluded.title,members=excluded.members,doi_aliases=excluded.doi_aliases,arxiv_aliases=excluded.arxiv_aliases,evidence=excluded.evidence,confidence=excluded.confidence,updated=excluded.updated',(stable,json.loads(rows[ids[0]]['data']).get('title'),dump(ids),dump(aliases),dump(arxiv),dump(evidence),min([e['confidence'] for e in evidence] or [1]),now()))
        identities[stable]=ids
        for pid in ids:membership[pid]=stable
    edges=[]
    for pid,n in metadata.items():
        formal=n.get('external_formal_doi');ispreprint=bool(n.get('preprint_source'))
        if ispreprint and n.get('formal_venue') and formal and not formal.startswith(('10.48550/','10.1101/','10.21203/','10.64898/')):
            evidence={'formal_venue':n['formal_venue'],'verified_metadata_candidates':[c for c in n['metadata_candidates'] if c['field'] in ['formal_venue','doi']],'classification':'preprint source/identifier linked to verified formal metadata; downloaded PDF version is a separate field','downloaded_version':n.get('downloaded_version')}
            edge={'paper_id':pid,'related_paper_id':doiowners.get(formal) if doiowners.get(formal)!=pid else None,'article_identity':membership[pid],'preprint_doi':dni(json.loads(rows[pid]['data']).get('doi')) if dni(json.loads(rows[pid]['data']).get('doi'))!=formal else None,'formal_doi':formal,'from_version':'preprint_or_author_manuscript','to_version':'formal_publication','confidence':.96,'evidence':evidence};edges.append(edge)
        for relation in n.get('version_relations',[]):
            other=doiowners.get(dni(relation['relation_doi']));reverse=relation.get('relation_type')=='has-preprint';edges.append({'paper_id':pid,'related_paper_id':other,'article_identity':membership[pid],'preprint_doi':relation['relation_doi'] if reverse else n.get('doi') if n.get('preprint_source') else None,'formal_doi':n.get('doi') if reverse else relation['relation_doi'],'from_version':'preprint_or_author_manuscript' if reverse else n.get('downloaded_version'),'to_version':'formal_publication' if reverse or relation.get('relation_type')=='is-preprint-of' else 'publisher_declared_related_version','confidence':relation['confidence'],'evidence':relation})
    edges=list({dump(e):e for e in edges}.values())
    for e in edges:
        key=hashlib.sha256(dump(e).encode()).hexdigest();db.execute('INSERT OR IGNORE INTO corpus_v2_versions VALUES(?,?,?,?,?,?,?,?,?,?,?)',(key,e['paper_id'],e['related_paper_id'],e['article_identity'],e['preprint_doi'],e['formal_doi'],e['from_version'],e['to_version'],e['confidence'],dump(e['evidence']),now()))
    return identities,membership,edges
def acceptance(p,n,q,results,cfg,validation=None):
    classifier=Classifier(cfg);domains=[d for d,r in results.items() if classifier.accepts(r)];issues=[]
    checks=validation or {}
    for d,r in results.items():
        if d in domains and checks.get(d,{}).get('verdict') in ['irrelevant','probably_irrelevant','uncertain']:domains.remove(d);issues.append('sample_semantic_disagreement:'+d)
    technical_relevant=bool(domains);downloaded=q is not None
    genre=n.get('pdf_explicit_genre');dtype=n.get('document_type');type_ok=dtype in cfg['acceptance']['allowed_types'] and not genre
    if not type_ok:issues.append('non_target_or_unknown_document_type:'+str(genre or dtype))
    if n.get('identity_metadata_conflict'):issues.append('doi_title_conflict_needs_review')
    date_risk=cfg['acceptance']['review_future_publication'] and n.get('formal_publication_status') in ['future_scheduled_or_unconfirmed','published_year_precision_only'] and n.get('canonical_publication_year')==int(datetime.datetime.now().strftime('%Y')) and not n.get('first_submission_date')
    if date_risk:issues.append('current_year_online_publication_not_confirmed')
    if downloaded and not q.get('valid_pdf'):issues.append('invalid_or_missing_pdf')
    if downloaded and cfg['acceptance']['require_confirmed_identity'] and not q.get('identity_confirmed'):issues.append('pdf_identity_unconfirmed')
    uncertain=any(r['relevance_label'] in ['uncertain','probably_relevant','probably_irrelevant'] for d,r in results.items() if d in p.get('domains',[]) and d not in domains)
    if not technical_relevant:issues.append('no_high_confidence_relevant_domain')
    eligible=downloaded and q.get('valid_pdf') and technical_relevant and not any(x!='no_high_confidence_relevant_domain' for x in issues)
    review=bool(any(x for x in issues if x not in ['no_high_confidence_relevant_domain']) or not technical_relevant and uncertain)
    excluded=not technical_relevant and not review or bool(genre) or dtype in ['book_chapter','dataset','dissertation','editorial']
    return {'domains':domains,'relevant':technical_relevant,'eligible':bool(eligible),'needs_review':review,'excluded':bool(excluded),'issues':issues,'topics':{d:results[d]['matched_topics'] for d in domains}}
def finalize(db,cfg,run_id):
    rows=rows_dict(db);metadata={pid:json.loads(data) for pid,data in db.execute('SELECT paper_id,normalized FROM corpus_v2_metadata')};pdfs=load_pdf_cache();v=version(cfg);classifications=collections.defaultdict(dict)
    from corpus_venues import normalize_venues
    normalize_venues(db,metadata,run_id)
    for pid,n in metadata.items():
        if n.get('downloaded_version_evidence',{}).get('formal_venue_and_doi_in_pdf_header') and n.get('pdf_host') in ['www.nature.com','www.frontiersin.org','link.springer.com','www.aclweb.org','aclanthology.org']:
            n['downloaded_version']='formal_publication_copy';n['downloaded_version_evidence']['selection']='PDF formal venue + DOI on formal publication site; retrieval source/preprint ID kept independently';put_metadata(db,pid,n,run_id)
    for r in db.execute('SELECT paper_id,domain,relevance_score,relevance_label,relevance_evidence,matched_topics,excluded_reason,method FROM corpus_v2_relevance WHERE classifier_version=?',(v,)):
        pid,d,score,label,evidence,topics,reason,method=r;classifications[pid][d]={'relevance_score':score,'relevance_label':label,'relevance_evidence':json.loads(evidence),'matched_topics':json.loads(topics),'excluded_reason':reason,'method':method}
    identities,membership,edges=build_identities(db,rows,metadata,pdfs);validations=collections.defaultdict(dict)
    for pid,d,verdict,checks in db.execute('SELECT paper_id,domain,verdict,checks FROM corpus_v2_validation WHERE run_id=?',(run_id,)):validations[pid][d]=dict(json.loads(checks),verdict=verdict)
    elig={pid:acceptance(json.loads(r['data']),metadata[pid],pdfs.get(pid) if r['status']=='downloaded' else None,classifications[pid],cfg,validations[pid]) for pid,r in rows.items()}
    reps={identity:max(ids,key=lambda pid:(elig[pid]['eligible'],rows[pid]['status']=='downloaded',bool(metadata[pid].get('formal_venue')),len(metadata[pid].get('authors') or []),pid)) for identity,ids in identities.items()}
    reconciliation=[];reclass=[];yearrows=[];version_rows=[];counts=collections.Counter();removed=collections.Counter();coverage={d['domain_name']:collections.Counter() for d in cfg['domains']};primary={d['domain_name']:collections.Counter() for d in cfg['domains']};unmapped=collections.Counter();domain_totals=collections.Counter()
    for pid,r in rows.items():
        p=json.loads(r['data']);n=metadata[pid];e=elig[pid];identity=membership[pid];rep=reps[identity];isdownloaded=r['status']=='downloaded';accepted=e['eligible'] and pid==rep;duplicate=pid!=rep and len(identities[identity])>1;state='metadata_only' if not isdownloaded else 'accepted_for_corpus' if accepted else 'duplicate_version' if duplicate and e['eligible'] else 'needs_review' if e['needs_review'] else 'excluded'
        if accepted:
            for d in e['domains']:
                domain_totals[d]+=1;topics=e['topics'].get(d,[]);coverage[d].update(topics)
                if not topics:unmapped[d]+=1
                else:
                    title=clean(p.get('title'));dconf=next(dv for dv in cfg['domains'] if dv['domain_name']==d);scores={t['topic_id']:sum(bool(re.search(pattern,title,re.I)) for pattern in t['positive_terms']) for t in dconf['topics'] if t['topic_id'] in topics};winner=max(scores,key=scores.get);primary[d][winner]+=1
        if isdownloaded:
            for d in p.get('domains',[]):
                if d not in e['domains']:removed[d]+=1
        counts[state]+=1;reason=';'.join(e['issues']);db.execute('INSERT INTO corpus_v2_status VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id) DO UPDATE SET article_identity=excluded.article_identity,representative_id=excluded.representative_id,downloaded=excluded.downloaded,valid_pdf=excluded.valid_pdf,relevant=excluded.relevant,accepted_for_corpus=excluded.accepted_for_corpus,duplicate_version=excluded.duplicate_version,metadata_only=excluded.metadata_only,needs_review=excluded.needs_review,excluded=excluded.excluded,exclusion_reason=excluded.exclusion_reason,domains=excluded.domains,topics=excluded.topics,primary_state=excluded.primary_state,run_id=excluded.run_id,updated=excluded.updated',(pid,identity,rep,int(isdownloaded),int(pdfs.get(pid,{}).get('valid_pdf',False)),int(e['relevant']),int(accepted),int(duplicate),int(not isdownloaded),int(e['needs_review']),int(e['excluded']),reason,dump(e['domains']),dump(e['topics']),state,run_id,now()))
        reconciliation.append({'paper_id':pid,'title':p.get('title'),'DOI':n.get('doi'),'original_download_status':r['status'],'downloaded':isdownloaded,'valid_pdf':pdfs.get(pid,{}).get('valid_pdf',False),'pdf_identity_confirmed':pdfs.get(pid,{}).get('identity_confirmed',False),'relevant':e['relevant'],'accepted_for_corpus':accepted,'exclusion_reason':reason,'duplicate_version':duplicate,'metadata_only':not isdownloaded,'needs_review':e['needs_review'],'excluded':e['excluded'],'primary_state':state,'article_identity':identity,'representative_id':rep,'related_paper_ids':identities[identity],'original_domains':p.get('domains'),'reclassified_domains':e['domains'],'matched_topics':e['topics'],'canonical_publication_year':n.get('canonical_publication_year'),'pdf_path':r['path'],'sha256':r['sha256'],'formal_venue':n.get('formal_venue'),'downloaded_version':n.get('downloaded_version')})
        for d,result in classifications[pid].items():reclass.append({'paper_id':pid,'title':p.get('title'),'domain':d,'original_domain_label':d in p.get('domains',[]),'retained_high_confidence':d in e['domains'],'downloaded':isdownloaded,'classifier_version':v,**result})
        if isdownloaded:yearrows.append({'paper_id':pid,'title':p.get('title'),'DOI':n.get('doi'),'original_year':p.get('year'),'source_reported_year':n.get('source_reported_year'),'first_submission_date':n.get('first_submission_date'),'first_publication_date':n.get('first_publication_date'),'published_online_date':n.get('published_online_date'),'published_print_date':n.get('published_print_date'),'europe_pmc_pub_year':n.get('europe_pmc_pub_year'),'canonical_publication_year':n.get('canonical_publication_year'),'date_source':n.get('date_source'),'canonical_date_role':n.get('canonical_date_role'),'formal_publication_status':n.get('formal_publication_status'),'future_issue_or_date':n.get('future_issue_or_date'),'date_conflicts':n.get('date_conflicts'),'field_provenance':n['field_provenance'],'article_identity':identity,'accepted_for_corpus':accepted})
        if isdownloaded or any(edge['paper_id']==pid for edge in edges):version_rows.append({'paper_id':pid,'title':p.get('title'),'downloaded':isdownloaded,'accepted_for_corpus':accepted,'primary_state':state,'article_identity':identity,'representative_id':rep,'duplicate_version':duplicate,'original_doi':p.get('doi'),'normalized_doi':n.get('doi'),'arxiv_id':n.get('arxiv_id'),'downloaded_version':n.get('downloaded_version'),'preprint_source':n.get('preprint_source'),'formal_venue':n.get('formal_venue'),'publisher':n.get('publisher'),'repository':n.get('repository'),'pdf_host':n.get('pdf_host'),'issn':n.get('issn'),'conference_track':n.get('conference_track'),'pdf_path':r['path'],'version_relationships':[edge for edge in edges if edge['paper_id']==pid]})
    db.commit();csvwrite(ROOT/'CORPUS_RECONCILIATION.csv',reconciliation);csvwrite(ROOT/'RECLASSIFICATION_RESULTS.csv',reclass);csvwrite(ROOT/'YEAR_NORMALIZATION.csv',yearrows);csvwrite(ROOT/'PUBLICATION_VERSION_MAP.csv',version_rows)
    inserted=seed_annual_jobs(db,cfg);plan=preview(db,cfg,100);csvwrite(WORK/'NEXT_ACQUISITION_PLAN.csv',plan)
    topicmetrics={}
    for d,total in domain_totals.items():
        known=total-unmapped[d];hhi=sum((c/max(1,known))**2 for c in primary[d].values());hhi_total_known=sum((c/max(1,total))**2 for c in primary[d].values());largest=primary[d].most_common(1)
        topicmetrics[d]={'accepted_independent_domain_total':total,'unmapped':unmapped[d],'unmapped_ratio':unmapped[d]/max(1,total),'classified_count':known,'primary_topics':dict(primary[d]),'multi_label_topics':dict(coverage[d]),'hhi_classified_only':hhi,'effective_topics_classified_only':1/hhi if hhi else None,'hhi_known_topics_total_denominator':hhi_total_known,'largest_known_topic':largest[0][0] if largest else None,'largest_known_topic_count':largest[0][1] if largest else 0,'largest_known_share_total':largest[0][1]/max(1,total) if largest else 0}
    coverage_rows=[]
    for dconf in cfg['domains']:
        d=dconf['domain_name'];metrics=topicmetrics.get(d,{})
        for topic in dconf['topics']:
            coverage_rows.append({'domain':d,'topic':topic['topic_id'],'topic_name':topic['name'],'aliases':topic.get('aliases'),'accepted_matching':coverage[d][topic['topic_id']],'primary_assigned':primary[d][topic['topic_id']],'domain_accepted_total':domain_totals[d],'observed_library_share':coverage[d][topic['topic_id']]/max(1,domain_totals[d]),'target_count_config':topic.get('target_count'),'hhi_classified_only':metrics.get('hhi_classified_only'),'effective_topics_classified_only':metrics.get('effective_topics_classified_only'),'unmapped_ratio':metrics.get('unmapped_ratio'),'scope':'local accepted independent corpus; multi-label topic counts are not additive'})
        coverage_rows.append({'domain':d,'topic':'unmapped','accepted_matching':unmapped[d],'primary_assigned':'not a technical topic','domain_accepted_total':domain_totals[d],'observed_library_share':unmapped[d]/max(1,domain_totals[d]),'scope':'missing classification; excluded from primary-topic HHI and largest-topic calculation'})
    csvwrite(WORK/'DOMAIN_COVERAGE_V2.csv',coverage_rows)
    acceptedrows=[r for r in reconciliation if r['accepted_for_corpus']];summary={'classifier_version':v,'records':len(rows),'downloaded_records':sum(r['downloaded'] for r in reconciliation),'valid_pdf_records':sum(r['valid_pdf'] for r in reconciliation),'accepted_for_corpus':len(acceptedrows),'primary_state_counts':dict(counts),'removed_original_domain_labels':dict(removed),'topic_metrics':topicmetrics,'canonical_year_all_downloaded':dict(collections.Counter(str(r['canonical_publication_year'] or 'unknown') for r in yearrows)),'canonical_year_accepted':dict(collections.Counter(str(r['canonical_publication_year'] or 'unknown') for r in acceptedrows)),'original_year_downloaded':dict(collections.Counter(str(json.loads(r['data']).get('year')) for r in rows.values() if r['status']=='downloaded')),'preprints_linked_to_formal':len({edge['article_identity'] for edge in edges if edge['to_version']=='formal_publication'}),'version_edges':len(edges),'new_annual_jobs_inserted_this_call':inserted,'annual_jobs_total':db.execute('SELECT count(*) FROM jobs WHERE lo=hi').fetchone()[0],'validation_pairs':db.execute('SELECT count(*) FROM corpus_v2_validation WHERE run_id=?',(run_id,)).fetchone()[0],'legacy_phase1_accepted':json.loads((STATE/'run_state.json').read_text(encoding='utf-8')).get('valid_unique_papers'),'run_id':run_id,'report_time':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat()}
    downloaded_ids=[pid for pid,r in rows.items() if r['status']=='downloaded'];den=len(downloaded_ids)
    summary['downloaded_primary_states']=dict(collections.Counter(r['primary_state'] for r in reconciliation if r['downloaded']))
    summary['catalog_preprints_linked_to_formal']=summary['preprints_linked_to_formal'];summary['preprints_linked_to_formal']=len({edge['article_identity'] for edge in edges if edge['to_version']=='formal_publication' and rows[edge['paper_id']]['status']=='downloaded'})
    summary['downloaded_unique_papers_removed_from_at_least_one_original_domain']=sum(any(d not in elig[pid]['domains'] for d in json.loads(rows[pid]['data']).get('domains',[])) for pid in downloaded_ids)
    summary['downloaded_issue_counts']=dict(collections.Counter(issue for pid in downloaded_ids for issue in elig[pid]['issues']))
    summary['metadata_completeness']={field:{'nonempty':sum(bool(metadata[pid].get(field)) for pid in downloaded_ids),'total':den,'ratio':sum(bool(metadata[pid].get(field)) for pid in downloaded_ids)/max(1,den)} for field in ['doi','authors','abstract','keywords','formal_venue','issn','publisher','document_type','canonical_publication_year']}
    summary['high_confidence_downloaded_unmapped']={d:{'total':sum(d in elig[pid]['domains'] for pid in downloaded_ids),'unmapped':sum(d in elig[pid]['domains'] and not elig[pid]['topics'].get(d) for pid in downloaded_ids)} for d in [dc['domain_name'] for dc in cfg['domains']]}
    summary['date_anomalies']={'original_year_changed':sum(metadata[pid].get('canonical_publication_year')!=json.loads(rows[pid]['data']).get('year') for pid in downloaded_ids),'future_issue_or_date':sum(bool(metadata[pid].get('future_issue_or_date')) for pid in downloaded_ids),'current_year_publication_unconfirmed':sum('current_year_online_publication_not_confirmed' in elig[pid]['issues'] for pid in downloaded_ids),'epmc_pubyear_differs_first_publication_year':sum(bool(metadata[pid].get('europe_pmc_pub_year') and metadata[pid].get('first_publication_date') and str(metadata[pid]['europe_pmc_pub_year'])!=metadata[pid]['first_publication_date'][:4]) for pid in downloaded_ids)}
    atomic_json(WORK/'summary.json',summary);suggest_topics(rows,metadata,classifications,elig,cfg);write_report(summary,plan,cfg);db.execute('UPDATE corpus_v2_runs SET finished=?,status=?,summary=? WHERE run_id=?',(now(),'validated' if summary['validation_pairs'] else 'reclassified',dump(summary),run_id));db.commit();print(dump({k:summary[k] for k in ['downloaded_records','accepted_for_corpus','primary_state_counts','removed_original_domain_labels','preprints_linked_to_formal','validation_pairs']}),flush=True)
STOPWORDS=set('the a an and of to in for with from by on at is are was were this that these those we our their it its as be been through using based study studies results method methods model models analysis approach approaches paper proposed proposal novel new show shows demonstrated different more can could may also used use research system systems'.split())
def text_features(p,n):
    title=clean(p.get('title')).casefold();abstract=clean(n.get('abstract')).casefold();tokens=re.findall(r'[a-z][a-z0-9-]{2,}',title+' '+abstract[:1800]);counts=collections.Counter(t for t in tokens if t not in STOPWORDS)
    words=re.findall(r'[a-z][a-z0-9-]{2,}',title)
    for a,b in zip(words,words[1:]):
        if a not in STOPWORDS and b not in STOPWORDS:counts[a+' '+b]+=2
    for token in re.findall(r'[a-z][a-z0-9-]{2,}',title):
        if token not in STOPWORDS:counts[token]+=2
    for phrase in re.findall(r'[\u4e00-\u9fff]{4,}',title):
        for i in range(len(phrase)-3):counts[phrase[i:i+4]]+=2
    return counts
def suggest_topics(rows,metadata,classifications,elig,cfg):
    suggestions=[];parameters=cfg.get('topic_discovery',{});threshold=parameters.get('cosine_threshold',.18);maxclusters=parameters.get('max_clusters_per_domain',25);minimum=parameters.get('minimum_cluster_size',3)
    for dconf in cfg['domains']:
        d=dconf['domain_name'];pool=[pid for pid,r in rows.items() if r['status']=='downloaded' and d in elig[pid]['domains'] and not elig[pid]['topics'].get(d)]
        features={pid:text_features(json.loads(rows[pid]['data']),metadata[pid]) for pid in pool};df=collections.Counter(t for f in features.values() for t in f);vectors={}
        for pid,f in features.items():
            vec={t:(1+math.log(c))*math.log((1+len(pool))/(1+df[t])) for t,c in f.items() if df[t]>=2};length=math.sqrt(sum(v*v for v in vec.values())) or 1;vectors[pid]={t:v/length for t,v in vec.items()}
        clusters=[]
        for pid in sorted(pool,key=lambda pid:-sum(features[pid].values())):
            v=vectors[pid];best=None;score=0
            for cluster in clusters:
                centroid=cluster['centroid'];sim=sum(value*centroid.get(t,0) for t,value in v.items())
                if sim>score:best=cluster;score=sim
            if best and score>=threshold:
                best['members'].append(pid);accum=collections.Counter()
                for x in best['members']:accum.update(vectors[x])
                length=math.sqrt(sum(z*z for z in accum.values())) or 1;best['centroid']={t:z/length for t,z in accum.items()}
            else:clusters.append({'members':[pid],'centroid':v})
        for cluster in sorted(clusters,key=lambda c:-len(c['members']))[:maxclusters]:
            ids=cluster['members'];counts=collections.Counter()
            for pid in ids:counts.update(features[pid])
            terms=[t for t,c in counts.most_common(15) if df[t]>=2][:8];suggestions.append({'domain':d,'suggestion_id':hashlib.sha256(dump([d,ids]).encode()).hexdigest()[:16],'suggested_topic_label':' / '.join(terms[:4]) or 'insufficient shared vocabulary','members':len(ids),'paper_ids':ids,'representative_titles':[json.loads(rows[x]['data']).get('title') for x in ids[:8]],'top_terms':terms,'method':'title-weighted sparse TF-IDF + greedy cosine clusters','cosine_threshold':threshold,'review_status':'needs_human_review','sufficient_cluster_size':len(ids)>=minimum,'auto_added_to_formal_taxonomy':False,'limitation':'词汇相似聚类，不证明新学术主题；需检查同义词、领域边界及技术贡献。'})
    csvwrite(ROOT/'UNMAPPED_TOPIC_SUGGESTIONS.csv',suggestions,['domain','suggestion_id','suggested_topic_label','members','paper_ids','representative_titles','top_terms','method','cosine_threshold','review_status','sufficient_cluster_size','auto_added_to_formal_taxonomy','limitation']);atomic_json(WORK/'topic_discovery_summary.json',{'clusters':len(suggestions),'proposed_not_adopted':True})
def choose_validation(db,cfg):
    rows=rows_dict(db);metadata={pid:json.loads(data) for pid,data in db.execute('SELECT paper_id,normalized FROM corpus_v2_metadata')};v=version(cfg);results=collections.defaultdict(dict)
    for pid,d,label,topics in db.execute('SELECT paper_id,domain,relevance_label,matched_topics FROM corpus_v2_relevance WHERE classifier_version=?',(v,)):results[pid][d]=(label,json.loads(topics))
    versionmembers={pid for pid, in db.execute('SELECT DISTINCT paper_id FROM corpus_v2_versions')};selected={};size=cfg['acceptance']['sample_per_domain']
    for dconf in cfg['domains']:
        d=dconf['domain_name'];pool=[pid for pid,r in rows.items() if r['status']=='downloaded' and (d in json.loads(r['data']).get('domains',[]) or results[pid].get(d,('irrelevant',[]))[0] in ['relevant','probably_relevant'])];frequency=collections.Counter(t for pid in pool for t in results[pid].get(d,('unknown',[]))[1]);strata=collections.defaultdict(list);strata_of={}
        for pid in pool:
            label,topics=results[pid].get(d,('unknown',[]));n=metadata[pid];labels=[]
            if not topics:labels.append('unmapped')
            if label in ['uncertain','probably_relevant','probably_irrelevant','irrelevant']:labels.append('relevance_boundary_or_removed')
            if pid in versionmembers:labels.append('preprint_formal_version')
            if n.get('date_conflicts') or n.get('future_issue_or_date'):labels.append('date_anomaly')
            if any(frequency[t]>=100 for t in topics):labels.append('high_frequency_topic')
            if any(frequency[t]<20 for t in topics):labels.append('low_frequency_topic')
            if not labels:labels.append('other_topics')
            strata_of[pid]=labels
            for label in labels:strata[label].append(pid)
        for label,ids in strata.items():ids.sort(key=lambda pid:hashlib.sha256((d+label+pid).encode()).hexdigest())
        keys=['relevance_boundary_or_removed','unmapped','preprint_formal_version','date_anomaly','low_frequency_topic','high_frequency_topic','other_topics'];chosen=[];roundn=0
        while len(chosen)<size:
            changed=False
            for key in keys:
                poolx=strata.get(key,[])
                if roundn<len(poolx) and poolx[roundn] not in chosen:chosen.append(poolx[roundn]);changed=True
                if len(chosen)>=size:break
            roundn+=1
            if roundn>max([len(x) for x in strata.values()] or [0]):break
        selected[d]=[{'paper_id':pid,'strata':strata_of[pid]} for pid in chosen]
    atomic_json(WORK/'validation_selection.json',selected);return selected
def validate_samples(db,cfg,run_id):
    selected=choose_validation(db,cfg);rows=rows_dict(db);metadata={pid:json.loads(data) for pid,data in db.execute('SELECT paper_id,normalized FROM corpus_v2_metadata')};pdfs=load_pdf_cache();local=LocalSemantic(cfg);pairs=[]
    for d,items in selected.items():
        for selection in items:
            pid=selection['paper_id'];pdfs[pid]=check_pdf(pid,rows[pid],pdfs.get(pid),sample=True);p=json.loads(rows[pid]['data']);q=pdfs[pid];p['abstract']=clean(metadata[pid].get('abstract'))[:500]+'\nACTUAL PDF TITLE PAGE:\n'+q.get('header_excerpt','')[:500]+'\nACTUAL PDF BODY SAMPLE:\n'+q.get('body_excerpt','')[:580];pairs.append({'paper_id':pid,'domain':d,'p':p,'selection':selection})
    atomic_json(WORK/'pdf_checks.json',pdfs);size=cfg['semantic']['batch_size'];out=[];counts=collections.Counter()
    for i in range(0,len(pairs),size):
        if (WORK/'STOP').exists():break
        batch=pairs[i:i+size];answers=local.classify_batch(batch)
        for item in batch:
            pid=item['paper_id'];d=item['domain'];answer=answers[(pid,d)];n=metadata[pid];q=pdfs[pid];checks={'valid_pdf':q.get('valid_pdf'),'identity_confirmed':q.get('identity_confirmed'),'title_score':q.get('title_score'),'document_type':n.get('document_type'),'actual_pdf_genre':n.get('pdf_explicit_genre'),'formal_venue':n.get('formal_venue'),'canonical_year':n.get('canonical_publication_year'),'local_semantic':answer,'evidence_excerpt':item['p']['abstract']}
            db.execute('INSERT INTO corpus_v2_validation VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(run_id,paper_id,domain) DO UPDATE SET checks=excluded.checks,verdict=excluded.verdict,updated=excluded.updated',(run_id,pid,d,dump(item['selection']['strata']),dump(q.get('sample_pages') or [q.get('identity_page',1)]),dump(checks),answer['relevance_label'],now()));out.append({'paper_id':pid,'domain':d,'title':json.loads(rows[pid]['data']).get('title'),'strata':item['selection']['strata'],'sample_pages':q.get('sample_pages'),'verdict':answer['relevance_label'],'checks':checks});counts[(d,answer['relevance_label'])]+=1
        db.commit();atomic_json(WORK/'progress.json',{'stage':'sample_validation','completed':min(i+size,len(pairs)),'total':len(pairs)})
        if (i//size+1)%5==0:print(dump({'event':'sample_validation','completed':min(i+size,len(pairs)),'total':len(pairs)}),flush=True)
    comparisons={}
    for d in selected:
        subset=[x for x in out if x['domain']==d];positive_before={pid for pid,score,label in db.execute('SELECT paper_id,relevance_score,relevance_label FROM corpus_v2_relevance WHERE domain=? AND classifier_version=?',(d,version(cfg))) if label in cfg['acceptance']['labels'] and score>=cfg['acceptance']['minimum_score']}
        comparisons[d]={'sample_size':len(subset),'clear_negative_content_verdict':sum(x['verdict'] in ['irrelevant','probably_irrelevant'] for x in subset),'uncertain_content_verdict':sum(x['verdict']=='uncertain' for x in subset),'classification_content_disagreements_to_review':sum(x['paper_id'] in positive_before and x['verdict'] in ['irrelevant','probably_irrelevant','uncertain'] for x in subset),'non_target_document_type':sum(x['checks']['document_type'] not in cfg['acceptance']['allowed_types'] or bool(x['checks']['actual_pdf_genre']) for x in subset),'identity_unconfirmed':sum(not x['checks']['identity_confirmed'] for x in subset),'invalid_pdf':sum(not x['checks']['valid_pdf'] for x in subset)}
    csvwrite(WORK/'VALIDATION_SAMPLES.csv',out);atomic_json(WORK/'validation_summary.json',{'pairs':len(out),'per_domain':{d:sum(x['domain']==d for x in out) for d in selected},'labels':{str(k):v for k,v in counts.items()},'comparisons':comparisons,'local_calls':local.calls,'model':cfg['semantic']['model'],'limitations':'目的性分层样本；本地模型独立抽样内容检查，不是人工全文同行评议；不外推总体错误率。'})
def write_report(s,plan,cfg):
    validation=json.loads((WORK/'validation_summary.json').read_text(encoding='utf-8')) if (WORK/'validation_summary.json').exists() else {};sem=json.loads((WORK/'semantic_summary.json').read_text(encoding='utf-8')) if (WORK/'semantic_summary.json').exists() else {}
    lines=['# 文献库系统修复与第二版审计','',f"时间：{s['report_time']}；分类版本：{s['classifier_version']}。",'',f"本次未下载论文，复用现有数据库和API缓存；已下载 {s['downloaded_records']} 条、有效PDF {s['valid_pdf_records']} 条；统一严格口径 accepted_for_corpus 为 **{s['accepted_for_corpus']} 个独立研究成果**。原始paper数据、PDF文件、旧游标均保留。",'',
     '## 状态口径及逐篇对账','', 'downloaded=原下载状态；valid_pdf=实际文件结构/哈希可读；relevant=至少一个配置领域达到高置信阈值；accepted_for_corpus=有效、身份确认、目标类型、无关键待核实问题，且为article_identity的一个代表版本。duplicate_version只建立关系，文件不删除。metadata_only不计下载成功；needs_review与excluded也不计accepted。', '',f"当前互斥主状态分布：{dump(s['primary_state_counts'])}。布尔标签可重叠，主状态总和可逐篇核对。CORPUS_RECONCILIATION.csv 覆盖全部 {s['records']} 条候选，含相关性、文件、排除和版本关系。",'',f"此前3146 independent downloaded来自旧3154条下载快照，仅按身份去重；此前 {s['legacy_phase1_accepted']} phase1 accepted来自3163条记录，使用旧主题/类型初筛。本版对当前全部记录重新核验，不把这些不同分母和判据混在一起。新严格筛查减少accepted不是丢失PDF。",'',
     '## 1. 日期与年份修复','', '|年份|原始下载记录|规范化下载记录|严格accepted独立成果|','|---|---:|---:|---:|']
    for y in range(2015,2027):lines.append(f"|{y}|{s['original_year_downloaded'].get(str(y),0)}|{s['canonical_year_all_downloaded'].get(str(y),0)}|{s['canonical_year_accepted'].get(str(y),0)}|")
    lines+=['','其他/未知规范年份：'+dump({y:n for y,n in s['canonical_year_all_downloaded'].items() if y not in [str(z) for z in range(2015,2027)]}),'',
     '日期核对计数（原值保留）：'+dump(s.get('date_anomalies',{})),'',
     'original year不覆盖。新增first_submission_date、first_publication_date、published_online_date、published_print_date、source_reported_year、canonical_publication_year、date_source及角色/冲突字段。正式成果优先已经发表的在线/首次日期，预印本保留首次提交时间；只有未来卷期或当前年精度不足时不冒充已确认在线发表。', '',
     'Europe PMC检索FIRST_PDATE和返回firstPublicationDate单独保存，pubYear存为europe_pmc_pub_year/source_reported_year。旧审计422条“年份出筛选区间”实际首次日期全部符合查询区间；这不是过滤失效，而是字段口径差异。', '',
     '2023—2024样本不足的已证实机制：旧脚本将2023—2026合并并newest降序，有限分页首先抓到2026；队列尚未穷尽，下载可得性又影响样本。日期口径解释一部分年份移动，却不能凭它消除采样偏差。没有整个学术领域基线，不能把所有缺口定量归因于单一原因。', '',
     f"新年度独立任务 {s['annual_jobs_total']} 条（每年×领域×主题×来源×中英文查询），保留旧3060条范围队列，不重置其状态或游标。新增任务没有执行检索。每年最小量、领域/年份权重由corpus_config.json调整；规划优先accepted缺少的年份与主题，不默认最新年。",'',
     '## 2. 相关性与技术覆盖','',f"规则使用核心技术概念+领域上下文、同义词、排除/歧义条件、venue及来源分类证据。旧约60%子串规则已替换。只对规则边界候选运行已有本地Qwen，标签为五档；本地模型错误/无证据时保持uncertain，不按数量放宽。规则分数是审计置信等级，未校准为统计概率。边界运行统计：{dump(sem)}。",'',
     '原领域标签未被覆盖；新的逐领域结果独立存表。被移出原领域高置信口径的下载记录/标签数（一个文献可多领域）：'+dump(s['removed_original_domain_labels'])+'；涉及不同已下载文献 '+str(s.get('downloaded_unique_papers_removed_from_at_least_one_original_domain','unknown'))+' 篇。移出标签不等于已证明不相关，边界/缺证据记录单列待复核。','',
     '|领域|accepted独立成果|unmapped|unmapped比例|已分类HHI|已分类有效主题数|最大已知主题及数量|','|---|---:|---:|---:|---:|---:|---|']
    for d in [dv['domain_name'] for dv in cfg['domains']]:
        t=s['topic_metrics'].get(d,{});lines.append(f"|{d}|{t.get('accepted_independent_domain_total',0)}|{t.get('unmapped',0)}|{t.get('unmapped_ratio',0):.1%}|{t.get('hhi_classified_only',0):.3f}|{t.get('effective_topics_classified_only') or 0:.1f}|{t.get('largest_known_topic')} / {t.get('largest_known_topic_count',0)}|")
    lines+=['','unmapped单列，从不当作技术主主题或最大主题。HHI_classified_only=sum((主题数/已分类数)²)，有效主题数=1/HHI。另保存HHI_known_topics_total_denominator=sum((主题数/领域全部accepted数)²)，并报告未分类比例；两个分母明确区分，不用缺失桶夸大集中度。', '',
     '正式分类配置保留96个现有方向；未自动采纳新主题。UNMAPPED_TOPIC_SUGGESTIONS.csv 是标题加权TF-IDF/余弦聚类建议，含成员ID、代表标题、词汇、方法及待审核状态。它可能是同义词、领域边界或配置缺口，不等于已发现新的学科。','',
     '补充分母：全部已下载且相关性达到阈值的领域标签中，unmapped计数为 '+dump(s.get('high_confidence_downloaded_unmapped',{}))+'。上表进一步限定为accepted独立成果，不能与旧原领域标签的unmapped直接比较。','',
     '## 3. 正式发表与版本','',f"建立全候选版本边 {s['version_edges']} 条；已下载样本有 **{s['preprints_linked_to_formal']} 个研究成果**的预印本可关联已确认正式来源（全候选含metadata-only为 {s.get('catalog_preprints_linked_to_formal',s['preprints_linked_to_formal'])} 个）。DOI、arXiv ID、完全相同标题+作者重合+相容日期，以及出版商明确版本关系作为证据。没有证据的相似标题不合并。",'',
     'article_identity、downloaded_version、preprint_source、formal_venue、publisher、repository、pdf_host分开。预印本文件即使论文后来发表，仍保存为其实际下载版本；正式发表元数据单独保留。ISBN书系不自动等同于具体会议；workshop/demo/companion/short paper等分轨独立记录。', '',
     '## 4. 抽样验证与局限','',f"每领域目标至少100篇，覆盖高频/低频、unmapped、相关性边界、版本关系与日期异常。实际验证：{dump(validation)}。明细见acquisition/repair_v2/VALIDATION_SAMPLES.csv，实际PDF标题页及正文抽样文字作为证据，不渲染、不阅读全文。",'',
     '验证使用本地模型作独立内容复核，针对采样页而非完全重复规则。与高置信规则冲突的样本转needs_review，不悄悄当作正确；本地模型仍会出错，目的性分层样本不能直接外推全库错误率。','',
     '下载记录的主状态：'+dump(s.get('downloaded_primary_states',{}))+'；待复核/排除原因：'+dump(s.get('downloaded_issue_counts',{})),'',
     '补全后下载记录元数据非空率：'+dump(s.get('metadata_completeness',{}))+'。document_type包含unknown，非空不等于已核实；预印本没有formal_venue可属于合法缺失，不能伪造为期刊。','',
     '## 5. 下一轮采集优先级','', '建议先处理needs_review及补全来源/日期，再按年度×主题缺口采集。优先2023—2024，其次缺少的2019—2025年份；2026预印本/已在线/未来卷期分开。技术缺口由下列实际accepted主题统计与待审核新主题共同决定：','']
    for dconf in cfg['domains']:
        d=dconf['domain_name'];counts=s['topic_metrics'].get(d,{}).get('multi_label_topics',{});low=[t['name']+' '+str(counts.get(t['topic_id'],0))+'篇' for t in dconf['topics'] if counts.get(t['topic_id'],0)<t['target_count']];lines+=['- '+d+'：'+'；'.join(low)]
    lines+=['', '当前规划前12条（仅排队，未访问数据库来源）：','', '|领域|方向查询|年份|来源|已有accepted领域年份数|','|---|---|---:|---|---:|']
    for j in plan[:12]:lines.append(f"|{j['domain']}|{j['topic']}|{j['lo']}|{j['source']}|{j['observed_accepted_for_domain_year']}|")
    lines+=['', '## 代码、数据库与恢复','',
     '新增corpus_store/metadata/rules/planner/repair/runner模块，沿用collect.py、SQLite jobs、Paper Pilot来源适配器及download_engine.py逐主机限流。配置在corpus_config.json；聚类建议没有改动配置。',
     '- corpus_v2_metadata：规范字段/日期/来源、补全候选及冲突；corpus_v2_candidates保留逐字段值、来源、证据和置信度。',
     '- corpus_v2_relevance：逐领域score/label/evidence/topics/excluded_reason/classifier_version；旧版本可并存。',
     '- corpus_v2_status：统一布尔状态、互斥主状态、article_identity/representative关联。',
     '- corpus_v2_identity/versions/venues：成果、版本与有ISSN证据的出版来源规范表。',
     '- corpus_v2_jobs/semantic_queue/validation/runs/migrations：新队列映射、边界进度、样本、运行及回滚记录。',
     '迁移仅新增表/索引，未ALTER或覆盖原证据列。corpus_migrate.py down会先归档derived-data，回滚只删除corpus_v2_*结构；原表、PDF及已有/新进展分页jobs都保留。回滚代码按备份恢复，不建议用旧完整数据库覆盖后续新增进度。',
     '再次离线分析：run_reclassify.ps1（本地推理结果按输入/配置/模型摘要缓存，断点可恢复）。下阶段采集入口：run_balanced.ps1。默认仅查看规划；实际采集需显式-EnableAcquisition。旧run_phase1.ps1保持历史阶段入口，已完成阶段不自动重新无限采集。',
     '本次没有启动-EnableAcquisition，没有新PDF下载。完整规划在acquisition/repair_v2/NEXT_ACQUISITION_PLAN.csv。每篇去重/排除/接受均可由CORPUS_RECONCILIATION.csv追溯。']
    (ROOT/'LITERATURE_AUDIT_V2.md').write_text('\n'.join(lines),encoding='utf-8')
def main():
    import os
    parser=argparse.ArgumentParser();parser.add_argument('stage',choices=['prepare','semantic','finalize','validate','all']);args=parser.parse_args()
    if (STATE/'collector.lock').exists():raise SystemExit('Collector active; coordinate stop before database repair.')
    lock=WORK/'repair.lock'
    try:fd=os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    except FileExistsError:raise SystemExit('Repair lock exists. Verify its PID; do not run two repair processes.')
    with os.fdopen(fd,'w',encoding='utf-8') as output:output.write(dump({'pid':os.getpid(),'stage':args.stage,'started':now()}))
    try:repair_main(args)
    finally:lock.unlink(missing_ok=True)
def repair_main(args):
    cfg=load_config();db=sqlite3.connect(STATE/'state.sqlite3');migrate(db,(STATE/'repair_backup.txt').read_text(encoding='utf-8'))
    run_id=hashlib.sha256((version(cfg)+datetime.datetime.now().strftime('%Y%m%d')).encode()).hexdigest()[:16]
    db.execute('INSERT OR IGNORE INTO corpus_v2_runs VALUES(?,?,?,?,?,?)',(run_id,now(),None,'running',hashlib.sha256(dump(cfg).encode()).hexdigest(),None));db.commit()
    if args.stage in ['prepare','all']:print(dump(prepare(db,cfg,run_id)),flush=True)
    if args.stage in ['semantic','all']:print(dump(semantic(db,cfg,run_id)),flush=True)
    if args.stage in ['finalize','all']:finalize(db,cfg,run_id)
    if args.stage in ['validate','all']:validate_samples(db,cfg,run_id);finalize(db,cfg,run_id)
    db.close()
if __name__=='__main__':main()
