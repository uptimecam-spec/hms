"""Stored camera previews. The dashboard serves saved JPEGs and does not grab live frames."""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from dotenv import load_dotenv

from src.db import ROOT, get_device
from src.nvr_streams import build_rtsp_url, http_mjpeg_frame, http_snapshot

log = logging.getLogger("camera-uptime.stream")

SNAPSHOT_DIR = ROOT / "data" / "snapshots"
CHECK_IMAGE_DIR = ROOT / "data" / "check_images"
_semaphore: threading.Semaphore | None = None
_store_lock = threading.Lock()
_store_running = False
_nvr_semaphores: dict[str, threading.Semaphore] = {}
_nvr_sem_lock = threading.Lock()


def _ffmpeg_path() -> str | None:
    load_dotenv(ROOT / ".env", override=True)
    custom = (os.getenv("FFMPEG_PATH") or "").strip()
    if custom and Path(custom).exists():
        return custom
    return shutil.which("ffmpeg")


def _snapshot_concurrency() -> int:
    load_dotenv(ROOT / ".env", override=True)
    try:
        return max(1, min(24, int(os.getenv("SNAPSHOT_CONCURRENCY", "8"))))
    except ValueError:
        return 8


def _get_semaphore() -> threading.Semaphore:
    global _semaphore
    if _semaphore is None:
        _semaphore = threading.Semaphore(_snapshot_concurrency())
    return _semaphore


def capture_snapshot(rtsp_url: str, *, timeout_sec: float = 12.0) -> bytes | None:
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None
    cmd = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "error",
        "-rtsp_transport",
        "tcp",
        "-i",
        rtsp_url,
        "-frames:v",
        "1",
        "-f",
        "image2pipe",
        "-vcodec",
        "mjpeg",
        "pipe:1",
    ]
    try:
        with _get_semaphore():
            proc = subprocess.run(
                cmd,
                capture_output=True,
                timeout=timeout_sec + 2,
                check=False,
            )
        if proc.returncode != 0 or not proc.stdout:
            log.debug("ffmpeg snapshot failed")
            return None
        return proc.stdout
    except subprocess.TimeoutExpired:
        log.debug("ffmpeg snapshot timed out")
        return None
    except Exception:
        log.debug("ffmpeg snapshot error")
        return None


def _preview_jpeg(device: dict) -> bytes | None:
    """NVR picture first. Ping-offline cameras are still fetched through the NVR."""
    camera_ip = (device.get("ipAddress") or "").strip()
    nvr_host = (device.get("nvrHost") or "").strip()
    try:
        nvr_channel = int(device.get("nvrChannel") or 1)
    except (TypeError, ValueError):
        nvr_channel = 1

    if nvr_host:
        image = http_snapshot(nvr_host, nvr_channel)
        if image:
            return image
    if _ffmpeg_path() and nvr_host:
        via_nvr = build_rtsp_url(nvr_host, nvr_channel, direct_camera=False)
        if via_nvr:
            image = capture_snapshot(via_nvr, timeout_sec=8)
            if image:
                return image
    if camera_ip and camera_ip != nvr_host:
        image = http_snapshot(camera_ip, 1)
        if image:
            return image
    if _ffmpeg_path() and camera_ip:
        direct = build_rtsp_url(camera_ip, 1, direct_camera=True)
        if direct:
            return capture_snapshot(direct, timeout_sec=8)
    return None


def snapshot_file(device_id: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", device_id)
    return SNAPSHOT_DIR / f"{safe}.jpg"


def read_stored_snapshot(device_id: str) -> bytes | None:
    path = snapshot_file(device_id)
    if not path.is_file() or path.stat().st_size < 100:
        return None
    return path.read_bytes()


def _custom_rtsp_jpeg(device: dict) -> bytes | None:
    """A per-camera rtspUrl overrides discovery — grab one frame straight from it.

    Used for cameras that are not reachable through the Dahua NVR API (different
    vendor/firmware, non-standard RTSP path, or their own credentials).
    """
    rtsp_url = (device.get("rtspUrl") or "").strip()
    if not rtsp_url or not _ffmpeg_path():
        return None
    return capture_snapshot(rtsp_url, timeout_sec=10)


def _nvr_jpeg(device: dict) -> bytes | None:
    """Explicit rtspUrl first, then the NVR, then the camera, then a short RTSP grab."""
    image = _custom_rtsp_jpeg(device)
    if image:
        return image

    camera_ip = (device.get("ipAddress") or "").strip()
    nvr_host = (device.get("nvrHost") or "").strip()
    try:
        channel = int(device.get("nvrChannel") or 1)
    except (TypeError, ValueError):
        channel = 1

    if nvr_host:
        image = http_snapshot(nvr_host, channel)
        if image:
            return image
        image = http_mjpeg_frame(nvr_host, channel)
        if image:
            return image
    if camera_ip:
        image = http_snapshot(camera_ip, 1)
        if image:
            return image
    if not _ffmpeg_path():
        return None
    if nvr_host:
        via_nvr = build_rtsp_url(nvr_host, channel, direct_camera=False)
        if via_nvr:
            image = capture_snapshot(via_nvr, timeout_sec=6)
            if image:
                return image
    if camera_ip:
        direct = build_rtsp_url(camera_ip, 1, direct_camera=True)
        if direct:
            return capture_snapshot(direct, timeout_sec=6)
    return None


def _save_jpeg(device_id: str, data: bytes) -> None:
    path = snapshot_file(device_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def store_all_camera_snapshots(workers: int = 20) -> dict[str, int]:
    """Save one JPEG per camera. Safe to run once; skips cameras already stored."""
    from src.db import list_devices

    devices = [
        device
        for device in list_devices(include_inactive=False)
        if device.get("deviceType") == "CAMERA" and device.get("isActive")
    ]
    pending = [device for device in devices if read_stored_snapshot(device["deviceId"]) is None]
    saved = 0

    def grab(device: dict) -> bool:
        try:
            image = _nvr_jpeg(device)
        except Exception:
            log.exception("Stored snapshot failed for %s", device.get("deviceId"))
            return False
        if not image:
            return False
        _save_jpeg(device["deviceId"], image)
        return True

    if pending:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = [pool.submit(grab, device) for device in pending]
            for future in as_completed(futures):
                if future.result():
                    saved += 1
    already = len(devices) - len(pending)
    log.info("Stored snapshots: %s new, %s already on disk, %s cameras", saved, already, len(devices))
    return {"saved": saved, "already": already, "total": len(devices)}


def ensure_stored_snapshots() -> None:
    """Fill any missing preview files in the background. Does not refresh existing ones."""
    global _store_running
    with _store_lock:
        if _store_running:
            return
        _store_running = True

    def run() -> None:
        global _store_running
        try:
            store_all_camera_snapshots()
        finally:
            with _store_lock:
                _store_running = False

    threading.Thread(target=run, name="store-snapshots", daemon=True).start()


def preview_file_meta(device_id: str) -> tuple[bool, int | None]:
    """Return (available, mtime_version) for a usable stored preview file."""
    path = snapshot_file(device_id)
    try:
        stat = path.stat()
    except OSError:
        return False, None
    if not path.is_file() or stat.st_size < 800:
        return False, None
    return True, int(stat.st_mtime)


def clear_stored_snapshot(device_id: str) -> None:
    path = snapshot_file(device_id)
    try:
        if path.is_file():
            path.unlink()
    except OSError:
        pass


def has_stored_preview(device_id: str) -> bool:
    available, _version = preview_file_meta(device_id)
    return available


def snapshot_for_device(device_id: str) -> tuple[bytes | None, str | None]:
    """Return the saved JPEG only. Never opens a live stream."""
    available, _version = preview_file_meta(device_id)
    if not available:
        return None, "no stored preview"
    data = read_stored_snapshot(device_id)
    if data:
        return data, None
    return None, "no stored preview"


def refresh_device_snapshot(device_id: str) -> tuple[bool, str | None]:
    """Try to capture a fresh preview. Does not change ping/network status."""
    device = get_device(device_id)
    if not device:
        return False, "camera not found"
    try:
        image = _nvr_jpeg(device)
    except Exception:
        log.exception("Preview refresh failed for %s", device_id)
        return False, "preview capture error"
    if not image or len(image) < 800 or not image.startswith(b"\xff\xd8"):
        clear_stored_snapshot(device_id)
        return False, "preview unavailable"
    _save_jpeg(device_id, image)
    return True, None


def _nvr_gate(device: dict) -> threading.Semaphore:
    key = (device.get("nvrHost") or device.get("nvrId") or device.get("ipAddress") or "default").strip()
    with _nvr_sem_lock:
        sem = _nvr_semaphores.get(key)
        if sem is None:
            load_dotenv(ROOT / ".env", override=True)
            try:
                per_nvr = max(1, min(6, int(os.getenv("HEALTH_CHECK_PER_NVR", "2"))))
            except ValueError:
                per_nvr = 2
            sem = threading.Semaphore(per_nvr)
            _nvr_semaphores[key] = sem
        return sem


def capture_preview_for_check(device: dict) -> tuple[bytes | None, str | None]:
    """Capture a preview for a health-check record. Returns (jpeg, error)."""
    gate = _nvr_gate(device)
    with gate:
        try:
            image = _nvr_jpeg(device)
        except Exception as exc:
            log.debug("Check preview failed for %s: %s", device.get("deviceId"), exc)
            return None, str(exc)
    if not image or len(image) < 800 or not image.startswith(b"\xff\xd8"):
        return None, "preview unavailable"
    return image, None


def capture_live_frame(device_id: str) -> tuple[bytes | None, str | None]:
    """Grab one fresh frame for live preview. Does not require stored snapshots."""
    device = get_device(device_id)
    if not device:
        return None, "camera not found"
    image, err = capture_preview_for_check(device)
    if not image:
        return None, err or "live frame unavailable"
    return image, None


def save_check_image(device_id: str, check_id: str, data: bytes) -> str:
    """Persist a historical check image (Bunny when configured, else local).

    Returns a storage reference path stored on the health-check row.
    Always refreshes the local live dashboard thumbnail.
    """
    from src.bunny_storage import (
        bunny_configured,
        check_image_object_key,
        upload_bytes,
    )

    # Keep the live dashboard thumbnail in sync with the newest capture.
    _save_jpeg(device_id, data)

    if bunny_configured():
        object_key = check_image_object_key(device_id, check_id)
        try:
            bunny_path = upload_bytes(object_key, data, content_type="image/jpeg")
            log.info("Stored check image on Bunny: %s", bunny_path)
            return bunny_path
        except Exception:
            log.exception("Bunny upload failed; falling back to local disk for %s", check_id)

    safe_device = re.sub(r"[^A-Za-z0-9_-]", "_", device_id)
    safe_check = re.sub(r"[^A-Za-z0-9_-]", "_", check_id)
    folder = CHECK_IMAGE_DIR / safe_device
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{safe_check}.jpg"
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)
    return str(path.relative_to(ROOT)).replace("\\", "/")


def read_check_image(device_id: str, check_id: str, image_path: str | None = None) -> bytes | None:
    from src.bunny_storage import (
        bunny_configured,
        check_image_object_key,
        download_bytes,
        is_bunny_path,
    )

    if image_path and is_bunny_path(image_path):
        data = download_bytes(image_path)
        if data:
            return data
    elif bunny_configured():
        data = download_bytes(check_image_object_key(device_id, check_id))
        if data:
            return data

    candidates: list[Path] = []
    if image_path and not is_bunny_path(image_path):
        candidates.append(ROOT / image_path)
    safe_device = re.sub(r"[^A-Za-z0-9_-]", "_", device_id)
    safe_check = re.sub(r"[^A-Za-z0-9_-]", "_", check_id)
    candidates.append(CHECK_IMAGE_DIR / safe_device / f"{safe_check}.jpg")
    for path in candidates:
        try:
            if path.is_file() and path.stat().st_size >= 100:
                resolved = path.resolve()
                if str(resolved).startswith(str((ROOT / "data").resolve())):
                    return path.read_bytes()
        except OSError:
            continue
    return None
