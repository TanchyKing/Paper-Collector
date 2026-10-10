"""Preserve legal Europe PMC JATS using the existing engine's HTTP and rate limits."""
import hashlib,json,xml.etree.ElementTree as ET,os,uuid,shutil
from pathlib import Path
from corpus_store import dump,now,atomic_json
from literature_audit import similarity

def verify_jats(content,p):
    root=ET.fromstring(content)
    if root.tag.split('}')[-1]!='article':raise ValueError('not_jats_article')
    title=' '.join(root.findtext('.//article-title','').split())
    if not title:
        node=root.find('.//article-title');title=' '.join(node.itertext()) if node is not None else ''
    ids={n.get('pub-id-type'):''.join(n.itertext()).strip() for n in root.findall('.//article-id')}
    if similarity(title,p.get('title'))<.8:raise ValueError('jats_title_identity_mismatch')
    known={str(p.get(k) or '').lower() for k in ['doi','formal_doi']} - {''}
    if known and ids.get('doi') and ids['doi'].lower() not in known:raise ValueError('jats_doi_mismatch')
    return root,title,ids

async def preserve_jats(engine,client,pid,p,reserved_host=None):
    from download_engine import pmcids
    if not p.get('is_open_access'):return None
    ids=pmcids(p)
    if not ids:return None
    folder=engine.state/'fulltext_xml';folder.mkdir(exist_ok=True);target=folder/(pid+'.xml');sidecar=folder/(pid+'.json')
    if target.exists() and sidecar.exists():
        try:
            record=json.loads(sidecar.read_text(encoding='utf8'))
            if hashlib.sha256(target.read_bytes()).hexdigest()==record['sha256']:return record
        except (ValueError,KeyError):pass
    url='https://www.ebi.ac.uk/europepmc/webservices/rest/'+ids[0]+'/fullTextXML'
    if reserved_host=='www.ebi.ac.uk':return None
    try:
        content=await engine.api(client,pid,'europe_pmc',url,text=True)
        root,title,identifiers=verify_jats(content,p)
        payload=content.encode('utf8');record={'paper_id':pid,'path':str(target.relative_to(engine.root)),'sha256':hashlib.sha256(payload).hexdigest(),'bytes':len(payload),'source':url,'source_type':'Europe PMC OA JATS','retrieved_at':now(),'identifiers':identifiers,'permissions_xml':ET.tostring(root.find('.//permissions'),encoding='unicode') if root.find('.//permissions') is not None else None,'identity_check':'title similarity >=0.8 and DOI agreement when supplied'}
        if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest()!=record['sha256']:
            archive=folder/'quarantine';archive.mkdir(exist_ok=True);shutil.copy2(target,archive/(pid+'-'+uuid.uuid4().hex+'.xml'))
        temporary=target.with_name(target.name+'.'+uuid.uuid4().hex+'.part')
        with temporary.open('xb') as f:f.write(payload);f.flush();os.fsync(f.fileno())
        os.replace(temporary,target);atomic_json(sidecar,record)
        # Derived section-aware text; keep original JATS and license evidence separately.
        sections=[]
        for index,section in enumerate(root.findall('.//body//sec')):
            title=section.findtext('title','');paragraphs=[' '.join(n.itertext()).strip() for n in section.findall('./p')]
            if paragraphs:sections.append({'section_id':section.get('id') or str(index),'section_title':title,'text':'\n\n'.join(paragraphs),'source':record['path'],'parser':'stdlib_jats_sections_v1'})
        atomic_json(target.with_suffix('.sections.json'),sections)
        if getattr(engine,'cfg',{}).get('language_policy',{}).get('enabled'):
            from corpus_languages import record as language_record
            language_record(engine.db,engine.root,engine.cfg,pid,p,target,kind='xml')
        engine.lib.event('jats_preserved',**record);return record
    except Exception as error:
        engine.lib.event('jats_unavailable',paper_id=pid,source=url,error=type(error).__name__);return None
