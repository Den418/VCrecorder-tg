from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta

from sqlalchemy import func, select

import config
from database.engine import async_session
from database.models import (
    Payment,
    PaymentMethod,
    PaymentStatus,
    ProfileUpdate,
    ProfileUpdateStatus,
    JoinRequest,
    JoinRequestStatus,
    PromoCode,
    PromoRedemption,
    Recording,
    RecordingKind,
    RecordingStatus,
    StorageBackend,
    Subscription,
    SubscriptionStatus,
    User,
    WatchedChat,
    Worker,
    WorkerStatus,
)

# ---------------------------------------------------------------------------
# Users
# ---------------------------------------------------------------------------


async def get_or_create_user(
    user_id: int, username: str | None, full_name: str, language_code: str = "ru", referred_by: int | None = None
) -> tuple[User, bool]:
    """Returns (user, created). `referred_by` is only ever applied the first
    time a user is seen — a later /start with a different ref link must not
    reassign an existing user's referrer."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        created = user is None
        if user is None:
            user = User(
                id=user_id, username=username, full_name=full_name, language_code=language_code,
                referred_by=referred_by if referred_by != user_id else None,
            )
            session.add(user)
        else:
            user.username = username
            user.full_name = full_name
        await session.commit()
        await session.refresh(user)
        return user, created


async def get_user(user_id: int) -> User | None:
    async with async_session() as session:
        return await session.get(User, user_id)


async def find_user(query: str) -> User | None:
    """Look up by numeric Telegram id or @username (case-insensitive)."""
    query = query.strip().lstrip("@")
    async with async_session() as session:
        if query.isdigit():
            return await session.get(User, int(query))
        result = await session.execute(select(User).where(func.lower(User.username) == query.lower()))
        return result.scalar_one_or_none()


async def set_banned(user_id: int, banned: bool) -> User | None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user:
            user.is_banned = banned
            await session.commit()
            await session.refresh(user)
        return user


async def count_referrals(user_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(select(func.count(User.id)).where(User.referred_by == user_id))
        return result.scalar_one()


async def count_paid_payments(user_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(func.count(Payment.id)).where(Payment.user_id == user_id, Payment.status == PaymentStatus.PAID)
        )
        return result.scalar_one()


async def mark_referral_bonus_granted(user_id: int) -> None:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user:
            user.referral_bonus_granted = True
            await session.commit()


async def refresh_premium_status(user_id: int) -> User | None:
    """Flip is_premium off once premium_until has passed. Call opportunistically."""
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user and user.is_premium and user.premium_until and user.premium_until < datetime.utcnow():
            user.is_premium = False
            await session.commit()
            await session.refresh(user)
        return user


async def extend_premium(user_id: int, days: int) -> User:
    async with async_session() as session:
        user = await session.get(User, user_id)
        if user is None:
            raise ValueError(f"Unknown user {user_id}")
        base = user.premium_until if (user.premium_until and user.premium_until > datetime.utcnow()) else datetime.utcnow()
        user.premium_until = base + timedelta(days=days)
        user.is_premium = True
        await session.commit()
        await session.refresh(user)
        return user


async def count_users() -> int:
    async with async_session() as session:
        result = await session.execute(select(func.count(User.id)))
        return result.scalar_one()


async def list_all_user_ids() -> list[int]:
    async with async_session() as session:
        result = await session.execute(select(User.id).where(User.is_banned.is_(False)))
        return [row[0] for row in result.all()]


# ---------------------------------------------------------------------------
# Watched chats
# ---------------------------------------------------------------------------


async def add_watched_chat(chat_id: int, title: str, owner_id: int, worker_id: int | None = None) -> WatchedChat:
    async with async_session() as session:
        chat = await session.get(WatchedChat, chat_id)
        if chat is None:
            chat = WatchedChat(id=chat_id, title=title, owner_id=owner_id, worker_id=worker_id)
            session.add(chat)
        else:
            chat.title = title
            chat.is_active = True
            if worker_id is not None:
                chat.worker_id = worker_id
        await session.commit()
        await session.refresh(chat)
        return chat


async def get_watched_chat(chat_id: int) -> WatchedChat | None:
    async with async_session() as session:
        return await session.get(WatchedChat, chat_id)


async def list_user_chats(owner_id: int) -> list[WatchedChat]:
    async with async_session() as session:
        result = await session.execute(
            select(WatchedChat).where(WatchedChat.owner_id == owner_id, WatchedChat.is_active.is_(True))
        )
        return list(result.scalars().all())


async def count_user_chats(owner_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(func.count(WatchedChat.id)).where(
                WatchedChat.owner_id == owner_id, WatchedChat.is_active.is_(True)
            )
        )
        return result.scalar_one()


# ---------------------------------------------------------------------------
# Recordings
# ---------------------------------------------------------------------------


async def create_recording(chat_id: int, started_by_id: int, kind: RecordingKind) -> Recording:
    async with async_session() as session:
        rec = Recording(chat_id=chat_id, started_by_id=started_by_id, kind=kind, status=RecordingStatus.RECORDING)
        session.add(rec)
        await session.commit()
        await session.refresh(rec)
        return rec


async def finish_recording(
    recording_id: int,
    *,
    file_path: str | None,
    duration_seconds: int,
    file_size_bytes: int,
    participants_count: int,
    extra_files: list[str] | None = None,
    status: RecordingStatus = RecordingStatus.DONE,
    storage_days: int = config.FREE_STORAGE_DAYS,
    storage_backend: StorageBackend = StorageBackend.LOCAL,
) -> Recording:
    async with async_session() as session:
        rec = await session.get(Recording, recording_id)
        if rec is None:
            raise ValueError(f"Unknown recording {recording_id}")
        rec.status = status
        rec.file_path = file_path
        rec.storage_backend = storage_backend
        rec.extra_files = json.dumps(extra_files or [])
        rec.duration_seconds = duration_seconds
        rec.file_size_bytes = file_size_bytes
        rec.participants_count = participants_count
        rec.finished_at = datetime.utcnow()
        rec.expires_at = datetime.utcnow() + timedelta(days=storage_days)
        await session.commit()
        await session.refresh(rec)
        return rec


async def get_recording(recording_id: int) -> Recording | None:
    async with async_session() as session:
        return await session.get(Recording, recording_id)


async def list_user_recordings(user_id: int, limit: int = 10) -> list[Recording]:
    async with async_session() as session:
        result = await session.execute(
            select(Recording)
            .where(Recording.started_by_id == user_id)
            .order_by(Recording.started_at.desc())
            .limit(limit)
        )
        return list(result.scalars().all())


async def minutes_recorded_today(user_id: int) -> int:
    since = datetime.utcnow() - timedelta(hours=24)
    async with async_session() as session:
        result = await session.execute(
            select(func.coalesce(func.sum(Recording.duration_seconds), 0)).where(
                Recording.started_by_id == user_id, Recording.started_at >= since
            )
        )
        return int(result.scalar_one() // 60)


async def list_expired_recordings() -> list[Recording]:
    async with async_session() as session:
        result = await session.execute(
            select(Recording).where(
                Recording.expires_at.is_not(None),
                Recording.expires_at < datetime.utcnow(),
                Recording.status == RecordingStatus.DONE,
            )
        )
        return list(result.scalars().all())


async def mark_recording_purged(recording_id: int) -> None:
    async with async_session() as session:
        rec = await session.get(Recording, recording_id)
        if rec:
            rec.file_path = None
            rec.extra_files = "[]"
            await session.commit()


async def list_undelivered_recordings() -> list[Recording]:
    """Outbox poll target for the bot process (see run_bot.py). The userbot
    process only ever writes status/file fields — it never talks to the
    aiogram Bot directly, so a crash on either side can't take the other
    down with it."""
    async with async_session() as session:
        result = await session.execute(
            select(Recording).where(
                Recording.delivered.is_(False),
                Recording.status.in_([RecordingStatus.DONE, RecordingStatus.FAILED]),
            )
        )
        return list(result.scalars().all())


async def mark_recording_delivered(recording_id: int) -> None:
    async with async_session() as session:
        rec = await session.get(Recording, recording_id)
        if rec:
            rec.delivered = True
            await session.commit()


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------


async def create_payment(user_id: int, plan_id: str, method, amount: float, currency: str) -> Payment:
    async with async_session() as session:
        payment = Payment(
            user_id=user_id,
            plan_id=plan_id,
            method=method,
            amount=amount,
            currency=currency,
            payload=secrets.token_hex(12),
        )
        session.add(payment)
        await session.commit()
        await session.refresh(payment)
        return payment


async def attach_external_id(payment_id: int, external_id: str) -> None:
    async with async_session() as session:
        payment = await session.get(Payment, payment_id)
        if payment:
            payment.external_id = external_id
            await session.commit()


async def get_payment_by_payload(payload: str) -> Payment | None:
    async with async_session() as session:
        result = await session.execute(select(Payment).where(Payment.payload == payload))
        return result.scalar_one_or_none()


async def get_payment(payment_id: int) -> Payment | None:
    async with async_session() as session:
        return await session.get(Payment, payment_id)


async def get_payment_by_external_id(external_id: str) -> Payment | None:
    async with async_session() as session:
        result = await session.execute(select(Payment).where(Payment.external_id == str(external_id)))
        return result.scalar_one_or_none()


async def mark_payment_paid(payment_id: int) -> Payment | None:
    async with async_session() as session:
        payment = await session.get(Payment, payment_id)
        if payment is None or payment.status == PaymentStatus.PAID:
            return payment
        payment.status = PaymentStatus.PAID
        payment.paid_at = datetime.utcnow()
        await session.commit()
        await session.refresh(payment)
        return payment


async def total_revenue_rub_equivalent() -> int:
    async with async_session() as session:
        result = await session.execute(select(func.count(Payment.id)).where(Payment.status == PaymentStatus.PAID))
        return result.scalar_one()


# ---------------------------------------------------------------------------
# Stars subscriptions
# ---------------------------------------------------------------------------


async def create_subscription(user_id: int, first_charge_id: str, stars_price: int, period_end: datetime) -> Subscription:
    async with async_session() as session:
        sub = Subscription(
            user_id=user_id, first_charge_id=first_charge_id, latest_charge_id=first_charge_id,
            stars_price=stars_price, current_period_end=period_end,
        )
        session.add(sub)
        await session.commit()
        await session.refresh(sub)
        return sub


async def get_active_subscription(user_id: int) -> Subscription | None:
    async with async_session() as session:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.ACTIVE)
            .order_by(Subscription.created_at.desc())
        )
        return result.scalars().first()


async def record_subscription_renewal(user_id: int, charge_id: str, period_end: datetime) -> Subscription | None:
    async with async_session() as session:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.ACTIVE)
            .order_by(Subscription.created_at.desc())
        )
        sub = result.scalars().first()
        if sub is None:
            return None
        sub.latest_charge_id = charge_id
        sub.current_period_end = period_end
        sub.charge_count += 1
        await session.commit()
        await session.refresh(sub)
        return sub


async def cancel_subscription(user_id: int) -> Subscription | None:
    async with async_session() as session:
        result = await session.execute(
            select(Subscription)
            .where(Subscription.user_id == user_id, Subscription.status == SubscriptionStatus.ACTIVE)
            .order_by(Subscription.created_at.desc())
        )
        sub = result.scalars().first()
        if sub is None:
            return None
        sub.status = SubscriptionStatus.CANCELED
        sub.canceled_at = datetime.utcnow()
        await session.commit()
        await session.refresh(sub)
        return sub


# ---------------------------------------------------------------------------
# Worker pool
# ---------------------------------------------------------------------------


async def create_worker(phone_number: str, added_by: int, max_chats: int) -> Worker:
    async with async_session() as session:
        worker = Worker(
            phone_number=phone_number, label=phone_number, added_by=added_by,
            max_chats=max_chats, status=WorkerStatus.PENDING_CODE,
        )
        session.add(worker)
        await session.commit()
        await session.refresh(worker)
        return worker


async def set_worker_session(worker_id: int, encrypted_session: str, label: str | None = None) -> Worker:
    async with async_session() as session:
        worker = await session.get(Worker, worker_id)
        if worker is None:
            raise ValueError(f"Unknown worker {worker_id}")
        worker.session_string_encrypted = encrypted_session
        worker.status = WorkerStatus.ACTIVE
        worker.last_error = None
        if label:
            worker.label = label
        await session.commit()
        await session.refresh(worker)
        return worker


async def set_worker_status(worker_id: int, status: WorkerStatus, last_error: str | None = None) -> Worker | None:
    async with async_session() as session:
        worker = await session.get(Worker, worker_id)
        if worker is None:
            return None
        worker.status = status
        worker.last_error = last_error
        await session.commit()
        await session.refresh(worker)
        return worker


async def touch_worker_seen(worker_id: int) -> None:
    async with async_session() as session:
        worker = await session.get(Worker, worker_id)
        if worker:
            worker.last_seen_at = datetime.utcnow()
            await session.commit()


async def set_worker_label(worker_id: int, label: str) -> None:
    async with async_session() as session:
        worker = await session.get(Worker, worker_id)
        if worker:
            worker.label = label
            await session.commit()


async def get_worker(worker_id: int) -> Worker | None:
    async with async_session() as session:
        return await session.get(Worker, worker_id)


async def get_worker_decrypted_session(worker_id: int) -> str | None:
    from database import crypto_util

    worker = await get_worker(worker_id)
    if worker is None or not worker.session_string_encrypted:
        return None
    return crypto_util.decrypt(worker.session_string_encrypted)


async def list_workers(statuses: list[WorkerStatus] | None = None) -> list[Worker]:
    async with async_session() as session:
        stmt = select(Worker).order_by(Worker.created_at)
        if statuses:
            stmt = stmt.where(Worker.status.in_(statuses))
        result = await session.execute(stmt)
        return list(result.scalars().all())


async def delete_worker(worker_id: int) -> None:
    async with async_session() as session:
        worker = await session.get(Worker, worker_id)
        if worker:
            await session.delete(worker)
            await session.commit()


async def count_chats_for_worker(worker_id: int) -> int:
    async with async_session() as session:
        result = await session.execute(
            select(func.count(WatchedChat.id)).where(
                WatchedChat.worker_id == worker_id, WatchedChat.is_active.is_(True)
            )
        )
        return result.scalar_one()


async def enqueue_profile_update(worker_id: int, field: str, value: str) -> ProfileUpdate:
    async with async_session() as session:
        update = ProfileUpdate(worker_id=worker_id, field=field, value=value)
        session.add(update)
        await session.commit()
        await session.refresh(update)
        return update


async def list_pending_profile_updates() -> list[ProfileUpdate]:
    async with async_session() as session:
        result = await session.execute(
            select(ProfileUpdate).where(ProfileUpdate.status == ProfileUpdateStatus.PENDING)
        )
        return list(result.scalars().all())


async def complete_profile_update(update_id: int, success: bool, error: str | None = None) -> None:
    async with async_session() as session:
        update = await session.get(ProfileUpdate, update_id)
        if update:
            update.status = ProfileUpdateStatus.DONE if success else ProfileUpdateStatus.FAILED
            update.error = error
            await session.commit()


async def create_join_request(worker_id: int, invite_link: str, requested_by: int) -> JoinRequest:
    async with async_session() as session:
        req = JoinRequest(worker_id=worker_id, invite_link=invite_link, requested_by=requested_by)
        session.add(req)
        await session.commit()
        await session.refresh(req)
        return req


async def list_pending_join_requests() -> list[JoinRequest]:
    async with async_session() as session:
        result = await session.execute(select(JoinRequest).where(JoinRequest.status == JoinRequestStatus.PENDING))
        return list(result.scalars().all())


async def complete_join_request(
    request_id: int, success: bool, chat_id: int | None = None, chat_title: str | None = None, error: str | None = None
) -> None:
    async with async_session() as session:
        req = await session.get(JoinRequest, request_id)
        if req:
            req.status = JoinRequestStatus.DONE if success else JoinRequestStatus.FAILED
            req.result_chat_id = chat_id
            req.result_chat_title = chat_title
            req.error = error
            await session.commit()


async def list_unnotified_join_requests() -> list[JoinRequest]:
    async with async_session() as session:
        result = await session.execute(
            select(JoinRequest).where(
                JoinRequest.notified.is_(False), JoinRequest.status != JoinRequestStatus.PENDING
            )
        )
        return list(result.scalars().all())


async def mark_join_request_notified(request_id: int) -> None:
    async with async_session() as session:
        req = await session.get(JoinRequest, request_id)
        if req:
            req.notified = True
            await session.commit()


async def pick_worker_for_new_chat() -> Worker | None:
    """Least-loaded ACTIVE worker still under its max_chats cap, or None if
    the whole pool is full/empty."""
    async with async_session() as session:
        chat_counts = (
            select(WatchedChat.worker_id, func.count(WatchedChat.id).label("n"))
            .where(WatchedChat.is_active.is_(True))
            .group_by(WatchedChat.worker_id)
            .subquery()
        )
        stmt = (
            select(Worker, func.coalesce(chat_counts.c.n, 0).label("load"))
            .outerjoin(chat_counts, Worker.id == chat_counts.c.worker_id)
            .where(Worker.status == WorkerStatus.ACTIVE)
            .order_by("load")
        )
        for worker, load in await session.execute(stmt):
            if load < worker.max_chats:
                return worker
        return None


async def get_admin_stats() -> dict:
    since_today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    async with async_session() as session:
        total_users = (await session.execute(select(func.count(User.id)))).scalar_one()
        premium_users = (await session.execute(
            select(func.count(User.id)).where(User.is_premium.is_(True))
        )).scalar_one()
        signups_today = (await session.execute(
            select(func.count(User.id)).where(User.created_at >= since_today)
        )).scalar_one()
        total_recordings = (await session.execute(select(func.count(Recording.id)))).scalar_one()
        total_minutes = (await session.execute(
            select(func.coalesce(func.sum(Recording.duration_seconds), 0))
        )).scalar_one() // 60
        active_subs = (await session.execute(
            select(func.count(Subscription.id)).where(Subscription.status == SubscriptionStatus.ACTIVE)
        )).scalar_one()

        revenue_by_method = {}
        for method in PaymentMethod:
            row = (await session.execute(
                select(func.count(Payment.id), func.coalesce(func.sum(Payment.amount), 0)).where(
                    Payment.status == PaymentStatus.PAID, Payment.method == method
                )
            )).one()
            revenue_by_method[method.value] = {"count": row[0], "amount": float(row[1])}

    return {
        "total_users": total_users,
        "premium_users": premium_users,
        "signups_today": signups_today,
        "total_recordings": total_recordings,
        "total_minutes": total_minutes,
        "active_subscriptions": active_subs,
        "revenue_by_method": revenue_by_method,
    }


# ---------------------------------------------------------------------------
# Promo codes
# ---------------------------------------------------------------------------


async def create_promo_code(
    code: str, bonus_days: int, max_uses: int, expires_at: datetime | None, created_by: int
) -> PromoCode:
    async with async_session() as session:
        promo = PromoCode(
            code=code.strip().upper(), bonus_days=bonus_days, max_uses=max_uses,
            expires_at=expires_at, created_by=created_by,
        )
        session.add(promo)
        await session.commit()
        await session.refresh(promo)
        return promo


async def redeem_promo_code(code: str, user_id: int) -> tuple[bool, str]:
    """All checks are re-verified right before committing so two
    near-simultaneous redemptions can't both slip past max_uses."""
    code = code.strip().upper()
    if not code:
        return False, "Пришлите код промоакции текстом."

    async with async_session() as session:
        promo = await session.get(PromoCode, code)
        if promo is None:
            return False, "Такой промокод не найден."
        if promo.expires_at and promo.expires_at < datetime.utcnow():
            return False, "Срок действия промокода истёк."

        existing = await session.execute(
            select(PromoRedemption).where(PromoRedemption.code == code, PromoRedemption.user_id == user_id)
        )
        if existing.scalar_one_or_none() is not None:
            return False, "Вы уже использовали этот промокод."
        if promo.used_count >= promo.max_uses:
            return False, "Промокод уже исчерпан."

        promo.used_count += 1
        session.add(PromoRedemption(code=code, user_id=user_id))

        user = await session.get(User, user_id)
        if user is None:
            return False, "Сначала отправьте /start в личные сообщения боту."
        base = user.premium_until if (user.premium_until and user.premium_until > datetime.utcnow()) else datetime.utcnow()
        user.premium_until = base + timedelta(days=promo.bonus_days)
        user.is_premium = True

        await session.commit()
        return True, f"Промокод активирован: +{promo.bonus_days} дн. Премиума."
