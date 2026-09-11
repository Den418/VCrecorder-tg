"""
Premium plan catalogue. Each plan has a price per payment method since every
processor settles in a different currency (Stars, crypto, RUB).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    id: str
    title: str
    days: int
    stars_price: int          # Telegram Stars (XTR), whole units
    crypto_asset: str         # settlement asset offered on CryptoBot
    crypto_price: float
    rub_price: float          # YooKassa, RUB
    badge: str = ""           # e.g. "Выгодно" shown on the button


PLANS: dict[str, Plan] = {
    "1m": Plan(
        id="1m", title="1 месяц", days=30,
        stars_price=169, crypto_asset="USDT", crypto_price=2.99, rub_price=299,
    ),
    "3m": Plan(
        id="3m", title="3 месяца", days=90,
        stars_price=439, crypto_asset="USDT", crypto_price=7.49, rub_price=749,
        badge="−17%",
    ),
    "12m": Plan(
        id="12m", title="12 месяцев", days=365,
        stars_price=1399, crypto_asset="USDT", crypto_price=22.99, rub_price=2299,
        badge="Выгодно",
    ),
}

PLAN_ORDER = ["1m", "3m", "12m"]


def get_plan(plan_id: str) -> Plan | None:
    return PLANS.get(plan_id)
