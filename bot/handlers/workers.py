"""
Admin-only panel for the worker pool. Accounts are added the same way you'd
log into any Telegram client — phone number, SMS code, and the account's own
2FA password if it has one — never via tdata or any other session-import
shortcut. See userbot/pool.py for the runtime side that actually picks these
accounts up.

The in-progress login (a live, connected Pyrogram Client mid-handshake)
can't be serialized into aiogram's FSM storage, so it's tracked in a plain
module-level dict keyed by admin user id instead — mirroring the pattern
used for active recordings in userbot/recorder.py.
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from pyrogram.errors import (
    FloodWait,
    PasswordHashInvalid,
    PhoneCodeExpired,
    PhoneCodeInvalid,
    PhoneNumberInvalid,
    SessionPasswordNeeded,
)

import config
from bot.states import WorkerLoginStates, WorkerProfileStates
from database import crud
from database.models import WorkerStatus
from userbot.client import new_client

router = Router(name="workers")
log = logging.getLogger(__name__)


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


@dataclass
class _PendingLogin:
    worker_id: int
    client: object  # pyrogram.Client — typed loosely to avoid importing it at module scope for clarity
    phone_number: str
    phone_code_hash: str = ""


_pending: dict[int, _PendingLogin] = {}  # admin telegram user_id -> in-progress login

_STATUS_LABEL = {
    WorkerStatus.ACTIVE: "активен",
    WorkerStatus.DISABLED: "отключён",
    WorkerStatus.ERROR: "ошибка",
    WorkerStatus.PENDING_CODE: "ожидает код",
    WorkerStatus.PENDING_PASSWORD: "ожидает пароль",
}


async def _cleanup_pending(admin_id: int) -> None:
    pending = _pending.pop(admin_id, None)
    if pending is not None:
        with contextlib.suppress(Exception):
            await pending.client.disconnect()


# ---------------------------------------------------------------------------
# Panel: list + navigation
# ---------------------------------------------------------------------------


def _panel_keyboard(workers) -> InlineKeyboardBuilder:
    b = InlineKeyboardBuilder()
    for w in workers:
        icon = {"active": "🟢", "disabled": "⚪️", "error": "🔴"}.get(w.status.value, "🟡")
        b.button(text=f"{icon} {w.label or w.phone_number}", callback_data=f"worker:detail:{w.id}")
    b.button(text="➕ Добавить аккаунт", callback_data="worker:add")
    b.button(text="◀️ В меню", callback_data="menu:root")
    b.adjust(1)
    return b


@router.message(Command("workers"))
async def cmd_workers(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    await _show_panel(message)


@router.callback_query(F.data == "menu:workers")
async def cb_workers_panel(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await _show_panel(call.message, edit=True)
    await call.answer()


async def _show_panel(message: Message, edit: bool = False) -> None:
    workers = await crud.list_workers()
    if not workers:
        text = "Пул пуст. Добавьте первый аккаунт для записи."
    else:
        lines = ["<b>Пул аккаунтов-воркеров</b>\n"]
        for w in workers:
            chats = await crud.count_chats_for_worker(w.id)
            lines.append(f"{w.label or w.phone_number} — {_STATUS_LABEL[w.status]}, чатов {chats}/{w.max_chats}")
        text = "\n".join(lines)

    kb = _panel_keyboard(workers).as_markup()
    if edit:
        await message.edit_text(text, reply_markup=kb)
    else:
        await message.answer(text, reply_markup=kb)


@router.callback_query(F.data.startswith("worker:detail:"))
async def cb_worker_detail(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    worker_id = int(call.data.split(":")[2])
    worker = await crud.get_worker(worker_id)
    if worker is None:
        await call.answer("Аккаунт не найден", show_alert=True)
        return

    chats = await crud.count_chats_for_worker(worker.id)
    lines = [
        f"<b>{worker.label or worker.phone_number}</b>",
        f"Телефон: {worker.phone_number}",
        f"Статус: {_STATUS_LABEL[worker.status]}",
        f"Чатов: {chats}/{worker.max_chats}",
    ]
    if worker.last_seen_at:
        lines.append(f"На связи: {worker.last_seen_at:%d.%m.%Y %H:%M}")
    if worker.last_error:
        lines.append(f"Последняя ошибка: {worker.last_error}")

    b = InlineKeyboardBuilder()
    if worker.status == WorkerStatus.ACTIVE:
        b.button(text="⏸ Отключить", callback_data=f"worker:toggle:{worker.id}")
    elif worker.status in (WorkerStatus.DISABLED, WorkerStatus.ERROR):
        b.button(text="▶️ Включить", callback_data=f"worker:toggle:{worker.id}")
    b.button(text="✏️ Профиль", callback_data=f"worker:profile:{worker.id}")
    b.button(text="🗑 Удалить", callback_data=f"worker:delete:{worker.id}")
    b.button(text="◀️ Назад", callback_data="menu:workers")
    b.adjust(2, 1, 1)

    await call.message.edit_text("\n".join(lines), reply_markup=b.as_markup())
    await call.answer()


@router.callback_query(F.data.startswith("worker:toggle:"))
async def cb_worker_toggle(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    worker_id = int(call.data.split(":")[2])
    worker = await crud.get_worker(worker_id)
    if worker is None:
        await call.answer("Аккаунт не найден", show_alert=True)
        return
    new_status = WorkerStatus.DISABLED if worker.status == WorkerStatus.ACTIVE else WorkerStatus.ACTIVE
    await crud.set_worker_status(worker_id, new_status)
    await call.answer(f"Статус изменён: {_STATUS_LABEL[new_status]}")
    await cb_worker_detail(call)


@router.callback_query(F.data.startswith("worker:delete:"))
async def cb_worker_delete(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    worker_id = int(call.data.split(":")[2])
    await crud.delete_worker(worker_id)
    await call.answer("Аккаунт удалён")
    await _show_panel(call.message, edit=True)


# ---------------------------------------------------------------------------
# Add account — phone / SMS code / 2FA password
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "worker:add")
async def cb_worker_add(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    from bot.states import WorkerLoginStates

    await state.set_state(WorkerLoginStates.waiting_phone)
    await call.message.edit_text(
        "Отправьте номер телефона аккаунта в международном формате, например <code>+15551234567</code>.\n\n"
        "Это обычный вход в Telegram — номер, код из СМС, и пароль, если на аккаунте уже включена "
        "двухфакторная аутентификация. Импорт готовых сессий (tdata и подобное) не поддерживается."
    )
    await call.answer()


@router.message(WorkerLoginStates.waiting_phone)
async def on_phone_entered(message: Message, state: FSMContext) -> None:
    if not config.API_ID or not config.API_HASH:
        await state.clear()
        await message.answer(
            "API_ID / API_HASH не заданы в .env (получить: https://my.telegram.org → "
            "API development tools). Заполните и перезапустите процесс бота, затем начните заново: /workers"
        )
        return

    phone = (message.text or "").strip()
    if not phone.startswith("+") or not phone[1:].replace(" ", "").isdigit():
        await message.answer("Это не похоже на номер телефона. Формат: +15551234567")
        return

    worker = await crud.create_worker(phone, message.from_user.id, config.WORKER_DEFAULT_MAX_CHATS)
    client = new_client(session_string=None, name=f"login_{worker.id}")

    try:
        await client.connect()
        sent = await client.send_code(phone)
    except PhoneNumberInvalid:
        await crud.delete_worker(worker.id)
        await message.answer("Telegram не признаёт этот номер валидным. Проверьте формат и попробуйте снова.")
        return
    except FloodWait as exc:
        await crud.delete_worker(worker.id)
        await message.answer(f"Telegram просит подождать {exc.value} сек. перед повторной попыткой.")
        return
    except Exception:
        log.exception("send_code failed for %s", phone)
        await crud.delete_worker(worker.id)
        await message.answer("Не удалось запросить код. Попробуйте ещё раз позже.")
        return

    _pending[message.from_user.id] = _PendingLogin(
        worker_id=worker.id, client=client, phone_number=phone, phone_code_hash=sent.phone_code_hash,
    )
    from bot.states import WorkerLoginStates

    await state.set_state(WorkerLoginStates.waiting_code)
    await message.answer("Пришёл код в Telegram (или СМС) — отправьте его сюда цифрами.")


@router.message(WorkerLoginStates.waiting_code)
async def on_code_entered(message: Message, state: FSMContext) -> None:
    pending = _pending.get(message.from_user.id)
    if pending is None:
        await state.clear()
        await message.answer("Сессия входа истекла, начните заново: /workers")
        return

    code = (message.text or "").strip().replace(" ", "")
    try:
        await pending.client.sign_in(pending.phone_number, pending.phone_code_hash, code)
    except SessionPasswordNeeded:
        await state.set_state(WorkerLoginStates.waiting_password)
        await crud.set_worker_status(pending.worker_id, WorkerStatus.PENDING_PASSWORD)
        await message.answer("На аккаунте включён облачный пароль (2FA) — пришлите его сюда.")
        return
    except (PhoneCodeInvalid, PhoneCodeExpired):
        await message.answer("Код неверный или устарел. Пришлите новый код, либо начните заново: /workers")
        return
    except Exception:
        log.exception("sign_in failed")
        await _cleanup_pending(message.from_user.id)
        await crud.set_worker_status(pending.worker_id, WorkerStatus.ERROR, "sign_in failed")
        await state.clear()
        await message.answer("Не удалось войти. Начните заново: /workers")
        return

    await _finalize_login(message, state)


@router.message(WorkerLoginStates.waiting_password)
async def on_password_entered(message: Message, state: FSMContext) -> None:
    pending = _pending.get(message.from_user.id)
    if pending is None:
        await state.clear()
        await message.answer("Сессия входа истекла, начните заново: /workers")
        return

    password = (message.text or "").strip()
    try:
        await pending.client.check_password(password)
    except PasswordHashInvalid:
        await message.answer("Неверный пароль, попробуйте ещё раз.")
        return
    except Exception:
        log.exception("check_password failed")
        await _cleanup_pending(message.from_user.id)
        await crud.set_worker_status(pending.worker_id, WorkerStatus.ERROR, "check_password failed")
        await state.clear()
        await message.answer("Не удалось войти. Начните заново: /workers")
        return

    await _finalize_login(message, state)


async def _finalize_login(message: Message, state: FSMContext) -> None:
    from database import crypto_util

    pending = _pending.pop(message.from_user.id, None)
    if pending is None:
        return
    await state.clear()

    session_string = await pending.client.export_session_string()
    with contextlib.suppress(Exception):
        await pending.client.disconnect()

    encrypted = crypto_util.encrypt(session_string)
    await crud.set_worker_session(pending.worker_id, encrypted, label=pending.phone_number)

    await message.answer(
        f"Готово — аккаунт {pending.phone_number} добавлен и будет активен в течение "
        f"{config.WORKER_RECONCILE_INTERVAL_SECONDS} сек."
    )


# ---------------------------------------------------------------------------
# Profile editing (name / bio / photo) — needs a running (ACTIVE) worker
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("worker:profile:"))
async def cb_worker_profile_menu(call: CallbackQuery) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    worker_id = int(call.data.split(":")[2])
    worker = await crud.get_worker(worker_id)
    if worker is None:
        await call.answer("Аккаунт не найден", show_alert=True)
        return
    if worker.status != WorkerStatus.ACTIVE:
        await call.answer("Аккаунт должен быть активен, чтобы менять профиль", show_alert=True)
        return

    b = InlineKeyboardBuilder()
    b.button(text="Имя", callback_data=f"worker:setname:{worker_id}")
    b.button(text="Био", callback_data=f"worker:setbio:{worker_id}")
    b.button(text="Фото", callback_data=f"worker:setphoto:{worker_id}")
    b.button(text="◀️ Назад", callback_data=f"worker:detail:{worker_id}")
    b.adjust(3, 1)
    await call.message.edit_text("Что изменить в профиле аккаунта?", reply_markup=b.as_markup())
    await call.answer()


async def _ask_profile_field(call: CallbackQuery, state: FSMContext, target_state, prompt: str) -> None:
    worker_id = int(call.data.split(":")[2])
    await state.set_state(target_state)
    await state.update_data(worker_id=worker_id)
    await call.message.edit_text(prompt)
    await call.answer()


@router.callback_query(F.data.startswith("worker:setname:"))
async def cb_worker_setname(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await _ask_profile_field(call, state, WorkerProfileStates.waiting_first_name, "Пришлите новое имя аккаунта.")


@router.callback_query(F.data.startswith("worker:setbio:"))
async def cb_worker_setbio(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await _ask_profile_field(call, state, WorkerProfileStates.waiting_bio, "Пришлите новое описание (био) аккаунта.")


@router.callback_query(F.data.startswith("worker:setphoto:"))
async def cb_worker_setphoto(call: CallbackQuery, state: FSMContext) -> None:
    if not _is_admin(call.from_user.id):
        await call.answer()
        return
    await _ask_profile_field(call, state, WorkerProfileStates.waiting_photo, "Пришлите новое фото профиля картинкой.")


@router.message(WorkerProfileStates.waiting_first_name)
async def on_name_entered(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await crud.enqueue_profile_update(data["worker_id"], "first_name", (message.text or "").strip()[:64])
    await message.answer(f"Принято, имя обновится в течение {config.WORKER_RECONCILE_INTERVAL_SECONDS} сек.")


@router.message(WorkerProfileStates.waiting_bio)
async def on_bio_entered(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    await crud.enqueue_profile_update(data["worker_id"], "bio", (message.text or "").strip()[:70])
    await message.answer(f"Принято, био обновится в течение {config.WORKER_RECONCILE_INTERVAL_SECONDS} сек.")


@router.message(WorkerProfileStates.waiting_photo)
async def on_photo_entered(message: Message, state: FSMContext) -> None:
    if not _is_admin(message.from_user.id):
        await state.clear()
        return
    data = await state.get_data()
    await state.clear()
    if not message.photo:
        await message.answer("Это не похоже на фото. Пришлите изображение.")
        return

    # Saved to the shared storage volume both processes mount — the userbot
    # process (which owns the live connection needed to actually set it)
    # reads the file from here when it picks up the queued request below.
    dest = config.STORAGE_DIR / f"worker_photo_{data['worker_id']}.jpg"
    await message.bot.download(message.photo[-1], destination=dest)
    await crud.enqueue_profile_update(data["worker_id"], "photo", str(dest))
    await message.answer(f"Принято, фото обновится в течение {config.WORKER_RECONCILE_INTERVAL_SECONDS} сек.")
