"""Evidence-based document languages and zero-copy views of historical fulltext."""
import collections,hashlib,json,os,re,time,datetime
from pathlib import Path
import xml.etree.ElementTree as ET

VERSION='body-language-v1'
LABELS={'zh':'中文','en':'英文','mixed':'中英混合','other':'其他语言','unknown':'待确认'}
SCHEMA='''CREATE TABLE IF NOT EXISTS corpus_v6_job_language(job_id TEXT PRIMARY KEY,language TEXT,query TEXT,evidence TEXT,created TEXT);
CREATE TABLE IF NOT EXISTS corpus_v6_languages(paper_id TEXT,kind TEXT,language TEXT,metadata_language TEXT,path TEXT,view_path TEXT,sha256 TEXT,evidence TEXT,version TEXT,updated TEXT,PRIMARY KEY(paper_id,kind));
CREATE TABLE IF NOT EXISTS corpus_v6_language_history(evidence_id TEXT PRIMARY KEY,paper_id TEXT,kind TEXT,evidence TEXT,recorded TEXT);'''
def init(db):db.executescript(SCHEMA);db.commit()
def has_han(text):return bool(re.search(r'[\u4e00-\u9fff]',text or ''))
def metadata_language(p):
    raw=p.get('raw_source_evidence') or {};reported=raw.get('language') or raw.get('bibjson',{}).get('journal',{}).get('language') or p.get('language')
    if isinstance(reported,list):reported=','.join(reported)
    return {'reported':reported,'title_contains_han':has_han(p.get('title')),'basis':'metadata only; not fulltext language'}
def balanced_frontier(candidates,chinese_ids,ready,limit,fraction=.5,ready_fraction=.7):
    from corpus_continuous import download_frontier
    zh=[r for r in candidates if r[0] in chinese_ids];others=[r for r in candidates if r[0] not in chinese_ids]
    selected=download_frontier(zh,ready,min(len(zh),int(limit*fraction)),ready_fraction)
    selected.update(download_frontier(others,ready,limit-len(selected),ready_fraction))
    return selected
def detect_texts(texts,pages=None):
    measures=[]
    for text in texts:
        text=re.split(r'(?im)^\s*(?:references|bibliography|参考文献)\s*$',text)[0]
        han=len(re.findall(r'[\u4e00-\u9fff]',text));latin=len(re.findall(r'[A-Za-z]',text));kana=len(re.findall(r'[\u3040-\u30ff\uac00-\ud7af]',text));cyrillic=len(re.findall(r'[\u0400-\u04ff]',text))
        words=re.findall(r'[a-z]+',text.casefold());english=sum(w in {'the','and','of','to','in','a','is','that','for','with','as','we','this','are','was','by','from','on','be','were'} for w in words)
        measures.append(dict(han=han,latin=latin,other=kana+cyrillic,english_function_words=english,latin_word_count=len(words),han_fraction=han/max(1,han+latin),text_chars=len(text)))
    han=sum(m['han'] for m in measures);latin=sum(m['latin'] for m in measures);other=sum(m['other'] for m in measures);ratio=han/max(1,han+latin)
    if other>=80:lang='other'
    elif han+latin<150:lang='unknown'
    elif han>=100 and ratio>=.25:lang='zh'
    elif han<30 or ratio<.03:lang='en' if latin>=150 and sum(m['english_function_words'] for m in measures)>=8 else 'unknown'
    else:lang='mixed'
    return dict(language=lang,evidence={'method':VERSION,'basis':'sampled document body, excluding reference section when detected','sample_pages':pages,'counts':measures,'aggregate_han_fraction':ratio,'not_based_on_title_or_abstract_alone':True})
def detect_pdf(doc):
    # Skip bilingual title/abstract pages for ordinary research papers.
    indices=sorted(set([min(2,len(doc)-1),len(doc)//3,len(doc)//2,2*len(doc)//3,max(0,len(doc)-2)]))
    if len(doc)>=4:indices=[i for i in indices if i>=2]
    return detect_texts([doc[i].get_text()[:14000] for i in indices],[i+1 for i in indices])
def title_overlap(title,front):
    if not has_han(title):return None
    # Chinese words are not whitespace-delimited; tolerate PDF line wrapping.
    clean=lambda s:re.sub(r'[^\w]','',s or '').casefold()
    title=clean(title);front=clean(front)
    if title and title in front:return 1.0
    grams={title[i:i+4] for i in range(max(0,len(title)-3)) if has_han(title[i:i+4])}
    return sum(g in front for g in grams)/max(1,len(grams))
def directory(root,cfg,language,domain):
    policy=cfg.get('language_policy',{});folder=Path(root)/policy.get('storage_root','corpus_by_language')/LABELS.get(language,'待确认')/domain
    folder.mkdir(parents=True,exist_ok=True);return folder
def record(db,root,cfg,pid,p,path,kind='pdf',detected=None):
    if not cfg.get('language_policy',{}).get('enabled'):return None
    path=Path(path)
    if detected is None:
        if kind=='pdf':
            import pymupdf
            with pymupdf.open(path) as doc:detected=detect_pdf(doc)
        else:
            article=ET.fromstring(path.read_bytes());body=article.find('.//body');detected=detect_texts([' '.join((body if body is not None else article).itertext())])
    language=detected['language'];domain=(p.get('domains') or ['未分类'])[0];folder=directory(root,cfg,language,domain);view=folder/path.name
    sha=hashlib.sha256(path.read_bytes()).hexdigest();mode='primary_file'
    if path.resolve()!=view.resolve():
        if view.exists() and not os.path.samefile(path,view):view=folder/(pid+'_'+sha[:10]+path.suffix)
        if not view.exists():os.link(path,view)
        if not os.path.samefile(path,view):raise ValueError('language view must refer to the original file')
        mode='hardlink_no_extra_file_bytes'
    evidence=dict(detected['evidence'],metadata=metadata_language(p),storage_mode=mode)
    at=datetime.datetime.now(datetime.timezone.utc).isoformat();payload=json.dumps(evidence,ensure_ascii=False,sort_keys=True);key=hashlib.sha256(json.dumps([pid,kind,language,sha,payload],ensure_ascii=False).encode()).hexdigest()
    db.execute('INSERT OR IGNORE INTO corpus_v6_language_history VALUES(?,?,?,?,?)',(key,pid,kind,payload,at))
    db.execute('INSERT INTO corpus_v6_languages VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(paper_id,kind) DO UPDATE SET language=excluded.language,metadata_language=excluded.metadata_language,path=excluded.path,view_path=excluded.view_path,sha256=excluded.sha256,evidence=excluded.evidence,version=excluded.version,updated=excluded.updated',(pid,kind,language,json.dumps(metadata_language(p),ensure_ascii=False),str(path),str(view),sha,payload,VERSION,at));db.commit()
    return language
def summary(db):
    if not db.execute("SELECT 1 FROM sqlite_master WHERE name='corpus_v6_languages'").fetchone():return {}
    return {'pdf_by_language':dict(db.execute("SELECT language,count(*) FROM corpus_v6_languages WHERE kind='pdf' GROUP BY language")),'accepted_pdf_by_language':dict(db.execute("SELECT l.language,count(distinct s.article_identity) FROM corpus_v6_languages l JOIN corpus_v2_status s ON s.paper_id=l.paper_id WHERE l.kind='pdf' AND s.accepted_for_corpus=1 GROUP BY l.language")),'xml_by_language':dict(db.execute("SELECT language,count(*) FROM corpus_v6_languages WHERE kind='xml' GROUP BY language")),'basis':'original fulltext body; translated texts are not Chinese originals'}
def export(db,root):
    from corpus_store import csvwrite,atomic_json
    rows=[dict(zip(['paper_id','kind','language','metadata_language','original_path','language_path','sha256','evidence'],r)) for r in db.execute('SELECT paper_id,kind,language,metadata_language,path,view_path,sha256,evidence FROM corpus_v6_languages')]
    csvwrite(Path(root)/'CORPUS_LANGUAGE_INDEX.csv',rows);atomic_json(Path(root)/'CORPUS_LANGUAGE_SUMMARY.json',summary(db))
