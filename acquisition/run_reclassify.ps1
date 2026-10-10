param([ValidateSet('prepare','semantic','finalize','validate','all')][string]$Stage='all')
$ErrorActionPreference='Stop';$env:PYTHONUTF8='1'
if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'collector.lock')){throw 'Stop collector before changing derived corpus state.'}
if(Test-Path -LiteralPath (Join-Path $PSScriptRoot 'repair_v3\repair.lock')){throw 'Finish V3 repair before V2 reclassification.'}
. (Join-Path $PSScriptRoot 'runtime.ps1')
& (Get-CorpusPython) -u (Join-Path $PSScriptRoot 'corpus_repair.py') $Stage
exit $LASTEXITCODE
