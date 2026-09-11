from __future__ import annotations

from bot.loader import dp
from bot.middlewares import BanCheckMiddleware


def setup_handlers() -> None:
    from bot.handlers import admin, premium, recordings, start, workers

    dp.message.middleware(BanCheckMiddleware())
    dp.callback_query.middleware(BanCheckMiddleware())

    dp.include_router(start.router)
    dp.include_router(premium.router)
    dp.include_router(recordings.router)
    dp.include_router(admin.router)
    dp.include_router(workers.router)
