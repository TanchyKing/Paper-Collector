param(
    [double]$Minutes = 60,
    [int]$PageSize = 50,
    [int]$DownloadWorkers = 6,
    [int]$ResolverWorkers = 2,
    [double]$BacklogFirstMinutes = 10,
    [switch]$RetryFailed,
    [switch]$ExportOnly
)
$ErrorActionPreference = 'Stop'
$collectorRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
$collectorLock = Join-Path $PSScriptRoot 'collector.lock'
if (Test-Path -LiteralPath $collectorLock) {
    throw "Collector lock exists (PID $(Get-Content -LiteralPath $collectorLock)). Check that process before restarting."
}
. (Join-Path $PSScriptRoot 'runtime.ps1')
$collectorPython=Get-CorpusPython
$collectorStop = Join-Path $PSScriptRoot 'STOP'
if (Test-Path -LiteralPath $collectorStop) { Remove-Item -LiteralPath $collectorStop }
$env:PYTHONUTF8 = '1'
$collectorArguments = @('-u', (Join-Path $PSScriptRoot 'collect.py'), '--minutes', $Minutes, '--page-size', $PageSize, '--download-workers', $DownloadWorkers, '--resolver-workers', $ResolverWorkers, '--backlog-first-minutes', $BacklogFirstMinutes)
if ($RetryFailed) { $collectorArguments += '--retry-failed' }
if ($ExportOnly) { $collectorArguments += '--export-only' }
$collectorLog = Join-Path $PSScriptRoot ('run-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
& $collectorPython @collectorArguments | Tee-Object -FilePath $collectorLog
if ($LASTEXITCODE -ne 0) { throw "Collector failed with exit code $LASTEXITCODE. See $collectorLog" }
