"""
Factory for creating Pyrogram+PyTgCalls client pairs. Deliberately minimal —
no recording state, no long-running machinery — so it's safe for both
processes to import: the userbot process uses it for real worker runtimes
(userbot/pool.py), and the bot process uses it for the short-lived,
disconnect-when-done client used only during account login
(bot/handlers/workers.py). Nothing else in userbot/ should be imported from
the bot process — recorder.py/handlers.py/pool.py carry the actual call and
ffmpeg machinery that the two-process split exists to isolate.
"""
from __future__ import annotations

from pyrogram import Client
from pytgcalls import PyTgCalls

import config


def new_client(session_string: str | None = None, *, name: str = "worker") -> Client:
    """API_ID/API_HASH identify the *application*, shared by every worker
    account in the pool — each worker's own identity comes entirely from
    its session_string (or lack of one, for a fresh login)."""
    return Client(
        name=name,
        api_id=config.API_ID,
        api_hash=config.API_HASH,
        session_string=session_string,
        in_memory=True,
    )


def new_call_py(client: Client) -> PyTgCalls:
    return PyTgCalls(client)
