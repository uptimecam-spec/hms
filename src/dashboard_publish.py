"""Publish read-only dashboard snapshots to Bunny for the Vercel cloud app."""

from __future__ import annotations

import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from src.bunny_storage import bunny_configured, upload_bytes
from src.camera_status import build_camera_status_payload, display_status_for, status_label
from src.db import list_devices
from src.health_store import camera_history_payload

log = logging.getLogger("camera-uptime.publish")

STATUS_KEY = "dashboard/camera-status.json"
HISTORIES_KEY = "dashboard/histories.json"


def _active_cameras() -> list[dict[str, Any]]:
    return [
        d
        for d in list_devices(include_inactive=False)
        if d.get("isActive") and d.get("deviceType") == "CAMERA"
    ]


def _enrich_cameras_with_images(
    payload: dict[str, Any], devices: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Attach latest check-image ids; return per-device history map."""
    by_id = {d["deviceId"]: d for d in devices}
    histories: dict[str, dict[str, Any]] = {}
    cam_by_id = {c["deviceId"]: c for c in payload.get("cameras") or []}

    def _one(device: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        device_id = device["deviceId"]
        return device_id, camera_history_payload(device, limit=48)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [pool.submit(_one, d) for d in by_id.values()]
        for future in as_completed(futures):
            device_id, hist = future.result()
            histories[device_id] = hist
            first_img = next(
                (i for i in hist.get("items") or [] if i.get("hasImage") and i.get("id")),
                None,
            )
            cam = cam_by_id.get(device_id)
            if not cam:
                continue
            if first_img:
                cam["previewAvailable"] = True
                cam["previewStatus"] = "AVAILABLE"
                cam["latestImageCheckId"] = first_img["id"]
                cam["latestImagePath"] = first_img.get("imagePath")
                # Stable cache-buster for snapshot URLs until the next capture.
                cam["previewVersion"] = first_img["id"]
                display = display_status_for(cam.get("networkStatus") or "UNKNOWN", "AVAILABLE")
                cam["displayStatus"] = display
                cam["displayStatusLabel"] = status_label(display)
            else:
                cam["latestImageCheckId"] = None
                cam["latestImagePath"] = None

    for nvr in payload.get("nvrs") or []:
        for cam in nvr.get("cameras") or []:
            src = cam_by_id.get(cam.get("deviceId"))
            if not src:
                continue
            for key in (
                "previewAvailable",
                "previewStatus",
                "latestImageCheckId",
                "latestImagePath",
                "previewVersion",
                "displayStatus",
                "displayStatusLabel",
            ):
                if key in src:
                    cam[key] = src[key]

    cameras = payload.get("cameras") or []
    online = sum(1 for c in cameras if c.get("networkStatus") == "ONLINE")
    offline = sum(1 for c in cameras if c.get("networkStatus") == "OFFLINE")
    preview_unavailable = sum(
        1
        for c in cameras
        if c.get("networkStatus") == "ONLINE" and c.get("previewStatus") == "UNAVAILABLE"
    )
    summary = payload.setdefault("summary", {})
    summary["total"] = len(cameras)
    summary["online"] = online
    summary["offline"] = offline
    summary["previewUnavailable"] = preview_unavailable
    summary["unknown"] = len(cameras) - online - offline
    return histories


def publish_dashboard_snapshot(*, worker: dict[str, Any] | None = None) -> dict[str, Any]:
    """Upload camera-status.json + histories.json to Bunny (2 objects)."""
    if not bunny_configured():
        return {"ok": False, "reason": "bunny not configured"}

    devices = _active_cameras()
    worker_info = worker or {
        "running": True,
        "last_cycle": {},
        "mandatory_interval_sec": 3600,
        "heartbeat": None,
        "scheduler": {},
        "open_warnings": 0,
    }
    payload = build_camera_status_payload(devices, worker=worker_info, active_only=True)
    published_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    payload["publishedAt"] = published_at
    payload["updatedAt"] = published_at
    payload["remoteMode"] = True

    histories = _enrich_cameras_with_images(payload, devices)

    upload_bytes(
        STATUS_KEY,
        json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )
    upload_bytes(
        HISTORIES_KEY,
        json.dumps({"publishedAt": published_at, "byDeviceId": histories}, separators=(",", ":")).encode(
            "utf-8"
        ),
        content_type="application/json",
    )

    log.info(
        "Published dashboard snapshot cameras=%s histories=%s at=%s",
        len(devices),
        len(histories),
        published_at,
    )
    return {
        "ok": True,
        "cameras": len(devices),
        "histories": len(histories),
        "publishedAt": published_at,
    }
