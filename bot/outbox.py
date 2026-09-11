"""
The bot process's half of the outbox pattern described in
database/models.py (Recording.delivered, JoinRequest.notified). The
userbot process only ever writes status to the DB; this loop is what
actually messages the user, since only the bot process holds the aiogram
Bot instance. Polling the DB (instead of e.g. a Redis pub/sub channel)
keeps the two processes decoupled with zero extra infrastructure — an
acceptable trade-off for a few seconds of extra latency.
"""
from __future__ import annotations

import asyncio
import logging

from bot.delivery import deliver_recording
from bot.loader import bot
from database import crud
from database.models import JoinRequestStatus

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 3


async def outbox_loop() -> None:
    while True:
        try:
            pending = await crud.list_undelivered_recordings()
            for rec in pending:
                try:
                    await deliver_recording(rec)
                except Exception:
                    log.exception("Failed to deliver recording %s, will retry next poll", rec.id)
                    continue
                await crud.mark_recording_delivered(rec.id)
        except Exception:
            log.exception("Outbox loop iteration failed")

        try:
            for req in await crud.list_unnotified_join_requests():
                text = (
                    f"Готово! Чат «{req.result_chat_title}» подключён. "
                    f"Во время звонка используйте /record в группе."
                    if req.status == JoinRequestStatus.DONE
                    else f"Не удалось подключить чат: {req.error}"
                )
                try:
                    await bot.send_message(req.requested_by, text)
                except Exception:
                    log.exception("Failed to notify user %s about join_request %s", req.requested_by, req.id)
                await crud.mark_join_request_notified(req.id)
        except Exception:
            log.exception("Join-request notify pass failed")

        await asyncio.sleep(POLL_INTERVAL_SECONDS)
