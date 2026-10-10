"""Regenerate the handoff guide from committed, auditable project facts."""
import pathlib,json,sqlite3,collections
from corpus_store import ROOT,STATE,now

def main():
    db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
    counts=dict(db.execute('SELECT primary_state,count(*) FROM corpus_v2_status GROUP BY primary_state'))
    downloaded=db.execute("SELECT count(*) FROM papers WHERE status='downloaded'").fetchone()[0]
    effects=json.loads((STATE/'migration_v3/repair_effects.json').read_text(encoding='utf8')) if (STATE/'migration_v3/repair_effects.json').exists() else {}
    gate=json.loads((STATE/'migration_v3/BATCH_VERIFY.json').read_text(encoding='utf8')) if (STATE/'migration_v3/BATCH_VERIFY.json').exists() else {}
    state=json.loads((STATE/'run_state.json').read_text(encoding='utf8'))
    text=f'''# NEXT_RUN_GUIDE

生成时间：{now()}（UTC）。项目位置由脚本自身解析；当前本机工作目录为 `{ROOT}`。

## 当前项目状态

实际候选 {db.execute('SELECT count(*) FROM papers').fetchone()[0]}；已下载 PDF {downloaded}（比迁移基线新增 {downloaded-3163}）；严格状态 {json.dumps(counts,ensure_ascii=False)}。
采集器最后状态：{state.get('status')}；run_id={state.get('run_id')}。实时状态只看 acquisition/run_state.json 与 collector.lock 对应进程。
迁移原数据基线：3163 PDF / 10.46 GiB / 22093 candidates / 2508 accepted / 576 needs_review。

## 本次实际改动与修复

- 建立项目独立 .venv（Python 3.12.14），恢复 Paper Pilot 0.7.0 原来源适配器；安装版本与 Git commit 已锁定。
- 启动脚本改用 .venv 或 CORPUS_PYTHON；旧审计/备份指针改为相对路径；project_paths.py 读取旧数据库 PDF 路径时映射到本机目录，原路径证据不覆盖。
- V2派生表内补充 work publication_status / formal_doi / local_file_version、biblio、来源ID与日期角色；保留原始 papers/observations 数据与历史下载任务。
- 扫描679条未有正式 venue 的已有 arXiv，发现129个 related DOI 线索；每个正式元数据补全都核验标题，arXiv关系额外核验作者。保存 raw Atom / Crossref 响应及 provenance。
- 用户给出的1605.01680已修正为 published / Applied Physics Letters / 10.1063/1.4948770；本地文件仍为 arXiv/preprint，volume108、issue18、article183509。
- 原576 needs_review逐条读取派生证据。最终迁移前后状态变化和实质字段改动：{json.dumps(effects,ensure_ascii=False)}。字段角色新增遍及全部记录，不等于全部论文都得到新外部元数据。
- 扩展现有主题的窄同义词；仍保留 unmapped、独立语义负证据和不确定样本。未把所有 unmapped 强行分类。
- 增加断点批次数量上限、配置范围过滤、候选范围缓存、work/version正在下载时的互斥检查、查询总期限、V2派生索引。并发保持6下载/2OA。
- 支持 Europe PMC 合法OA JATS优先保存与身份核验，保留 raw XML、license证据、section-aware文本侧文件；XML失败不会伪造全文成功。PDF继续可进入未来 GROBID流程。
- 本地模型不可用时不缓存失败结论、不把待复核队列当作完成。smoke配置关闭新模型调用，旧结果保留。
- 每轮 collect.py 结束自动更新 V3 audit。50项测试通过；具体最新测试日志 acquisition/migration_v3/tests.log。

## 下一步命令与下载闸门

从项目根目录在 PowerShell 执行。默认仅查看规划：

```powershell
.\\acquisition\\run_balanced.ps1
```

当前 smoke 验收：{json.dumps({k:v for k,v in gate.items() if k not in ['issues','new_pdf_details']},ensure_ascii=False)}。
smoke须累计至少50个新有效PDF、accepted比例至少85%、原文件不变、独立work去重正常、SQLite完整且断点已保存；未通过不启动扩大批次。下载失败率、各源、venue与年份均应从审计核对，而非只看下载数。

继续/重跑受控smoke（上限60新PDF/本轮，不重下既有work）：

```powershell
$env:CORPUS_CONFIG = (Resolve-Path .\\acquisition\\smoke_v3.json).Path
.\\acquisition\\run_balanced.ps1 -EnableAcquisition -Foreground -Minutes 15 -MaxNewPdfs 60
& .\\.venv\\Scripts\\python.exe .\\acquisition\\verify_batch_v3.py --min-new 50
Remove-Item Env:CORPUS_CONFIG
```

验收 exit 0且smoke_passed=true后，可用下一批config按领域/年度/主题计划执行受控增量。CSV规划在 NEXT_ACQUISITION_PLAN_V3.csv；文件的enabled=false表示规划，不会自动运行：

```powershell
$env:CORPUS_CONFIG = (Resolve-Path .\\acquisition\\incremental_v3.json).Path
.\\acquisition\\run_balanced.ps1 -EnableAcquisition -Foreground -Minutes 20 -MaxNewPdfs 100
Remove-Item Env:CORPUS_CONFIG
& .\\.venv\\Scripts\\python.exe .\\acquisition\\corpus_v3.py audit
```

如果incremental_v3.json尚未生成，先以主corpus_config.json建立下一批配置，限定2019—2025缺口与来源；不得将缺失配置当作全量不限额启动。所有真实采集需要显式 EnableAcquisition。若语义服务未恢复，保持本轮semantic.enabled=false，待复核内容留队列。

## 暂停与恢复

```powershell
New-Item -ItemType File -Path .\\acquisition\\STOP -Force
Get-Content .\\acquisition\\run_state.json
```

采集器完成在途请求、导出、释放 collector.lock 后停止；不要并行启动另一个。已实际验证 STOP 保存和同库重启。再次执行同一条run_balanced命令会清除STOP并从原 jobs cursor / URL状态 / resolution_queue继续，下载成功work跳过。单轮数量上限按当次新下载计数。

修复：在acquisition/repair_v3创建STOP；在论文/来源批次边界保存。恢复前确认repair.lock中的PID已退出，然后仅清除该STOP并运行相同stage。V2修复同理使用repair_v2/STOP。异常断电留下lock时，先用Get-CimInstance核对PID和命令行；确认退出后才移除该lock，不要删除SQLite/WAL/队列。不要用旧完整数据库覆盖后来新增采集进度。

```powershell
& .\\.venv\\Scripts\\python.exe .\\acquisition\\corpus_v3.py enrich --request-limit 80
& .\\.venv\\Scripts\\python.exe .\\acquisition\\corpus_v3.py repair
& .\\.venv\\Scripts\\python.exe .\\acquisition\\corpus_v3.py audit
& .\\.venv\\Scripts\\python.exe -m unittest discover -s acquisition -p 'test_*.py'
```

已请求元数据缓存可复用，新增证据、字段候选、版本边幂等写入；历史修复CSV在repair_v3/history。repair执行前应再用SQLite backup API保存快照。run_reclassify.ps1为完整旧V2流程；本机qwen3:8b缺失时不要运行需要新模型的semantic/all阶段。

## 增加新研究领域

只需复制corpus_config.json内一个domain定义（或建立一个独立JSON配置），改domain_name、aliases、description、positive/context/negative/boundary_terms，设置target_years、target_count、priority、oa_resolvers；topics配置topic_id/name/aliases/queries/positive_terms/negative_terms/target_count/priority。
设置sampling的year_weights、minimum_by_year/minimum_by_domain_year、providers/multilingual_providers；通过CORPUS_CONFIG选择它。先plan-only、小样本验证规则/目录/去重，再按明确上限采集；无需编辑采集核心的领域列表。新增领域空topics/错误正则应先修正，不能用弱关键词凑数。
正式venue/source偏好目前写入每批plan并通过providers/oa_resolvers调度；venue质量仍须审计，不保证配置名即可强制达到某期刊分布。20000全文为长期目标，当前未达到。

## 环境、凭证和 MCP

缺少可选环境变量OPENALEX_EMAIL、UNPAYWALL_EMAIL、SEMANTIC_SCHOLAR_API_KEY。只在本机环境或未入库的.env配置真实值，不能打印secret；未配置邮箱时本项目跳过Unpaywall，不使用虚构联系邮箱补查。公共来源仍可工作。Ollama端点127.0.0.1:11434的qwen3:8b未可用，恢复后再处理无缓存语义任务。
Paper Pilot真实stdio MCP healthcheck=ok，Codex server paper_pilot已注册；当前聊天不保证热加载新增工具，下次会话可检查。参考官方配置说明：https://learn.chatgpt.com/docs/extend/mcp 。禁止影子库的MCP工具已禁用，采集器也拒绝相应URL。
Paper Pilot来源：https://github.com/aytzey/paper-pilot ，commit6aab58edce8bc7c827bfd024df7dfce19d44fed7。原环境锁文件缺失，当前环境恢复记录在MIGRATION_VERIFY_REPORT；requirements-migration.lock是本次验证后的锁。

## 增量同步到远程服务器

尚未提供远程主机/目录/凭证，当前没有上传。export_incremental.py只生成本地可校验包，不访问网络，也不包含.env、Codex auth、虚拟环境或个人MCP配置。
先停止采集/修复，生成一致SQLite backup snapshot和新增PDF/XML/元数据、规范字段、provenance、代码、报告与SHA-256传输清单：

```powershell
& .\\.venv\\Scripts\\python.exe .\\acquisition\\export_incremental.py
# 以某次已成功同步的完整文件清单为增量基线：
& .\\.venv\\Scripts\\python.exe .\\acquisition\\export_incremental.py --since-manifest .\\sync_exports\\<已同步批次>\\CORPUS_FILE_MANIFEST.json
# 远端尚没有原3163篇时，首次使用 --full 包含全部PDF。
```

默认基线是迁移前3163文件；只有远端已经持有该基线才能将默认增量包视作完整同步。包内TRANSFER_MANIFEST.json逐文件SHA-256，CORPUS_FILE_MANIFEST.json是完整文件清单。保存每次成功传输receipt后再推进since-manifest基线；不能把生成包当作已上传。
传输可使用你实际安装的scp/rsync/SFTP；上传到远端新的batch目录，核对hash和snapshot integrity_check，再由远端明确合并/切换，不能向正在运行的库覆盖SQLite。rsync采用追加/--ignore-existing，不使用--delete。初次远端可用python3建立venv，按requirements-migration.lock安装后克隆上述Paper Pilot固定commit并pip install --no-deps；Windows pywin32行已有平台条件。
project_paths.py兼容原Windows路径与项目相对路径，远端运行前先执行迁移/文件核验。后续GROBID/文本chunk/embedding服务没有配置或真实验证，本轮不声称已建设完成。
'''
    (ROOT/'NEXT_RUN_GUIDE.md').write_text(text,encoding='utf8');db.close();print('NEXT_RUN_GUIDE.md updated')

if __name__=='__main__':main()
