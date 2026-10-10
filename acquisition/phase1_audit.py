"""File-based, resumable phase-one acceptance; no LLM/full-paper analysis."""
from __future__ import annotations
from project_paths import resolve_project_path, pointer_path
import csv,hashlib,json,re,time
from collections import Counter,defaultdict
from datetime import datetime,timezone,timedelta
from pathlib import Path
import pymupdf as fitz
from collect import ROOT,STATE,DOMAINS,TOPICS,dump,norm,doi_norm,paper_keys,validate,PDF_VALIDATION_LOCK
from corpus_store import load_config
# Historical finite-phase compatibility; continuous mode uses active taxonomy.
DOMAINS=list(load_config().get('legacy_seed_domains',{}).values()) or DOMAINS

POLICY=STATE/'phase1_policy.json'
DOMAIN_TERMS={
 '生物制药':['pharma','drug','therap','mrna','antibod','gene','vaccine','protein','cell therapy','car t','synthetic biology','biomanufact','bioinform','organoid','bioprocess','microbial','biologic','药','治疗','抗体','蛋白','疫苗'],
 '软件与通信':['software','computer','algorithm','machine learning','artificial intelligence','neural','database','wireless','communication','internet','distributed comput','operating system','programming','language model','cloud comput','network','cryptograph','iot','mimo','软件','通信','计算机','网络','人工智能'],
 '新材料':['material','polymer','perovskite','batter','nanocrystal','nanoparticle','nanomaterial','catalys','superconduct','alloy','ceramic','graphene','carbon','composite','ferroelect','thin film','metal organic','材料','电池','高分子','催化','钙钛矿'],
 '半导体与集成电路':['semiconductor','chip','transistor','cmos','photonic','finfet','vlsi','circuit','lithograph','wafer','chiplet','rram','mram','epitax','nanowire','mosfet','photodetect','microelectronic','neuromorphic','silicon','gallium','半导体','芯片','集成电路','光子','氮化镓','碳化硅']}
def policy(): return json.loads(POLICY.read_text(encoding='utf-8'))
def atomic(path,text):
    tmp=path.with_name(path.name+'.tmp');tmp.write_text(text,encoding='utf-8');tmp.replace(path)
def csvfile(path,fields,rows):
    tmp=path.with_name(path.name+'.tmp')
    with tmp.open('w',encoding='utf-8-sig',newline='') as f:
        w=csv.DictWriter(f,fieldnames=fields,extrasaction='ignore');w.writeheader();w.writerows(rows)
    tmp.replace(path)
def category(p):
    t=str(p.get('document_type') or '').lower()
    if 'review' in t: return 'review'
    if 'preprint' in t or 'posted-content' in t: return 'preprint'
    if 'conference' in t or 'proceedings-article' in t: return 'conference_paper'
    if any(x in t for x in ['article','journal','methods','brief report']): return 'journal_paper'
    return 'unknown'
def topic_labels(p,d):
    observed={norm(t) for t in p.get('topics',[])}
    return sorted({TOPIC_ALIASES[d][t] for t in observed if t in TOPIC_ALIASES[d]})
TOPIC_ALIASES={d:{norm(t):en for en,zh in pairs for t in [en,zh]} for d,pairs in TOPICS.items()}
def signature(p):
    return hashlib.sha256(dump(['front_page_identity_v2',p.get('title'),p.get('doi'),p.get('document_type'),p.get('eligible'),p.get('domains'),p.get('abstract'),p.get('keywords')]).encode()).hexdigest()

def identity_page(pdf,p):
    """Allow repository/license covers; confirm identity before the reference section."""
    tokens=[x for x in norm(p.get('title')).split() if len(x)>3 and x not in {'with','from','using','based','study','review','applications'}]
    doi=doi_norm(p.get('doi'));choices=[]
    for i in range(min(3,len(pdf))):
        text=pdf[i].get_text();normalized=norm(text)
        score=sum(x in normalized for x in tokens)/max(1,len(tokens))
        from corpus_languages import title_overlap
        chinese_overlap=title_overlap(p.get('title'),text)
        if chinese_overlap is not None:score=chinese_overlap
        doi_found=bool(doi and doi in text.lower())
        choices.append((score,doi_found,-i,text))
    score,doi_found,minus_i,text=max(choices,key=lambda c:(c[0],c[1],c[2]))
    return -minus_i,text,score,doi_found

class PhaseRegistry:
    def __init__(self,lib):
        self.lib=lib;self.db=lib.db;self.config=policy();self.rows={};self.checks={};self.last_refresh=0;self.cached_summary=None
        self.db.executescript('''
        create table if not exists phase1_checks(paper_id text primary key,path text,size integer,mtime_ns integer,signature text,result text,checked text);
        create table if not exists phase1_state(key text primary key,value text);
        create table if not exists phase1_progress(id integer primary key,time text,total integer,domains text,topics text,status text);
        ''');self.db.commit()
    def inspect(self,p,path,sha):
        result=dict(valid=False,error='',warnings=[],title_score=0,doi_on_first_page=False,metadata_signature=signature(p))
        try:
            if not path or not resolve_project_path(path).is_file():raise ValueError('missing_file')
            content=resolve_project_path(path).read_bytes();info=validate(content,p)
            if info['sha256']!=sha:raise ValueError('stored_hash_mismatch')
            typ=str(p.get('document_type') or '').lower()
            if typ in ['book','monograph','dissertation','dataset','editorial','peer-review'] or any(x in typ for x in ['book-chapter','book-section','published erratum']):raise ValueError('non_target_document_type: '+typ)
            if not p.get('eligible',True):raise ValueError('not_eligible_under_collection_screen')
            with PDF_VALIDATION_LOCK,fitz.open(stream=content,filetype='pdf') as pdf:
                first=pdf[0].get_text();last=pdf[-1].get_text()
                identity_index,header,title_score,doi_found=identity_page(pdf,p)
                if re.search(r'^(abstracts? only|table of contents|supporting information|supplementary information)\b',first.strip(),re.I):raise ValueError('cover_abstract_or_supplement')
            if title_score<.35 and not doi_found:raise ValueError('identity_not_confirmed_on_front_pages')
            scope=norm((p.get('title') or '')+' '+(p.get('abstract') or '')+' '+' '.join(p.get('keywords') or [])+' '+header)
            relevant_domains=[d for d in p.get('domains',[]) if d in DOMAIN_TERMS and any(term in scope for term in DOMAIN_TERMS[d])]
            if not relevant_domains:raise ValueError('subject_scope_unconfirmed_or_irrelevant')
            result['relevant_domains']=relevant_domains
            if set(relevant_domains)!=set(p.get('domains',[])):result['warnings'].append('some_domain_labels_not_confirmed')
            result.update(valid=True,pages=info['pages'],bytes=info['bytes'],sha256=info['sha256'],title_score=title_score,identity_page=identity_index+1,doi_on_identity_page=doi_found,doi_on_first_page=bool(p.get('doi') and doi_norm(p['doi']) in first.lower()),sampled_text_chars=len(header)+len(last))
            if title_score<.5:result['warnings'].append('front_page_title_match_weak')
            if not p.get('doi'):result['warnings'].append('missing_doi')
            if not p.get('abstract'):result['warnings'].append('missing_abstract')
            if not p.get('year'):result['warnings'].append('missing_year')
            if not p.get('authors'):result['warnings'].append('missing_authors')
            if category(p)=='unknown':result['warnings'].append('unknown_document_type')
            if 'book' in typ:result['warnings'].append('ambiguous_book_conference_type')
            if not any(topic_labels(p,d) for d in p.get('domains',[]) if d in TOPICS):result['warnings'].append('unmapped_topic_requires_review')
        except Exception as e:result['error']=str(e) or type(e).__name__
        return result
    def check(self,pid,p,path,sha,force=False):
        f=resolve_project_path(path) if path else None;stat=f.stat() if f and f.is_file() else None
        cached=self.db.execute('select size,mtime_ns,signature,result from phase1_checks where paper_id=?',(pid,)).fetchone()
        if not force and cached and stat and cached[:3]==(stat.st_size,stat.st_mtime_ns,signature(p)):
            result=json.loads(cached[3])
        else:
            result=self.inspect(p,path,sha)
            self.db.execute('insert into phase1_checks values(?,?,?,?,?,?,?) on conflict(paper_id) do update set path=excluded.path,size=excluded.size,mtime_ns=excluded.mtime_ns,signature=excluded.signature,result=excluded.result,checked=excluded.checked',(pid,path,stat.st_size if stat else 0,stat.st_mtime_ns if stat else 0,signature(p),dump(result),datetime.now(timezone.utc).isoformat()))
        self.checks[pid]=result;return result
    def refresh(self,force=False,callback=None):
        self.rows={};self.cached_summary=None
        records=self.db.execute("select id,data,path,sha256 from papers where status='downloaded'").fetchall()
        for i,(pid,data,path,sha) in enumerate(records):
            p=json.loads(data);self.rows[pid]=dict(p=p,path=path,sha=sha);self.check(pid,p,path,sha,force)
            if (i+1)%100==0:
                self.db.commit()
                if callback:callback(i+1,len(records))
        self.db.commit();self.last_refresh=time.monotonic();return self.summary()
    def add(self,pid):
        row=self.db.execute("select data,path,sha256 from papers where id=? and status='downloaded'",(pid,)).fetchone()
        if not row:return
        p=json.loads(row[0]);self.rows[pid]=dict(p=p,path=row[1],sha=row[2]);self.check(pid,p,row[1],row[2]);self.db.commit();self.cached_summary=None
    def groups(self):
        parent={pid:pid for pid in self.rows};keys={}
        def find(a):
            while parent[a]!=a:parent[a]=parent[parent[a]];a=parent[a]
            return a
        for pid,row in self.rows.items():
            p=row['p'];kk=[k for k in paper_keys(p) if not k.startswith('title:')]
            title=norm(p.get('title'))
            if len(title)>=25:kk.append('exact_title:'+title)
            if row['sha']:kk.append('sha256:'+row['sha'])
            for key in kk:
                if key in keys:parent[find(pid)]=find(keys[key])
                else:keys[key]=pid
        result=defaultdict(list)
        for pid in self.rows:result[find(pid)].append(pid)
        return result
    def summary(self):
        if self.cached_summary is not None:return self.cached_summary
        groups=self.groups();domains=Counter();topics=defaultdict(Counter);venues=defaultdict(Counter);years=Counter();types=Counter();valid_groups={};metadata=Counter()
        for gid,ids in groups.items():
            valid=[pid for pid in ids if self.checks.get(pid,{}).get('valid')]
            if not valid:continue
            valid_groups[gid]=valid
            pp=[self.rows[pid]['p'] for pid in valid];canonical=max(pp,key=lambda p:(bool(p.get('doi')),bool(p.get('abstract')),len(p.get('authors') or [])))
            labels=set(d for pid in valid for d in self.checks[pid].get('relevant_domains',self.rows[pid]['p'].get('domains',[])) if d in TOPICS)
            for d in labels:
                domains[d]+=1
                for t in set(t for p in pp for t in topic_labels(p,d)):topics[d][t]+=1
                if canonical.get('venue') and category(canonical)!='preprint':venues[d][canonical['venue']]+=1
            years[str(canonical.get('year') or 'unknown')]+=1;types[category(canonical)]+=1
            for field in ['doi','abstract','year','authors','keywords']:metadata[field]+=int(any(bool(p.get(field)) for p in pp))
        breadth={d:sum(n>=self.config['minimum_papers_per_topic'] for n in topics[d].values()) for d in DOMAINS}
        total=len(valid_groups);met=total>=self.config['target_unique_papers'] and all(domains[d]>=self.config['minimum_per_domain'] and breadth[d]>=self.config['minimum_topics_per_domain'] for d in DOMAINS)
        self.cached_summary=dict(valid_unique_papers=total,downloaded_records=len(self.rows),valid_records=sum(bool(v.get('valid')) for k,v in self.checks.items() if k in self.rows),duplicate_paper_records=sum(len(ids)-1 for ids in groups.values()),domains=dict(domains),topics={d:dict(topics[d]) for d in DOMAINS},venues={d:dict(venues[d]) for d in DOMAINS},topics_with_minimum=breadth,years=dict(years),types=dict(types),metadata_complete=dict(metadata),goal_met=met,config=self.config,invalid_records=sum(not self.checks.get(pid,{}).get('valid',False) for pid in self.rows))
        return self.cached_summary
    def progress(self,status='running'):
        s=dict(self.summary());s.update(status=status,updated=datetime.now(timezone(timedelta(hours=8))).isoformat())
        atomic(STATE/'phase1_status.json',dump(s))
        self.db.execute('insert into phase1_progress(time,total,domains,topics,status) values(?,?,?,?,?)',(s['updated'],s['valid_unique_papers'],dump(s['domains']),dump(s['topics_with_minimum']),status));self.db.commit()
        return s
    def sample(self,ids):
        selected=set();bydomain={d:[] for d in DOMAINS}
        for d in DOMAINS:
            pool=[pid for pid in ids if d in self.checks[pid].get('relevant_domains',self.rows[pid]['p'].get('domains',[]))]
            pool.sort(key=lambda pid:(len(topic_labels(self.rows[pid]['p'],d)),hashlib.sha256((d+pid).encode()).hexdigest()))
            seen=set()
            for pid in pool:
                p=self.rows[pid]['p'];bucket=(tuple(topic_labels(p,d)),p.get('year'),p.get('downloaded_from','').split('/')[2] if '://' in p.get('downloaded_from','') else '')
                if bucket not in seen:bydomain[d].append(pid);seen.add(bucket)
                if len(bydomain[d])>=self.config['sample_per_domain']:break
            for pid in pool:
                if len(bydomain[d])>=self.config['sample_per_domain']:break
                if pid not in bydomain[d]:bydomain[d].append(pid)
            selected.update(bydomain[d])
        samples={}
        for pid in selected:
            row=self.rows[pid];p=row['p'];warnings=[]
            with PDF_VALIDATION_LOCK,fitz.open(resolve_project_path(row['path'])) as pdf:
                identity_index,first,title_score,doi_found=identity_page(pdf,p)
                indices=sorted(set([0,identity_index,len(pdf)//4,len(pdf)//2,3*len(pdf)//4,len(pdf)-1]))
                excerpts=[pdf[i].get_text() for i in indices];text='\n'.join(excerpts)
                author_tokens=[norm(a).split()[-1] for a in p.get('authors',[]) if norm(a)]
                author_score=sum(x in norm(first) for x in author_tokens)/max(1,len(author_tokens))
                if title_score<.5:warnings.append('sample_title_match_weak')
                if author_tokens and author_score<.3:warnings.append('authors_not_confirmed_in_first_page')
                if len(norm(text))<1000:warnings.append('sampled_body_text_sparse')
                samples[pid]=dict(sample_pages=','.join(str(i+1) for i in indices),sample_identity_page=identity_index+1,sample_title_score=round(title_score,3),sample_author_score=round(author_score,3),sample_doi_found=bool(p.get('doi') and doi_norm(p['doi']) in text.lower()),sample_year_found=bool(p.get('year') and str(p['year']) in text),sample_body_chars=len(text),sample_warnings=';'.join(warnings),sample_evidence=norm(first)[:900])
        return samples,{d:len(v) for d,v in bydomain.items()}
    def report(self,status='running',full=False):
        if full:self.refresh(force=True)
        summary=self.summary();groups=self.groups();valid_ids=[next(pid for pid in ids if self.checks.get(pid,{}).get('valid')) for ids in groups.values() if any(self.checks.get(pid,{}).get('valid') for pid in ids)]
        samples,samplecounts=self.sample(valid_ids)
        quality=[];duplicate_ids={pid:gid for gid,ids in groups.items() for pid in ids if len(ids)>1}
        for pid,row in self.rows.items():
            p=row['p'];q=self.checks.get(pid,{})
            quality.append(dict(paper_id=pid,title=p.get('title'),domains=dump(p.get('domains')),confirmed_domains=dump(q.get('relevant_domains',[])),pdf_path=row['path'],valid_pdf=q.get('valid',False),failure_reason=q.get('error',''),duplicate_group=duplicate_ids.get(pid,''),sha256=row['sha'],file_bytes=q.get('bytes',0),pages=q.get('pages',0),identity_page=q.get('identity_page',''),title_score=q.get('title_score',0),doi_complete=bool(p.get('doi')),abstract_complete=bool(p.get('abstract')),year_complete=bool(p.get('year')),authors_complete=bool(p.get('authors')),quality_warnings=';'.join(q.get('warnings',[])),sampled=pid in samples,**samples.get(pid,{})))
        fields=['paper_id','title','domains','confirmed_domains','pdf_path','valid_pdf','failure_reason','duplicate_group','sha256','file_bytes','pages','identity_page','title_score','doi_complete','abstract_complete','year_complete','authors_complete','quality_warnings','sampled','sample_pages','sample_identity_page','sample_title_score','sample_author_score','sample_doi_found','sample_year_found','sample_body_chars','sample_warnings','sample_evidence']
        csvfile(ROOT/'PHASE1_QUALITY.csv',fields,quality)
        coverage=[]
        for d in DOMAINS:
            coverage.append(dict(domain=d,topic='ALL',topic_zh='',valid_unique_papers=summary['domains'].get(d,0),meets_topic_minimum=summary['domains'].get(d,0)>=self.config['minimum_per_domain']))
            for en,zh in TOPICS[d]:
                n=summary['topics'][d].get(en,0);coverage.append(dict(domain=d,topic=en,topic_zh=zh,valid_unique_papers=n,meets_topic_minimum=n>=self.config['minimum_papers_per_topic']))
        csvfile(ROOT/'PHASE1_COVERAGE.csv',['domain','topic','topic_zh','valid_unique_papers','meets_topic_minimum'],coverage)
        statuses=dict(self.db.execute('select status,count(*) from papers group by status'));raw=self.db.execute("select count(*) from observations where instr(job_id,'_download')=0").fetchone()[0]
        candidates=self.db.execute('select count(*) from papers').fetchone()[0]
        failures=Counter(err.split(':')[0] if err else 'unknown' for err, in self.db.execute("select error from papers where status in ('failed','suspected_incomplete')"))
        sources=defaultdict(Counter)
        for source,outcome,n in self.db.execute("select provider,outcome,count(*) from http_attempts where stage='download' group by provider,outcome"):
            sources[source]['attempts']+=n;sources[source]['success']+=n if outcome=='success' else 0
        physical={row['path'] for row in self.rows.values() if row['path'] and resolve_project_path(row['path']).is_file()};pdf_bytes=sum(resolve_project_path(f).stat().st_size for f in physical)
        disk_bytes=sum(f.stat().st_size for f in ROOT.rglob('*') if f.is_file()) if full else getattr(self,'last_directory_bytes',None)
        if full:self.last_directory_bytes=disk_bytes
        runtime=sum(json.loads(params).get('minutes',0) or 0 for params, in self.db.execute('select parameters from engine_runs'))
        measured=self.db.execute("select sum(json_extract(summary,'$.minutes')),sum(json_extract(summary,'$.new_pdf_files')) from engine_runs where summary is not null").fetchone()
        rate=(measured[1] or 0)/(measured[0] or 1)
        now=datetime.now(timezone(timedelta(hours=8))).isoformat()
        stop_error=self.db.execute("select value from phase1_state where key='last_error'").fetchone()
        result=dict(summary,status=status,report_time=now,raw_search_hits=raw,unique_candidates=candidates,statuses=statuses,failure_reasons=dict(failures),sample_counts=samplecounts,sample_warnings=sum(bool(q.get('sample_warnings')) for q in samples.values()),physical_pdf_paths=len(physical),pdf_bytes=pdf_bytes,total_directory_bytes=disk_bytes,source_rates={k:dict(v,success_rate=v['success']/max(1,v['attempts'])) for k,v in sources.items()},measured_download_minutes=measured[0],measured_new_pdf_files=measured[1],measured_pdf_per_minute=rate)
        atomic(STATE/'phase1_acceptance.json',dump(result))
        label='验收完成' if status=='completed' and summary['goal_met'] else '尚未完成验收：'+status
        lines=['# 第一阶段文献采集与质量验收','',f'状态：**{label}**','',f'北京时间：{now}','',f'运行/停止原因：{status}；异常详情：{stop_error[0] if stop_error else "无记录"}。','',f"实际有效且独立的文献：**{summary['valid_unique_papers']} / {self.config['target_unique_papers']}**；已下载记录 {len(self.rows)} 条；逻辑重复记录 {summary['duplicate_paper_records']} 条；无效或非目标类型 {summary['invalid_records']} 条。原文献文件均保留，不删除或覆盖。",'', '|领域|有效独立文献|最低目标|至少10篇的细分方向数|抽样检查数|','|---|---:|---:|---:|---:|']
        for d in DOMAINS:lines.append(f"|{d}|{summary['domains'].get(d,0)}|{self.config['minimum_per_domain']}|{summary['topics_with_minimum'][d]}|{samplecounts[d]}|")
        lines+=['',f"检索命中 {raw} 条，唯一候选 {candidates} 条（未下载、无关和未知类型也保留）；当前状态 {dump(statuses)}。未包含来源端已去掉的重复命中及下载验证关联事件。",'',f'下载失败原因：{dump(failures)}。逐 URL 原因、永久拒绝、冷却和 OA 补查结果在 SQLite download_locations/http_attempts/resolution_queue 中保留。','', '## 细分方向与缺口','']
        for d in DOMAINS:
            missing=[en for en,zh in TOPICS[d] if summary['topics'][d].get(en,0)<self.config['minimum_papers_per_topic']]
            lines += ['### '+d,'','已下载方向及数量：'+dump(summary['topics'][d]),'','低于10篇的方向：'+'、'.join(missing),'']
        lines+=['## 年份分布','','|年份|独立文献数|','|---|---:|']
        for y in range(2015,2027):lines.append(f"|{y}|{summary['years'].get(str(y),0)}|")
        lines+=['', '其他年份或未知年份：'+dump({y:n for y,n in summary['years'].items() if y not in [str(i) for i in range(2015,2027)]}),'','## 文献类型与元数据质量','',dump(summary['types']),'','|字段|完整文献数|完整率|','|---|---:|---:|']
        for field,n in summary['metadata_complete'].items():lines.append(f"|{field}|{n}|{n/max(1,summary['valid_unique_papers']):.1%}|")
        lines+=['',f"抽样每领域至少 {self.config['sample_per_domain']} 篇，按查询方向、年份和下载主机分层选择，读取首页、前3页中的标题页、四分位页、中间页和末页。样本标题、作者、DOI、年份和正文文本对应的自动检查保存在 PHASE1_QUALITY.csv；发现 {result['sample_warnings']} 个需复核样本。没有让大模型逐篇阅读，没有渲染页面。抽样文本一致性不能替代专家对研究方法或期刊质量的评价。",'', '文件有效性核验：文件存在、SHA-256 与入库值一致、PDF 文件头/EOF、解析器无修复、非加密、各页结构可加载、标题/DOI与前3页中的标题页抽样文本相符。相同 DOI、去版本 arXiv ID、至少25字符的规范化相同标题或相同文件哈希合并为一个独立文献；各领域标签可重叠，总数不相加。类型按来源字段分类，来源未明确的保留 unknown。','', '## 下载效率与访问限制','',f"PDF 文件实际占用 {pdf_bytes/1024**3:.3f} GiB；整个目录占用 {disk_bytes/1024**3:.3f} GiB。" if disk_bytes is not None else f"PDF 文件实际占用 {pdf_bytes/1024**3:.3f} GiB；整个目录体积在最终验收时统计。",'',f'已有完整测量运行的下载速率为 {rate:.2f} 篇/分钟；包含在途下载完成时间，不包含暂停修改脚本时间；更早无逐地址计时的下载不据此推算。','','|PDF地址来源|成功/尝试|成功率|','|---|---:|---:|']
        for source,v in sorted(sources.items(),key=lambda x:-x[1]['attempts']):lines.append(f"|{source}|{v['success']}/{v['attempts']}|{v['success']/max(1,v['attempts']):.1%}|")
        lines+=['','主要瓶颈：主机限流、403/429、HTML 验证页、无 PDF 的 OA 元数据、网络停滞及较大 PDF。维持6个下载线程、2个补查线程和原有逐主机限制，不增加并发绕过网站限制；没有付费 API、Sci-Hub 或影子库。','', '## 阶段控制与文件','', '终止条件为至少3000篇有效独立文献、每领域至少750篇，并为避免集中于热门主题，基本细分覆盖按每领域至少12个方向、各至少10篇定义。达到条件后禁止新任务领取，等待在途请求完成，再执行完整验收；验收不通过不会宣称完成。','', 'PHASE1_COVERAGE.csv、PHASE1_QUALITY.csv、manifest.csv 与本报告都来自实际数据库/文件检查。实时状态：acquisition/phase1_status.json、run_state.json；每100篇进度记录：phase1_progress 表和 phase1_progress.jsonl。中断后使用 acquisition/run_phase1.ps1 恢复；已完成阶段默认不再发起网络采集。']
        atomic(ROOT/'PHASE1_REPORT.md','\n'.join(lines));self.lib.export(metadata=False)
        return result
