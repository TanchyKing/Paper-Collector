# 多领域开放获取文献采集

当前正式工作区为D:\papers，使用run_balanced.ps1的长期采集入口；下文旧阶段和旧路径说明保留作历史记录。最新启动、停止、恢复说明见[README_CONTINUOUS.md](README_CONTINUOUS.md)。当前120 GB全文容量阶段覆盖全部批准范围，网络恢复后自动继续；直接运行watch_progress.cmd查看下载及网络状态。

根目录：`D:\Study\njust\揭榜挂帅\papers`

## 第一阶段（当前默认模式）

无限量模式已替换为第一阶段控制：phase1_policy.json 启用后，collect.py 和通用 run.ps1 都会使用 PhaseRunner。推荐通过 run_phase1.ps1 运行：

```powershell
& 'D:\Study\njust\揭榜挂帅\papers\acquisition\run_phase1.ps1'
```

这是隐藏的独立本地进程，默认没有会话时间截止，阶段有明确终点：经实际文件检查和 DOI/arXiv/规范标题/哈希再去重，至少3000篇不同文献，四领域各至少750篇；基本细分覆盖要求每领域至少12个方向各至少10篇。达到条件时停止领取检索、下载和补查任务，等待在途请求结束，执行全库验收，再退出。最终验收若发现计数不足，会继续补齐；不会按预计下载量宣布完成。磁盘不足或连续6小时没有新增有效独立文献则保存状态并给出未完成报告。

当前状态在 phase1_status.json 和 run_state.json，每新增100篇有效独立文献写入 phase1_progress.jsonl/SQLite phase1_progress，并更新根目录 PHASE1_REPORT.md、PHASE1_COVERAGE.csv、PHASE1_QUALITY.csv 和 manifest.csv。采集中的报告明确标为尚未完成；最终 completed 报告才是验收交付。每领域至少30篇分层抽样读取首页、前3页中的标题页及正文四分位/中间/末页，检查标题、作者、DOI和年份的文本对应情况，不进行逐篇全文研究或页面渲染。

优先下载已有候选，必要检索只补字段数量/主题覆盖缺口，选择低覆盖方向、不同年份及分页结果。既有PDF仍保留；阶段不计入明显无效、非目标文献、主题无法确认或重复版本。连接池容量提高以避免多主机重定向造成 PoolTimeout，实际并发及网站限流未提高。中断后确认旧进程已经退出，再运行上述脚本续传；collector.lock 防止两个采集器并行。阶段已完成时再运行也不会恢复无限采集。

这是已实际运行的采集程序及其断点记录。检索结果不代表穷尽文献，相关性是标题、摘要和关键词的词法初筛，未逐篇评估科研质量。PDF 校验只做结构和首尾页抽样，不进行全文分析或页面渲染。

## 继续采集

在 PowerShell 中执行：

```powershell
& 'D:\Study\njust\揭榜挂帅\papers\acquisition\run.ps1' -Minutes 60
```

`-Minutes` 是本次运行的时间预算，**不是文献数量上限**。设为 `0` 不限制本次运行时间；当前仍受第一阶段的数量和覆盖终止条件控制，达到目标会自动停止。程序跳过已完成请求、已有文献和已成功下载文件，保留尚未执行的分页请求。Windows 执行策略若阻止脚本，可用 `powershell -ExecutionPolicy Bypass -File` 仅启动此脚本。

本次任务已转为隐藏后台进程，以 `--minutes 0` 持续采集，后台日志为 `background-*.log` 和 `background-*.err.log`。后台运行时不要再启动第二个采集器；先检查 `run_state.json` 的 PID/更新时间及 `collector.lock`。磁盘剩余空间低于默认 2 GiB 时自动保存现场并停止；进程必须随电脑保持开机和网络可用，系统重启后使用上述命令恢复，不依赖定时任务。后台进程的自动状态更新只写本地文件，不发送消息。

重试下载失败（包括需要人工复核的可疑 PDF）：

```powershell
& 'D:\Study\njust\揭榜挂帅\papers\acquisition\run.ps1' -Minutes 60 -RetryFailed
```

仅更新清单和统计：

```powershell
& 'D:\Study\njust\揭榜挂帅\papers\acquisition\run.ps1' -ExportOnly
```

平稳停止：在 `acquisition` 下创建一个名为 `STOP` 的空文件。程序完成正在进行的请求并保存状态，一般在 20—60 秒内停止。下次通过 `run.ps1` 启动会移除此控制文件。也可以按 Ctrl+C，程序会在 finally 中保存清单。锁文件记录运行进程 PID，禁止同时启动两个采集器。若异常断电留下锁，先确认该 PID 已退出，再移除 `acquisition\collector.lock`；不要删除 SQLite 数据库。

## 保存内容

- 根目录 `manifest.csv`：每篇去重后的候选记录，包括排除、无 OA 链接、待下载、失败及成功状态。
- 四个领域文件夹：PDF 及 `index.csv`。一篇论文可有多个标签，但只保存一个 PDF；其他领域索引指向该路径。
- `acquisition\metadata\<id>.json`：逐篇元数据、来源证据、类型证据、所属领域、PDF 路径与下载状态。源未报告的字段保持空值或 unknown，不编造摘要、作者或类型。
- `acquisition\retrievals.csv`：每次检索命中，含 canonical paper_id 和 duplicate 标记；原始记录在 SQLite observations.payload 中。
- `acquisition\state.sqlite3`：唯一文献、DOI/arXiv/标题索引、文件哈希、查询参数、分页游标、重试时间和操作事件。开启 WAL，事务保存每批查询。
- `acquisition\failures.csv`：下载失败、可疑不完整及无 OA 链接的记录，保留原因。
- `acquisition\coverage.csv`：按领域和细分查询的候选、初筛入选与下载数量。
- `acquisition\stats.json`：最近一次统计；`run-*.log` 为执行日志。
- `acquisition\OPTIMIZATION_RESULTS.md` 与 `optimization-results.json`：本次已完成下载验证的固定结果；`source-success-rates.csv` 为全部 PDF 地址来源的成功率。
- `acquisition\preservation-check.json`：对优化前所有已下载 PDF 的文件存在性、大小和 SHA-256 保护核验；`backups` 保存优化前脚本和完整 SQLite 备份。
- `acquisition\topics.json`：96 个细分主题的中英文关键词，可追加主题后重新运行，已完成请求不会重复。
- `acquisition\seeds`：通过 Paper Pilot MCP 进行四领域初始检索和 PDF 下载的真实返回值。

清单和状态导出会更新程序生成的索引文件；文献 PDF 不覆盖、不删除。中断时已经得到完整响应但尚未入库的检索可能重新请求，这是为避免漏记录；提交后的请求不重复执行。

## 检索与访问约束

初始能力核查、六来源检索及四领域试下载实际通过配置好的 Paper Pilot MCP 完成。批量阶段直接复用该服务已安装版本的 `AcademicSearchService` 来源适配器和免费公共 API，避免模型逐篇工具调用。保留来源原始类型、许可和链接证据；不会调用 research_topic/deep_read_topic/render_pdf_pages。

支持 OpenAlex、DOAJ、Semantic Scholar、Crossref、Europe PMC、arXiv。年份轮换为 2023—2026、2019—2022、2015—2018、1900—2014；早期 OpenAlex 查询按被引数排序作为经典论文发现的启发式，不能等同于已经确认的经典质量。检索包含非 OA 元数据；下载仅使用 OA 记录、公开仓储和 Unpaywall 返回的合法来源。2026 年未来期刊日期可能是预先排期，保留日期证据，不当作已正式出版的结论。

实际接口限制：Paper Pilot 每来源每页 1—100 条；单游标只能指定一个来源；Semantic Scholar relevance 可见前 1000 条、bulk 有内部 token/offset；Crossref 最新日期分页有 10000 条 offset 上限；arXiv 适配器 start 上限 30000。程序保留下一页游标，在主题、年份和语言之间轮换；到达来源上限应进一步拆分关键词或年份。

升级后的入口仍为 collect.py/run.ps1，下载调度与 OA 补查实现在 download_engine.py。默认 PDF 下载并发 6，OA 查询并发 2；arXiv 每主机并发 1、起始请求间隔 4 秒，通用下载主机并发不超过 2、间隔 2 秒，查询 API 每主机并发 1、间隔至少 1.5 秒（Semantic Scholar 4 秒、arXiv API 6 秒）。重定向后的主机也遵守相同门限。429 遵循秒数或 HTTP 日期形式的 Retry-After，并持久化冷却状态；单 URL 收到 401/403、404/410 或明确无效内容后不自动重复请求，先寻找其他 OA 地址。一个主机在十分钟内累计 3 次 403 会冷却一小时。

默认前 10 分钟优先下载，随后按积压阈值自动切换：可处理下载积压超过 750 篇时暂停新增检索，降到 250 篇以内恢复；阈值用于调度，不是最终文献数量上限。恢复后继续原有四领域、年份与语言分页队列。数据库和逐篇状态随每次操作提交，完整 CSV/报告每 120 秒或新增 100 次成功下载导出，避免每 20 秒全量读取数万 JSON 文件阻塞网络。

新增 SQLite 表 download_locations 记录每个合法 URL 的来源、永久拒绝/无效/冷却/暂时失败状态，resolution_queue 记录逐篇逐来源 OA 补查，http_attempts 记录实际 URL 下载尝试和 API 查询的结果、HTTP 状态、耗时和字节数。download_metrics.json 保存当前运行的速度、来源成功率、并发峰值和导出耗时；run-metrics-<run_id>.json 保存已结束的一轮测量，不能把中途瞬时速度当作完整运行速度。

OA 补查交叉使用 DOI、已知 arXiv ID、OpenAlex、Unpaywall、Europe PMC、Semantic Scholar，缺少标识时仅接受严格匹配标题的 arXiv 结果。PMC 全文使用 2026 年更新后的 NIH NLM NCBI PMC Article Datasets on AWS 官方公开桶，经版本 JSON 的 DOI、授权标志、撤稿标志及 PDF 地址校验后下载；不假设每篇都有版本 1，也不使用已退役的旧 FTP 路径。不下载 XML 正文、图片或补充文件进行分析。官方文档：https://pmc.ncbi.nlm.nih.gov/tools/pmcaws/ 。NLM 数据来源注明：NIH NLM NCBI PMC Article Datasets on AWS，访问日期记录在逐篇 oa_cross_checks 元数据。

网络连接超时默认 6 秒，读取停滞超时 20 秒，单地址总期限 45 秒；暂时网络错误逐 URL 最多 3 次并指数退避，永久拒绝地址留档，新增 OA 地址可继续尝试。SQLite 索引和文件哈希继续复用。run.ps1 可用 -DownloadWorkers、-ResolverWorkers、-BacklogFirstMinutes 调整上述并发和初始调度；禁止同时启动第二个采集器。

禁止 Sci-Hub、LibGen、其他影子库和收费下载 API；TLS 校验正常开启，不绕过付费墙或验证码。Unpaywall 沿用已配置的 Paper Pilot 邮箱设置，其未配置时默认值为 nomail@mail.com；没有伪造个人联系信息。

## PDF 校验和统计口径

PDF 必须存在且有 `%PDF-` 文件头、末尾 EOF 标记，解析器不能报告修复，非加密，所有页面结构可加载；抽样首尾页文本并核对标题或 DOI。1 页、疑似封面/摘要/附录、很少可提取文本或标题不匹配的下载标记为可疑，不计成功。扫描 PDF 因无法充分校验可能误判，保留失败原因以便人工复核。校验不能证明每篇内容完整，未进行全文阅读。

DOI、去版本的 arXiv ID、Unicode 规范化标题+年份在下载前去重；SHA-256 在下载后避免保存相同文件。版本可能被合并，同标题且不同年份但无公共标识的版本仍可能需要人工检查。各领域数量可重叠；全局 unique_candidates 和 distinct_pdf_files 才是去重后总量。duplicates_removed 是返回候选中被合并的命中数，未包含服务内部已经去掉、未返回的重复结果。pdf_bytes 只计算保存的独立 PDF，不含缓存/元数据/数据库。
