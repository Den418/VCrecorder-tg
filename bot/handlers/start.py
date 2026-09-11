from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

import config
from bot import keyboards, texts
from bot.loader import bot
from bot.states import ConnectStates
from database import crud

router = Router(name="start")


@router.message(CommandStart())
async def cmd_start(message: Message, command: CommandObject) -> None:
    referrer_id: int | None = None
    if command.args and command.args.startswith("ref_"):
        raw = command.args.removeprefix("ref_")
        if raw.isdigit():
            candidate = int(raw)
            if candidate != message.from_user.id and await crud.get_user(candidate):
                referrer_id = candidate

    user, created = await crud.get_or_create_user(
        message.from_user.id,
        message.from_user.username,
        message.from_user.full_name or "",
        message.from_user.language_code or "ru",
        referred_by=referrer_id,
    )

    if created and user.referred_by:
        await crud.extend_premium(user.referred_by, config.REFERRAL_SIGNUP_BONUS_DAYS)
        try:
            await bot.send_message(
                user.referred_by,
                f"По вашей ссылке присоединился новый пользователь — "
                f"начислено {config.REFERRAL_SIGNUP_BONUS_DAYS} дн. Премиума.",
            )
        except Exception:
            pass  # referrer may have blocked the bot — not fatal

    await message.answer(texts.WELCOME, reply_markup=keyboards.main_menu(is_admin=message.from_user.id in config.ADMIN_IDS))


@router.callback_query(F.data == "menu:root")
async def cb_menu_root(call: CallbackQuery) -> None:
    await call.message.edit_text(texts.MAIN_MENU_CAPTION, reply_markup=keyboards.main_menu(is_admin=call.from_user.id in config.ADMIN_IDS))
    await call.answer()


@router.callback_query(F.data == "menu:help")
async def cb_help(call: CallbackQuery) -> None:
    await call.message.edit_text(texts.HELP, reply_markup=keyboards.back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:connect_help")
async def cb_connect_help(call: CallbackQuery, state: FSMContext) -> None:
    await crud.refresh_premium_status(call.from_user.id)
    user = await crud.get_user(call.from_user.id)
    max_chats = config.PREMIUM_MAX_CHATS if (user and user.is_premium) else config.FREE_MAX_CHATS
    current = await crud.count_user_chats(call.from_user.id)
    if current >= max_chats:
        await call.message.edit_text(
            f"Лимит подключённых чатов ({max_chats}) исчерпан для вашего тарифа. "
            f"Оформите Премиум, чтобы подключить больше.",
            reply_markup=keyboards.back_to_menu(),
        )
        await call.answer()
        return

    worker = await crud.pick_worker_for_new_chat()
    if worker is None:
        await call.message.edit_text(
            "Сейчас нет свободных аккаунтов для записи — все заняты или пул ещё не настроен. "
            "Напишите в поддержку.",
            reply_markup=keyboards.back_to_menu(),
        )
        await call.answer()
        return

    await state.set_state(ConnectStates.waiting_invite_link)
    await state.update_data(worker_id=worker.id)
    await call.message.edit_text(
        "Пришлите пригласительную ссылку на вашу группу (Group → Add Members → Invite via Link) — "
        "аккаунт зайдёт сам, добавлять его вручную не нужно.",
        reply_markup=keyboards.back_to_menu(),
    )
    await call.answer()


@router.message(ConnectStates.waiting_invite_link)
async def on_invite_link_entered(message: Message, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    link = (message.text or "").strip()
    if not ("t.me/" in link or link.startswith("@")):
        await message.answer("Это не похоже на ссылку-приглашение. Пришлите ссылку вида https://t.me/+... заново: «➕ Подключить чат»")
        return

    await crud.create_join_request(data["worker_id"], link, message.from_user.id)
    await message.answer(
        f"Принято — зайду в группу в течение {config.WORKER_RECONCILE_INTERVAL_SECONDS} сек. "
        f"и напишу здесь, когда чат будет подключён."
    )


@router.callback_query(F.data == "menu:profile")
async def cb_profile(call: CallbackQuery) -> None:
    await crud.refresh_premium_status(call.from_user.id)
    user = await crud.get_user(call.from_user.id)
    chats_count = await crud.count_user_chats(call.from_user.id)
    minutes_today = await crud.minutes_recorded_today(call.from_user.id)

    if user and user.is_premium:
        status = "Премиум"
        premium_line = f"Действует до: {user.premium_until:%d.%m.%Y}\n"
        max_chats = config.PREMIUM_MAX_CHATS
    else:
        status = "Бесплатный"
        premium_line = ""
        max_chats = config.FREE_MAX_CHATS

    text = texts.PROFILE_TEMPLATE.format(
        status=status, premium_line=premium_line, chats_count=chats_count,
        max_chats=max_chats, minutes_today=minutes_today,
    )
    await call.message.edit_text(text, reply_markup=keyboards.back_to_menu())
    await call.answer()


@router.callback_query(F.data == "menu:referral")
async def cb_referral(call: CallbackQuery) -> None:
    referrals = await crud.count_referrals(call.from_user.id)
    link = f"https://t.me/{config.BOT_USERNAME}?start=ref_{call.from_user.id}" if config.BOT_USERNAME else "(укажите BOT_USERNAME в .env, чтобы получить ссылку)"

    text = (
        "<b>Пригласить друзей</b>\n\n"
        f"Ваша ссылка:\n<code>{link}</code>\n\n"
        f"Приглашено: {referrals}\n\n"
        f"За каждого друга, который перейдёт по ссылке — "
        f"{config.REFERRAL_SIGNUP_BONUS_DAYS} дн. Премиума сразу. "
        f"Когда друг оформит первую подписку — ещё {config.REFERRAL_PURCHASE_BONUS_DAYS} дн."
    )
    await call.message.edit_text(text, reply_markup=keyboards.back_to_menu())
    await call.answer()
