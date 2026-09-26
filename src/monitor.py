"""CLI entry: run web dashboard (default), or one-shot check / telegram test."""

from __future__ import annotations

import logging
import sys

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cmd = argv[0] if argv else "web"

    if cmd in ("web", "run", "dashboard", "loop"):
        from src.webapp import main as web_main

        web_main()
        return 0

    if cmd == "check":
        from src.core import run_ping_round

        run_ping_round(source="cli-check")
        return 0

    if cmd == "test-telegram":
        from src.core import load_settings, send_telegram, telegram_configured

        settings = load_settings()
        if not telegram_configured(settings):
            print("Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID in .env first")
            return 1
        return 0 if send_telegram(settings, "Camera uptime monitor test OK") else 1

    print("Usage: python -m src.monitor [web|check|test-telegram]")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
