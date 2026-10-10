# 文献采集实际结果快照

北京时间：2026-10-06T15:17:42.982951+08:00

采集器在后台持续运行；本报告是时间点快照，最新统计见 stats.json。

|领域|返回命中|去重候选|初筛入选|已下载论文|下载失败或可疑|PDF MiB（标签口径）|
|---|---:|---:|---:|---:|---:|---:|
|生物制药|3175|2882|1222|520|81|1952.57|
|软件与通信|3450|3120|1803|503|61|1148.91|
|新材料|3383|3175|2240|480|106|2035.23|
|半导体与集成电路|2987|2705|1167|294|73|1200.22|

全局：12995 条返回记录，11640 条唯一候选，合并 1355 条重复命中；实际保存 1782 个独立 PDF，共 6260.54 MiB。

当前状态：{"excluded_relevance": 5252, "downloaded": 1782, "pending": 1488, "failed": 311, "no_oa_url": 2806, "downloading": 1}

下载失败原因（按每篇首个原因归类）：{"PoolTimeout": 72, "not_pdf": 89, "HTTP_403": 96, "host_cooldown": 9, "HTTP_404": 6, "suspected_incomplete": 17, "ConnectTimeout": 1, "title_mismatch": 10, "HTTP_502": 6, "unknown": 1, "disallowed_url": 1, "ConnectError": 1, "TimeoutError": 1, "ReadTimeout": 1}

来源完成页数：{"arxiv": 39, "crossref": 56, "doaj": 56, "europe_pmc": 43, "openalex": 61, "semantic_scholar": 12}

检索队列：{"done": 267, "pending": 2580, "retry": 1}

各领域标签可重叠，不能相加作为全局总量。候选已保留无关、非目标类型和无 OA 链接记录；初筛为词法相关性及来源类型筛选，未逐篇评估科研质量。成功 PDF 通过文件结构及首尾页抽样检查，未进行全文分析。

## 已实际执行的细分查询

### 生物制药

CAR T cell therapy、antibody engineering、gene therapy、mRNA therapeutics、mRNA治疗

### 软件与通信

6G wireless communications、distributed systems、large language models、machine learning、大语言模型

### 新材料

functional polymers、nanomaterials、perovskite solar cells、solid state batteries、钙钛矿太阳能电池

### 半导体与集成电路

chiplet integration、gallium nitride power devices、integrated circuit design、silicon photonics、硅光子学

## 文件和恢复

根目录 manifest.csv；领域目录 index.csv；acquisition/metadata 逐篇元数据；state.sqlite3 断点；failures.csv 失败记录；coverage.csv 细分覆盖。

后台运行状态见 run_state.json；停止时创建 acquisition/STOP；中断后执行 acquisition/run.ps1 -Minutes 60，显式重试追加 -RetryFailed。磁盘剩余低于 2 GiB 自动停止并保存进度。详见 README.md。