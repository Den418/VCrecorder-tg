from __future__ import annotations

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from config import DATABASE_URL
from database.models import Base

engine = create_async_engine(DATABASE_URL, echo=False, pool_pre_ping=True)
async_session = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)

if DATABASE_URL.startswith("sqlite"):
    # WAL lets readers and a writer work concurrently instead of locking the
    # whole file — needed now that the bot and userbot run as two separate
    # OS processes hitting the same SQLite file (see run_bot.py / run_userbot.py).
    # For real multi-instance production deployments, use PostgreSQL instead
    # (just change DATABASE_URL) — SQLite+WAL is fine for a single-host setup.
    @event.listens_for(engine.sync_engine, "connect")
    def _set_sqlite_pragma(dbapi_connection, _connection_record):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()


async def init_models() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
