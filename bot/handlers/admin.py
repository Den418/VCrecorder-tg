from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from aiogram import Bot, Router
from aiogram.filters import Command
from aiogram.types import Message

import config
from database import crud

router = Router(name="admin")


def _is_admin(user_id: int) -> bool:
    return user_id in config.ADMIN_IDS


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    s = await crud.get_admin_stats()
    revenue_lines = "\n".join(
        f"  {method}: {v['count']} шт., {v['amount']:.2f}"
        for method, v in s["revenue_by_method"].items()
        if v["count"] > 0
    ) or "  пока нет оплат"

    text = (
        f"Пользователей: {s['total_users']} (сегодня: +{s['signups_today']})\n"
        f"Премиум: {s['premium_users']}\n"
        f"Активных подписок Stars: {s['active_subscriptions']}\n"
        f"Записей: {s['total_recordings']} ({s['total_minutes']} мин суммарно)\n\n"
        f"Оплаты по методам:\n{revenue_lines}"
    )
    await message.answer(text)


@router.message(Command("find"))
async def cmd_find(message: Message) -> None:
    """Usage: /find <user_id или @username>"""
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) != 2:
        await message.answer("Использование: /find <user_id или @username>")
        return

    user = await crud.find_user(parts[1])
    if user is None:
        await message.answer("Пользователь не найден.")
        return

    chats = await crud.count_user_chats(user.id)
    paid = await crud.count_paid_payments(user.id)
    referrals = await crud.count_referrals(user.id)
    premium_line = f"до {user.premium_until:%d.%m.%Y}" if user.is_premium and user.premium_until else "нет"

    text = (
        f"<b>{user.full_name}</b> (@{user.username or '—'}, id <code>{user.id}</code>)\n"
        f"Премиум: {premium_line}\n"
        f"Забанен: {'да' if user.is_banned else 'нет'}\n"
        f"Подключено чатов: {chats}\n"
        f"Успешных оплат: {paid}\n"
        f"Приглашено рефералов: {referrals}\n"
        f"Регистрация: {user.created_at:%d.%m.%Y}"
    )
    await message.answer(text)


@router.message(Command("grant"))
async def cmd_grant(message: Message) -> None:
    """Usage: /grant <user_id> <days>"""
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 3:
        await message.answer("Использование: /grant <user_id> <дней>")
        return
    try:
        user_id, days = int(parts[1]), int(parts[2])
    except ValueError:
        await message.answer("user_id и дней должны быть числами")
        return

    user = await crud.get_user(user_id)
    if user is None:
        await message.answer("Пользователь ещё не запускал бота")
        return

    updated = await crud.extend_premium(user_id, days)
    await message.answer(f"Готово: премиум для {user_id} до {updated.premium_until:%d.%m.%Y}")


@router.message(Command("ban"))
async def cmd_ban(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /ban <user_id>")
        return
    user = await crud.set_banned(int(parts[1]), True)
    await message.answer("Забанен." if user else "Пользователь не найден.")


@router.message(Command("unban"))
async def cmd_unban(message: Message) -> None:
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: /unban <user_id>")
        return
    user = await crud.set_banned(int(parts[1]), False)
    await message.answer("Разбанен." if user else "Пользователь не найден.")


@router.message(Command("addpromo"))
async def cmd_addpromo(message: Message) -> None:
    """Usage: /addpromo <CODE> <bonus_days> [max_uses=1] [expires_in_days]"""
    if not _is_admin(message.from_user.id):
        return
    parts = message.text.split()
    if len(parts) < 3:
        await message.answer("Использование: /addpromo <CODE> <дней_бонуса> [макс_активаций=1] [сгорает_через_дней]")
        return
    code = parts[1]
    if len(code) < 6:
        await message.answer("Код короче 6 символов легко подобрать перебором — придумайте длиннее.")
        return
    try:
        bonus_days = int(parts[2])
        max_uses = int(parts[3]) if len(parts) > 3 else 1
        expires_at = datetime.utcnow() + timedelta(days=int(parts[4])) if len(parts) > 4 else None
    except ValueError:
        await message.answer("Дни/лимиты должны быть числами.")
        return

    promo = await crud.create_promo_code(code, bonus_days, max_uses, expires_at, message.from_user.id)
    expiry_text = f", истекает {promo.expires_at:%d.%m.%Y}" if promo.expires_at else ""
    await message.answer(
        f"Промокод <code>{promo.code}</code> создан: +{promo.bonus_days} дн., "
        f"до {promo.max_uses} активаций{expiry_text}."
    )


@router.message(Command("broadcast"))
async def cmd_broadcast(message: Message, bot: Bot) -> None:
    """Usage: reply to a message with /broadcast to send it to every user."""
    if not _is_admin(message.from_user.id):
        return
    if not message.reply_to_message:
        await message.answer("Ответьте этой командой на сообщение, которое нужно разослать.")
        return

    user_ids = await crud.list_all_user_ids()
    sent, failed = 0, 0
    status = await message.answer(f"Рассылка запущена: 0/{len(user_ids)}")
    for i, uid in enumerate(user_ids, start=1):
        try:
            await message.reply_to_message.copy_to(uid)
            sent += 1
        except Exception:
            failed += 1
        if i % 25 == 0:
            await status.edit_text(f"Рассылка: {i}/{len(user_ids)} (ошибок: {failed})")
        await asyncio.sleep(0.05)  # stay well under Bot API rate limits
    await status.edit_text(f"Готово. Отправлено: {sent}, ошибок: {failed}")
