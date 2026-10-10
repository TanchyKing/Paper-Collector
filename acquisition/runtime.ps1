$ErrorActionPreference = 'Stop'
function Get-CorpusPython {
    $projectRoot = (Resolve-Path -LiteralPath (Join-Path $PSScriptRoot '..')).Path
    if ($env:CORPUS_PYTHON) {
        if (-not (Test-Path -LiteralPath $env:CORPUS_PYTHON)) { throw 'CORPUS_PYTHON does not exist.' }
        return $env:CORPUS_PYTHON
    }
    $localPython = Join-Path $projectRoot '.venv\Scripts\python.exe'
    if (Test-Path -LiteralPath $localPython) { return $localPython }
    throw 'Set CORPUS_PYTHON to the restored Paper Pilot environment, or create the project .venv.'
}
