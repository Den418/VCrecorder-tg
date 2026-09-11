"""
Entrypoint for the USERBOT process: runs the worker pool
(userbot/pool.py) — one Pyrogram + PyTgCalls client per Worker row in the
database, started/stopped automatically to match what the admin panel adds,
disables, or removes (see userbot/pool.py::reconcile_loop).

This process never imports anything from bot/ — it only writes recording
and worker status to the shared database; the bot process delivers finished
files and runs the account-login flow (see bot/outbox.py,
bot/handlers/workers.py). Run this and run_bot.py as two separate OS
processes (or just `python run.py` to start both together).

    python run_userbot.py
"""
from __future__ import annotations

import asyncio
import logging

import config
from database.engine import init_models
from userbot import pool

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
)
log = logging.getLogger("run_userbot")


def _check_config() -> None:
    missing = []
    if not config.API_ID or not config.API_HASH:
        missing.append("API_ID / API_HASH")
    if not config.WORKER_ENCRYPTION_KEY:
        missing.append("WORKER_ENCRYPTION_KEY (сгенерировать: python -m database.generate_encryption_key)")
    if missing:
        raise SystemExit(f"Заполните .env — не хватает: {', '.join(missing)}")


async def main() -> None:
    _check_config()
    await init_models()

    log.info(
        "Userbot process starting — pool reconciles every %ss (аккаунты добавляются через панель в боте)",
        config.WORKER_RECONCILE_INTERVAL_SECONDS,
    )
    await pool.reconcile_loop()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    # SystemExit (e.g. from _check_config()) is deliberately NOT caught here
    # — see run_bot.py for why.
