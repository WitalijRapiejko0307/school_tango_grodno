import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text

from app.channels.tracking_bot import TrackingBot
from app.channels.telegram import router as telegram_router, send_due_reminder
from app.config import get_settings
from app.db import Base, async_session_maker, engine
from app.services.gather import process_due_gathers
from app.services.notifications import mark_sent, plan_due

logger = logging.getLogger(__name__)

_storage = MemoryStorage()
_dispatcher = Dispatcher(storage=_storage)
_dispatcher.include_router(telegram_router)


async def _reminder_loop(bot: Bot) -> None:
    while True:
        try:
            async with async_session_maker() as session:
                now = datetime.now(UTC)
                due_list = await plan_due(session, now)
                for item in due_list:
                    if item.telegram_user_id is None:
                        continue
                    try:
                        await send_due_reminder(bot, item)
                        await mark_sent(
                            session,
                            item.reminder_id,
                            now,
                            nudge=item.kind == "attendance_nudge",
                        )
                    except Exception:
                        logger.exception(
                            "Failed to send reminder %s to %s",
                            item.reminder_id,
                            item.telegram_user_id,
                        )
                await session.commit()
        except Exception:
            logger.exception("Reminder loop iteration failed")
        await asyncio.sleep(60)


async def _gather_loop(bot: Bot) -> None:
    while True:
        try:
            async with async_session_maker() as session:
                now = datetime.now(UTC)
                await process_due_gathers(session, bot, now)
                await session.commit()
        except Exception:
            logger.exception("Gather loop iteration failed")
        await asyncio.sleep(60)


async def _register_telegram_webhook(bot: Bot, settings) -> None:
    public_url = settings.PUBLIC_URL.strip()
    if not public_url:
        return
    webhook_url = f"{public_url.rstrip('/')}/telegram/webhook"
    kwargs: dict[str, str] = {}
    if settings.WEBHOOK_SECRET:
        kwargs["secret_token"] = settings.WEBHOOK_SECRET
    await bot.set_webhook(webhook_url, **kwargs)
    logger.info("Telegram webhook registered for %s", webhook_url)


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        dialect = conn.engine.dialect.name
        if dialect == "postgresql":
            # create_all does not add columns to tables that already exist.
            await conn.execute(
                text(
                    "ALTER TABLE persons ADD COLUMN IF NOT EXISTS "
                    "name_key VARCHAR(255)"
                )
            )
            await conn.execute(
                text(
                    "CREATE INDEX IF NOT EXISTS ix_persons_name_key "
                    "ON persons (name_key)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE subscriptions "
                    "ALTER COLUMN valid_until DROP NOT NULL"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE groups ADD COLUMN IF NOT EXISTS "
                    "archived_at TIMESTAMPTZ"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE contact_cards ADD COLUMN IF NOT EXISTS "
                    "archived_at TIMESTAMPTZ"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE persons ADD COLUMN IF NOT EXISTS "
                    "archived_at TIMESTAMPTZ"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE attendances ADD COLUMN IF NOT EXISTS "
                    "marked_by_id VARCHAR(36)"
                )
            )
            # Archived entities can be deleted for good; dependents keep the id
            # and a snapshot in deleted_entities, so these FKs must not exist.
            await conn.execute(
                text(
                    "DO $$ DECLARE r record; BEGIN "
                    "FOR r IN SELECT c.conname, c.conrelid::regclass AS tbl "
                    "FROM pg_constraint c WHERE c.contype = 'f' AND "
                    "c.confrelid::regclass::text IN "
                    "('persons','groups','weekly_slots','products','prices') "
                    "LOOP EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I', "
                    "r.tbl, r.conname); END LOOP; END $$"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE products ADD COLUMN IF NOT EXISTS "
                    "audience VARCHAR(16) NOT NULL DEFAULT 'person'"
                )
            )
            await conn.execute(
                text(
                    "UPDATE products SET audience = 'couple' "
                    "WHERE name ILIKE '%семейная пара%' "
                    "AND audience = 'person'"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE subscriptions ADD COLUMN IF NOT EXISTS "
                    "partner_id VARCHAR(36)"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE day_gathers ALTER COLUMN stream_id DROP NOT NULL"
                )
            )
            await conn.execute(
                text(
                    "ALTER TABLE day_gathers "
                    "DROP CONSTRAINT IF EXISTS uq_day_gathers_person_stream_date"
                )
            )
            await conn.execute(
                text(
                    "DO $$ BEGIN "
                    "IF NOT EXISTS ("
                    "SELECT 1 FROM pg_constraint "
                    "WHERE conname = 'uq_day_gathers_person_date'"
                    ") THEN "
                    "ALTER TABLE day_gathers ADD CONSTRAINT "
                    "uq_day_gathers_person_date "
                    "UNIQUE (person_id, gather_date); "
                    "END IF; END $$"
                )
            )
            has_alembic = await conn.execute(
                text(
                    "SELECT 1 FROM information_schema.tables "
                    "WHERE table_name = 'alembic_version'"
                )
            )
        else:
            has_alembic = await conn.execute(
                text(
                    "SELECT 1 FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'alembic_version'"
                )
            )
        if has_alembic.first() is not None:
            await conn.execute(
                text(
                    "UPDATE alembic_version SET version_num = 'e0f1a2b3c4d5' "
                    "WHERE version_num IN ("
                    "'a1b2c3d4e5f6', 'b7c8d9e0f1a2', "
                    "'c8d9e0f1a2b3', 'd9e0f1a2b3c4'"
                    ")"
                )
            )

    bot: Bot | None = None
    reminder_task: asyncio.Task | None = None
    gather_task: asyncio.Task | None = None
    if settings.BOT_TOKEN:
        bot = TrackingBot(token=settings.BOT_TOKEN)
        if settings.PUBLIC_URL.strip():
            try:
                await _register_telegram_webhook(bot, settings)
            except Exception:
                logger.exception("Failed to register Telegram webhook")
        reminder_task = asyncio.create_task(_reminder_loop(bot))
        gather_task = asyncio.create_task(_gather_loop(bot))

    app.state.bot = bot
    app.state.dispatcher = _dispatcher

    yield

    for task in (reminder_task, gather_task):
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    if bot is not None:
        await bot.session.close()


app = FastAPI(title="TangoGrodno", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request) -> dict[str, bool]:
    bot: Bot | None = getattr(request.app.state, "bot", None)
    if bot is None:
        raise HTTPException(status_code=503, detail="Telegram bot is not configured")

    settings = get_settings()
    if settings.WEBHOOK_SECRET:
        header = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
        if header != settings.WEBHOOK_SECRET:
            raise HTTPException(status_code=401, detail="Invalid webhook secret")

    dispatcher: Dispatcher = request.app.state.dispatcher
    payload = await request.json()
    update = Update.model_validate(payload, context={"bot": bot})
    try:
        await dispatcher.feed_update(bot, update)
    except Exception:
        logger.exception("Telegram update failed")
    return {"ok": True}
