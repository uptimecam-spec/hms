---
name: no-nvr-image-login
description: >-
  Enforces camera-direct image capture: never log into an NVR to fetch
  snapshots, MJPEG, or RTSP previews. Use when changing streaming, previews,
  NVR credentials, health captures, live frames, or camera image pipelines.
---

# No NVR login for images

In development and production for this project, **we must not use NVR as a login service to send us images**.

## Hard rules

1. Pull stills and live frames from the **camera IP** (`device.ipAddress`) only.
2. Do **not** call snapshot, MJPEG, or RTSP against `nvrHost` / NVR channel URLs for pictures.
3. Do **not** use `NVR_PASSWORD` / `NVR_LOGIN_ATTEMPTS` to authenticate to an NVR for image delivery.
4. NVR DATA exports and `nvrId` / `nvrHost` may still group inventory. That is not an image path.
5. Custom `rtspUrl` on a device is allowed only when it points at the camera (or an explicit camera stream), not an NVR proxy channel.

## Allowed

- ICMP / ping for uptime (no NVR login).
- Mapping cameras from `NVR DATA/*.csv` and `*.txt` without logging into the recorder.
- Digest/RTSP credentials against the **camera** address.

## Forbidden patterns

```python
# Bad — NVR as image source
http_snapshot(nvr_host, channel)
http_mjpeg_frame(nvr_host, channel)
build_rtsp_url(nvr_host, channel, direct_camera=False)
fetch_nvr_channels(nvr_host)  # not for startup or preview paths
```

```python
# Good — camera IP only
http_snapshot(camera_ip, 1)
http_mjpeg_frame(camera_ip, 1)
build_rtsp_url(camera_ip, 1, direct_camera=True)
```

## When editing `src/streaming.py` or `src/nvr_streams.py`

- Keep capture helpers camera-direct.
- Refuse or remove any new NVR-channel image path.
- Prefer export-only stream target assignment over live NVR CGI login.
