import argparse,pathlib,sqlite3,datetime
from corpus_store import STATE,migrate,rollback,UP,TABLES
def main():
    parser=argparse.ArgumentParser();parser.add_argument('action',choices=['up','down']);args=parser.parse_args()
    if (STATE/'collector.lock').exists():raise SystemExit('Collector lock exists. Stop the collector safely before migration.')
    if (STATE/'repair_v2/repair.lock').exists():raise SystemExit('Corpus repair is active; migration cannot run concurrently.')
    db=sqlite3.connect(STATE/'state.sqlite3');folder=STATE/'backups'/('migration-'+datetime.datetime.now().strftime('%Y%m%d-%H%M%S'));folder.mkdir(parents=True)
    backup=folder/'state.sqlite3';target=sqlite3.connect(backup);db.backup(target);target.close()
    (folder/'schema.sql').write_text('\n'.join(r[0]+';' for r in db.execute("select sql from sqlite_master where sql is not null")),encoding='utf-8')
    if args.action=='up':migrate(db,str(backup))
    else:rollback(db,str(folder/'derived-data-archive.sqlite3'))
    print(args.action,'integrity=',db.execute('pragma integrity_check').fetchone()[0],'backup=',backup);db.close()
if __name__=='__main__':main()
