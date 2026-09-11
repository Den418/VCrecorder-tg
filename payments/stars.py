"""
Telegram Stars payments use the native Bot API (no external provider needed).
See: https://core.telegram.org/bots/payments-stars

Stars power a recurring monthly subscription here (not a one-time purchase
like CryptoBot/YooKassa) — created via bot.create_invoice_link(...,
subscription_period=...) rather than bot.send_invoice(), since only
create_invoice_link currently accepts subscription_period. The Bot API
currently only supports one fixed period length for subscriptions: 30 days.
See bot/handlers/premium.py for the handlers that actually drive this.
"""
from __future__ import annotations

from aiogram.types import LabeledPrice

from payments.plans import PLANS

STARS_CURRENCY = "XTR"
STARS_SUBSCRIPTION_PERIOD_SECONDS = 2592000  # 30 days — the only period Bot API currently accepts
STARS_SUBSCRIPTION_PRICE = PLANS["1m"].stars_price


def subscription_prices() -> list[LabeledPrice]:
    return [LabeledPrice(label="Премиум — подписка (30 дней)", amount=STARS_SUBSCRIPTION_PRICE)]
