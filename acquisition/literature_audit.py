"""Immutable-snapshot audit. Never opens the live library for writing or changes PDFs."""
from __future__ import annotations
from project_paths import resolve_project_path, pointer_path
import argparse,csv,datetime,hashlib,html,json,math,pathlib,re,sqlite3,time,unicodedata
from collections import Counter,defaultdict
from difflib import SequenceMatcher
from urllib.parse import urlparse
import pymupdf as fitz
from collect import DOMAINS,TOPICS,norm,doi_norm,paper_keys,validate
from phase1_audit import PhaseRegistry,identity_page,category
from audit_taxonomy import TAXONOMY,GENERAL,SELECTED,CONFERENCES
ROOT=pathlib.Path(__file__).resolve().parent.parent
FOLDER=pointer_path(ROOT/'acquisition/audit/LATEST.txt')
def dump(value):return json.dumps(value,ensure_ascii=False)
def write_json(path,value):path.write_text(dump(value),encoding='utf-8')
def csvwrite(name,rows,fields=None):
    if fields is None:fields=list(dict.fromkeys(k for r in rows for k in r))
    target=ROOT/name if name in ['JOURNAL_RANKING.csv','TOP_PUBLICATIONS.csv','DOMAIN_COVERAGE.csv','YEAR_AUDIT.csv','METADATA_ISSUES.csv'] else FOLDER/name
    temp=target.with_name(target.name+'.tmp')
    with temp.open('w',encoding='utf-8-sig',newline='') as f:
        writer=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for row in rows:writer.writerow({k:dump(v) if isinstance(v,(list,dict,set)) else v for k,v in row.items()})
    temp.replace(target)
def title_norm(title):return norm(re.sub('<[^>]+>',' ',html.unescape(title or '')))
def similarity(a,b):
    a=title_norm(a);b=title_norm(b)
    if not a or not b:return 0
    aa=set(a.split());bb=set(b.split());f1=2*len(aa&bb)/max(1,len(aa)+len(bb))
    return max(f1,SequenceMatcher(None,a,b).ratio())
def dates_crossref(raw):
    result={}
    for key in ['published','published-online','published-print','issued','posted','created','indexed']:
        parts=(raw.get(key) or {}).get('date-parts')
        if parts and parts[0]:result[key]='-'.join(str(n).zfill(4 if i==0 else 2) for i,n in enumerate(parts[0]))
    return result
def crossref_title(raw):
    title=(raw.get('title') or [''])[0];subtitle=(raw.get('subtitle') or [''])[0]
    return title+(': '+subtitle if subtitle and title_norm(subtitle) not in title_norm(title) else '')
def venue_norm(name):
    s=title_norm(name);s=re.sub(r'\b(?:basel switzerland|oxford england|berlin germany|deerfield beach fla|washington d c)\b','',s)
    s=' '.join(s.split())
    aliases={'plos one':'plos one','pnas':'proceedings of the national academy of sciences','pnas usa':'proceedings of the national academy of sciences','arxiv cornell university':'arxiv'}
    return aliases.get(s,s)
def issns(values):return sorted({str(v).upper() for v in values if v and re.fullmatch(r'\d{4}-\d{3}[\dXx]',str(v))})
def fact(provider,raw,p):
    f={'provider':provider,'title':'','doi':'','venue':'','issn':[],'publisher':'','kind':'unknown','dates':{},'year':None,'topics':[],'evidence_url':''}
    if provider=='crossref':
        f.update(title=crossref_title(raw),doi=doi_norm(raw.get('DOI')),venue=(raw.get('container-title') or [''])[0],issn=issns(raw.get('ISSN') or []),publisher=raw.get('publisher') or '',dates=dates_crossref(raw),evidence_url='https://api.crossref.org/works/'+doi_norm(raw.get('DOI')))
        typ=raw.get('type','');f['kind']='conference' if typ=='proceedings-article' else 'journal' if typ=='journal-article' else 'preprint' if typ=='posted-content' else 'non_article' if typ in ['book','book-chapter','dissertation','dataset','component'] else 'unknown'
        if typ=='book-chapter' and (raw.get('event') or {}).get('name'):f.update(kind='conference',venue=raw['event']['name'])
        f['raw_type']=typ
        f['related_dois']=[doi_norm(v.get('id')) for key,vs in (raw.get('relation') or {}).items() if key in ['is-preprint-of','has-preprint','is-version-of','has-version','is-identical-to'] for v in vs if v.get('id-type')=='doi']
        f['year']=int(next((f['dates'][k][:4] for k in ['published-online','published','issued','published-print','posted'] if k in f['dates']),0)) or None
    elif provider=='openalex':
        loc=raw.get('primary_location') or {};source=loc.get('source') or {}
        # Prefer the published journal/conference location to an OA repository.
        for other in raw.get('locations',[]):
            if other.get('is_published') and (other.get('source') or {}).get('type') in ['journal','conference']:
                loc=other;source=other['source'];break
        f.update(title=raw.get('display_name') or raw.get('title') or '',doi=doi_norm(raw.get('doi')),venue=source.get('display_name') or '',issn=issns(source.get('issn') or []),publisher=source.get('host_organization_name') or '',kind=source.get('type') or 'unknown',year=raw.get('publication_year'),dates={'publication_date':raw.get('publication_date')} if raw.get('publication_date') else {},topics=raw.get('topics') or [],evidence_url=raw.get('id') or '')
        f['raw_type']=raw.get('type')
        if raw.get('type') in ['book-chapter','book','dataset','dissertation']:f['kind']='non_article'
        if f['kind']=='repository':f['kind']='preprint' if raw.get('type')=='preprint' else 'repository'
    elif provider=='doaj':
        b=raw.get('bibjson') or {};journal=b.get('journal') or {};ident=b.get('identifier') or []
        f.update(title=b.get('title') or '',doi=next((doi_norm(i.get('id')) for i in ident if i.get('type')=='doi'),''),venue=journal.get('title') or '',issn=issns((journal.get('issns') or [])+[i.get('id') for i in ident if i.get('type') in ['issn','eissn','pissn']]),publisher=journal.get('publisher') or '',kind='journal',year=int(b['year']) if str(b.get('year','')).isdigit() else None,dates={'bibjson_year_month':str(b.get('year'))+'-'+str(b.get('month','')).zfill(2)} if b.get('year') else {},evidence_url='https://doaj.org/article/'+str(raw.get('id') or p.get('source_id') or ''))
    elif provider=='europe_pmc':
        ji=raw.get('journalInfo') or {};journal=ji.get('journal') or {}
        f.update(title=raw.get('title') or '',doi=doi_norm(raw.get('doi')),venue=journal.get('title') or '',issn=issns([journal.get('issn'),journal.get('essn')]),kind='journal' if journal.get('title') else 'unknown',year=int(raw['pubYear']) if str(raw.get('pubYear','')).isdigit() else None,dates={k:raw[k] for k in ['firstPublicationDate','electronicPublicationDate','pubYear'] if raw.get(k)},evidence_url='https://europepmc.org/article/'+str(raw.get('source','MED'))+'/'+str(raw.get('id','')))
    elif provider=='semantic_scholar':
        f.update(title=raw.get('title') or '',doi=doi_norm((raw.get('externalIds') or {}).get('DOI')),venue=raw.get('venue') or '',year=raw.get('year'),dates={'publicationDate':raw['publicationDate']} if raw.get('publicationDate') else {},evidence_url=raw.get('url') or '')
    f['title_similarity']=round(similarity(p.get('title'),f['title']),4)
    f['identity_confirmed']=bool(f['title_similarity']>=.8 and (not p.get('doi') or not f['doi'] or doi_norm(p['doi'])==f['doi']))
    return f
def selected_publication(venue,kind):
    key=venue_norm(venue)
    if kind=='journal':
        if key in {venue_norm(v) for v in GENERAL}:return '综合性旗舰期刊','跨领域','严格完整刊名匹配；不是出版商或下载主机匹配'
        for domain,names in SELECTED.items():
            if key in {venue_norm(v) for v in names}:return '重点专业期刊' if domain!='跨领域' else '重点综合性期刊',domain,'本次公开声明的分领域重点出版物名单；不声称JCR分区或单篇质量'
    if kind=='conference' and not any(w in key for w in ['workshop','companion','demonstration','tutorial','findings','student research','software engineering in practice','software engineering in society']):
        for name,pattern in CONFERENCES.items():
            if re.fullmatch(pattern,key):return '重点会议',('半导体与集成电路' if name in ['DAC','ICCAD','ISSCC','IEDM','VLSI Symposium','ASP-DAC','DATE'] else '软件与通信'),'正式会议完整名称匹配；排除workshop/companion/demo/findings；参考CSRankings及学会技术会议定位'
    return '未列入重点名单','','未列入不等于低质量；缺少可比评估证据'
def explicit_pdf_genre(p,q):
    title=p.get('title') or '';header=q.get('header_excerpt','')
    notice=re.match(r'^(retraction|corrigendum)(?:\s*[:\-]|\s+(?:notice|of|to)\b)',title,re.I)
    if notice:return notice.group(1).lower()
    if re.search(r'\b(?:a dissertation\s+submitted|doctoral thesis|master[’\x27]s thesis|thesis\s+submitted|submitted in partial fulfi(?:l|ll)ment)\b',header[:6000],re.I):return 'dissertation'
    if re.match(r'^(?:supporting information|supplementary (?:information|material)|abstracts? only|table of contents)\b',header.strip(),re.I):return 'supplement_or_cover'
    prefix=re.match(r'^(?:(?:author|publisher)\s+)?(correction|erratum|editorial|obituary|book review|news and views)\b',title,re.I)
    if prefix:return prefix.group(1).lower()
    if re.match(r'^F\d+:',title) and re.search(r'this forum',header[:2000],re.I):return 'forum_session_overview'
    lines=[line.strip() for line in header[:2000].splitlines()]
    if any(line.upper() in ['ARTICLE','RESEARCH ARTICLE','ORIGINAL RESEARCH'] for line in lines[:12]):return ''
    for line in lines[:45]:
        if re.fullmatch(r'EDITORIAL|CORRECTION|ERRATUM|OBITUARY|BOOK REVIEW|NEWS AND VIEWS|RETRACTION|CORRIGENDUM',line,re.I):
            if line.lower()=='correction' and 'correction' in title.lower():continue
            return line.lower()
    return ''
def load():
    db=sqlite3.connect('file:'+str(FOLDER/'snapshot.sqlite3').replace('\\','/')+'?mode=ro',uri=True)
    allrows=[dict(zip(['id','data','status','path','sha','bytes','pages'],r)) for r in db.execute('select id,data,status,path,sha256,bytes,pages from papers')]
    for r in allrows:r['p']=json.loads(r.pop('data'))
    rows={r['id']:r for r in allrows if r['status']=='downloaded'}
    return db,allrows,rows
def duplicate_groups(rows):
    registry=PhaseRegistry.__new__(PhaseRegistry);registry.rows={pid:{'p':r['p'],'path':r['path'],'sha':r['sha']} for pid,r in rows.items()}
    return registry.groups()
def choose_samples(rows,groups):
    representatives=[min(ids) for ids in groups.values()];samples={d:[] for d in DOMAINS}
    for d in DOMAINS:
        strata=defaultdict(list)
        for pid in representatives:
            p=rows[pid]['p']
            if d in p.get('domains',[]):strata[(p.get('year'),venue_norm(p.get('venue') or 'unknown'))].append(pid)
        for key,pool in strata.items():pool.sort(key=lambda pid:hashlib.sha256((d+pid+'audit50').encode()).hexdigest())
        keys=sorted(strata,key=lambda k:(-len(strata[k]),str(k)));roundn=0
        while len(samples[d])<50:
            added=0
            for key in keys:
                if roundn<len(strata[key]):samples[d].append(strata[key][roundn]);added+=1
                if len(samples[d])>=50:break
            if not added:break
            roundn+=1
    write_json(FOLDER/'sample_ids.json',samples);return samples
def pdf_audit():
    db,allrows,rows=load();groups=duplicate_groups(rows);samples=choose_samples(rows,groups);sampleids=set(pid for v in samples.values() for pid in v)
    cache=FOLDER/'pdf_checks.jsonl';done={}
    if cache.exists():
        for line in cache.read_text(encoding='utf-8').splitlines():
            try:q=json.loads(line);done[q['id']]=q
            except ValueError:pass
    for i,(pid,r) in enumerate(rows.items()):
        if pid in done:continue
        p=r['p'];path=resolve_project_path(r['path']) if r['path'] else None
        q={'id':pid,'path':r['path'],'structural_valid':False,'file_unchanged':False,'identity_confirmed':False,'sampled_domains':[d for d in DOMAINS if pid in samples[d]],'errors':[]}
        try:
            stat=path.stat();content=path.read_bytes();q['sha256']=hashlib.sha256(content).hexdigest();q['file_bytes']=len(content);q['file_unchanged']=q['sha256']==r['sha'] and stat.st_size==r['bytes']
            try:info=validate(content,p);q['structural_valid']=True;q['validation']=info
            except Exception as error:q['errors'].append(str(error))
            with fitz.open(stream=content,filetype='pdf') as pdf:
                index,header,score,doi_found=identity_page(pdf,p);q.update(pages=len(pdf),identity_page=index+1,title_score=round(score,4),identity_confirmed=score>=.35 or doi_found,repaired=pdf.is_repaired,encrypted=pdf.is_encrypted)
                front='\n'.join(pdf[n].get_text() for n in range(min(3,len(pdf))));q['header_excerpt']=header[:5000];q['front_text']=front[:15000]
                q['pdf_dois']=sorted(set(doi_norm(x.rstrip('.,;)')) for x in re.findall(r'10\.\d{4,9}/[^\s<>"\[\]]+',header,re.I)))
                q['year_evidence']=re.findall(r'.{0,70}(?:©|copyright|published|publication|accepted|received|arxiv|[12]\d{3}).{0,100}',front,re.I)[:30]
                q['copyright_years']=sorted(set(re.findall(r'(?:©|copyright)\s*(?:\(c\)\s*)?([12]\d{3})',front,re.I)))
                q['header_years']=sorted(set(re.findall(r'\b(19\d{2}|20\d{2})\b',header)))
                if pid in sampleids:
                    indices=sorted(set([index,0,len(pdf)//3,2*len(pdf)//3,len(pdf)-1]));q['sample_pages']=[n+1 for n in indices];q['body_excerpt']='\n'.join(pdf[n].get_text()[:4000] for n in indices[2:])[:12000]
                stat_after=path.stat();q['stat_unchanged_during_audit']=stat.st_size==stat_after.st_size and stat.st_mtime_ns==stat_after.st_mtime_ns
        except Exception as error:q['errors'].append(type(error).__name__+': '+str(error))
        with cache.open('a',encoding='utf-8') as f:f.write(dump(q)+'\n')
        if (i+1)%100==0:print(dump({'pdf_checked':i+1,'total':len(rows)}),flush=True)
    print(dump({'pdf_audit_done':len(rows),'sample_counts':{d:len(v) for d,v in samples.items()}}),flush=True)

PATTERNS={d:[(en,zh,re.compile(pattern+'|'+re.escape(zh)+'|'+re.escape(next((z for e,z in TOPICS[d] if e==en),zh)),re.I)) for en,zh,pattern in ts] for d,ts in TAXONOMY.items()}
DOMAIN_ANCHORS={
 '生物制药':r'pharma|drug|antibod|protein|gene therap|mrna|vaccine|cell therap|bioprocess|biomanufact|crispr|biologic|metabolic engineer|药|抗体|蛋白|疫苗|基因治疗',
 '软件与通信':r'software|computer|algorithm|machine learning|deep learning|neural network|wireless|communication|language model|database|operating system|internet|cryptograph|network|人工智能|软件|通信|计算机',
 '新材料':r'material|perovskite|batter|nanoparticle|nanocrystal|polymer|ceramic|alloy|cataly|superconduct|composite|graphene|ferroelectric|材料|电池|催化|高分子',
 '半导体与集成电路':r'semiconductor|transistor|mosfet|cmos|lithograph|chiplet|chip|integrated circuit|silicon photonic|gallium nitride|silicon carbide|rram|mram|epitax|vlsi|neuromorph|photonic integrat|半导体|芯片|集成电路'}
def topic_matches(p,extra=''):
    text=(p.get('title') or '')+'\n'+(p.get('abstract') or '')+'\n'+' '.join(p.get('keywords') or [])+'\n'+extra
    return {d:[en for en,zh,pattern in PATTERNS[d] if pattern.search(text)] for d in DOMAINS}
def enrich(db,rows):
    facts=defaultdict(list)
    def add(pid,p):
        if pid not in rows:return
        raw=p.get('raw_source_evidence') or {};provider=p.get('source')
        if raw and provider in ['openalex','doaj','europe_pmc','crossref','semantic_scholar']:facts[pid].append(fact(provider,raw,rows[pid]['p']))
    for pid,r in rows.items():add(pid,r['p'])
    for pid,payload in db.execute("select paper_id,payload from observations where paper_id in (select id from papers where status='downloaded') and instr(job_id,'_download')=0"):
        try:add(pid,json.loads(payload))
        except (ValueError,TypeError):pass
    crossref={}
    for path in (FOLDER/'crossref').glob('*.json'):
        try:record=json.loads(path.read_text(encoding='utf-8'));crossref[doi_norm(record['doi'])]=record
        except ValueError:pass
    supplements=json.loads((FOLDER/'supplement_metadata.json').read_text(encoding='utf-8')) if (FOLDER/'supplement_metadata.json').exists() else {}
    enriched={}
    priorities={'crossref':0,'openalex':1,'doaj':2,'europe_pmc':3,'semantic_scholar':4}
    downloads={pid:(url,host) for pid,url,host in db.execute("select paper_id,url,host from http_attempts where stage='download' and outcome='success' order by id")}
    for pid,r in rows.items():
        p=r['p'];doi=doi_norm(p.get('doi'));cr=crossref.get(doi)
        supplement=supplements.get(pid,{})
        if not cr and supplement.get('verified'):cr=dict(status=200,message=supplement['message'])
        if cr and cr.get('status')==200:
            fresh=fact('crossref',cr['message'],p);fresh.update(evidence_mode='live_free_api',verified_at=cr.get('retrieved_at') or supplement.get('verified_at'));facts[pid].append(fresh)
        fs=sorted({dump(f):f for f in facts[pid]}.values(),key=lambda f:(priorities.get(f['provider'],9),0 if f.get('evidence_mode')=='live_free_api' else 1))
        reliable=[f for f in fs if f['identity_confirmed']];formal=[f for f in reliable if f['kind'] in ['journal','conference'] and f['venue']]
        chosen=formal[0] if formal else next((f for f in reliable if f['venue'] and f['kind'] not in ['repository','unknown']),None)
        preprint=category(p)=='preprint' or any(f['kind']=='preprint' for f in reliable) or any(s in venue_norm(p.get('venue') or '') for s in ['arxiv','biorxiv','medrxiv','research square','preprint'])
        if not chosen and preprint:
            server=next((f['venue'] for f in reliable if f['kind'] in ['preprint','repository'] and f['venue']),p.get('venue') or '')
            url=p.get('downloaded_from') or downloads.get(pid,('',''))[0]
            if 'arxiv' in url or 'arxiv' in venue_norm(server):server='arXiv'
            elif 'biorxiv' in url:server='bioRxiv'
            elif 'medrxiv' in url:server='medRxiv'
            elif doi.startswith('10.21203/'):server='Research Square'
            chosen={'venue':server or 'Unknown preprint server','kind':'preprint','publisher':next((f['publisher'] for f in reliable if f['publisher']),''),'issn':[],'provider':'repository_record','evidence_url':url}
        venue=html.unescape(chosen['venue'] if chosen else p.get('venue') or 'Unknown formal publication');kind=chosen['kind'] if chosen else 'unverified_venue'
        if venue_norm(venue) in ['pubmed','europe pmc','pubmed central','pmc','crossref','openalex','semantic scholar']:venue='Unknown formal publication';kind='unknown'
        allissn=sorted({s for f in reliable if venue_norm(f['venue'])==venue_norm(venue) for s in f['issn']})
        publisher=next((f['publisher'] for f in reliable if f['publisher'] and venue_norm(f['venue'])==venue_norm(venue)),chosen.get('publisher','') if chosen else '')
        preferred_name=next((v for v in GENERAL+[v for vs in SELECTED.values() for v in vs] if venue_norm(v)==venue_norm(venue)),None)
        if preferred_name:venue=preferred_name
        if kind=='preprint' and venue_norm(venue)=='arxiv':venue='arXiv'
        tier,field,criterion=selected_publication(venue,kind)
        alltopics=[t for f in reliable for t in f.get('topics',[])];topicnames=[]
        for t in alltopics:
            topicnames += [t.get('display_name','')]+[(t.get(k) or {}).get('display_name','') for k in ['subfield','field','domain']]
        url=p.get('downloaded_from') or downloads.get(pid,('',''))[0];host=urlparse(url).hostname or ''
        crossreffact=next((f for f in fs if f['provider']=='crossref'),None)
        enriched[pid]={'id':pid,'title':p.get('title'),'doi_original':doi,'doi_confirmed':doi if any(f['doi']==doi and f['identity_confirmed'] for f in reliable) else '',
         'doi_supplement':next((f['doi'] for f in reliable if f['doi']),''),'venue_original':p.get('venue'),'publication_name':venue,'publication_kind':kind,'issn':allissn,'publisher':publisher,'publication_evidence':chosen.get('evidence_url','') if chosen else '',
         'verification_provider':chosen.get('provider','') if chosen else '', 'publication_confirmed':bool(chosen and kind in ['journal','conference']), 'download_host':host or 'Unknown actual download host','download_url':url,
         'downloaded_version':'preprint' if preprint or host and 'arxiv' in host else 'unknown_or_publisher_version','original_type':p.get('document_type'),'tier':tier,'selection_field':field,'selection_criterion':criterion,
         'crossref_status':cr.get('status') if cr else 'not_requested_or_no_doi','crossref_title_similarity':crossreffact['title_similarity'] if crossreffact else None,'crossref_type':cr.get('message',{}).get('type') if cr else None,
         'original_year':p.get('year'),'original_date':p.get('publication_date'),'original_date_source':p.get('publication_date_source'),'facts':fs,'academic_topics':topicnames}
    write_json(FOLDER/'enriched_metadata.json',enriched);return enriched

def build_report():
    db,allrows,rows=load();groups=duplicate_groups(rows);samples=json.loads((FOLDER/'sample_ids.json').read_text(encoding='utf-8'))
    checks={q['id']:q for q in (json.loads(line) for line in (FOLDER/'pdf_checks.jsonl').read_text(encoding='utf-8').splitlines())}
    meta=enrich(db,rows);issues=[]
    # Include only verified additional DOIs / declared version relations in deduplication.
    grouping_rows={pid:dict(r,p=dict(r['p'],doi=r['p'].get('doi') or meta[pid]['doi_supplement'])) for pid,r in rows.items()}
    groups=duplicate_groups(grouping_rows)
    doi_owner={doi_norm(r['p'].get('doi')):pid for pid,r in grouping_rows.items() if r['p'].get('doi')}
    for pid,m in meta.items():
        for f in m['facts']:
            if not f['identity_confirmed']:continue
            for related in f.get('related_dois',[]):
                other=doi_owner.get(related)
                if not other:continue
                a=next((key for key,ids in groups.items() if pid in ids),None);b=next((key for key,ids in groups.items() if other in ids),None)
                if a is not None and b is not None and a!=b:groups[a]+=groups.pop(b)
    def issue(pid,kind,severity,evidence,suggestion='',scope='downloaded'):
        p=rows[pid]['p'] if pid in rows else next((r['p'] for r in allrows if r['id']==pid),{})
        issues.append({'paper_id':pid,'title':p.get('title'),'doi':p.get('doi'),'domains':p.get('domains'),'scope':scope,'issue_type':kind,'severity':severity,'evidence':evidence,'suggestion':suggestion,'pdf_path':rows.get(pid,{}).get('path','')})
    canonical={};duplicates={}
    for gid,ids in groups.items():
        good=[pid for pid in ids if checks.get(pid,{}).get('structural_valid') and checks[pid].get('identity_confirmed') and checks[pid].get('file_unchanged')]
        representative=max(good or ids,key=lambda pid:(meta[pid]['publication_confirmed'],bool(rows[pid]['p'].get('doi')),bool(rows[pid]['p'].get('abstract')),pid));canonical[representative]=ids
        if len(ids)>1:
            for pid in ids:duplicates[pid]=representative;issue(pid,'duplicate_or_version','review',dump({'group':ids,'representative':representative,'doi':rows[pid]['p'].get('doi'),'sha256':rows[pid]['sha']}),'保留文件；知识库按规范文献组计数，分别保存版本')
    for pid,r in rows.items():
        p=r['p'];m=meta[pid];q=checks.get(pid,{})
        for field in ['doi','authors','abstract','keywords','venue','year','document_type']:
            if not p.get(field) or p.get(field)=='unknown':issue(pid,'missing_'+field,'missing','原始字段为空或unknown','补查后单独保存带来源的字段，不盲目回写')
        if not m['publication_confirmed'] and m['publication_kind']!='preprint':issue(pid,'formal_publication_unknown','unknown',dump({'original_venue':p.get('venue'),'facts':m['facts']}),'用DOI或正式会议记录确认期刊/会议；不能把下载主机当发表来源')
        if not m['publisher']:issue(pid,'publisher_unknown','unknown','未获得与已确认刊名对应的出版商记录')
        if m['publication_kind']=='journal' and not m['issn']:issue(pid,'issn_unknown','unknown','期刊存在但未取得可信ISSN')
        if not q.get('file_unchanged'):issue(pid,'file_hash_or_size_mismatch','error',dump({k:q.get(k) for k in ['file_unchanged','sha256','file_bytes']}),'核对备份与入库记录，不覆盖文件')
        if not q.get('structural_valid') or not q.get('identity_confirmed'):
            structural_problem=q.get('repaired') or q.get('encrypted') or any(not e.startswith('title_mismatch:') for e in q.get('errors',[]))
            issue(pid,'pdf_structural_failure' if structural_problem else 'pdf_identity_unconfirmed','error' if structural_problem else 'review',dump({'errors':q.get('errors'),'title_score':q.get('title_score'),'identity_page':q.get('identity_page'),'hash_matches':q.get('file_unchanged'),'header_excerpt':q.get('header_excerpt','')[:1000]}),'隔离索引标记并人工复核语言/标题/DOI；不删除原文件')
        genre=explicit_pdf_genre(p,q)
        if genre:issue(pid,'non_target_pdf_genre','review',dump({'explicit_pdf_heading':genre,'excerpt':q.get('header_excerpt','')[:1600]}),'标题页体裁或论坛语境明示；按目标类型重审，不删除文件')
        if m['crossref_title_similarity'] is not None and m['crossref_title_similarity']<.8:issue(pid,'doi_title_mismatch_suspected','error' if m['crossref_title_similarity']<.5 else 'review',dump({'crossref_similarity':m['crossref_title_similarity'],'source_title':next((f['title'] for f in m['facts'] if f['provider']=='crossref'),'')}),'比对标题页、DOI和作者；不能仅凭相似标题自动合并')
        if m['publication_confirmed'] and category(p)=='preprint':issue(pid,'preprint_has_formal_publication','review',dump({'formal_name':m['publication_name'],'formal_kind':m['publication_kind'],'download_version':m['downloaded_version']}),'拆分文献发表类型与下载版本标签，保留两套日期')
        if m['publication_kind']=='preprint' and category(p) not in ['preprint','unknown']:issue(pid,'preprint_misclassified','review',dump({'original_type':p.get('document_type'),'server':m['publication_name']}),'检查正式发表证据后再调整类型')
        if m['publication_kind']=='conference' and category(p) in ['journal_paper','unknown']:issue(pid,'conference_type_unrecorded','review',dump({'original_type':p.get('document_type'),'venue':m['publication_name']}),'会议来源与来源type共同核实')
        nonarticle=[f for f in m['facts'] if f['kind']=='non_article' and f['identity_confirmed']]
        if nonarticle:issue(pid,'published_type_requires_review','review',dump(nonarticle),'区分百科/书章节与真实会议proceedings章节；不能把所有book-chapter直接判为无关')
        if re.search('<[a-zA-Z][^>]*>',p.get('abstract') or ''):issue(pid,'abstract_contains_markup','review','摘要包含HTML/JATS标签','生成纯文本摘要字段并保留原始标记，不改变PDF')
        stale=[k for k in p.get('metadata_gaps',[]) if p.get(k) and p.get(k)!='unknown']
        if stale:issue(pid,'stale_metadata_gaps','review',dump(stale),'缺失字段清单应按当前值重算，不能沿用首次记录')
        if m['crossref_status'] in [404,'network_error']:issue(pid,'doi_lookup_unresolved','unknown',dump({'status':m['crossref_status'],'doi':p.get('doi')}),'可能为其他注册机构或接口问题；不据404认定DOI伪造')
        if p.get('venue') and venue_norm(p['venue']) in ['pubmed','europe pmc','pmc','arxiv'] and m['publication_confirmed']:issue(pid,'repository_vs_publication_conflation','review',dump({'original_venue':p['venue'],'formal_venue':m['publication_name']}),'分别存formal_venue、repository、pdf_host字段')
    # Journal/conference ranking counts all downloaded independent works, not candidate metadata or tag sums.
    rankings=defaultdict(list)
    for representative,ids in canonical.items():
        m=meta[representative];rankname=m['publication_name'];tier,field,criteria=selected_publication(rankname,m['publication_kind'])
        if tier=='重点会议':
            rankname=next((name for name,pattern in CONFERENCES.items() if re.fullmatch(pattern,venue_norm(rankname))),rankname)
        rankings[(m['publication_kind'],venue_norm(rankname))].append(representative)
    ranked=[];top=[]
    for key,ids in rankings.items():
        infos=[meta[pid] for pid in ids];display=max((m['publication_name'] for m in infos),key=lambda s:(s[:1].isupper(),len(s)));tier,field,criteria=selected_publication(display,key[0])
        structural=sum(bool(checks.get(pid,{}).get('structural_valid')) for pid in ids)
        domaincounts={d:sum(any(d in rows[x]['p'].get('domains',[]) for x in canonical[pid]) for pid in ids) for d in DOMAINS}
        ranked.append({'rank':0,'publication_name':display,'publication_kind':key[0],'unique_downloaded_papers':len(ids),'downloaded_records':sum(len(canonical[pid]) for pid in ids),'structural_pdf_pass':structural,'confirmed_publication_papers':sum(meta[pid]['publication_confirmed'] for pid in ids),'issn':sorted({s for m in infos for s in m['issn']}),'publishers':sorted({m['publisher'] for m in infos if m['publisher']}),'tier':tier,'selection_field':field,'domain_counts':domaincounts,'original_name_variants':sorted({str(rows[x]['p'].get('venue') or '') for pid in ids for x in canonical[pid]}),'download_hosts':dict(Counter(m['download_host'] for m in infos)),'evidence_providers':dict(Counter(m['verification_provider'] or 'unknown' for m in infos))})
        for pid in ids:
            m=meta[pid]
            if m['publication_confirmed'] and m['tier']!='未列入重点名单':
                heading=explicit_pdf_genre(rows[pid]['p'],checks[pid])
                top.append({'paper_id':pid,'title':m['title'],'doi':m['doi_confirmed'] or m['doi_supplement'] or 'UNKNOWN','publication_name':m['publication_name'],'publication_kind':m['publication_kind'],'issn':m['issn'],'publisher':m['publisher'],'tier':m['tier'],'selection_field':m['selection_field'],'paper_domains':sorted({d for x in canonical[pid] for d in rows[x]['p'].get('domains',[])}),'matched_topics':topic_matches(rows[pid]['p']),'publication_evidence':m['publication_evidence'],'verification_provider':m['verification_provider'],'crossref_title_similarity':m['crossref_title_similarity'],'classification_basis':m['selection_criterion'],'downloaded_version':m['downloaded_version'],'document_type_original':rows[pid]['p'].get('document_type'),'pdf_explicit_genre':heading, 'pdf_identity_confirmed':checks[pid].get('identity_confirmed'),'pdf_path':rows[pid]['path']})
    # Full-catalog raw-venue counts are separate from verified downloaded source counts.
    raw_catalog=defaultdict(list)
    for r in allrows:raw_catalog[venue_norm(r['p'].get('venue') or 'Unknown original venue')].append(r)
    for r in ranked:r.update(row_scope='independent_downloaded',all_catalog_raw_venue_candidates=0,catalog_status_counts={})
    for key,candidates in raw_catalog.items():
        existing=[r for r in ranked if venue_norm(r['publication_name'])==key]
        if existing:target=max(existing,key=lambda r:r['unique_downloaded_papers'])
        else:
            target={'rank':0,'publication_name':candidates[0]['p'].get('venue') or 'Unknown original venue','publication_kind':'metadata_only_unverified','unique_downloaded_papers':0,'downloaded_records':0,'structural_pdf_pass':0,'confirmed_publication_papers':0,'issn':[],'publishers':[],'tier':'未核实的候选来源','selection_field':'','domain_counts':{},'original_name_variants':sorted({r['p'].get('venue') or '' for r in candidates}),'download_hosts':{},'evidence_providers':{},'row_scope':'catalog_metadata_only'};ranked.append(target)
        target.update(all_catalog_raw_venue_candidates=len(candidates),catalog_status_counts=dict(Counter(r['status'] for r in candidates)))
    ranked.sort(key=lambda r:(-r['unique_downloaded_papers'],-r.get('all_catalog_raw_venue_candidates',0),r['publication_name']))
    for i,r in enumerate(ranked):r['rank']=i+1
    csvwrite('JOURNAL_RANKING.csv',ranked);csvwrite('TOP_PUBLICATIONS.csv',top,['paper_id','title','doi','publication_name','publication_kind','issn','publisher','tier','selection_field','paper_domains','matched_topics','publication_evidence','verification_provider','crossref_title_similarity','classification_basis','downloaded_version','document_type_original','pdf_explicit_genre','pdf_identity_confirmed','pdf_path'])
    # Independent text vocabulary; original acquisition query tags are retained separately.
    topicrows=[];domain_stats={};topids={r['paper_id'] for r in top};primary_assign={};matches={};domain_ids={}
    for d in DOMAINS:
        ids=[pid for pid,group in canonical.items() if any(d in rows[x]['p'].get('domains',[]) for x in group)];domain_ids[d]=ids
        counts=Counter();tagcounts=Counter();primary=Counter()
        for pid in ids:
            p=rows[pid]['p'];academic=' '.join(meta[pid]['academic_topics']);matched=topic_matches(p,academic)[d];matches[(pid,d)]=matched
            counts.update(matched);tags={norm(t.removeprefix('pilot:')) for x in canonical[pid] for t in rows[x]['p'].get('topics',[])}
            for en,zh in TOPICS[d]:tagcounts[en]+=int(norm(en) in tags or norm(zh) in tags)
            scores={en:3*bool(pattern.search(p.get('title') or ''))+bool(pattern.search(p.get('abstract') or ''))+int(norm(en) in tags)*.1 for en,zh,pattern in PATTERNS[d] if en in matched}
            winner=max(scores,key=scores.get) if scores else 'unmapped';primary[winner]+=1;primary_assign[(pid,d)]=winner
        hhi=sum((n/max(1,len(ids)))**2 for n in primary.values());domain_stats[d]={'unique_tagged_papers':len(ids),'multi_label_content_counts':dict(counts),'primary_topic_counts':dict(primary),'primary_hhi':hhi,'effective_primary_topics':1/hhi if hhi else 0,'top_primary_share':primary.most_common(1)[0][1]/max(1,len(ids)) if primary else 0,'unmapped':primary.get('unmapped',0)}
        for en,zh,pattern in PATTERNS[d]:
            n=counts[en];topicrows.append({'domain':d,'topic':en,'topic_zh':zh,'content_matched_unique_downloaded':n,'original_query_tag_count':tagcounts[en],'domain_library_total':len(ids),'observed_library_share':n/max(1,len(ids)),'primary_assigned_count':primary[en],'confirmed_selected_publication_papers':sum(pid in topids and en in matches[(pid,d)] for pid in ids),'not_in_original_query_framework':en not in {x[0] for x in TOPICS[d]},'matching_pattern':pattern.pattern,'evidence_basis':'标题+摘要+关键词+可信OpenAlex分类字段；词法多标签，不是领域总体覆盖率','library_gap_assessment':'未检出；建议定向补查，不能认定该技术无文献' if not n else '本库稀少(<10)；需核对同义词与重要性' if n<10 else '本库有可观测样本，不能据此推断学术领域覆盖率'})
    csvwrite('DOMAIN_COVERAGE.csv',topicrows)
    # Per-paper year evidence plus long-form distributions, source histograms, and executed jobs.
    yearrows=[];disagreements=[];year_counter=Counter();sourceyears=Counter();onlineyears=Counter();future=[]
    for pid,ids in canonical.items():
        p=rows[pid]['p'];m=meta[pid];q=checks[pid];year=p.get('year');year_counter[str(year or 'unknown')]+=1;sourceyears[(p.get('source') or 'unknown',str(year or 'unknown'))]+=1
        cr=next((f for f in m['facts'] if f['provider']=='crossref' and f['identity_confirmed']),None);datefacts=cr['dates'] if cr else {}
        candidate=datefacts.get('published-online') or datefacts.get('published') or datefacts.get('issued') or datefacts.get('published-print') or datefacts.get('posted') or ''
        onlineyear=int(candidate[:4]) if candidate[:4].isdigit() else None
        if onlineyear:onlineyears[str(onlineyear)]+=1
        differing=bool(year and onlineyear and int(year)!=onlineyear)
        evidence={'original':{'year':year,'date':p.get('publication_date'),'source':p.get('publication_date_source')},'provider_dates':{f['provider']:f['dates'] for f in m['facts'] if f['identity_confirmed']},'pdf_copyright_years':q.get('copyright_years'), 'pdf_header_years':q.get('header_years'),'pdf_excerpt':q.get('year_evidence')}
        assessment='Crossref发表日期与本库年份一致' if cr and not differing else '未取得足够独立日期证据；未更改年份'
        if differing:
            assessment='日期口径不一致，可能是在线/卷期/预印本差异；需保留各日期，不能直接判定错误';disagreements.append(pid);issue(pid,'publication_year_disagreement','review',dump(evidence),'优先核对published-online、published-print与first_submission；保留原值')
        if q.get('copyright_years') and str(year) not in q['copyright_years']:issue(pid,'pdf_copyright_year_disagreement','review',dump(evidence),'版权页/许可页可能与发表年不同；核对标题页，不按正文引文年份回写')
        if p.get('publication_date') and str(p['publication_date'])[:10]>'2026-10-06':future.append(pid);issue(pid,'future_publication_date','review',dump(evidence),'区分预排卷期和实际在线发表，不直接删除记录')
        yearrows.append({'row_type':'paper_evidence','paper_id':pid,'title':p.get('title'),'doi':p.get('doi'),'year':year,'source':p.get('source'),'publication_name':m['publication_name'],'download_host':m['download_host'],'original_date':p.get('publication_date'),'original_date_source':p.get('publication_date_source'),'crossref_online_date':datefacts.get('published-online',''),'crossref_print_date':datefacts.get('published-print',''),'crossref_published_date':datefacts.get('published',''),'crossref_posted_date':datefacts.get('posted',''),'candidate_online_first_year':onlineyear or '', 'year_disagreement':differing,'pdf_copyright_years':q.get('copyright_years'),'pdf_title_page':q.get('identity_page'),'count':1,'assessment':assessment,'evidence':evidence})
    for year,n in sorted(year_counter.items()):yearrows.append({'row_type':'library_year_distribution','year':year,'count':n,'assessment':'独立已下载文献的原始年份；不推算领域总体'})
    for (source,year),n in sorted(sourceyears.items()):yearrows.append({'row_type':'search_source_year_distribution','source':source,'year':year,'count':n})
    for (host,year),n in sorted(Counter((meta[pid]['download_host'],str(rows[pid]['p'].get('year') or 'unknown')) for pid in canonical).items()):yearrows.append({'row_type':'download_host_year_distribution','source':host,'year':year,'count':n})
    jobstats=[dict(zip(['source','lo','hi','sort','pages_done','jobs_total','pages_results'],r)) for r in db.execute("select source,lo,hi,sort,sum(status='done'),count(*),sum(case when status='done' then coalesce(total,0) else 0 end) from jobs group by source,lo,hi,sort")]
    for j in jobstats:yearrows.append({'row_type':'query_queue_execution','source':j['source'],'year':str(j['lo'])+'-'+str(j['hi']),'count':j['pages_done'],'assessment':'sort='+j['sort']+'; 持久化任务数='+str(j['jobs_total'])+'，done为已执行分页任务，不是穷尽率','evidence':j})
    filter_violations=[dict(zip(['paper_id','source','reported_year','lo','hi','job_id','raw_first_publication_date'],r)) for r in db.execute("select o.paper_id,o.source,json_extract(o.payload,'$.year'),j.lo,j.hi,j.id,json_extract(o.payload,'$.raw_source_evidence.firstPublicationDate') from observations o join jobs j on o.job_id=j.id where json_extract(o.payload,'$.year') is not null and (cast(json_extract(o.payload,'$.year') as integer)<j.lo or cast(json_extract(o.payload,'$.year') as integer)>j.hi)")]
    for v in filter_violations:
        first=v.get('raw_first_publication_date') or '';v['first_date_inside_filter']=first[:4].isdigit() and v['lo']<=int(first[:4])<=v['hi']
        yearrows.append({'row_type':'source_year_filter_conflict','paper_id':v['paper_id'],'source':v['source'],'year':v['reported_year'],'count':1,'evidence':v,'assessment':'FIRST_PDATE符合筛选区间，但入库year取pubYear，已确认日期口径差异，不是年份过滤失效' if v['first_date_inside_filter'] else 'reported_year在区间外且缺少相应筛选日期证据；需核对'})
    csvwrite('YEAR_AUDIT.csv',yearrows)
    # Stratified nonprobability PDF samples: conservative content relevance, explicit uncertainty.
    sample_rows=[];sample_summary={}
    decisions=json.loads((FOLDER/'manual_sample_review.json').read_text(encoding='utf-8')) if (FOLDER/'manual_sample_review.json').exists() else {}
    for d,ids in samples.items():
        results=Counter()
        for pid in ids:
            p=rows[pid]['p'];q=checks[pid];m=meta[pid];header=q.get('header_excerpt','');title=p.get('title') or '';abstract=p.get('abstract') or ''
            direct_title=bool(re.search(DOMAIN_ANCHORS[d],title,re.I));direct_abstract=bool(re.search(DOMAIN_ANCHORS[d],abstract,re.I));pdf_anchor=bool(re.search(DOMAIN_ANCHORS[d],header,re.I));body_anchor=bool(re.search(DOMAIN_ANCHORS[d],q.get('body_excerpt',''),re.I))
            verdict='confirmed_within_rule' if direct_title and pdf_anchor and q.get('identity_confirmed') or direct_abstract and pdf_anchor and body_anchor and q.get('identity_confirmed') else 'uncertain_context_requires_review'
            if not q.get('identity_confirmed') or not q.get('structural_valid'):verdict='identity_or_pdf_failure'
            # Mechanical, clinical-care and environmental uses of chip/systems are not automatic domain matches.
            offdomain=bool(re.search(r'\b(?:cement|milling|ballistic|pyrolys\w*|economic\w*|health care|healthcare|patient satisfaction|education|soil|hydrolog\w*|flood\w*|river|tillage|sewer|surgical|orthop\w*|land cover|personality|tourism|leadership)\b',title,re.I))
            if offdomain and not direct_title:verdict='suspected_irrelevant'
            if d=='半导体与集成电路' and re.search(r'lab.on.a.chip|organ.on.a.chip|microfluid',title,re.I) and not re.search(r'cmos|semiconductor|transistor|integrated circuit|photonic|silicon',title+' '+abstract,re.I):verdict='suspected_irrelevant'
            if d=='软件与通信' and re.search(r'hydrolog|water|land cover|flood|river|tillage|soil',title,re.I) and not re.search(r'software|computer|machine learning|deep learning|network secur|wireless',title,re.I):verdict='uncertain_context_requires_review'
            automatic_verdict=verdict;decision=decisions.get(pid)
            if decision:
                verdict=decision['verdict']
                if decision.get('extra_issue'):issue(pid,decision['extra_issue'],'review',decision['reason'],'保留原文件，在增量索引中复核并标注')
            results[verdict]+=1
            evidence={'title_anchor':direct_title,'abstract_anchor':direct_abstract,'pdf_header_anchor':pdf_anchor,'sample_body_anchor':body_anchor,'identity_title_score':q.get('title_score'),'header_excerpt':header[:2200],'body_excerpt':q.get('body_excerpt','')[:1200],'abstract_excerpt':abstract[:1800]}
            sample_rows.append({'domain':d,'paper_id':pid,'title':title,'doi':p.get('doi'),'year':p.get('year'),'publication':m['publication_name'],'sample_pages':q.get('sample_pages'),'automatic_verdict':automatic_verdict,'verdict':verdict,'semantic_reason':decision.get('reason','') if decision else '', 'evidence':evidence,'reviewer':decision['reviewer'] if decision else 'deterministic_content_rules_v1','limitation':'标题/摘要及分层抽样页核验；未阅读全文，不评价实验设计；非概率样本不可外推总体错误率'})
            if verdict!='confirmed_within_rule':issue(pid,'sample_relevance_'+verdict,'review',dump({'domain':d,**evidence}),'语义复核确认后标注域标签；不修改PDF')
        sample_summary[d]=dict(results,sampled=len(ids))
    csvwrite('sample_review.csv',sample_rows)
    for pid,r in rows.items():
        p=r['p'];domainconfirmed=any(re.search(DOMAIN_ANCHORS[d],(p.get('title') or '')+' '+(p.get('abstract') or ''),re.I) for d in p.get('domains',[]) if d in DOMAIN_ANCHORS)
        if not domainconfirmed:issue(pid,'subject_scope_unconfirmed','review','标题及摘要未命中所标领域技术词；可能是同义词或无关记录','人工复核所属领域，不能用简单词法认定所有记录无关')
    csvwrite('METADATA_ISSUES.csv',issues,['paper_id','title','doi','domains','scope','issue_type','severity','evidence','suggestion','pdf_path'])
    completeness={}
    for scope,ps in [('all_candidates',[r['p'] for r in allrows]),('downloaded_records',[r['p'] for r in rows.values()]),('independent_downloaded',[rows[pid]['p'] for pid in canonical])]:
        completeness[scope]={'denominator':len(ps),**{field:sum(bool(p.get(field)) and p.get(field)!='unknown' for p in ps) for field in ['doi','authors','abstract','keywords','venue','year','document_type']}}
    completeness['enriched_independent']={'denominator':len(canonical),'confirmed_formal_publication':sum(meta[pid]['publication_confirmed'] for pid in canonical),'known_formal_or_preprint_name':sum(meta[pid]['publication_confirmed'] or meta[pid]['publication_kind']=='preprint' for pid in canonical),'issn':sum(bool(meta[pid]['issn']) for pid in canonical),'publisher':sum(bool(meta[pid]['publisher']) for pid in canonical)}
    manifest=list(csv.DictReader((FOLDER/'manifest.csv').open(encoding='utf-8-sig')));manifestids={r['id']:r for r in manifest};manifest_diff=sum(r['id'] not in manifestids or manifestids[r['id']]['download_status']!=r['status'] for r in allrows)
    structural_only=sum(bool(q.get('pages',0)>=2 and not q.get('repaired') and not q.get('encrypted') and all(error.startswith('title_mismatch:') for error in q.get('errors',[]))) for q in checks.values())
    scope_after=json.loads((ROOT/'acquisition/run_state.json').read_text(encoding='utf-8'))
    live=sqlite3.connect('file:'+str(ROOT/'acquisition/state.sqlite3').replace('\\','/')+'?mode=ro',uri=True);later_count=live.execute("select count(*) from papers where status='downloaded'").fetchone()[0];live.close()
    supplement_records=json.loads((FOLDER/'supplement_metadata.json').read_text(encoding='utf-8')) if (FOLDER/'supplement_metadata.json').exists() else {}
    stats={'snapshot_info':json.loads((FOLDER/'snapshot_info.json').read_text(encoding='utf-8')),'candidates':len(allrows),'downloaded_records':len(rows),'independent_downloaded':len(canonical),'duplicate_extra_records':len(rows)-len(canonical),'physical_paths':len({r['path'] for r in rows.values()}),'structural_valid_records':sum(q.get('structural_valid',False) for q in checks.values()),'structural_only_valid_records':structural_only,'hash_unchanged_records':sum(q.get('file_unchanged',False) for q in checks.values()),'pdf_bytes':sum(checks[pid].get('file_bytes',0) for pid in rows),'confirmed_selected_papers':len(top),'top_counts':dict(Counter(t['publication_name'] for t in top)),'flagship_counts':{name:sum(venue_norm(r['publication_name'])==venue_norm(name) for r in top) for name in GENERAL},'issue_counts':dict(Counter(i['issue_type'] for i in issues)),'sample_summary':sample_summary,'completeness':completeness,'domain_stats':domain_stats,'year_counts':dict(year_counter),'crossref_online_year_counts':dict(onlineyears),'year_disagreements':len(disagreements),'future_dates':len(future),'jobstats':jobstats,'year_filter_conflicts':len(filter_violations),'year_conflicts_first_date_in_range':sum(v['first_date_inside_filter'] for v in filter_violations),'manifest_status_differences':manifest_diff,'metadata_progress':json.loads((FOLDER/'metadata_progress.json').read_text(encoding='utf-8')),'supplement_total':len(supplement_records),'supplement_verified':sum(r.get('verified',False) for r in supplement_records.values()),'later_downloaded_records':later_count,'later_collector_state':scope_after,'report_generated_at':datetime.datetime.now().astimezone().isoformat()}
    write_json(FOLDER/'audit_summary.json',stats);write_json(FOLDER/'canonical_groups.json',canonical)
    write_json(FOLDER/'classification_policy.json',{'general_flagship_names':GENERAL,'selected_journals_by_field':SELECTED,'selected_conference_name_patterns':CONFERENCES,'technical_taxonomy':TAXONOMY,'not_claiming_jcr_or_universal_ranking':True})
    stats['selected_explicit_nonresearch']=sum(bool(r['pdf_explicit_genre']) for r in top)
    stats['selected_without_explicit_nonresearch']=len(top)-stats['selected_explicit_nonresearch']
    write_json(FOLDER/'audit_summary.json',stats)
    render_report(stats,ranked,topicrows,sample_rows,issues,meta,rows)
    print(dump({k:stats[k] for k in ['downloaded_records','independent_downloaded','structural_valid_records','hash_unchanged_records','confirmed_selected_papers','flagship_counts','year_disagreements','sample_summary']}),flush=True)

def render_report(s,ranked,topics,samples,issues,meta,rows):
    snapshot=s['snapshot_info'];progress=s['metadata_progress'];independent=s['independent_downloaded'];selected=s['confirmed_selected_papers'];completed=progress.get('done',False)
    lines=['# 学术文献库覆盖与数据质量审计','',f"状态：{'审计完成（固定快照）' if completed else '审计进行中，免费元数据核验尚未结束'}",'',f"生成时间：{s['report_generated_at']}；数据库快照时间：{snapshot['created_at']}。",'',
      f"审计范围为 SQLite 一致性快照中的 {s['candidates']:,} 条候选、{s['downloaded_records']:,} 条已下载记录，按 DOI、去版本 arXiv ID、规范化长标题、文件 SHA-256 合并为 **{independent:,} 篇独立文献**。其中包含需复核的领域标签及非目标类型，不能直接等同于阶段验收合格数。跨领域标签不相加。",'',
      f"采集器随后自行达标停止，实时状态为 {s['later_collector_state'].get('status')}，系统阶段验收有效独立文献 {s['later_collector_state'].get('valid_unique_papers')} 篇。本次快照之后新增 {s['later_downloaded_records']-s['downloaded_records']} 条下载记录未计入本审计；系统阶段计数与本次来源/版本/主题审计采用不同口径，不混用。",'',
      '## 关键结论','',f"1. 已下载文献主要来自预印本服务器 arXiv，以及 Scientific Reports、Nature Communications、PLOS ONE、Sensors 等开放获取期刊。详见下方实际排名，预印本服务器单列，下载主机不是期刊。",'',
      f"2. 按本次明确声明的重点出版物名单，确认 **{selected} 篇**来自重点期刊或会议（{selected/max(1,independent):.1%}）。严格 Nature/Science/Cell 旗舰刊分别为 {s['flagship_counts']['Nature']}/{s['flagship_counts']['Science']}/{s['flagship_counts']['Cell']} 篇。Nature Communications、Science Advances 等另列，不能算成 Nature 或 Science 旗舰刊。重点名单是审计选刊标准，不代表所有论文质量已经被评估。",'',
      f"其中 {s['selected_explicit_nonresearch']} 条PDF明示editorial/论坛概述等非研究体裁；去掉这些标记后为 {s['selected_without_explicit_nonresearch']} 条，仍只是来源/体裁筛查结果，不是全文研究质量评分。ICSE的Software Engineering in Practice/Society等明确非主研究分轨不纳入本次重点主会名单，但仍保留在全部来源排名。",'',
      '3. 各领域确有多个技术方向，但热门主题、可下载性和检索排序明显塑造样本。下文比较原查询标签与重新匹配的标题/摘要/学术分类；比例仅是本地文献库分布，不是整个学术领域覆盖率。','',
      f"4. 原始年份分布中 2026 年 {s['year_counts'].get('2026',0)} 篇、2023 年 {s['year_counts'].get('2023',0)} 篇、2024 年 {s['year_counts'].get('2024',0)} 篇。程序把2023—2026合为一区间且降序检索，分页未完成时容易集中于2026。已有 {s['year_disagreements']} 篇年份与可信 Crossref 在线/发表日期口径不一致，但这些差异本身不能全判为错误。",'',
      '5. 知识库建设的主要风险是：期刊名称变体、下载版本与正式发表类型混在一起、日期口径混杂、缺少关键词/DOI、少量重复版本和不相关领域标签。PDF 能解析和文件哈希正确，并不证明分类、来源和论文质量正确。','',
      '6. 下一阶段应按细分方向及2019—2025尤其2023—2024缺口补充经核实的正式论文与综述，并补齐软件系统和器件制造等不足主题；优先质量和结构平衡，不能单纯追求总篇数。','',
      '## 方法、计数边界与可复现性','',
      f"- 数据库通过 Python SQLite online backup API 从只读连接生成完整快照，`PRAGMA integrity_check` 为 ok；采集器没有被暂停、重启或配置修改。快照路径：`{FOLDER/'snapshot.sqlite3'}`。", 
      f"- manifest.csv 同时只读保存作交叉核对；与快照相比有 {s['manifest_status_differences']} 条状态或ID差异。它是采集器定时导出，不是事务快照；统计以 SQLite 快照为准。",
      '- 未额外下载论文，未改动或删除任何PDF，未向原数据库回写补查字段。Crossref 原始响应、API状态、PDF检查、抽样与补全记录均单独保存在审计目录。',
      f"- 新 Crossref 免费单DOI查询：{progress.get('completed')} / {progress.get('total')} 个，当前 HTTP/网络结果 {dump(progress.get('outcomes'))}。单请求串行、每请求间隔至少0.3秒；429退避，不使用付费服务。", 
      f"- 对标题页/会议线索另补查 {s['supplement_total']} 个缺失DOI候选，可信标题匹配确认 {s['supplement_verified']} 个；其余明确保留未知/歧义，不猜测。",
      '- OpenAlex实时批量接口返回429，明确提示本IP共享免费预算耗尽；已停止重试。本审计使用文献库中保存的OpenAlex原始数据；此证据与新查询时间不同，在逐篇记录中区分来源。',
      '- 来源身份核验优先匹配 DOI + 标题，规范标题/token F1/字符相似度至少0.8。不能满足的补查记录只用作异常证据，不据此认定发表来源。下载主机、出版商、期刊/会议、预印本服务器及下载版本分别记录。',
      '- DOI/ISSN可空值或UNKNOWN；没有期刊名称、正式会议信息或版本关系证据的记录不猜测。期刊聚合合并大小写、标点及已明确地理限定变体，具体ISSN仍单独保存。',
      '- 全库逐文件检查存在性、大小、SHA-256、文件头、EOF、解析器修复/加密状态、页面结构和前3页标题/DOI；局部抽样不是对每页内容完整性的绝对证明。无需渲染页面。',
      '- 技术主题由公开的正则同义词字典匹配标题、摘要、关键词及可信OpenAlex分类，多标签可重叠。研究领域范围、前沿重要性及合理配额需要领域专家进一步确认。','',
      '## 1. 期刊、会议与预印本来源','', '|排名|实际发表来源/服务器|类型|独立文献|重点名单|','|---:|---|---|---:|---|']
    for r in ranked[:35]:lines.append(f"|{r['rank']}|{r['publication_name']}|{r['publication_kind']}|{r['unique_downloaded_papers']}|{r['tier']}|")
    lines+=['',f"全部 {len(ranked)} 个来源组及论文数量均在 JOURNAL_RANKING.csv，含ISSN、出版商、名称变体、领域计数和实际下载主机。unique_downloaded_papers是本次独立下载分母；all_catalog_raw_venue_candidates覆盖全部{ s['candidates']}条候选按原始venue归组，含尚未下载与已排除记录，并非都已外部核实。catalog_metadata_only行只表示候选元数据，不能当已下载或已确认发表的论文；也不纳入重点出版物计数。不能因同属Springer Nature、Elsevier、IEEE而认定为重点出版物。",'',
      '### 可解释的重点出版物标准','',
      '- **综合性旗舰刊**：仅完整刊名 Nature、Science、Cell，身份须被可信元数据确认。',
      '- **重点综合性刊**：Nature Communications、Science Advances、PNAS等独立刊物单列。',
      '- **生物制药**：关注药物、蛋白/抗体、核酸及细胞疗法的方法和转化研究；名单含Nature Biotechnology、Nature Medicine、Nature Methods、Molecular Therapy、Journal of Controlled Release等。',
      '- **软件通信**：分别选择系统、软件工程、数据库、机器学习和通信领域的技术出版物；含IEEE TPAMI/TSE/JSAC/TWC、ACM TOCS/TODS等，会议按正式完整名称核实。',
      '- **新材料**：分别选择材料机理、合成制备、器件应用与能源材料出版物；含Nature Materials/Energy/Nanotechnology、Advanced Materials/Functional/Energy Materials、ACS Nano、Nano Letters、JACS等。',
      '- **半导体与集成电路**：分别选择器件、电路、EDA、光子与工艺出版物；含IEEE JSSC/TED/EDL/TCAD、Nature Electronics/Photonics、Light: Science & Applications等，以及ISSCC、IEDM、VLSI、DAC、ICCAD等技术会议。',
      '- 名单是本次审计的明确选择，不能称为完整顶刊清单、官方分区、现行影响因子排名，未列入也不代表质量低。会议workshop/companion/demo/findings不纳入主会重点计数；载体为Lecture Notes等书系却未确认具体会议时仍为未知。',
      '- [CSRankings方法说明](https://csrankings.org/faq)解释分领域顶级会议的选择；[IEDM官方定位](https://ieee-iedm.org/)确认其器件、物理和工艺范围。这些解释支持会议选择框架，逐篇发表身份仍由DOI/正式记录确认。','',
      '### 实际匹配的重点出版物','', '|出版物|实际匹配独立文献|','|---|---:|']
    for venue,n in sorted(s['top_counts'].items(),key=lambda kv:-kv[1]):lines.append(f'|{venue}|{n}|')
    lines+=['', 'TOP_PUBLICATIONS.csv 列出**每一篇**匹配文献的实际标题、DOI（未确认则UNKNOWN）、正式来源、ISSN、出版商、核验链接、标题相似度、选刊依据和PDF路径。另有原始文献类型和PDF明示体裁，重点刊物中的editorial/correction也不能自动当作高水平研究论文。重叠领域标签不重复计算总数。','',
      '## 2. 研究领域分布与可能缺口','', '|领域|独立标记文献|主主题最大占比|主主题HHI|有效主主题数|无法映射|','|---|---:|---:|---:|---:|---:|']
    for d,v in s['domain_stats'].items():lines.append(f"|{d}|{v['unique_tagged_papers']}|{v['top_primary_share']:.1%}|{v['primary_hhi']:.3f}|{v['effective_primary_topics']:.1f}|{v['unmapped']}|")
    lines+=['','主主题是为浓度分析所做的单标签分配（标题权重高于摘要），技术主题本身仍保留多标签。HHI=sum(主主题占比²)，越大表示本库分布越集中；有效主题数=1/HHI，不是领域完整性评分。','']
    for d,v in s['domain_stats'].items():
        candidates=[r for r in topics if r['domain']==d];topcounts=sorted(candidates,key=lambda r:-r['content_matched_unique_downloaded'])[:6];sparse=[r for r in candidates if r['content_matched_unique_downloaded']<10]
        lines+=['### '+d,'','观测到的主主题分布：'+dump(v['primary_topic_counts']),'', '标题/摘要等多标签频繁方向：'+ '；'.join(r['topic_zh']+' '+str(r['content_matched_unique_downloaded'])+'篇' for r in topcounts),'', '本库未检出或稀少(<10)的候选补充方向：'+'；'.join(r['topic_zh']+' '+str(r['content_matched_unique_downloaded'])+'篇' for r in sparse),'', '上述缺口属于本地样本观察或专家框架提出的补查假设。同义词、缺失摘要和技术命名变化也可能导致低匹配数；不可据此断言整个领域缺少研究。','']
    lines+=['DOMAIN_COVERAGE.csv 给出每个细分方向的内容匹配、原始查询标签、主主题数量、重点出版物分布及具体匹配表达式；新增方向有not_in_original_query_framework标记。高水平出版物出现在哪个主题，只说明本库已有匹配论文，不代表该主题研究成熟度。','',
      '## 3. 年份异常与证据','', '|原始年份|已下载独立文献|','|---|---:|']
    for year,n in sorted(s['year_counts'].items()):lines.append(f'|{year}|{n}|')
    lines+=['','### 已验证的筛选与日期行为','',
      '- collect.py 将(2023,2026,newest)、(2019,2022,relevance)、(2015,2018,relevance)、(1900,2014,relevance)作为初始分组。最新分组排序是降序，并非每年等配额抽样。',
      '- OpenAlex使用publication_year范围和publication_date:desc；arXiv按submittedDate排序并使用首次提交published日期；DOAJ按bibjson.year.exact:desc，只有年份排序精度；Europe PMC使用首次发表日期，Crossref使用published字段，Semantic Scholar使用publicationDate/year。',
      '- 2023—2026仍有大量未执行分页/关键词任务，且阶段采集会在达标后停止，最新页优先+有限执行+OA下载可得性共同造成样本偏斜。队列done任务数量只能说明实际执行进度，不能等同于数据库所有结果已取完。',
      '- metadata ingest 对年份和venue采取“原有非空值保留”的合并方式，没有保留足够逐来源冲突字段；后来出现正式发表信息也不一定替换首次预印本年份。',
      '- DOI中包含2025/2026是标识符组成，不是可信发表年份证据；Crossref created/indexed也不是发表日期。版权许可页、收到/接受日期、参考文献年份、PDF修改时间都不能直接当发表年。','',
      f"可信 Crossref 在线优先日期与本库年份不一致：**{s['year_disagreements']} 篇**；原始publication_date晚于快照日2026-10-06：**{s['future_dates']} 篇**。来源返回年份超出查询区间的观测 {s['year_filter_conflicts']} 条，也可能是来源日期口径不同。逐条差异、PDF版权/标题页年份和查询任务证据在YEAR_AUDIT.csv。未覆盖原有年份。",'',
      f"进一步核查这 {s['year_filter_conflicts']} 条全部来自Europe PMC，其中 {s['year_conflicts_first_date_in_range']} 条原始firstPublicationDate实际上符合FIRST_PDATE查询区间。程序year字段却直接取pubYear：这是**已证实的字段口径不一致，不是查询过滤失效**。例：Nanoformulations targeting immune cells for cancer therapy: mRNA therapeutics 的首次发表日期2022-12-01，pubYear为2023，落入2019—2022首次发表队列。",'',
      '### 修复建议与推断边界','',
      '- 建立first_submission_date、accepted_date、published_online_date、published_print_date、source_reported_year等独立字段，每项保存来源、核验时间、精度与冲突。知识库检索时显式选择日期口径。',
      '- 将2023、2024、2025、2026分别建年级分页队列，并对来源/技术方向设最小配额；2026按已实际在线发表日期核验，预排卷期单列。',
      '- 目前可以确认采样偏差的机制及若干日期冲突；没有领域总体年份基线，不能定量断言2026文献本身“虚假”。未获得Crossref日期的条目仍未知。','',
      '## 4. 元数据、文件与重复版本','', '|统计范围|数量|DOI|作者|摘要|关键词|原始venue|年份|文献类型|','|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    for scope in ['all_candidates','downloaded_records','independent_downloaded']:
        c=s['completeness'][scope];lines.append('|'+scope+'|'+str(c['denominator'])+'|'+'|'.join(f"{c[k]} ({c[k]/max(1,c['denominator']):.1%})" for k in ['doi','authors','abstract','keywords','venue','year','document_type'])+'|')
    lines+=['',f"补查后的来源完整性（独立已下载文献，n={independent}）：{dump(s['completeness']['enriched_independent'])}。venue非空可能只是预印本服务器，不能等同于正式刊名完整。",'',
      f"实际检查文件路径 {s['physical_paths']} 个，已下载记录 {s['downloaded_records']} 条；基础结构可读取 {s['structural_only_valid_records']} 条，结构+前页身份自动检查通过 {s['structural_valid_records']} 条；大小与SHA-256和入库记录一致 {s['hash_unchanged_records']} 条；PDF实际占用 {s['pdf_bytes']/1024**3:.3f} GiB。逻辑重复/不同版本额外记录 {s['duplicate_extra_records']} 条；未删除任何文件。3份前页身份匹配不足涉及双语/非英文PDF，不据此直接认定文件损坏。MuPDF输出少量ICC配置/对象关键词警告，未做图像渲染验证。",'',
      '|问题类型|记录条数（问题可重叠）|','|---|---:|']
    for key,n in sorted(s['issue_counts'].items(),key=lambda kv:-kv[1]):lines.append(f'|{key}|{n}|')
    lines+=['', 'METADATA_ISSUES.csv 中每条问题保留标题、DOI、领域、证据和修复建议；“review/suspected”不等于已确认错误。低标题相似度也可能是双语标题或元数据截断，需结合PDF和作者判断。', '',
      '### 内容相关性分层抽样','', '|领域|样本数|技术相关|复核不属于该领域|仍疑似无关|边界/证据不足|文件/身份失败|','|---|---:|---:|---:|---:|---:|---:|']
    for d,v in s['sample_summary'].items():lines.append(f"|{d}|{v['sampled']}|{v.get('confirmed_within_rule',0)}|{v.get('confirmed_out_of_scope',0)}|{v.get('suspected_irrelevant',0)}|{v.get('uncertain_context_requires_review',0)}|{v.get('identity_or_pdf_failure',0)}|")
    lines+=['',f"抽样在每个领域内按原始年份×出版来源分层，哈希种子固定，先轮询各层再补足50篇；属于非概率、多来源覆盖样本，不能把错误数直接外推为全库错误率。实际读取标题页、前3页和正文约三分位/末页，综合标题、摘要与正文技术词判断；没有逐篇阅读全文或评价研究设计。样本记录：`{FOLDER/'sample_review.csv'}`。",'',
      f"已对34份规则异常或对照样本作助理语义复核（阅读标题、摘要与已提取的实际PDF抽样文本；非领域专家全文评审）。发现 {sum(v.get('confirmed_out_of_scope',0) for v in s['sample_summary'].values())} 个领域标签不合适样本，另有1份Logic Design明确为百科章节而非目标研究论文；这些错误可以重叠，不能将比例外推至全库。临床应用模型、软件教育、通用工业追溯和MEMS微流控仍保留边界不确定性。记录在manual_sample_review.json与sample_review.csv。",'',
      '具体证据：拟南芥茎振动/植物力学不属于生物制药；鱼样毒素检测的“screening”不是药物筛选；小学增值评价、VR哲学及纳米金刚石表征不应仅凭language/models/analysis字词计为软件通信。现有relevant函数采用约60%查询词的子串命中，叠加跨领域查询，存在这种混入机制。', '',
      '## 5. 下一阶段优先顺序','',
      '1. **先修复索引与版本关系**：将formal_venue、publisher、repository、pdf_host拆开；统一期刊名称与ISSN；将article identity和downloaded_version拆开；保留重复版本与正式发表的对应关系。',
      '2. **先核实年份和主题标签**：优先YEAR_AUDIT中日期冲突、METADATA_ISSUES中标题/DOI冲突及疑似无关样本。保留原始值，修复作为带来源的增量字段。',
      '3. **按缺口补充2019—2025，尤其2023—2024**：采用独立年份/来源/主题队列，并补充能够代表方法演进的经典论文，不把最新OA页当成全领域样本。',
      '4. **按技术方向选文献**：生物制药关注工艺放大、CMC、细胞/病毒载体生产与质控；软件通信关注OS、数据库、存储、编译器、协议安全和正式系统会议；材料关注结构金属、磁性/稀土、膜分离、规模制备与回收；半导体关注刻蚀沉积清洗、良率、HBM、电源管理、测试可靠性和实际工艺/设计会议。具体缺口须结合DOMAIN_COVERAGE的观测数量。',
      '5. **重点正式来源与综述优先**：按分领域选刊/会议补齐方法代表作，并保留合法OA可获得性记录；没有OA PDF时也可先完善可信元数据。本次审计未下载新论文。','',
      '## 输出与恢复','',
      '- JOURNAL_RANKING.csv：所有来源组的实际数量、ISSN/出版商及主机分布。',
      '- TOP_PUBLICATIONS.csv：全部重点出版物具体论文与逐篇身份核实依据。',
      '- DOMAIN_COVERAGE.csv：原查询标签与独立内容主题匹配、集中方向及可能缺口。',
      '- YEAR_AUDIT.csv：逐篇日期证据和按年份/来源/主机/检索队列的统计。',
      '- METADATA_ISSUES.csv：字段缺失、重复、冲突、未知和疑似错误。',
      '- LITERATURE_AUDIT_REPORT.md：本报告。所有统计以固定快照为准，后续采集不改变本次分母。',
      f"- 审计脚本：acquisition/literature_audit.py、audit_metadata.py、audit_taxonomy.py；复跑report只读快照并使用已保存的API/PDF检查，不重查、不下载。审计工作目录：`{FOLDER}`。",'',
      '参考：[Crossref公开API与限流](https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/)、[Crossref元数据字段](https://github.com/Crossref/rest-api-doc)、[OpenAlex出版日期与来源过滤文档](https://github.com/ourresearch/openalex-docs/blob/main/api-entities/works/filter-works.md)。']
    (ROOT/'LITERATURE_AUDIT_REPORT.md').write_text('\n'.join(lines),encoding='utf-8')

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('mode',choices=['pdf','report']);args=parser.parse_args()
    if args.mode=='pdf':pdf_audit()
    else:build_report()
