from __future__ import annotations

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

import config
from database.models import Recording
from payments.plans import PLAN_ORDER, PLANS


def main_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="🎙 Мои записи", callback_data="menu:recordings")
    b.button(text="⭐ Премиум", callback_data="menu:premium")
    b.button(text="➕ Подключить чат", callback_data="menu:connect_help")
    b.button(text="👤 Профиль", callback_data="menu:profile")
    b.button(text="👥 Пригласить друзей", callback_data="menu:referral")
    b.button(text="ℹ️ Помощь", callback_data="menu:help")
    rows = [2, 2, 2]
    if is_admin:
        b.button(text="🛠 Пул аккаунтов", callback_data="menu:workers")
        rows.append(1)
    b.adjust(*rows)
    return b.as_markup()


def back_to_menu() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="◀️ В меню", callback_data="menu:root")
    return b.as_markup()


_SHORT_DURATION = {"1m": "1 мес", "3m": "3 мес", "12m": "12 мес"}


def premium_plans() -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(
        text=f"⭐ Подписка — {PLANS['1m'].stars_price} Stars/мес",
        callback_data="stars:subscribe",
    )
    for plan_id in PLAN_ORDER:
        plan = PLANS[plan_id]
        label = f"{_SHORT_DURATION[plan_id]} — {plan.rub_price:.0f}₽"
        if plan.badge:
            label += f" {plan.badge}"
        b.button(text=label, callback_data=f"plan:{plan_id}")
    b.button(text="🔁 Подписка", callback_data="stars:manage")
    b.button(text="🎁 Промокод", callback_data="promo:enter")
    b.button(text="◀️ В меню", callback_data="menu:root")
    b.adjust(1, 3, 2, 1)
    return b.as_markup()


def payment_methods(plan_id: str) -> InlineKeyboardMarkup:
    plan = PLANS[plan_id]
    b = InlineKeyboardBuilder()
    b.button(text=f"💎 {plan.crypto_price:g} {plan.crypto_asset} (крипта)", callback_data=f"pay:cryptobot:{plan_id}")
    b.button(text=f"💳 {plan.rub_price:.0f}₽ картой (ЮKassa)", callback_data=f"pay:yookassa:{plan_id}")
    b.button(text="◀️ Назад", callback_data="menu:premium")
    b.adjust(1)
    return b.as_markup()


def pay_link(url: str, check_callback: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="💳 Перейти к оплате", url=url)
    b.button(text="✅ Я оплатил(а)", callback_data=check_callback)
    b.adjust(1)
    return b.as_markup()


def stars_subscribe_link(url: str) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="⭐ Оформить подписку", url=url)
    b.button(text="◀️ Назад", callback_data="menu:premium")
    b.adjust(1)
    return b.as_markup()


def subscription_manage(has_active: bool) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    if has_active:
        b.button(text="Отменить подписку", callback_data="stars:cancel")
    b.button(text="◀️ Назад", callback_data="menu:premium")
    b.adjust(1)
    return b.as_markup()


def recording_actions(rec: Recording) -> InlineKeyboardMarkup:
    b = InlineKeyboardBuilder()
    b.button(text="📥 Получить файл", callback_data=f"rec:send:{rec.id}")
    b.adjust(1)
    return b.as_markup()
