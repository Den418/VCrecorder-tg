from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from pathlib import Path

import filestore
from database import crud
from database.models import StorageBackend

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600


async def cleanup_loop() -> None:
    """Deletes recording files whose retention period has passed. Storage
    duration depends on the plan the owner was on when the recording
    finished (see config.FREE_STORAGE_DAYS / PREMIUM_STORAGE_DAYS)."""
    while True:
        try:
            expired = await crud.list_expired_recordings()
            for rec in expired:
                if rec.file_path:
                    await filestore.delete_recording(rec.file_path, rec.storage_backend)
                    for extra in json.loads(rec.extra_files or "[]"):
                        await filestore.delete_recording(extra, rec.storage_backend)
                    if rec.storage_backend == StorageBackend.LOCAL:
                        with contextlib.suppress(OSError):
                            Path(rec.file_path).parent.rmdir()  # only succeeds once empty
                await crud.mark_recording_purged(rec.id)
            if expired:
                log.info("Purged %d expired recording(s)", len(expired))
        except Exception:
            log.exception("Cleanup loop iteration failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
