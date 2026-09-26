$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  Write-Host "Creating venv..."
  python -m venv .venv
  & "$Root\.venv\Scripts\pip.exe" install -r requirements.txt
}
# Prefer full stack: worker + web
& "$Root\scripts\run-all.ps1"
