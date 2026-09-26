"""Monitoring Worker — clock-aligned hourly + random health checks with image history.

Runs as a standalone process (does not need the web dashboard open):
  python -m src.worker
"""

from __future__ import annotations

import json
import logging
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any

from dotenv import load_dotenv

from src.core import load_settings, send_telegram
from src.db import (
    ROOT,
    apply_check_to_device,
    close_open_incidents,
    get_device,
    init_db,
    list_devices,
    open_incident,
    seed_from_cameras_yaml,
)
from src.health_store import (
    ensure_phase3_schema,
    has_check_in_window,
    insert_health_check,
    list_monitoring_warnings,
    local_hour_window,
    new_health_check_id,
    purge_expired_check_images,
    resolve_missed_check_warnings,
    scan_missed_mandatory_checks,
)
from src.icmp import icmp_ping
from src.nvr_catalog import ensure_nvr_sync
from src.nvr_streams import ensure_stream_discovery
from src.streaming import capture_preview_for_check, save_check_image

log = logging.getLogger("camera-uptime.worker")

_worker_started = False
_worker_lock = threading.Lock()
_last_cycle: dict[str, Any] = {"at": None, "checked": 0, "due": 0}
_heartbeat_path = ROOT / "data" / "worker_heartbeat.txt"
_scheduler_state_path = ROOT / "data" / "scheduler_state.json"

MANDATORY_INTERVAL_SEC = 3600


def mandatory_interval_sec() -> int:
    load_dotenv(ROOT / ".env", override=True)
    return MANDATORY_INTERVAL_SEC


def health_check_concurrency() -> int:
    load_dotenv(ROOT / ".env", override=True)
    try:
        return max(1, min(32, int(os.getenv("HEALTH_CHECK_CONCURRENCY", "12"))))
    except ValueError:
        return 12


def write_heartbeat() -> None:
    _heartbeat_path.parent.mkdir(parents=True, exist_ok=True)
    _heartbeat_path.write_text(
        time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), encoding="utf-8"
    )


def _load_scheduler_state() -> dict[str, Any]:
    if not _scheduler_state_path.is_file():
        return {}
    try:
        return json.loads(_scheduler_state_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_scheduler_state(state: dict[str, Any]) -> None:
    _scheduler_state_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = _scheduler_state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, indent=2), encoding="utf-8")
    temporary.replace(_scheduler_state_path)


def _active_cameras() -> list[dict[str, Any]]:
    return [
        d
        for d in list_devices(include_inactive=False)
        if d.get("isActive") and d.get("deviceType") == "CAMERA"
    ]


def process_device(
    device: dict[str, Any],
    *,
    check_kind: str = "MANDATORY",
    interval_sec: int | None = None,
    capture_image: bool = True,
) -> dict[str, Any]:
    """
    ICMP check (with one confirmation on failure), optional preview capture,
    one healthChecks history row, and device status update.
    """
    kind = (check_kind or "MANDATORY").upper()
    interval = interval_sec or mandatory_interval_sec()
    settings = load_settings()
    prev_status = device.get("status")
    device_id = device["deviceId"]

    first = icmp_ping(
        device["ipAddress"],
        count=4,
        timeout_ms=settings.icmp_timeout_ms,
    )
    if first.success:
        final = first
    else:
        final = icmp_ping(
            device["ipAddress"],
            count=2,
            timeout_ms=settings.icmp_timeout_ms,
        )

    status = "ONLINE" if final.success else "OFFLINE"
    preview_status = "SKIPPED"
    preview_error = None
    image_path = None
    check_id = new_health_check_id()

    if capture_image:
        jpeg, preview_error = capture_preview_for_check(device)
        if jpeg:
            try:
                image_path = save_check_image(device_id, check_id, jpeg)
                preview_status = "AVAILABLE"
                preview_error = None
            except Exception as exc:
                preview_status = "UNAVAILABLE"
                preview_error = f"image store failed: {exc}"
        else:
            preview_status = "UNAVAILABLE"
            preview_error = preview_error or "preview unavailable"

    hc = insert_health_check(
        device_id=device_id,
        check_type="ICMP",
        checked_at=final.checked_at,
        success=final.success,
        response_time=final.response_time_ms,
        packet_loss=final.packet_loss_pct,
        result=status,
        error=final.error,
        check_kind=kind,
        preview_status=preview_status,
        image_path=image_path,
        network_status=status,
        nvr_id=device.get("nvrId"),
        ip_address=device.get("ipAddress"),
        device_name=device.get("deviceName"),
        preview_error=preview_error,
        check_id=check_id,
    )

    apply_check_to_device(
        device_id,
        status=status,
        checked_at=final.checked_at,
        response_time=final.response_time_ms,
        packet_loss=final.packet_loss_pct,
        error=final.error,
        interval_sec=interval,
    )

    incident = None
    if status == "OFFLINE":
        incident = open_incident(device_id, reason="ICMP_FAIL", error=final.error)
        if prev_status != "OFFLINE":
            send_telegram(
                settings,
                f"DEVICE OFFLINE\n{device.get('deviceName')} ({device.get('deviceType')})\n"
                f"IP: {device.get('ipAddress')}\nKind: {kind}\n"
                f"Error: {final.error or 'ping failed'}\nTime: {final.checked_at}",
            )
    else:
        closed = close_open_incidents(device_id)
        if kind == "MANDATORY":
            resolve_missed_check_warnings(device_id)
        if closed and prev_status == "OFFLINE" and settings.recovery_notify:
            send_telegram(
                settings,
                f"DEVICE BACK ONLINE\n{device.get('deviceName')} ({device.get('deviceType')})\n"
                f"IP: {device.get('ipAddress')}\nRTT: {final.response_time_ms} ms\n"
                f"Time: {final.checked_at}",
            )

    log.info(
        "%s %s %s -> %s preview=%s image=%s rtt=%s",
        kind,
        device_id,
        device["ipAddress"],
        status,
        preview_status,
        "yes" if image_path else "no",
        final.response_time_ms,
    )
    return {
        "deviceId": device_id,
        "status": status,
        "previewStatus": preview_status,
        "checkKind": kind,
        "healthCheck": hc,
        "incident": incident,
        "icmp": {
            "success": final.success,
            "responseTime": final.response_time_ms,
            "packetLoss": final.packet_loss_pct,
            "checkedAt": final.checked_at,
            "error": final.error,
        },
    }


def run_full_health_cycle(
    *,
    check_kind: str = "MANDATORY",
    capture_images: bool = True,
    skip_existing_in_hour: bool = True,
) -> dict[str, Any]:
    """Ping + preview every active camera concurrently (NVR-throttled)."""
    init_db()
    ensure_phase3_schema()
    seed_from_cameras_yaml()
    ensure_nvr_sync()
    ensure_stream_discovery()

    kind = (check_kind or "MANDATORY").upper()
    interval = mandatory_interval_sec()
    hour_key, start_utc, end_utc = local_hour_window()
    cameras = _active_cameras()

    due: list[dict[str, Any]] = []
    skipped = 0
    for device in cameras:
        if skip_existing_in_hour and has_check_in_window(
            device["deviceId"], kind, start_utc, end_utc
        ):
            skipped += 1
            continue
        due.append(device)

    results: list[dict[str, Any]] = []
    workers = health_check_concurrency()
    log.info(
        "Starting %s cycle hour=%s cameras=%s due=%s skipped=%s workers=%s",
        kind,
        hour_key,
        len(cameras),
        len(due),
        skipped,
        workers,
    )

    def _one(device: dict[str, Any]) -> dict[str, Any] | None:
        try:
            return process_device(
                device,
                check_kind=kind,
                interval_sec=interval,
                capture_image=capture_images,
            )
        except Exception:
            log.exception("Failed processing %s", device.get("deviceId"))
            return None

    if due:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_one, device) for device in due]
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)

    missed = []
    if kind == "MANDATORY":
        try:
            missed = scan_missed_mandatory_checks(max_age_sec=interval + 300)
            if missed:
                log.warning("MISSED CHECK warnings open: %s", len(missed))
        except Exception:
            log.exception("Missed-check scan failed")

    write_heartbeat()
    summary = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "hourKey": hour_key,
        "due": len(due),
        "skipped": skipped,
        "checked": len(results),
        "online": sum(1 for r in results if r["status"] == "ONLINE"),
        "offline": sum(1 for r in results if r["status"] == "OFFLINE"),
        "previewAvailable": sum(1 for r in results if r.get("previewStatus") == "AVAILABLE"),
        "previewUnavailable": sum(
            1 for r in results if r.get("previewStatus") == "UNAVAILABLE"
        ),
        "checkKind": kind,
        "missed_warnings": len(missed),
        "results": results,
    }
    _last_cycle.clear()
    _last_cycle.update(summary)
    log.info(
        "Finished %s cycle checked=%s online=%s offline=%s preview_ok=%s",
        kind,
        summary["checked"],
        summary["online"],
        summary["offline"],
        summary["previewAvailable"],
    )
    try:
        from src.dashboard_publish import publish_dashboard_snapshot

        pub = publish_dashboard_snapshot(worker=worker_status())
        if pub.get("ok"):
            log.info(
                "Dashboard published to Bunny at=%s cameras=%s",
                pub.get("publishedAt"),
                pub.get("cameras"),
            )
        else:
            log.warning("Dashboard publish skipped: %s", pub.get("reason"))
    except Exception:
        log.exception("Dashboard publish to Bunny failed")
    return summary


def run_due_cycle(force_all: bool = False, check_kind: str = "MANDATORY") -> dict[str, Any]:
    """Back-compat entry used by the web app / manual triggers."""
    return run_full_health_cycle(
        check_kind=check_kind,
        capture_images=True,
        skip_existing_in_hour=not force_all,
    )


def run_random_sample(limit: int = 1) -> dict[str, Any]:
    """Legacy helper: random sample of cameras (kept for compatibility)."""
    active = _active_cameras()
    if not active:
        return {"checked": 0, "results": []}
    sample = random.sample(active, k=min(limit, len(active)))
    results = [
        process_device(d, check_kind="RANDOM", interval_sec=mandatory_interval_sec())
        for d in sample
    ]
    return {"checked": len(results), "results": results}


def worker_status() -> dict[str, Any]:
    hb = None
    if _heartbeat_path.exists():
        hb = _heartbeat_path.read_text(encoding="utf-8").strip()
    state = _load_scheduler_state()
    return {
        "running": _worker_started or _is_heartbeat_fresh(hb),
        "standalone_hint": "python -m src.worker",
        "heartbeat": hb,
        "scheduler": {
            "hourlyDone": state.get("hourly_done"),
            "randomDone": state.get("random_done"),
            "randomMinute": state.get("random_minute"),
        },
        "last_cycle": {
            "at": _last_cycle.get("at"),
            "due": _last_cycle.get("due"),
            "checked": _last_cycle.get("checked"),
            "online": _last_cycle.get("online"),
            "offline": _last_cycle.get("offline"),
            "checkKind": _last_cycle.get("checkKind"),
            "missed_warnings": _last_cycle.get("missed_warnings"),
        },
        "mandatory_interval_sec": mandatory_interval_sec(),
        "open_warnings": len(list_monitoring_warnings(status="OPEN", limit=100)),
    }


def _is_heartbeat_fresh(hb: str | None, max_age_sec: int = 120) -> bool:
    if not hb:
        return False
    try:
        dt = datetime.strptime(hb, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds() <= max_age_sec
    except ValueError:
        return False


def _pick_random_minute(avoid_minute: int | None = None) -> int:
    """Pick a minute in 5..55 so it does not collide with the :00 hourly job."""
    choices = [m for m in range(5, 56) if m != avoid_minute]
    return random.choice(choices)


def _worker_loop() -> None:
    log.info(
        "Clock-aligned health scheduler active (hourly@:00 + 1 random full cycle/hour)"
    )
    state = _load_scheduler_state()

    while True:
        try:
            now = datetime.now().astimezone()
            hour_key, _start, _end = local_hour_window(now)
            minute = now.minute

            if state.get("hour_key") != hour_key:
                state["hour_key"] = hour_key
                state["random_minute"] = _pick_random_minute()
                # Keep prior done markers only if they match this hour
                if state.get("hourly_done") != hour_key:
                    state["hourly_done"] = None
                if state.get("random_done") != hour_key:
                    state["random_done"] = None
                _save_scheduler_state(state)
                log.info(
                    "Hour %s — random check scheduled at minute %s",
                    hour_key,
                    state["random_minute"],
                )

            # Catch-up or on-the-hour scheduled mandatory cycle
            need_hourly = state.get("hourly_done") != hour_key
            if need_hourly and (minute == 0 or minute >= 1):
                # At minute 0 run immediately; after that, catch up if missed :00
                if minute == 0 or state.get("hourly_done") != hour_key:
                    log.info("Running scheduled hourly (MANDATORY) cycle for %s", hour_key)
                    run_full_health_cycle(
                        check_kind="MANDATORY",
                        capture_images=True,
                        skip_existing_in_hour=True,
                    )
                    state["hourly_done"] = hour_key
                    if not state.get("random_minute"):
                        state["random_minute"] = _pick_random_minute()
                    _save_scheduler_state(state)

            # One full-fleet random cycle at the chosen minute
            random_minute = int(state.get("random_minute") or _pick_random_minute())
            state["random_minute"] = random_minute
            if (
                state.get("hourly_done") == hour_key
                and state.get("random_done") != hour_key
                and minute >= random_minute
            ):
                log.info(
                    "Running random health cycle for %s at minute %s",
                    hour_key,
                    random_minute,
                )
                run_full_health_cycle(
                    check_kind="RANDOM",
                    capture_images=True,
                    skip_existing_in_hour=True,
                )
                state["random_done"] = hour_key
                _save_scheduler_state(state)

            # Once per local day: delete expired images only (keep health-check rows)
            day_key = now.strftime("%Y-%m-%d")
            if state.get("image_purge_day") != day_key and minute >= 10:
                try:
                    summary = purge_expired_check_images()
                    log.info(
                        "Image retention purge: deleted=%s cleared=%s failed=%s cutoff=%s",
                        summary.get("deleted"),
                        summary.get("cleared"),
                        summary.get("failed"),
                        summary.get("cutoff"),
                    )
                    state["image_purge_day"] = day_key
                    _save_scheduler_state(state)
                except Exception:
                    log.exception("Image retention purge failed")

            write_heartbeat()
        except Exception:
            log.exception("Worker scheduler tick failed")
        time.sleep(10)


def ensure_worker() -> None:
    """Embedded worker (optional). Prefer standalone `python -m src.worker`."""
    global _worker_started
    with _worker_lock:
        if _worker_started:
            return
        hb = (
            _heartbeat_path.read_text(encoding="utf-8").strip()
            if _heartbeat_path.exists()
            else None
        )
        if _is_heartbeat_fresh(hb, max_age_sec=90):
            log.info("Standalone worker heartbeat fresh — skipping embedded worker")
            return
        t = threading.Thread(target=_worker_loop, name="monitoring-worker", daemon=True)
        t.start()
        _worker_started = True
        log.info("Embedded monitoring worker started")


def run_check_now(device_id: str, check_kind: str = "MANUAL") -> dict[str, Any]:
    device = get_device(device_id)
    if not device:
        raise KeyError(device_id)
    return process_device(device, check_kind=check_kind, capture_image=True)


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    init_db()
    ensure_phase3_schema()
    seed_from_cameras_yaml()
    ensure_nvr_sync()
    global _worker_started
    _worker_started = True
    log.info("Standalone Monitoring Worker (dashboard-independent)")
    _worker_loop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
