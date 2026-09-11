from __future__ import annotations

import json
import logging
from pathlib import Path

from aiogram.types import FSInputFile

import config
import filestore
from bot.loader import bot
from bot.texts import RECORDING_CAPTION, RECORDING_TOO_LARGE
from database import crud
from database.models import Recording, RecordingKind, StorageBackend

log = logging.getLogger(__name__)


def _format_duration(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


async def _send_one(recipient: int, source: str, filename: str, *, as_video: bool, caption: str | None) -> None:
    """`source` is either a local path or (for S3-backed recordings) a
    presigned URL — aiogram accepts a plain string for either, so there's no
    need to download-then-reupload when the file lives in S3; Telegram's
    servers fetch it directly from the presigned link."""
    file = source if source.startswith("http") else FSInputFile(source, filename=filename)
    try:
        if as_video:
            await bot.send_video(recipient, file, caption=caption, supports_streaming=True)
        else:
            await bot.send_audio(recipient, file, caption=caption, title=filename)
    except Exception:
        log.exception("Primary send failed for %s, falling back to send_document", filename)
        await bot.send_document(recipient, file, caption=caption)


async def deliver_recording(rec: Recording) -> None:
    chat = await crud.get_watched_chat(rec.chat_id)
    chat_title = chat.title if chat else str(rec.chat_id)
    recipient = rec.started_by_id

    if rec.status.value == "failed" or not rec.file_path:
        await bot.send_message(
            recipient,
            f"Не удалось обработать запись «{chat_title}». Попробуйте ещё раз или напишите в поддержку.",
        )
        return

    caption = RECORDING_CAPTION.format(
        chat_title=chat_title,
        duration=_format_duration(rec.duration_seconds),
        participants=rec.participants_count,
    )

    if rec.storage_backend == StorageBackend.LOCAL and not Path(rec.file_path).exists():
        await bot.send_message(recipient, f"Файл записи «{chat_title}» больше не найден на диске.")
        return

    if rec.file_size_bytes > config.MAX_TELEGRAM_UPLOAD_BYTES:
        size_mb = rec.file_size_bytes / (1024 * 1024)
        location = rec.file_path if rec.storage_backend == StorageBackend.LOCAL else f"S3: {rec.file_path}"
        await bot.send_message(
            recipient,
            caption + "\n\n" + RECORDING_TOO_LARGE.format(
                size_mb=size_mb, limit_mb=config.MAX_TELEGRAM_UPLOAD_BYTES / (1024 * 1024)
            ) + f"\n\n<code>{location}</code>",
        )
        return

    filename = Path(rec.file_path).name
    source = await filestore.resolve_delivery_source(rec.file_path, rec.storage_backend)
    is_video = rec.kind == RecordingKind.VIDEO and filename.endswith(".mp4")
    await _send_one(recipient, source, filename, as_video=is_video, caption=caption)

    for extra in json.loads(rec.extra_files or "[]"):
        extra_source = await filestore.resolve_delivery_source(extra, rec.storage_backend)
        extra_name = Path(extra).name
        file = extra_source if extra_source.startswith("http") else FSInputFile(extra_source, filename=extra_name)
        try:
            await bot.send_document(recipient, file, caption="Отдельные дорожки по каждому участнику")
        except Exception:
            # Bonus file only (e.g. too large, or transient network issue) —
            # the primary recording above already delivered successfully,
            # so log and move on rather than surfacing this as a failure.
            log.warning("Could not deliver bonus multitrack file %s for recording %s", extra_name, rec.id)
