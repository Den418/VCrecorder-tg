"""
Wrapper around the official `yookassa` Python SDK. The SDK itself is
synchronous, so calls are pushed to a worker thread to avoid blocking the
event loop.
Docs: https://yookassa.ru/developers/api
Get shop_id / secret_key from your YooKassa merchant dashboard.
"""
from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from yookassa import Configuration, Payment
from yookassa.domain.common import SecurityHelper

import config

if config.YOOKASSA_SHOP_ID and config.YOOKASSA_SECRET_KEY:
    Configuration.configure(config.YOOKASSA_SHOP_ID, config.YOOKASSA_SECRET_KEY)


@dataclass
class YooKassaInvoice:
    payment_id: str
    confirmation_url: str
    status: str


async def create_payment(*, amount_rub: float, description: str, payload: str) -> YooKassaInvoice:
    def _create() -> YooKassaInvoice:
        response = Payment.create(
            {
                "amount": {"value": f"{amount_rub:.2f}", "currency": "RUB"},
                "confirmation": {"type": "redirect", "return_url": config.YOOKASSA_RETURN_URL},
                "capture": True,
                "description": description[:128],
                "metadata": {"payload": payload},
            },
            uuid.uuid4().hex,  # idempotence key — safe to retry on network errors
        )
        return YooKassaInvoice(
            payment_id=response.id,
            confirmation_url=response.confirmation.confirmation_url,
            status=response.status,
        )

    return await asyncio.to_thread(_create)


async def get_payment_status(payment_id: str) -> str:
    def _get() -> str:
        return Payment.find_one(payment_id).status

    return await asyncio.to_thread(_get)


def is_ip_trusted(ip: str) -> bool:
    """YooKassa notifications aren't HMAC-signed — instead verify the
    sender's IP against YooKassa's published ranges before trusting a
    webhook payload."""
    return SecurityHelper().is_ip_trusted(ip)
