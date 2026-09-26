$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$venvPython = Join-Path $Root ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
  python -m venv .venv
  & "$Root\.venv\Scripts\pip.exe" install -r requirements.txt
}
& $venvPython -m src.monitor check
