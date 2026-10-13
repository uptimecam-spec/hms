"""Flask web dashboard + Device Master (Phase 1–3). Worker runs standalone by default."""

from __future__ import annotations

import logging
import os
import secrets
import uuid
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    session,
    url_for,
)
from werkzeug.utils import secure_filename

from src.access_control import (
    authenticate,
    can_see_device,
    ensure_access_schema,
    has_permission,
)
from src.admin_routes import register_admin_routes
from src.camera_status import build_camera_status_payload, camera_status_row
from src.core import dashboard_payload, load_settings, run_ping_round, send_telegram, telegram_configured
from src.db import (
    DEVICE_STATUSES,
    DEVICE_TYPES,
    ROOT,
    UPLOAD_DIR,
    create_device,
    deactivate_device,
    get_device,
    init_db,
    last_successful_check,
    list_devices,
    list_incidents,
    seed_from_cameras_yaml,
    set_frame_frozen,
    update_device,
)
from src.favorites import current_username, get_favorites, set_favorites, toggle_favorite
from src.health_store import (
    camera_history_payload,
    ensure_phase3_schema,
    get_health_check,
    list_health_checks,
    list_monitoring_warnings,
    resolve_frozen_warnings,
)
from src.nvr_catalog import ensure_nvr_sync, sync_nvr_cameras_to_db
from src.nvr_streams import ensure_stream_discovery
from src.streaming import (
    capture_live_frame,
    capture_rtsp_frame,
    check_rtsp_stream,
    ensure_stored_snapshots,
    read_check_image,
    refresh_device_snapshot,
    snapshot_for_device,
)
from src.live_bridge import (
    extract_bearer_or_header,
    live_bridge_configured,
    token_matches,
)
from src.worker import ensure_worker, run_check_now, run_due_cycle, worker_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camera-uptime.web")

load_dotenv(ROOT / ".env", override=False)

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = (
    os.getenv("FLASK_SECRET_KEY")
    or os.getenv("DASHBOARD_SESSION_SECRET")
    or "phase1-local-device-master"
)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 14,
)


def _actor():
    if session.get("auth") is not True:
        return None
    from src.access_control import get_user, get_user_by_username

    user_id = session.get("userId")
    user = get_user(user_id) if user_id else None
    if user is None and session.get("user"):
        user = get_user_by_username(str(session.get("user")))
    if not user or not user.get("isActive"):
        return None
    return user


def _csrf_token() -> str:
    token = session.get("csrf")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf"] = token
    return token


def _csrf_ok() -> bool:
    expected = str(session.get("csrf") or "")
    sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf") or ""
    return bool(expected) and secrets.compare_digest(str(sent), expected)


def _safe_next(raw: str | None) -> str:
    value = (raw or "").strip() or "/camera-status"
    parsed = urlparse(value)
    if parsed.scheme or parsed.netloc or not value.startswith("/") or value.startswith("/login"):
        return "/camera-status"
    return value


def _wants_html() -> bool:
    if request.path.startswith("/api/"):
        return False
    accept = (request.headers.get("Accept") or "").lower()
    if not accept or "*/*" in accept or "text/html" in accept:
        return True
    best = request.accept_mimetypes.best_match(["application/json", "text/html"])
    return best == "text/html"


def _permission_for(path: str) -> str | None:
    if path == "/":
        return None
    if path.startswith("/admin") or path.startswith("/api/admin"):
        return "ADMIN"
    if path.startswith("/devices/") and path.endswith("/history"):
        return "cameras.view"
    if path.startswith("/devices") or path.startswith("/api/sync-nvr"):
        return "devices.manage"
    if path.startswith("/health-checks") or path == "/api/health-checks":
        return "health.view"
    if path.startswith("/warnings") or path == "/api/warnings":
        return "warnings.view"
    if path.startswith("/api/status") or path.startswith("/api/ping-now"):
        return "dashboard.view"
    if path.startswith("/api/test-telegram"):
        return "settings.manage"
    if "/live.jpg" in path or "/rtsp" in path or path.startswith("/live"):
        return "cameras.live"
    if path.endswith("/ping") or "/preview/retry" in path:
        return "cameras.ping"
    return "cameras.view"


def _forbidden():
    if _wants_html():
        return render_template("forbidden.html"), 403
    return jsonify({"ok": False, "error": "forbidden"}), 403


def _missing():
    if _wants_html():
        return render_template("forbidden.html"), 404
    return jsonify({"ok": False, "error": "not found"}), 404


@app.context_processor
def _shell_context():
    actor = _actor()
    role = (actor or {}).get("role") or {}
    unrestricted = bool(role.get("unrestricted"))
    permissions = list(role.get("permissions") or [])
    if unrestricted:
        from src.access_control import permission_catalog

        permissions = [item["key"] for item in permission_catalog()]
    return {
        "remote_mode": False,
        "shell_user": actor.get("username") if actor else None,
        "shell_role": role.get("name"),
        "shell_unrestricted": unrestricted,
        "shell_permissions": permissions,
        "shell_can_admin": bool(
            actor and (has_permission(actor, "users.manage") or has_permission(actor, "roles.manage"))
        ),
    }


@app.get("/login")
def login():
    if _actor():
        return redirect(_safe_next(request.args.get("next")))
    return render_template(
        "login.html",
        error=None,
        username="",
        next_url=_safe_next(request.args.get("next")),
        hint="Sign in with your Camera Monitor account.",
    )


@app.post("/login")
def login_submit():
    username = (request.form.get("username") or "").strip()
    password = request.form.get("password") or ""
    next_url = _safe_next(request.form.get("next") or request.args.get("next"))
    user = authenticate(username, password)
    if not user:
        return (
            render_template(
                "login.html",
                error="Invalid username or password.",
                username=username,
                next_url=next_url,
                hint="Sign in with your Camera Monitor account.",
            ),
            401,
        )
    session.clear()
    session.permanent = True
    session["auth"] = True
    session["user"] = user["username"]
    session["userId"] = user["userId"]
    session["csrf"] = secrets.token_urlsafe(32)
    return redirect(next_url)


@app.get("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


register_admin_routes(app, _actor, _csrf_token, _csrf_ok)


@app.before_request
def _boot() -> None:
    init_db()
    ensure_phase3_schema()
    ensure_access_schema()
    seed_from_cameras_yaml()
    ensure_nvr_sync()
    ensure_stream_discovery()
    ensure_stored_snapshots()
    # Prefer standalone worker (python -m src.worker). Embed only if explicitly enabled
    # and no fresh heartbeat from the standalone process.
    if os.getenv("EMBEDDED_WORKER", "0") in ("1", "true", "yes"):
        ensure_worker()


@app.before_request
def _auth_gate():
    path = request.path
    if request.endpoint == "static" or path.startswith("/static") or path.startswith("/bridge"):
        return None
    if path in {"/login", "/logout"}:
        return None
    actor = _actor()
    if actor is None:
        if _wants_html():
            nxt = request.full_path if request.query_string else request.path
            if nxt.endswith("?"):
                nxt = nxt[:-1]
            return redirect(url_for("login", next=nxt))
        return jsonify({"ok": False, "error": "authentication required"}), 401
    needed = _permission_for(path)
    if needed == "ADMIN":
        if not (has_permission(actor, "users.manage") or has_permission(actor, "roles.manage")):
            return _forbidden()
    elif needed and not has_permission(actor, needed):
        return _forbidden()
    device_id = (request.view_args or {}).get("device_id")
    query_device = request.args.get("deviceId") if path.startswith("/api/") else None
    for candidate in (device_id, query_device):
        if not candidate:
            continue
        device = get_device(str(candidate))
        if device and not can_see_device(actor, device):
            return _missing()
    return None


def _scoped_devices(*, include_inactive: bool = True) -> list[dict]:
    actor = _actor()
    return [d for d in list_devices(include_inactive=include_inactive) if can_see_device(actor, d)]


def _scoped_cameras(*, active_only: bool = False) -> list[dict]:
    rows = _scoped_devices(include_inactive=not active_only)
    cameras = [d for d in rows if d.get("deviceType") == "CAMERA"]
    if active_only:
        cameras = [d for d in cameras if d.get("isActive")]
    return cameras


def _session_username() -> str:
    actor = _actor()
    if actor:
        return str(actor["username"])
    return current_username(None)


@app.get("/")
def index():
    actor = _actor()
    if has_permission(actor, "cameras.view"):
        return redirect(url_for("camera_status_page"))
    if has_permission(actor, "users.manage") or has_permission(actor, "roles.manage"):
        return redirect("/admin")
    if has_permission(actor, "devices.manage"):
        return redirect(url_for("devices_list"))
    return _forbidden()


def _allowed_ids() -> set[str]:
    return {d["deviceId"] for d in _scoped_devices()}


def _filter_rows(items: list[dict]) -> list[dict]:
    allowed = _allowed_ids()
    return [item for item in items if not item.get("deviceId") or item.get("deviceId") in allowed]


@app.get("/api/status")
def api_status():
    payload = dashboard_payload()
    payload["devices"] = [_public_device(d) for d in _scoped_devices()]
    payload["worker"] = worker_status()
    payload["open_incidents"] = _filter_rows(list_incidents(status="OPEN", limit=20))
    payload["monitoring_warnings"] = _filter_rows(list_monitoring_warnings(status="OPEN", limit=50))
    return jsonify(payload)


@app.post("/api/ping-now")
def api_ping_now():
    summary = run_due_cycle(force_all=True, check_kind="MANUAL")
    try:
        run_ping_round(source="manual")
    except Exception:
        log.exception("Legacy yaml ping failed")
    return jsonify(
        {
            "ok": True,
            "worker_cycle": summary,
            "dashboard": dashboard_payload(),
            "devices": [_public_device(d) for d in _scoped_devices(include_inactive=False)],
        }
    )


@app.post("/api/test-telegram")
def api_test_telegram():
    settings = load_settings()
    if not telegram_configured(settings):
        return jsonify({"ok": False, "error": "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env"}), 400
    ok = send_telegram(settings, "Camera ping dashboard test OK")
    return jsonify({"ok": ok})


@app.get("/api/health-checks")
def api_health_checks():
    device_id = request.args.get("deviceId")
    limit = int(request.args.get("limit") or 100)
    items = _filter_rows(list_health_checks(device_id=device_id, limit=limit))
    return jsonify({"items": items})


@app.get("/api/devices/<device_id>/last-working")
def api_last_working(device_id: str):
    hc = last_successful_check(device_id)
    return jsonify({"deviceId": device_id, "lastWorking": hc})


@app.get("/api/warnings")
def api_warnings():
    return jsonify({"items": _filter_rows(list_monitoring_warnings(status=request.args.get("status") or "OPEN"))})


def _public_device(device: dict) -> dict:
    """Drop fields that can carry stream credentials."""
    hidden = {"rtspUrl"}
    return {key: value for key, value in device.items() if key not in hidden}


def _camera_devices() -> list[dict]:
    return _scoped_cameras(active_only=False)


def _active_cameras() -> list[dict]:
    return _scoped_cameras(active_only=True)


@app.get("/camera-status")
def camera_status_page():
    devices = _active_cameras()
    worker = worker_status()
    payload = build_camera_status_payload(devices, worker=worker, active_only=True)
    last_cycle = worker.get("last_cycle") or {}
    username = _session_username()
    favorites = [item for item in get_favorites(username) if item in {d["deviceId"] for d in devices}]
    boot = {
        "cameras": payload["cameras"],
        "nvrs": payload["nvrs"],
        "summary": payload["summary"],
        "worker": {
            "running": bool(worker.get("running")),
            "interval": worker.get("mandatory_interval_sec"),
            "lastCycle": last_cycle.get("at"),
        },
        "username": username,
        "favorites": favorites,
    }
    return render_template(
        "camera_status.html",
        cameras=payload["cameras"],
        nvrs=payload["nvrs"],
        summary=payload["summary"],
        worker=worker,
        boot=boot,
        devices=devices,
    )


@app.get("/api/favorites")
def api_favorites_get():
    username = _session_username()
    allowed = {d["deviceId"] for d in _scoped_cameras()}
    favorites = [item for item in get_favorites(username) if item in allowed]
    return jsonify({"ok": True, "user": username, "favorites": favorites})


@app.put("/api/favorites")
def api_favorites_put():
    body = request.get_json(silent=True) or {}
    username = _session_username()
    allowed = {d["deviceId"] for d in _scoped_cameras()}
    raw = body.get("favorites") if isinstance(body.get("favorites"), list) else []
    ids = set_favorites(username, [item for item in raw if str(item) in allowed])
    return jsonify({"ok": True, "user": username, "favorites": ids})


@app.post("/api/favorites/toggle")
def api_favorites_toggle():
    body = request.get_json(silent=True) or {}
    username = _session_username()
    device_id = str(body.get("deviceId") or "").strip()
    if not device_id:
        return jsonify({"ok": False, "error": "deviceId required"}), 400
    device = get_device(device_id)
    if not device or not can_see_device(_actor(), device):
        return jsonify({"ok": False, "error": "not found"}), 404
    ids, favourited = toggle_favorite(username, device_id)
    return jsonify({"ok": True, "user": username, "favorites": ids, "favourited": favourited, "deviceId": device_id})


@app.get("/api/camera-status")
def api_camera_status():
    devices = _active_cameras()
    worker = worker_status()
    payload = build_camera_status_payload(devices, worker=worker, active_only=True)
    payload["updatedAt"] = payload["worker"].get("last_cycle", {}).get("at")
    return jsonify(payload)


@app.post("/api/cameras/<device_id>/ping")
def api_camera_ping(device_id: str):
    device = get_device(device_id)
    if not device:
        return jsonify({"ok": False, "error": "camera not found"}), 404
    try:
        result = run_check_now(device_id, check_kind="MANUAL")
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)}), 500
    fresh = get_device(device_id) or device
    return jsonify({"ok": True, "ping": result.get("icmp"), "status": result.get("status"), "camera": camera_status_row(fresh)})


@app.get("/cameras")
def cameras_page():
    devices = _camera_devices()
    nvrs = sorted({d.get("nvrId") or "—" for d in devices})
    mfrs = sorted({d.get("manufacturer") or "—" for d in devices})
    return render_template(
        "cameras.html",
        devices=devices,
        nvrs=nvrs,
        manufacturers=mfrs,
        worker=worker_status(),
    )


@app.get("/live")
def live_wall_page():
    devices = _camera_devices()
    nvrs = sorted({d.get("nvrId") or "—" for d in devices})
    mfrs = sorted({d.get("manufacturer") or "—" for d in devices})
    return render_template(
        "live_wall.html",
        devices=devices,
        nvrs=nvrs,
        manufacturers=mfrs,
        worker=worker_status(),
    )


@app.get("/api/cameras")
def api_cameras():
    return jsonify({"items": [_public_device(d) for d in _camera_devices()]})


@app.post("/api/sync-nvr")
def api_sync_nvr():
    summary = sync_nvr_cameras_to_db()
    return jsonify({"ok": True, **summary, "items": [_public_device(d) for d in _camera_devices()]})


@app.get("/api/cameras/<device_id>/snapshot.jpg")
def api_camera_snapshot(device_id: str):
    data, err = snapshot_for_device(device_id)
    if not data:
        return Response(status=404)
    return Response(
        data,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=120"},
    )


@app.get("/api/cameras/<device_id>/live.jpg")
def api_camera_live_frame(device_id: str):
    """One fresh frame for the open-camera live preview. Per-request capture only."""
    device = get_device(device_id)
    if not device:
        return Response(status=404)
    data, err = capture_live_frame(device_id)
    if not data:
        return Response(status=503)
    return Response(
        data,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.get("/api/cameras/<device_id>/rtsp.jpg")
def api_camera_rtsp_frame(device_id: str):
    """One frame forced through RTSP (never HTTP snapshot/MJPEG)."""
    data, _source, err = capture_rtsp_frame(device_id)
    if not data:
        return Response(err or "RTSP stream unavailable", status=503)
    return Response(
        data,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.post("/api/cameras/<device_id>/rtsp/check")
def api_camera_rtsp_check(device_id: str):
    """Verify RTSP transport and prove motion using two separated frames."""
    if not get_device(device_id):
        return jsonify({"ok": False, "error": "camera not found"}), 404
    result, _latest = check_rtsp_stream(device_id)
    if result.get("working") and result.get("frameChanged"):
        set_frame_frozen(device_id, None)
        resolve_frozen_warnings(device_id)
        result["camera"] = camera_status_row(get_device(device_id) or {})
    return jsonify(result), (200 if result.get("working") else 503)


@app.get("/bridge/v1/health")
def bridge_health():
    """Public health for Cloudflare Tunnel checks (no camera access)."""
    return jsonify(
        {
            "ok": True,
            "bridge": True,
            "tokenConfigured": live_bridge_configured(),
        }
    )


@app.get("/bridge/v1/live/<device_id>.jpg")
def bridge_live_frame(device_id: str):
    """Token-protected live JPEG for the Vercel-hosted dashboard via Cloudflare Tunnel."""
    if not live_bridge_configured():
        return jsonify({"ok": False, "error": "LIVE_BRIDGE_TOKEN not set on site"}), 503
    provided = extract_bearer_or_header(
        request.headers.get("Authorization"),
        request.headers.get("X-Live-Bridge-Token"),
    )
    if not token_matches(provided):
        return Response("Unauthorized", status=401)
    device = get_device(device_id)
    if not device:
        return Response(status=404)
    data, err = capture_live_frame(device_id)
    if not data:
        return Response(err or "live frame unavailable", status=503)
    return Response(
        data,
        mimetype="image/jpeg",
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate",
            "Pragma": "no-cache",
        },
    )


@app.post("/api/cameras/<device_id>/preview/retry")
def api_camera_preview_retry(device_id: str):
    """Reconnect preview/stream only. Network/ping status is left unchanged."""
    device = get_device(device_id)
    if not device:
        return jsonify({"ok": False, "error": "camera not found"}), 404
    ok, err = refresh_device_snapshot(device_id)
    fresh = get_device(device_id) or device
    row = camera_status_row(fresh)
    if not ok:
        return jsonify(
            {
                "ok": False,
                "error": err or "preview unavailable",
                "previewStatus": "UNAVAILABLE",
                "previewAvailable": False,
                "camera": row,
                "networkStatus": row.get("networkStatus"),
            }
        ), 503
    return jsonify(
        {
            "ok": True,
            "previewStatus": "AVAILABLE",
            "previewAvailable": True,
            "camera": row,
            "networkStatus": row.get("networkStatus"),
        }
    )


@app.get("/api/cameras/<device_id>/health-history")
def api_camera_health_history(device_id: str):
    device = get_device(device_id)
    if not device:
        return jsonify({"ok": False, "error": "camera not found"}), 404
    limit = request.args.get("limit", 72, type=int) or 72
    payload = camera_history_payload(device, limit=max(1, min(500, limit)))
    return jsonify({"ok": True, **payload})


@app.get("/api/cameras/<device_id>/health-checks/<check_id>/image.jpg")
def api_camera_health_check_image(device_id: str, check_id: str):
    device = get_device(device_id)
    if not device:
        return Response(status=404)
    check = get_health_check(check_id)
    if not check or check.get("deviceId") != device_id:
        return Response(status=404)
    data = read_check_image(device_id, check_id, check.get("imagePath"))
    if not data:
        return Response(status=404)
    return Response(
        data,
        mimetype="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )


@app.get("/devices")
def devices_list():
    devices = _scoped_devices()
    allowed = {d["deviceId"] for d in devices}
    last_map = {d["deviceId"]: last_successful_check(d["deviceId"]) for d in devices}
    incidents = [item for item in list_incidents(status="OPEN", limit=20) if item.get("deviceId") in allowed]
    warnings = [item for item in list_monitoring_warnings(status="OPEN", limit=20) if item.get("deviceId") in allowed]
    return render_template(
        "devices.html",
        devices=devices,
        last_working=last_map,
        types=DEVICE_TYPES,
        statuses=DEVICE_STATUSES,
        worker=worker_status(),
        incidents=incidents,
        warnings=warnings,
    )


@app.get("/devices/new")
def devices_new():
    return render_template(
        "device_form.html",
        device=None,
        types=DEVICE_TYPES,
        statuses=DEVICE_STATUSES,
        mode="create",
    )


@app.get("/devices/<device_id>/edit")
def devices_edit(device_id: str):
    device = get_device(device_id)
    if not device:
        flash("Device not found", "error")
        return redirect(url_for("devices_list"))
    return render_template(
        "device_form.html",
        device=device,
        types=DEVICE_TYPES,
        statuses=DEVICE_STATUSES,
        mode="edit",
    )


@app.get("/devices/<device_id>/history")
def devices_history(device_id: str):
    device = get_device(device_id)
    if not device:
        flash("Device not found", "error")
        return redirect(url_for("devices_list"))
    history = camera_history_payload(device, limit=200)
    items = history.get("items") or []
    last = last_successful_check(device_id)
    online_count = sum(1 for c in items if c.get("overallStatus") == "ONLINE")
    offline_count = sum(1 for c in items if c.get("overallStatus") == "OFFLINE")
    preview_count = sum(1 for c in items if c.get("overallStatus") == "PREVIEW_ERROR")
    return render_template(
        "device_history.html",
        device=device,
        history=history,
        items=items,
        last_working=last,
        online_count=online_count,
        offline_count=offline_count,
        preview_count=preview_count,
        remote_mode=False,
    )


def _form_payload() -> dict:
    return {
        "deviceName": request.form.get("deviceName", "").strip(),
        "deviceType": request.form.get("deviceType", "CAMERA").strip().upper(),
        "ipAddress": request.form.get("ipAddress", "").strip(),
        "location": request.form.get("location", "").strip(),
        "department": request.form.get("department", "").strip(),
        "building": request.form.get("building", "").strip(),
        "floor": request.form.get("floor", "").strip(),
        "nvrId": request.form.get("nvrId", "").strip(),
        "cameraChannel": request.form.get("cameraChannel", "").strip(),
        "installationDate": request.form.get("installationDate", "").strip(),
        "maintenanceNotes": request.form.get("maintenanceNotes", "").strip(),
        "status": request.form.get("status", "UNKNOWN").strip().upper(),
        "isActive": request.form.get("isActive") == "on",
    }


def _save_reference_image(device_id: str) -> str | None:
    f = request.files.get("referenceImage")
    if not f or not f.filename:
        return None
    ext = Path(secure_filename(f.filename)).suffix.lower() or ".jpg"
    if ext not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
        raise ValueError("Reference image must be jpg/png/webp/gif")
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    name = f"{device_id}_{uuid.uuid4().hex[:6]}{ext}"
    path = UPLOAD_DIR / name
    f.save(path)
    return str(Path("uploads") / "devices" / name).replace("\\", "/")


@app.post("/devices/new")
def devices_create():
    try:
        payload = _form_payload()
        device = create_device(payload)
        img = _save_reference_image(device["deviceId"])
        if img:
            update_device(device["deviceId"], {"referenceImage": img})
        flash("Device saved", "ok")
        return redirect(url_for("devices_list"))
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("devices_new"))


@app.post("/devices/<device_id>/edit")
def devices_update(device_id: str):
    try:
        payload = _form_payload()
        img = _save_reference_image(device_id)
        if img:
            payload["referenceImage"] = img
        updated = update_device(device_id, payload)
        if not updated:
            flash("Device not found", "error")
        else:
            flash("Device updated", "ok")
        return redirect(url_for("devices_list"))
    except Exception as e:
        flash(str(e), "error")
        return redirect(url_for("devices_edit", device_id=device_id))


@app.post("/devices/<device_id>/deactivate")
def devices_deactivate(device_id: str):
    if deactivate_device(device_id):
        flash("Device deactivated", "ok")
    else:
        flash("Device not found", "error")
    return redirect(url_for("devices_list"))


@app.post("/devices/<device_id>/test")
def devices_test(device_id: str):
    try:
        result = run_check_now(device_id, check_kind="MANUAL")
        icmp = result["icmp"]
        flash(
            f"MANUAL ICMP {result['status']} · RTT={icmp.get('responseTime')} ms · "
            f"loss={icmp.get('packetLoss')}% · {icmp.get('error') or 'ok'}",
            "ok" if result["status"] == "ONLINE" else "error",
        )
    except KeyError:
        flash("Device not found", "error")
    except Exception as e:
        flash(f"Test failed: {e}", "error")
    return redirect(url_for("devices_list"))


@app.get("/health-checks")
def health_checks_page():
    allowed = _allowed_ids()
    checks = [item for item in list_health_checks(limit=200) if item.get("deviceId") in allowed]
    warnings = [item for item in list_monitoring_warnings(status="OPEN", limit=50) if item.get("deviceId") in allowed]
    return render_template(
        "health_checks.html",
        checks=checks,
        worker=worker_status(),
        warnings=warnings,
    )


@app.get("/warnings")
def warnings_page():
    warnings = [item for item in list_monitoring_warnings(status=None, limit=100) if item.get("deviceId") in _allowed_ids()]
    return render_template(
        "warnings.html",
        warnings=warnings,
        worker=worker_status(),
    )


@app.get("/media/<path:subpath>")
def media(subpath: str):
    # Only serve reference images of devices this user may see; never the db/state files in data/.
    wanted = subpath.replace("\\", "/")
    if not wanted.startswith("uploads/devices/"):
        abort(404)
    if not any(d.get("referenceImage") == wanted for d in _scoped_devices()):
        abort(404)
    return send_from_directory(ROOT / "data", wanted)


def main() -> None:
    init_db()
    ensure_phase3_schema()
    seeded = seed_from_cameras_yaml()
    if seeded:
        log.info("Seeded %s cameras into Device Master", seeded)
    settings = load_settings()
    if os.getenv("EMBEDDED_WORKER", "0") in ("1", "true", "yes"):
        ensure_worker()
    else:
        log.info("EMBEDDED_WORKER=0 — expecting standalone: python -m src.worker")
    log.info(
        "Web http://127.0.0.1:%s  Status /camera-status  Cameras /cameras  Live /live",
        settings.web_port,
    )
    app.run(
        host=settings.web_host,
        port=settings.web_port,
        debug=False,
        use_reloader=False,
        threaded=True,
    )


if __name__ == "__main__":
    main()
