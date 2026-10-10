"""Export a coherent, dated snapshot without interrupting the collector."""
import csv,json,sqlite3
from collections import Counter
from datetime import datetime,timezone,timedelta
from pathlib import Path
root=Path(__file__).resolve().parent.parent
db=sqlite3.connect(root/'acquisition'/'state.sqlite3'); db.execute('begin')
topics=json.loads((root/'acquisition'/'topics.json').read_text(encoding='utf-8'))
rows=db.execute('select data,status,bytes,error,path from papers').fetchall()
observations=db.execute('select domain,count(*) from observations group by domain').fetchall()
obs=dict(observations); nobs=sum(obs.values()); domains={d:Counter() for d in topics}; coverage={d:set() for d in topics}; failures=Counter(); source_jobs=Counter(); statuses=Counter()
for data,status,size,error,path in rows:
    p=json.loads(data); statuses[status]+=1
    if status in ['failed','suspected_incomplete']:
        failures[(error or 'unknown').split(':')[0]]+=1
    for d in p['domains']:
        domains[d]['candidates']+=1; domains[d]['eligible']+=int(bool(p.get('eligible')))
        domains[d][status]+=1
        if status=='downloaded': domains[d]['pdf_bytes']+=size
for d,t,s,n in db.execute("select domain,topic,source,count(*) from jobs where status='done' group by domain,topic,source"):
    coverage[d].add(t); source_jobs[s]+=n
pdfs=db.execute('select count(distinct sha256),sum(bytes) from files').fetchone()
jobs=dict(db.execute('select status,count(*) from jobs group by status').fetchall())
now=datetime.now(timezone(timedelta(hours=8))).isoformat()
result=dict(snapshot_time_china=now,raw_retrievals=nobs,unique_candidates=len(rows),duplicates_removed=nobs-len(rows),distinct_pdf_files=pdfs[0],pdf_bytes=pdfs[1] or 0,domains=domains,statuses=statuses,download_failure_reasons=failures,source_completed_query_pages=source_jobs,jobs=jobs,actual_queries={d:sorted(v) for d,v in coverage.items()})
db.rollback(); db.close()
(root/'acquisition'/'snapshot.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
lines=['# 文献采集实际结果快照','',f'北京时间：{now}','', '采集器在后台持续运行；本报告是时间点快照，最新统计见 stats.json。','', '|领域|返回命中|去重候选|初筛入选|已下载论文|下载失败或可疑|PDF MiB（标签口径）|','|---|---:|---:|---:|---:|---:|---:|']
for d,v in domains.items(): lines.append(f"|{d}|{obs.get(d,0)}|{v['candidates']}|{v['eligible']}|{v['downloaded']}|{v['failed']+v['suspected_incomplete']}|{v['pdf_bytes']/1048576:.2f}|")
lines+=['',f"全局：{nobs} 条返回记录，{len(rows)} 条唯一候选，合并 {nobs-len(rows)} 条重复命中；实际保存 {pdfs[0]} 个独立 PDF，共 {(pdfs[1] or 0)/1048576:.2f} MiB。",'',f"当前状态：{json.dumps(statuses,ensure_ascii=False)}",'',f"下载失败原因（按每篇首个原因归类）：{json.dumps(failures,ensure_ascii=False)}",'',f"来源完成页数：{json.dumps(source_jobs,ensure_ascii=False)}",'',f"检索队列：{json.dumps(jobs,ensure_ascii=False)}",'', '各领域标签可重叠，不能相加作为全局总量。候选已保留无关、非目标类型和无 OA 链接记录；初筛为词法相关性及来源类型筛选，未逐篇评估科研质量。成功 PDF 通过文件结构及首尾页抽样检查，未进行全文分析。','', '## 已实际执行的细分查询','']
for d,v in coverage.items(): lines+=['### '+d,'','、'.join(sorted(v)),'']
lines+=['## 文件和恢复','', '根目录 manifest.csv；领域目录 index.csv；acquisition/metadata 逐篇元数据；state.sqlite3 断点；failures.csv 失败记录；coverage.csv 细分覆盖。','', '后台运行状态见 run_state.json；停止时创建 acquisition/STOP；中断后执行 acquisition/run.ps1 -Minutes 60，显式重试追加 -RetryFailed。磁盘剩余低于 2 GiB 自动停止并保存进度。详见 README.md。']
(root/'acquisition'/'RESULTS.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
