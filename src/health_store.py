"""healthChecks + monitoringWarnings store (Phase 2–3) with check image history."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from src.db import connect, init_db, row_to_dict, utc_now, utc_now_dt, parse_utc

CHECK_KINDS = ("MANDATORY", "RANDOM", "MANUAL", "CONFIRMATION")
PREVIEW_STATUSES = ("AVAILABLE", "UNAVAILABLE", "SKIPPED")

_HISTORY_COLUMNS = (
    ("checkKind", "TEXT DEFAULT 'MANDATORY'"),
    ("previewStatus", "TEXT"),
    ("imagePath", "TEXT"),
    ("networkStatus", "TEXT"),
    ("nvrId", "TEXT"),
    ("ipAddress", "TEXT"),
    ("deviceName", "TEXT"),
    ("previewError", "TEXT"),
)


def ensure_phase3_schema() -> None:
    init_db()
    with connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(healthChecks)").fetchall()}
        for name, decl in _HISTORY_COLUMNS:
            if name not in cols:
                conn.execute(f"ALTER TABLE healthChecks ADD COLUMN {name} {decl}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS monitoringWarnings (
                warningId TEXT PRIMARY KEY,
                deviceId TEXT,
                warningType TEXT NOT NULL,
                message TEXT,
                status TEXT NOT NULL DEFAULT 'OPEN',
                createdAt TEXT NOT NULL,
                resolvedAt TEXT,
                CHECK (status IN ('OPEN','RESOLVED')),
                CHECK (warningType IN ('MISSED_CHECK','WORKER_STALE'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_warn_open ON monitoringWarnings(status, warningType)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hc_kind_time ON healthChecks(deviceId, checkKind, checkedAt DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hc_device_kind_hour ON healthChecks(deviceId, checkKind, checkedAt)"
        )
        conn.commit()


def new_health_check_id() -> str:
    return f"hc_{uuid.uuid4().hex[:12]}"


def insert_health_check(
    *,
    device_id: str,
    check_type: str,
    checked_at: str,
    success: bool,
    response_time: float | None,
    packet_loss: float | None,
    result: str | None,
    error: str | None,
    check_kind: str = "MANDATORY",
    preview_status: str | None = None,
    image_path: str | None = None,
    network_status: str | None = None,
    nvr_id: str | None = None,
    ip_address: str | None = None,
    device_name: str | None = None,
    preview_error: str | None = None,
    check_id: str | None = None,
) -> dict[str, Any]:
    ensure_phase3_schema()
    kind = (check_kind or "MANDATORY").upper()
    if kind not in CHECK_KINDS:
        kind = "MANDATORY"
    preview = (preview_status or "SKIPPED").upper()
    if preview not in PREVIEW_STATUSES:
        preview = "SKIPPED"
    network = (network_status or result or ("ONLINE" if success else "OFFLINE")).upper()
    hc_id = check_id or new_health_check_id()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO healthChecks (
                id, deviceId, checkType, checkedAt, success,
                responseTime, packetLoss, result, error, checkKind,
                previewStatus, imagePath, networkStatus, nvrId, ipAddress, deviceName, previewError
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                hc_id,
                device_id,
                check_type,
                checked_at,
                1 if success else 0,
                response_time,
                packet_loss,
                result,
                error,
                kind,
                preview,
                image_path,
                network,
                nvr_id,
                ip_address,
                device_name,
                preview_error,
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT * FROM healthChecks WHERE id = ?", (hc_id,)
        ).fetchone()
    return row_to_dict(row)  # type: ignore


def get_health_check(hc_id: str) -> dict[str, Any] | None:
    ensure_phase3_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM healthChecks WHERE id = ?", (hc_id,)
        ).fetchone()
    return row_to_dict(row)


def last_mandatory_check(device_id: str) -> dict[str, Any] | None:
    ensure_phase3_schema()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM healthChecks
            WHERE deviceId = ? AND checkKind = 'MANDATORY'
            ORDER BY checkedAt DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
    return row_to_dict(row)


def has_check_in_window(
    device_id: str,
    check_kind: str,
    start_utc: str,
    end_utc: str,
) -> bool:
    """True if a non-confirmation check of this kind already exists in [start, end)."""
    ensure_phase3_schema()
    kind = (check_kind or "MANDATORY").upper()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT id FROM healthChecks
            WHERE deviceId = ?
              AND checkKind = ?
              AND checkedAt >= ?
              AND checkedAt < ?
            LIMIT 1
            """,
            (device_id, kind, start_utc, end_utc),
        ).fetchone()
    return row is not None


def list_health_checks(
    device_id: str | None = None,
    limit: int = 100,
    *,
    include_confirmation: bool = False,
) -> list[dict[str, Any]]:
    ensure_phase3_schema()
    with connect() as conn:
        if device_id:
            if include_confirmation:
                rows = conn.execute(
                    """
                    SELECT * FROM healthChecks
                    WHERE deviceId = ?
                    ORDER BY checkedAt DESC
                    LIMIT ?
                    """,
                    (device_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM healthChecks
                    WHERE deviceId = ?
                      AND IFNULL(checkKind, 'MANDATORY') != 'CONFIRMATION'
                    ORDER BY checkedAt DESC
                    LIMIT ?
                    """,
                    (device_id, limit),
                ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT * FROM healthChecks
                WHERE IFNULL(checkKind, 'MANDATORY') != 'CONFIRMATION'
                ORDER BY checkedAt DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def camera_history_payload(device: dict[str, Any], limit: int = 72) -> dict[str, Any]:
    """Timeline payload for camera detail UI."""
    checks = list_health_checks(device_id=device.get("deviceId"), limit=limit)
    items = []
    for c in checks:
        kind = (c.get("checkKind") or "MANDATORY").upper()
        network = (c.get("networkStatus") or c.get("result") or ("ONLINE" if c.get("success") else "OFFLINE")).upper()
        preview = (c.get("previewStatus") or "SKIPPED").upper()
        if network == "ONLINE" and preview == "UNAVAILABLE":
            overall = "PREVIEW_ERROR"
        elif network == "ONLINE":
            overall = "ONLINE"
        elif network == "OFFLINE":
            overall = "OFFLINE"
        else:
            overall = network
        items.append(
            {
                "id": c.get("id"),
                "checkedAt": c.get("checkedAt"),
                "checkKind": kind,
                "checkKindLabel": (
                    "Scheduled Hourly Check"
                    if kind == "MANDATORY"
                    else "Random Health Check"
                    if kind == "RANDOM"
                    else "Manual Check"
                    if kind == "MANUAL"
                    else kind.title()
                ),
                "networkStatus": network,
                "previewStatus": preview,
                "overallStatus": overall,
                "pingSuccess": bool(c.get("success")),
                "responseTimeMs": c.get("responseTime"),
                "packetLossPct": c.get("packetLoss"),
                "error": c.get("error"),
                "previewError": c.get("previewError"),
                "hasImage": bool(c.get("imagePath")),
                "imagePath": c.get("imagePath"),
                "nvrId": c.get("nvrId") or device.get("nvrId"),
                "ipAddress": c.get("ipAddress") or device.get("ipAddress"),
                "deviceName": c.get("deviceName") or device.get("deviceName"),
            }
        )
    return {
        "deviceId": device.get("deviceId"),
        "deviceName": device.get("deviceName"),
        "nvrId": device.get("nvrId"),
        "ipAddress": device.get("ipAddress"),
        "items": items,
    }


def open_missed_check_warning(device_id: str, message: str) -> dict[str, Any]:
    ensure_phase3_schema()
    with connect() as conn:
        existing = conn.execute(
            """
            SELECT * FROM monitoringWarnings
            WHERE deviceId = ? AND warningType = 'MISSED_CHECK' AND status = 'OPEN'
            ORDER BY createdAt DESC LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE monitoringWarnings SET message = ? WHERE warningId = ?",
                (message, existing["warningId"]),
            )
            conn.commit()
            return row_to_dict(
                conn.execute(
                    "SELECT * FROM monitoringWarnings WHERE warningId = ?",
                    (existing["warningId"],),
                ).fetchone()
            )  # type: ignore
        wid = f"warn_{uuid.uuid4().hex[:10]}"
        now = utc_now()
        conn.execute(
            """
            INSERT INTO monitoringWarnings
            (warningId, deviceId, warningType, message, status, createdAt)
            VALUES (?, ?, 'MISSED_CHECK', ?, 'OPEN', ?)
            """,
            (wid, device_id, message, now),
        )
        conn.commit()
        return row_to_dict(
            conn.execute(
                "SELECT * FROM monitoringWarnings WHERE warningId = ?", (wid,)
            ).fetchone()
        )  # type: ignore


def resolve_missed_check_warnings(device_id: str) -> int:
    ensure_phase3_schema()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE monitoringWarnings
            SET status = 'RESOLVED', resolvedAt = ?
            WHERE deviceId = ? AND warningType = 'MISSED_CHECK' AND status = 'OPEN'
            """,
            (utc_now(), device_id),
        )
        conn.commit()
        return cur.rowcount


def list_monitoring_warnings(
    status: str | None = "OPEN", limit: int = 50
) -> list[dict[str, Any]]:
    ensure_phase3_schema()
    with connect() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT w.*, d.deviceName, d.ipAddress
                FROM monitoringWarnings w
                LEFT JOIN devices d ON d.deviceId = w.deviceId
                WHERE w.status = ?
                ORDER BY w.createdAt DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT w.*, d.deviceName, d.ipAddress
                FROM monitoringWarnings w
                LEFT JOIN devices d ON d.deviceId = w.deviceId
                ORDER BY w.createdAt DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def scan_missed_mandatory_checks(max_age_sec: int = 3600) -> list[dict[str, Any]]:
    """
    If last MANDATORY check is older than max_age_sec (default 1 hour),
    raise MISSED CHECK warning.
    """
    from src.db import list_devices

    ensure_phase3_schema()
    now = utc_now_dt()
    warnings = []
    for d in list_devices(include_inactive=False):
        if not d.get("isActive"):
            continue
        if d.get("deviceType") != "CAMERA":
            continue
        if d.get("status") == "UNDER_MAINTENANCE":
            continue
        last = last_mandatory_check(d["deviceId"])
        last_dt = parse_utc(last["checkedAt"]) if last else None
        age = (now - last_dt).total_seconds() if last_dt else None
        if last_dt is None or age > max_age_sec:
            age_min = int((age or 0) / 60) if age is not None else None
            msg = (
                f"MISSED CHECK: no MANDATORY ICMP in the last hour for {d['deviceName']} "
                f"({d['deviceId']}). "
                + (
                    f"Last mandatory: {last['checkedAt']} (~{age_min} min ago)."
                    if last
                    else "No mandatory check recorded yet."
                )
            )
            w = open_missed_check_warning(d["deviceId"], msg)
            warnings.append(w)
        else:
            resolve_missed_check_warnings(d["deviceId"])
    return warnings


def local_hour_window(now: datetime | None = None) -> tuple[str, str, str]:
    """Return (hour_key, start_utc_iso, end_utc_iso) for the current local hour."""
    local = now or datetime.now().astimezone()
    start_local = local.replace(minute=0, second=0, microsecond=0)
    end_local = start_local + timedelta(hours=1)
    start_utc = start_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    end_utc = end_local.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    hour_key = start_local.strftime("%Y-%m-%dT%H")
    return hour_key, start_utc, end_utc


def purge_expired_check_images(*, retention_days: int | None = None) -> dict[str, int]:
    """Delete image files older than retention; keep health-check rows intact.

    Clears imagePath / sets preview note so history remains without the JPEG.
    """
    from src.bunny_storage import (
        bunny_configured,
        bunny_settings,
        delete_object,
        is_bunny_path,
    )
    from src.db import ROOT

    ensure_phase3_schema()
    days = retention_days
    if days is None:
        days = bunny_settings()["retention_days"] if bunny_configured() else 30
    cutoff = (utc_now_dt() - timedelta(days=max(1, int(days)))).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )

    deleted = 0
    cleared = 0
    failed = 0
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, deviceId, imagePath
            FROM healthChecks
            WHERE imagePath IS NOT NULL
              AND TRIM(imagePath) != ''
              AND checkedAt < ?
            ORDER BY checkedAt ASC
            LIMIT 2000
            """,
            (cutoff,),
        ).fetchall()

        for row in rows:
            image_path = row["imagePath"]
            ok = False
            if is_bunny_path(image_path):
                ok = delete_object(image_path)
            else:
                local = ROOT / image_path
                try:
                    if local.is_file():
                        local.unlink()
                    ok = True
                except OSError:
                    ok = False

            if ok:
                deleted += 1
                conn.execute(
                    """
                    UPDATE healthChecks
                    SET imagePath = NULL,
                        previewError = COALESCE(previewError, ?)
                    WHERE id = ?
                    """,
                    (
                        f"Image removed after {days}-day retention; check record retained",
                        row["id"],
                    ),
                )
                cleared += 1
            else:
                failed += 1
        conn.commit()

    return {
        "retentionDays": int(days),
        "cutoff": cutoff,
        "candidates": len(rows),
        "deleted": deleted,
        "cleared": cleared,
        "failed": failed,
    }
