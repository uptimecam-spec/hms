# Network Device Monitor (Phase 1)

LAN device monitoring with Device Master, ping dashboard, and optional Telegram alerts.

## Open

- Ping dashboard: http://127.0.0.1:8099/
- **Camera status monitoring** (hourly ping, table + previews): http://127.0.0.1:8099/camera-status
- All cameras (NVR DATA inventory + health table): http://127.0.0.1:8099/cameras
- Live wall (RTSP snapshot grid + filters): http://127.0.0.1:8099/live
- Network Devices (Add / Edit / Deactivate / Test Connection): http://127.0.0.1:8099/devices
- Architecture: `docs\architecture.md`

## Phase 1 done

1.1 Architecture document + diagram  
1.2 Device Master SQLite (`data\devices.db`) with required fields  
1.3 Registration UI under **Network Devices**

Cameras are imported from `NVR DATA\*.csv` / `*.txt` on startup (401 unique IPs). Optional `cameras.yaml` seeds when the DB is empty.

## Run

```powershell
cd C:\dev\software\camera-uptime-monitor
.\scripts\run-monitor.ps1
```

Telegram still optional via `.env` (`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`).
