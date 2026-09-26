"""Shared settings, camera load, ping/tcp checks, state + history, Telegram."""

from __future__ import annotations

import json
import logging
import os
import platform
import socket
import subprocess
import threading
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
STATE_PATH = ROOT / "data" / "state.json"
HISTORY_PATH = ROOT / "data" / "ping_history.jsonl"
CAMERAS_PATH = ROOT / "cameras.yaml"
ENV_PATH = ROOT / ".env"

log = logging.getLogger("camera-uptime")
_lock = threading.Lock()


@dataclass
class Camera:
    id: str
    name: str
    host: str
    port: int = 554
    enabled: bool = True


@dataclass
class Settings:
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    check_interval_sec: int = 3600
    fail_threshold: int = 1
    recovery_notify: bool = True
    icmp_timeout_ms: int = 1500
    tcp_timeout_sec: float = 3.0
    web_host: str = "0.0.0.0"
    web_port: int = 8099


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_settings() -> Settings:
    load_dotenv(ENV_PATH, override=True)
    return Settings(
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
        check_interval_sec=int(os.getenv("CHECK_INTERVAL_SEC", "3600")),
        fail_threshold=max(1, int(os.getenv("FAIL_THRESHOLD", "1"))),
        recovery_notify=os.getenv("RECOVERY_NOTIFY", "true").lower() in ("1", "true", "yes"),
        icmp_timeout_ms=int(os.getenv("ICMP_TIMEOUT_MS", "1500")),
        tcp_timeout_sec=float(os.getenv("TCP_TIMEOUT_SEC", "3")),
        web_host=os.getenv("WEB_HOST", "0.0.0.0").strip() or "0.0.0.0",
        web_port=int(os.getenv("WEB_PORT", "8099")),
    )


def load_cameras() -> list[Camera]:
    raw = yaml.safe_load(CAMERAS_PATH.read_text(encoding="utf-8")) or {}
    cams: list[Camera] = []
    for row in raw.get("cameras") or []:
        cams.append(
            Camera(
                id=str(row["id"]),
                name=str(row.get("name") or row["id"]),
                host=str(row["host"]).strip(),
                port=int(row.get("port") or 554),
                enabled=bool(row.get("enabled", True)),
            )
        )
    return cams


def icmp_ping(host: str, timeout_ms: int) -> tuple[bool, str]:
    system = platform.system().lower()
    if system == "windows":
        cmd = ["ping", "-n", "1", "-w", str(timeout_ms), host]
    else:
        sec = max(1, int((timeout_ms + 999) / 1000))
        cmd = ["ping", "-c", "1", "-W", str(sec), host]
    try:
        r = subprocess.run(
            cmd, capture_output=True, text=True, timeout=max(5, timeout_ms / 1000 + 2)
        )
        if r.returncode == 0:
            return True, "icmp ok"
        line = (r.stdout or r.stderr or "icmp fail").strip().splitlines()
        return False, (line[-1] if line else "icmp fail")[:200]
    except Exception as e:
        return False, f"icmp error: {e}"


def tcp_check(host: str, port: int, timeout_sec: float) -> tuple[bool, str]:
    try:
        with socket.create_connection((host, port), timeout=timeout_sec):
            return True, f"tcp {port} open"
    except OSError as e:
        return False, f"tcp {port}: {e}"


def probe_camera(cam: Camera, settings: Settings) -> dict:
    icmp_ok, icmp_msg = icmp_ping(cam.host, settings.icmp_timeout_ms)
    tcp_ok, tcp_msg = tcp_check(cam.host, cam.port, settings.tcp_timeout_sec)
    online = icmp_ok or tcp_ok
    return {
        "id": cam.id,
        "name": cam.name,
        "host": cam.host,
        "port": cam.port,
        "icmp_ok": icmp_ok,
        "icmp_detail": icmp_msg,
        "tcp_ok": tcp_ok,
        "tcp_detail": tcp_msg,
        "online": online,
        "detail": "; ".join(
            ([icmp_msg] if icmp_ok else []) + ([tcp_msg] if tcp_ok else [])
            or [icmp_msg, tcp_msg]
        ),
    }


def telegram_configured(settings: Settings) -> bool:
    return bool(settings.telegram_bot_token and settings.telegram_chat_id)


def send_telegram(settings: Settings, text: str) -> bool:
    if not telegram_configured(settings):
        log.info("Telegram not configured; alert logged only: %s", text.replace("\n", " | "))
        return False
    url = f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": settings.telegram_chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
            timeout=15,
        )
        if r.status_code != 200:
            log.error("Telegram HTTP %s: %s", r.status_code, r.text[:300])
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram send failed: %s", e)
        return False


def _read_state() -> dict:
    if not STATE_PATH.exists():
        return {"cameras": {}, "last_round": None}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"cameras": {}, "last_round": None}


def _write_state(data: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")


def append_history(round_doc: dict) -> None:
    HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    with HISTORY_PATH.open("a", encoding="utf-8") as f:
        f.write(json.dumps(round_doc, ensure_ascii=False) + "\n")


def read_history(limit: int = 48) -> list[dict]:
    if not HISTORY_PATH.exists():
        return []
    lines = HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    out: list[dict] = []
    for line in lines[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    out.reverse()
    return out


def run_ping_round(settings: Settings | None = None, source: str = "scheduler") -> dict:
    """Ping all enabled cameras, update state/history, Telegram on newly offline."""
    settings = settings or load_settings()
    cameras = [c for c in load_cameras() if c.enabled]
    results = [probe_camera(c, settings) for c in cameras]
    now = utc_now()
    round_id = str(uuid.uuid4())[:8]

    with _lock:
        state = _read_state()
        cam_states = state.setdefault("cameras", {})
        alerts: list[str] = []

        for r in results:
            prev = cam_states.get(r["id"], {})
            prev_online = prev.get("online")
            fails = int(prev.get("consecutive_fails") or 0)
            oks = int(prev.get("consecutive_oks") or 0)

            if r["online"]:
                oks += 1
                fails = 0
                if prev_online is False and settings.recovery_notify:
                    alerts.append(
                        f"CAMERA BACK ONLINE\n{r['name']} ({r['id']})\n"
                        f"Host: {r['host']}:{r['port']}\nDetail: {r['detail']}\nTime: {now}"
                    )
                online = True
            else:
                fails += 1
                oks = 0
                online = False
                if fails >= settings.fail_threshold and prev_online is not False:
                    alerts.append(
                        f"CAMERA OFFLINE\n{r['name']} ({r['id']})\n"
                        f"Host: {r['host']}:{r['port']}\nDetail: {r['detail']}\nTime: {now}"
                    )

            cam_states[r["id"]] = {
                "name": r["name"],
                "host": r["host"],
                "port": r["port"],
                "online": online,
                "icmp_ok": r["icmp_ok"],
                "tcp_ok": r["tcp_ok"],
                "icmp_detail": r["icmp_detail"],
                "tcp_detail": r["tcp_detail"],
                "detail": r["detail"],
                "consecutive_fails": fails,
                "consecutive_oks": oks,
                "last_check": now,
                "last_change": now
                if prev_online is not None and prev_online != online
                else prev.get("last_change"),
            }
            if prev_online is None:
                cam_states[r["id"]]["last_change"] = now

        round_doc = {
            "id": round_id,
            "checked_at": now,
            "source": source,
            "online_count": sum(1 for r in results if r["online"]),
            "offline_count": sum(1 for r in results if not r["online"]),
            "cameras": results,
        }
        state["last_round"] = round_doc
        state["updated_at"] = now
        state["telegram_configured"] = telegram_configured(settings)
        state["check_interval_sec"] = settings.check_interval_sec
        _write_state(state)
        append_history(round_doc)

    for msg in alerts:
        send_telegram(settings, msg)

    log.info(
        "Ping round %s (%s): %s online, %s offline",
        round_id,
        source,
        round_doc["online_count"],
        round_doc["offline_count"],
    )
    return round_doc


def dashboard_payload() -> dict:
    settings = load_settings()
    with _lock:
        state = _read_state()
    history = read_history(48)
    cameras_cfg = [asdict(c) for c in load_cameras()]
    return {
        "updated_at": state.get("updated_at"),
        "last_round": state.get("last_round"),
        "cameras": state.get("cameras") or {},
        "cameras_config": cameras_cfg,
        "history": history,
        "telegram_configured": telegram_configured(settings),
        "check_interval_sec": settings.check_interval_sec,
        "fail_threshold": settings.fail_threshold,
    }
