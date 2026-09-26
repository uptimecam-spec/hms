Set WshShell = CreateObject("WScript.Shell")
WshShell.CurrentDirectory = "C:\dev\software\camera-uptime-monitor"
WshShell.Run "powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File ""C:\dev\software\camera-uptime-monitor\scripts\run-all.ps1""", 0, False
