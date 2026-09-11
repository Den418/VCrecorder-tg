"""
Runtime side of the worker pool. Each ACTIVE Worker row in the database gets
its own Pyrogram Client + PyTgCalls instance here — this is what actually
joins and records calls. The admin panel (bot/handlers/workers.py, running
in the *other* process) only ever writes to the Worker table; it never talks
to these runtime objects directly. reconcile_loop() is what closes the loop:
it polls the table and starts/stops runtime clients to match, so a newly
added worker goes live and a disabled one goes offline within one poll
interval — no restart needed.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass

from pyrogram import Client
from pytgcalls import PyTgCalls

import config
from database import crud
from database.models import WorkerStatus
from userbot import recorder
from userbot import handlers as worker_handlers
from userbot.client import new_call_py, new_client

log = logging.getLogger(__name__)


@dataclass
class WorkerRuntime:
    worker_id: int
    client: Client
    call_py: PyTgCalls


_runtimes: dict[int, WorkerRuntime] = {}


def get_runtime(worker_id: int) -> WorkerRuntime | None:
    return _runtimes.get(worker_id)


def running_worker_ids() -> set[int]:
    return set(_runtimes.keys())


async def start_worker(worker_id: int) -> bool:
    if worker_id in _runtimes:
        return True

    session_string = await crud.get_worker_decrypted_session(worker_id)
    if not session_string:
        log.warning("Worker %s has no stored session yet, skipping start", worker_id)
        return False

    client = new_client(session_string, name=f"worker_{worker_id}")
    call_py = new_call_py(client)
    worker_handlers.register_handlers(client, call_py, worker_id)
    recorder.register_handlers(call_py)

    try:
        await client.start()
        await call_py.start()
    except Exception as exc:
        log.exception("Failed to start worker %s", worker_id)
        with contextlib.suppress(Exception):
            await client.stop()
        await crud.set_worker_status(worker_id, WorkerStatus.ERROR, last_error=str(exc)[:500])
        return False

    _runtimes[worker_id] = WorkerRuntime(worker_id=worker_id, client=client, call_py=call_py)
    await crud.touch_worker_seen(worker_id)
    me = await client.get_me()
    if me.username:
        await crud.set_worker_label(worker_id, f"@{me.username}")
    log.info("Worker %s started as %s (id=%s)", worker_id, me.username or me.first_name, me.id)
    return True


async def stop_worker(worker_id: int) -> None:
    runtime = _runtimes.pop(worker_id, None)
    if runtime is None:
        return
    # Any recording currently running on this worker is left alone here —
    # stop_recording() is what tears down a live call, and disabling a
    # worker shouldn't silently kill an in-progress recording out from under
    # the user. The client disconnecting mid-recording would fail the
    # ffmpeg pipe anyway, so this is a "don't disable workers mid-recording"
    # operational note more than something to code around.
    with contextlib.suppress(Exception):
        await runtime.client.stop()
    log.info("Worker %s stopped", worker_id)


async def _apply_pending_profile_updates() -> None:
    """The other half of the queue in bot/handlers/workers.py: that process
    only ever inserts a ProfileUpdate row (it has no access to any live
    connection — those live here). This applies each pending request through
    the target worker's already-connected runtime client, if it has one."""
    for update in await crud.list_pending_profile_updates():
        runtime = _runtimes.get(update.worker_id)
        if runtime is None:
            continue  # worker not currently online — picked up again next poll once it is

        try:
            if update.field == "first_name":
                await runtime.client.update_profile(first_name=update.value)
            elif update.field == "bio":
                await runtime.client.update_profile(bio=update.value)
            elif update.field == "photo":
                from pyrogram import types

                await runtime.client.set_profile_photo(photo=types.InputChatPhotoStatic(update.value))
                with contextlib.suppress(OSError):
                    import os

                    os.remove(update.value)
            else:
                raise ValueError(f"Unknown profile field {update.field!r}")
        except Exception as exc:
            log.exception("Profile update %s failed for worker %s", update.id, update.worker_id)
            await crud.complete_profile_update(update.id, success=False, error=str(exc)[:500])
        else:
            await crud.complete_profile_update(update.id, success=True)


async def _apply_pending_join_requests() -> None:
    """The other half of the queue in bot/handlers/start.py: that process
    only ever inserts a JoinRequest row. This has the target worker's
    already-connected client actually join the group, then registers it as
    a WatchedChat directly — a plain DB write, safe from either process."""
    from pyrogram.errors import (
        ChannelsTooMuch,
        FloodWait,
        InviteHashExpired,
        InviteHashInvalid,
        UserAlreadyParticipant,
    )
    from pyrogram.types import ChatJoinResultRequestSent, ChatJoinResultSuccess

    for req in await crud.list_pending_join_requests():
        runtime = _runtimes.get(req.worker_id)
        if runtime is None:
            continue  # worker not online yet — retried next poll

        try:
            result = await runtime.client.join_chat(req.invite_link)
        except UserAlreadyParticipant:
            try:
                chat = await runtime.client.get_chat(req.invite_link)
                await crud.add_watched_chat(chat.id, chat.title or "", req.requested_by, worker_id=req.worker_id)
                await crud.complete_join_request(req.id, True, chat_id=chat.id, chat_title=chat.title)
            except Exception as exc:
                await crud.complete_join_request(req.id, False, error=f"already a member, but lookup failed: {exc}")
            continue
        except InviteHashExpired:
            await crud.complete_join_request(req.id, False, error="Ссылка истекла или недействительна")
            continue
        except InviteHashInvalid:
            await crud.complete_join_request(req.id, False, error="Некорректная ссылка")
            continue
        except ChannelsTooMuch:
            await crud.complete_join_request(req.id, False, error="Аккаунт уже состоит в максимальном числе групп")
            continue
        except FloodWait as exc:
            await crud.complete_join_request(req.id, False, error=f"Telegram просит подождать {exc.value} сек., попробуйте позже")
            continue
        except Exception:
            log.exception("join_chat failed for join_request %s", req.id)
            await crud.complete_join_request(
                req.id, False, error="Не удалось подключить чат — попробуйте другую ссылку или напишите в поддержку"
            )
            continue

        if isinstance(result, ChatJoinResultRequestSent):
            await crud.complete_join_request(
                req.id, False,
                error="Группа требует подтверждения администратора — примите заявку от аккаунта внутри самой группы",
            )
            continue

        if isinstance(result, ChatJoinResultSuccess):
            chat = result.chat
            await crud.add_watched_chat(chat.id, chat.title or "", req.requested_by, worker_id=req.worker_id)
            await crud.complete_join_request(req.id, True, chat_id=chat.id, chat_title=chat.title)
            if config.ANNOUNCE_RECORDING:
                with contextlib.suppress(Exception):
                    await runtime.client.send_message(
                        chat.id,
                        "Аккаунт подключён для записи голосовых/видеочатов по команде администратора группы.\n"
                        "/record — начать запись во время звонка.",
                    )
        else:
            await crud.complete_join_request(req.id, False, error="Требуется дополнительное подтверждение (guard-бот) — вступите вручную")


async def _reconcile_once() -> None:
    active_workers = await crud.list_workers([WorkerStatus.ACTIVE])
    active_ids = {w.id for w in active_workers}
    running_ids = running_worker_ids()

    for worker in active_workers:
        if worker.id not in running_ids:
            await start_worker(worker.id)

    for stale_id in running_ids - active_ids:
        await stop_worker(stale_id)

    for worker_id in running_worker_ids():
        await crud.touch_worker_seen(worker_id)

    await _apply_pending_profile_updates()
    await _apply_pending_join_requests()


async def reconcile_loop() -> None:
    while True:
        try:
            await _reconcile_once()
        except Exception:
            log.exception("Worker pool reconcile iteration failed")
        await asyncio.sleep(config.WORKER_RECONCILE_INTERVAL_SECONDS)
