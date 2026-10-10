param([double]$Minutes=0,[switch]$EnableAcquisition,[switch]$Foreground,[int]$MaxNewPdfs=0,[double]$BacklogFirstMinutes=0)
$ErrorActionPreference='Stop'
$repairRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
. (Join-Path $PSScriptRoot 'runtime.ps1')
$repairPython=Get-CorpusPython
if(-not(Test-Path -LiteralPath $repairPython)){throw 'Configured Paper Pilot Python runtime unavailable.'}
if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'collector.lock')){throw 'Collector lock exists. Do not start a second collector.'}
if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'repair_v2\repair.lock')){throw 'Corpus repair is active. Finish it before acquisition.'}
if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'repair_v3\repair.lock')){throw 'V3 repair is active. Finish it before acquisition.'}
$env:PYTHONUTF8='1'
$repairArgs=@('-u',(Join-Path $PSScriptRoot 'collect.py'),'--corpus-mode','--minutes',$Minutes,'--download-workers','6','--resolver-workers','2','--backlog-first-minutes',$BacklogFirstMinutes,'--reserve-gb','10')
$repairConfiguration=Get-Content -LiteralPath (Join-Path $PSScriptRoot 'corpus_config.json') -Raw|ConvertFrom-Json
if($repairConfiguration.continuous.full_export_seconds){$repairArgs+=@('--export-seconds',[string]$repairConfiguration.continuous.full_export_seconds)}
if(-not $EnableAcquisition){$repairArgs+='--plan-only'; & $repairPython @repairArgs;exit $LASTEXITCODE}
if($MaxNewPdfs -gt 0){$repairArgs+=@('--max-new-pdfs',$MaxNewPdfs)}
$repairArgs+='--enable-acquisition'
$repairStop=Join-Path $PSScriptRoot 'STOP';if(Test-Path -LiteralPath $repairStop){Remove-Item -LiteralPath $repairStop}
if($Foreground){& $repairPython @repairArgs;exit $LASTEXITCODE}
$repairLog=Join-Path $PSScriptRoot ('balanced-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.log');$repairErr=$repairLog.Replace('.log','.err.log');$repairArgs[1]='"'+$repairArgs[1]+'"'
$repairProcess=Start-Process -FilePath $repairPython -ArgumentList $repairArgs -WorkingDirectory $repairRoot -WindowStyle Hidden -RedirectStandardOutput $repairLog -RedirectStandardError $repairErr -PassThru
$repairVerified=$false;$repairWorkerId=$null;$repairRunState=$null
for($repairAttempt=0;$repairAttempt -lt 90;$repairAttempt++){
  Start-Sleep -Seconds 1
  $repairLockPath=Join-Path $PSScriptRoot 'collector.lock';$repairStatePath=Join-Path $PSScriptRoot 'run_state.json'
  if(Test-Path -LiteralPath $repairLockPath){
    try{$repairWorkerId=[int](Get-Content -LiteralPath $repairLockPath -Raw);$repairWorker=Get-CimInstance Win32_Process -Filter "ProcessId=$repairWorkerId" -ErrorAction Stop
      if(Test-Path -LiteralPath $repairStatePath){$repairRunState=Get-Content -LiteralPath $repairStatePath -Raw|ConvertFrom-Json}
      if($repairWorker.CommandLine -match 'collect\.py' -and $repairRunState.pid -eq $repairWorkerId -and $repairRunState.status -eq 'running' -and $repairRunState.mode -eq 'corpus_balanced_annual'){$repairVerified=$true;break}
    }catch{}
  }
  if($repairProcess.HasExited -and -not $repairWorkerId){break}
}
[PSCustomObject]@{LauncherPID=$repairProcess.Id;WorkerPID=$repairWorkerId;RunningVerified=$repairVerified;Log=$repairLog;ErrorLog=$repairErr;Mode='balanced_annual';RunState=$repairRunState}|ConvertTo-Json -Depth 5
if(-not $repairVerified){Write-Warning 'Startup was not confirmed. Inspect the logs and collector.lock PID before trying again; do not start a second collector.';exit 1}
