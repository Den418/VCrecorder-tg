from __future__ import annotations

import logging

import config
from bot.loader import bot
from bot.texts import PAYMENT_SUCCESS
from database import crud
from database.models import PaymentStatus
from payments.plans import get_plan

log = logging.getLogger(__name__)


async def activate_premium(payment_id: int, user_id: int, plan_id: str) -> None:
    plan = get_plan(plan_id)
    if not plan:
        log.error("Unknown plan_id %s for payment %s", plan_id, payment_id)
        return

    payment = await crud.get_payment(payment_id)
    if payment is None:
        return
    if payment.status == PaymentStatus.PAID:
        # Already processed by a webhook / an earlier tap of "I've paid" —
        # a second call here must not double-grant days or the referral bonus.
        return

    is_first_purchase = (await crud.count_paid_payments(user_id)) == 0

    await crud.mark_payment_paid(payment_id)
    user = await crud.extend_premium(user_id, plan.days)
    await bot.send_message(user_id, PAYMENT_SUCCESS.format(until=user.premium_until.strftime("%d.%m.%Y")))

    if is_first_purchase and user.referred_by and not user.referral_bonus_granted:
        await crud.mark_referral_bonus_granted(user_id)
        await crud.extend_premium(user.referred_by, config.REFERRAL_PURCHASE_BONUS_DAYS)
        try:
            await bot.send_message(
                user.referred_by,
                f"Приглашённый вами друг оформил первую подписку: "
                f"начислено {config.REFERRAL_PURCHASE_BONUS_DAYS} дн. Премиума.",
            )
        except Exception:
            pass  # referrer may have blocked the bot — not fatal
