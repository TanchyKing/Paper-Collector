"""Persistent storage stage and resumable inventories, independent of taxonomy."""
import csv,collections,json,pathlib,sqlite3
from corpus_store import ROOT,STATE,now,dump,atomic_json

def usage(db,state=None):
    state=pathlib.Path(state or STATE)
    pdf=db.execute('SELECT coalesce(sum(bytes),0) FROM files').fetchone()[0]
    xml=sum(p.stat().st_size for p in (state/'fulltext_xml').rglob('*.xml') if p.is_file())
    return dict(pdf_bytes=pdf,xml_bytes=xml,fulltext_bytes=pdf+xml,basis='stored PDF files plus original JATS/XML; existing files included; database, cache, extracted text and backups excluded')

def stage(db,cfg,state=None):
    policy=cfg.get('storage_stage',{});v=usage(db,state);limit=policy.get('target_fulltext_bytes') if policy.get('enabled') else None
    v.update(enabled=bool(limit),target_fulltext_bytes=limit,remaining_bytes=max(0,limit-v['fulltext_bytes']) if limit else None,target_reached=bool(limit and v['fulltext_bytes']>=limit))
    return v

def write_rows(path,fields,rows):
    path=pathlib.Path(path);temp=path.with_suffix(path.suffix+'.tmp');count=0
    with temp.open('w',encoding='utf-8-sig',newline='') as stream:
        writer=csv.DictWriter(stream,fieldnames=fields,extrasaction='ignore');writer.writeheader()
        for row in rows:
            writer.writerow({k:dump(v) if isinstance(v,(list,dict)) else v for k,v in row.items()});count+=1
    temp.replace(path);return count

def inventory(db,cfg,root=None,state=None):
    root=pathlib.Path(root or ROOT);state=pathlib.Path(state or STATE)
    # A verified XML may complete a metadata-only PDF row. Inventory by article identity.
    xml_ids={p.stem for p in (state/'fulltext_xml').glob('*.xml') if p.with_suffix('.json').exists()}
    complete={r[0] for r in db.execute('SELECT article_identity FROM corpus_v2_status WHERE valid_pdf=1 AND downloaded=1')}
    if xml_ids:
        for pid,identity in db.execute('SELECT paper_id,article_identity FROM corpus_v2_status'):
            if pid in xml_ids:complete.add(identity)
    fields=['article_identity','paper_id','title','authors','venue','doi','arxiv_id','openalex_work_id','year','domains','topics','document_type','needs_review','excluded','download_status','next_action','pdf_urls','last_error','source','source_id','landing_url']
    seen=set();counts=collections.Counter();destinations={}
    # Prefer the eligible version, while retaining every version in SQLite/metadata.
    rows=db.execute('SELECT s.article_identity,s.paper_id,p.data,m.normalized,s.domains,s.topics,s.needs_review,s.excluded,s.relevant,p.status,p.error FROM corpus_v2_status s JOIN papers p ON p.id=s.paper_id JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id ORDER BY s.excluded,s.needs_review,s.relevant DESC,s.paper_id')
    def candidates():
        for identity,pid,data,normalized,domains,topics,review,excluded,relevant,status,error in rows:
            if identity in complete or identity in seen:continue
            seen.add(identity);p=json.loads(data);n=json.loads(normalized);allowed=n.get('document_type') in cfg['acceptance']['allowed_types'] and not n.get('pdf_explicit_genre')
            assigned=json.loads(domains);mapped=json.loads(topics);year=n.get('canonical_publication_year')
            scoped=any(d.get('enabled',True) and d['domain_name'] in assigned and (year in d['target_years'] or cfg['sampling'].get('include_classics') and year is not None and 1800<int(year)<min(d['target_years'])) and set(mapped.get(d['domain_name'],[]))&{t['topic_id'] for t in d['topics']} for d in cfg['domains'])
            action='excluded_keep_evidence' if excluded else 'needs_review_before_download' if review or not relevant or not allowed or not scoped else 'resolve_legal_OA_and_download'
            counts[action]+=1
            yield dict(article_identity=identity,paper_id=pid,title=p.get('title'),authors=n.get('authors') or p.get('authors'),venue=n.get('formal_venue') or p.get('venue'),doi=n.get('doi') or p.get('doi'),arxiv_id=n.get('arxiv_id'),openalex_work_id=n.get('openalex_work_id'),year=n.get('canonical_publication_year'),domains=json.loads(domains),topics=json.loads(topics),document_type=n.get('document_type'),needs_review=review,excluded=excluded,download_status=status,next_action=action,pdf_urls=p.get('pdf_urls',[]),last_error=error,source=p.get('source'),source_id=p.get('source_id'),landing_url=p.get('url'))
    allpath=root/'DOWNLOAD_REMAINING_CANDIDATES.csv';total=write_rows(allpath,fields,candidates());destinations['all_remaining']=str(allpath)
    for action,name in [('resolve_legal_OA_and_download','DOWNLOAD_PENDING.csv'),('needs_review_before_download','DOWNLOAD_NEEDS_REVIEW.csv')]:
        with allpath.open(encoding='utf-8-sig',newline='') as stream:write_rows(root/name,fields,(r for r in csv.DictReader(stream) if r['next_action']==action))
        destinations[action]=str(root/name)
    names=[d['domain_name'] for d in cfg['domains'] if d.get('enabled',True)]
    def search_jobs():
        cols=['job_id','domain','query','source','year_from','year_to','cursor','page','status','attempts','next_retry','last_error','topic_id','queue','purpose','parked']
        for row in db.execute("SELECT j.id,j.domain,j.topic,j.source,j.lo,j.hi,j.cursor,j.page,j.status,j.attempts,j.next_retry,j.error,q.topic_id,q.queue,q.purpose,coalesce(f.parked,0) FROM jobs j LEFT JOIN corpus_v4_job_policy q ON q.job_id=j.id LEFT JOIN corpus_v5_frontier f ON f.job_id=j.id WHERE j.status!='done' ORDER BY j.domain,j.id"):
            if row[1] in names:yield dict(zip(cols,row))
    taskfields=['job_id','domain','query','source','year_from','year_to','cursor','page','status','attempts','next_retry','last_error','topic_id','queue','purpose','parked'];tasks=write_rows(root/'DOWNLOAD_UNFINISHED_SEARCH_TASKS.csv',taskfields,search_jobs());destinations['unfinished_search_tasks']=str(root/'DOWNLOAD_UNFINISHED_SEARCH_TASKS.csv')
    v=stage(db,cfg,state);summary=dict(updated=now(),storage_stage=v,remaining_independent_articles=total,action_counts=dict(counts),unfinished_search_tasks=tasks,files=destinations,not_an_exhaustive_list_of_all_future_publications=True,resume='Change storage_stage.target_fulltext_bytes or disable storage_stage after a safe stop; run run_balanced.ps1 -EnableAcquisition -Minutes 0; keep the same SQLite and files')
    atomic_json(root/'DOWNLOAD_REMAINING_SUMMARY.json',summary)
    (root/'DOWNLOAD_STORAGE_STAGE.md').write_text('\n'.join(['# 120 GB 文献阶段与续采清单','',f"快照 UTC：{summary['updated']}。",'',f"已保存全文原文件 {v['fulltext_bytes']/1e9:.3f} GB（PDF {v['pdf_bytes']/1e9:.3f} GB，XML {v['xml_bytes']/1e9:.3f} GB）。",'',f"当前阶段目标：{v['target_fulltext_bytes']/1e9 if v['target_fulltext_bytes'] else '不限'} GB，包含已有全文。数据库、缓存、备份、派生文本另计。",'', '达到目标后停止新下载和发现，等待在途请求、保存检查点、更新清单，再释放 collector.lock。原有 10 GiB/1 GiB 安全阈值继续有效。最多 6 个在途文件，每个最多 75 MiB，因此终点允许小幅浮动；不删除已取得全文。','',f"未完成独立成果 {total}；可解析合法 OA 并继续下载 {counts['resolve_legal_OA_and_download']}；待复核 {counts['needs_review_before_download']}；排除但保留证据 {counts['excluded_keep_evidence']}；未完成检索任务 {tasks}。",'', 'DOWNLOAD_PENDING.csv 保存待下载清单；DOWNLOAD_NEEDS_REVIEW.csv 保留边界/未知类型，不能直接批量接受；DOWNLOAD_REMAINING_CANDIDATES.csv 包含排除证据和所有未完成身份的代表版本；完整版本仍保留在 SQLite。DOWNLOAD_UNFINISHED_SEARCH_TASKS.csv 保存任务、游标、退避和 parked 状态。','', '尚未发现的未来文献无法预先列出；批准范围配置、主题、动态任务及年度缺口仍保留。清单每次统计更新及停止时自动刷新。','', '换大盘时先等待锁释放，再完整复制项目及数据库/WAL、全文和 metadata。使用新位置的 run_balanced.ps1，先提高或关闭 storage_stage 容量目标，再恢复同一 SQLite；不要从空库重建。',''])+'\n',encoding='utf8')
    return summary

if __name__=='__main__':
    from corpus_store import load_config
    with sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True) as db:
        db.execute('BEGIN');print(dump(inventory(db,load_config())))
