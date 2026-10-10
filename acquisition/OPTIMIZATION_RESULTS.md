# 下载系统优化与实际运行验证

快照时间（北京时间）：2026-10-06T14:08:17.532855+08:00

实际运行 8.43 分钟，新增独立 PDF 280 份，速度 33.23 篇/分钟。旧进程观测速度 8.40 篇/分钟，本轮比值 3.96。两次处理的论文和网络状态不同，不能保证后续始终保持这个比值。

## 修改与验证

复用原有目录、数据库、已下载 PDF 与检索分页队列。6 个下载工作线程、2 个 OA 工作线程，主机独立限流与冷却；优先积压，达到低积压阈值后恢复四领域检索。403 等永久拒绝逐 URL 留档，不强行重试；DOI 落地页先查 OA，交叉查找 OpenAlex/Unpaywall/Europe PMC/arXiv/Semantic Scholar，并接入 PMC 当前免费公开数据桶。连接 6 秒、读取停滞 20 秒、单地址总期限 45 秒。全量导出改为 120 秒或新增 100 次成功下载，逐条状态始终写入 SQLite。

五项初始针对性测试通过，包括重复领取、403 换源、冷却持久化、标识归一化、已有文件保护；另见新增调度测试。完整 PDF 结构检查与旧文件哈希保护结果见 audit.json 和 preservation-check.json。

## 各来源结果

分母为本轮已经完成的 URL 下载尝试，包含重定向；不是文献检索来源的收录率。

|来源|成功/尝试|成功率|平均下载尝试耗时（秒）|
|---|---:|---:|---:|
|Frontiers|82/82|100.0%|7.99|
|Nature|61/72|84.7%|11.48|
|arXiv|52/53|98.1%|6.80|
|Springer|35/40|87.5%|13.67|
|PMC Open Data|21/22|95.5%|8.35|
|NCBI PMC|0/12|0.0%|1.93|
|ACL Anthology|6/6|100.0%|2.97|
|MDPI|1/5|20.0%|3.05|
|www.epj-conferences.org|0/4|0.0%|2.86|
|www.researchsquare.com|4/4|100.0%|8.57|
|imapsource.org|2/4|50.0%|3.95|
|Europe PMC|0/3|0.0%|0.96|
|Wiley|0/3|0.0%|0.94|
|pubs.rsc.org|0/2|0.0%|1.45|
|www.cell.com|0/2|0.0%|1.95|
|www.osti.gov|2/2|100.0%|14.02|
|www.sciencedirect.com|0/2|0.0%|3.09|
|imapsjmep.org|2/2|100.0%|7.52|
|PLOS|1/1|100.0%|2.92|
|biblio.ugent.be|1/1|100.0%|4.88|
|dergipark.org.tr|1/1|100.0%|3.34|
|enpuir.npu.edu.ua|0/1|0.0%|10.52|
|essd.copernicus.org|1/1|100.0%|3.76|
|hdl.handle.net|0/1|0.0%|4.16|
|ieeexplore.ieee.org|1/1|100.0%|12.45|
|infoscience.epfl.ch|1/1|100.0%|6.30|
|iopscience.iop.org|0/1|0.0%|2.38|
|journal-spqeo.org.ua|1/1|100.0%|3.78|
|open.library.ubc.ca|0/1|0.0%|1.75|
|opg.optica.org|0/1|0.0%|2.33|
|ppam.semnan.ac.ir|1/1|100.0%|5.67|
|tns.ewapub.com|1/1|100.0%|3.08|
|translational-medicine.biomedcentral.com|1/1|100.0%|9.36|
|www.degruyter.com|0/1|0.0%|4.17|
|www.jstage.jst.go.jp|1/1|100.0%|3.91|
|www.matec-conferences.org|0/1|0.0%|5.06|
|www.oejournal.org|0/1|0.0%|0.47|
|www.spiedigitallibrary.org|0/1|0.0%|2.39|
|cds.cern.ch|0/1|0.0%|2.97|
|eprints.gla.ac.uk|1/1|100.0%|9.74|

## OA 补查与领域分布

新增地址：{"openalex": 12, "europe_pmc": 6, "unpaywall": 2, "arxiv": 4, "pmc_cloud": 95}

实际下载地址来源：{"existing_metadata": 252, "arxiv": 4, "openalex": 2, "pmc_cloud": 21, "unpaywall": 1}

各领域新增论文（标签可重叠）：{"半导体与集成电路": 53, "新材料": 72, "软件与通信": 80, "生物制药": 77}

## 仍有的限制

arXiv 每主机间隔 4 秒、并发 1，因此理论起始请求上限 15 次/分钟。出版商可能返回 403、HTML 验证页、无效 DOI 落地页；这些不能通过增加并发解决。部分 PDF 大、跨境连接慢或站点响应慢。OA 元数据可缺少 PDF，PMC 有文章无 PDF 的情况，Semantic Scholar 等仍可能返回 429。有效性检查会拒绝可疑单页、扫描件、截断或标题不匹配内容。

## 继续运行

当前状态见 run_state.json；后台以无总数量上限的模式运行，优先积压并自动恢复分页检索。停止时创建 acquisition/STOP；进程完全退出后用 run.ps1 -Minutes 0 续传。不要并行启动多个采集器。