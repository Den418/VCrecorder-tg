from __future__ import annotations

import contextlib
import functools
import logging

from pyrogram import Client, filters
from pyrogram.types import Message
from pytgcalls import PyTgCalls

import config
from database import crud
from database.models import RecordingKind
from userbot import recorder

log = logging.getLogger(__name__)


def _safe_handler(func):
    """Pyrogram's default behaviour for an exception raised inside a message
    handler is to log it and send nothing back — from the user's side that
    looks exactly like the bot doing nothing at all. Every handler below
    goes through this so a bug always produces a visible reply instead of
    silence."""

    @functools.wraps(func)
    async def wrapped(client: Client, message: Message) -> None:
        try:
            await func(client, message)
        except Exception:
            log.exception("Unhandled error in %s (chat=%s)", func.__name__, message.chat.id)
            with contextlib.suppress(Exception):
                await message.reply_text(
                    "Что-то пошло не так. Попробуйте ещё раз через минуту или напишите в поддержку — "
                    "я всё записал в лог."
                )

    return wrapped


async def _is_admin(message: Message) -> bool:
    if message.chat.type.name.lower() == "private":
        return False
    member = await message.chat.get_member(message.from_user.id)
    return member.status.name.lower() in ("administrator", "owner")


def _make_cmd_connect(worker_id: int):
    @_safe_handler
    async def cmd_connect(_client: Client, message: Message) -> None:
        if not await _is_admin(message):
            await message.reply_text("Подключать запись может только администратор группы.")
            return

        user, _created = await crud.get_or_create_user(
            message.from_user.id, message.from_user.username, message.from_user.first_name or ""
        )
        await crud.refresh_premium_status(user.id)
        user = await crud.get_user(user.id)

        max_chats = config.PREMIUM_MAX_CHATS if user.is_premium else config.FREE_MAX_CHATS
        current = await crud.count_user_chats(user.id)
        existing = await crud.get_watched_chat(message.chat.id)
        if existing is None and current >= max_chats:
            await message.reply_text(
                f"Лимит подключённых чатов ({max_chats}) исчерпан для вашего тарифа.\n"
                f"Оформите Премиум в личных сообщениях бота, чтобы подключить больше чатов."
            )
            return

        await crud.add_watched_chat(message.chat.id, message.chat.title or "", user.id, worker_id=worker_id)
        await message.reply_text(
            "Чат подключён.\n\n"
            "Когда в группе идёт голосовой или видеочат, администратор может запустить:\n"
            "• /record — записать аудио\n"
            "• /record video — записать аудио и видео\n"
            "• /stoprecord — остановить запись и получить файл в личных сообщениях с ботом"
        )

    return cmd_connect


def _make_cmd_record(worker_id: int, call_py: PyTgCalls):
    @_safe_handler
    async def cmd_record(client: Client, message: Message) -> None:
        if not await _is_admin(message):
            await message.reply_text("Запись может запускать только администратор группы.")
            return

        chat = await crud.get_watched_chat(message.chat.id)
        if chat is None or not chat.is_active:
            await message.reply_text("Сначала подключите чат — «➕ Подключить чат» в личных сообщениях с ботом.")
            return

        if recorder.is_recording(message.chat.id):
            await message.reply_text("Запись уже идёт. Остановить: /stoprecord")
            return

        await crud.refresh_premium_status(chat.owner_id)
        owner = await crud.get_user(chat.owner_id)

        wants_video = len(message.command) > 1 and message.command[1].lower() == "video"
        kind = RecordingKind.VIDEO if wants_video else RecordingKind.AUDIO
        is_premium = bool(owner and owner.is_premium)
        max_minutes = config.PREMIUM_MAX_MINUTES if is_premium else config.FREE_MAX_MINUTES
        storage_days = config.PREMIUM_STORAGE_DAYS if is_premium else config.FREE_STORAGE_DAYS

        status = await message.reply_text("⏳ Подключаюсь к звонку…")
        try:
            await recorder.start_recording(
                message.chat.id, message.from_user.id, kind, max_minutes,
                worker_id=worker_id, call_py=call_py, client=client, storage_days=storage_days,
            )
        except recorder.NoActiveCallError:
            await status.edit_text(
                "В чате сейчас нет активного голосового/видеочата. Запустите его и повторите /record."
            )
        except recorder.AlreadyRecordingError:
            await status.edit_text("Запись уже идёт.")
        else:
            await status.delete()

    return cmd_record


@_safe_handler
async def cmd_stop_record(_client: Client, message: Message) -> None:
    if not await _is_admin(message):
        await message.reply_text("Остановить запись может только администратор группы.")
        return

    if not recorder.is_recording(message.chat.id):
        await message.reply_text("Сейчас ничего не записывается.")
        return

    chat = await crud.get_watched_chat(message.chat.id)
    owner = await crud.get_user(chat.owner_id) if chat else None
    storage_days = config.PREMIUM_STORAGE_DAYS if (owner and owner.is_premium) else config.FREE_STORAGE_DAYS

    try:
        await recorder.stop_recording(message.chat.id, storage_days=storage_days)
    except recorder.RecorderError as exc:
        await message.reply_text(str(exc))
        return

    # That's it on this side: recorder.stop_recording() already wrote the
    # final status/file_path to the DB. Delivering the file to the user is
    # the bot process's job (it owns the aiogram Bot instance) — it picks
    # this recording up via the outbox poll in bot/outbox.py within a few
    # seconds. The userbot process deliberately never imports anything from
    # bot/ so a crash on either side can't take the other down with it.
    await message.reply_text("Готово — файл придёт в личные сообщения с ботом через несколько секунд.")


@_safe_handler
async def cmd_status(_client: Client, message: Message) -> None:
    if recorder.is_recording(message.chat.id):
        await message.reply_text("🔴 Запись сейчас идёт. /stoprecord — остановить.")
    else:
        await message.reply_text("Запись не идёт. /record — начать.")


def register_handlers(client: Client, call_py: PyTgCalls, worker_id: int) -> None:
    """Binds the group commands to one worker's Pyrogram client. Called once
    per worker as it starts (userbot/pool.py). /connect and /record need to
    know which worker they're running as (to tag the chat / to call the
    right call_py.record), so those are built per-worker via closures;
    /stoprecord and /status don't (they look everything up from the shared
    recorder._sessions registry by chat_id) and are registered as-is.
    /connect stays as a manual fallback — the primary flow is the invite-link
    one in bot/handlers/start.py, which doesn't need it at all."""
    from pyrogram.handlers import MessageHandler

    client.add_handler(MessageHandler(_make_cmd_connect(worker_id), filters.command("connect") & filters.group))
    client.add_handler(MessageHandler(_make_cmd_record(worker_id, call_py), filters.command("record") & filters.group))
    client.add_handler(MessageHandler(cmd_stop_record, filters.command("stoprecord") & filters.group))
    client.add_handler(MessageHandler(cmd_status, filters.command("status") & filters.group))
