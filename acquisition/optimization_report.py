import argparse,csv,json,sqlite3
from collections import Counter,defaultdict
from pathlib import Path
from datetime import datetime,timezone,timedelta
root=Path(__file__).resolve().parent.parent;state=root/'acquisition'
parser=argparse.ArgumentParser();parser.add_argument('--run-id',action='append');args=parser.parse_args()
db=sqlite3.connect(state/'state.sqlite3');db.execute('begin')
ids=args.run_id or [r[0] for r in db.execute("select run_id from engine_runs where finished is not null order by started")]
runs=[]; sources=defaultdict(Counter); recovered=Counter(); resolutions=Counter(); added_urls=Counter(); changed=Counter()
for rid in ids:
    row=db.execute('select summary from engine_runs where run_id=?',(rid,)).fetchone()
    if row and row[0]: runs.append(json.loads(row[0]))
    for src,outcome,n,seconds in db.execute("select provider,outcome,count(*),sum(seconds) from http_attempts where run_id=? and stage='download' group by provider,outcome",(rid,)):
        sources[src]['attempts']+=n; sources[src]['seconds']+=seconds or 0
        if outcome=='success': sources[src]['success']+=n
        else:sources[src][outcome]+=n
    for provenance,n in db.execute("select l.provenance,count(*) from http_attempts a join download_locations l on a.paper_id=l.paper_id and a.url=l.url where a.run_id=? and a.outcome='success' and a.stage='download' group by l.provenance",(rid,)): recovered[provenance]+=n
    for data,n in db.execute("select p.data,count(*) from http_attempts a join papers p on p.id=a.paper_id where a.run_id=? and a.outcome='success' and a.stage='download' group by a.paper_id",(rid,)):
        for d in json.loads(data)['domains']: changed[d]+=n
    for payload in db.execute("select payload from events where kind='oa_crosscheck' and json_extract(payload,'$.run_id')=?",(rid,)):
        p=json.loads(payload[0]); resolutions[p['provider']]+=1; added_urls[p['provider']]+=p['new_locations']
db.rollback();db.close()
for v in sources.values():
    v['success']=v.get('success',0); v['success_rate']=v['success']/max(1,v['attempts']);v['mean_seconds']=v['seconds']/max(1,v['attempts'])
minutes=sum(r['minutes'] for r in runs);new=sum(r['new_pdf_files'] for r in runs)
baseline=json.loads((state/'optimization-baseline.json').read_text(encoding='utf-8'))['baseline_window']
now=datetime.now(timezone(timedelta(hours=8))).isoformat()
result=dict(snapshot_time_china=now,run_ids=ids,completed_runs=runs,measured_active_minutes=minutes,new_unique_pdf_files=new,pdf_per_minute=new/max(.001,minutes),baseline_pdf_per_minute=baseline['success_per_minute'],observed_speed_ratio=(new/max(.001,minutes))/baseline['success_per_minute'],domain_new_downloaded_papers=changed,source_rates=sources,oa_cross_checks=resolutions,new_oa_locations=added_urls,download_provenance=recovered,denominator='completed URL download attempts, including redirects; not unique papers or metadata API availability')
(state/'optimization-results.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
columns=['source','attempts','success','success_rate','mean_seconds','forbidden','invalid','not_found','transient','cooldown','exhausted']
with (state/'source-success-rates.csv').open('w',encoding='utf-8-sig',newline='') as f:
    w=csv.DictWriter(f,fieldnames=columns,extrasaction='ignore');w.writeheader();w.writerows(dict(source=k,**v) for k,v in sources.items())
lines=['# 下载系统优化与实际运行验证','',f'快照时间（北京时间）：{now}','',f'实际运行 {minutes:.2f} 分钟，新增独立 PDF {new} 份，速度 {result["pdf_per_minute"]:.2f} 篇/分钟。旧进程观测速度 {baseline["success_per_minute"]:.2f} 篇/分钟，本轮比值 {result["observed_speed_ratio"]:.2f}。两次处理的论文和网络状态不同，不能保证后续始终保持这个比值。','', '## 修改与验证','', '复用原有目录、数据库、已下载 PDF 与检索分页队列。6 个下载工作线程、2 个 OA 工作线程，主机独立限流与冷却；优先积压，达到低积压阈值后恢复四领域检索。403 等永久拒绝逐 URL 留档，不强行重试；DOI 落地页先查 OA，交叉查找 OpenAlex/Unpaywall/Europe PMC/arXiv/Semantic Scholar，并接入 PMC 当前免费公开数据桶。连接 6 秒、读取停滞 20 秒、单地址总期限 45 秒。全量导出改为 120 秒或新增 100 次成功下载，逐条状态始终写入 SQLite。','', '五项初始针对性测试通过，包括重复领取、403 换源、冷却持久化、标识归一化、已有文件保护；另见新增调度测试。完整 PDF 结构检查与旧文件哈希保护结果见 audit.json 和 preservation-check.json。','', '## 各来源结果','', '分母为本轮已经完成的 URL 下载尝试，包含重定向；不是文献检索来源的收录率。','', '|来源|成功/尝试|成功率|平均下载尝试耗时（秒）|','|---|---:|---:|---:|']
for src,v in sorted(sources.items(),key=lambda x:-x[1]['attempts']): lines.append(f'|{src}|{v["success"]}/{v["attempts"]}|{100*v["success_rate"]:.1f}%|{v["mean_seconds"]:.2f}|')
lines+=['','## OA 补查与领域分布','',f'新增地址：{json.dumps(added_urls,ensure_ascii=False)}', '',f'实际下载地址来源：{json.dumps(recovered,ensure_ascii=False)}','',f'各领域新增论文（标签可重叠）：{json.dumps(changed,ensure_ascii=False)}','', '## 仍有的限制','', 'arXiv 每主机间隔 4 秒、并发 1，因此理论起始请求上限 15 次/分钟。出版商可能返回 403、HTML 验证页、无效 DOI 落地页；这些不能通过增加并发解决。部分 PDF 大、跨境连接慢或站点响应慢。OA 元数据可缺少 PDF，PMC 有文章无 PDF 的情况，Semantic Scholar 等仍可能返回 429。有效性检查会拒绝可疑单页、扫描件、截断或标题不匹配内容。','', '## 继续运行','', '当前状态见 run_state.json；后台以无总数量上限的模式运行，优先积压并自动恢复分页检索。停止时创建 acquisition/STOP；进程完全退出后用 run.ps1 -Minutes 0 续传。不要并行启动多个采集器。']
(state/'OPTIMIZATION_RESULTS.md').write_text('\n'.join(lines),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
