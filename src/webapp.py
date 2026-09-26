"""Flask web dashboard + Device Master (Phase 1–3). Worker runs standalone by default."""

from __future__ import annotations

import logging
import os
import uuid
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_from_directory,
    url_for,
)
from werkzeug.utils import secure_filename

from src.camera_status import build_camera_status_payload, camera_status_row
from src.core import dashboard_payload, load_settings, run_ping_round, send_telegram, telegram_configured
from src.db import (
    DEVICE_STATUSES,
    DEVICE_TYPES,
    UPLOAD_DIR,
    create_device,
    deactivate_device,
    get_device,
    init_db,
    last_successful_check,
    list_devices,
    list_incidents,
    seed_from_cameras_yaml,
    update_device,
)
from src.health_store import (
    camera_history_payload,
    ensure_phase3_schema,
    get_health_check,
    list_health_checks,
    list_monitoring_warnings,
)
from src.nvr_catalog import ensure_nvr_sync, sync_nvr_cameras_to_db
from src.nvr_streams import ensure_stream_discovery
from src.streaming import (
    capture_live_frame,
    ensure_stored_snapshots,
    read_check_image,
    refresh_device_snapshot,
    snapshot_for_device,
)
from src.worker import ensure_worker, run_check_now, run_due_cycle, worker_status

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("camera-uptime.web")

app = Flask(__name__, template_folder="templates", static_folder="static")
app.secret_key = "phase1-local-device-master"
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.before_request
def _boot() -> None:
    init_db()
    ensure_phase3_schema()
    seed_from_cameras_yaml()
    ensure_nvr_sync()
    ensure_stream_discovery()
    ensure_stored_snapshots()
    # Prefer standalone worker (python -m src.worker). Embed only if explicitly enabled
    # and no fresh heartbeat from the standalone process.
    if os.getenv("EMBEDDED_WORKER", "0") in ("1", "true", "yes"):
        ensure_worker()


@app.get("/")
def index():
    return render_template("dashboard.html")


@app.get("/api/status")
def api_status():
    payload = dashboard_payload()
    payload["devices"] = [_public_device(d) for d in list_devices(include_inactive=True)]
    payload["worker"] = worker_status()
    payload["open_incidents"] = list_incidents(status="OPEN", limit=20)
    payload["monitoring_warnings"] = list_monitoring_warnings(status="OPEN", limit=50)
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
            "devices": [_public_device(d) for d in list_devices()],
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
    return jsonify({"items": list_health_checks(device_id=device_id, limit=limit)})


@app.get("/api/devices/<device_id>/last-working")
def api_last_working(device_id: str):
    hc = last_successful_check(device_id)
    return jsonify({"deviceId": device_id, "lastWorking": hc})


@app.get("/api/warnings")
def api_warnings():
    return jsonify({"items": list_monitoring_warnings(status=request.args.get("status") or "OPEN")})


def _public_device(device: dict) -> dict:
    """Drop fields that can carry stream credentials."""
    hidden = {"rtspUrl"}
    return {key: value for key, value in device.items() if key not in hidden}


def _camera_devices() -> list[dict]:
    return [d for d in list_devices(include_inactive=True) if d.get("deviceType") == "CAMERA"]


def _active_cameras() -> list[dict]:
    return [d for d in _camera_devices() if d.get("isActive")]


@app.get("/camera-status")
def camera_status_page():
    devices = _active_cameras()
    worker = worker_status()
    payload = build_camera_status_payload(devices, worker=worker, active_only=True)
    last_cycle = worker.get("last_cycle") or {}
    boot = {
        "cameras": payload["cameras"],
        "nvrs": payload["nvrs"],
        "summary": payload["summary"],
        "worker": {
            "running": bool(worker.get("running")),
            "interval": worker.get("mandatory_interval_sec"),
            "lastCycle": last_cycle.get("at"),
        },
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
    devices = list_devices(include_inactive=True)
    last_map = {d["deviceId"]: last_successful_check(d["deviceId"]) for d in devices}
    return render_template(
        "devices.html",
        devices=devices,
        last_working=last_map,
        types=DEVICE_TYPES,
        statuses=DEVICE_STATUSES,
        worker=worker_status(),
        incidents=list_incidents(status="OPEN", limit=20),
        warnings=list_monitoring_warnings(status="OPEN", limit=20),
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
    checks = list_health_checks(device_id=device_id, limit=200)
    history = camera_history_payload(device, limit=200)
    last = last_successful_check(device_id)
    return render_template(
        "device_history.html",
        device=device,
        checks=checks,
        history=history,
        last_working=last,
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
    checks = list_health_checks(limit=200)
    return render_template(
        "health_checks.html",
        checks=checks,
        worker=worker_status(),
        warnings=list_monitoring_warnings(status="OPEN", limit=50),
    )


@app.get("/warnings")
def warnings_page():
    return render_template(
        "warnings.html",
        warnings=list_monitoring_warnings(status=None, limit=100),
        worker=worker_status(),
    )


@app.get("/media/<path:subpath>")
def media(subpath: str):
    return send_from_directory(Path(__file__).resolve().parent.parent / "data", subpath)


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
