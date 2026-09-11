from __future__ import annotations

import contextlib
import logging
from pathlib import Path

import config
from database.models import StorageBackend
from filestore import s3

log = logging.getLogger(__name__)


async def store_recording(local_path: Path, key: str) -> tuple[str, StorageBackend]:
    """Called right after ffmpeg finishes. If S3 is configured, uploads and
    removes the local copy, returning the S3 object key; otherwise the file
    just stays on local disk. Either way, the returned string is what gets
    saved into Recording.file_path."""
    if not config.S3_ENABLED:
        return str(local_path), StorageBackend.LOCAL

    try:
        await s3.upload_file(local_path, key)
    except Exception:
        log.exception("S3 upload failed for %s, keeping local copy instead", local_path)
        return str(local_path), StorageBackend.LOCAL

    with contextlib.suppress(OSError):
        local_path.unlink()
    return key, StorageBackend.S3


async def resolve_delivery_source(file_path: str, backend: StorageBackend) -> str:
    """Returns something Telegram can be told to send: a local filesystem
    path (backend=LOCAL) or a presigned HTTPS URL (backend=S3) — aiogram's
    file parameters accept a plain URL string directly, so a presigned link
    can be handed straight to send_audio/send_video/send_document without
    round-tripping the bytes through this process first."""
    if backend == StorageBackend.S3:
        return await s3.presigned_url(file_path)
    return file_path


async def delete_recording(file_path: str, backend: StorageBackend) -> None:
    if backend == StorageBackend.S3:
        with contextlib.suppress(Exception):
            await s3.delete_object(file_path)
    else:
        with contextlib.suppress(OSError):
            Path(file_path).unlink(missing_ok=True)
