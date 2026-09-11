"""
Encrypts Worker.session_string_encrypted at rest. A Pyrogram session string
grants full access to a Telegram account — same sensitivity class as a
password — so it's never written to the database in plaintext.

Uses Fernet (symmetric, authenticated encryption) from `cryptography`.
WORKER_ENCRYPTION_KEY must be set in .env — generate one with:

    python -m database.generate_encryption_key

There's deliberately no silent auto-generated fallback: if a key were
generated on the fly and not saved, every worker session encrypted with it
would become permanently undecryptable the moment the process restarts.
"""
from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

import config


class EncryptionNotConfigured(RuntimeError):
    pass


def _fernet() -> Fernet:
    if not config.WORKER_ENCRYPTION_KEY:
        raise EncryptionNotConfigured(
            "WORKER_ENCRYPTION_KEY is not set in .env — generate one with "
            "`python -m database.generate_encryption_key` and add it before adding worker accounts."
        )
    try:
        return Fernet(config.WORKER_ENCRYPTION_KEY.encode())
    except (ValueError, TypeError) as exc:
        raise EncryptionNotConfigured("WORKER_ENCRYPTION_KEY is not a valid Fernet key") from exc


def encrypt(plaintext: str) -> str:
    return _fernet().encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        raise EncryptionNotConfigured(
            "Could not decrypt a worker session — WORKER_ENCRYPTION_KEY doesn't match the key it was encrypted with."
        ) from exc
