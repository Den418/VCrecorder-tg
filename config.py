"""
Central configuration. Every value comes from the environment (see
.env.example) — nothing here should ever contain a real secret.
"""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = BASE_DIR / "storage"
RECORDINGS_DIR = STORAGE_DIR / "recordings"
RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)


def _bool(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _int(name: str, default: int) -> int:
    val = os.getenv(name)
    return int(val) if val else default


# --------------------------------------------------------------------------
# Bot (aiogram) — customer-facing shop / control panel, regular Bot API
# --------------------------------------------------------------------------
BOT_TOKEN: str = os.getenv("BOT_TOKEN", "")
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "")  # without @, used in deep links
ADMIN_IDS: list[int] = [int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip()]
SUPPORT_USERNAME: str = os.getenv("SUPPORT_USERNAME", "your_support")

# --------------------------------------------------------------------------
# Userbot (Pyrogram + pytgcalls) — the account that actually joins group
# voice/video chats in order to record them. Use a dedicated account, not
# your personal one: it will be visible as a call participant (by design,
# see userbot/recorder.py) and heavy automation carries some risk of limits.
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Userbot pool — API_ID/API_HASH identify the Telegram *application* shared
# by every worker account. Individual worker sessions are added through the
# admin panel (phone/code/2FA login), not via a single .env session — see
# database/models.py::Worker, userbot/pool.py.
# --------------------------------------------------------------------------
API_ID: int = _int("API_ID", 0)
API_HASH: str = os.getenv("API_HASH", "")

# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
DATABASE_URL: str = os.getenv("DATABASE_URL", f"sqlite+aiosqlite:///{STORAGE_DIR / 'bot.db'}")

# --------------------------------------------------------------------------
# Payments
# --------------------------------------------------------------------------
CRYPTOBOT_TOKEN: str = os.getenv("CRYPTOBOT_TOKEN", "")
CRYPTOBOT_TESTNET: bool = _bool("CRYPTOBOT_TESTNET", False)

YOOKASSA_SHOP_ID: str = os.getenv("YOOKASSA_SHOP_ID", "")
YOOKASSA_SECRET_KEY: str = os.getenv("YOOKASSA_SECRET_KEY", "")
YOOKASSA_RETURN_URL: str = os.getenv("YOOKASSA_RETURN_URL", "https://t.me/")

# Public HTTPS base URL the bot is reachable on, used to build webhook
# endpoints for CryptoBot / YooKassa (e.g. https://pay.example.com).
PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
WEBHOOK_HOST: str = os.getenv("WEBHOOK_HOST", "0.0.0.0")
WEBHOOK_PORT: int = _int("WEBHOOK_PORT", 8080)
# Random path segment mixed into the CryptoBot webhook URL so that random
# requests can't pretend to be a paid invoice before signature checking.
WEBHOOK_SECRET: str = os.getenv("WEBHOOK_SECRET", "change-me")

# --------------------------------------------------------------------------
# Worker pool — recording accounts added via the admin panel (official
# phone/code/2FA login, not tdata import). See database/crypto_util.py,
# userbot/pool.py.
# --------------------------------------------------------------------------
WORKER_ENCRYPTION_KEY: str = os.getenv("WORKER_ENCRYPTION_KEY", "")
WORKER_DEFAULT_MAX_CHATS: int = _int("WORKER_DEFAULT_MAX_CHATS", 20)
WORKER_RECONCILE_INTERVAL_SECONDS: int = _int("WORKER_RECONCILE_INTERVAL_SECONDS", 15)

# --------------------------------------------------------------------------
# Recording behaviour & plan limits
# --------------------------------------------------------------------------
# Recording a call without telling the people on it can violate wiretapping /
# data-protection law depending on jurisdiction. The bot always posts a
# message in the chat when it starts/stops recording — keep this on unless
# you have replaced it with your own compliant consent flow.
ANNOUNCE_RECORDING: bool = _bool("ANNOUNCE_RECORDING", True)

FREE_MAX_MINUTES: int = _int("FREE_MAX_MINUTES", 15)
FREE_MAX_CHATS: int = _int("FREE_MAX_CHATS", 1)
FREE_STORAGE_DAYS: int = _int("FREE_STORAGE_DAYS", 3)

PREMIUM_MAX_MINUTES: int = _int("PREMIUM_MAX_MINUTES", 180)
PREMIUM_MAX_CHATS: int = _int("PREMIUM_MAX_CHATS", 10)
PREMIUM_STORAGE_DAYS: int = _int("PREMIUM_STORAGE_DAYS", 30)

# --------------------------------------------------------------------------
# Referral program
# --------------------------------------------------------------------------
REFERRAL_SIGNUP_BONUS_DAYS: int = _int("REFERRAL_SIGNUP_BONUS_DAYS", 1)   # when a friend just joins via your link
REFERRAL_PURCHASE_BONUS_DAYS: int = _int("REFERRAL_PURCHASE_BONUS_DAYS", 7)  # when that friend makes their first purchase

# --------------------------------------------------------------------------
# S3-compatible storage for finished recordings (optional — AWS S3, MinIO,
# Cloudflare R2, Backblaze B2, etc. all speak this API). When unset,
# recordings simply stay on local disk under storage/recordings/.
# --------------------------------------------------------------------------
S3_ENABLED: bool = _bool("S3_ENABLED", False)
S3_ENDPOINT_URL: str = os.getenv("S3_ENDPOINT_URL", "")  # leave empty for real AWS S3
S3_BUCKET: str = os.getenv("S3_BUCKET", "")
S3_ACCESS_KEY: str = os.getenv("S3_ACCESS_KEY", "")
S3_SECRET_KEY: str = os.getenv("S3_SECRET_KEY", "")
S3_REGION: str = os.getenv("S3_REGION", "us-east-1")
S3_PRESIGNED_URL_TTL_SECONDS: int = _int("S3_PRESIGNED_URL_TTL_SECONDS", 3600)

# Telegram Bot API upload ceiling without a self-hosted Bot API server.
MAX_TELEGRAM_UPLOAD_BYTES: int = 50 * 1024 * 1024
