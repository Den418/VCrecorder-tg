"""
Small aiohttp server that receives payment confirmations:

  POST /webhooks/cryptobot/<WEBHOOK_SECRET>   (CryptoBot "invoice_paid")
  POST /webhooks/yookassa/<WEBHOOK_SECRET>    (YooKassa "payment.succeeded")
  GET  /health

Both routes are also protected independently (HMAC signature for CryptoBot,
IP allowlist for YooKassa) — the secret path segment is a first line of
defense so randomly-guessed requests don't even reach that logic.

If you don't have a public HTTPS endpoint, payments still work: the bot's
"✅ Я оплатил(а)" button polls the provider directly (see
bot/handlers/premium.py::_manual_check). Webhooks just make activation
instant instead of on next tap.
"""
from __future__ import annotations

import json
import logging

from aiohttp import web

import config
from bot.premium_service import activate_premium
from database import crud
from database.models import PaymentStatus
from payments import cryptobot, yookassa_pay

log = logging.getLogger(__name__)
routes = web.RouteTableDef()


@routes.get("/health")
async def health(_request: web.Request) -> web.Response:
    return web.json_response({"ok": True})


@routes.post("/webhooks/cryptobot/{secret}")
async def cryptobot_webhook(request: web.Request) -> web.Response:
    if request.match_info["secret"] != config.WEBHOOK_SECRET:
        raise web.HTTPNotFound()

    raw_body = await request.read()
    signature = request.headers.get("crypto-pay-api-signature", "")
    if not cryptobot.verify_webhook_signature(raw_body, signature):
        log.warning("Rejected CryptoBot webhook with invalid signature")
        raise web.HTTPForbidden()

    data = json.loads(raw_body)
    if data.get("update_type") != "invoice_paid":
        return web.json_response({"ok": True})  # ignore other update types

    invoice = data["payload"]
    payload_token = invoice.get("payload", "")
    payment = await crud.get_payment_by_payload(payload_token)
    if payment is None:
        log.warning("CryptoBot webhook for unknown payload %s", payload_token)
        return web.json_response({"ok": True})

    if payment.status != PaymentStatus.PAID:
        await crud.attach_external_id(payment.id, str(invoice["invoice_id"]))
        await activate_premium(payment.id, payment.user_id, payment.plan_id)

    return web.json_response({"ok": True})


@routes.post("/webhooks/yookassa/{secret}")
async def yookassa_webhook(request: web.Request) -> web.Response:
    if request.match_info["secret"] != config.WEBHOOK_SECRET:
        raise web.HTTPNotFound()

    if not yookassa_pay.is_ip_trusted(request.remote or ""):
        log.warning("Rejected YooKassa webhook from untrusted IP %s", request.remote)
        raise web.HTTPForbidden()

    data = await request.json()
    if data.get("event") != "payment.succeeded":
        return web.json_response({"ok": True})

    obj = data.get("object", {})
    payload_token = (obj.get("metadata") or {}).get("payload", "")
    payment = await crud.get_payment_by_payload(payload_token)
    if payment is None:
        log.warning("YooKassa webhook for unknown payload %s", payload_token)
        return web.json_response({"ok": True})

    if payment.status != PaymentStatus.PAID:
        await crud.attach_external_id(payment.id, obj["id"])
        await activate_premium(payment.id, payment.user_id, payment.plan_id)

    return web.json_response({"ok": True})


def create_app() -> web.Application:
    app = web.Application()
    app.add_routes(routes)
    return app


async def run_webhook_server() -> None:
    app = create_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    await site.start()
    log.info("Webhook server listening on %s:%s", config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    if config.PUBLIC_BASE_URL:
        log.info(
            "Configure webhooks to: %s/webhooks/cryptobot/%s and %s/webhooks/yookassa/%s",
            config.PUBLIC_BASE_URL, config.WEBHOOK_SECRET, config.PUBLIC_BASE_URL, config.WEBHOOK_SECRET,
        )
