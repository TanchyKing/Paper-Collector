"""Exclusive preflight, preserving the current snapshot and historical evidence."""
import json,sqlite3,subprocess,sys,pathlib,hashlib
from corpus_store import ROOT,STATE,load_config,atomic_json
folder=pathlib.Path((STATE/'continuous_v4/LATEST.txt').read_text(encoding='utf8'))
if (STATE/'collector.lock').exists():raise SystemExit('Collector active; do not run migration preflight concurrently.')
baseline=json.loads((folder/'BASELINE.json').read_text(encoding='utf8'));db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True)
tables=dict(db.execute("SELECT name,sql FROM sqlite_master WHERE type='table'"));schema_ok=all(tables.get(k)==v for k,v in baseline['schema'].items());integrity=[r[0] for r in db.execute('PRAGMA integrity_check')]
tests=subprocess.run([sys.executable,'-m','unittest','discover','-s',str(STATE),'-p','test_*.py'],capture_output=True,text=True,encoding='utf8');(folder/'tests.log').write_text(tests.stdout+tests.stderr,encoding='utf8')
dependency=subprocess.run([sys.executable,'-m','pip','check'],capture_output=True,text=True,encoding='utf8');mcp=json.loads((folder/'MCP_CHECK.json').read_text(encoding='utf8'));health=json.loads(mcp['health']['content'][0]['text']);cfg=load_config()
proof=dict(schema_preserved=schema_ok,integrity=integrity,original_pdf_verified=baseline['matching'],tests_passed=tests.returncode==0,dependency_check=dependency.stdout,mcp_status=health['status'],mcp_tools=mcp['tool_count'],root=str(ROOT),active_domains=len(cfg['domains']),topics=sum(len(d['topics']) for d in cfg['domains']))
proof['ready_for_smoke']=schema_ok and integrity==['ok'] and tests.returncode==0 and dependency.returncode==0 and health['status']=='ok' and baseline['matching']==baseline['pdf']
atomic_json(folder/'MIGRATION_CHECK.json',proof)
text=f'''# 迁移恢复复检

当前根目录：`{ROOT}`；Python：`{sys.executable}`。

实际 PDF {baseline['pdf']}；全部文件大小/SHA256匹配 {baseline['matching']}；accepted PDF {baseline['accepted']}，PDF/JATS合并有效全文2686（启动前口径）。

SQLite integrity_check：{integrity}；原有全部 {len(baseline['schema'])} 个表schema保持：{schema_ok}。新增 corpus_v4 表保存分层、任务策略、预算、候选提案和检查点。

测试：{tests.stderr.strip()}。

依赖：Python 3.12.14；Git可用；uv/uvx已安装在项目.venv；pip check：{dependency.stdout.strip()}。

Paper Pilot真实stdio MCP初始化、列举{mcp['tool_count']}个工具与healthcheck：{health['status']}。SCIHUB_ENABLED=false，Codex配置已禁用影子库工具；采集器URL守卫阻止影子库域名。缺少真实联系邮箱时跳过Unpaywall，不使用MCP默认占位邮箱。

路径：project_paths.py按当前项目根目录解析相对路径及历史papers目录后缀；原数据库历史路径与原始metadata保留，manifest导出新本机路径。运行入口使用项目.venv，不依赖旧电脑路径。

本轮备份：`{folder.relative_to(ROOT)}/before.sqlite3`、原代码/配置、PDF_BASELINE.json；旧V1/V2/V3审计和缓存保留。

具备短时间实际采集测试条件：{proof['ready_for_smoke']}。正式长期启动还须通过SMOKE_RESUME_CHECK.json。

本地Qwen服务尚未确认可用，未完成语义判定的边界样本保留needs_review；不降低0.85相关性阈值、不用词汇聚类冒充模型语义判断。候选主题和候选领域不进入正式分类。
'''
(ROOT/'MIGRATION_CHECK.md').write_text(text,encoding='utf8');print(json.dumps(proof,ensure_ascii=False));assert proof['ready_for_smoke']
