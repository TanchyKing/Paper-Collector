# 学术论文语料库自动采集系统 · 复刻与运行指南

> **本文档的用途**：在任意一台全新机器（无任何本项目基础）上，由 AI 执行代理（ZCode / Codex 等）依据本文档独立复刻整套系统并投入运行。文档包含三部分：**启动问卷（超参数）**、**系统实现规格**、**运维手册**。
>
> **给执行代理的第一条指令**：不要跳过第 1 节直接写代码。正确顺序是——先向用户提出第 1 节的问卷并根据其回答生成范围配置，再按第 2–4 节搭建与实现，最后按第 5 节分阶段启动。全程遵守第 3.7 节的合法与安全红线。

---

## 0. 系统一句话概述

一个**长期无人值守的开放获取论文采集流水线**：从合法 OA 来源检索元数据 → 规则引擎打相关性分 → 解析合法全文链接 → 下载并校验 PDF/JATS → 按领域文件夹归档 → 持续审计与断点续采。全部状态持久化在一个 SQLite 库中，任何中断（断网、关机、断电）都可无损恢复。

设计基调（复刻时必须保留的精神，比代码形状更重要）：

1. **不伪造成功**——下载失败就是失败，XML 失败不能记成全文成功，检索返回零篇不能算覆盖。
2. **不确定就挂起**——规则判不准的进 needs_review 队列，绝不降门槛自动接受。
3. **一切可审计**——每个阶段有验收证据文件（`*_CHECK.json`）和备份指针，原始文件永不改写。
4. **对来源友善**——限流、退避、每日 API 预算、只走合法 OA，绝不绕过付费墙。

---

## 1. 启动问卷（超参数）

执行代理在新机器上搭好骨架后、开启任何下载之前，**必须**向用户提出以下问题。其中前两问决定是否允许开启下载。

### 1.1 必问项

| # | 问题 | 类型 | 默认值 | 说明 |
|---|------|------|--------|------|
| Q1 | **研究领域** | 必答其一 | 无 | 一个或多个领域名（如"半导体与集成电路""生物制药"）。代理负责为每个领域自动扩展：别名、检索式、正面词/上下文词/负面词/边界词、主题(topic)细分 |
| Q2 | **关键词** | 必答其一 | 无 | 自由检索词（如"固态电池电解质"），不必归属于任何领域 |
| Q3 | **语言** | 可选 | **英文** | `en` / `zh` / 双语。双语=两套检索配置都启用，全文按**正文实际语言**归档（不是按检索词或作者国籍判断） |
| Q4 | **期刊范围** | 可选 | **全部可检索到的期刊** | 用户可给优先期刊/出版社清单 → 写入 `preferred_venues` 作调度优先级（注意：这是偏好不是硬过滤，无法保证达到某期刊分布；过度收窄会显著减少 OA 可得全文，代理应提醒） |
| Q5 | **时间范围** | 可选 | **最近 10 年** | 即"当前年份−9 … 当前年份"（如 2026 年运行 → 2017–2026）；最近 2–3 年给约 3 倍检索权重，其余年份均摊。用户可给任意区间 |

**门控规则（严格执行）**：

- Q1 与 Q2 **至少一项非空** → 允许开启下载。
- **两项都为空 → 项目照常搭建、配置生成、进入 plan-only 模式，但绝不开启采集**，并向用户明确说明："未开启下载：缺少领域与关键词，请补充其一。"
- Q3/Q4/Q5 用户不答就用默认值，不追问。

### 1.2 可选进阶项（用户跳过则全部用默认值，一次问完不反复打扰）

| # | 问题 | 默认值 |
|---|------|--------|
| Q6 | 全文容量目标（到达后自动停止） | 120 GB（可改小/可关闭） |
| Q7 | 是否启用本地语义边界复核（Ollama，仅限本机地址） | 关闭 |
| Q8 | 联系邮箱（OpenAlex/Unpaywall 礼貌池） | 不填 → 跳过 Unpaywall，**绝不编造邮箱** |
| Q9 | 下载并发 / OA 解析并发 | 6 / 2 |
| Q10 | 项目根目录 | 当前工作目录下的 `papers/` |

### 1.3 回答如何映射到配置

- **只给领域（Q1）**：每个领域生成一个 domain 配置块，代理扩展同义词与检索式；topics 从领域名拆解或代理拟定，每个 topic 自带 queries 与正负词。
- **只给关键词（Q2）**：生成一个或多个"临时领域"（domain_name 可用关键词组命名），`positive_terms` = 关键词本身，queries 由关键词组合生成；这是合法路径，不要求用户补领域。
- **两者都给**：关键词并入所属领域的 queries/positive_terms；无关关键词单列临时领域。
- **Q3 双语**：每个 topic 生成中英两行检索配置（英文走 arXiv/Crossref/OpenAlex/Europe PMC 等；中文额外启用 DOAJ 中文过滤、Crossref 中文查询），中文约占检索派发机会的一半；无中文结果的领域不算失败。
- **Q5**：写入 `target_years` 与 `year_weights`（近 2–3 年权重 3/2.8 类比例递减；历史年份保留约 20% 派发机会防止只查新文献；更早年份仅留经典/引用链检索）。

---

## 2. 系统实现规格

### 2.1 环境准备（新机器）

```powershell
# Windows（原项目平台；Linux/macOS 把 venv 路径与硬链接改为对应实现即可）
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install httpx beautifulsoup4 lxml feedparser \
  PyMuPDF bibtexparser jsonschema python-dotenv pydantic
# Windows 追加: pywin32（硬链接 API）
```

依赖原则：**纯 Python、无 GPU、无数据库服务**；核心依赖只有 httpx（异步 HTTP）、PyMuPDF（PDF 校验/抽样文本）、lxml/BeautifulSoup（XML/HTML 解析）。不要引入重型框架。

### 2.2 目录结构

```
papers/
├── acquisition/               # 全部代码
│   ├── state.sqlite3          # 唯一状态库（候选/任务/游标/身份/审计历史）
│   ├── collect.py             # 采集主循环
│   ├── corpus_config.json     # 正式配置（由问卷回答生成）
│   ├── corpus_rules.py        # 相关性规则引擎
│   ├── download_engine.py     # 下载/校验/限流
│   ├── network_recovery.py    # 断网自动恢复
│   ├── run_balanced.ps1       # 唯一启动入口（生成 run_state.json、collector.lock）
│   ├── verify_*.py            # 各阶段验收脚本
│   ├── test_*.py              # unittest 回归测试
│   ├── STOP                   # 存在此文件 = 请求安全停止
│   └── collector.lock         # 运行互斥锁（内容=PID）
├── <领域名>/                   # 每个领域一个文件夹
│   ├── <哈希>.pdf             # 文件名=内容/身份哈希
│   └── index.csv
├── corpus_by_language/        # 可选（双语模式）：zh/en/mixed/other/unknown 视图
├── stats.json                 # 全局统计（约每 2 分钟刷新）
├── run_state.json             # PID/状态/速率/容量进度（实时真相源）
└── CORPUS_AUDIT_<UTC>.md      # 周期审计报告
```

### 2.3 数据模型（SQLite 核心表）

| 表 | 内容 |
|----|------|
| `papers` | 研究成果身份（规范化标题/DOI/年份/venue）；身份去重的锚点 |
| `versions` | 同一 work 的多版本关系（preprint ↔ 正式版），版本去重不误删合法预印本 |
| `observations` | 各来源看到的原始记录（保留原始 JSON 作证据） |
| `search_jobs` | 检索任务（领域×topic×年份×来源），带游标与 attempts |
| `download_queue` / `download_status` | 候选 URL、状态机（pending/downloading/downloaded/failed/no_oa_url/excluded_relevance）、已拒绝 URL 保护 |
| `decisions` | 规则引擎判定：label、score、证据 JSON、分类器版本 |
| `status_history` | 互斥主状态变迁：accepted_for_corpus / duplicate_version / excluded / needs_review / metadata_only |
| `api_budget` | 按 UTC 日的每来源请求计数（缓存命中也保守计入） |

**互斥主状态规则**：每条已下载记录有且只有一个主状态；accepted 需同时满足"有效全文 + 相关性达标 + 版本非重复"；metadata_only 是合法终态（无 OA 全文但元数据有价值就保存元数据）。

### 2.4 采集管线（七个阶段）

```
Discovery → Metadata → Relevance → Resolution → Download → Validation → Corpus
```

1. **Discovery/检索**：按领域×topic×年份独立查询，来源见附录 B；每年一个游标，断点续查。
2. **Metadata**：规范化（标题、DOI、日期角色、venue），保留原始响应；字段冲突（如多来源年份不一）留存证据不强行裁决。
3. **Relevance（规则引擎，无模型）**：对 title+abstract+keywords 命中配置词表：
   - 命中负面词且无例外 → `irrelevant`
   - 命中边界词 → `uncertain`（"相邻应用/教育场景"类）
   - 标题核心词 + 上下文词 → `relevant` (0.94)
   - ≥2 个不同核心词 + 上下文 + topic 命中 → `probably_relevant` (0.8)
   - 仅弱命中 → `uncertain` (0.5)；什么都没有 → `irrelevant` (0.02)
   - **入库门槛 = label ∈ {relevant, probably_relevant} 且 score ≥ 0.85**。铁律：泛词（screening/model/network/analysis 类）永远不单独构成核心证据。
4. **Resolution（OA 解析）**：DOI → Unpaywall/OpenAlex/Aurope PMC/出版社 OA 页面找合法全文链接；Crossref 链接本身**不**证明可获取，必须有 CC 许可或其他 OA 证据。
5. **Download**：并发下载（默认 6），单请求 ≤75 MiB，主机级限流与冷却；403/429 冷却换源，不重置已拒绝 URL。
6. **Validation**：PDF 头/尾/页结构/首末页抽样文本/标题或 DOI 匹配；JATS/XML 校验 license 证据；失败即失败，不伪造成功。
7. **Corpus 入库**：哈希命名入领域文件夹 + index.csv；身份与版本去重；更新统计。

### 2.5 语义复核（可选，默认关）

- 只用于**规则判为 uncertain 的边界样本**，不给全量分类。
- 只允许本机 Ollama（`127.0.0.1:11434`，模型如 `qwen3:8b`），**外部模型 API 在代码层面直接拒绝**。
- 结果带模型身份（digest）+提示词版本缓存；服务不可用→样本留队列，**不缓存失败结论**。

### 2.6 网络与资源治理

- **断网恢复**：失败重试退避 10→300 秒，不因连续失败永久放弃；所有主机失联 → 暂停新网络任务、每 30 秒探测公共页面、恢复后唤醒队列；恢复次数持久化。
- **调度**：planner 每轮最多新增 96 任务；活跃 frontier ≤384；PDF 候选窗口 ≤600（70% 给健康主机可重试链接，30% 留 OA 解析）；high/medium/exploration 队列约 80/15/5。
- **磁盘安全**：剩余 <10 GiB 暂停新 PDF（metadata 最多再收 2000 条）；<1 GiB 整体安全停止。
- **容量阶段**：`target_fulltext_bytes`（默认 120 GB，含已有 PDF+JATS）达标 → 自动停止并保存续采清单（状态 `stopped_capacity_target`）；**恢复前必须调大目标或关闭容量阶段，否则会立即再次停止**。

### 2.7 观测与审计

- `stats.json`（每 ~2 分钟）：accepted、各领域下载/排除/待审、语言分布、失败原因直方图、容量进度。
- `run_state.json`：PID、status、速率、断网状态——**判断采集器是否活着的唯一真相源**。
- 审计：每新增 2000 accepted 或每 6 小时 → `CORPUS_AUDIT_<UTC>.md` + 下一批计划 CSV。
- 进度查看脚本 `watch_progress.ps1`：只读，不启停采集。

---

## 3. 红线与运维

### 3.1 合法与安全红线（违反即停止）

1. 只用合法 OA 来源与开放接口；**禁止影子库**（sci-hub 等），URL 层面拉黑。
2. 不绕过任何付费墙或访问控制；无真实邮箱则跳过 Unpaywall，不编造。
3. 语义模型仅限本机端点；密钥/邮箱只放 `.env`，任何输出不打印 secret。
4. 不修改用户网卡/防火墙/系统代理。

### 3.2 启动 / 停止 / 恢复

```powershell
# 启动（长期运行）
.\acquisition\run_balanced.ps1 -EnableAcquisition -Minutes 0
# 安全停止
New-Item -ItemType File -Path .\acquisition\STOP -Force
# 等 collector.lock 自动消失后，重跑同一条启动命令即从断点继续
```

- **绝不同时启动第二个采集器**；锁 + PID + 命令行三重核验。
- 异常残留锁：先用 `Get-CimInstance` 核对 PID 已退出才清理；**不要用旧备份覆盖现库来续采**。
- 后台进程随关机/休眠中断，恢复后重跑启动命令即可，无开机自启。

### 3.3 分阶段启动（不可跳步）

| 阶段 | 做什么 | 通过标准 |
|------|--------|----------|
| 1. plan-only | 生成配置，只出规划不联网下载 | 配置合法、领域/词表非空 |
| 2. smoke | 限时限量的真实小批量（如 15 分钟/60 PDF 上限） | 新有效 PDF ≥50；**每个配置领域 ≥5 个新 accepted**；accepted 占比 ≥85%；旧文件哈希不变；SQLite integrity ok；断点已保存 |
| 3. 受控增量 | 按计划 CSV 跑批次 | 每轮结束跑审计脚本核对来源/venue/年度，不只看下载数 |
| 4. 长期运行 | `Minutes 0` + 容量目标 | 每轮自动审计 + 每 6 小时审计报告 |

smoke 未通过 → 修复后重跑 smoke，**不得**直接扩大批次。

### 3.4 回归测试

交付前实现并在每次改动后运行 `python -m unittest discover -s acquisition -p "test_*.py"`。最低覆盖：身份去重、版本关系、规则分类各档、PDF 校验拒绝路径、断网恢复（模拟连续失败后成功）、容量停止、STOP/恢复、游标保持。原项目为 106 项，新实现至少覆盖上述场景。

---

## 附录 A：配置 JSON 骨架（问卷回答 → 此结构）

```jsonc
{
  "acceptance": { "labels": ["relevant", "probably_relevant"], "minimum_score": 0.85 },
  "storage_stage": { "enabled": true, "target_fulltext_bytes": 120000000000 },
  "semantic": {                       // 可选，默认整块关闭
    "enabled": false, "optional": true, "model": "qwen3:8b",
    "endpoint": "http://127.0.0.1:11434/api/chat", "temperature": 0
  },
  "domains": [                        // Q1/Q2 生成；关键词-only 时为一个临时领域
    {
      "domain_name": "<用户领域或关键词组>",
      "enabled": true,
      "positive_terms": ["..."],      // 代理扩展
      "context_terms": ["..."],
      "negative_terms": ["..."],
      "boundary_terms": ["..."],
      "target_years": [2017, 2026],   // Q5 默认：最近10年
      "year_weights": { "<近2-3年>": 3.0, "<再往前1年>": 2.8 },
      "preferred_venues": [],         // Q4，空=全部期刊
      "topics": [
        {
          "topic_id": "t1", "topic_name": "...",
          "queries": { "en": ["..."], "zh": ["..."] },   // Q3=双语时生成 zh 行
          "positive_terms": ["..."], "negative_terms": ["..."],
          "target_count": 0, "priority": "high"
        }
      ]
    }
  ]
}
```

## 附录 B：合法来源清单（默认启用）

| 来源 | 用途 | 备注 |
|------|------|------|
| arXiv | 预印本元数据+PDF | Atom API |
| Crossref | 正式元数据、DOI、中文查询 | 公共 REST，礼貌池邮箱可配 |
| OpenAlex | 元数据、引用、OA 位置 | 邮箱可配 |
| Europe PMC | OA 全文（PDF/JATS）+ 许可证据 | JATS 需 license 校验 |
| DOAJ | 期刊 OA 文章；中文过滤 | 中文来源主力 |
| publisher_oa | 出版社开放页面上的 PDF | 只读已有 OA 证据的公开页 |
| Semantic Scholar | 元数据补充 | 有 API key 更好，无 key 可跑 |
| Unpaywall | OA 位置解析 | **无真实邮箱则整体跳过** |

## 附录 C：默认参数速查

| 参数 | 默认值 |
|------|--------|
| 相关性门槛 | 0.85（relevant=0.94 / probably_relevant=0.8 / uncertain=0.5 / probably_irrelevant=0.2 / irrelevant=0.02） |
| 语言 | 英文（双语为可选） |
| 期刊 | 全部可检索 |
| 时间 | 最近 10 年，近 2–3 年加权 |
| 并发 | 6 下载 / 2 OA 解析；单请求 ≤75 MiB |
| 调度 | 每轮 ≤96 新任务；frontier ≤384；PDF 候选 ≤600 |
| 重试退避 | 10–300 秒；全网失联每 30 秒探测 |
| 审计 | 每 2000 accepted 或 6 小时；统计每 2 分钟 |
| 磁盘 | <10 GiB 暂停 PDF；<1 GiB 全停 |
| 容量目标 | 120 GB 全文原文件（可调/可关） |
| 语义复核 | 关闭；仅本机 Ollama；仅边界样本 |

---

*本指南由对 `D:\papers` 现存系统的逆向整理生成（2026-10-09）；数值默认值取自当前运行实例，属可调超参数而非硬约束。*
