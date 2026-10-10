# 成果转化项目长期文献库

当前新增中英文兼顾：273主题均有中英文查询，中文约50%的可用检索机会，其他队列继续。全文按实际正文语言保存到D:\papers\corpus_by_language，历史文件保留原路径并通过硬链接归入语言目录；CORPUS_LANGUAGE_INDEX.csv与CORPUS_LANGUAGE_SUMMARY.json记录语言、有效全文数量及证据。watch_progress.cmd同时显示语言数量。详见根目录BILINGUAL_CORPUS_AND_QWEN.md。译文不冒充中文原始文献。

网络中断后可自动恢复：连接失败、超时和服务器临时故障保留重试任务，按10秒起步、最长300秒的退避重试，不因三次网络失败永久放弃。多个主机都失联时暂停新网络任务，每30秒检查公共页面；连接恢复后唤醒队列并继续原检查点。网络状态与恢复次数写入run_state.json及SQLite历史，watch_progress.cmd可直接查看。

已完成并验证的全文不重复下载。中途断开的单个文件会重新请求整个文件并校验，当前不支持字节级断点续传。采集进程持续运行时无需手动重启；电脑重启或进程退出后，用下方启动命令恢复。容量仍为包含已有文件的120 GB，全部32领域保持2000–2026年度范围及0.85相关性阈值。

当前阶段目标为 **120 GB（120,000,000,000 字节）全文原文件总量，包含已有 PDF 和 JATS/XML**；数据库、缓存、备份另计。到达后自动停止并保存清单及检查点。`Minutes 0` 表示无时间限制，当前仍受此容量目标约束。最新用户容量要求覆盖历史无限量描述。详见根目录 `DOWNLOAD_STORAGE_STAGE.md` 和 `DOWNLOAD_REMAINING_SUMMARY.json`。

当前根目录由脚本位置计算，工作区为 `D:\papers`。保留现有 V2/V3 表、原始 metadata、PDF、缓存和历史审计。本入口接续原采集器，历史有限数量的 phase1 入口只用于兼容旧阶段。

## 启动、停止、恢复

```powershell
# 只查看规划，默认不产生网络采集
.\acquisition\run_balanced.ps1
# 当前容量阶段运行，0分钟无时间限制，达到120 GB自动停止
.\acquisition\run_balanced.ps1 -EnableAcquisition -Minutes 0
# 安全停止，等待collector.lock自动消失
New-Item -ItemType File -Path .\acquisition\STOP -Force
# 确认锁消失后恢复，入口自动清除STOP，继续同一SQLite与游标
.\acquisition\run_balanced.ps1 -EnableAcquisition -Minutes 0
```

不要同时启动第二个采集器。入口用排他锁、真实worker PID、命令行和run_state共同核验。异常退出遗留锁时，先确认锁中PID对应进程已退出，再清除锁；不要直接删除活跃进程的锁。后台进程依赖本机运行；关机、休眠会中断，恢复后按上述命令继续。没有配置开机自启。

## 配置与审阅

`acquisition/corpus_config.json` 是正式配置。当前批准并启用 8 个领域族、32 个领域、273 个 topic；原六领域及 142 个 topic 完整保留。批准 JSON 与执行时 67 行提案的并集映射到 99 个原始名称，见根目录 APPROVED_DOMAIN_SCOPE_MAP.csv。`technology_transfer` 和 `ai_knowledge_infrastructure` 继续是独立共性领域族。enabled领域的topics、正/负/上下文词、query、年份策略、优先级、depth、preferred_sources/venues全部由配置提供。

`target_count`/`target_depth`用于评分尺度，不是停止数量。相关性门槛保持0.85；generic model/network/analysis等不作为独立核心证据。Qwen 是可选复核，本次不安装或启动并关闭服务调用；规则边界保留待审核，不自动接受。已有语义负例、逐条验证和版本身份守卫继续有效。

`DOMAIN_EXPANSION_PROPOSAL.csv`、`TAXONOMY_EXPANSION_PROPOSAL.csv`保留观察到的本地数量、代表论文、来源、重叠及置信度。`acquisition/candidate_domains.json`是高置信候选配置，enabled=false、unreviewed。未经审核不会加入正式分类、也不重新归类历史文献。词汇聚类与来源主题归纳不是已完成的模型语义审核。候选目录不是穷尽的产业taxonomy。2026-10-07 用户已明确批准的全部范围，以正式配置和 APPROVED_DOMAIN_SCOPE_MAP.csv 为准；此次批准之外的新候选继续待审核。

## 调度和治理

复用Discovery→Metadata→Relevance→Resolution→Download→Validation→Corpus。年度独立查询；已有范围队列和已完成游标保留。planner每次最多新增96个任务，管理frontier上限384，按coverage评分选择topic的年份、一个来源及用途，采用high/medium/exploration队列与约80/15/5的派发比例；非可用队列会回退。旧的大量未派发年度任务保留在库中，不全量派发。

评分证据包含主题与年份缺口、unmapped、正式发表、综述、重点venue、引用影响、来源成功/可用、领域重要性、新兴性和成果转化关联。已充足主题降低优先级，仍可继续。完成年度检索每周刷新，不改写原完成任务。综述与original research分别标记；经典年度检索按OpenAlex cited_by_count排序，并从重复参考文献建立持久引用发现队列。无合法全文时保存metadata。

PDF候选frontier限制600；下载慢时元数据搜索降低频率但继续，PDF和OA解析保持主机限流。免费API每日预算按UTC持久计数，缓存命中也保守计入预算。403/429由现有引擎冷却/换源；不绕过付费墙或访问控制，真实邮箱缺失时跳过Unpaywall。

`corpus_v4_tiers`保存A Core/B Standard/C Supporting、综述角色、理由、引用/来源及版本。正式venue和类型只是代理信号，A需要至少两个独立信号且含影响力或配置重点venue；不使用影响因子作为单独判据。Tier不删除或限制PDF保存。全文有效性仍由现有PDF/JATS身份、完整性、相关性、类型和版本去重判定。

每增加2000个accepted独立全文成果或6小时生成 `CORPUS_AUDIT_<UTC>.md` 与 `NEXT_ACQUISITION_PLAN.csv`；每24小时重新分析领域/主题提案，分析读取快照并在采集线程外执行，结果由唯一采集器保存。每两分钟更新统计、覆盖、规划，并做被动WAL检查点。

剩余空间低于10GiB时暂停新PDF，允许最多额外2000条metadata候选，恢复空间后继续。低于1GiB则整体安全停止，给SQLite留余量。已在途请求最多6个、单请求75MiB上限。磁盘数据分别标明PDF、项目语料占用（不含环境/备份/导出）、卷已用、平均PDF和可用空间。

## 实时观测与证据

- `stats.json`及`acquisition/stats.json`：用户要求的统计字段，accepted是PDF/JATS合并后的独立成果；accepted_pdf另列；total_research_outputs含metadata-only独立身份。
- `LIVE_COVERAGE.csv`：domain→topic→year→publication_type→article_role→count，多标签不能直接相加当作全库独立总数。
- `acquisition/run_state.json`：PID、真实运行状态、时长预算/批次上限、低磁盘暂停、语义服务可用性。
- `acquisition/download_metrics.json`：当前run速率、新PDF、主机与请求成功率。
- `acquisition/LATEST_AUDIT.json`：最近审计路径；`NEXT_ACQUISITION_PLAN.csv`：下一批任务评分依据。
- `acquisition/continuous_v4/LATEST.txt`：本次迁移备份与SHA基线、测试日志、MCP检查、两轮真实采集/断点恢复验收。

审计互斥主状态needs_review/excluded主要报告已下载记录；metadata-only中的复核/排除标志也保留在corpus_v2_status。DOI/venue未知保持未知，日期冲突留存证据，不删除有价值的合法预印本。

元数据来源文档：[Crossref REST API](https://www.crossref.org/documentation/retrieve-metadata/rest-api/)。公共免费接口可受服务速率、可用性及权限限制；这些结果反映实际请求，不保证每个领域都有OA全文。

## 全范围扩展执行与覆盖

逐项状态见 `APPROVED_SCOPE_COVERAGE.csv`，分别记录配置、活跃检索任务、实际派发、成功检索、有效独立全文和 metadata-only；检索成功可以返回零篇，不能当作全文覆盖。`ALL_DOMAINS_EXECUTION_REPORT.md/.json` 保存本次执行快照。

持久化 `corpus_v5_dispatch` 按领域族、领域、主题轮询，再以覆盖评分排序；错误和重启不会清零派发历史。`corpus_v5_frontier` 暂存超额历史任务和分页，保留游标并在有空间时恢复。全量 manifest/index 每 600 秒导出，轻量统计、覆盖及规划每 120 秒更新；检查点和队列仍逐步提交。

备份路径见 `acquisition/ALL_DOMAINS_BACKUP.txt`；包含原 SQLite、配置/代码、批准/提案快照、旧 PDF 哈希基线、保留检查和真实停止/恢复验收。历史补充标签证据在 `acquisition/APPROVED_HISTORICAL_LABEL_EVIDENCE.json`，状态历史在 `corpus_v5_status_history`。

若异常退出遗留锁，先核验锁中 PID 的真实进程命令行，确认对应工作进程已经退出后才清理失效锁。随后正常恢复会将 running/downloading/inflight 恢复为 pending，保留 attempts、退避、已拒绝 URL、游标、身份及版本关系。不要恢复旧备份覆盖当前库来继续采集。

## 达到120 GB后的续采

停止原因是 `stopped_capacity_target`。不要直接再次启动相同目标，否则会立即安全停止。换盘完成并校验项目、历史路径、SQLite 和全文后，先把配置 `storage_stage.target_fulltext_bytes` 提高，或把 `storage_stage.enabled` 设为 false，再运行同一入口恢复。原 0.85 相关性阈值、领域覆盖、限流、去重及复核队列保持。

已知可续采身份清单 `DOWNLOAD_PENDING.csv`；边界与未知类型 `DOWNLOAD_NEEDS_REVIEW.csv`；所有未取得有效全文的代表身份及排除证据 `DOWNLOAD_REMAINING_CANDIDATES.csv`；未完成检索任务和游标 `DOWNLOAD_UNFINISHED_SEARCH_TASKS.csv`。尚未发现的未来文献继续由配置/主题/年度缺口规划，不声称清单穷尽所有未来文献。每次统计更新和停止时自动刷新。

## 自行查看下载进度

在 PowerShell 运行 `& D:\papers\acquisition\watch_progress.ps1`，每15秒自动刷新实际进程存活、120 GB进度、PDF总数、本次新增、最近5/15分钟成功下载速度、全程平均及最后成功时间。Ctrl+C只关闭查看，不停止后台采集。加 `-Once` 只查看一次。该脚本只读SQLite与状态文件，不修改队列或启动采集器。

“进程存活”和“PDF正在增长”分开显示；近期速度为零时查看最后成功时间及当前日志，不要把全程平均当作即时速度。JSON实时指标见 `acquisition/download_metrics.json`、`acquisition/run_state.json`；容量和续采清单汇总见 `DOWNLOAD_REMAINING_SUMMARY.json`。

若 Windows PowerShell 提示禁止运行 .ps1，请使用 `& D:\papers\acquisition\watch_progress.cmd`，或双击该 .cmd。它只为查看进度的子进程指定 ExecutionPolicy Bypass，不修改系统/用户永久策略；若组织组策略另有限制，它仍可能阻止运行。加 `-Once` 可只查看一次。总 PDF 包含全部历史文件；本次新增只从本次启动前基线计算，重启会重设本次基线，旧文件不删除。

## 2000–2026 年度补充与下载恢复

2026-10-07 客户端授权扩展后，32个领域的常规年度范围均为2000–2026。2023/2024权重仍为3/2.8；历史2000–2018约占20%的检索派发机会，并轮换独立年份，避免只有配置扩大却永远查询最近年份。2000年前保留有限经典/引用链检索。每次新增最多96任务，活跃frontier仍不超过384，PDF候选仍最多600。

PDF候选窗口70%名额优先给未冷却主机上已到重试时间的可用链接，按领域轮询；其余名额保留OA解析。不能仅按最近metadata时间让冷却/无OA候选挤出旧的可下载链接。实际HostGate继续限流，403/404/无效PDF结果不被重置。

本地PoolTimeout触发工作线程HTTP客户端连接池重建，不消耗远程链接的永久失败次数；原有历史请求记录完整保留。OA解析不会因某来源有待执行搜索就整体让路。已对旧PoolTimeout关闭的600条候选恢复有限重试，拒绝/无效URL保护保持。网络连接故障仍可能发生，扩展年份不保证单独提高下载速度。

测试及验收：acquisition/years-recovery-tests.log、acquisition/YEARS_RECOVERY_CHECK.json；备份：acquisition/YEARS_RECOVERY_BACKUP.txt。120 GB容量目标和0.85相关性门槛未更改。
