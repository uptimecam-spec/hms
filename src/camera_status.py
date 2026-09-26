"""Payload helpers for the Camera Status Monitoring dashboard."""

from __future__ import annotations

import re
from typing import Any

from src.streaming import preview_file_meta

STATUS_LABELS = {
    "ONLINE": "Online",
    "OFFLINE": "Offline",
    "UNKNOWN": "Not checked",
    "WARNING": "Warning",
    "UNDER_MAINTENANCE": "Maintenance",
    "ONLINE_PREVIEW_UNAVAILABLE": "Preview Error",
}

_membership_cache: dict[str, Any] | None = None


def status_label(raw: str | None) -> str:
    key = (raw or "UNKNOWN").upper()
    return STATUS_LABELS.get(key, key.title())


def is_online_offline(raw: str | None) -> str | None:
    key = (raw or "").upper()
    if key == "ONLINE":
        return "online"
    if key == "OFFLINE":
        return "offline"
    return None


def _nvr_host_label(host: str | None) -> str | None:
    match = re.fullmatch(r"192\.168\.(\d{1,3})\.(\d{1,3})", (host or "").strip())
    if not match:
        return None
    return f"{int(match.group(1))}.{int(match.group(2))}"


def _nvr_membership_by_ip() -> dict[str, list[str]]:
    """Read-only IP → NVR export membership. Does not mutate device records."""
    global _membership_cache
    from src.nvr_catalog import NVR_DATA_DIR, load_nvr_cameras

    stamp = 0.0
    if NVR_DATA_DIR.is_dir():
        for path in NVR_DATA_DIR.iterdir():
            if path.suffix.lower() in (".csv", ".txt"):
                try:
                    stamp = max(stamp, path.stat().st_mtime)
                except OSError:
                    continue

    if _membership_cache and _membership_cache.get("stamp") == stamp:
        return _membership_cache["map"]

    mapping: dict[str, list[str]] = {}
    channel_by_nvr: dict[str, dict[str, int]] = {}
    name_by_nvr: dict[str, dict[str, str]] = {}
    for row in load_nvr_cameras():
        mapping.setdefault(row.ip, [])
        if row.nvr_source not in mapping[row.ip]:
            mapping[row.ip].append(row.nvr_source)
        channels = channel_by_nvr.setdefault(row.nvr_source, {})
        if row.ip not in channels:
            channels[row.ip] = len(channels) + 1
        names = name_by_nvr.setdefault(row.nvr_source, {})
        if row.ip not in names:
            names[row.ip] = row.display_name

    _membership_cache = {
        "stamp": stamp,
        "map": mapping,
        "channels": channel_by_nvr,
        "names": name_by_nvr,
    }
    return mapping


def _catalog_channels() -> dict[str, dict[str, int]]:
    _nvr_membership_by_ip()
    assert _membership_cache is not None
    return _membership_cache["channels"]


def _catalog_names() -> dict[str, dict[str, str]]:
    _nvr_membership_by_ip()
    assert _membership_cache is not None
    return _membership_cache["names"]


def nvr_groups_for_device(device: dict[str, Any]) -> list[str]:
    """NVR ids this camera belongs to (catalog first, then stored nvrId)."""
    membership = _nvr_membership_by_ip()
    ip = (device.get("ipAddress") or "").strip()
    if ip and ip in membership:
        return list(membership[ip])

    raw = (device.get("nvrId") or "").strip()
    if raw:
        parts = [p.strip() for p in raw.split(",") if p.strip()]
        if parts:
            return parts

    from_host = _nvr_host_label(device.get("nvrHost"))
    if from_host:
        return [from_host]
    return ["Unknown"]


def preview_status_for(device: dict[str, Any]) -> tuple[str, int | None]:
    device_id = device.get("deviceId")
    if not device_id:
        return "UNAVAILABLE", None
    available, version = preview_file_meta(str(device_id))
    if available:
        return "AVAILABLE", version
    return "UNAVAILABLE", None


def display_status_for(network_status: str, preview_status: str) -> str:
    """Overall camera status keeps network health; preview is a separate signal."""
    net = (network_status or "UNKNOWN").upper()
    if net == "ONLINE" and preview_status == "UNAVAILABLE":
        return "ONLINE_PREVIEW_UNAVAILABLE"
    return net


def camera_status_row(
    device: dict[str, Any],
    *,
    nvr_group: str | None = None,
) -> dict[str, Any]:
    st = (device.get("status") or "UNKNOWN").upper()
    preview, preview_version = preview_status_for(device)
    display = display_status_for(st, preview)
    groups = nvr_groups_for_device(device)
    primary = nvr_group or (groups[0] if groups else "Unknown")
    channels = _catalog_channels().get(primary) or {}
    names = _catalog_names().get(primary) or {}
    ip = (device.get("ipAddress") or "").strip()
    group_channel = channels.get(ip)
    group_name = names.get(ip) or device.get("deviceName")

    return {
        "deviceId": device.get("deviceId"),
        "deviceName": device.get("deviceName"),
        "displayName": group_name,
        "ipAddress": device.get("ipAddress"),
        "status": st,
        "statusLabel": status_label(st),
        "networkStatus": st,
        "networkStatusLabel": status_label(st),
        "previewStatus": preview,
        "previewAvailable": preview == "AVAILABLE",
        "previewVersion": preview_version,
        "displayStatus": display,
        "displayStatusLabel": status_label(display),
        "isActive": bool(device.get("isActive")),
        "lastCheckAt": device.get("lastCheckAt"),
        "lastSuccessAt": device.get("lastSuccessAt"),
        "nextCheckAt": device.get("nextCheckAt"),
        "lastResponseTimeMs": device.get("lastResponseTimeMs"),
        "lastPacketLossPct": device.get("lastPacketLossPct"),
        "lastError": device.get("lastError"),
        "nvrId": device.get("nvrId"),
        "nvrHost": device.get("nvrHost"),
        "nvrChannel": device.get("nvrChannel"),
        "nvrGroup": primary,
        "nvrLabel": f"NVR {primary}",
        "nvrGroups": groups,
        "groupChannel": group_channel,
        "manufacturer": device.get("manufacturer"),
        "cameraChannel": device.get("cameraChannel"),
        "servicePort": device.get("servicePort"),
        "location": device.get("location"),
        "checkIntervalSec": device.get("checkIntervalSec") or 3600,
    }


def _sort_key(cam: dict[str, Any]) -> tuple:
    """Offline first, then preview error, then online; channel within each band."""
    net = (cam.get("networkStatus") or cam.get("status") or "UNKNOWN").upper()
    preview = (cam.get("previewStatus") or "").upper()
    display = (cam.get("displayStatus") or "").upper()
    if net == "OFFLINE" or display == "OFFLINE":
        band = 0
    elif net == "ONLINE" and (preview == "UNAVAILABLE" or display == "ONLINE_PREVIEW_UNAVAILABLE"):
        band = 1
    elif net == "ONLINE":
        band = 3
    else:
        band = 2
    channel = cam.get("groupChannel") or cam.get("nvrChannel") or 10_000
    try:
        channel_n = int(channel)
    except (TypeError, ValueError):
        channel_n = 10_000
    name = (cam.get("displayName") or cam.get("deviceName") or "").lower()
    return (band, channel_n, name, cam.get("ipAddress") or "")


def _nvr_summary(nvr_id: str, cameras: list[dict[str, Any]]) -> dict[str, Any]:
    online = sum(1 for c in cameras if c["networkStatus"] == "ONLINE")
    offline = sum(1 for c in cameras if c["networkStatus"] == "OFFLINE")
    preview_unavailable = sum(
        1
        for c in cameras
        if c["networkStatus"] == "ONLINE" and c["previewStatus"] == "UNAVAILABLE"
    )
    unknown = len(cameras) - online - offline
    host = next((c.get("nvrHost") for c in cameras if c.get("nvrHost")), None)
    if not host:
        parts = nvr_id.split(".")
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            host = f"192.168.{int(parts[0])}.{int(parts[1])}"
    return {
        "nvrId": nvr_id,
        "nvrLabel": f"NVR {nvr_id}",
        "nvrHost": host,
        "total": len(cameras),
        "online": online,
        "offline": offline,
        "previewUnavailable": preview_unavailable,
        "unknown": unknown,
        "cameras": cameras,
    }


def build_camera_status_payload(
    devices: list[dict[str, Any]],
    *,
    worker: dict[str, Any],
    active_only: bool = True,
) -> dict[str, Any]:
    rows = devices
    if active_only:
        rows = [d for d in devices if d.get("isActive")]

    cameras = [camera_status_row(d) for d in rows]
    online = sum(1 for c in cameras if c["networkStatus"] == "ONLINE")
    offline = sum(1 for c in cameras if c["networkStatus"] == "OFFLINE")
    preview_unavailable = sum(
        1
        for c in cameras
        if c["networkStatus"] == "ONLINE" and c["previewStatus"] == "UNAVAILABLE"
    )
    unknown = len(cameras) - online - offline

    grouped: dict[str, list[dict[str, Any]]] = {}
    for device in rows:
        for nvr_id in nvr_groups_for_device(device):
            grouped.setdefault(nvr_id, []).append(camera_status_row(device, nvr_group=nvr_id))

    nvrs: list[dict[str, Any]] = []
    for nvr_id in sorted(grouped.keys(), key=lambda x: [int(p) if p.isdigit() else p for p in x.split(".")]):
        cams = sorted(grouped[nvr_id], key=_sort_key)
        nvrs.append(_nvr_summary(nvr_id, cams))

    return {
        "summary": {
            "total": len(cameras),
            "online": online,
            "offline": offline,
            "previewUnavailable": preview_unavailable,
            "unknown": unknown,
            "nvrCount": len(nvrs),
            "checkIntervalSec": worker.get("mandatory_interval_sec") or 3600,
        },
        "worker": worker,
        "cameras": cameras,
        "nvrs": nvrs,
    }
