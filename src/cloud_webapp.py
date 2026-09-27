"""Read-only Camera Status dashboard for Vercel (Bunny-backed)."""

from __future__ import annotations

import logging
import os
import secrets
import time
from functools import wraps
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

from src.bunny_storage import (
    bunny_readable,
    check_image_object_key,
    download_bytes,
    download_json,
    is_bunny_path,
    public_or_proxy_hint,
)
from src.db import ROOT
from src.favorites import current_username, get_favorites, set_favorites, toggle_favorite
from src.image_cache import IMAGE_CACHE
from src.live_bridge import (
    bridge_live_path,
    cloud_live_bridge_ready,
    live_bridge_token,
    live_bridge_url,
)

log = logging.getLogger("camera-uptime.cloud")

load_dotenv(ROOT / ".env", override=False)

STATUS_KEY = "dashboard/camera-status.json"
HISTORIES_KEY = "dashboard/histories.json"

_TEMPLATES = Path(__file__).resolve().parent / "templates"

app = Flask(__name__, template_folder=str(_TEMPLATES))
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("VERCEL", "").strip() == "1"
    or os.getenv("SESSION_COOKIE_SECURE", "").strip().lower() in ("1", "true", "yes"),
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
)


def _configure_secret() -> None:
    secret = (os.getenv("FLASK_SECRET_KEY") or os.getenv("DASHBOARD_SESSION_SECRET") or "").strip()
    if not secret:
        password = (os.getenv("DASHBOARD_PASSWORD") or "camera-monitor").strip()
        secret = f"camera-uptime::{password}"
    app.secret_key = secret


_configure_secret()

_status_cache: dict[str, Any] = {"at": 0.0, "data": None}
_histories_cache: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL_SEC = 20.0


def _auth_enabled() -> bool:
    return bool((os.getenv("DASHBOARD_PASSWORD") or "").strip())


def _expected_credentials() -> tuple[str, str]:
    user = (os.getenv("DASHBOARD_USER") or "admin").strip() or "admin"
    password = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
    return user, password


def _is_logged_in() -> bool:
    if not _auth_enabled():
        return True
    user, _password = _expected_credentials()
    return bool(session.get("auth") is True and session.get("user") == user)


def _wants_html() -> bool:
    if request.path.startswith("/api/"):
        return False
    accept = (request.headers.get("Accept") or "").lower()
    # Browsers often send */* or empty on navigation; always show the login page.
    if not accept or "*/*" in accept or "text/html" in accept:
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "text/html"


def _safe_next(raw: str | None) -> str:
    value = (raw or "").strip() or "/camera-status"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc:
        return "/camera-status"
    if not value.startswith("/"):
        return "/camera-status"
    if value.startswith("/login"):
        return "/camera-status"
    return value


def _auth_required(view: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(view)
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        if _is_logged_in():
            return view(*args, **kwargs)
        if _wants_html():
            nxt = request.full_path if request.query_string else request.path
            if nxt.endswith("?"):
                nxt = nxt[:-1]
            return redirect(url_for("login", next=nxt))
        return jsonify({"ok": False, "error": "authentication required"}), 401

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


@app.get("/login")
def login():
    if _is_logged_in():
        return redirect(_safe_next(request.args.get("next")))
    if not _auth_enabled():
        return redirect("/camera-status")
    return render_template(
        "login.html",
        error=None,
        username=(os.getenv("DASHBOARD_USER") or "admin").strip() or "admin",
        next_url=_safe_next(request.args.get("next")),
    )


@app.post("/login")
def login_submit():
    if not _auth_enabled():
        return redirect("/camera-status")
    expected_user, expected_password = _expected_credentials()
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = _safe_next(request.form.get("next") or request.args.get("next"))
    if secrets.compare_digest(username, expected_user) and secrets.compare_digest(
        password, expected_password
    ):
        session.clear()
        session.permanent = True
        session["auth"] = True
        session["user"] = expected_user
        return redirect(next_url)
    return (
        render_template(
            "login.html",
            error="Invalid username or password.",
            username=username,
            next_url=next_url,
        ),
        401,
    )


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


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
    username = current_username(session.get("user"))
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
        "username": username,
        "favorites": get_favorites(username, prefer_bunny=True),
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


@app.get("/api/favorites")
@_auth_required
def api_favorites_get():
    username = current_username(session.get("user") or request.args.get("user"))
    return jsonify(
        {
            "ok": True,
            "user": username,
            "favorites": get_favorites(username, prefer_bunny=True),
        }
    )


@app.put("/api/favorites")
@_auth_required
def api_favorites_put():
    body = request.get_json(silent=True) or {}
    username = current_username(session.get("user") or body.get("user"))
    ids = set_favorites(
        username,
        body.get("favorites") if isinstance(body.get("favorites"), list) else [],
        sync_bunny=True,
        prefer_bunny=True,
    )
    return jsonify({"ok": True, "user": username, "favorites": ids})


@app.post("/api/favorites/toggle")
@_auth_required
def api_favorites_toggle():
    body = request.get_json(silent=True) or {}
    username = current_username(session.get("user") or body.get("user"))
    device_id = str(body.get("deviceId") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "deviceId required"}), 400
    ids, favourited = toggle_favorite(
        username, device_id, sync_bunny=True, prefer_bunny=True
    )
    return jsonify(
        {
            "ok": True,
            "user": username,
            "favorites": ids,
            "favourited": favourited,
            "deviceId": device_id,
        }
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
    cache_key = f"{device_id}:{check_id}:{image_path or ''}"
    cached = IMAGE_CACHE.get(cache_key)
    if cached:
        return cached

    data = None
    if image_path and is_bunny_path(image_path):
        data = download_bytes(image_path)
    if not data:
        data = download_bytes(check_image_object_key(device_id, check_id))
    if not data and image_path and not is_bunny_path(image_path):
        return None
    if data:
        IMAGE_CACHE.put(cache_key, data)
    return data


def _jpeg_response(data: bytes, *, immutable: bool = False, max_age: int = 86400) -> Response:
    if immutable:
        cache = f"private, max-age={max_age}, immutable"
    else:
        cache = f"private, max-age={max_age}"
    return Response(
        data,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": cache,
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.get("/api/cameras/<device_id>/health-checks/<check_id>/image.jpg")
@_auth_required
def api_camera_health_check_image(device_id: str, check_id: str):
    hist = _load_history(device_id) or {}
    image_path = None
    for item in hist.get("items") or []:
        if item.get("id") == check_id:
            image_path = item.get("imagePath")
            break
    cdn = public_or_proxy_hint(image_path) if image_path else None
    if not cdn:
        cdn = public_or_proxy_hint(check_image_object_key(device_id, check_id))
    if cdn:
        return redirect(cdn, code=302)
    data = _image_for_check(device_id, check_id, image_path)
    if not data:
        return Response(status=404)
    return _jpeg_response(data, immutable=True, max_age=30 * 24 * 3600)


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
    cdn = public_or_proxy_hint(image_path) if image_path else None
    if not cdn:
        cdn = public_or_proxy_hint(check_image_object_key(device_id, check_id))
    if cdn:
        return redirect(cdn, code=302)
    data = _image_for_check(device_id, check_id, image_path)
    if not data:
        return Response(status=404)
    return _jpeg_response(data, immutable=True, max_age=7 * 24 * 3600)


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
