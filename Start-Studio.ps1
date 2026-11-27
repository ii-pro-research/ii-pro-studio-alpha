param([int]$Port = 0, [switch]$NoBrowser)
$ErrorActionPreference = 'Stop'
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $python)) { throw 'Create the Alpha environment first: py -3.12 -m venv .venv' }
$studioArguments = @('--port', $Port, '--no-docker-probe')
if ($NoBrowser) { $studioArguments += '--no-browser' }
Push-Location -LiteralPath $PSScriptRoot
try { & $python -B -m ii_pro_ui @studioArguments; exit $LASTEXITCODE } finally { Pop-Location }
