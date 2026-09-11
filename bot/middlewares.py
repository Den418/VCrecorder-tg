from __future__ import annotations

from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import TelegramObject, User

from database import crud


class BanCheckMiddleware(BaseMiddleware):
    """Silently drops updates from banned users."""

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user: User | None = data.get("event_from_user")
        if user is not None:
            record = await crud.get_user(user.id)
            if record and record.is_banned:
                return None
        return await handler(event, data)
