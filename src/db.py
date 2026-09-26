"""Device Master + healthChecks + incidents (Phase 1–2)."""

from __future__ import annotations

import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data" / "devices.db"
UPLOAD_DIR = ROOT / "data" / "uploads" / "devices"

DEVICE_TYPES = ("CAMERA", "NVR", "IP_PHONE")
DEVICE_STATUSES = ("ONLINE", "OFFLINE", "WARNING", "UNDER_MAINTENANCE", "UNKNOWN")


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def utc_now_dt() -> datetime:
    return datetime.now(timezone.utc)


def parse_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, decl: str) -> None:
    cols = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if column not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")


def init_db() -> None:
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS devices (
                deviceId TEXT PRIMARY KEY,
                deviceName TEXT NOT NULL,
                deviceType TEXT NOT NULL,
                ipAddress TEXT NOT NULL,
                location TEXT,
                referenceImage TEXT,
                status TEXT NOT NULL DEFAULT 'UNKNOWN',
                department TEXT,
                building TEXT,
                floor TEXT,
                nvrId TEXT,
                cameraChannel TEXT,
                installationDate TEXT,
                maintenanceNotes TEXT,
                isActive INTEGER NOT NULL DEFAULT 1,
                createdAt TEXT NOT NULL,
                updatedAt TEXT NOT NULL,
                CHECK (deviceType IN ('CAMERA','NVR','IP_PHONE')),
                CHECK (status IN ('ONLINE','OFFLINE','WARNING','UNDER_MAINTENANCE','UNKNOWN'))
            )
            """
        )
        # Phase 2 scheduling / last-success columns
        for col, decl in (
            ("nextCheckAt", "TEXT"),
            ("lastCheckAt", "TEXT"),
            ("lastSuccessAt", "TEXT"),
            ("checkIntervalSec", "INTEGER"),
            ("lastResponseTimeMs", "REAL"),
            ("lastPacketLossPct", "REAL"),
            ("lastError", "TEXT"),
            ("manufacturer", "TEXT"),
            ("servicePort", "INTEGER"),
            ("rtspUrl", "TEXT"),
            ("nvrHost", "TEXT"),
            ("nvrChannel", "INTEGER"),
        ):
            _ensure_column(conn, "devices", col, decl)

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS healthChecks (
                id TEXT PRIMARY KEY,
                deviceId TEXT NOT NULL,
                checkType TEXT NOT NULL,
                checkedAt TEXT NOT NULL,
                success INTEGER NOT NULL,
                responseTime REAL,
                packetLoss REAL,
                result TEXT,
                error TEXT,
                FOREIGN KEY (deviceId) REFERENCES devices(deviceId)
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hc_device_time ON healthChecks(deviceId, checkedAt DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_hc_success ON healthChecks(deviceId, success, checkedAt DESC)"
        )

        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS incidents (
                incidentId TEXT PRIMARY KEY,
                deviceId TEXT NOT NULL,
                status TEXT NOT NULL,
                reason TEXT,
                openedAt TEXT NOT NULL,
                closedAt TEXT,
                lastError TEXT,
                FOREIGN KEY (deviceId) REFERENCES devices(deviceId),
                CHECK (status IN ('OPEN','CLOSED'))
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inc_open ON incidents(deviceId, status)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_devices_active ON devices(isActive)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_devices_next ON devices(nextCheckAt)"
        )
        conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    if "isActive" in d:
        d["isActive"] = bool(d.get("isActive"))
    if "success" in d and d["success"] is not None:
        d["success"] = bool(d.get("success"))
    return d


def list_devices(include_inactive: bool = True) -> list[dict[str, Any]]:
    init_db()
    q = "SELECT * FROM devices"
    if not include_inactive:
        q += " WHERE isActive = 1"
    q += " ORDER BY deviceType, deviceName"
    with connect() as conn:
        rows = conn.execute(q).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def get_device(device_id: str) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM devices WHERE deviceId = ?", (device_id,)
        ).fetchone()
    return row_to_dict(row)


def create_device(payload: dict[str, Any]) -> dict[str, Any]:
    init_db()
    now = utc_now()
    device_id = payload.get("deviceId") or f"dev_{uuid.uuid4().hex[:10]}"
    dtype = str(payload.get("deviceType") or "CAMERA").upper()
    if dtype not in DEVICE_TYPES:
        raise ValueError(f"Invalid deviceType: {dtype}")
    status = str(payload.get("status") or "UNKNOWN").upper()
    if status not in DEVICE_STATUSES:
        status = "UNKNOWN"

    fields = {
        "deviceId": device_id,
        "deviceName": str(payload.get("deviceName") or "").strip(),
        "deviceType": dtype,
        "ipAddress": str(payload.get("ipAddress") or "").strip(),
        "location": (payload.get("location") or "") or None,
        "referenceImage": payload.get("referenceImage"),
        "status": status,
        "department": (payload.get("department") or "") or None,
        "building": (payload.get("building") or "") or None,
        "floor": (payload.get("floor") or "") or None,
        "nvrId": (payload.get("nvrId") or "") or None,
        "cameraChannel": (payload.get("cameraChannel") or "") or None,
        "installationDate": (payload.get("installationDate") or "") or None,
        "maintenanceNotes": (payload.get("maintenanceNotes") or "") or None,
        "manufacturer": (payload.get("manufacturer") or "") or None,
        "servicePort": payload.get("servicePort"),
        "rtspUrl": (payload.get("rtspUrl") or "") or None,
        "isActive": 1 if payload.get("isActive", True) else 0,
        "createdAt": now,
        "updatedAt": now,
        "nextCheckAt": now,
        "checkIntervalSec": payload.get("checkIntervalSec") or 3600,
    }
    if not fields["deviceName"] or not fields["ipAddress"]:
        raise ValueError("deviceName and ipAddress are required")

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO devices (
                deviceId, deviceName, deviceType, ipAddress, location, referenceImage,
                status, department, building, floor, nvrId, cameraChannel,
                installationDate, maintenanceNotes, manufacturer, servicePort, rtspUrl,
                isActive, createdAt, updatedAt, nextCheckAt, checkIntervalSec
            ) VALUES (
                :deviceId, :deviceName, :deviceType, :ipAddress, :location, :referenceImage,
                :status, :department, :building, :floor, :nvrId, :cameraChannel,
                :installationDate, :maintenanceNotes, :manufacturer, :servicePort, :rtspUrl,
                :isActive, :createdAt, :updatedAt, :nextCheckAt, :checkIntervalSec
            )
            """,
            fields,
        )
        conn.commit()
    return get_device(device_id)  # type: ignore


def update_device(device_id: str, payload: dict[str, Any]) -> dict[str, Any] | None:
    init_db()
    existing = get_device(device_id)
    if not existing:
        return None

    dtype = str(payload.get("deviceType", existing["deviceType"])).upper()
    if dtype not in DEVICE_TYPES:
        raise ValueError(f"Invalid deviceType: {dtype}")
    status = str(payload.get("status", existing["status"])).upper()
    if status not in DEVICE_STATUSES:
        raise ValueError(f"Invalid status: {status}")

    fields = {
        "deviceId": device_id,
        "deviceName": str(payload.get("deviceName", existing["deviceName"])).strip(),
        "deviceType": dtype,
        "ipAddress": str(payload.get("ipAddress", existing["ipAddress"])).strip(),
        "location": payload.get("location", existing.get("location")),
        "referenceImage": payload.get(
            "referenceImage", existing.get("referenceImage")
        ),
        "status": status,
        "department": payload.get("department", existing.get("department")),
        "building": payload.get("building", existing.get("building")),
        "floor": payload.get("floor", existing.get("floor")),
        "nvrId": payload.get("nvrId", existing.get("nvrId")),
        "cameraChannel": payload.get("cameraChannel", existing.get("cameraChannel")),
        "installationDate": payload.get(
            "installationDate", existing.get("installationDate")
        ),
        "maintenanceNotes": payload.get(
            "maintenanceNotes", existing.get("maintenanceNotes")
        ),
        "manufacturer": payload.get("manufacturer", existing.get("manufacturer")),
        "servicePort": payload.get("servicePort", existing.get("servicePort")),
        "rtspUrl": payload.get("rtspUrl", existing.get("rtspUrl")),
        "isActive": 1
        if payload.get("isActive", existing.get("isActive", True))
        else 0,
        "updatedAt": utc_now(),
    }
    for key in (
        "location",
        "department",
        "building",
        "floor",
        "nvrId",
        "cameraChannel",
        "installationDate",
        "maintenanceNotes",
        "referenceImage",
        "manufacturer",
        "rtspUrl",
    ):
        if fields[key] == "":
            fields[key] = None

    with connect() as conn:
        conn.execute(
            """
            UPDATE devices SET
                deviceName=:deviceName, deviceType=:deviceType, ipAddress=:ipAddress,
                location=:location, referenceImage=:referenceImage, status=:status,
                department=:department, building=:building, floor=:floor,
                nvrId=:nvrId, cameraChannel=:cameraChannel,
                installationDate=:installationDate, maintenanceNotes=:maintenanceNotes,
                manufacturer=:manufacturer, servicePort=:servicePort, rtspUrl=:rtspUrl,
                isActive=:isActive, updatedAt=:updatedAt
            WHERE deviceId=:deviceId
            """,
            fields,
        )
        conn.commit()
    return get_device(device_id)


def deactivate_device(device_id: str) -> dict[str, Any] | None:
    return update_device(device_id, {"isActive": False, "status": "UNDER_MAINTENANCE"})


def assign_stream_targets(mapping: dict[str, tuple[str, int]]) -> int:
    """Store NVR host and channel for each camera.

    A manually configured rtspUrl (per-camera override for cameras not reachable
    through the NVR API) is preserved — discovery only updates NVR host/channel.
    """
    if not mapping:
        return 0
    init_db()
    updated = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT deviceId, ipAddress FROM devices WHERE deviceType = 'CAMERA'"
        ).fetchall()
        for row in rows:
            target = mapping.get(row["ipAddress"])
            if not target:
                continue
            host, channel = target
            conn.execute(
                """
                UPDATE devices
                SET nvrHost = ?, nvrChannel = ?, updatedAt = ?
                WHERE deviceId = ?
                """,
                (host, int(channel), utc_now(), row["deviceId"]),
            )
            updated += 1
        conn.commit()
    return updated


def apply_rtsp_overrides_from_yaml() -> int:
    """Apply per-camera `rtsp_url` from cameras.yaml onto matching devices by IP.

    Config-driven so a direct-RTSP override survives NVR re-discovery and DB
    reseeds. Matches on host/IP, so it also reaches cameras that were imported
    from the NVR catalog rather than seeded from cameras.yaml.
    """
    init_db()
    import yaml

    cams_path = ROOT / "cameras.yaml"
    if not cams_path.exists():
        return 0
    raw = yaml.safe_load(cams_path.read_text(encoding="utf-8")) or {}
    overrides: dict[str, str] = {}
    for row in raw.get("cameras") or []:
        host = str(row.get("host") or "").strip()
        url = str(row.get("rtsp_url") or "").strip()
        if host and url:
            overrides[host] = url
    if not overrides:
        return 0
    applied = 0
    with connect() as conn:
        rows = conn.execute(
            "SELECT deviceId, ipAddress, rtspUrl FROM devices WHERE deviceType = 'CAMERA'"
        ).fetchall()
        for row in rows:
            url = overrides.get((row["ipAddress"] or "").strip())
            if not url or row["rtspUrl"] == url:
                continue
            conn.execute(
                "UPDATE devices SET rtspUrl = ?, updatedAt = ? WHERE deviceId = ?",
                (url, utc_now(), row["deviceId"]),
            )
            applied += 1
        conn.commit()
    return applied


def set_device_status(device_id: str, status: str) -> None:
    if status not in DEVICE_STATUSES:
        return
    init_db()
    with connect() as conn:
        conn.execute(
            "UPDATE devices SET status = ?, updatedAt = ? WHERE deviceId = ?",
            (status, utc_now(), device_id),
        )
        conn.commit()


def devices_due_for_check(now: datetime | None = None) -> list[dict[str, Any]]:
    """Active devices whose nextCheckAt is null or <= now."""
    init_db()
    now = now or utc_now_dt()
    now_s = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT * FROM devices
            WHERE isActive = 1
              AND (nextCheckAt IS NULL OR nextCheckAt <= ?)
            ORDER BY nextCheckAt IS NOT NULL, nextCheckAt ASC
            """,
            (now_s,),
        ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


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
) -> dict[str, Any]:
    init_db()
    hc_id = f"hc_{uuid.uuid4().hex[:12]}"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO healthChecks (
                id, deviceId, checkType, checkedAt, success,
                responseTime, packetLoss, result, error
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        conn.commit()
    return get_health_check(hc_id)  # type: ignore


def get_health_check(hc_id: str) -> dict[str, Any] | None:
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM healthChecks WHERE id = ?", (hc_id,)
        ).fetchone()
    return row_to_dict(row)


def list_health_checks(device_id: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if device_id:
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
                ORDER BY checkedAt DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def last_successful_check(device_id: str) -> dict[str, Any] | None:
    """When was this device last working?"""
    init_db()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM healthChecks
            WHERE deviceId = ? AND success = 1
            ORDER BY checkedAt DESC
            LIMIT 1
            """,
            (device_id,),
        ).fetchone()
    return row_to_dict(row)


def apply_check_to_device(
    device_id: str,
    *,
    status: str,
    checked_at: str,
    response_time: float | None,
    packet_loss: float | None,
    error: str | None,
    interval_sec: int,
) -> None:
    init_db()
    nxt = (
        datetime.strptime(checked_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        + timedelta(seconds=max(60, interval_sec))
    ).strftime("%Y-%m-%dT%H:%M:%SZ")
    last_success = checked_at if status == "ONLINE" else None
    with connect() as conn:
        if last_success:
            conn.execute(
                """
                UPDATE devices SET
                    status = ?, lastCheckAt = ?, lastSuccessAt = ?,
                    lastResponseTimeMs = ?, lastPacketLossPct = ?, lastError = ?,
                    nextCheckAt = ?, updatedAt = ?
                WHERE deviceId = ?
                """,
                (
                    status,
                    checked_at,
                    last_success,
                    response_time,
                    packet_loss,
                    error,
                    nxt,
                    utc_now(),
                    device_id,
                ),
            )
        else:
            conn.execute(
                """
                UPDATE devices SET
                    status = ?, lastCheckAt = ?,
                    lastResponseTimeMs = ?, lastPacketLossPct = ?, lastError = ?,
                    nextCheckAt = ?, updatedAt = ?
                WHERE deviceId = ?
                """,
                (
                    status,
                    checked_at,
                    response_time,
                    packet_loss,
                    error,
                    nxt,
                    utc_now(),
                    device_id,
                ),
            )
        conn.commit()


def open_incident(device_id: str, reason: str, error: str | None) -> dict[str, Any] | None:
    init_db()
    with connect() as conn:
        existing = conn.execute(
            """
            SELECT * FROM incidents
            WHERE deviceId = ? AND status = 'OPEN'
            ORDER BY openedAt DESC LIMIT 1
            """,
            (device_id,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE incidents SET lastError = ? WHERE incidentId = ?",
                (error, existing["incidentId"]),
            )
            conn.commit()
            return row_to_dict(
                conn.execute(
                    "SELECT * FROM incidents WHERE incidentId = ?",
                    (existing["incidentId"],),
                ).fetchone()
            )
        iid = f"inc_{uuid.uuid4().hex[:10]}"
        now = utc_now()
        conn.execute(
            """
            INSERT INTO incidents (incidentId, deviceId, status, reason, openedAt, lastError)
            VALUES (?, ?, 'OPEN', ?, ?, ?)
            """,
            (iid, device_id, reason, now, error),
        )
        conn.commit()
        return row_to_dict(
            conn.execute(
                "SELECT * FROM incidents WHERE incidentId = ?", (iid,)
            ).fetchone()
        )


def close_open_incidents(device_id: str) -> int:
    init_db()
    with connect() as conn:
        cur = conn.execute(
            """
            UPDATE incidents SET status = 'CLOSED', closedAt = ?
            WHERE deviceId = ? AND status = 'OPEN'
            """,
            (utc_now(), device_id),
        )
        conn.commit()
        return cur.rowcount


def list_incidents(status: str | None = "OPEN", limit: int = 50) -> list[dict[str, Any]]:
    init_db()
    with connect() as conn:
        if status:
            rows = conn.execute(
                """
                SELECT i.*, d.deviceName, d.ipAddress, d.deviceType
                FROM incidents i
                LEFT JOIN devices d ON d.deviceId = i.deviceId
                WHERE i.status = ?
                ORDER BY i.openedAt DESC
                LIMIT ?
                """,
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT i.*, d.deviceName, d.ipAddress, d.deviceType
                FROM incidents i
                LEFT JOIN devices d ON d.deviceId = i.deviceId
                ORDER BY i.openedAt DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
    return [row_to_dict(r) for r in rows]  # type: ignore


def seed_from_cameras_yaml() -> int:
    init_db()
    import yaml

    cams_path = ROOT / "cameras.yaml"
    if not cams_path.exists():
        return 0
    with connect() as conn:
        n = conn.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE deviceType = 'CAMERA'"
        ).fetchone()["c"]
    if n > 0:
        return 0

    raw = yaml.safe_load(cams_path.read_text(encoding="utf-8")) or {}
    created = 0
    for row in raw.get("cameras") or []:
        if not row.get("enabled", True):
            continue
        host = str(row.get("host") or "").strip()
        if not host:
            continue
        create_device(
            {
                "deviceId": f"cam_{row.get('id')}",
                "deviceName": row.get("name") or row.get("id"),
                "deviceType": "CAMERA",
                "ipAddress": host,
                "location": "Office",
                "cameraChannel": "1",
                "status": "UNKNOWN",
                "isActive": True,
                # Optional full RTSP URL for cameras not reachable via the NVR API.
                "rtspUrl": str(row.get("rtsp_url") or "").strip() or None,
            }
        )
        created += 1
    return created
