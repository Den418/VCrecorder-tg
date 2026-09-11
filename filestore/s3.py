"""
Thin async wrapper around boto3's S3 client. Works against real AWS S3 or
any S3-compatible service (MinIO, Cloudflare R2, Backblaze B2, ...) via
S3_ENDPOINT_URL. boto3 itself is synchronous, so every call is pushed to a
worker thread — the same pattern used for the YooKassa SDK elsewhere in this
project.

Recordings are uploaded once, right after ffmpeg finishes (see
userbot/recorder.py::_maybe_upload_to_s3), and the local copy is deleted —
this matters for horizontally-scaled or ephemeral-disk deployments where the
userbot worker that recorded a call might not be the same machine serving
the file later.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

import config

log = logging.getLogger(__name__)

_client = None


def _get_client():
    global _client
    if _client is None:
        import boto3

        _client = boto3.client(
            "s3",
            endpoint_url=config.S3_ENDPOINT_URL or None,
            aws_access_key_id=config.S3_ACCESS_KEY,
            aws_secret_access_key=config.S3_SECRET_KEY,
            region_name=config.S3_REGION,
        )
    return _client


async def upload_file(local_path: Path, key: str) -> None:
    def _upload() -> None:
        _get_client().upload_file(str(local_path), config.S3_BUCKET, key)

    await asyncio.to_thread(_upload)


async def delete_object(key: str) -> None:
    def _delete() -> None:
        _get_client().delete_object(Bucket=config.S3_BUCKET, Key=key)

    await asyncio.to_thread(_delete)


async def presigned_url(key: str, expires_in: int | None = None) -> str:
    def _sign() -> str:
        return _get_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": config.S3_BUCKET, "Key": key},
            ExpiresIn=expires_in or config.S3_PRESIGNED_URL_TTL_SECONDS,
        )

    return await asyncio.to_thread(_sign)
