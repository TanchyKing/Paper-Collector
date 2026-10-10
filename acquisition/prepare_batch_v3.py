"""Derive a bounded batch config from the existing planner and actual accepted coverage."""
import argparse,copy,json,sqlite3
from corpus_store import ROOT,STATE,load_config,atomic_json
from corpus_planner import preview,coverage_counts

def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['smoke','incremental']);args=parser.parse_args()
    if (STATE/'collector.lock').exists():raise SystemExit('Prepare batch only after collector checkpoint.')
    cfg=copy.deepcopy(load_config());db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True);pool=preview(db,cfg,10368);counts=coverage_counts(db)[0]
    years=[2023,2024] if args.phase=='smoke' else [2023,2024,2019,2020,2021,2022,2025]
    selected_topics={}
    for d in cfg['domains']:
        names={t['name'] for t in d['topics']};selected=[]
        for job in pool:
            if job['domain']==d['domain_name'] and job['lo'] in [2023,2024] and job['topic'] in names and job['topic'] not in selected:selected.append(job['topic'])
        selected_topics[d['domain_name']]=[t['topic_id'] for t in d['topics'] if t['name'] in selected[:4]];d['target_years']=years
        if not selected_topics[d['domain_name']]:raise ValueError('No configured pending topics for '+d['domain_name'])
        cfg['sampling']['minimum_by_domain_year'][d['domain_name']]={str(y):counts[(d['domain_name'],y)]+(5 if args.phase=='smoke' else 10) for y in years}
    cfg['semantic']['enabled']=False;cfg['sampling']['providers']=['openalex','europe_pmc','crossref','doaj'];cfg['sampling']['multilingual_providers']=[]
    cfg['batch']={'phase':args.phase,'target_new_pdfs':60 if args.phase=='smoke' else 100,'years':years,'topics_by_domain':selected_topics,'incremental_requires_smoke_pass':True,'minimum_pdfs_per_domain_for_smoke':5,'formal_publication_metadata_preferred':True}
    path=STATE/(args.phase+'_v3.json');atomic_json(path,cfg);db.close();print(path)

if __name__=='__main__':main()
