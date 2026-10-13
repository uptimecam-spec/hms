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

from src.access_control import (
    actor_from_directory,
    authenticate_directory,
    camera_allowed,
    directory_user,
    filter_camera_status_payload,
    has_permission,
    permission_catalog,
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
ACCESS_KEY = "dashboard/access-control.json"

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
_access_cache: dict[str, Any] = {"at": 0.0, "data": None}
_CACHE_TTL_SEC = 20.0


@app.context_processor
def _shell_context():
    actor = _current_actor()
    role = (actor or {}).get("role") or {}
    unrestricted = bool(role.get("unrestricted"))
    permissions = list(role.get("permissions") or [])
    if unrestricted:
        permissions = [item["key"] for item in permission_catalog()]
    return {
        "remote_mode": True,
        "shell_user": actor.get("username") if actor else None,
        "shell_role": role.get("name"),
        "shell_unrestricted": unrestricted,
        "shell_permissions": permissions,
        "shell_can_admin": False,
    }


def _auth_enabled() -> bool:
    return bool((os.getenv("DASHBOARD_PASSWORD") or "").strip())


def _expected_credentials() -> tuple[str, str]:
    user = (os.getenv("DASHBOARD_USER") or "admin").strip() or "admin"
    password = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
    return user, password


def _load_access_directory() -> dict[str, Any] | None:
    if not bunny_readable():
        return None
    now = time.time()
    if _access_cache["data"] is not None and (now - float(_access_cache["at"])) < _CACHE_TTL_SEC:
        return _access_cache["data"]  # type: ignore[return-value]
    data = download_json(ACCESS_KEY)
    _access_cache["at"] = now
    _access_cache["data"] = data if isinstance(data, dict) else None
    return _access_cache["data"]  # type: ignore[return-value]


def _env_super_actor() -> dict[str, Any]:
    user, _password = _expected_credentials()
    return {
        "userId": "env_super",
        "username": user,
        "displayName": user,
        "isActive": True,
        "roleId": "role_super_admin",
        "role": {
            "roleId": "role_super_admin",
            "name": "Super Admin",
            "unrestricted": True,
            "permissions": [item["key"] for item in permission_catalog()],
        },
        "grants": [],
        "denies": [],
    }


def _current_actor() -> dict[str, Any] | None:
    if not _auth_enabled():
        return _env_super_actor()
    if session.get("auth") is not True:
        return None
    directory = _load_access_directory()
    username = str(session.get("user") or "")
    user_id = session.get("userId")
    if directory:
        raw = directory_user(directory, user_id=str(user_id)) if user_id and user_id != "env_super" else None
        if raw is None and username:
            raw = directory_user(directory, username=username)
        if raw is not None:
            if not raw.get("isActive"):
                return None
            return actor_from_directory(directory, user_id=str(raw.get("userId") or ""))
    env_user, _password = _expected_credentials()
    if username == env_user:
        return _env_super_actor()
    return None


def _is_logged_in() -> bool:
    return _current_actor() is not None


def _visible_status() -> dict[str, Any]:
    payload = _load_status() or _empty_payload()
    return filter_camera_status_payload(payload, _current_actor())


def _camera_forbidden(device_id: str, permission: str = "cameras.view"):
    actor = _current_actor()
    if not has_permission(actor, permission):
        return jsonify({"ok": False, "error": "forbidden"}), 403
    payload = _load_status() or {}
    cam = next((c for c in payload.get("cameras") or [] if c.get("deviceId") == device_id), None)
    if cam is None or not camera_allowed(actor, cam):
        return jsonify({"ok": False, "error": "not found"}), 404
    return None


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
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = _safe_next(request.form.get("next") or request.args.get("next"))
    directory = _load_access_directory()
    actor = authenticate_directory(directory, username, password)
    if actor is None and not (directory and directory_user(directory, username=username)):
        expected_user, expected_password = _expected_credentials()
        if secrets.compare_digest(username, expected_user) and secrets.compare_digest(password, expected_password):
            actor = _env_super_actor()
    if actor:
        session.clear()
        session.permanent = True
        session["auth"] = True
        session["user"] = actor["username"]
        session["userId"] = actor["userId"]
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
    payload = _visible_status()
    worker = payload.get("worker") or {}
    last_cycle = worker.get("last_cycle") or {}
    published = payload.get("publishedAt") or payload.get("updatedAt")
    username = current_username(session.get("user"))
    allowed = {c.get("deviceId") for c in (payload.get("cameras") or [])}
    favorites = [item for item in get_favorites(username, prefer_bunny=True) if item in allowed]
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
        "favorites": favorites,
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
    username = current_username(session.get("user"))
    allowed = {c.get("deviceId") for c in (_visible_status().get("cameras") or [])}
    favorites = [item for item in get_favorites(username, prefer_bunny=True) if item in allowed]
    return jsonify({"ok": True, "user": username, "favorites": favorites})


@app.put("/api/favorites")
@_auth_required
def api_favorites_put():
    body = request.get_json(silent=True) or {}
    username = current_username(session.get("user"))
    allowed = {c.get("deviceId") for c in (_visible_status().get("cameras") or [])}
    raw = body.get("favorites") if isinstance(body.get("favorites"), list) else []
    ids = set_favorites(
        username,
        [item for item in raw if str(item) in allowed],
        sync_bunny=True,
        prefer_bunny=True,
    )
    return jsonify({"ok": True, "user": username, "favorites": ids})


@app.post("/api/favorites/toggle")
@_auth_required
def api_favorites_toggle():
    body = request.get_json(silent=True) or {}
    username = current_username(session.get("user"))
    device_id = str(body.get("deviceId") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "deviceId required"}), 400
    denied = _camera_forbidden(device_id)
    if denied:
        return denied
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
    payload = _visible_status()
    payload["remoteMode"] = True
    payload["liveBridgeEnabled"] = cloud_live_bridge_ready()
    if not payload.get("updatedAt"):
        payload["updatedAt"] = payload.get("publishedAt")
    return jsonify(payload)


@app.get("/api/cameras/<device_id>/health-history")
@_auth_required
def api_camera_health_history(device_id: str):
    denied = _camera_forbidden(device_id)
    if denied:
        return denied
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


@app.get("/devices/<device_id>/history")
@_auth_required
def devices_history(device_id: str):
    denied = _camera_forbidden(device_id)
    if denied:
        if _wants_html():
            return render_template("forbidden.html"), denied[1]
        return denied
    status = _visible_status()
    cam = next(
        (c for c in status.get("cameras") or [] if c.get("deviceId") == device_id),
        None,
    )
    hist = _load_history(device_id) or {}
    items = list(hist.get("items") or []) if isinstance(hist, dict) else []
    if not cam and not items:
        return (
            render_template(
                "device_history.html",
                device={"deviceId": device_id, "deviceName": device_id, "status": "UNKNOWN"},
                history={"items": []},
                items=[],
                last_working=None,
                online_count=0,
                offline_count=0,
                preview_count=0,
                remote_mode=True,
            ),
            404,
        )
    device = {
        "deviceId": device_id,
        "deviceName": (cam or {}).get("deviceName") or (cam or {}).get("displayName") or hist.get("deviceName") or device_id,
        "ipAddress": (cam or {}).get("ipAddress") or hist.get("ipAddress"),
        "nvrId": (cam or {}).get("nvrId") or hist.get("nvrId"),
        "cameraChannel": (cam or {}).get("cameraChannel") or (cam or {}).get("groupChannel"),
        "nvrChannel": (cam or {}).get("nvrChannel"),
        "status": (cam or {}).get("networkStatus") or (cam or {}).get("status") or "UNKNOWN",
        "networkStatus": (cam or {}).get("networkStatus"),
    }
    online_count = sum(1 for c in items if c.get("overallStatus") == "ONLINE")
    offline_count = sum(1 for c in items if c.get("overallStatus") == "OFFLINE")
    preview_count = sum(1 for c in items if c.get("overallStatus") == "PREVIEW_ERROR")
    last_working = next((c for c in items if c.get("overallStatus") == "ONLINE"), None)
    return render_template(
        "device_history.html",
        device=device,
        history={"items": items, **({k: v for k, v in hist.items() if k != "items"} if isinstance(hist, dict) else {})},
        items=items,
        last_working=last_working,
        online_count=online_count,
        offline_count=offline_count,
        preview_count=preview_count,
        remote_mode=True,
    )


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
    denied = _camera_forbidden(device_id)
    if denied:
        return Response(status=404)
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
    denied = _camera_forbidden(device_id)
    if denied:
        return Response(status=404)
    status = _visible_status()
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
    denied = _camera_forbidden(device_id, "cameras.live")
    if denied:
        return Response(status=404 if denied[1] == 404 else 403)
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
