$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$py = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) { python -m venv .venv; & "$Root\.venv\Scripts\pip.exe" install -r requirements.txt }
$env:EMBEDDED_WORKER = "0"
& $py -m src.monitor web
