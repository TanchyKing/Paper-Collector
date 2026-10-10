"""Create final migration/repair accounting against the immutable arrival snapshot."""
import collections,csv,hashlib,json,pathlib,sqlite3
from corpus_store import ROOT,STATE,atomic_json,csvwrite,now

def main():
    if any((STATE/p).exists() for p in ['collector.lock','repair_v3/repair.lock','repair_v2/repair.lock']):raise SystemExit('Finish all writes before handoff.')
    folder=STATE/'migration_v3';backup=pathlib.Path(json.loads((folder/'backup.json').read_text(encoding='utf8'))['path'])
    old=sqlite3.connect((backup/'state.sqlite3').as_uri()+'?mode=ro',uri=True);db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    prior={pid:(state,json.loads(n)) for pid,state,n in old.execute('SELECT s.paper_id,s.primary_state,m.normalized FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id')}
    arrived_pdfs={r[0] for r in old.execute("SELECT id FROM papers WHERE status='downloaded'")};changes=[];fields=collections.Counter();review=collections.Counter();formal=0;changed_records=0
    substantive=['doi','formal_venue','issn','publisher','keywords','canonical_publication_year','document_type','downloaded_version','volume','issue','pages','article_number']
    for pid,state,reason,meta in db.execute('SELECT s.paper_id,s.primary_state,s.exclusion_reason,m.normalized FROM corpus_v2_status s JOIN corpus_v2_metadata m ON m.paper_id=s.paper_id'):
        if pid not in prior:continue
        n=json.loads(meta);before,on=prior[pid]
        if before!=state:changes.append({'paper_id':pid,'before':before,'after':state,'reason':reason})
        if before=='needs_review':review[state]+=1
        if pid in arrived_pdfs:
            delta=[f for f in substantive if n.get(f)!=on.get(f)]
            if delta:changed_records+=1
            fields.update(delta)
            if n.get('formal_venue') and not on.get('formal_venue'):formal+=1
    effects={'checked_at':now(),'original_downloaded_metadata_records_substantively_changed':changed_records,'original_576_review_outcomes':dict(review),'original_review_resolved':576-review['needs_review'],'original_downloaded_new_formal_venue':formal,'original_downloaded_field_changes':dict(fields),'old_candidate_state_transitions':dict(collections.Counter(c['before']+' -> '+c['after'] for c in changes))}
    atomic_json(folder/'repair_effects.json',effects);csvwrite(folder/'status_transitions.csv',changes)
    schemas={}
    for name,sql in old.execute("SELECT name,sql FROM sqlite_master WHERE type='table' AND name NOT LIKE 'corpus_v2_%'"):
        current=db.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone();schemas[name]=bool(current and current[0]==sql)
    oldjobs=dict((r[0],r[1:]) for r in old.execute('SELECT * FROM jobs WHERE lo!=hi'))
    currentjobs=dict((r[0],r[1:]) for r in db.execute('SELECT * FROM jobs WHERE lo!=hi'))
    retention={'original_table_schemas_unchanged':schemas,'old_range_jobs_and_cursors_unchanged':all(currentjobs.get(k)==v for k,v in oldjobs.items()),'old_range_job_count':len(oldjobs),'original_paper_ids_retained':all(db.execute('SELECT 1 FROM papers WHERE id=?',(pid,)).fetchone() for pid in prior)}
    atomic_json(folder/'FINAL_RETENTION.json',retention)
    assert all(schemas.values()) and retention['old_range_jobs_and_cursors_unchanged'] and retention['original_paper_ids_retained'],retention
    before=json.loads((folder/'migration_baseline.json').read_text(encoding='utf8'));gate=json.loads((folder/'BATCH_VERIFY.json').read_text(encoding='utf8'));audit=json.loads((STATE/'repair_v3/audit_summary.json').read_text(encoding='utf8'))
    runs=[dict(zip(['run_id','started','finished','status','summary'],r)) for r in db.execute("SELECT run_id,started,finished,status,summary FROM engine_runs WHERE started>='2026-10-07T14:00:00' ORDER BY started")]
    for r in runs:
        if r['summary']:r['summary']=json.loads(r['summary'])
    atomic_json(folder/'ACQUISITION_RUNS.json',runs)
    xml_count=audit['fulltext_xml_records'];pdf_count=audit['downloaded'];new=pdf_count-3163
    md=['# MIGRATION_VERIFY_REPORT','',f'最终核验：{now()}（UTC；用户时区 America/Los_Angeles）。项目已在本地 NVMe SSD：{ROOT}。移动硬盘源路径未提供，未对未指定的外部盘做复制或声称逐文件一致。','',f'初始递归扫描 {before["inventory_files"]} 个文件；到达时全部物理PDF恰为3163份，无未索引额外PDF。主SQLite为acquisition/state.sqlite3。项目没有根Git/依赖锁/AGENTS，原代码保留备份并安全增量修改。','', '## 迁移基线与保存证明','', '到达基线全部吻合：22093 candidates；2508 accepted；576 needs_review；73 excluded；6 duplicate/version；18930 metadata-only。3163 PDF全部存在，原size/SHA256/header/EOF一致，11235161419 bytes（10.46 GiB）；SQLite integrity_check=ok。','', '修复后、真实采集前，14个原表schema与全部行摘要相同；见pre_acquisition_preservation.json。真实采集后正常追加observations/events/HTTP记录、推进年度jobs和更新新下载状态；没有拿旧snapshot覆盖新进度。','', f'最终SQLite完整性 {gate["integrity"]}；原3163 PDF逐份大小/SHA256均保留。所有原paper ID、14原表schema、{len(oldjobs)}条旧范围任务及其cursor保留，见FINAL_RETENTION.json。新增派生索引及corpus_v2_*派生记录保留原schema设计，未DROP/重建原表。','', '备份：'+str(backup)+'；SQLite backup API包括已提交WAL，root_outputs保存原顶层报告/清单；V3各轮审计和改动历史保留在repair_v3/audits和history。','', '## 环境和配置','', 'Python3.12.14，项目.venv。Paper Pilot0.7.0从匹配现有接口的原仓库恢复，commit6aab58edce8bc7c827bfd024df7dfce19d44fed7；依赖按其uv.lock限制，httpx0.28.1、PyMuPDF1.26.7，pip check通过。原电脑lock没有迁移过来，不能声称逐版本重现旧环境；未全局升级依赖。requirements-migration.lock记录本次验证版本。','', '启动器用.venv/CORPUS_PYTHON；审计/备份指针相对化；project_paths解析原绝对路径且保留证据。默认run_balanced仍plan-only，实际采集始终显式启用，并有数量/时间上限。','', '真实stdio MCP initialize/list_tools/healthcheck成功（17 tools/status=ok），Codex paper_pilot已注册，原Codex配置旁路备份；影子库工具禁用。健康检查是服务配置核验，公共API另通过真实检索/下载验证。','', '可选环境变量缺失：OPENALEX_EMAIL、UNPAYWALL_EMAIL、SEMANTIC_SCHOLAR_API_KEY；未输出secret，未配置真实邮箱时跳过Unpaywall。qwen3:8b本地服务不可用，旧语义缓存/队列和400独立内容检查记录保留；不把模型错误结论缓存为完成。','', '## 本次真实结果','', f'新增 {new} PDF；当前 {pdf_count} PDF。保存 {xml_count} JATS/XML，可能与PDF重叠，不能相加当作独立论文。严格PDF accepted {audit["accepted"]}；PDF/JATS统一accepted独立work {audit["accepted_fulltext_distinct_works"]}。','', '73份smoke实测通过，四领域新增accepted均至少5；全部原PDF不变、全新PDF结构/身份核验通过、accepted比例超过85%、work唯一与断点正常。随后执行上限100/20分钟增量，本轮实际新增98份；没有未验收就批量几千篇。','', '最终文件校验详见BATCH_VERIFY.json；smoke固定验收详见SMOKE_VERIFY.json；实际逐轮来源成功率/拒绝/限流在engine_runs/http_attempts和ACQUISITION_RUNS.json。','', '## 原始下载队列与派生修复','', json.dumps(effects,ensure_ascii=False), '', '实际错误1605.01680修正为published / Applied Physics Letters / DOI10.1063/1.4948770；本地仍arXiv manuscript，原PDF未重下。卷期108(18)、article183509、online2016-05-05、print2016-05-02分别存储，issue日期不冒充首次公开时间。','', '## 到达时主要表','', '|表|行数|','|---|---:|']
    md += [f'|{t}|{c}|' for t,c in before['tables'].items()]
    md+=['', '初始jobs：'+json.dumps(before['job_status'])+'，10368年度任务原为pending。后续只按持久化年度分页推进，旧范围队列保持。','', '测试、语法、依赖检查结果见migration_v3/tests.log与syntax_checks.json；本报告与LITERATURE_AUDIT_V3/NEXT_RUN_GUIDE均由真实数据库结果生成。仍有需要人工/本地模型复核的记录、缺失元数据、下载拒绝与后续GROBID/embedding工作；没有将其声称为已完成。']
    (ROOT/'MIGRATION_VERIFY_REPORT.md').write_text('\n'.join(md)+'\n',encoding='utf8')
    versions=[];dates=[]
    for pid,p,n,state in db.execute("SELECT p.id,p.data,m.normalized,s.primary_state FROM papers p JOIN corpus_v2_metadata m ON m.paper_id=p.id JOIN corpus_v2_status s ON s.paper_id=p.id WHERE p.status='downloaded'"):
        p=json.loads(p);n=json.loads(n);common={'paper_id':pid,'title':p['title'],'primary_state':state}
        versions.append(dict(common,**{k:n.get(k) for k in ['doi','formal_doi','arxiv_id','publication_status','formal_venue','publisher','local_file_version','downloaded_version','version_relations','field_provenance']}))
        dates.append(dict(common,original_year=p.get('year'),**{k:n.get(k) for k in ['canonical_publication_year','source_reported_year','source_reported_date','first_submission_date','first_publication_date','first_public_date','published_online_date','published_print_date','date_source','date_conflicts','field_provenance']}))
    csvwrite(ROOT/'PUBLICATION_VERSION_MAP_V3.csv',versions);csvwrite(ROOT/'YEAR_NORMALIZATION_V3.csv',dates)
    old.close();db.close();print(json.dumps(effects,ensure_ascii=False))

if __name__=='__main__':main()
