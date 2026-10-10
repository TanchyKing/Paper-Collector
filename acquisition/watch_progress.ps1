param([switch]$Once,[ValidateRange(5,3600)][int]$IntervalSeconds=15)
$ErrorActionPreference='Stop'
. (Join-Path $PSScriptRoot 'runtime.ps1')
$progressPython=Get-CorpusPython
$env:PYTHONUTF8='1'
do {
  try {
    $progressData=& $progressPython (Join-Path $PSScriptRoot 'progress_report.py') | ConvertFrom-Json
    if($LASTEXITCODE -ne 0){throw '进度快照读取失败'}
    $progressWorker=$null
    if($progressData.pid){$progressWorker=Get-CimInstance Win32_Process -Filter "ProcessId=$($progressData.pid)" -ErrorAction SilentlyContinue}
    $progressAlive=[bool]($progressWorker -and $progressWorker.CommandLine -match 'collect\.py')
    if(-not $Once){Clear-Host}
    Write-Host ('检查时间：{0}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'))
    Write-Host ('进程存活：{0} | 记录状态：{1} | PID：{2} | 状态更新距今：{3} 秒' -f $progressAlive,$progressData.recorded_status,$progressData.pid,$progressData.state_age_seconds)
    if($null -ne $progressData.target_GB){
      Write-Host ('全文：{0:N3} / {1:N0} GB（{2:N2}%），剩余约 {3:N3} GB' -f $progressData.fulltext_GB,$progressData.target_GB,$progressData.progress_percent,$progressData.remaining_GB)
    }else{Write-Host ('全文：{0:N3} GB，容量目标未启用' -f $progressData.fulltext_GB)}
    Write-Host ('PDF总数（包含全部历史文件）：{0} | 本次启动前：{1} | 本次新增：{2} | 在途请求：{3}' -f $progressData.pdf_count,$progressData.baseline_pdf_this_run,$progressData.new_pdf_this_run,$progressData.active_download_requests)
    Write-Host ('最近 5 分钟：{0:N2} 份/分钟（成功 {1} 份）' -f $progressData.windows.'5'.pdf_per_minute,$progressData.windows.'5'.successful_downloads)
    Write-Host ('最近15分钟：{0:N2} 份/分钟（成功 {1} 份）' -f $progressData.windows.'15'.pdf_per_minute,$progressData.windows.'15'.successful_downloads)
    Write-Host ('本次全程平均：{0:N2} 份/分钟' -f $progressData.run_average_pdf_per_minute)
    $progressNetworkNames=@{online='在线';retrying_network='连接波动，正在退避重试';waiting_for_network='等待网络恢复，自动检查';unknown='尚未确认';unrecorded='旧快照未记录'}
    Write-Host ('网络：{0} | 自动恢复次数：{1}' -f $progressNetworkNames[$progressData.network_status],$progressData.network_recoveries)
    if($progressData.next_probe_local){Write-Host ('下次连通性检查：{0}' -f $progressData.next_probe_local)}
    if($progressData.languages.pdf_by_language){Write-Host ('全文语言索引：中文 {0} | 英文 {1} | 混合 {2} | 待确认 {3}' -f $progressData.languages.pdf_by_language.zh,$progressData.languages.pdf_by_language.en,$progressData.languages.pdf_by_language.mixed,$progressData.languages.pdf_by_language.unknown)}
    Write-Host ('最后成功下载：{0}' -f $progressData.last_success_local)
    if(-not $progressAlive){Write-Host '采集进程已退出；请查看记录状态和检查点。'}
    if($progressAlive -and $progressData.windows.'15'.successful_downloads -eq 0){Write-Host '最近15分钟尚无成功PDF；进程仍在，不代表全文文件持续增长。'}
    if(-not $Once){Write-Host ('每 {0} 秒自动刷新；Ctrl+C 只关闭此查看窗口，不停止采集。' -f $IntervalSeconds)}
  }catch{Write-Host ('读取暂时失败：{0}；下次刷新重试。' -f $_.Exception.Message);if($Once){exit 1}}
  if(-not $Once){Start-Sleep -Seconds $IntervalSeconds}
}while(-not $Once)
