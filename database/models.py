from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import BigInteger, Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class PaymentMethod(str, enum.Enum):
    STARS = "stars"
    CRYPTOBOT = "cryptobot"
    YOOKASSA = "yookassa"


class PaymentStatus(str, enum.Enum):
    PENDING = "pending"
    PAID = "paid"
    EXPIRED = "expired"
    FAILED = "failed"


class RecordingStatus(str, enum.Enum):
    RECORDING = "recording"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


class RecordingKind(str, enum.Enum):
    AUDIO = "audio"
    VIDEO = "video"


class StorageBackend(str, enum.Enum):
    LOCAL = "local"
    S3 = "s3"


class SubscriptionStatus(str, enum.Enum):
    ACTIVE = "active"
    CANCELED = "canceled"


class WorkerStatus(str, enum.Enum):
    PENDING_CODE = "pending_code"          # login started, SMS code requested
    PENDING_PASSWORD = "pending_password"  # code accepted, 2FA password requested
    ACTIVE = "active"
    DISABLED = "disabled"                  # turned off from the panel, session kept
    ERROR = "error"                        # e.g. logged out / banned — see last_error


class ProfileUpdateStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class JoinRequestStatus(str, enum.Enum):
    PENDING = "pending"
    DONE = "done"
    FAILED = "failed"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram user id
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    full_name: Mapped[str] = mapped_column(String(255), default="")
    language_code: Mapped[str] = mapped_column(String(8), default="ru")

    is_premium: Mapped[bool] = mapped_column(Boolean, default=False)
    premium_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    referred_by: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    referral_bonus_granted: Mapped[bool] = mapped_column(Boolean, default=False)

    is_banned: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    chats: Mapped[list["WatchedChat"]] = relationship(back_populates="owner")
    payments: Mapped[list["Payment"]] = relationship(back_populates="user")


class WatchedChat(Base):
    """A group the userbot has been added to and is allowed to record."""

    __tablename__ = "watched_chats"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)  # Telegram chat id (negative)
    title: Mapped[str] = mapped_column(String(255), default="")
    owner_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    worker_id: Mapped[int | None] = mapped_column(Integer, ForeignKey("workers.id"), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())

    owner: Mapped["User"] = relationship(back_populates="chats")
    recordings: Mapped[list["Recording"]] = relationship(back_populates="chat")


class Recording(Base):
    __tablename__ = "recordings"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    chat_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("watched_chats.id"))
    started_by_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))

    kind: Mapped[RecordingKind] = mapped_column(Enum(RecordingKind), default=RecordingKind.AUDIO)
    status: Mapped[RecordingStatus] = mapped_column(Enum(RecordingStatus), default=RecordingStatus.RECORDING)

    file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    storage_backend: Mapped[StorageBackend] = mapped_column(Enum(StorageBackend), default=StorageBackend.LOCAL)
    extra_files: Mapped[str] = mapped_column(String(2000), default="")  # JSON list, e.g. multitrack extras
    duration_seconds: Mapped[int] = mapped_column(Integer, default=0)
    file_size_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    participants_count: Mapped[int] = mapped_column(Integer, default=0)

    # Outbox pattern: the userbot process (userbot/) only ever writes DB
    # rows. The bot process (bot/), which owns the aiogram Bot instance,
    # polls for done-but-undelivered recordings and sends them. This is what
    # lets the two run as fully independent processes — see run_bot.py /
    # run_userbot.py — without one importing the other's runtime objects.
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)

    started_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    chat: Mapped["WatchedChat"] = relationship(back_populates="recordings")


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))

    plan_id: Mapped[str] = mapped_column(String(32))
    method: Mapped[PaymentMethod] = mapped_column(Enum(PaymentMethod))
    status: Mapped[PaymentStatus] = mapped_column(Enum(PaymentStatus), default=PaymentStatus.PENDING)

    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String(8))

    external_id: Mapped[str] = mapped_column(String(128), default="")  # provider invoice/payment id
    payload: Mapped[str] = mapped_column(String(64), default="", unique=True)  # internal correlation token

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    paid_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    user: Mapped["User"] = relationship(back_populates="payments")


class Subscription(Base):
    """A recurring Telegram Stars subscription — distinct from the one-off
    Payment rows placed via CryptoBot/YooKassa. Telegram owns the billing
    cycle: subscription_expiration_date from each successful_payment update
    is copied into current_period_end, never computed locally."""

    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    status: Mapped[SubscriptionStatus] = mapped_column(Enum(SubscriptionStatus), default=SubscriptionStatus.ACTIVE)

    # Charge id of the FIRST payment in the subscription — editUserStarSubscription
    # (Bot API) needs this specific id to cancel/reactivate.
    first_charge_id: Mapped[str] = mapped_column(String(128))
    latest_charge_id: Mapped[str] = mapped_column(String(128))

    stars_price: Mapped[int] = mapped_column(Integer)
    current_period_end: Mapped[datetime] = mapped_column(DateTime)
    charge_count: Mapped[int] = mapped_column(Integer, default=1)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class PromoCode(Base):
    __tablename__ = "promo_codes"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    bonus_days: Mapped[int] = mapped_column(Integer)
    max_uses: Mapped[int] = mapped_column(Integer, default=1)
    used_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_by: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class PromoRedemption(Base):
    __tablename__ = "promo_redemptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(32), ForeignKey("promo_codes.code"))
    user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("users.id"))
    redeemed_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class Worker(Base):
    """A recording account in the pool, added through the admin panel via
    the normal Telegram login flow (phone number -> SMS code -> 2FA
    password, exactly like logging into any Telegram client). Not tdata
    import — see userbot/pool.py / bot/handlers/workers.py.

    session_string is encrypted at rest (database/crypto_util.py) since it
    is equivalent to full account access, same sensitivity as a password."""

    __tablename__ = "workers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    label: Mapped[str] = mapped_column(String(64), default="")  # display name, defaults to phone number
    phone_number: Mapped[str] = mapped_column(String(32), default="")
    session_string_encrypted: Mapped[str | None] = mapped_column(String(4000), nullable=True)
    status: Mapped[WorkerStatus] = mapped_column(Enum(WorkerStatus), default=WorkerStatus.PENDING_CODE)

    max_chats: Mapped[int] = mapped_column(Integer, default=20)
    added_by: Mapped[int] = mapped_column(BigInteger)  # admin's Telegram user id

    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class ProfileUpdate(Base):
    """A requested name/bio/photo change for a worker account. The bot
    process only ever inserts rows here — it never touches a worker's live
    Pyrogram connection directly, since that connection only exists in the
    separate userbot process. userbot/pool.py polls for PENDING rows and
    applies them through the already-connected runtime client."""

    __tablename__ = "profile_updates"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[int] = mapped_column(Integer, ForeignKey("workers.id"))
    field: Mapped[str] = mapped_column(String(16))  # "first_name" | "bio" | "photo"
    value: Mapped[str] = mapped_column(String(1000))  # text, or a local file path for photo
    status: Mapped[ProfileUpdateStatus] = mapped_column(Enum(ProfileUpdateStatus), default=ProfileUpdateStatus.PENDING)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())


class JoinRequest(Base):
    """Customer supplies an invite link (or public @username); the owning
    worker joins it itself instead of asking the customer to go find and
    add a whole Telegram account to their group by hand. Same cross-process
    queue pattern as ProfileUpdate — the bot process only ever inserts a
    row, the userbot process (which owns the live connection) applies it
    and writes the result back."""

    __tablename__ = "join_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    worker_id: Mapped[int] = mapped_column(Integer, ForeignKey("workers.id"))
    invite_link: Mapped[str] = mapped_column(String(300))
    requested_by: Mapped[int] = mapped_column(BigInteger)  # customer's Telegram user id

    status: Mapped[JoinRequestStatus] = mapped_column(Enum(JoinRequestStatus), default=JoinRequestStatus.PENDING)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)  # bot process has told the customer the outcome

    result_chat_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    result_chat_title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    error: Mapped[str | None] = mapped_column(String(500), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.now())
