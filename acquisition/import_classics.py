from project_paths import resolve_project_path, pointer_path
"""Import the supplementary classic-paper MCP returns; collector must be stopped."""
import json,time
from pathlib import Path
from collect import Library,ROOT,STATE,validate,publisher_urls,dump
if (STATE/'collector.lock').exists(): raise SystemExit('Stop the collector before importing supplemental MCP results.')
lib=Library()
try:
    for key in ['classic_bio','classic_software','classic_materials','classic_chips']:
        source=json.loads((STATE/'seeds'/f'{key}.json').read_text(encoding='utf-8'))
        jid='mcp_'+key
        if not lib.db.execute('select 1 from observations where job_id=?',(jid,)).fetchone():
            for p in source['results']:
                p=dict(p)
                if 'Faculty Opinions recommendation' in p['title']: p['document_type']='peer-review'; p['type_evidence']='record title: recommendation'
                lib.ingest(p,source['domain'],source['topic'],jid)
            for status in source.get('source_status',[]):
                nr=status.get('next_request')
                if nr: lib.job(source['domain'],nr['topic'],nr['source'],nr['from_year'],nr['to_year'],nr['sort_by'],nr['cursor'],1,1600)
            lib.event('classic_mcp_search',domain=source['domain'],source_status=source.get('source_status'))
        download_files=[STATE/'seeds'/f'{key}_download.json']
        if key=='classic_software': download_files.append(STATE/'seeds'/'classic_software_author_download.json')
        for df in download_files:
            if not df.exists(): continue
            result=json.loads(df.read_text(encoding='utf-8'))
            if result.get('isError'):
                lib.event('classic_mcp_download_failed',domain=source['domain'],response=result)
                failed_doi={'classic_bio':'10.1038/nature13011','classic_software':'10.1145/359545.359563'}.get(key)
                p=next((x for x in source['results'] if x.get('doi')==failed_doi),None)
                if p and not lib.db.execute('select 1 from observations where job_id=?',(jid+'_download_failure',)).fetchone():
                    p=dict(p); p['document_type']='journal_article_unspecified'; p['type_evidence']='MCP publication venue; subtype unspecified'
                    pid=lib.ingest(p,source['domain'],'CRISPR RNA guided endonuclease Cas9' if key=='classic_bio' else source['topic'],jid+'_download_failure')
                    if lib.db.execute('select status from papers where id=?',(pid,)).fetchone()[0]!='downloaded':
                        lib.db.execute("update papers set status='failed',attempts=1,error=?,next_retry=? where id=?",('HTTP_403: MCP OA URL failed; see events for source response',time.time()+300,pid))
                continue
            dl=json.loads(next(x['text'] for x in result['content'] if x['type']=='text'))
            path=resolve_project_path(dl['pdf_path']); path.resolve().relative_to(ROOT.resolve())
            if not path.exists(): continue
            p=next((x for x in source['results'] if x.get('doi')==dl['paper'].get('doi')),None)
            if not p: continue
            p=dict(p); p['document_type']='journal_article_unspecified'; p['type_evidence']='MCP publication venue; subtype unspecified'
            pid=lib.ingest(p,source['domain'],'pilot:classic '+source['topic'],jid+'_download')
            info=validate(path.read_bytes(),p); dest=ROOT/source['domain']/f'{pid}.pdf'
            if not dest.exists(): path.rename(dest)
            else: dest=path
            lib.success(pid,dest,info)
        lib.db.commit()
    print(dump(lib.export()))
finally: lib.db.close()
