"""Read-only progress snapshot for the local capacity collection stage."""
import datetime,json,pathlib,sqlite3,sys
from corpus_store import ROOT,STATE

def snapshot():
    state=json.loads((STATE/'run_state.json').read_text(encoding='utf8'));metrics=json.loads((STATE/'download_metrics.json').read_text(encoding='utf8'))
    timestamp=datetime.datetime.now(datetime.timezone.utc);db=sqlite3.connect((STATE/'state.sqlite3').as_uri()+'?mode=ro',uri=True,timeout=3)
    db.execute('BEGIN');windows={}
    for minutes in [5,15]:
        cutoff=(timestamp-datetime.timedelta(minutes=minutes)).isoformat();count,size=db.execute("SELECT count(*),coalesce(sum(bytes),0) FROM http_attempts WHERE run_id=? AND stage='download' AND outcome='success' AND created>=?",(metrics['run_id'],cutoff)).fetchone()
        windows[str(minutes)]=dict(successful_downloads=count,pdf_per_minute=count/minutes,MB_per_minute=size/1e6/minutes)
    last=db.execute("SELECT created FROM http_attempts WHERE run_id=? AND stage='download' AND outcome='success' ORDER BY id DESC LIMIT 1",(metrics['run_id'],)).fetchone()
    count,size=db.execute('SELECT count(distinct sha256),coalesce(sum(bytes),0) FROM files').fetchone();xml=sum(p.stat().st_size for p in (STATE/'fulltext_xml').rglob('*.xml') if p.is_file());cfg=json.loads((STATE/'corpus_config.json').read_text(encoding='utf8'));policy=cfg.get('storage_stage',{});target=policy.get('target_fulltext_bytes') if policy.get('enabled') else None
    from corpus_languages import summary
    languages=summary(db)
    network=state.get('network_recovery') or {};db.close();updated=datetime.datetime.fromisoformat(state['updated'])
    return dict(languages=languages,snapshot_utc=timestamp.isoformat(),recorded_status=state['status'],pid=state.get('pid'),state_age_seconds=round((timestamp-updated).total_seconds()),pdf_count=count,baseline_pdf_this_run=metrics['baseline_files'],fulltext_GB=(size+xml)/1e9,target_GB=target/1e9 if target else None,progress_percent=(size+xml)/target*100 if target else None,remaining_GB=max(0,target-size-xml)/1e9 if target else None,new_pdf_this_run=metrics['new_pdf_files'],run_average_pdf_per_minute=metrics['pdf_per_minute'],windows=windows,last_success_local=datetime.datetime.fromisoformat(last[0]).astimezone().strftime('%Y-%m-%d %H:%M:%S %z') if last else None,active_download_requests=metrics.get('active_download_requests',0),run_id=metrics['run_id'],network_status=network.get('status','unrecorded'),network_recoveries=network.get('recoveries',0),next_probe_local=datetime.datetime.fromtimestamp(network['next_probe']).astimezone().strftime('%H:%M:%S') if network.get('paused') and network.get('next_probe') else None)

if __name__=='__main__':print(json.dumps(snapshot(),ensure_ascii=False))
