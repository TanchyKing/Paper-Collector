from project_paths import resolve_project_path, pointer_path
import hashlib,json,sqlite3
from pathlib import Path
root=Path(__file__).resolve().parent.parent
backup=max((root/'acquisition'/'backups').glob('*/state.sqlite3'),key=lambda p:p.stat().st_mtime)
old=sqlite3.connect(backup)
records=old.execute("select id,path,sha256,bytes from papers where status='downloaded'").fetchall(); old.close()
errors=[]
for pid,path,sha,size in records:
    f=resolve_project_path(path)
    if not f.is_file(): errors.append({'id':pid,'error':'missing','path':path});continue
    if f.stat().st_size!=size or hashlib.sha256(f.read_bytes()).hexdigest()!=sha: errors.append({'id':pid,'error':'changed','path':path})
result=dict(backup=str(backup),previous_downloaded_records=len(records),unchanged=len(records)-len(errors),errors=errors)
(root/'acquisition'/'preservation-check.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(result,ensure_ascii=False))
