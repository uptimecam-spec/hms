"""Read-only Camera Status dashboard for Vercel (Bunny-backed)."""

from __future__ import annotations

import logging
import os
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from flask import Flask, Response, jsonify, redirect, render_template, request
import requests

from src.bunny_storage import (
    bunny_readable,
    check_image_object_key,
    download_bytes,
    download_json,
    is_bunny_path,
)
from src.live_bridge import (
    bridge_live_path,
    cloud_live_bridge_ready,
    live_bridge_token,
    live_bridge_url,
)

log = logging.getLogger("camera-uptime.cloud")

STATUS_KEY = "dashboard/camera-status.json"
HISTORIES_KEY = "dashboard/histories.json"

_TEMPLATES = Path(__file__).resolve().parent / "templates"

app = Flask(__name__, template_folder=str(_TEMPLATES))

_status_cache: dict[str, Any] = {"at": 0.0, "data": None}
_histories_cache: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL_SEC = 20.0


def _auth_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        password = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
        if not password:
            return view(*args, **kwargs)
        user = (os.getenv("DASHBOARD_USER") or "admin").strip() or "admin"
        auth = request.authorization
        if not auth or auth.username != user or auth.password != password:
            return Response(
                "Authentication required",
                401,
                {"WWW-Authenticate": 'Basic realm="Camera Monitor"'},
            )
        return view(*args, **kwargs)

    return wrapped


def _load_status() -> dict[str, Any] | None:
    if not bunny_readable():
        return None
    now = time.time()
    if _status_cache["data"] is not None and (now - float(_status_cache["at"])) < _CACHE_TTL_SEC:
        return _status_cache["data"]  # type: ignore[return-value]
    data = download_json(STATUS_KEY)
    _status_cache["at"] = now
    _status_cache["data"] = data
    return data


def _load_histories() -> dict[str, Any]:
    if not bunny_readable():
        return {}
    now = time.time()
    if _histories_cache["data"] is not None and (now - float(_histories_cache["at"])) < _CACHE_TTL_SEC:
        return _histories_cache["data"]  # type: ignore[return-value]
    raw = download_json(HISTORIES_KEY) or {}
    by_device = raw.get("byDeviceId") if isinstance(raw, dict) else None
    data = by_device if isinstance(by_device, dict) else {}
    _histories_cache["at"] = now
    _histories_cache["data"] = data
    return data


def _load_history(device_id: str) -> dict[str, Any] | None:
    histories = _load_histories()
    hist = histories.get(device_id)
    return hist if isinstance(hist, dict) else None


def _empty_payload() -> dict[str, Any]:
    return {
        "summary": {
            "total": 0,
            "online": 0,
            "offline": 0,
            "previewUnavailable": 0,
            "unknown": 0,
        },
        "cameras": [],
        "nvrs": [],
        "worker": {"running": False, "last_cycle": {}, "mandatory_interval_sec": 3600},
        "publishedAt": None,
        "updatedAt": None,
        "remoteMode": True,
    }


@app.get("/")
@_auth_required
def index():
    return redirect("/camera-status")


@app.get("/camera-status")
@_auth_required
def camera_status_page():
    payload = _load_status() or _empty_payload()
    worker = payload.get("worker") or {}
    last_cycle = worker.get("last_cycle") or {}
    published = payload.get("publishedAt") or payload.get("updatedAt")
    boot = {
        "cameras": payload.get("cameras") or [],
        "nvrs": payload.get("nvrs") or [],
        "summary": payload.get("summary") or {},
        "worker": {
            "running": bool(worker.get("running")),
            "interval": worker.get("mandatory_interval_sec"),
            "lastCycle": last_cycle.get("at") or published,
        },
        "remoteMode": True,
        "publishedAt": published,
        "liveBridgeEnabled": cloud_live_bridge_ready(),
    }
    return render_template(
        "camera_status.html",
        cameras=boot["cameras"],
        nvrs=boot["nvrs"],
        summary=boot["summary"],
        worker=worker,
        boot=boot,
        devices=[],
        remote_mode=True,
        live_bridge_enabled=cloud_live_bridge_ready(),
        published_at=published,
    )


@app.get("/api/camera-status")
@_auth_required
def api_camera_status():
    payload = _load_status() or _empty_payload()
    payload["remoteMode"] = True
    payload["liveBridgeEnabled"] = cloud_live_bridge_ready()
    if not payload.get("updatedAt"):
        payload["updatedAt"] = payload.get("publishedAt")
    return jsonify(payload)


@app.get("/api/cameras/<device_id>/health-history")
@_auth_required
def api_camera_health_history(device_id: str):
    hist = _load_history(device_id)
    if not hist:
        status = _load_status() or {}
        known = {c.get("deviceId") for c in status.get("cameras") or []}
        if device_id not in known:
            return jsonify({"ok": False, "error": "camera not found"}), 404
        return jsonify(
            {
                "ok": True,
                "deviceId": device_id,
                "deviceName": None,
                "nvrId": None,
                "ipAddress": None,
                "items": [],
            }
        )
    return jsonify({"ok": True, **hist})


def _image_for_check(device_id: str, check_id: str, image_path: str | None = None) -> bytes | None:
    if image_path and is_bunny_path(image_path):
        data = download_bytes(image_path)
        if data:
            return data
    data = download_bytes(check_image_object_key(device_id, check_id))
    if data:
        return data
    if image_path and not is_bunny_path(image_path):
        # Local-relative paths are not available on Vercel.
        return None
    return None


@app.get("/api/cameras/<device_id>/health-checks/<check_id>/image.jpg")
@_auth_required
def api_camera_health_check_image(device_id: str, check_id: str):
    hist = _load_history(device_id) or {}
    image_path = None
    for item in hist.get("items") or []:
        if item.get("id") == check_id:
            image_path = item.get("imagePath")
            break
    data = _image_for_check(device_id, check_id, image_path)
    if not data:
        return Response(status=404)
    return Response(
        data,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=300"},
    )


@app.get("/api/cameras/<device_id>/snapshot.jpg")
@_auth_required
def api_camera_snapshot(device_id: str):
    status = _load_status() or {}
    cam = next(
        (c for c in status.get("cameras") or [] if c.get("deviceId") == device_id),
        None,
    )
    check_id = (cam or {}).get("latestImageCheckId")
    image_path = (cam or {}).get("latestImagePath")
    if not check_id:
        hist = _load_history(device_id) or {}
        first = next(
            (i for i in hist.get("items") or [] if i.get("hasImage") and i.get("id")),
            None,
        )
        if first:
            check_id = first["id"]
            image_path = first.get("imagePath")
    if not check_id:
        return Response(status=404)
    data = _image_for_check(device_id, check_id, image_path)
    if not data:
        return Response(status=404)
    return Response(
        data,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=120"},
    )


@app.get("/api/cameras/<device_id>/live.jpg")
@_auth_required
def api_camera_live_frame(device_id: str):
    """Proxy one live JPEG from the on-site bridge (Cloudflare Tunnel)."""
    if not cloud_live_bridge_ready():
        return Response("Live bridge not configured", status=503)
    url = f"{live_bridge_url()}{bridge_live_path(device_id)}"
    try:
        upstream = requests.get(
            url,
            headers={
                "Authorization": f"Bearer {live_bridge_token()}",
                "X-Live-Bridge-Token": live_bridge_token(),
            },
            timeout=45,
        )
    except requests.RequestException as exc:
        log.warning("Live bridge request failed for %s: %s", device_id, exc)
        return Response("Live bridge unreachable", status=503)
    if upstream.status_code != 200 or not upstream.content:
        return Response(
            upstream.content or b"live frame unavailable",
            status=upstream.status_code if upstream.status_code >= 400 else 503,
            mimetype=upstream.headers.get("Content-Type", "text/plain"),
        )
    return Response(
        upstream.content,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/healthz")
def healthz():
    return jsonify(
        {
            "ok": True,
            "bunny": bunny_readable(),
            "liveBridge": cloud_live_bridge_ready(),
        }
    )
