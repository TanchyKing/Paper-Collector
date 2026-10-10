# 学术论文语料库自动采集系统

A long-running, policy-first pipeline that builds a domain-organized open-access paper corpus from public APIs — with rule-based relevance gating, resumable state, and full audit trails.

> **本仓库不包含任何论文全文。** 语料（PDF/JATS）由使用者按本仓库代码自行采集到本地；OA（开放获取）不等于可再分发，请勿把采集到的全文上传到任何公开位置。

## 这是什么

一套长期无人值守的论文采集流水线：从合法开放获取来源检索元数据 → 规则引擎做相关性判定 → 解析合法全文链接 → 下载并校验 PDF/JATS → 按领域文件夹归档 → 周期审计与断点续采。全部状态持久化在单个 SQLite 库中，断网、关机、断电后均可无损恢复。

核心设计原则：

- **不伪造成功**——下载失败就是失败；检索返回零篇不算覆盖；XML 失败不能记成全文成功。
- **不确定就挂起**——规则判不准的进 needs_review 队列，不降门槛自动接受。
- **一切可审计**——每阶段有验收证据与备份指针，原始文件永不改写。
- **对来源友善**——限流、退避、每日 API 预算；绝不绕过付费墙或访问控制。

## 合规与数据来源

- 来源全部为公共开放接口：arXiv、Crossref、OpenAlex、Europe PMC、DOAJ、Semantic Scholar，以及**仅限已有 OA 证据的公开页面**上的出版社 PDF 链接（`citation_pdf_url` 等标准 meta 标签）。
- **拒绝影子库**：sci-hub / libgen / annas-archive / z-lib 等在代码与 URL 层双重拉黑。
- **不绕过任何付费墙**；未配置真实联系邮箱时整体跳过 Unpaywall，不编造邮箱。
- 诚实的 User-Agent 标识；请求退避 10–300 秒；各来源按 UTC 日计数 API 预算。
- 相关性判入库门槛 0.85，规则引擎可完全离线工作；本地语义复核（Ollama，仅限 127.0.0.1）为可选开关，默认关闭。

## 快速开始

环境要求：Python 3.12+（Windows 优先，PowerShell；Linux/macOS 需自行替换硬链接实现）。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install httpx beautifulsoup4 lxml feedparser PyMuPDF bibtexparser jsonschema python-dotenv pydantic
```

首次运行流程（不可跳步）：

1. **回答启动问卷**——领域 / 关键词（至少其一，否则不开启下载）、语言（默认英文）、期刊（默认全部）、时间范围（默认最近 10 年），由 AI 代理据此生成配置；
2. **plan-only**——只生成规划，不联网下载；
3. **smoke 小批量验收**——新有效 PDF ≥50、每个配置领域 ≥5 篇 accepted、accepted 占比 ≥85%、旧文件哈希不变、SQLite 完整；
4. **长期运行**——`-Minutes 0` 无时间限制，按容量目标（默认 120 GB，可调）自动停止并保存续采清单。

```powershell
# 长期运行 / 安全停止 / 断点恢复
.\acquisition\run_balanced.ps1 -EnableAcquisition -Minutes 0
New-Item -ItemType File -Path .\acquisition\STOP -Force   # 安全停止
.\acquisition\run_balanced.ps1 -EnableAcquisition -Minutes 0   # 锁释放后原样恢复
```

完整的复刻规格、启动问卷定义、数据模型、默认参数速查见 **[REPLICATION_GUIDE.md](REPLICATION_GUIDE.md)**。

## 平台说明

- 下载并发默认 6、单请求 ≤75 MiB；磁盘余量 <10 GiB 自动暂停新 PDF，<1 GiB 整体安全停止。
- 双语模式下按正文实际语言归档（`corpus_by_language/`），历史文件经 NTFS 硬链接挂载、不占双倍空间；其他平台请改用符号链接或副本。
- 后台进程随关机/休眠中断，恢复后重跑同一条启动命令即可；未配置开机自启。

## 致谢与许可

本项目基于 [Paper Pilot](https://github.com/aytzey/paper-pilot)（MIT License，Copyright © 2026 aytzey，衍生自 commit `6aab58e`）适配扩展而成，感谢原作者。本项目同样以 MIT 许可发布，见 [LICENSE](LICENSE)。

## 免责声明

本项目仅自动化访问公开、合法的开放获取资源，不提供、不存储、不分发任何受版权保护的论文全文。使用者需自行遵守所在司法辖区的法律及各数据来源的服务条款，并对基于本项目的采集行为与语料用途负责。本项目不与上述任何来源或 Paper Pilot 原作者存在隶属关系。
