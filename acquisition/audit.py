from project_paths import resolve_project_path, pointer_path
"""Read-only audit of stored PDFs and current collection state."""
import json, sqlite3, hashlib
from collections import Counter
from pathlib import Path
import pymupdf as fitz
root=Path(__file__).resolve().parent.parent
db=sqlite3.connect(root/'acquisition'/'state.sqlite3')
failures=[]; titles=[]; sizes=Counter(); types=Counter(); year_counts=Counter(); total=0
for pid,data,status,path,sha,size,pages in db.execute('select id,data,status,path,sha256,bytes,pages from papers'):
    p=json.loads(data)
    types[p.get('document_type') or 'unknown']+=1
    year_counts[str(p.get('year') or 'unknown')]+=1
    if status!='downloaded': continue
    try:
        f=resolve_project_path(path); content=f.read_bytes()
        assert hashlib.sha256(content).hexdigest()==sha, 'hash mismatch'
        assert len(content)==size, 'size mismatch'
        assert content.startswith(b'%PDF-') and b'%%EOF' in content[-4096:], 'header or EOF missing'
        with fitz.open(f) as pdf:
            assert len(pdf)==pages and not pdf.is_repaired and not pdf.is_encrypted, 'parser mismatch'
            for i in range(len(pdf)): pdf.load_page(i).get_contents()
        sizes[str(f.parent)]+=size; total+=1
        titles.append(dict(id=pid,title=p['title'],domains=p['domains'],year=p.get('year'),pages=pages,bytes=size,path=path))
    except Exception as e: failures.append(dict(id=pid,path=path,error=str(e)))
result=dict(verified_downloaded_records=total,errors=failures,physical_directory_bytes=sizes,type_counts=types,year_counts=year_counts,downloaded_papers=titles)
out=root/'acquisition'/'audit.json'; out.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(dict(verified=total,errors=failures,physical_directory_bytes=sizes,types=types,downloaded_titles=titles[-12:]),ensure_ascii=False))
