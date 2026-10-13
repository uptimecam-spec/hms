"""Per-user favourite cameras (local JSON + optional Bunny sync)."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from pathlib import Path
from typing import Any

from src.db import ROOT

log = logging.getLogger("camera-uptime.favorites")

FAV_PATH = ROOT / "data" / "user_favorites.json"
BUNNY_KEY = "dashboard/user-favorites.json"
_lock = threading.Lock()

_USER_RE = re.compile(r"^[A-Za-z0-9._@-]{1,64}$")


def normalize_username(raw: str | None) -> str:
    value = (raw or "").strip() or "local"
    if not _USER_RE.match(value):
        value = re.sub(r"[^A-Za-z0-9._@-]", "_", value)[:64] or "local"
    return value


def current_username(session_user: str | None = None) -> str:
    if session_user:
        return normalize_username(session_user)
    env_user = (os.getenv("DASHBOARD_USER") or "").strip()
    return normalize_username(env_user or "local")


def _empty_store() -> dict[str, Any]:
    return {"users": {}}


def _sanitize_ids(device_ids: list[Any] | None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in device_ids or []:
        device_id = str(raw or "").strip()
        if not device_id or device_id in seen:
            continue
        seen.add(device_id)
        out.append(device_id)
    return out


def _read_local() -> dict[str, Any]:
    if not FAV_PATH.exists():
        return _empty_store()
    try:
        data = json.loads(FAV_PATH.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        log.warning("Could not read favorites file %s", FAV_PATH)
        return _empty_store()
    users = data.get("users") if isinstance(data, dict) else None
    if not isinstance(users, dict):
        return _empty_store()
    return {"users": {normalize_username(k): _sanitize_ids(v if isinstance(v, list) else []) for k, v in users.items()}}


def _write_local(store: dict[str, Any]) -> None:
    FAV_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = FAV_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(store, indent=2), encoding="utf-8")
    temporary.replace(FAV_PATH)


def _read_bunny() -> dict[str, Any] | None:
    try:
        from src.bunny_storage import bunny_readable, download_json
    except Exception:
        return None
    if not bunny_readable():
        return None
    data = download_json(BUNNY_KEY)
    if not isinstance(data, dict):
        return None
    users = data.get("users")
    if not isinstance(users, dict):
        return _empty_store()
    return {
        "users": {
            normalize_username(k): _sanitize_ids(v if isinstance(v, list) else [])
            for k, v in users.items()
        }
    }


def _write_bunny(store: dict[str, Any]) -> bool:
    try:
        from src.bunny_storage import bunny_configured, upload_bytes
    except Exception:
        return False
    if not bunny_configured():
        return False
    try:
        upload_bytes(
            BUNNY_KEY,
            json.dumps(store, indent=2).encode("utf-8"),
            content_type="application/json",
        )
        return True
    except Exception:
        log.exception("Failed to upload favorites to Bunny")
        return False


def load_store(*, prefer_bunny: bool = False) -> dict[str, Any]:
    with _lock:
        if prefer_bunny:
            remote = _read_bunny()
            if remote is not None:
                return remote
        return _read_local()


def save_store(store: dict[str, Any], *, sync_bunny: bool = False) -> dict[str, Any]:
    clean = {
        "users": {
            normalize_username(k): _sanitize_ids(v if isinstance(v, list) else [])
            for k, v in (store.get("users") or {}).items()
        }
    }
    with _lock:
        _write_local(clean)
        if sync_bunny:
            _write_bunny(clean)
    return clean


def get_favorites(username: str, *, prefer_bunny: bool = False) -> list[str]:
    store = load_store(prefer_bunny=prefer_bunny)
    return list(store.get("users", {}).get(normalize_username(username), []))


def set_favorites(
    username: str,
    device_ids: list[Any] | None,
    *,
    sync_bunny: bool = False,
    prefer_bunny: bool = False,
) -> list[str]:
    user = normalize_username(username)
    ids = _sanitize_ids(device_ids)
    store = load_store(prefer_bunny=prefer_bunny)
    users = dict(store.get("users") or {})
    users[user] = ids
    save_store({"users": users}, sync_bunny=sync_bunny)
    return ids


def toggle_favorite(
    username: str,
    device_id: str,
    *,
    sync_bunny: bool = False,
    prefer_bunny: bool = False,
) -> tuple[list[str], bool]:
    user = normalize_username(username)
    device = str(device_id or "").strip()
    current = get_favorites(user, prefer_bunny=prefer_bunny)
    if device in current:
        next_ids = [d for d in current if d != device]
        favourited = False
    else:
        next_ids = current + ([device] if device else [])
        favourited = True
    set_favorites(user, next_ids, sync_bunny=sync_bunny, prefer_bunny=prefer_bunny)
    return next_ids, favourited
