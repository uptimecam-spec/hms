"""Camera snapshot storage, health captures, and RTSP probes."""
from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import threading
import time
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


def jpeg_dimensions(data: bytes) -> tuple[int, int] | None:
    """Return (width, height) from a JPEG SOF marker, or None if it is not a JPEG."""
    if not data or not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9):
            index += 2
            continue
        if index + 4 > length:
            return None
        segment = int.from_bytes(data[index + 2 : index + 4], "big")
        if segment < 2 or index + 2 + segment > length:
            return None
        if marker in (0xC0, 0xC1, 0xC2) and segment >= 7:
            height = int.from_bytes(data[index + 5 : index + 7], "big")
            width = int.from_bytes(data[index + 7 : index + 9], "big")
            if width > 0 and height > 0:
                return width, height
            return None
        index += 2 + segment
    return None


# Below this long edge the still is an extra stream (CIF/D1) and looks soft
# when the preview is shown. 720p and above is the camera's main picture.
MAIN_PREVIEW_EDGE = 960


def _long_edge(data: bytes | None) -> int:
    size = jpeg_dimensions(data) if data else None
    return max(size) if size else 0


def clearer_jpeg(primary: bytes | None, alternate: bytes | None) -> bytes | None:
    """Keep the larger frame. A substream still must not replace a main-stream still."""
    if not alternate:
        return primary
    if not primary:
        return alternate
    primary_size = jpeg_dimensions(primary)
    alternate_size = jpeg_dimensions(alternate)
    if (
        primary_size
        and alternate_size
        and (alternate_size[0] * alternate_size[1]) > (primary_size[0] * primary_size[1])
    ):
        return alternate
    return primary


def _ffmpeg_grab(rtsp_url: str, timeout_sec: float, *, keyframe_only: bool) -> bytes | None:
    ffmpeg = _ffmpeg_path()
    if not ffmpeg:
        return None
    # No scale filter. q:v 2 is the highest practical mjpeg quality (2–31).
    # skip_frame nokey is an input option so the still is a real keyframe,
    # not a predicted frame that looks smeared when its references are missing.
    cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-rtsp_transport", "tcp"]
    if keyframe_only:
        cmd.extend(["-skip_frame", "nokey"])
    cmd.extend([
        "-i", rtsp_url,
        "-an",
        "-frames:v", "1",
        "-q:v", "2",
        "-f", "image2pipe",
        "-vcodec", "mjpeg",
        "pipe:1",
    ])
    try:
        with _get_semaphore():
            proc = subprocess.run(
                cmd, capture_output=True, timeout=timeout_sec + 2, check=False
            )
    except subprocess.TimeoutExpired:
        log.debug("ffmpeg snapshot timed out")
        return None
    except Exception:
        log.debug("ffmpeg snapshot error")
        return None
    if proc.returncode == 0 and proc.stdout.startswith(b"\xff\xd8"):
        return proc.stdout
    return None


def capture_snapshot(
    rtsp_url: str, *, timeout_sec: float = 12.0, allow_predicted_frame: bool = True
) -> bytes | None:
    image = _ffmpeg_grab(rtsp_url, timeout_sec, keyframe_only=True)
    if image or not allow_predicted_frame:
        return image
    return _ffmpeg_grab(rtsp_url, max(4.0, timeout_sec / 2), keyframe_only=False)


def _preview_jpeg(device: dict) -> bytes | None:
    """Still from the camera IP only. Never log into an NVR for a picture."""
    camera_ip = (device.get("ipAddress") or "").strip()
    if not camera_ip:
        return None
    image = http_snapshot(camera_ip, 1)
    if image:
        return image
    url = build_rtsp_url(camera_ip, 1, direct_camera=True)
    if url:
        return capture_snapshot(url, timeout_sec=8)
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
    url = (device.get("rtspUrl") or "").strip()
    return capture_snapshot(url, timeout_sec=10) if url and _ffmpeg_path() else None


def _rtsp_still(
    host: str, channel: int, *, direct_camera: bool, timeout_sec: float
) -> bytes | None:
    """Main stream first. The extra stream is used only when the main stream has no JPEG."""
    image = None
    for subtype, predicted in ((0, True), (1, False)):
        url = build_rtsp_url(host, channel, direct_camera=direct_camera, subtype=subtype)
        if not url:
            return image
        grabbed = capture_snapshot(
            url, timeout_sec=timeout_sec, allow_predicted_frame=predicted
        )
        image = clearer_jpeg(image, grabbed)
        if _long_edge(image) >= MAIN_PREVIEW_EDGE:
            return image
        if image and subtype == 0:
            # A small main-stream still is the camera's real picture. Do not
            # replace it by also opening the extra stream.
            return image
    return image


def _camera_jpeg(device: dict) -> bytes | None:
    """Still from the camera IP (or a custom camera RTSP URL). Never from an NVR."""
    image = _custom_rtsp_jpeg(device)
    if _long_edge(image) >= MAIN_PREVIEW_EDGE:
        return image
    camera_ip = (device.get("ipAddress") or "").strip()
    if not camera_ip:
        return image
    image = clearer_jpeg(image, http_snapshot(camera_ip, 1))
    if _long_edge(image) >= MAIN_PREVIEW_EDGE:
        return image
    for subtype in (0, 1):
        image = clearer_jpeg(image, http_mjpeg_frame(camera_ip, 1, subtype=subtype))
        if _long_edge(image) >= MAIN_PREVIEW_EDGE:
            return image
    if not _ffmpeg_path():
        return image
    return clearer_jpeg(
        image,
        _rtsp_still(camera_ip, 1, direct_camera=True, timeout_sec=8),
    )


def _save_jpeg(device_id: str, data: bytes) -> None:
    path = snapshot_file(device_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def store_all_camera_snapshots(workers: int = 20) -> dict[str, int]:
    from src.db import list_devices

    devices = [
        d for d in list_devices(include_inactive=False)
        if d.get("deviceType") == "CAMERA" and d.get("isActive")
    ]
    pending = [d for d in devices if _long_edge(read_stored_snapshot(d["deviceId"])) < MAIN_PREVIEW_EDGE]
    saved = 0

    def grab(device: dict) -> bool:
        try:
            with _capture_gate(device):
                image = _camera_jpeg(device)
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
    log.info(
        "Stored snapshots: %s new, %s already on disk, %s cameras",
        saved, already, len(devices),
    )
    return {"saved": saved, "already": already, "total": len(devices)}


def ensure_stored_snapshots() -> None:
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
    available, _version = preview_file_meta(device_id)
    if not available:
        return None, "no stored preview"
    data = read_stored_snapshot(device_id)
    return (data, None) if data else (None, "no stored preview")


def refresh_device_snapshot(device_id: str) -> tuple[bool, str | None]:
    device = get_device(device_id)
    if not device:
        return False, "camera not found"
    try:
        image = _camera_jpeg(device)
    except Exception:
        log.exception("Preview refresh failed for %s", device_id)
        return False, "preview capture error"
    if not image or len(image) < 800 or not image.startswith(b"\xff\xd8"):
        return False, "preview unavailable"
    _save_jpeg(device_id, image)
    return True, None


def _capture_gate(device: dict) -> threading.Semaphore:
    """Limit concurrent captures per camera IP. Do not key this on an NVR host."""
    key = (device.get("ipAddress") or device.get("deviceId") or "default").strip()
    with _nvr_sem_lock:
        sem = _nvr_semaphores.get(key)
        if sem is None:
            load_dotenv(ROOT / ".env", override=True)
            try:
                per_camera = max(1, min(6, int(os.getenv("HEALTH_CHECK_PER_CAMERA", os.getenv("HEALTH_CHECK_PER_NVR", "2")))))
            except ValueError:
                per_camera = 2
            sem = threading.Semaphore(per_camera)
            _nvr_semaphores[key] = sem
        return sem


def capture_preview_for_check(device: dict) -> tuple[bytes | None, str | None]:
    with _capture_gate(device):
        try:
            image = _camera_jpeg(device)
        except Exception as exc:
            log.debug("Check preview failed for %s: %s", device.get("deviceId"), exc)
            return None, str(exc)
    if not image or len(image) < 800 or not image.startswith(b"\xff\xd8"):
        return None, "preview unavailable"
    return image, None


def capture_live_frame(device_id: str) -> tuple[bytes | None, str | None]:
    """One preview frame from the camera IP main stream when available."""
    device = get_device(device_id)
    if not device:
        return None, "camera not found"
    image, err = capture_preview_for_check(device)
    if not image:
        return None, err or "live frame unavailable"
    return image, None


def capture_rtsp_frame(device_id: str) -> tuple[bytes | None, str | None, str | None]:
    """Capture strictly through camera RTSP, bypassing HTTP snapshot/MJPEG endpoints."""
    device = get_device(device_id)
    if not device:
        return None, None, "camera not found"
    if not _ffmpeg_path():
        return None, None, "ffmpeg not configured"

    custom = (device.get("rtspUrl") or "").strip()
    if custom:
        image = capture_snapshot(custom, timeout_sec=10)
        if image:
            return image, "custom", None

    camera_ip = (device.get("ipAddress") or "").strip()
    if camera_ip:
        image = _rtsp_still(camera_ip, 1, direct_camera=True, timeout_sec=10)
        if image:
            return image, "camera", None
    return None, None, "RTSP stream unavailable"


def check_rtsp_stream(
    device_id: str, *, sample_gap_sec: float = 2.0
) -> tuple[dict, bytes | None]:
    """Read two RTSP frames and report whether video is changing."""
    started = time.monotonic()
    first, source, error = capture_rtsp_frame(device_id)
    if not first:
        return ({
            "ok": False,
            "working": False,
            "frameChanged": False,
            "source": source,
            "error": error or "RTSP stream unavailable",
            "elapsedMs": round((time.monotonic() - started) * 1000),
        }, None)
    time.sleep(max(0.5, min(5.0, sample_gap_sec)))
    second, second_source, second_error = capture_rtsp_frame(device_id)
    if not second:
        return ({
            "ok": False,
            "working": False,
            "frameChanged": False,
            "source": source,
            "firstFrameBytes": len(first),
            "error": second_error or "second RTSP frame unavailable",
            "elapsedMs": round((time.monotonic() - started) * 1000),
        }, first)
    changed = first != second
    return ({
        "ok": True,
        "working": True,
        "frameChanged": changed,
        "source": second_source or source,
        "firstFrameBytes": len(first),
        "secondFrameBytes": len(second),
        "error": None,
        "elapsedMs": round((time.monotonic() - started) * 1000),
    }, second)


def save_check_image(device_id: str, check_id: str, data: bytes) -> str:
    from src.bunny_storage import bunny_configured, check_image_object_key, upload_bytes

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


def read_check_image(
    device_id: str, check_id: str, image_path: str | None = None
) -> bytes | None:
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
