"""Load camera inventory from NVR DATA exports and sync to Device Master."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.db import ROOT, create_device, deactivate_device, init_db, update_device

log = logging.getLogger("camera-uptime.nvr")

NVR_DATA_DIR = ROOT / "NVR DATA"
_GENERIC_NAMES = frozenset({"ipc", "channel", "cp ip cam"})

_nvr_synced = False


@dataclass
class NvrCameraRow:
    ip: str
    service_port: int
    remote_channel: str
    name: str
    manufacturer: str
    username: str
    password: str
    nvr_source: str

    @property
    def display_name(self) -> str:
        raw = (self.name or "").strip()
        if not raw or raw.lower() in _GENERIC_NAMES or re.match(r"^channel\d+$", raw, re.I):
            return f"Camera {self.ip}"
        return raw


def _read_export_text(path: Path) -> str:
    raw = path.read_bytes()
    for enc in ("utf-8-sig", "utf-16", "utf-16-le", "utf-16-be", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("latin-1")


def _parse_export_file(path: Path) -> list[NvrCameraRow]:
    text = _read_export_text(path)
    header_line = next((ln for ln in text.splitlines() if ln.strip()), "")
    delim = "\t" if "\t" in header_line else ","
    rows: list[NvrCameraRow] = []
    for line in text.splitlines()[1:]:
        if not line.strip() or line.lstrip().startswith('"Note'):
            continue
        parts = [x.strip().strip('"') for x in line.split(delim)]
        if len(parts) < 4:
            continue
        ip = parts[0]
        if not re.match(r"^\d+\.\d+\.\d+\.\d+$", ip):
            continue
        try:
            port = int(parts[1] or "554")
        except ValueError:
            port = 554
        rows.append(
            NvrCameraRow(
                ip=ip,
                service_port=port,
                remote_channel=parts[2] if len(parts) > 2 else "1",
                name=parts[3] if len(parts) > 3 else "",
                manufacturer=(parts[4] if len(parts) > 4 else "") or "ONVIF",
                username=(parts[5] if len(parts) > 5 else "") or "admin",
                password=parts[6] if len(parts) > 6 else "",
                nvr_source=path.stem,
            )
        )
    return rows


def load_nvr_cameras() -> list[NvrCameraRow]:
    if not NVR_DATA_DIR.is_dir():
        return []
    out: list[NvrCameraRow] = []
    for path in sorted(NVR_DATA_DIR.iterdir()):
        if path.suffix.lower() not in (".csv", ".txt"):
            continue
        try:
            out.extend(_parse_export_file(path))
        except Exception:
            log.exception("Failed parsing %s", path.name)
    return out


def _pick_best_row(rows: list[NvrCameraRow]) -> NvrCameraRow:
    def score(r: NvrCameraRow) -> tuple[int, int]:
        name = (r.name or "").strip().lower()
        generic = name in _GENERIC_NAMES or bool(re.match(r"^channel\d+$", name))
        return (0 if generic else 1, len(r.display_name))

    return max(rows, key=score)


def _device_id_for_ip(ip: str) -> str:
    return "cam_" + ip.replace(".", "_")


def _deactivate_duplicate_ip_cameras(ip: str, keep_device_id: str) -> None:
    from src.db import connect

    with connect() as conn:
        rows = conn.execute(
            """
            SELECT deviceId FROM devices
            WHERE ipAddress = ? AND deviceType = 'CAMERA' AND deviceId != ?
            """,
            (ip, keep_device_id),
        ).fetchall()
    for row in rows:
        deactivate_device(row["deviceId"])


def _get_device_by_ip(ip: str) -> dict[str, Any] | None:
    init_db()
    from src.db import connect

    with connect() as conn:
        row = conn.execute(
            """
            SELECT * FROM devices
            WHERE ipAddress = ? AND deviceType = 'CAMERA'
            ORDER BY isActive DESC, updatedAt DESC
            LIMIT 1
            """,
            (ip,),
        ).fetchone()
    if row is None:
        return None
    from src.db import row_to_dict

    return row_to_dict(row)


def sync_nvr_cameras_to_db() -> dict[str, int]:
    """Upsert all cameras from NVR DATA; dedupe by IP."""
    init_db()
    raw = load_nvr_cameras()
    by_ip: dict[str, list[NvrCameraRow]] = {}
    for row in raw:
        by_ip.setdefault(row.ip, []).append(row)

    created = updated = 0
    for ip, group in by_ip.items():
        best = _pick_best_row(group)
        nvr_ids = sorted({r.nvr_source for r in group})
        nvr_label = nvr_ids[0] if len(nvr_ids) == 1 else ",".join(nvr_ids)
        payload = {
            "deviceName": best.display_name,
            "deviceType": "CAMERA",
            "ipAddress": ip,
            "nvrId": nvr_label,
            "cameraChannel": best.remote_channel or "1",
            "manufacturer": best.manufacturer,
            "servicePort": best.service_port,
            "rtspUrl": "",
            "location": f"NVR {nvr_label}",
            "status": "UNKNOWN",
            "isActive": True,
            "checkIntervalSec": 3600,
        }
        existing = _get_device_by_ip(ip)
        canonical_id = existing["deviceId"] if existing else _device_id_for_ip(ip)
        if existing:
            update_device(
                canonical_id,
                {
                    **payload,
                    "status": existing.get("status") or "UNKNOWN",
                },
            )
            updated += 1
        else:
            create_device({**payload, "deviceId": canonical_id})
            created += 1
        _deactivate_duplicate_ip_cameras(ip, canonical_id)

    summary = {"parsed": len(raw), "unique_ips": len(by_ip), "created": created, "updated": updated}
    log.info(
        "NVR sync: %s rows, %s unique IPs (%s created, %s updated)",
        summary["parsed"],
        summary["unique_ips"],
        created,
        updated,
    )
    return summary


def ensure_nvr_sync() -> dict[str, int] | None:
    """Import NVR DATA once per process, and only when the camera list is still empty.

    A full upsert rewrites every camera and blocks the status page while the
    worker is also writing ping results. Cameras already in the database stay
    put; use POST /api/sync-nvr to reload the export files.
    """
    global _nvr_synced
    if _nvr_synced:
        return None
    _nvr_synced = True
    from src.db import connect

    with connect() as conn:
        existing = conn.execute(
            "SELECT COUNT(*) AS c FROM devices WHERE deviceType = 'CAMERA' AND isActive = 1"
        ).fetchone()["c"]
    if existing:
        log.info("NVR catalog already loaded (%s cameras); skipping startup rewrite", existing)
        return None
    return sync_nvr_cameras_to_db()
