"""Token helpers for the on-site live bridge (Cloudflare Tunnel → LAN capture)."""

from __future__ import annotations

import os
import secrets
from typing import Any

from dotenv import load_dotenv

from src.db import ROOT


def _load_env() -> None:
    load_dotenv(ROOT / ".env", override=True)


def live_bridge_token() -> str:
    _load_env()
    return (os.getenv("LIVE_BRIDGE_TOKEN") or "").strip()


def live_bridge_configured() -> bool:
    return bool(live_bridge_token())


def live_bridge_url() -> str:
    """Public HTTPS base used by Vercel to call the on-site bridge."""
    _load_env()
    return (os.getenv("LIVE_BRIDGE_URL") or "").strip().rstrip("/")


def cloud_live_bridge_ready() -> bool:
    """True when Vercel can proxy Check Live to the on-site bridge."""
    _load_env()
    url = (os.getenv("LIVE_BRIDGE_URL") or "").strip()
    token = (os.getenv("LIVE_BRIDGE_TOKEN") or "").strip()
    return bool(url and token)


def extract_bearer_or_header(authorization: str | None, header_token: str | None) -> str:
    auth = (authorization or "").strip()
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return (header_token or "").strip()


def token_matches(provided: str) -> bool:
    expected = live_bridge_token()
    if not expected or not provided:
        return False
    return secrets.compare_digest(provided, expected)


def bridge_live_path(device_id: str) -> str:
    return f"/bridge/v1/live/{device_id}.jpg"


def bridge_health_path() -> str:
    return "/bridge/v1/health"
