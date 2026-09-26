"""Device connection probes and Device Master sync helpers."""

from __future__ import annotations

import logging
from typing import Any

from src.core import Settings, icmp_ping, load_settings, tcp_check
from src.db import get_device, list_devices, set_device_status

log = logging.getLogger("camera-uptime.devices")


def default_ports(device_type: str) -> list[int]:
    t = (device_type or "CAMERA").upper()
    if t == "IP_PHONE":
        return [80, 443, 5060]
    if t == "NVR":
        return [554, 80, 443, 37777]
    return [554, 80]


def test_connection(device: dict[str, Any], settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or load_settings()
    host = device["ipAddress"]
    dtype = device.get("deviceType") or "CAMERA"
    icmp_ok, icmp_msg = icmp_ping(host, settings.icmp_timeout_ms)

    ports = default_ports(dtype)
    # Prefer explicit channel/port hints later; for now type defaults
    tcp_results = []
    any_tcp = False
    for port in ports:
        ok, msg = tcp_check(host, port, settings.tcp_timeout_sec)
        tcp_results.append({"port": port, "ok": ok, "detail": msg})
        if ok:
            any_tcp = True

    online = icmp_ok or any_tcp
    status = "ONLINE" if online else "OFFLINE"
    return {
        "deviceId": device.get("deviceId"),
        "deviceName": device.get("deviceName"),
        "ipAddress": host,
        "deviceType": dtype,
        "icmp_ok": icmp_ok,
        "icmp_detail": icmp_msg,
        "tcp": tcp_results,
        "online": online,
        "status": status,
    }


def test_connection_by_id(device_id: str, update_status: bool = True) -> dict[str, Any]:
    device = get_device(device_id)
    if not device:
        raise KeyError(device_id)
    result = test_connection(device)
    if update_status:
        set_device_status(device_id, result["status"])
        result["statusSaved"] = True
    return result


def active_devices_for_monitor() -> list[dict[str, Any]]:
    return [d for d in list_devices(include_inactive=False) if d.get("isActive")]
