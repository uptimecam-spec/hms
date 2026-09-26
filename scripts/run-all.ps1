$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
# Start standalone worker (hourly mandatory) then web dashboard — independent processes
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Root\scripts\run-worker.ps1`"" -WorkingDirectory $Root -WindowStyle Hidden
Start-Sleep -Seconds 1
Start-Process -FilePath "powershell.exe" -ArgumentList "-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File `"$Root\scripts\run-web.ps1`"" -WorkingDirectory $Root -WindowStyle Hidden
