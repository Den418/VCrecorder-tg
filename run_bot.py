"""
Entrypoint for the BOT process: the aiogram shop/control-panel bot, the
payment webhook server, the recording-cleanup loop, and the outbox poller
that delivers finished recordings.

This process never imports anything from userbot/ — it only talks to the
userbot process indirectly, through the shared database. Run this and
run_userbot.py as two separate OS processes (docker-compose.yml already
does this as two services, and run.py does it locally without Docker); a
crash or a stuck ffmpeg job in one can't take the other down with it.

    python run_bot.py
"""
from __future__ import annotations

import asyncio
import logging

import config

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("run_bot")


def _check_config() -> None:
    missing = []
    if not config.BOT_TOKEN:
        missing.append("BOT_TOKEN")
    if not config.API_ID or not config.API_HASH:
        missing.append("API_ID / API_HASH (нужны для входа аккаунтов через панель /workers)")
    if not config.WORKER_ENCRYPTION_KEY:
        missing.append("WORKER_ENCRYPTION_KEY (сгенерировать: python -m database.generate_encryption_key)")
    if missing:
        raise SystemExit(f"Заполните .env — не хватает: {', '.join(missing)} (см. .env.example)")


async def main() -> None:
    _check_config()

    # Imported here, after the config check — bot/loader.py constructs the
    # aiogram Bot object at import time, which validates BOT_TOKEN
    # immediately. Importing it before _check_config() runs would mean a
    # missing/invalid token surfaces as a raw aiogram traceback instead of
    # the clear message above.
    from bot import setup_handlers
    from bot.cleanup import cleanup_loop
    from bot.loader import bot, dp
    from bot.outbox import outbox_loop
    from bot.webhook_server import run_webhook_server
    from database.engine import init_models

    await init_models()
    setup_handlers()

    log.info("Bot process starting (polling + webhooks + cleanup + outbox)")
    await asyncio.gather(
        dp.start_polling(bot),
        run_webhook_server(),
        cleanup_loop(),
        outbox_loop(),
    )


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    # SystemExit (e.g. from _check_config()) is deliberately NOT caught here
    # — letting it propagate prints the message above to stderr and exits
    # non-zero, so a process supervisor (run.py, Docker, systemd) can tell
    # "failed to start" apart from a clean stop.
