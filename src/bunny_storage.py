"""Bunny.net Edge Storage client for health-check images."""

from __future__ import annotations

import json
import logging
import os
import re
from typing import Any
from urllib.parse import quote

import requests
from dotenv import load_dotenv

from src.db import ROOT

log = logging.getLogger("camera-uptime.bunny")

BUNNY_PREFIX = "bunny:"
DEFAULT_ENDPOINT = "https://sg.storage.bunnycdn.com"
DEFAULT_RETENTION_DAYS = 30


def _load_env() -> None:
    load_dotenv(ROOT / ".env", override=True)


def bunny_configured() -> bool:
    """True when write credentials are present (uploads / deletes)."""
    _load_env()
    zone = (os.getenv("BUNNY_STORAGE_ZONE") or "").strip()
    key = (os.getenv("BUNNY_STORAGE_PASSWORD") or "").strip()
    return bool(zone and key)


def bunny_readable() -> bool:
    """True when zone + read or write password is present (downloads)."""
    _load_env()
    zone = (os.getenv("BUNNY_STORAGE_ZONE") or "").strip()
    cfg_read = (os.getenv("BUNNY_STORAGE_READONLY_PASSWORD") or "").strip()
    cfg_write = (os.getenv("BUNNY_STORAGE_PASSWORD") or "").strip()
    return bool(zone and (cfg_read or cfg_write))


def bunny_settings() -> dict[str, Any]:
    _load_env()
    endpoint = (os.getenv("BUNNY_STORAGE_ENDPOINT") or DEFAULT_ENDPOINT).strip().rstrip("/")
    # Prefer HTTP storage host over S3 endpoint for the REST AccessKey API.
    if "-s3.storage.bunnycdn.com" in endpoint:
        endpoint = endpoint.replace("-s3.storage.bunnycdn.com", ".storage.bunnycdn.com")
    try:
        retention = max(1, int(os.getenv("BUNNY_IMAGE_RETENTION_DAYS", str(DEFAULT_RETENTION_DAYS))))
    except ValueError:
        retention = DEFAULT_RETENTION_DAYS
    return {
        "zone": (os.getenv("BUNNY_STORAGE_ZONE") or "").strip(),
        "password": (os.getenv("BUNNY_STORAGE_PASSWORD") or "").strip(),
        "read_password": (os.getenv("BUNNY_STORAGE_READONLY_PASSWORD") or "").strip(),
        "endpoint": endpoint,
        "retention_days": retention,
        "cdn_base": (os.getenv("BUNNY_CDN_BASE_URL") or "").strip().rstrip("/"),
    }


def is_bunny_path(path: str | None) -> bool:
    return bool(path and str(path).startswith(BUNNY_PREFIX))


def to_bunny_path(object_key: str) -> str:
    key = object_key.lstrip("/")
    return f"{BUNNY_PREFIX}{key}"


def from_bunny_path(path: str) -> str:
    raw = str(path or "")
    if raw.startswith(BUNNY_PREFIX):
        return raw[len(BUNNY_PREFIX) :].lstrip("/")
    return raw.lstrip("/")


def check_image_object_key(device_id: str, check_id: str) -> str:
    safe_device = re.sub(r"[^A-Za-z0-9_-]", "_", device_id)
    safe_check = re.sub(r"[^A-Za-z0-9_-]", "_", check_id)
    return f"check-images/{safe_device}/{safe_check}.jpg"


def _object_url(endpoint: str, zone: str, object_key: str) -> str:
    parts = [quote(p, safe="") for p in object_key.split("/") if p]
    return f"{endpoint}/{quote(zone, safe='')}/{'/'.join(parts)}"


def upload_bytes(object_key: str, data: bytes, *, content_type: str = "image/jpeg") -> str:
    """Upload raw bytes. Returns bunny: path on success. Raises on failure."""
    if not bunny_configured():
        raise RuntimeError("Bunny storage is not configured")
    cfg = bunny_settings()
    url = _object_url(cfg["endpoint"], cfg["zone"], object_key)
    response = requests.put(
        url,
        data=data,
        headers={
            "AccessKey": cfg["password"],
            "Content-Type": content_type,
        },
        timeout=60,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(
            f"Bunny upload failed ({response.status_code}): {response.text[:200]}"
        )
    return to_bunny_path(object_key)


def download_bytes(object_key_or_bunny_path: str) -> bytes | None:
    if not bunny_readable():
        return None
    cfg = bunny_settings()
    object_key = from_bunny_path(object_key_or_bunny_path)
    url = _object_url(cfg["endpoint"], cfg["zone"], object_key)
    access = cfg["read_password"] or cfg["password"]
    if not access:
        return None
    try:
        response = requests.get(
            url,
            headers={"AccessKey": access},
            timeout=45,
        )
    except requests.RequestException:
        log.exception("Bunny download error for %s", object_key)
        return None
    if response.status_code != 200 or not response.content:
        return None
    return response.content


def download_json(object_key: str) -> dict[str, Any] | None:
    raw = download_bytes(object_key)
    if not raw:
        return None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        log.warning("Bunny JSON parse failed for %s", object_key)
        return None
    return data if isinstance(data, dict) else None


def delete_object(object_key_or_bunny_path: str) -> bool:
    if not bunny_configured():
        return False
    cfg = bunny_settings()
    object_key = from_bunny_path(object_key_or_bunny_path)
    url = _object_url(cfg["endpoint"], cfg["zone"], object_key)
    try:
        response = requests.delete(
            url,
            headers={"AccessKey": cfg["password"]},
            timeout=45,
        )
    except requests.RequestException:
        log.exception("Bunny delete error for %s", object_key)
        return False
    if response.status_code in (200, 204, 404):
        return True
    log.warning("Bunny delete failed (%s) for %s", response.status_code, object_key)
    return False


def public_or_proxy_hint(object_key_or_bunny_path: str) -> str | None:
    """Optional CDN URL when BUNNY_CDN_BASE_URL is set."""
    cfg = bunny_settings()
    if not cfg["cdn_base"]:
        return None
    key = from_bunny_path(object_key_or_bunny_path)
    return f"{cfg['cdn_base']}/{key}"
