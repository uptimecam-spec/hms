$ErrorActionPreference = "Stop"
Unregister-ScheduledTask -TaskName "CameraUptimeMonitor" -Confirm:$false -ErrorAction SilentlyContinue
$startup = [Environment]::GetFolderPath("Startup")
$cmd = Join-Path $startup "CameraUptimeMonitor.cmd"
if (Test-Path $cmd) { Remove-Item $cmd -Force; Write-Host "Removed Startup launcher: $cmd" }
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" | Where-Object { $_.CommandLine -match 'camera-uptime-monitor' } | ForEach-Object {
  Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue
  Write-Host "Stopped PID $($_.ProcessId)"
}
Write-Host "Autostart removed (task + Startup shortcut). Restart to confirm."
