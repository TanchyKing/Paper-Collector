param([int]$DownloadWorkers=6,[int]$ResolverWorkers=2,[switch]$Foreground)
$ErrorActionPreference='Stop'
$phaseRoot=(Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'collector.lock')){throw 'A collector is already running. Check the PID; do not start a second collector.'}
. (Join-Path $PSScriptRoot 'runtime.ps1')
$phasePython=Get-CorpusPython
$phaseStop=Join-Path $PSScriptRoot 'STOP'
if(Test-Path -LiteralPath $phaseStop){Remove-Item -LiteralPath $phaseStop}
$env:PYTHONUTF8='1'
$phaseArguments=@('-u',(Join-Path $PSScriptRoot 'collect.py'),'--phase1','--minutes','0','--download-workers',$DownloadWorkers,'--resolver-workers',$ResolverWorkers,'--backlog-first-minutes','2')
if($Foreground){& $phasePython @phaseArguments;exit $LASTEXITCODE}
$phaseLog=Join-Path $PSScriptRoot ('phase1-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
$phaseErr=$phaseLog.Replace('.log','.err.log')
$phaseArguments[1]='"'+$phaseArguments[1]+'"'
$phaseProcess=Start-Process -FilePath $phasePython -ArgumentList $phaseArguments -WorkingDirectory $phaseRoot -WindowStyle Hidden -RedirectStandardOutput $phaseLog -RedirectStandardError $phaseErr -PassThru
[PSCustomObject]@{LauncherPID=$phaseProcess.Id;Log=$phaseLog;ErrorLog=$phaseErr;State=(Join-Path $PSScriptRoot 'run_state.json')}|ConvertTo-Json
