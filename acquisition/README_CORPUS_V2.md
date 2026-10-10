> 2026-10-07续接：当前根目录由脚本位置计算（本机D:/papers），运行与停止方式以 [README_CONTINUOUS.md](README_CONTINUOUS.md) 为准。下文的旧电脑路径和有限阶段限制是历史记录，保留用于审计。

# 现有文献系统修复与恢复

根目录继续使用 `D:\Study\njust\揭榜挂帅\papers`。本次不检索或下载新论文，不修改 PDF，不覆盖 `papers.data` 的原始证据。

## 入口

在根目录的 PowerShell 中：

```powershell
# 查看下一轮按年度、领域、方向排序的规划，不启动网络采集
.\acquisition\run_balanced.ps1

# 下阶段获得采集指令后，启动独立本地进程，默认运行60分钟
.\acquisition\run_balanced.ps1 -EnableAcquisition -Minutes 60

# 显式指定前台运行（仍遵守主机限流）
.\acquisition\run_balanced.ps1 -EnableAcquisition -Minutes 60 -Foreground

# 重跑现有文献离线分析：缓存模型结果，无 PDF 下载
.\acquisition\run_reclassify.ps1 -Stage all
```

`run_balanced.ps1` 检查采集和修复锁，实际启动时检查 collector.lock 的工作 PID、命令行和 run_state.json。无法确认启动会明确返回失败，不能据 LauncherPID 推断服务一直运行。旧 `run_phase1.ps1` 为历史阶段入口，已经完成的阶段不重新无限运行。直接调用 collect.py 默认也只展示规划；采集需要明确的 `--enable-acquisition`。

## 配置

`corpus_config.json` 保存全部领域定义、96个现有主题、同义/排除/上下文规则、查询词、优先级、target_years、target_count，以及年度权重和最小量。新增领域只修改此文件。词项是大小写不敏感正则；英语请使用词边界，避免子串误命中。

`sampling.minimum_by_year` 示例：`{"2023":200,"2024":200}`；`minimum_by_domain_year` 示例：`{"软件与通信":{"2023":50}}`。数字只是示例，不是程序内固定配额。`year_weights` 影响缺口优先级。主题 target_count 用于报告缺口，领域 target_count 用于平衡下载。每年、领域、查询方向、来源和语言独立分页；不重置旧范围队列。CorpusRunner 只执行年度队列。

`acquisition.enabled` 默认 false，实际启动仍需显式开关。`-Minutes` 控制运行预算，0代表人工停止模式；磁盘 reserve_gb、no_progress_hours 等限制仍有效。不要在本次修复期间启动采集。

`UNMAPPED_TOPIC_SUGGESTIONS.csv` 只是待审阅建议，不自动改动正式分类。主题 HHI 仅以已分类文献计算；未分类比例和使用总分母的已知主题平方和另外报告。

## 分层分类与元数据

precision_rules 根据核心技术概念、标题/摘要/关键词、领域上下文及排除/歧义条件分类；venue、学术索引类别与来源证据保留供复核，出版声望不自动提升相关性。边界候选才用已存在的本地 `qwen3:8b`，地址仅允许 localhost。保存五档标签、证据、模型摘要、输入摘要及版本缓存，不调用付费或外部语言模型。

修复使用已保存的 Crossref/OpenAlex/Europe PMC/其他来源及上次审计免费API响应补全，不能验证的保持未知。原始非空值不覆盖；候选、置信度、证据及冲突单独保存。当前日期只选择已发表证据，预印本提交、在线出版、纸版卷期和 Europe PMC pubYear 分开。下载主机不作为正式venue或正式版本的依据。

## 新数据库结构

只增加12个 `corpus_v2_*` 表和对应索引，原表 schema 和原始记录不改动；在 jobs 中追加年度任务，不改旧任务及游标。

- metadata：`normalized` JSON 保存 first_submission_date、first_publication_date、published_online_date、published_print_date、source_reported_year、europe_pmc_pub_year、canonical_publication_year、date_source、date_conflicts、formal_publication_status；以及规范doi/authors/abstract/keywords、formal_venue/canonical_formal_venue/ISSN/publisher、repository/pdf_host/preprint_source/downloaded_version、document_type/conference_track、field_provenance/metadata_candidates/metadata_conflicts。
- candidates：paper_id、field、value、source、evidence、confidence、verified，保留逐字段候选。
- relevance：paper_id、domain、classifier_version、relevance_score、relevance_label、relevance_evidence、matched_topics、excluded_reason、method。
- status：article_identity、representative_id、downloaded、valid_pdf、relevant、accepted_for_corpus、duplicate_version、metadata_only、needs_review、excluded、exclusion_reason、domains、topics、primary_state。
- identity/versions：成果成员、DOI/arXiv别名、关系证据及置信度；一成果最多一个accepted代表，其他版本文件保留。
- venues：正式期刊ISSN/名称变体、会议/分轨、证据；会议书系ISSN不合并不同会议。
- jobs/semantic_queue/validation/runs/migrations：年度映射、边界复核断点、400分层验证记录、运行配置和迁移记录。

## 安全暂停与断点

采集：在 acquisition 创建 STOP；采集器停止新增工作并保存队列。修复：在 acquisition/repair_v2 创建 STOP，会在语义/验证批次间停下并保存；prepare 是本地批量规范化步骤，需等当前步骤结束。确认真实PID退出后才清除 STOP 并重启。两个程序互相检查锁，不能同时写数据库。

如果进程意外退出留下锁，先读取其中 PID，用 `Get-CimInstance Win32_Process` 验证命令行和进程是否仍在运行。只有确认该进程已退出才清除对应 lock。`repair_v2/progress.json`、semantic_cache、semantic_queue 和 validation 保存进度；重启读取原 SQLite，不建立另一个文献项目。

## 备份、回滚与验证

改动前的完整 SQLite、schema、脚本和V1审计文件在 `backups/repair-v2-20261007-141256`，迁移前快照在 `backups/migration-20261007-142200`。`repair_backup.txt` 指向首个备份。

```powershell
# 使用 Paper Pilot 配置的 Python，先停止所有采集/修复进程
& 'C:\Users\Tanch\AppData\Local\uv\cache\archive-v0\n2zi6flhk3W_MNh-\Scripts\python.exe' .\acquisition\corpus_migrate.py down
```

down 先保存完整 derived-data archive，再在事务中删除新增表/索引，原表、PDF、年度jobs及后续页进展都保留。恢复旧脚本请从上述备份复制对应脚本；不要拿旧完整 SQLite 覆盖后续新增状态。up 可再次执行。回滚流程已在临时数据库验证；本次没有对真实生产状态回滚。

`verify_corpus_repair.py` 对照改前快照，逐表计算旧行摘要和schema比较、检查全部3163个已下载PDF的大小和SHA256、旧HTTP请求数量、年度队列未执行、400验证样本及accepted唯一性。结果在 `repair_v2/PRESERVATION_CHECK.json`。

42项自动测试覆盖通用误分类、日期口径、未来卷期、原值保护/冲突留存、年度游标幂等及稀缺主题优先、schema回滚、ISSN合并、版本身份、已有有效PDF不重下，以及原有网络限流/阶段审计逻辑。抽样复核使用PDF实际文字与本地模型，非人工全文同行评审，不可直接外推全库错误率。

## 输出

根目录：CORPUS_RECONCILIATION.csv、RECLASSIFICATION_RESULTS.csv、UNMAPPED_TOPIC_SUGGESTIONS.csv、PUBLICATION_VERSION_MAP.csv、YEAR_NORMALIZATION.csv、LITERATURE_AUDIT_V2.md。

明细：repair_v2/VALIDATION_SAMPLES.csv、NEXT_ACQUISITION_PLAN.csv、summary.json、validation_summary.json、PRESERVATION_CHECK.json、pdf_checks.json、semantic_cache。V1审计文件仍保留。恢复采集时 manifest.csv 将同时导出原字段和新的规范日期/领域/状态列。
