"""Conservative venue registry: journal ISSN aliases, conference event names."""
import collections,hashlib,re
from corpus_store import dump,now,put_metadata
from literature_audit import venue_norm
def normalize_venues(db,metadata,run_id):
    parent={};bykey={};counts=collections.Counter()
    def find(x):
        parent.setdefault(x,x)
        if parent[x]!=x:parent[x]=find(parent[x])
        return parent[x]
    def join(a,b):parent[find(a)]=find(b)
    for pid,n in metadata.items():
        if not n.get('formal_venue'):continue
        kind=n.get('formal_publication_type') or 'unknown';name=venue_norm(n['formal_venue']);find(pid)
        # Proceedings series ISSNs cannot establish a particular conference identity.
        keys=[kind+':name:'+name]
        if kind=='journal':keys += ['journal:issn:'+s.upper() for s in n.get('issn',[]) if re.fullmatch(r'\d{4}-\d{3}[\dXx]',s)]
        for key in keys:
            if key in bykey:join(pid,bykey[key])
            else:bykey[key]=pid
        counts[(kind,n['formal_venue'])]+=1
    groups=collections.defaultdict(list)
    for pid in parent:groups[find(pid)].append(pid)
    for ids in groups.values():
        kind=metadata[ids[0]].get('formal_publication_type');names={metadata[i]['formal_venue'] for i in ids};issns=sorted({s for i in ids for s in metadata[i].get('issn',[])})
        canonical=max(names,key=lambda name:(counts[(kind,name)],len(name),name));key='venue:'+hashlib.sha256((str(kind)+':'+(issns[0] if kind=='journal' and issns else venue_norm(canonical))).encode()).hexdigest()[:24]
        publishers=sorted({metadata[i]['publisher'] for i in ids if metadata[i].get('publisher')});evidence={'method':'journal: verified shared ISSN or normalized name; conference: normalized event/track name only','original_names':sorted(names),'issn_evidence':issns,'member_paper_ids':ids}
        db.execute('INSERT INTO corpus_v2_venues VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(venue_id) DO UPDATE SET canonical_name=excluded.canonical_name,issns=excluded.issns,aliases=excluded.aliases,publishers=excluded.publishers,evidence=excluded.evidence,updated=excluded.updated',(key,canonical,kind,dump(issns),dump(sorted(names)),dump(publishers),dump(evidence),now()))
        for pid in ids:
            n=metadata[pid];n['canonical_formal_venue']=canonical;n['formal_venue_id']=key;n['field_provenance']['canonical_formal_venue']={'source':'corpus_v2_venues','evidence':evidence['method'],'confidence':.96};put_metadata(db,pid,n,run_id)
    return len(groups)
