"""Discover NVR channels and open camera previews without exposing credentials."""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from urllib.parse import quote

import requests
import urllib3
from requests.auth import HTTPDigestAuth

from src.db import ROOT, assign_stream_targets
from src.nvr_catalog import NVR_DATA_DIR, load_nvr_cameras

log = logging.getLogger("camera-uptime.nvr-streams")
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_discovery_started = False
_auth_lock = threading.Lock()
_auth_cache: dict[str, tuple[HTTPDigestAuth, str]] = {}
# host -> monotonic time when the failed attempts may be retried.
_auth_failed: dict[str, float] = {}
# Cooldown after all login attempts fail, before a host is retried.
AUTH_RETRY_BUFFER_SEC = 600.0


def login_attempts() -> list[tuple[str, str]]:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
    pairs: list[tuple[str, str]] = []
    user = (os.getenv("NVR_USERNAME") or "").strip()
    password = os.getenv("NVR_PASSWORD") or ""
    if user and password:
        pairs.append((user, password))
    raw = os.getenv("NVR_LOGIN_ATTEMPTS") or ""
    for part in raw.split(";"):
        part = part.strip()
        if "|" not in part:
            continue
        attempt_user, attempt_password = part.split("|", 1)
        attempt_user = attempt_user.strip()
        if attempt_user and attempt_password:
            pairs.append((attempt_user, attempt_password))
    unique: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for pair in pairs:
        if pair in seen:
            continue
        seen.add(pair)
        unique.append(pair)
    for row in load_nvr_cameras():
        pair = ((row.username or "").strip(), row.password or "")
        if not pair[0] or not pair[1] or pair in seen:
            continue
        seen.add(pair)
        unique.append(pair)
    return unique


def nvr_host_from_source(source: str) -> str | None:
    """Export file '6.19' is the NVR at 192.168.6.19."""
    match = re.fullmatch(r"(\d{1,3})\.(\d{1,3})", (source or "").strip())
    if not match:
        return None
    return f"192.168.{int(match.group(1))}.{int(match.group(2))}"


def targets_from_exports() -> dict[str, tuple[str, int]]:
    """Camera IP -> (NVR host, 1-based channel) using export order."""
    grouped: dict[str, list] = {}
    for row in load_nvr_cameras():
        grouped.setdefault(row.nvr_source, []).append(row)
    mapping: dict[str, tuple[str, int]] = {}
    for source, rows in grouped.items():
        host = nvr_host_from_source(source)
        if not host:
            continue
        for index, row in enumerate(rows, start=1):
            mapping.setdefault(row.ip, (host, index))
    return mapping


def _request(host: str, path: str, auth: HTTPDigestAuth, timeout: float) -> requests.Response | None:
    for scheme in ("http", "https"):
        try:
            response = requests.get(
                f"{scheme}://{host}{path}",
                auth=auth,
                timeout=timeout,
                verify=False,
            )
        except requests.RequestException:
            continue
        if response.status_code == 200:
            return response
    return None


def _probe_login(host: str, auth: HTTPDigestAuth, timeout: float) -> int | None:
    """Status of a credential probe: 200 = ok, 401 = wrong password,
    403 = digest accepted but device refused (e.g. brute-force lockout)."""
    for scheme in ("http", "https"):
        try:
            response = requests.get(
                f"{scheme}://{host}/cgi-bin/magicBox.cgi?action=getDeviceType",
                auth=auth,
                timeout=timeout,
                verify=False,
            )
        except requests.RequestException:
            continue
        return response.status_code
    return None


def _auth_for(host: str) -> HTTPDigestAuth | None:
    with _auth_lock:
        retry_at = _auth_failed.get(host)
        if retry_at is not None:
            if time.monotonic() < retry_at:
                return None
            # Buffer elapsed: drop the mark and try the attempts again.
            del _auth_failed[host]
        cached = _auth_cache.get(host)
    if cached:
        return cached[0]
    for user, password in login_attempts():
        auth = HTTPDigestAuth(user, password)
        status = _probe_login(host, auth, 3)
        if status == 200:
            with _auth_lock:
                _auth_cache[host] = (auth, user)
                _auth_failed.pop(host, None)
            log.info("NVR %s authenticated as %s", host, user)
            return auth
        if status == 403:
            # Digest accepted this credential but the device refused it — almost
            # always an anti-brute-force lockout. Stop here so we don't fire the
            # remaining (wrong) passwords and deepen the lockout; back off instead.
            with _auth_lock:
                _auth_failed[host] = time.monotonic() + AUTH_RETRY_BUFFER_SEC
            log.warning(
                "NVR %s accepted %s but refused it (status 403 — likely account "
                "lockout); backing off %.0f min",
                host,
                user,
                AUTH_RETRY_BUFFER_SEC / 60,
            )
            return None
    with _auth_lock:
        _auth_failed[host] = time.monotonic() + AUTH_RETRY_BUFFER_SEC
    log.warning(
        "NVR %s did not accept configured logins; retrying in %.0f min",
        host,
        AUTH_RETRY_BUFFER_SEC / 60,
    )
    return None


def fetch_nvr_channels(host: str) -> dict[str, tuple[str, int]]:
    """Return camera IP -> (nvr host, 1-based channel) from the live NVR."""
    auth = _auth_for(host)
    if auth is None:
        return {}
    response = _request(
        host,
        "/cgi-bin/configManager.cgi?action=getConfig&name=RemoteDevice",
        auth,
        12,
    )
    if response is None:
        log.warning("NVR %s channel list unavailable", host)
        return {}
    found: dict[int, str] = {}
    enabled: dict[int, bool] = {}
    for line in response.text.splitlines():
        if "Password" in line or "UserName" in line:
            continue
        match = re.match(
            r"table\.RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.(Address|Enable)=(.*)",
            line.strip(),
        )
        if not match:
            continue
        index = int(match.group(1))
        key = match.group(2)
        value = match.group(3).strip()
        if key == "Address" and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", value):
            found[index] = value
        elif key == "Enable":
            enabled[index] = value.lower() == "true"
    mapping: dict[str, tuple[str, int]] = {}
    for index, ip in found.items():
        if enabled.get(index, True):
            mapping.setdefault(ip, (host, index + 1))
    log.info("NVR %s published %s camera channels", host, len(mapping))
    return mapping


def discover_stream_targets() -> int:
    """Load export channels, then refresh them from each live NVR."""
    mapping = targets_from_exports()
    hosts = sorted({host for host, _channel in mapping.values()})
    for host in hosts:
        try:
            live = fetch_nvr_channels(host)
        except Exception:
            log.exception("NVR %s discovery failed", host)
            continue
        mapping.update(live)
    updated = assign_stream_targets(mapping)
    log.info("Stream targets saved for %s cameras", updated)
    return updated


def ensure_stream_discovery() -> None:
    global _discovery_started
    if _discovery_started:
        return
    _discovery_started = True
    threading.Thread(target=discover_stream_targets, name="nvr-discovery", daemon=True).start()


def build_rtsp_url(host: str, channel: int, *, direct_camera: bool) -> str | None:
    """Dahua/CP Plus realmonitor URL. Caller must not log or return this value."""
    attempts = login_attempts()
    if not attempts:
        return None
    user, password = attempts[0]
    auth = f"{quote(user, safe='')}:{quote(password, safe='')}@"
    if direct_camera:
        query = "channel=1&subtype=1"
    else:
        query = f"channel={int(channel)}&subtype=1"
    return f"rtsp://{auth}{host}:554/cam/realmonitor?{query}"


def http_snapshot(host: str, channel: int) -> bytes | None:
    auth = _auth_for(host)
    if auth is None:
        return None
    response = _request(host, f"/cgi-bin/snapshot.cgi?channel={int(channel)}", auth, 8)
    if response is None or not response.content.startswith(b"\xff\xd8"):
        return None
    return response.content


def http_mjpeg_frame(host: str, channel: int) -> bytes | None:
    """First JPEG from a Dahua MJPEG stream when snapshot.cgi is unsupported."""
    auth = _auth_for(host)
    if auth is None:
        return None
    path = f"/cgi-bin/mjpg/video.cgi?channel={int(channel)}&subtype=1"
    for scheme in ("http", "https"):
        response = None
        try:
            response = requests.get(
                f"{scheme}://{host}{path}",
                auth=auth,
                timeout=(3, 8),
                verify=False,
                stream=True,
            )
            if response.status_code != 200:
                continue
            buf = bytearray()
            for chunk in response.iter_content(8192):
                if not chunk:
                    break
                buf.extend(chunk)
                start = buf.find(b"\xff\xd8")
                if start < 0:
                    if len(buf) > 65536:
                        del buf[:-2]
                    continue
                end = buf.find(b"\xff\xd9", start + 2)
                if end >= 0:
                    return bytes(buf[start : end + 2])
                if len(buf) > 2_000_000:
                    return None
        except requests.RequestException:
            continue
        finally:
            if response is not None:
                response.close()
    return None


def redact_rtsp(text: str) -> str:
    return re.sub(r"rtsp://\S+", "rtsp://***", text, flags=re.I)
