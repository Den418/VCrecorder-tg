"""
Minimal async client for the CryptoBot "Crypto Pay" API.
Docs: https://help.send.tg/en/articles/10279948-crypto-pay-api

Get a token: open @CryptoBot -> Crypto Pay -> Create App.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
from dataclasses import dataclass
from typing import Any

import aiohttp

import config

log = logging.getLogger(__name__)

_BASE_URL = "https://testnet-pay.crypt.bot/api" if config.CRYPTOBOT_TESTNET else "https://pay.crypt.bot/api"


class CryptoBotError(RuntimeError):
    pass


@dataclass
class CryptoInvoice:
    invoice_id: int
    status: str
    pay_url: str
    asset: str
    amount: str


async def _call(method: str, **params: Any) -> dict:
    if not config.CRYPTOBOT_TOKEN:
        raise CryptoBotError("CRYPTOBOT_TOKEN is not configured")
    headers = {"Crypto-Pay-API-Token": config.CRYPTOBOT_TOKEN}
    async with aiohttp.ClientSession() as session:
        async with session.post(f"{_BASE_URL}/{method}", json=params, headers=headers, timeout=aiohttp.ClientTimeout(total=15)) as resp:
            data = await resp.json()
    if not data.get("ok"):
        raise CryptoBotError(str(data.get("error")))
    return data["result"]


async def create_invoice(*, asset: str, amount: float, description: str, payload: str, expires_in: int = 3600) -> CryptoInvoice:
    result = await _call(
        "createInvoice",
        currency_type="crypto",
        asset=asset,
        amount=str(amount),
        description=description[:1024],
        payload=payload[:4000],
        expires_in=expires_in,
        allow_comments=False,
        allow_anonymous=True,
    )
    return CryptoInvoice(
        invoice_id=result["invoice_id"],
        status=result["status"],
        pay_url=result.get("bot_invoice_url") or result.get("pay_url"),
        asset=result["asset"],
        amount=result["amount"],
    )


async def get_invoice_status(invoice_id: int) -> str | None:
    result = await _call("getInvoices", invoice_ids=str(invoice_id))
    items = result.get("items", [])
    return items[0]["status"] if items else None


def verify_webhook_signature(raw_body: bytes, signature: str) -> bool:
    """
    Verify the `crypto-pay-api-signature` header exactly as documented:
    secret = SHA256(api_token); expected = HMAC-SHA256(secret, raw_body).

    IMPORTANT: hash the *raw* request bytes, never a re-serialized copy of
    the parsed JSON — re-encoding can reorder keys/whitespace and silently
    break verification.
    """
    if not config.CRYPTOBOT_TOKEN or not signature:
        return False
    secret = hashlib.sha256(config.CRYPTOBOT_TOKEN.encode()).digest()
    computed = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(computed, signature)
