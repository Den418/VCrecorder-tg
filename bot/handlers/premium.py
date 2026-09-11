from __future__ import annotations

import logging
import time

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message, PreCheckoutQuery

from bot import keyboards, texts
from bot.loader import bot
from bot.premium_service import activate_premium as _activate_premium
from bot.states import PromoStates
from database import crud
from database.models import PaymentMethod
from payments import cryptobot, stars, yookassa_pay
from payments.plans import get_plan

router = Router(name="premium")
log = logging.getLogger(__name__)


@router.callback_query(F.data == "menu:premium")
async def cb_premium(call: CallbackQuery) -> None:
    await call.message.edit_text(texts.build_premium_intro(), reply_markup=keyboards.premium_plans())
    await call.answer()


@router.callback_query(F.data.startswith("plan:"))
async def cb_plan(call: CallbackQuery) -> None:
    plan_id = call.data.split(":")[1]
    plan = get_plan(plan_id)
    if not plan:
        await call.answer("Тариф не найден", show_alert=True)
        return
    await call.message.edit_text(
        texts.CHOOSE_PAYMENT_METHOD.format(plan_title=plan.title),
        reply_markup=keyboards.payment_methods(plan_id),
    )
    await call.answer()


# ---------------------------------------------------------------------------
# Telegram Stars — recurring monthly subscription
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "stars:subscribe")
async def cb_stars_subscribe(call: CallbackQuery) -> None:
    existing = await crud.get_active_subscription(call.from_user.id)
    if existing:
        await call.answer("У вас уже есть активная подписка.", show_alert=True)
        return

    payment = await crud.create_payment(
        call.from_user.id, "1m", PaymentMethod.STARS, stars.STARS_SUBSCRIPTION_PRICE, "XTR"
    )
    link = await bot.create_invoice_link(
        title="Премиум — подписка",
        description="Автопродление раз в 30 дней. Отменить можно в любой момент в разделе «Управлять подпиской».",
        payload=payment.payload,
        currency=stars.STARS_CURRENCY,
        prices=stars.subscription_prices(),
        subscription_period=stars.STARS_SUBSCRIPTION_PERIOD_SECONDS,
    )
    await call.message.edit_text(
        f"Подписка — {stars.STARS_SUBSCRIPTION_PRICE} Stars каждые 30 дней, отменить можно в любой момент.",
        reply_markup=keyboards.stars_subscribe_link(link),
    )
    await call.answer()


@router.callback_query(F.data == "stars:manage")
async def cb_stars_manage(call: CallbackQuery) -> None:
    sub = await crud.get_active_subscription(call.from_user.id)
    if sub is None:
        text = "У вас нет активной подписки через Telegram Stars."
    else:
        text = (
            f"Подписка активна.\n"
            f"Следующее списание: {sub.current_period_end:%d.%m.%Y}\n"
            f"Списаний всего: {sub.charge_count}\n"
            f"Цена: {sub.stars_price} Stars / 30 дней"
        )
    await call.message.edit_text(text, reply_markup=keyboards.subscription_manage(sub is not None))
    await call.answer()


@router.callback_query(F.data == "stars:cancel")
async def cb_stars_cancel(call: CallbackQuery) -> None:
    sub = await crud.get_active_subscription(call.from_user.id)
    if sub is None:
        await call.answer("Активная подписка не найдена.", show_alert=True)
        return

    try:
        await bot.edit_user_star_subscription(
            user_id=call.from_user.id, telegram_payment_charge_id=sub.first_charge_id, is_canceled=True,
        )
    except Exception:
        log.exception("edit_user_star_subscription failed for user %s", call.from_user.id)
        await call.answer("Не удалось отменить подписку, попробуйте ещё раз позже.", show_alert=True)
        return

    await crud.cancel_subscription(call.from_user.id)
    await call.message.edit_text(
        f"Подписка отменена. Доступ останется до {sub.current_period_end:%d.%m.%Y} — "
        f"новых списаний не будет.",
        reply_markup=keyboards.back_to_menu(),
    )
    await call.answer()


@router.pre_checkout_query()
async def process_pre_checkout(pre_checkout_query: PreCheckoutQuery) -> None:
    # Must be answered within 10 seconds per Bot API requirements. Renewal
    # charges don't go through pre_checkout_query at all — Telegram only
    # sends this for the first charge of a subscription (and one-time buys).
    payment = await crud.get_payment_by_payload(pre_checkout_query.invoice_payload)
    await pre_checkout_query.answer(ok=payment is not None)


@router.message(F.successful_payment)
async def process_successful_payment(message: Message) -> None:
    sp = message.successful_payment

    if sp.is_recurring and not sp.is_first_recurring:
        # A renewal charge Telegram billed automatically — there's no fresh
        # Payment row for this one (we never called create_invoice_link
        # again), so reconcile against the user's active Subscription
        # instead of the payload.
        existing = await crud.get_payment_by_external_id(sp.telegram_payment_charge_id)
        if existing and existing.status.value == "paid":
            log.info("Renewal charge %s already processed, ignoring duplicate update", sp.telegram_payment_charge_id)
            return

        sub = await crud.record_subscription_renewal(
            message.from_user.id, sp.telegram_payment_charge_id, sp.subscription_expiration_date,
        )
        if sub is None:
            log.warning("Renewal payment for user %s with no active Subscription row", message.from_user.id)
            return
        payment = await crud.create_payment(message.from_user.id, "1m", PaymentMethod.STARS, sub.stars_price, "XTR")
        await crud.attach_external_id(payment.id, sp.telegram_payment_charge_id)
        await crud.mark_payment_paid(payment.id)
        user = await crud.extend_premium(message.from_user.id, 30)
        await bot.send_message(
            message.from_user.id,
            texts.PAYMENT_SUCCESS.format(until=user.premium_until.strftime("%d.%m.%Y")),
        )
        return

    payment = await crud.get_payment_by_payload(sp.invoice_payload)
    if payment is None:
        log.warning("successful_payment for unknown payload %s", sp.invoice_payload)
        return

    await crud.attach_external_id(payment.id, sp.telegram_payment_charge_id)

    if sp.is_first_recurring:
        await crud.create_subscription(
            message.from_user.id, sp.telegram_payment_charge_id,
            stars.STARS_SUBSCRIPTION_PRICE, sp.subscription_expiration_date,
        )

    await _activate_premium(payment.id, payment.user_id, payment.plan_id)


# ---------------------------------------------------------------------------
# CryptoBot
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("pay:cryptobot:"))
async def cb_pay_cryptobot(call: CallbackQuery) -> None:
    plan_id = call.data.split(":")[2]
    plan = get_plan(plan_id)
    if not plan:
        await call.answer("Тариф не найден", show_alert=True)
        return

    payment = await crud.create_payment(
        call.from_user.id, plan.id, PaymentMethod.CRYPTOBOT, plan.crypto_price, plan.crypto_asset
    )
    try:
        invoice = await cryptobot.create_invoice(
            asset=plan.crypto_asset,
            amount=plan.crypto_price,
            description=f"Премиум — {plan.title}",
            payload=payment.payload,
        )
    except cryptobot.CryptoBotError:
        log.exception("CryptoBot invoice creation failed")
        await call.message.answer("Не удалось создать счёт в CryptoBot. Попробуйте другой способ оплаты или повторите позже.")
        await call.answer()
        return

    await crud.attach_external_id(payment.id, str(invoice.invoice_id))
    await call.message.answer(
        texts.PAYMENT_PENDING,
        reply_markup=keyboards.pay_link(invoice.pay_url, f"check:cryptobot:{payment.id}"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("check:cryptobot:"))
async def cb_check_cryptobot(call: CallbackQuery) -> None:
    payment_id = int(call.data.split(":")[2])
    await _manual_check(call, payment_id, provider="cryptobot")


# ---------------------------------------------------------------------------
# YooKassa
# ---------------------------------------------------------------------------


@router.callback_query(F.data.startswith("pay:yookassa:"))
async def cb_pay_yookassa(call: CallbackQuery) -> None:
    plan_id = call.data.split(":")[2]
    plan = get_plan(plan_id)
    if not plan:
        await call.answer("Тариф не найден", show_alert=True)
        return

    payment = await crud.create_payment(call.from_user.id, plan.id, PaymentMethod.YOOKASSA, plan.rub_price, "RUB")
    try:
        invoice = await yookassa_pay.create_payment(
            amount_rub=plan.rub_price, description=f"Премиум — {plan.title}", payload=payment.payload
        )
    except Exception:
        log.exception("YooKassa payment creation failed")
        await call.message.answer("Не удалось создать платёж в ЮKassa. Попробуйте другой способ оплаты или повторите позже.")
        await call.answer()
        return

    await crud.attach_external_id(payment.id, invoice.payment_id)
    await call.message.answer(
        texts.PAYMENT_PENDING,
        reply_markup=keyboards.pay_link(invoice.confirmation_url, f"check:yookassa:{payment.id}"),
    )
    await call.answer()


@router.callback_query(F.data.startswith("check:yookassa:"))
async def cb_check_yookassa(call: CallbackQuery) -> None:
    payment_id = int(call.data.split(":")[2])
    await _manual_check(call, payment_id, provider="yookassa")


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


async def _manual_check(call: CallbackQuery, payment_id: int, provider: str) -> None:
    """Fallback for when a webhook hasn't arrived yet (e.g. no public HTTPS
    configured) — lets the user confirm payment with a tap."""
    from database.engine import async_session
    from database.models import Payment, PaymentStatus

    async with async_session() as session:
        payment = await session.get(Payment, payment_id)

    if payment is None:
        await call.answer("Платёж не найден", show_alert=True)
        return
    if payment.status == PaymentStatus.PAID:
        await call.answer("Уже активировано", show_alert=True)
        return

    is_paid = False
    if provider == "cryptobot":
        status = await cryptobot.get_invoice_status(int(payment.external_id))
        is_paid = status == "paid"
    elif provider == "yookassa":
        status = await yookassa_pay.get_payment_status(payment.external_id)
        is_paid = status == "succeeded"

    if is_paid:
        await _activate_premium(payment.id, payment.user_id, payment.plan_id)
        await call.message.edit_text("Оплата подтверждена. Премиум активирован.")
    else:
        await call.answer("Пока не оплачено. Если вы уже оплатили — подождите немного и попробуйте снова.", show_alert=True)


# ---------------------------------------------------------------------------
# Promo codes
# ---------------------------------------------------------------------------


@router.callback_query(F.data == "promo:enter")
async def cb_promo_enter(call: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PromoStates.waiting_for_code)
    await call.message.edit_text(
        "Пришлите код промоакции сообщением в чат.",
        reply_markup=keyboards.back_to_menu(),
    )
    await call.answer()


_promo_fail_tracker: dict[int, list[float]] = {}
_PROMO_MAX_FAILS = 5
_PROMO_WINDOW_SECONDS = 300


@router.message(PromoStates.waiting_for_code)
async def on_promo_code_entered(message: Message, state: FSMContext) -> None:
    await state.clear()
    now = time.monotonic()
    attempts = [t for t in _promo_fail_tracker.get(message.from_user.id, []) if now - t < _PROMO_WINDOW_SECONDS]
    if len(attempts) >= _PROMO_MAX_FAILS:
        await message.answer(
            "Слишком много неверных попыток подряд. Попробуйте снова через несколько минут.",
            reply_markup=keyboards.back_to_menu(),
        )
        return

    ok, text = await crud.redeem_promo_code(message.text or "", message.from_user.id)
    if not ok:
        attempts.append(now)
        _promo_fail_tracker[message.from_user.id] = attempts
    else:
        _promo_fail_tracker.pop(message.from_user.id, None)
    await message.answer(text, reply_markup=keyboards.back_to_menu())
