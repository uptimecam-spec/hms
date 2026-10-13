"""Users, roles, and location → NVR → camera access.

Authorization is data-driven: roles, permissions, and resource grants live in
SQLite. Adding a user, role, location, NVR, or camera does not require a code
change. Super Admin is the only unrestricted role.
"""

from __future__ import annotations

import copy
import logging
import os
import threading
import uuid
from typing import Any

from werkzeug.security import check_password_hash, generate_password_hash

from src.db import connect, init_db, list_devices, utc_now

log = logging.getLogger("camera-uptime.access")

UNASSIGNED_LOCATION = "Unassigned"
SCOPE_TYPES = ("LOCATION", "NVR", "CAMERA")
_MIN_PASSWORD = 8

PERMISSION_CATALOG: list[dict[str, str]] = [
    {"key": "cameras.view", "label": "View cameras", "group": "Cameras"},
    {"key": "cameras.live", "label": "View live video", "group": "Cameras"},
    {"key": "cameras.ping", "label": "Run camera checks", "group": "Cameras"},
    {"key": "nvr.access", "label": "NVR access", "group": "NVR"},
    {"key": "locations.access", "label": "Location access", "group": "Locations"},
    {"key": "users.manage", "label": "Manage users", "group": "Administration"},
    {"key": "roles.manage", "label": "Manage roles", "group": "Administration"},
    {"key": "devices.manage", "label": "Manage devices", "group": "Administration"},
    {"key": "health.view", "label": "View health checks", "group": "Monitoring"},
    {"key": "warnings.view", "label": "View warnings", "group": "Monitoring"},
    {"key": "dashboard.view", "label": "View ping dashboard", "group": "Monitoring"},
    {"key": "settings.manage", "label": "System settings", "group": "Administration"},
]

_PERMISSION_KEYS = {item["key"] for item in PERMISSION_CATALOG}
_ready = False
_ready_lock = threading.Lock()

ROLE_SUPER = "role_super_admin"
ROLE_ADMIN = "role_admin"
ROLE_OPERATOR = "role_operator"

_ADMIN_PERMS = [item["key"] for item in PERMISSION_CATALOG]
_OPERATOR_PERMS = [
    "cameras.view",
    "cameras.live",
    "nvr.access",
    "locations.access",
]


class AccessError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status = status


def permission_catalog() -> list[dict[str, str]]:
    return [dict(item) for item in PERMISSION_CATALOG]


def ensure_access_schema() -> None:
    """Create tables and seed system roles. Safe to call on every boot."""
    global _ready
    with _ready_lock:
        if _ready:
            return
        init_db()
        with connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS roles (
                    roleId TEXT PRIMARY KEY,
                    name TEXT NOT NULL UNIQUE,
                    description TEXT,
                    isSystem INTEGER NOT NULL DEFAULT 0,
                    unrestricted INTEGER NOT NULL DEFAULT 0,
                    createdAt TEXT NOT NULL,
                    updatedAt TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS role_permissions (
                    roleId TEXT NOT NULL,
                    permissionKey TEXT NOT NULL,
                    PRIMARY KEY (roleId, permissionKey),
                    FOREIGN KEY (roleId) REFERENCES roles(roleId) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    userId TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    passwordHash TEXT NOT NULL,
                    displayName TEXT,
                    details TEXT,
                    roleId TEXT NOT NULL,
                    isActive INTEGER NOT NULL DEFAULT 1,
                    createdAt TEXT NOT NULL,
                    updatedAt TEXT NOT NULL,
                    FOREIGN KEY (roleId) REFERENCES roles(roleId)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS access_grants (
                    userId TEXT NOT NULL,
                    scopeType TEXT NOT NULL,
                    scopeId TEXT NOT NULL,
                    PRIMARY KEY (userId, scopeType, scopeId),
                    FOREIGN KEY (userId) REFERENCES users(userId) ON DELETE CASCADE,
                    CHECK (scopeType IN ('LOCATION','NVR','CAMERA'))
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS access_denies (
                    userId TEXT NOT NULL,
                    deviceId TEXT NOT NULL,
                    PRIMARY KEY (userId, deviceId),
                    FOREIGN KEY (userId) REFERENCES users(userId) ON DELETE CASCADE
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_users_role ON users(roleId)"
            )
            _seed_roles(conn)
            _seed_first_admin(conn)
            conn.commit()
        _ready = True
    _publish_async()


def _seed_roles(conn) -> None:
    now = utc_now()
    system = (
        (
            ROLE_SUPER,
            "Super Admin",
            "Full access to every camera, NVR, location, user, and setting.",
            1,
            _ADMIN_PERMS,
        ),
        (
            ROLE_ADMIN,
            "Admin",
            "Module permissions from this role. Camera visibility follows assigned locations, NVRs, and cameras.",
            0,
            _ADMIN_PERMS,
        ),
        (
            ROLE_OPERATOR,
            "Operator",
            "View assigned cameras and live video only.",
            0,
            _OPERATOR_PERMS,
        ),
    )
    for role_id, name, description, unrestricted, perms in system:
        existing = conn.execute(
            "SELECT roleId FROM roles WHERE roleId = ?", (role_id,)
        ).fetchone()
        if existing is None:
            conn.execute(
                """
                INSERT INTO roles (roleId, name, description, isSystem, unrestricted, createdAt, updatedAt)
                VALUES (?, ?, ?, 1, ?, ?, ?)
                """,
                (role_id, name, description, unrestricted, now, now),
            )
            for key in perms:
                conn.execute(
                    "INSERT OR IGNORE INTO role_permissions (roleId, permissionKey) VALUES (?, ?)",
                    (role_id, key),
                )
    # Super Admin always holds the full catalog, including keys added later.
    conn.execute("DELETE FROM role_permissions WHERE roleId = ?", (ROLE_SUPER,))
    for key in _ADMIN_PERMS:
        conn.execute(
            "INSERT INTO role_permissions (roleId, permissionKey) VALUES (?, ?)",
            (ROLE_SUPER, key),
        )
    conn.execute(
        "UPDATE roles SET unrestricted = 1, isSystem = 1, updatedAt = ? WHERE roleId = ?",
        (now, ROLE_SUPER),
    )


def _seed_first_admin(conn) -> None:
    row = conn.execute("SELECT COUNT(*) AS n FROM users").fetchone()
    if row and int(row["n"]) > 0:
        return
    try:
        from dotenv import load_dotenv

        from src.db import ROOT

        load_dotenv(ROOT / ".env", override=False)
    except Exception:
        log.debug("dotenv not loaded while seeding admin", exc_info=True)
    username = (os.getenv("DASHBOARD_USER") or "admin").strip() or "admin"
    password = (os.getenv("DASHBOARD_PASSWORD") or "").strip()
    if len(password) < _MIN_PASSWORD:
        log.warning(
            "No users exist and DASHBOARD_PASSWORD is missing or shorter than %s characters. "
            "Set it in .env and restart to create the Super Admin.",
            _MIN_PASSWORD,
        )
        return
    now = utc_now()
    user_id = "user_" + uuid.uuid4().hex[:12]
    conn.execute(
        """
        INSERT INTO users (
            userId, username, passwordHash, displayName, details, roleId, isActive, createdAt, updatedAt
        ) VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            user_id,
            username,
            generate_password_hash(password),
            username,
            "Bootstrap Super Admin from dashboard credentials.",
            ROLE_SUPER,
            now,
            now,
        ),
    )
    log.info("Seeded Super Admin account %s", username)


def _publish_async() -> None:
    threading.Thread(target=publish_access_directory, name="access-publish", daemon=True).start()


def location_of(camera: dict[str, Any]) -> str:
    text = str(camera.get("location") or "").strip()
    return text or UNASSIGNED_LOCATION


def nvr_ids_of(camera: dict[str, Any]) -> list[str]:
    groups = camera.get("nvrGroups")
    if isinstance(groups, list) and groups:
        out = [str(item).strip() for item in groups if str(item).strip()]
        if out:
            return out
    for key in ("nvrGroup", "nvrId"):
        raw = str(camera.get(key) or "").strip()
        if raw and raw not in {"—", "-"}:
            parts = [part.strip() for part in raw.split(",") if part.strip()]
            if parts:
                return parts
    return []


def device_access_view(device: dict[str, Any]) -> dict[str, Any]:
    """Normalize a Device Master row into the shape access checks use."""
    from src.camera_status import nvr_groups_for_device

    groups = nvr_groups_for_device(device)
    return {
        "deviceId": device.get("deviceId"),
        "location": device.get("location"),
        "nvrGroups": groups,
        "nvrGroup": groups[0] if groups else None,
        "nvrId": device.get("nvrId"),
        "deviceName": device.get("deviceName"),
        "ipAddress": device.get("ipAddress"),
        "isActive": device.get("isActive"),
        "deviceType": device.get("deviceType"),
    }


def access_index(actor: dict[str, Any] | None) -> dict[str, Any]:
    if not actor:
        return {
            "active": False,
            "unrestricted": False,
            "perms": set(),
            "cameras": set(),
            "nvrs": set(),
            "locations": set(),
            "denies": set(),
        }
    role = actor.get("role") or {}
    perms = set(role.get("permissions") or [])
    cameras: set[str] = set()
    nvrs: set[str] = set()
    locations: set[str] = set()
    for grant in actor.get("grants") or []:
        scope_type = str(grant.get("scopeType") or "")
        scope_id = str(grant.get("scopeId") or "")
        if not scope_id:
            continue
        if scope_type == "CAMERA":
            cameras.add(scope_id)
        elif scope_type == "NVR":
            nvrs.add(scope_id)
        elif scope_type == "LOCATION":
            locations.add(scope_id)
    return {
        "active": bool(actor.get("isActive", True)),
        "unrestricted": bool(role.get("unrestricted")),
        "perms": perms,
        "cameras": cameras,
        "nvrs": nvrs,
        "locations": locations,
        "denies": {str(item) for item in (actor.get("denies") or []) if str(item)},
    }


def has_permission(actor: dict[str, Any] | None, key: str) -> bool:
    if not actor or not actor.get("isActive", True):
        return False
    role = actor.get("role") or {}
    if role.get("unrestricted"):
        return True
    return key in set(role.get("permissions") or [])


def camera_allowed(
    actor: dict[str, Any] | None,
    camera: dict[str, Any],
    index: dict[str, Any] | None = None,
) -> bool:
    """True when this actor may see this camera record or its media."""
    idx = index if index is not None else access_index(actor)
    if not idx["active"]:
        return False
    if idx["unrestricted"]:
        return True
    if "cameras.view" not in idx["perms"]:
        return False
    device_id = str(camera.get("deviceId") or "")
    if not device_id or device_id in idx["denies"]:
        return False
    if device_id in idx["cameras"]:
        return True
    if "nvr.access" in idx["perms"] and idx["nvrs"].intersection(nvr_ids_of(camera)):
        return True
    if "locations.access" in idx["perms"] and location_of(camera) in idx["locations"]:
        return True
    return False


def can_see_device(actor: dict[str, Any] | None, device: dict[str, Any]) -> bool:
    if not actor or not actor.get("isActive", True):
        return False
    if (actor.get("role") or {}).get("unrestricted"):
        return True
    if device.get("deviceType") == "CAMERA":
        return camera_allowed(actor, device_access_view(device))
    if device.get("deviceType") == "NVR":
        return has_permission(actor, "nvr.access") or has_permission(actor, "devices.manage")
    return has_permission(actor, "devices.manage")


def filter_camera_status_payload(payload: dict[str, Any], actor: dict[str, Any] | None) -> dict[str, Any]:
    """Return a copy of a camera-status payload limited to this actor."""
    data = copy.deepcopy(payload)
    index = access_index(actor)
    cameras = [cam for cam in data.get("cameras") or [] if camera_allowed(actor, cam, index)]
    allowed = {cam.get("deviceId") for cam in cameras}
    nvrs_out: list[dict[str, Any]] = []
    for nvr in data.get("nvrs") or []:
        cams = [cam for cam in nvr.get("cameras") or [] if cam.get("deviceId") in allowed]
        if not cams:
            continue
        item = dict(nvr)
        item["cameras"] = cams
        item["total"] = len(cams)
        item["online"] = sum(
            1
            for cam in cams
            if cam.get("networkStatus") == "ONLINE" and not bool(cam.get("frameFrozen"))
        )
        item["offline"] = sum(1 for cam in cams if cam.get("networkStatus") == "OFFLINE")
        item["frozen"] = sum(
            1
            for cam in cams
            if cam.get("networkStatus") == "ONLINE" and bool(cam.get("frameFrozen"))
        )
        item["previewUnavailable"] = sum(
            1
            for cam in cams
            if cam.get("displayStatus") == "ONLINE_PREVIEW_UNAVAILABLE"
        )
        item["unknown"] = (
            item["total"]
            - item["online"]
            - item["offline"]
            - item["frozen"]
        )
        nvrs_out.append(item)
    data["cameras"] = cameras
    data["nvrs"] = nvrs_out
    online = sum(
        1
        for cam in cameras
        if cam.get("networkStatus") == "ONLINE" and not bool(cam.get("frameFrozen"))
    )
    offline = sum(1 for cam in cameras if cam.get("networkStatus") == "OFFLINE")
    frozen = sum(
        1
        for cam in cameras
        if cam.get("networkStatus") == "ONLINE" and bool(cam.get("frameFrozen"))
    )
    preview_unavailable = sum(
        1
        for cam in cameras
        if cam.get("displayStatus") == "ONLINE_PREVIEW_UNAVAILABLE"
    )
    summary = dict(data.get("summary") or {})
    summary["total"] = len(cameras)
    summary["online"] = online
    summary["offline"] = offline
    summary["frozen"] = frozen
    summary["previewUnavailable"] = preview_unavailable
    summary["unknown"] = len(cameras) - online - offline - frozen
    summary["nvrCount"] = len(nvrs_out)
    data["summary"] = summary
    return data


def explain_access(actor: dict[str, Any] | None, cameras: list[dict[str, Any]]) -> dict[str, Any]:
    index = access_index(actor)
    visible = [cam for cam in cameras if camera_allowed(actor, cam, index)]
    locations = sorted({location_of(cam) for cam in visible})
    nvrs: set[str] = set()
    for cam in visible:
        nvrs.update(nvr_ids_of(cam))
    role = (actor or {}).get("role") or {}
    return {
        "unrestricted": bool(role.get("unrestricted")),
        "cameraCount": len(visible),
        "locationCount": len(locations),
        "nvrCount": len(nvrs),
        "locations": locations,
        "nvrs": sorted(nvrs),
        "cameraIds": [cam.get("deviceId") for cam in visible],
        "permissions": permission_catalog()
        if role.get("unrestricted")
        else [item for item in permission_catalog() if item["key"] in set(role.get("permissions") or [])],
    }


def _role_row(conn, role_id: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM roles WHERE roleId = ?", (role_id,)).fetchone()
    if row is None:
        return None
    perms = [
        r["permissionKey"]
        for r in conn.execute(
            "SELECT permissionKey FROM role_permissions WHERE roleId = ? ORDER BY permissionKey",
            (role_id,),
        ).fetchall()
    ]
    return {
        "roleId": row["roleId"],
        "name": row["name"],
        "description": row["description"] or "",
        "isSystem": bool(row["isSystem"]),
        "unrestricted": bool(row["unrestricted"]),
        "permissions": perms,
        "createdAt": row["createdAt"],
        "updatedAt": row["updatedAt"],
    }


def _grants_for(conn, user_id: str) -> list[dict[str, str]]:
    rows = conn.execute(
        """
        SELECT scopeType, scopeId FROM access_grants
        WHERE userId = ?
        ORDER BY scopeType, scopeId
        """,
        (user_id,),
    ).fetchall()
    return [{"scopeType": r["scopeType"], "scopeId": r["scopeId"]} for r in rows]


def _denies_for(conn, user_id: str) -> list[str]:
    rows = conn.execute(
        "SELECT deviceId FROM access_denies WHERE userId = ? ORDER BY deviceId",
        (user_id,),
    ).fetchall()
    return [r["deviceId"] for r in rows]


def _user_from_row(conn, row) -> dict[str, Any]:
    role = _role_row(conn, row["roleId"]) or {
        "roleId": row["roleId"],
        "name": "Unknown",
        "description": "",
        "isSystem": False,
        "unrestricted": False,
        "permissions": [],
    }
    return {
        "userId": row["userId"],
        "username": row["username"],
        "displayName": row["displayName"] or "",
        "details": row["details"] or "",
        "roleId": row["roleId"],
        "isActive": bool(row["isActive"]),
        "createdAt": row["createdAt"],
        "updatedAt": row["updatedAt"],
        "role": role,
        "grants": _grants_for(conn, row["userId"]),
        "denies": _denies_for(conn, row["userId"]),
    }


def list_roles() -> list[dict[str, Any]]:
    ensure_access_schema()
    with connect() as conn:
        rows = conn.execute("SELECT roleId FROM roles ORDER BY unrestricted DESC, name").fetchall()
        return [_role_row(conn, r["roleId"]) for r in rows]  # type: ignore[misc]


def get_role(role_id: str) -> dict[str, Any] | None:
    ensure_access_schema()
    with connect() as conn:
        return _role_row(conn, role_id)


def list_users() -> list[dict[str, Any]]:
    ensure_access_schema()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM users ORDER BY displayName, username").fetchall()
        return [_user_from_row(conn, row) for row in rows]


def get_user(user_id: str) -> dict[str, Any] | None:
    ensure_access_schema()
    with connect() as conn:
        row = conn.execute("SELECT * FROM users WHERE userId = ?", (user_id,)).fetchone()
        if row is None:
            return None
        return _user_from_row(conn, row)


def get_user_by_username(username: str) -> dict[str, Any] | None:
    ensure_access_schema()
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(username) = lower(?)",
            (username.strip(),),
        ).fetchone()
        if row is None:
            return None
        return _user_from_row(conn, row)


def authenticate(username: str, password: str) -> dict[str, Any] | None:
    """Return the actor when the password matches an active account."""
    ensure_access_schema()
    name = (username or "").strip()
    if not name or not password:
        return None
    with connect() as conn:
        row = conn.execute(
            "SELECT * FROM users WHERE lower(username) = lower(?)",
            (name,),
        ).fetchone()
        if row is None:
            return None
        if not row["isActive"]:
            return None
        if not check_password_hash(row["passwordHash"], password):
            return None
        return _user_from_row(conn, row)


def _active_super_count(conn, exclude_user_id: str | None = None) -> int:
    q = """
        SELECT COUNT(*) AS n
        FROM users u
        JOIN roles r ON r.roleId = u.roleId
        WHERE u.isActive = 1 AND r.unrestricted = 1
    """
    args: tuple[Any, ...] = ()
    if exclude_user_id:
        q += " AND u.userId != ?"
        args = (exclude_user_id,)
    row = conn.execute(q, args).fetchone()
    return int(row["n"] if row else 0)


def _normalize_permissions(raw: Any) -> list[str]:
    if not isinstance(raw, list):
        raise AccessError("permissions must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        key = str(item or "").strip()
        if key not in _PERMISSION_KEYS:
            raise AccessError(f"Unknown permission: {key}")
        if key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _normalize_grants(raw: Any) -> list[dict[str, str]]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AccessError("grants must be a list")
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise AccessError("each grant must be an object")
        scope_type = str(item.get("scopeType") or "").strip().upper()
        scope_id = str(item.get("scopeId") or "").strip()
        if scope_type not in SCOPE_TYPES:
            raise AccessError("grant scopeType must be LOCATION, NVR, or CAMERA")
        if not scope_id or len(scope_id) > 256:
            raise AccessError("grant scopeId is required")
        key = (scope_type, scope_id)
        if key in seen:
            continue
        seen.add(key)
        out.append({"scopeType": scope_type, "scopeId": scope_id})
    return out


def _normalize_denies(raw: Any) -> list[str]:
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise AccessError("denies must be a list")
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        device_id = str(item or "").strip()
        if not device_id or len(device_id) > 128 or device_id in seen:
            continue
        seen.add(device_id)
        out.append(device_id)
    return out


def _validate_username(username: str) -> str:
    name = (username or "").strip()
    if len(name) < 2 or len(name) > 64:
        raise AccessError("Username must be 2–64 characters")
    allowed = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-")
    if any(ch not in allowed for ch in name):
        raise AccessError("Username may contain letters, numbers, and . _ @ -")
    return name


def _camera_records() -> list[dict[str, Any]]:
    return [device_access_view(d) for d in list_devices(include_inactive=True) if d.get("deviceType") == "CAMERA"]


def _actor_covers_grant(actor: dict[str, Any], scope_type: str, scope_id: str, cameras: list[dict[str, Any]]) -> bool:
    if (actor.get("role") or {}).get("unrestricted"):
        return True
    if scope_type == "CAMERA":
        cam = next((c for c in cameras if c.get("deviceId") == scope_id), None)
        return bool(cam and camera_allowed(actor, cam))
    if scope_type == "LOCATION":
        subset = [c for c in cameras if location_of(c) == scope_id]
        return bool(subset) and all(camera_allowed(actor, c) for c in subset)
    if scope_type == "NVR":
        subset = [c for c in cameras if scope_id in nvr_ids_of(c)]
        return bool(subset) and all(camera_allowed(actor, c) for c in subset)
    return False


def _assert_can_edit_user(actor: dict[str, Any], target: dict[str, Any] | None, role: dict[str, Any]) -> None:
    if not has_permission(actor, "users.manage"):
        raise AccessError("You cannot manage users", 403)
    actor_super = bool((actor.get("role") or {}).get("unrestricted"))
    if target and (target.get("role") or {}).get("unrestricted") and not actor_super:
        raise AccessError("Only a Super Admin can edit a Super Admin", 403)
    if role.get("unrestricted") and not actor_super:
        raise AccessError("Only a Super Admin can assign the Super Admin role", 403)
    if not actor_super:
        actor_perms = set((actor.get("role") or {}).get("permissions") or [])
        if not set(role.get("permissions") or []).issubset(actor_perms):
            raise AccessError("You cannot assign a role with broader permissions than your own", 403)


def _assert_grants_within_actor(
    actor: dict[str, Any],
    grants: list[dict[str, str]],
    denies: list[str],
) -> None:
    if (actor.get("role") or {}).get("unrestricted"):
        return
    cameras = _camera_records()
    for grant in grants:
        if not _actor_covers_grant(actor, grant["scopeType"], grant["scopeId"], cameras):
            raise AccessError("You cannot grant access you do not have", 403)
    for device_id in denies:
        cam = next((c for c in cameras if c.get("deviceId") == device_id), None)
        if cam is None or not camera_allowed(actor, cam):
            raise AccessError("You cannot change access for a camera you cannot view", 403)


def _replace_grants(conn, user_id: str, grants: list[dict[str, str]], denies: list[str]) -> None:
    conn.execute("DELETE FROM access_grants WHERE userId = ?", (user_id,))
    conn.execute("DELETE FROM access_denies WHERE userId = ?", (user_id,))
    for grant in grants:
        conn.execute(
            "INSERT INTO access_grants (userId, scopeType, scopeId) VALUES (?, ?, ?)",
            (user_id, grant["scopeType"], grant["scopeId"]),
        )
    for device_id in denies:
        conn.execute(
            "INSERT INTO access_denies (userId, deviceId) VALUES (?, ?)",
            (user_id, device_id),
        )


def _guard_last_super(conn, user_id: str, *, will_be_active_super: bool) -> None:
    if will_be_active_super:
        return
    row = conn.execute(
        """
        SELECT r.unrestricted, u.isActive
        FROM users u JOIN roles r ON r.roleId = u.roleId
        WHERE u.userId = ?
        """,
        (user_id,),
    ).fetchone()
    if row and row["unrestricted"] and row["isActive"] and _active_super_count(conn, exclude_user_id=user_id) < 1:
        raise AccessError("At least one active Super Admin is required")


def create_user(actor: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    ensure_access_schema()
    username = _validate_username(str(payload.get("username") or ""))
    password = str(payload.get("password") or "")
    if len(password) < _MIN_PASSWORD:
        raise AccessError(f"Password must be at least {_MIN_PASSWORD} characters")
    role_id = str(payload.get("roleId") or "").strip()
    role = get_role(role_id)
    if role is None:
        raise AccessError("Role not found")
    _assert_can_edit_user(actor, None, role)
    grants = _normalize_grants(payload.get("grants"))
    denies = _normalize_denies(payload.get("denies"))
    _assert_grants_within_actor(actor, grants, denies)
    display_name = str(payload.get("displayName") or "").strip()[:120]
    details = str(payload.get("details") or "").strip()[:2000]
    is_active = bool(payload.get("isActive", True))
    now = utc_now()
    user_id = "user_" + uuid.uuid4().hex[:12]
    with connect() as conn:
        taken = conn.execute(
            "SELECT userId FROM users WHERE lower(username) = lower(?)",
            (username,),
        ).fetchone()
        if taken:
            raise AccessError("That username is already in use", 409)
        conn.execute(
            """
            INSERT INTO users (
                userId, username, passwordHash, displayName, details, roleId, isActive, createdAt, updatedAt
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                username,
                generate_password_hash(password),
                display_name or username,
                details or None,
                role_id,
                1 if is_active else 0,
                now,
                now,
            ),
        )
        _replace_grants(conn, user_id, grants, denies)
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE userId = ?", (user_id,)).fetchone()
        saved = _user_from_row(conn, row)
    publish_access_directory()
    return saved


def _merge_unseen_assignments(
    actor: dict[str, Any],
    target: dict[str, Any] | None,
    grants: list[dict[str, str]],
    denies: list[str],
) -> tuple[list[dict[str, str]], list[str]]:
    """Keep assignments the editor is not allowed to see, so a scoped admin cannot wipe them."""
    if target is None or (actor.get("role") or {}).get("unrestricted"):
        return grants, denies
    cameras = _camera_records()
    merged = list(grants)
    seen = {(g["scopeType"], g["scopeId"]) for g in grants}
    for grant in target.get("grants") or []:
        key = (grant["scopeType"], grant["scopeId"])
        if key in seen:
            continue
        if not _actor_covers_grant(actor, grant["scopeType"], grant["scopeId"], cameras):
            merged.append({"scopeType": grant["scopeType"], "scopeId": grant["scopeId"]})
            seen.add(key)
    merged_denies = list(denies)
    deny_seen = set(denies)
    for device_id in target.get("denies") or []:
        if device_id in deny_seen:
            continue
        cam = next((c for c in cameras if c.get("deviceId") == device_id), None)
        if cam is None or not camera_allowed(actor, cam):
            merged_denies.append(device_id)
            deny_seen.add(device_id)
    return merged, merged_denies


def preview_access(actor: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    draft = preview_actor(payload)
    target = get_user(str(payload.get("userId"))) if payload.get("userId") else None
    _assert_can_edit_user(actor, target, draft["role"])
    _assert_grants_within_actor(actor, draft["grants"], draft["denies"])
    grants, denies = _merge_unseen_assignments(actor, target, draft["grants"], draft["denies"])
    draft["grants"] = grants
    draft["denies"] = denies
    summary = explain_access(draft, _camera_records())
    summary.pop("cameraIds", None)
    return summary


def update_user(actor: dict[str, Any], user_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_access_schema()
    target = get_user(user_id)
    if target is None:
        raise AccessError("User not found", 404)
    role_id = str(payload.get("roleId") or target["roleId"]).strip()
    role = get_role(role_id)
    if role is None:
        raise AccessError("Role not found")
    _assert_can_edit_user(actor, target, role)
    username = _validate_username(str(payload.get("username") or target["username"]))
    grants = _normalize_grants(payload.get("grants"))
    denies = _normalize_denies(payload.get("denies"))
    _assert_grants_within_actor(actor, grants, denies)
    grants, denies = _merge_unseen_assignments(actor, target, grants, denies)
    display_name = str(payload.get("displayName") if payload.get("displayName") is not None else target["displayName"]).strip()[:120]
    details = str(payload.get("details") if payload.get("details") is not None else target["details"]).strip()[:2000]
    is_active = bool(payload.get("isActive", target["isActive"]))
    password = payload.get("password")
    password_text = str(password or "")
    if password_text and len(password_text) < _MIN_PASSWORD:
        raise AccessError(f"Password must be at least {_MIN_PASSWORD} characters")
    if actor.get("userId") == user_id and not is_active:
        raise AccessError("You cannot deactivate your own account")
    now = utc_now()
    with connect() as conn:
        _guard_last_super(
            conn,
            user_id,
            will_be_active_super=bool(is_active and role.get("unrestricted")),
        )
        taken = conn.execute(
            "SELECT userId FROM users WHERE lower(username) = lower(?) AND userId != ?",
            (username, user_id),
        ).fetchone()
        if taken:
            raise AccessError("That username is already in use", 409)
        fields = {
            "username": username,
            "displayName": display_name or username,
            "details": details or None,
            "roleId": role_id,
            "isActive": 1 if is_active else 0,
            "updatedAt": now,
            "userId": user_id,
        }
        conn.execute(
            """
            UPDATE users
            SET username = :username, displayName = :displayName, details = :details,
                roleId = :roleId, isActive = :isActive, updatedAt = :updatedAt
            WHERE userId = :userId
            """,
            fields,
        )
        if password_text:
            conn.execute(
                "UPDATE users SET passwordHash = ? WHERE userId = ?",
                (generate_password_hash(password_text), user_id),
            )
        _replace_grants(conn, user_id, grants, denies)
        conn.commit()
        row = conn.execute("SELECT * FROM users WHERE userId = ?", (user_id,)).fetchone()
        saved = _user_from_row(conn, row)
    publish_access_directory()
    return saved


def create_role(actor: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
    ensure_access_schema()
    if not has_permission(actor, "roles.manage"):
        raise AccessError("You cannot manage roles", 403)
    name = str(payload.get("name") or "").strip()
    if len(name) < 2 or len(name) > 80:
        raise AccessError("Role name must be 2–80 characters")
    description = str(payload.get("description") or "").strip()[:500]
    perms = _normalize_permissions(payload.get("permissions") or [])
    if not (actor.get("role") or {}).get("unrestricted"):
        actor_perms = set((actor.get("role") or {}).get("permissions") or [])
        if not set(perms).issubset(actor_perms):
            raise AccessError("You cannot grant a permission you do not have", 403)
    now = utc_now()
    role_id = "role_" + uuid.uuid4().hex[:12]
    with connect() as conn:
        taken = conn.execute(
            "SELECT roleId FROM roles WHERE lower(name) = lower(?)",
            (name,),
        ).fetchone()
        if taken:
            raise AccessError("A role with that name already exists", 409)
        conn.execute(
            """
            INSERT INTO roles (roleId, name, description, isSystem, unrestricted, createdAt, updatedAt)
            VALUES (?, ?, ?, 0, 0, ?, ?)
            """,
            (role_id, name, description or None, now, now),
        )
        for key in perms:
            conn.execute(
                "INSERT INTO role_permissions (roleId, permissionKey) VALUES (?, ?)",
                (role_id, key),
            )
        conn.commit()
        saved = _role_row(conn, role_id)
    publish_access_directory()
    return saved  # type: ignore[return-value]


def update_role(actor: dict[str, Any], role_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    ensure_access_schema()
    if not has_permission(actor, "roles.manage"):
        raise AccessError("You cannot manage roles", 403)
    existing = get_role(role_id)
    if existing is None:
        raise AccessError("Role not found", 404)
    if existing.get("unrestricted") and not (actor.get("role") or {}).get("unrestricted"):
        raise AccessError("Only a Super Admin can edit the Super Admin role", 403)
    description = str(
        payload.get("description") if payload.get("description") is not None else existing["description"]
    ).strip()[:500]
    now = utc_now()
    with connect() as conn:
        if existing.get("unrestricted"):
            conn.execute(
                "UPDATE roles SET description = ?, updatedAt = ? WHERE roleId = ?",
                (description or None, now, role_id),
            )
            conn.commit()
            saved = _role_row(conn, role_id)
        else:
            name = str(payload.get("name") or existing["name"]).strip()
            if len(name) < 2 or len(name) > 80:
                raise AccessError("Role name must be 2–80 characters")
            perms = _normalize_permissions(
                payload.get("permissions") if payload.get("permissions") is not None else existing["permissions"]
            )
            if not (actor.get("role") or {}).get("unrestricted"):
                actor_perms = set((actor.get("role") or {}).get("permissions") or [])
                if not set(perms).issubset(actor_perms):
                    raise AccessError("You cannot grant a permission you do not have", 403)
            taken = conn.execute(
                "SELECT roleId FROM roles WHERE lower(name) = lower(?) AND roleId != ?",
                (name, role_id),
            ).fetchone()
            if taken:
                raise AccessError("A role with that name already exists", 409)
            conn.execute(
                "UPDATE roles SET name = ?, description = ?, updatedAt = ? WHERE roleId = ?",
                (name, description or None, now, role_id),
            )
            conn.execute("DELETE FROM role_permissions WHERE roleId = ?", (role_id,))
            for key in perms:
                conn.execute(
                    "INSERT INTO role_permissions (roleId, permissionKey) VALUES (?, ?)",
                    (role_id, key),
                )
            conn.commit()
            saved = _role_row(conn, role_id)
    publish_access_directory()
    return saved  # type: ignore[return-value]


def delete_role(actor: dict[str, Any], role_id: str) -> None:
    ensure_access_schema()
    if not has_permission(actor, "roles.manage"):
        raise AccessError("You cannot manage roles", 403)
    existing = get_role(role_id)
    if existing is None:
        raise AccessError("Role not found", 404)
    if existing.get("isSystem") or existing.get("unrestricted"):
        raise AccessError("System roles cannot be deleted")
    with connect() as conn:
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM users WHERE roleId = ?",
            (role_id,),
        ).fetchone()
        if used and int(used["n"]) > 0:
            raise AccessError("Reassign users before deleting this role")
        conn.execute("DELETE FROM role_permissions WHERE roleId = ?", (role_id,))
        conn.execute("DELETE FROM roles WHERE roleId = ?", (role_id,))
        conn.commit()
    publish_access_directory()


def build_resource_tree(actor: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Location → NVR → camera tree. Non-super actors only see cameras they can access."""
    cameras = _camera_records()
    if actor is not None and not (actor.get("role") or {}).get("unrestricted"):
        index = access_index(actor)
        cameras = [cam for cam in cameras if camera_allowed(actor, cam, index)]
    tree: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for cam in cameras:
        loc = location_of(cam)
        groups = nvr_ids_of(cam) or ["Unassigned"]
        stub = {
            "deviceId": cam.get("deviceId"),
            "name": cam.get("deviceName") or cam.get("deviceId"),
            "ip": cam.get("ipAddress") or "",
            "isActive": bool(cam.get("isActive")),
            "nvrIds": groups,
        }
        for nvr_id in groups:
            tree.setdefault(loc, {}).setdefault(nvr_id, [])
            bucket = tree[loc][nvr_id]
            if not any(item["deviceId"] == stub["deviceId"] for item in bucket):
                bucket.append(stub)
    locations = []
    for loc in sorted(tree, key=str.lower):
        nvrs = []
        for nvr_id in sorted(tree[loc], key=lambda value: [(0, int(part), "") if part.isdigit() else (1, 0, part) for part in str(value).split(".")]):
            cams = sorted(tree[loc][nvr_id], key=lambda item: (str(item["name"]).lower(), item["ip"]))
            nvrs.append({"id": nvr_id, "label": f"NVR {nvr_id}", "cameras": cams})
        locations.append({"id": loc, "nvrs": nvrs})
    return locations


def preview_actor(payload: dict[str, Any]) -> dict[str, Any]:
    """Build an unsaved actor so the UI can preview effective access."""
    role = get_role(str(payload.get("roleId") or ""))
    if role is None:
        raise AccessError("Role not found")
    return {
        "userId": payload.get("userId") or "preview",
        "username": payload.get("username") or "preview",
        "isActive": bool(payload.get("isActive", True)),
        "role": role,
        "grants": _normalize_grants(payload.get("grants")),
        "denies": _normalize_denies(payload.get("denies")),
    }


def export_directory() -> dict[str, Any]:
    """Directory published for the remote dashboard. Password hashes only."""
    ensure_access_schema()
    with connect() as conn:
        role_rows = conn.execute("SELECT roleId FROM roles").fetchall()
        roles = [_role_row(conn, r["roleId"]) for r in role_rows]
        user_rows = conn.execute("SELECT * FROM users").fetchall()
        users = []
        for row in user_rows:
            actor = _user_from_row(conn, row)
            users.append(
                {
                    "userId": actor["userId"],
                    "username": actor["username"],
                    "passwordHash": row["passwordHash"],
                    "displayName": actor["displayName"],
                    "details": actor["details"],
                    "roleId": actor["roleId"],
                    "isActive": actor["isActive"],
                    "grants": actor["grants"],
                    "denies": actor["denies"],
                }
            )
    return {
        "version": 1,
        "publishedAt": utc_now(),
        "roles": roles,
        "users": users,
    }


def publish_access_directory() -> dict[str, Any]:
    try:
        from src.bunny_storage import bunny_configured, upload_bytes
    except Exception:
        return {"ok": False, "reason": "storage unavailable"}
    if not bunny_configured():
        return {"ok": False, "reason": "bunny not configured"}
    try:
        import json

        payload = export_directory()
        upload_bytes(
            "dashboard/access-control.json",
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            content_type="application/json",
        )
        log.info("Published access directory users=%s", len(payload["users"]))
        return {"ok": True, "users": len(payload["users"])}
    except Exception:
        log.exception("Failed to publish access directory")
        return {"ok": False, "reason": "publish failed"}


def directory_user(
    directory: dict[str, Any] | None,
    *,
    user_id: str | None = None,
    username: str | None = None,
) -> dict[str, Any] | None:
    if not directory:
        return None
    users = [user for user in directory.get("users") or [] if isinstance(user, dict)]
    if user_id:
        found = next((user for user in users if user.get("userId") == user_id), None)
        if found is not None:
            return found
    if username:
        name = username.strip().lower()
        return next(
            (user for user in users if str(user.get("username") or "").lower() == name),
            None,
        )
    return None


def actor_from_directory(
    directory: dict[str, Any] | None,
    *,
    user_id: str | None = None,
    username: str | None = None,
) -> dict[str, Any] | None:
    match = directory_user(directory, user_id=user_id, username=username)
    if match is None or not match.get("isActive") or not directory:
        return None
    roles = {role["roleId"]: role for role in directory.get("roles") or [] if isinstance(role, dict)}
    role = roles.get(match.get("roleId")) or {
        "roleId": match.get("roleId"),
        "name": "Unknown",
        "unrestricted": False,
        "permissions": [],
    }
    return {
        "userId": match.get("userId"),
        "username": match.get("username"),
        "displayName": match.get("displayName") or match.get("username"),
        "details": match.get("details") or "",
        "isActive": True,
        "roleId": match.get("roleId"),
        "role": role,
        "grants": match.get("grants") or [],
        "denies": match.get("denies") or [],
    }


def authenticate_directory(
    directory: dict[str, Any] | None, username: str, password: str
) -> dict[str, Any] | None:
    if not directory:
        return None
    name = (username or "").strip().lower()
    if not name or not password:
        return None
    for user in directory.get("users") or []:
        if not isinstance(user, dict):
            continue
        if str(user.get("username") or "").lower() != name:
            continue
        if not user.get("isActive"):
            return None
        hashed = str(user.get("passwordHash") or "")
        if not hashed or not check_password_hash(hashed, password):
            return None
        return actor_from_directory(directory, user_id=str(user.get("userId") or ""))
    return None
